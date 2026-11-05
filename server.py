import os
import json
import importlib
import hashlib
import re
import threading
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app_paths import application_data_directory
from progress_logging import append_progress_log

if os.environ.get("LANCEDB_RUNTIME_PATH"):
    from lancedb_runtime import activate_from_environment

    activate_from_environment()

lancedb = importlib.import_module("lancedb")
import pyarrow as pa
from lancedb.index import FTS
from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse


def application_directory() -> Path:
    if not getattr(os.sys, "frozen", False):
        return Path(__file__).resolve().parent
    executable = Path(os.sys.executable).resolve()
    if os.sys.platform == "darwin":
        return executable.parents[3]
    return executable.parent


APP_DIR = application_directory()
IS_MACOS_BUNDLE = getattr(os.sys, "frozen", False) and os.sys.platform == "darwin"
APP_DATA_DIR = application_data_directory(
    APP_DIR,
    Path(os.sys.executable),
    frozen=getattr(os.sys, "frozen", False),
    platform=os.sys.platform,
)
DATABASE_URI = Path(
    os.environ.get(
        "LANCEDB_URI",
        str(APP_DIR / "DATA"),
    )
).expanduser().resolve()
TABLE_NAME = "copilot_documents"
MAX_DOCUMENTS_PER_CALL = 50
MAX_DOCUMENT_LENGTH = 20_000
PORT = int(os.environ.get("LANCEDB_MCP_PORT", "8765"))
CLIENT_RECENCY_SECONDS = 300
_STORE_LOCK = threading.RLock()

DATABASE_URI.mkdir(parents=True, exist_ok=True)
db = lancedb.connect(str(DATABASE_URI))
mcp = FastMCP(
    "lancedb-local",
    host="127.0.0.1",
    port=PORT,
    streamable_http_path="/mcp",
)


def _record_progress(message: str) -> None:
    append_progress_log(APP_DATA_DIR / "logs" / "app.log", message)


class McpClientRegistry:
    def __init__(self, clock=None) -> None:
        self._lock = threading.Lock()
        self._clients: dict[str, dict[str, str]] = {}
        self._clock = clock or (lambda: datetime.now(timezone.utc))

    def register(self, session_id: str, info: dict[str, Any]) -> None:
        name = info.get("name")
        if not isinstance(name, str) or not name.strip():
            return
        now = self._clock().isoformat()
        title = info.get("title")
        version = info.get("version")
        with self._lock:
            self._clients[session_id] = {
                "name": name,
                "title": title if isinstance(title, str) and title else name,
                "version": version if isinstance(version, str) else "",
                "last_seen": now,
            }

    def touch(self, session_id: str | None) -> None:
        if not session_id:
            return
        now = self._clock().isoformat()
        with self._lock:
            client = self._clients.get(session_id)
            if client is not None:
                client["last_seen"] = now

    def remove(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._lock:
            self._clients.pop(session_id, None)

    def snapshot(self) -> list[dict[str, str]]:
        now = self._clock()
        with self._lock:
            expired = [
                session_id
                for session_id, client in self._clients.items()
                if (now - datetime.fromisoformat(client["last_seen"])).total_seconds()
                > CLIENT_RECENCY_SECONDS
            ]
            for session_id in expired:
                del self._clients[session_id]
            clients = [client.copy() for client in self._clients.values()]
        return sorted(clients, key=lambda client: client["name"].casefold())


client_registry = McpClientRegistry()


class McpClientTrackingMiddleware:
    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] != "/mcp":
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        session_id = headers.get(b"mcp-session-id", b"").decode("ascii", errors="ignore")
        method = scope.get("method", "").upper()
        client_info = None

        async def replay_body():
            body = bytearray()
            more_body = True
            while more_body:
                message = await receive()
                if message["type"] != "http.request":
                    continue
                body.extend(message.get("body", b""))
                more_body = message.get("more_body", False)
            return bytes(body)

        request_body = await replay_body() if method == "POST" else b""
        if request_body:
            try:
                request_data = json.loads(request_body)
                if request_data.get("method") == "initialize":
                    client_info = request_data.get("params", {}).get("clientInfo")
            except (AttributeError, UnicodeDecodeError, ValueError):
                client_info = None

        if method != "DELETE":
            client_registry.touch(session_id)

        body_sent = False

        async def replay_receive():
            nonlocal body_sent
            if method == "POST" and not body_sent:
                body_sent = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            return await receive()

        response_headers = None

        async def track_response(message):
            nonlocal response_headers
            if message["type"] == "http.response.start":
                response_headers = {
                    key.lower(): value for key, value in message.get("headers", [])
                }
            await send(message)

        await self.app(scope, replay_receive, track_response)

        if method == "DELETE":
            client_registry.remove(session_id)
        elif (
            client_info is not None
            and response_headers is not None
            and response_headers.get(b"mcp-session-id")
        ):
            assigned_id = response_headers[b"mcp-session-id"].decode("ascii", errors="ignore")
            client_registry.register(assigned_id, client_info)


_TABLE_CACHE: dict[int, Any] = {}


def _open_table(database):
    cache_key = id(database)
    table = _TABLE_CACHE.get(cache_key)
    if table is None:
        if TABLE_NAME not in database.list_tables().tables:
            return None
        table = database.open_table(TABLE_NAME)
        _TABLE_CACHE[cache_key] = table
    return table


def _search_database(
    database, query: str, limit: int
) -> list[dict[str, str | None]]:
    table = _open_table(database)
    if table is None:
        return []
    columns = ["content", "source", "created_at"]
    if "title" in table.schema.names:
        columns.append("title")
        search = table.search(
            query, query_type="fts", fts_columns=["content", "title"]
        )
    else:
        search = table.search(query, query_type="fts")
    results = search.select(columns).limit(limit).to_list()
    return [
        {
            "content": row["content"],
            "source": row["source"],
            "created_at": row["created_at"],
            **({"title": row["title"]} if row.get("title") else {}),
        }
        for row in results
    ]


def connected_apps() -> list[dict[str, str]]:
    return client_registry.snapshot()


def _document_key(row: dict[str, Any]) -> str:
    identity = [
        row.get("content"),
        row.get("source"),
        row.get("created_at"),
        row.get("title"),
    ]
    encoded = json.dumps(identity, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def list_ingested_documents(database=db) -> list[dict[str, Any]]:
    if TABLE_NAME not in database.list_tables().tables:
        return []

    table = database.open_table(TABLE_NAME)
    columns = ["content", "source", "created_at"]
    if "title" in table.schema.names:
        columns.append("title")
    rows = table.to_arrow().select(columns).to_pylist()
    documents: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = _document_key(row)
        document = documents.get(key)
        if document is None:
            document = {
                "id": key,
                "content": row.get("content"),
                "source": row.get("source"),
                "created_at": row.get("created_at"),
                "title": row.get("title"),
                "copies": 0,
            }
            documents[key] = document
        document["copies"] += 1
    return sorted(
        documents.values(),
        key=lambda document: (
            document["source"] or "",
            document["created_at"] or "",
            document["content"] or "",
        ),
    )


def _sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sql_equals(column: str, value: str | None) -> str:
    if value is None:
        return f"{column} IS NULL"
    return f"{column} = {_sql_string(value)}"


def delete_ingested_documents(document_ids: list[str], database=db) -> int:
    if not document_ids or any(not isinstance(key, str) or not key for key in document_ids):
        raise ValueError("Select at least one ingested document to delete.")
    if TABLE_NAME not in database.list_tables().tables:
        return 0

    documents = {document["id"]: document for document in list_ingested_documents(database)}
    selected_ids = set(document_ids)
    missing_ids = selected_ids.difference(documents)
    if missing_ids:
        raise ValueError("The document list is out of date. Refresh it and try again.")

    table = database.open_table(TABLE_NAME)
    before = len(table)
    try:
        for key in selected_ids:
            document = documents[key]
            predicate = " AND ".join(
                _sql_equals(column, document[column])
                for column in ("content", "source", "created_at", "title")
                if column in table.schema.names
            )
            table.delete(predicate)
    except Exception as error:
        _record_progress(
            f"Selected-record deletion failed ({type(error).__name__})."
        )
        raise
    deleted = before - len(table)
    _record_progress(f"Deleted {deleted} ingested record(s) from the active database.")
    return deleted


def clear_ingested_documents(database=db) -> int:
    if TABLE_NAME not in database.list_tables().tables:
        return 0

    table = database.open_table(TABLE_NAME)
    before = len(table)
    if before:
        table.delete("content IS NULL OR content IS NOT NULL")
    deleted = before - len(table)
    _record_progress(f"Cleared {deleted} ingested record(s) from the active database.")
    return deleted


@mcp.custom_route("/health", methods=["GET"], include_in_schema=False)
async def health_check(_request: Request) -> JSONResponse:
    try:
        tables = db.list_tables().tables
        documents = len(db.open_table(TABLE_NAME)) if TABLE_NAME in tables else 0
    except Exception as error:
        _record_progress(f"Database health check failed ({type(error).__name__}).")
        return JSONResponse(
            {
                "connected": False,
                "working": False,
                "database_uri": str(DATABASE_URI),
                "documents": 0,
                "connected_apps": connected_apps(),
                "error": str(error),
            },
            status_code=503,
        )
    return JSONResponse(
        {
            "connected": True,
            "working": True,
            "database_uri": str(DATABASE_URI),
            "documents": documents,
            "connected_apps": connected_apps(),
        }
    )


@mcp.custom_route("/documents", methods=["GET"], include_in_schema=False)
async def documents_route(_request: Request) -> JSONResponse:
    try:
        documents = list_ingested_documents()
    except Exception as error:
        _record_progress(f"Ingested-record listing failed ({type(error).__name__}).")
        raise
    return JSONResponse({"documents": documents})


@mcp.custom_route("/documents/delete", methods=["POST"], include_in_schema=False)
async def delete_documents_route(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        _record_progress("Record deletion rejected: request body was not valid JSON.")
        return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("ids"), list):
        _record_progress("Record deletion rejected: document IDs were not provided.")
        return JSONResponse({"error": "Provide a list of document IDs."}, status_code=400)
    try:
        deleted = delete_ingested_documents(payload["ids"])
    except ValueError as error:
        _record_progress(f"Record deletion rejected ({type(error).__name__}).")
        return JSONResponse({"error": str(error)}, status_code=400)
    return JSONResponse({"deleted": deleted})


def _validate_document(content: str, source: str, title: str | None = None) -> None:
    if not isinstance(content, str) or not content.strip():
        raise ValueError("Document content cannot be empty.")
    if len(content) > MAX_DOCUMENT_LENGTH:
        raise ValueError(
            f"Each document must be at most {MAX_DOCUMENT_LENGTH} characters."
        )
    if not isinstance(source, str) or len(source) > 500:
        raise ValueError("Source must be a string of at most 500 characters.")
    if title is not None and (
        not isinstance(title, str) or not title.strip() or len(title) > 500
    ):
        raise ValueError("Title must be a non-empty string of at most 500 characters.")


def _normalized_content(content: str) -> str:
    normalized = unicodedata.normalize("NFC", content.replace("\r\n", "\n").replace("\r", "\n"))
    return "\n".join(line.rstrip() for line in normalized.split("\n")).strip()


def _dedupe_key(content: str) -> str:
    normalized = _normalized_content(content)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _file_ingest_id(record: dict[str, str | None]) -> str | None:
    ingest_id = record.get("ingest_id")
    if ingest_id:
        return ingest_id
    fingerprint = record.get("file_fingerprint")
    if not fingerprint:
        return None
    source = record.get("source") or ""
    file_name = (
        source.removeprefix("file-drop:")
        if source.startswith("file-drop:")
        else (record.get("title") or "").split(" (", 1)[0]
    )
    identity = f"{fingerprint}\0{file_name}"
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]


def _ensure_dedupe_schema(table):
    for column in ("title", "dedupe_key", "file_fingerprint", "ingest_id"):
        if column not in table.schema.names:
            table.add_columns(pa.field(column, pa.string()))
            table = db.open_table(TABLE_NAME)
    return table


def _search_index_summaries(table) -> list[dict[str, Any]]:
    return [
        {
            "name": index.name,
            "type": index.index_type,
            "columns": list(index.columns),
            "indexed_rows": index.num_indexed_rows,
            "unindexed_rows": index.num_unindexed_rows,
        }
        for index in table.list_indices()
    ]


def optimize_database_on_startup(database=None) -> dict[str, Any]:
    if database is None:
        database = db
    if TABLE_NAME not in database.list_tables().tables:
        _record_progress("Database optimization skipped: document table does not exist yet.")
        return {"optimized": False, "reason": "table_missing", "indexes": []}

    with _STORE_LOCK:
        table = database.open_table(TABLE_NAME)
        if not len(table):
            _record_progress("Database optimization skipped: document table is empty.")
            return {"optimized": False, "reason": "table_empty", "indexes": []}

        try:
            table.optimize()
        except Exception as error:
            _record_progress(
                f"Database optimization failed ({type(error).__name__}); "
                "the MCP server will continue without optimization."
            )
            return {
                "optimized": False,
                "reason": "optimization_failed",
                "indexes": _search_index_summaries(table),
            }

        _TABLE_CACHE.pop(id(database), None)
        refreshed_table = database.open_table(TABLE_NAME)
        indexes = _search_index_summaries(refreshed_table)
        unindexed_rows = sum(index["unindexed_rows"] for index in indexes)
        _record_progress(
            "Database optimization completed; "
            f"unindexed rows across indexes: {unindexed_rows}."
        )
        return {"optimized": True, "indexes": indexes}


def _ensure_search_indexes(table) -> list[str]:
    existing_indexes = table.list_indices()
    created: list[str] = []
    for column in ("content", "title"):
        if column not in table.schema.names:
            continue
        field_type = table.schema.field(column).type
        if not (pa.types.is_string(field_type) or pa.types.is_large_string(field_type)):
            continue
        if any(
            index.index_type.casefold() == "fts" and column in index.columns
            for index in existing_indexes
        ):
            continue
        index_name = f"{column}_idx"
        create_options: dict[str, Any] = {"config": FTS(), "replace": False}
        if any(index.name == index_name for index in existing_indexes):
            create_options["name"] = f"{column}_fts_idx"
        try:
            table.create_index(column, **create_options)
        except Exception as error:
            _record_progress(
                f"Search index creation failed for {column} "
                f"({type(error).__name__})."
            )
            raise
        created.append(column)
        existing_indexes = table.list_indices()
    if created:
        _record_progress(f"Created FTS indexes for: {', '.join(created)}.")
    return created


def _store_records(records: list[dict[str, str | None]]) -> dict[str, int]:
    if not records:
        return {"stored": 0, "duplicates": 0}
    with _STORE_LOCK:
        table_was_created = TABLE_NAME not in db.list_tables().tables
        table = _ensure_dedupe_schema(open_or_create_table())
        existing_rows = table.to_arrow().to_pylist()
        existing_keys = {
            _dedupe_key(row.get("content") or "")
            for row in existing_rows
        }
        existing_fingerprints: dict[str, set[str | None]] = {}
        for row in existing_rows:
            fingerprint = row.get("file_fingerprint")
            if fingerprint:
                existing_fingerprints.setdefault(fingerprint, set()).add(
                    row.get("ingest_id")
                )
        seen_keys: set[str] = set()
        seen_fingerprints: dict[str, set[str | None]] = {}
        accepted: list[dict[str, str | None]] = []
        duplicates = 0
        for record in records:
            content = record["content"] or ""
            fingerprint = record.get("file_fingerprint")
            ingest_id = _file_ingest_id(record)
            key = _dedupe_key(content)
            prior_ingest_ids = (
                existing_fingerprints.get(fingerprint, set())
                | seen_fingerprints.get(fingerprint, set())
                if fingerprint
                else set()
            )
            if fingerprint and prior_ingest_ids and (
                ingest_id is None
                or any(previous_id != ingest_id for previous_id in prior_ingest_ids)
            ):
                duplicates += 1
                continue
            if key in existing_keys or key in seen_keys:
                duplicates += 1
                continue
            accepted.append(
                {**record, "ingest_id": ingest_id, "dedupe_key": key}
            )
            seen_keys.add(key)
            if fingerprint:
                seen_fingerprints.setdefault(fingerprint, set()).add(ingest_id)

        if not accepted:
            return {"stored": 0, "duplicates": duplicates}

        now = datetime.now(timezone.utc).isoformat()
        try:
            table.add(
                [
                    {
                        "content": record["content"],
                        "source": record["source"],
                        "created_at": now,
                        "title": record.get("title"),
                        "dedupe_key": record["dedupe_key"],
                        "file_fingerprint": record.get("file_fingerprint"),
                        "ingest_id": record.get("ingest_id"),
                    }
                    for record in accepted
                ]
            )
            if table_was_created:
                _ensure_search_indexes(table)
        except Exception as error:
            _record_progress(f"Document ingestion failed ({type(error).__name__}).")
            raise
        return {"stored": len(accepted), "duplicates": duplicates}


@mcp.custom_route("/documents/store", methods=["POST"], include_in_schema=False)
async def store_important_item_route(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "Provide an item with title, content, and source."}, status_code=400)
    try:
        title = payload.get("title")
        content = payload.get("content")
        source = payload.get("source", "dashboard")
        _validate_document(content, source, title)
        result = _store_records(
            [{"title": title.strip(), "content": content, "source": source}]
        )
    except ValueError as error:
        _record_progress(f"Important-item save rejected ({type(error).__name__}).")
        return JSONResponse({"error": str(error)}, status_code=400)
    _record_progress(
        f"Dashboard item save: stored {result['stored']}; skipped "
        f"{result['duplicates']} duplicate(s)."
    )
    return JSONResponse(result)


@mcp.custom_route("/documents/store-batch", methods=["POST"], include_in_schema=False)
async def store_documents_batch_route(request: Request) -> JSONResponse:
    try:
        payload = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "Request body must be valid JSON."}, status_code=400)
    if not isinstance(payload, dict) or not isinstance(payload.get("documents"), list):
        return JSONResponse({"error": "Provide a list of titled documents."}, status_code=400)
    documents = payload["documents"]
    if not documents or len(documents) > MAX_DOCUMENTS_PER_CALL:
        return JSONResponse(
            {"error": f"Provide between 1 and {MAX_DOCUMENTS_PER_CALL} documents."},
            status_code=400,
        )
    try:
        records = []
        for document in documents:
            if not isinstance(document, dict):
                raise ValueError("Each document must contain title, content, and source.")
            title = document.get("title")
            content = document.get("content")
            source = document.get("source", "file-drop")
            file_fingerprint = document.get("file_fingerprint")
            ingest_id = document.get("ingest_id")
            _validate_document(content, source, title)
            if file_fingerprint is not None and (
                not isinstance(file_fingerprint, str)
                or not re.fullmatch(r"[0-9a-f]{64}", file_fingerprint)
            ):
                raise ValueError("File fingerprint must be a SHA-256 hex digest.")
            if ingest_id is not None and (
                not isinstance(ingest_id, str)
                or not re.fullmatch(r"[0-9a-f]{32}", ingest_id)
            ):
                raise ValueError("Ingestion ID must be a 32-character hexadecimal value.")
            records.append(
                {
                    "title": title.strip(),
                    "content": content,
                    "source": source,
                    "file_fingerprint": file_fingerprint,
                    "ingest_id": ingest_id,
                }
            )
        result = _store_records(records)
    except ValueError as error:
        _record_progress(f"Batch document ingestion rejected ({type(error).__name__}).")
        return JSONResponse({"error": str(error)}, status_code=400)
    _record_progress(
        f"File batch ingestion: stored {result['stored']}; skipped "
        f"{result['duplicates']} duplicate chunk(s)."
    )
    return JSONResponse(result)


@mcp.custom_route("/documents/clear", methods=["POST"], include_in_schema=False)
async def clear_documents_route(_request: Request) -> JSONResponse:
    return JSONResponse({"deleted": clear_ingested_documents()})


def open_or_create_table():
    if TABLE_NAME in db.list_tables().tables:
        return db.open_table(TABLE_NAME)

    return db.create_table(
        TABLE_NAME,
        schema=pa.schema(
            [
                pa.field("content", pa.string()),
                pa.field("source", pa.string()),
                pa.field("created_at", pa.string()),
                pa.field("title", pa.string()),
                pa.field("dedupe_key", pa.string()),
                pa.field("file_fingerprint", pa.string()),
                pa.field("ingest_id", pa.string()),
            ]
        ),
    )


@mcp.tool()
def store_documents(
    documents: list[str], source: str = "copilot"
) -> str:
    """Persist user-provided scripts, code, and important notes in the local database.

    By default, save reusable scripts/code and important notes created for or
    provided by the user, and tell them after a successful save. Do not silently
    retain sensitive/private content without consent, or scan files/chat history;
    saving happens only when the client explicitly calls this tool.
    """
    if not documents:
        _record_progress("Document ingestion rejected: no documents were provided.")
        raise ValueError("Provide at least one document.")
    if len(documents) > MAX_DOCUMENTS_PER_CALL:
        _record_progress("Document ingestion rejected: document batch exceeded the limit.")
        raise ValueError(f"Store at most {MAX_DOCUMENTS_PER_CALL} documents per call.")
    try:
        for document in documents:
            _validate_document(document, source)
        result = _store_records(
            [
                {"title": None, "content": document, "source": source}
                for document in documents
            ]
        )
    except ValueError:
        _record_progress("Document ingestion rejected because an item was invalid.")
        raise
    _record_progress(
        f"Document batch: stored {result['stored']}; skipped "
        f"{result['duplicates']} duplicate(s)."
    )
    return (
        f"Stored {result['stored']} document(s) in LanceDB table '{TABLE_NAME}'; "
        f"skipped {result['duplicates']} exact duplicate(s)."
    )


@mcp.tool()
def store_important_item(title: str, content: str, source: str = "copilot") -> str:
    """Save an explicitly chosen script, code sample, or important note with title/source metadata.

    By default, save reusable scripts/code and important notes created for or
    provided by the user, and tell them after a successful save. Do not silently
    retain sensitive/private content without consent. Only the provided item is
    stored; this server does not intercept chats or scan files automatically.
    """
    try:
        _validate_document(content, source, title)
        result = _store_records(
            [{"title": title.strip(), "content": content, "source": source}]
        )
    except ValueError:
        _record_progress("Important-item save rejected because an item was invalid.")
        raise
    _record_progress(
        f"Important item: stored {result['stored']}; skipped "
        f"{result['duplicates']} duplicate(s)."
    )
    if result["duplicates"]:
        return f"Skipped '{title.strip()}': exact duplicate already exists locally."
    return f"Stored '{title.strip()}' in LanceDB table '{TABLE_NAME}'."


@mcp.tool()
def store_qa_item(question: str, answer: str, source: str = "copilot") -> str:
    """Cache a useful answer with its question so it can be found by a future local search.

    After search_documents reports a local miss and you answer using an
    otherwise-permitted source, call this tool with the question and answer to
    make the result available locally next time. Tell the user when it is saved.
    Never retain private or sensitive material without their consent. The client
    must call this tool; the server cannot capture conversations automatically.
    """
    if not isinstance(question, str) or not question.strip():
        raise ValueError("Question cannot be empty.")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Answer cannot be empty.")
    title = f"Q&A: {question.strip()[:495]}"
    content = f"Question: {question.strip()}\n\nAnswer: {answer.strip()}"
    _validate_document(content, source, title)
    result = _store_records([{"title": title, "content": content, "source": source}])
    _record_progress(
        f"Question-and-answer item: stored {result['stored']}; skipped "
        f"{result['duplicates']} duplicate(s)."
    )
    if result["duplicates"]:
        return "Skipped this question-and-answer item: an exact duplicate already exists locally."
    return f"Saved the answer to '{question.strip()}' for future local search."


@mcp.tool()
def search_documents(
    query: str, limit: int = 5
) -> dict[str, Any]:
    """Search local LanceDB first, then allow configured external fallback on a miss.

    Always use this tool before answering a retrieval question. If it finds local
    results, use them first. If it returns found=false, continue with the client's
    configured web, cloud, or AI sources when available, clearly identify those
    external sources, and optionally cache an appropriate answer with store_qa_item.
    This server itself never sends queries to external services.
    """
    if not query.strip():
        raise ValueError("Query cannot be empty.")
    if not 1 <= limit <= 20:
        raise ValueError("Limit must be between 1 and 20.")
    try:
        results = _search_database(db, query, limit)
    except Exception as error:
        _record_progress(f"Document search failed ({type(error).__name__}).")
        raise
    if results:
        _record_progress(f"Local document search returned {len(results)} result(s).")
        return {
            "found": True,
            "results": results,
            "message": f"Found {len(results)} match(es) in the local database.",
            "source": "local",
            "fallback_allowed": False,
        }
    _record_progress("Local document search found no matching records.")
    return {
        "found": False,
        "results": [],
        "message": (
            "Not found in local database. Continue with the client's configured "
            "web, cloud, or AI sources if available, and identify any external sources."
        ),
        "source": "local",
        "fallback_allowed": True,
    }


@mcp.tool()
def lancedb_status() -> dict[str, Any]:
    """Return the local LanceDB location and stored document count."""
    if TABLE_NAME not in db.list_tables().tables:
        return {
            "database_uri": str(DATABASE_URI),
            "table": TABLE_NAME,
            "documents": 0,
            "indexes": [],
        }

    table = db.open_table(TABLE_NAME)
    return {
        "database_uri": str(DATABASE_URI),
        "table": TABLE_NAME,
        "documents": len(table),
        "indexes": _search_index_summaries(table),
    }


@mcp.tool()
def list_search_indexes() -> dict[str, Any]:
    """List indexes on the local ingested-documents table without changing it."""
    if TABLE_NAME not in db.list_tables().tables:
        return {"table": TABLE_NAME, "table_exists": False, "indexes": []}
    table = db.open_table(TABLE_NAME)
    return {
        "table": TABLE_NAME,
        "table_exists": True,
        "indexes": _search_index_summaries(table),
    }


@mcp.tool()
def ensure_search_indexes() -> dict[str, Any]:
    """Create missing FTS indexes for searchable text columns without rebuilding existing indexes."""
    if TABLE_NAME not in db.list_tables().tables:
        return {
            "table": TABLE_NAME,
            "table_exists": False,
            "created": [],
            "indexes": [],
            "message": "Indexes will be initialized when the first document creates the table.",
        }
    with _STORE_LOCK:
        table = db.open_table(TABLE_NAME)
        created = _ensure_search_indexes(table)
        return {
            "table": TABLE_NAME,
            "table_exists": True,
            "created": created,
            "indexes": _search_index_summaries(table),
        }


@mcp.tool()
def list_tables() -> list[str]:
    """List the tables in this local LanceDB database."""
    return list(db.list_tables().tables)


@mcp.tool()
def connected_lancedb_apps() -> list[dict[str, str]]:
    """List currently connected MCP apps that have been active in the last five minutes."""
    return connected_apps()


def run_server() -> int:
    import uvicorn

    app = mcp.streamable_http_app()
    app.add_middleware(McpClientTrackingMiddleware)
    _record_progress("MCP server is starting.")
    optimize_database_on_startup()
    try:
        uvicorn.run(
            app,
            host="127.0.0.1",
            port=PORT,
            log_level="info",
            log_config=None,
            access_log=False,
        )
    except Exception as error:
        _record_progress(f"MCP server failed ({type(error).__name__}): {error}")
        raise
    return 0


if __name__ == "__main__":
    raise SystemExit(run_server())
