#!/bin/bash
set -Eeuo pipefail

APP_NAME="LANCEDB MCP SERVER"
APP_BUNDLE="$APP_NAME.app"
MIN_PYTHON="3.13"
BUILD_PYTHON=""
for candidate in python3.14 python3.13 python3; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; raise SystemExit(0 if (3, 13) <= sys.version_info[:2] < (3, 15) else 1)'; then
        BUILD_PYTHON="$candidate"
        break
    fi
done
if [[ -z "$BUILD_PYTHON" ]]; then
    echo "ERROR: The pinned PySide6 release requires Python 3.13 or 3.14." >&2
    exit 2
fi

usage() {
    cat <<'EOF'
Usage:
  ./Build\ LanceDB\ MCP\ Server\ for\ macOS.sh [SOURCE_PROJECT_DIR] [OUTPUT_BASE_DIR]

Defaults:
  SOURCE_PROJECT_DIR  ~/Desktop/lancedb mcp app
  OUTPUT_BASE_DIR     ~/Desktop/LANCE MCP SERVER

Builds directly into the selected output folder and refuses to replace an
existing folder.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi

SOURCE_DIR="${1:-$HOME/Desktop/lancedb mcp app}"
OUTPUT_BASE="${2:-$HOME/Desktop/LANCE MCP SERVER}"

if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: Native macOS .app builds must run on macOS; this host is not Darwin." >&2
    exit 2
fi

for tool in "$BUILD_PYTHON" rsync xcrun; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: Required tool '$tool' is missing." >&2
        echo "Install Xcode Command Line Tools with: xcode-select --install" >&2
        exit 2
    fi
done

if [[ ! -d "$SOURCE_DIR" ]]; then
    echo "ERROR: Source project folder not found: $SOURCE_DIR" >&2
    echo "Copy the 'lancedb mcp app' source folder from Windows to this Mac first." >&2
    exit 2
fi

SOURCE_DIR="$("$BUILD_PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve(strict=True))' "$SOURCE_DIR")"
OUTPUT_DIR="$("$BUILD_PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$OUTPUT_BASE")"
"$BUILD_PYTHON" - "$SOURCE_DIR" "$OUTPUT_DIR" <<'PY'
from pathlib import Path
import sys

source = Path(sys.argv[1])
output = Path(sys.argv[2])
if source == output or source in output.parents or output in source.parents:
    raise SystemExit("ERROR: Source and output folders must not contain one another.")
PY

for required in app_paths.py gui.py server.py file_ingestion.py lancedb_runtime.py requirements.txt; do
    if [[ ! -f "$SOURCE_DIR/$required" ]]; then
        echo "ERROR: Expected source file is missing: $SOURCE_DIR/$required" >&2
        exit 2
    fi
done

"$BUILD_PYTHON" - "$MIN_PYTHON" <<'PY'
import sys
minimum = tuple(map(int, sys.argv[1].split(".")))
actual = sys.version_info[:2]
if actual < minimum:
    raise SystemExit(
        f"ERROR: Python {sys.argv[1]} or newer is required; found "
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}."
    )
print(f"Using Python {sys.version.split()[0]} ({__import__('platform').machine()}).")
PY

xcrun --find clang >/dev/null

mkdir -p "$(dirname "$OUTPUT_DIR")"
if [[ -e "$OUTPUT_DIR" ]]; then
    echo "ERROR: Output folder already exists; refusing to overwrite it: $OUTPUT_DIR" >&2
    exit 2
fi
mkdir "$OUTPUT_DIR"
cp "$0" "$OUTPUT_DIR/Build LanceDB MCP Server for macOS.sh"

BUILD_DIR="$OUTPUT_DIR/.build"
BUILD_SOURCE="$BUILD_DIR/source"
VENV="$BUILD_DIR/.venv"
DIST_DIR="$OUTPUT_DIR"
WORK_DIR="$BUILD_DIR/pyinstaller-work"
APP_PATH="$DIST_DIR/$APP_BUNDLE"
BUILD_LOG="$BUILD_DIR/build.log"

mkdir -p "$BUILD_SOURCE" "$WORK_DIR"
echo "Output folder: $OUTPUT_DIR"
echo "Copying source without Windows Python runtime, databases, or sidecars..."
rsync -a \
    --exclude '/runtime/' \
    --exclude '/data/' \
    --exclude '/DATA/' \
    --exclude '/LANCEDB_DATA/' \
    --exclude '/RUNTIME/' \
    --exclude '/logs/' \
    --exclude '/*.data/' \
    --exclude '/*.runtime/' \
    --exclude '/__pycache__/' \
    --exclude '*.pyc' \
    --exclude '/.venv/' \
    --exclude '/.git/' \
    "$SOURCE_DIR/" "$BUILD_SOURCE/"

echo "Checking cross-platform paths and runtime support in the isolated source copy..."
"$BUILD_PYTHON" - "$BUILD_SOURCE" <<'PY'
from pathlib import Path
import re
import sys

root = Path(sys.argv[1])
gui_source = (root / "gui.py").read_text(encoding="utf-8")
server_source = (root / "server.py").read_text(encoding="utf-8")
if "PyMuPDF==" not in (root / "requirements.txt").read_text(encoding="utf-8"):
    raise SystemExit("ERROR: requirements.txt must include the PDF text-extraction dependency.")
runtime_source = (root / "lancedb_runtime.py").read_text(encoding="utf-8")
if not (
    "def application_directory() -> Path:" in gui_source
    and "def application_directory() -> Path:" in server_source
    and "if os.name == \"nt\":" in runtime_source
    and "sys.platform == \"win32\" and getattr(sys, \"frozen\", False)" in runtime_source
):
    raise SystemExit(
        "ERROR: Sources lack the cross-platform app paths and runtime lock; "
        "update the project source before building."
    )
print("Verified cross-platform app paths, runtime locks, and process flags.")
raise SystemExit(0)

gui_path = root / "gui.py"
gui = gui_path.read_text(encoding="utf-8")
gui_line = re.compile(
    r"^APP_DIR = Path\(sys\.executable\)\.resolve\(\)\.parent if "
    r"getattr\(sys, \"frozen\", False\) else Path\(__file__\)\.resolve\(\)\.parent$",
    re.MULTILINE,
)
gui_replacement = '''def application_directory() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        return executable.parents[3]
    return executable.parent


APP_DIR = application_directory()'''
gui, gui_count = gui_line.subn(gui_replacement, gui, count=1)
if gui_count != 1:
    raise SystemExit("ERROR: gui.py app-directory source did not match the expected version.")
gui_path.write_text(gui, encoding="utf-8")

server_path = root / "server.py"
server = server_path.read_text(encoding="utf-8")
server_block = re.compile(
    r"^APP_DIR = \(\n"
    r"    Path\(os\.sys\.executable\)\.resolve\(\)\.parent\n"
    r"    if getattr\(os\.sys, \"frozen\", False\)\n"
    r"    else Path\(__file__\)\.resolve\(\)\.parent\n"
    r"\)$",
    re.MULTILINE,
)
server_replacement = '''def application_directory() -> Path:
    if not getattr(os.sys, "frozen", False):
        return Path(__file__).resolve().parent
    executable = Path(os.sys.executable).resolve()
    if os.sys.platform == "darwin":
        return executable.parents[3]
    return executable.parent


APP_DIR = application_directory()'''
server, server_count = server_block.subn(server_replacement, server, count=1)
if server_count != 1:
    raise SystemExit("ERROR: server.py app-directory source did not match the expected version.")
server_path.write_text(server, encoding="utf-8")

runtime_path = root / "lancedb_runtime.py"
runtime = runtime_path.read_text(encoding="utf-8")
lock_block = re.compile(
    r"@contextmanager\n"
    r"def _exclusive_update_lock\(root: Path\):\n"
    r".*?(?=\ndef ensure_lancedb_runtime\()",
    re.DOTALL,
)
lock_replacement = '''@contextmanager
def _exclusive_update_lock(root: Path):
    lock_path = root / ".update.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(bytes([0]))
            lock_file.flush()

        if os.name == "nt":
            import msvcrt

            def acquire():
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)

            def release():
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            def acquire():
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

            def release():
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        deadline = time.monotonic() + 120
        while True:
            try:
                acquire()
                break
            except (OSError, BlockingIOError):
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        "Another app instance is still updating the LanceDB runtime."
                    )
                time.sleep(0.25)
        try:
            yield
        finally:
            release()

'''
runtime, lock_count = lock_block.subn(
    lambda _match: lock_replacement, runtime, count=1
)
if lock_count != 1:
    raise SystemExit("ERROR: runtime lock source did not match the expected version.")
runtime, flag_count = re.subn(
    r"subprocess\.CREATE_NO_WINDOW if getattr\(sys, \"frozen\", False\) else 0",
    r'subprocess.CREATE_NO_WINDOW if sys.platform == "win32" and '
    r'getattr(sys, "frozen", False) else 0',
    runtime,
    count=1,
)
if flag_count != 1:
    raise SystemExit("ERROR: runtime subprocess flag source did not match the expected version.")
stable_message_count = runtime.count("No stable Windows wheel was published")
runtime = runtime.replace(
    "No stable Windows wheel was published",
    "No stable compatible wheel was published",
)
compatibility_message_count = runtime.count(
    "PyPI has no wheel compatible with this Windows/Python runtime."
)
runtime = runtime.replace(
    "PyPI has no wheel compatible with this Windows/Python runtime.",
    "PyPI has no wheel compatible with this OS/Python runtime.",
)
if stable_message_count != 1 or compatibility_message_count != 1:
    raise SystemExit("ERROR: runtime platform messages did not match the expected source.")
runtime_path.write_text(runtime, encoding="utf-8")
print("Patched frozen app paths, the portable runtime lock, and Windows-only process flags.")
PY

echo "Checking ingested-record management support in the isolated source copy..."
"$BUILD_PYTHON" - "$BUILD_SOURCE" <<'PY'
from pathlib import Path
import sys

root = Path(sys.argv[1])
gui_source = (root / "gui.py").read_text(encoding="utf-8")
server_source = (root / "server.py").read_text(encoding="utf-8")
if not (
    "Ingested Documents" in gui_source
    and "Progress Log" in gui_source
    and "Drop text/code, PDF, Excel (.xlsx), or Word (.docx) files here to ingest" in gui_source
    and "def store_qa_item(" in server_source
    and "def store_documents_batch_route(" in server_source
    and "def ensure_search_indexes(" in server_source
    and "def list_search_indexes(" in server_source
    and (root / "file_ingestion.py").is_file()
    and "QMessageBox {" in gui_source
    and "LANCEDB MCP SERVER" in gui_source
    and 'DATABASE = APP_DIR / "DATA"' in gui_source
    and "def delete_ingested_documents(" in server_source
    and 'str(APP_DIR / "DATA")' in server_source
    and "append_progress_log" in gui_source
    and (root / "progress_logging.py").is_file()
):
    raise SystemExit(
        "ERROR: Sources lack the dashboard, record-deletion API, or progress logging support."
    )
if "PyMuPDF==" not in (root / "requirements.txt").read_text(encoding="utf-8"):
    raise SystemExit("ERROR: requirements.txt must include the PDF text-extraction dependency.")
requirements = (root / "requirements.txt").read_text(encoding="utf-8")
if "openpyxl==" not in requirements or "python-docx==" not in requirements:
    raise SystemExit("ERROR: requirements.txt must include the Excel and Word dependencies.")
print("Verified dashboard, record-deletion API, and progress logging support.")
raise SystemExit(0)

def replace_once(path, old, new, label):
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"ERROR: Expected one {label} block; found {count}.")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")

gui_path = root / "gui.py"
replace_once(
    gui_path,
    "    QPushButton,\n    QVBoxLayout,",
    "    QPushButton,\n    QPlainTextEdit,\n    QVBoxLayout,",
    "GUI text-log import",
)
replace_once(
    gui_path,
    '''if getattr(sys, "frozen", False):
    APP_DATA_DIR = APP_DIR / "LanceDB MCP App.data"
else:
    APP_DATA_DIR = APP_DIR

RUNTIME_DIR = APP_DIR / "runtime"
PYTHON = RUNTIME_DIR / "python.exe"
SERVER = APP_DIR / "server.py"
DATABASE = APP_DATA_DIR / "LANCEDB_DATA"''',
    '''APP_DATA_DIR = APP_DIR / "DATA"
RUNTIME_DIR = APP_DIR / "RUNTIME"
PYTHON = RUNTIME_DIR / "python.exe"
SERVER = APP_DIR / "server.py"
DATABASE = APP_DIR / "LANCEDB_DATA"''',
    "GUI sidecar paths",
)
replace_once(
    gui_path,
    "self.setMinimumSize(1040, 700)",
    "self.setMinimumSize(1040, 940)",
    "dashboard minimum size",
)
replace_once(
    gui_path,
    '''        layout.addWidget(health_panel, 2, 0)

        database_panel = QGroupBox("Database Folder")''',
    '''        layout.addWidget(health_panel, 2, 0)

        token_panel = QGroupBox("Database Token Usage")
        token_layout = QVBoxLayout(token_panel)
        self.database_tokens = QLabel("Tokens in database (est.): --")
        self.served_tokens = QLabel("Tokens returned by searches (est.): --")
        self.inserted_totals = QLabel("Data inserted: --")
        token_layout.addWidget(self.database_tokens)
        token_layout.addWidget(self.served_tokens)
        token_layout.addWidget(self.inserted_totals)
        layout.addWidget(token_panel, 3, 0)

        database_panel = QGroupBox("Database Folder")''',
    "database token panel",
)
replace_once(gui_path, "layout.addWidget(database_panel, 3, 0)", "layout.addWidget(database_panel, 4, 0)", "database folder row")
replace_once(gui_path, "layout.addWidget(endpoint_panel, 4, 0)", "layout.addWidget(endpoint_panel, 5, 0)", "endpoint row")
replace_once(gui_path, "layout.addLayout(actions, 5, 0)", "layout.addLayout(actions, 6, 0)", "actions row")
replace_once(gui_path, "layout.addWidget(clients_panel, 1, 1, 6, 1)", "layout.addWidget(clients_panel, 1, 1, 7, 1)", "client panel span")
replace_once(
    gui_path,
    '''        layout.addWidget(footer, 6, 0)

        layout.setColumnStretch(0, 3)''',
    '''        layout.addWidget(footer, 7, 0)

        log_panel = QGroupBox("Progress Log")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 16, 12, 12)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("progressLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(1200)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_panel, 8, 0, 1, 2)

        layout.setColumnStretch(0, 3)''',
    "dashboard progress log panel",
)
replace_once(
    gui_path,
    '''            QListWidget#clients {
                background: rgba(2, 17, 9, 145);''',
    '''            QPlainTextEdit#progressLog {
                background: rgba(2, 17, 9, 145);
                border: 1px solid rgba(172, 216, 184, 70);
                border-radius: 10px;
                padding: 8px;
                color: #f4fff6;
                font-family: Menlo, monospace;
                font-size: 12px;
            }
            QListWidget#clients {
                background: rgba(2, 17, 9, 145);''',
    "progress log styling",
)
replace_once(
    gui_path,
    '''        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_health)
        self.timer.start()''',
    '''        self.log_offsets = {}
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(800)
        self.log_timer.timeout.connect(self.refresh_logs)
        self.log_timer.start()
        self.refresh_logs()

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_health)
        self.timer.start()''',
    "live log refresh timer",
)
replace_once(
    gui_path,
    '''    @staticmethod
    def validate_database_folder(path):''',
    '''    def record_progress(self, message):
        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            with (LOGS / "app.log").open("a", encoding="utf-8") as log_file:
                log_file.write(
                    f"{datetime.now().astimezone().isoformat(timespec='seconds')} {message}\\n"
                )
        except OSError as error:
            self.status.setText(f"Could not write progress log: {error}")
        self.refresh_logs()

    def refresh_logs(self):
        for name in ("app.log", "runtime.log", "mcp-server.log", "mcp-server-error.log"):
            path = LOGS / name
            try:
                if not path.is_file():
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as log_file:
                    offset = self.log_offsets.get(path, 0)
                    if path.stat().st_size < offset:
                        offset = 0
                    log_file.seek(offset)
                    content = log_file.read()
                    self.log_offsets[path] = log_file.tell()
                if content:
                    for line in content.rstrip().splitlines():
                        self.log_view.appendPlainText(f"[{name}] {line}")
            except OSError as error:
                self.log_view.appendPlainText(f"[{name}] Could not read log: {error}")

    @staticmethod
    def validate_database_folder(path):''',
    "live progress log methods",
)
replace_once(
    gui_path,
    '''        self.document_count.setText(
            f"Documents in {self.database_path.name}: {health.get('documents', 0)}"
        )
        health_path = Path(health.get("database_uri", self.database_path))''',
    '''        self.document_count.setText(
            f"Documents in {self.database_path.name}: {health.get('documents', 0)}"
        )
        usage = health.get("usage", {})
        self.database_tokens.setText(
            f"Tokens in database (est.): {usage.get('tokens_stored_est', 0):,}"
        )
        self.served_tokens.setText(
            f"Tokens returned by searches (est.): {usage.get('tokens_served_est', 0):,}"
        )
        self.inserted_totals.setText(
            f"Data inserted: {usage.get('documents_inserted', 0):,} documents / "
            f"{usage.get('tokens_inserted_est', 0):,} tokens (est.)"
        )
        health_path = Path(health.get("database_uri", self.database_path))''',
    "live token metrics",
)
replace_once(
    gui_path,
    '''    def closeEvent(self, event):
        self.timer.stop()
        self.stop_server()''',
    '''    def closeEvent(self, event):
        self.timer.stop()
        self.log_timer.stop()
        self.stop_server()''',
    "log timer shutdown",
)
replace_once(
    gui_path,
    "for row in (1, 2, 4, 5, 6):",
    "for row in (1, 2, 3, 4, 5, 6, 7):",
    "dashboard row sizing",
)
replace_once(
    gui_path,
    '''        for row in (1, 2, 3, 4, 5, 6, 7):
            layout.setRowStretch(row, 0)''',
    '''        for row in (1, 2, 3, 4, 5, 6, 7):
            layout.setRowStretch(row, 0)
        layout.setRowStretch(8, 2)''',
    "progress log sizing",
)
replace_once(
    gui_path,
    '''        try:
            LOGS.mkdir(parents=True, exist_ok=True)
            self.database_path.mkdir(parents=True, exist_ok=True)''',
    '''        try:
            self.record_progress(f"Starting MCP server for database: {self.database_path}")
            LOGS.mkdir(parents=True, exist_ok=True)
            self.database_path.mkdir(parents=True, exist_ok=True)''',
    "server startup progress",
)
replace_once(
    gui_path,
    '''    def run(self):
        try:
            result = ensure_lancedb_runtime(
                APP_DIR,
                getattr(sys, "frozen", False),
                self.progress.emit,
            )
            self.completed.emit(result)
        except Exception as error:
            self.failed.emit(str(error))''',
    '''    def run(self):
        def report(message):
            try:
                LOGS.mkdir(parents=True, exist_ok=True)
                with (LOGS / "runtime.log").open("a", encoding="utf-8") as log_file:
                    log_file.write(
                        f"{datetime.now().astimezone().isoformat(timespec='seconds')} "
                        f"{message}\\n"
                    )
            except OSError as error:
                self.failed.emit(f"Could not write runtime progress log: {error}")
                return
            self.progress.emit(message)

        try:
            report("Checking LanceDB runtime compatibility and updates.")
            result = ensure_lancedb_runtime(
                APP_DIR,
                getattr(sys, "frozen", False),
                report,
            )
            report(f"LanceDB runtime ready: {result}")
            self.completed.emit(result)
        except Exception as error:
            report(f"Runtime preparation failed: {error}")
            self.failed.emit(str(error))''',
    "runtime progress logging",
)
server_path = root / "server.py"
replace_once(
    server_path,
    "import os\nimport json\nimport importlib",
    "import os\nimport json\nimport hashlib\nimport importlib",
    "database usage hash import",
)
replace_once(
    server_path,
    '''APP_DATA_DIR = (
    APP_DIR / f"{Path(os.sys.executable).stem}.data"
    if getattr(os.sys, "frozen", False)
    else APP_DIR
)

DATABASE_URI = Path(os.environ.get("LANCEDB_URI", str(APP_DATA_DIR / "LANCEDB_DATA"))).expanduser().resolve()''',
    '''APP_DATA_DIR = APP_DIR / "DATA"

DATABASE_URI = Path(
    os.environ.get("LANCEDB_URI", str(APP_DIR / "LANCEDB_DATA"))
).expanduser().resolve()
USAGE_FILE = APP_DATA_DIR / (
    "database-usage-" + hashlib.sha256(str(DATABASE_URI).encode("utf-8")).hexdigest()[:16] + ".json"
)''',
    "server sidecar paths",
)
replace_once(
    server_path,
    '''DATABASE_URI.mkdir(parents=True, exist_ok=True)
db = lancedb.connect(str(DATABASE_URI))
mcp = FastMCP("lancedb-local", host="127.0.0.1", port=PORT, streamable_http_path="/mcp")''',
    '''APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URI.mkdir(parents=True, exist_ok=True)
db = lancedb.connect(str(DATABASE_URI))
mcp = FastMCP("lancedb-local", host="127.0.0.1", port=PORT, streamable_http_path="/mcp")

_usage_lock = threading.Lock()
_usage_fields = (
    "tokens_stored_est",
    "tokens_served_est",
    "documents_inserted",
    "tokens_inserted_est",
    "documents_total",
)


def _estimate_tokens(text: str) -> int:
    return (len(text) + 3) // 4


def _save_usage_locked() -> None:
    temporary = USAGE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(_usage, indent=2) + "\\n", encoding="utf-8")
    temporary.replace(USAGE_FILE)


def _initial_usage() -> dict[str, int]:
    if USAGE_FILE.is_file():
        loaded = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid database usage file: {USAGE_FILE}")
        return {
            field: max(0, int(loaded.get(field, 0)))
            for field in _usage_fields
        }

    documents = 0
    tokens = 0
    if TABLE_NAME in db.list_tables().tables:
        table = db.open_table(TABLE_NAME)
        for batch in table.to_batches(columns=["content"], batch_size=1024):
            contents = batch.column("content").to_pylist()
            documents += len(contents)
            tokens += sum(_estimate_tokens(content or "") for content in contents)
    return {
        "tokens_stored_est": tokens,
        "tokens_served_est": 0,
        "documents_inserted": 0,
        "tokens_inserted_est": 0,
        "documents_total": documents,
    }


_usage = _initial_usage()
with _usage_lock:
    _save_usage_locked()


def database_usage() -> dict[str, int]:
    with _usage_lock:
        return dict(_usage)''',
    "database usage tracking",
)
replace_once(
    server_path,
    '''                "connected_apps": connected_apps(),
                "error": str(error),''',
    '''                "connected_apps": connected_apps(),
                "usage": database_usage(),
                "error": str(error),''',
    "health error usage metrics",
)
replace_once(
    server_path,
    '''            "documents": documents,
            "connected_apps": connected_apps(),
        }''',
    '''            "documents": documents,
            "connected_apps": connected_apps(),
            "usage": database_usage(),
        }''',
    "health usage metrics",
)
replace_once(
    server_path,
    '''    table.add(
        [
            {"content": document, "source": source, "created_at": now}
            for document in documents
        ]
    )
    table.create_index("content", config=FTS(), replace=True)''',
    '''    table.add(
        [
            {"content": document, "source": source, "created_at": now}
            for document in documents
        ]
    )
    inserted_tokens = sum(_estimate_tokens(document) for document in documents)
    with _usage_lock:
        _usage["tokens_stored_est"] += inserted_tokens
        _usage["documents_total"] += len(documents)
        _usage["documents_inserted"] += len(documents)
        _usage["tokens_inserted_est"] += inserted_tokens
        _save_usage_locked()
    print(
        f"Stored {len(documents)} document(s), about {inserted_tokens} tokens.",
        flush=True,
    )
    table.create_index("content", config=FTS(), replace=True)''',
    "insert token accounting",
)
replace_once(
    server_path,
    '''    return search_local_then_cloud(query, limit, db, _get_cloud_database)''',
    '''    results = search_local_then_cloud(query, limit, db, _get_cloud_database)
    tokens_served = sum(_estimate_tokens(row.get("content", "")) for row in results)
    with _usage_lock:
        _usage["tokens_served_est"] += tokens_served
        _save_usage_locked()
    print(
        f"Search returned {len(results)} document(s), about {tokens_served} tokens.",
        flush=True,
    )
    return results''',
    "search token accounting",
)
PY
echo "Creating an isolated Python environment..."
"$BUILD_PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$BUILD_SOURCE/requirements.txt"
"$VENV/bin/python" -m pip install "pyinstaller==6.22.3"

echo "Checking Python syntax..."
"$VENV/bin/python" -m compileall -q \
    "$BUILD_SOURCE/gui.py" \
    "$BUILD_SOURCE/server.py" \
    "$BUILD_SOURCE/file_ingestion.py" \
    "$BUILD_SOURCE/lancedb_runtime.py"

echo "Creating a bundled dependency-version manifest..."
"$VENV/bin/python" - "$BUILD_SOURCE" "$BUILD_DIR/package_versions.json" <<'PY'
from importlib import metadata
from pathlib import Path
import json
import re
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
required = set()
for line in (source / "requirements.txt").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if not line or line.startswith("#"):
        continue
    name = re.split(r"[<>=!~; ]", line, maxsplit=1)[0]
    required.add(re.sub(r"[-_.]+", "-", name).lower())

versions = {}
for distribution in metadata.distributions():
    name = distribution.metadata.get("Name")
    if not name:
        continue
    normalized = re.sub(r"[-_.]+", "-", name).lower()
    if normalized in required and normalized != "lancedb" and not normalized.startswith("lance-"):
        versions[normalized] = distribution.version
destination.write_text(json.dumps(versions, indent=2) + "\n", encoding="utf-8")
print(f"Recorded {len(versions)} bundled dependency versions.")
PY

echo "Building the native macOS app bundle..."
(
    cd "$BUILD_SOURCE"
    "$VENV/bin/python" -m PyInstaller \
        --noconfirm \
        --clean \
        --windowed \
        --name "$APP_NAME" \
        --osx-bundle-identifier "com.local.lancedb-mcp-server" \
        --icon "$BUILD_SOURCE/assets/lancedb-mcp-server.icns" \
        --add-data "$BUILD_SOURCE/assets/lancedb-mcp-server.png:assets" \
        --add-data "$BUILD_SOURCE/assets/lancedb-mcp-server.icns:assets" \
        --distpath "$DIST_DIR" \
        --workpath "$WORK_DIR" \
        --specpath "$WORK_DIR" \
        --hidden-import server \
        --hidden-import packaging \
        --hidden-import symtable \
        --collect-all packaging \
        --collect-all colorama \
        --collect-all deprecation \
        --collect-all pydantic \
        --collect-all tqdm \
        --collect-all dateutil \
        --collect-all typing_extensions \
        --collect-all urllib3 \
        --exclude-module lancedb \
        --exclude-module lance_namespace \
        --exclude-module lance_namespace_urllib3_client \
        --add-data "$BUILD_DIR/package_versions.json:." \
        --collect-submodules mcp.server \
        --collect-all pyarrow \
        --collect-all pymupdf \
        --collect-all openpyxl \
        --collect-all et_xmlfile \
        --collect-all docx \
        --collect-all lxml \
        "$BUILD_SOURCE/gui.py"
) 2>&1 | tee "$BUILD_LOG"

if [[ ! -d "$APP_PATH" ]]; then
    echo "ERROR: PyInstaller did not produce the expected app bundle: $APP_PATH" >&2
    exit 1
fi

echo "Validating the bundled PyMuPDF text-extraction runtime..."
"$APP_PATH/Contents/MacOS/$APP_NAME" --validate-pdf-runtime

echo "Validating the bundled Excel/Word text-extraction runtime..."
"$APP_PATH/Contents/MacOS/$APP_NAME" --validate-office-runtime

echo "Creating the database and runtime folders (no database files are copied or removed)..."
mkdir "$OUTPUT_DIR/DATA" "$OUTPUT_DIR/RUNTIME"

echo
echo "Build completed."
echo "App:     $APP_PATH"
echo "Runtime: $OUTPUT_DIR/RUNTIME"
echo "Database: $OUTPUT_DIR/DATA"
echo "Bundle architecture: $(uname -m)"
echo
echo "Keep the .app, DATA, and RUNTIME together in this folder."
echo "First launch needs network access to install the external LanceDB runtime."
echo "See the companion macOS build guide for launch, MCP health checks, and signing."
