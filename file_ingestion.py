import hashlib
import re
import tempfile
import uuid
import zipfile
from datetime import date, datetime, time
from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".txt",
    ".md",
    ".markdown",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".json",
    ".yaml",
    ".yml",
    ".ps1",
    ".sh",
    ".bat",
    ".csv",
    ".html",
    ".css",
    ".xml",
    ".toml",
    ".ini",
    ".sql",
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".pdf",
    ".xlsx",
    ".docx",
}
MAX_FILES_PER_DROP = 10
MAX_TEXT_FILE_BYTES = 5 * 1024 * 1024
MAX_PDF_FILE_BYTES = 100 * 1024 * 1024
MAX_OFFICE_FILE_BYTES = 100 * 1024 * 1024
MAX_OFFICE_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_OFFICE_ARCHIVE_MEMBERS = 20_000
MAX_OFFICE_ROWS = 20_000
MAX_RECORDS_PER_DROP = 20_000
CHUNK_CHARACTERS = 18_000
MAX_CHUNKS_PER_UPLOAD_BATCH = 50
MAX_PDF_PAGES = 250
MAX_EXTRACTED_PDF_CHARACTERS = 900_000
MAX_EXTRACTED_OFFICE_CHARACTERS = 900_000
PRIVATE_KEY_MARKER = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----")
SECRET_ASSIGNMENT = re.compile(
    r"""(?im)^\s*(?:api[_-]?key|access[_-]?token|auth[_-]?token|password|passwd|client[_-]?secret|private[_-]?key)\s*[:=]\s*(['"]?)([^\s,'"#]+)\1\s*(?:#.*)?$"""
)
PLACEHOLDER_SECRET_VALUES = {
    "changeme",
    "example",
    "placeholder",
    "none",
    "null",
    "your-api-key",
    "your-password",
    "your-secret",
    "your-token",
}
SENSITIVE_NAME_MARKERS = (
    "credential",
    "secret",
    "private",
    "id_rsa",
    "id_ed25519",
    "token",
)


def _sensitive_reason(path: Path, content: str) -> str | None:
    filename = path.name.casefold()
    if filename == ".env" or filename.startswith(".env.") or filename.startswith(".env-"):
        return "environment files are skipped as they may contain credentials"
    if path.suffix.casefold() in {".pem", ".key", ".p12", ".pfx", ".jks"}:
        return "key and certificate files are not ingested"
    if any(marker in filename for marker in SENSITIVE_NAME_MARKERS):
        return "credential/private-key named files are skipped"
    if PRIVATE_KEY_MARKER.search(content):
        return "private-key material was detected"
    for match in SECRET_ASSIGNMENT.finditer(content):
        value = match.group(2).casefold().strip("'\"")
        if value not in PLACEHOLDER_SECRET_VALUES and not value.startswith(
            ("${", "$env:", "os.environ", "getenv(")
        ):
            return "a credential-like literal was detected"
    return None


def _split_into_chunks(content: str) -> list[tuple[str, int, int]]:
    chunks: list[tuple[str, int, int]] = []
    current: list[str] = []
    current_size = 0
    current_start = 1
    current_end = 0

    def flush() -> None:
        nonlocal current, current_size, current_start, current_end
        if current:
            chunks.append(("".join(current), current_start, current_end))
            current = []
            current_size = 0

    for line_number, line in enumerate(content.splitlines(keepends=True), start=1):
        if len(line) > CHUNK_CHARACTERS:
            flush()
            for offset in range(0, len(line), CHUNK_CHARACTERS):
                chunks.append(
                    (
                        line[offset : offset + CHUNK_CHARACTERS],
                        line_number,
                        line_number,
                    )
                )
            continue
        if current and current_size + len(line) > CHUNK_CHARACTERS:
            flush()
        if not current:
            current_start = line_number
        current.append(line)
        current_size += len(line)
        current_end = line_number

    flush()
    return chunks


def _read_pdf_pages(
    path: Path,
) -> tuple[list[tuple[int, str]], str | None]:
    try:
        import pymupdf as fitz
    except ImportError:
        return [], "PDF support is unavailable because PyMuPDF is not installed"

    try:
        with fitz.open(path) as document:
            if document.needs_pass:
                return [], "password-protected PDFs are not ingested"
            if document.page_count > MAX_PDF_PAGES:
                return [], f"PDF exceeds the {MAX_PDF_PAGES}-page limit"
            pages = []
            extracted_characters = 0
            for page_number, page in enumerate(document, start=1):
                text = page.get_text("text")
                extracted_characters += len(text)
                if extracted_characters > MAX_EXTRACTED_PDF_CHARACTERS:
                    return [], "PDF extracted text exceeds the per-file processing limit"
                pages.append((page_number, text))
    except Exception as error:
        return [], f"could not read PDF ({type(error).__name__})"

    text_pages = [(page_number, text) for page_number, text in pages if text.strip()]
    if not text_pages:
        return [], "PDF has no extractable text; OCR is not used"
    empty_pages = [
        str(page_number)
        for page_number, text in pages
        if not text.strip()
    ]
    warning = (
        f"pages {', '.join(empty_pages)} have no extractable text; OCR is not used"
        if empty_pages
        else None
    )
    return text_pages, warning


def validate_pdf_runtime() -> str:
    import pymupdf

    document = pymupdf.open()
    document.new_page().insert_text((72, 72), "PDF runtime validation")
    pdf_bytes = document.tobytes()
    document.close()
    with pymupdf.open(stream=pdf_bytes, filetype="pdf") as reopened:
        extracted = reopened[0].get_text("text")
    if "PDF runtime validation" not in extracted:
        raise RuntimeError("The PDF text extraction runtime did not return generated text.")
    return pymupdf.VersionBind


def validate_office_runtime() -> tuple[str, str]:
    from docx import Document
    from openpyxl import Workbook, __version__ as openpyxl_version
    from docx import __version__ as docx_version

    with tempfile.TemporaryDirectory(prefix="lancedb-office-check-") as directory:
        root = Path(directory)
        workbook_path = root / "runtime-check.xlsx"
        workbook = Workbook()
        workbook.active.append(["Name", "Value"])
        workbook.active.append(["Workbook check", "ready"])
        workbook.save(workbook_path)
        workbook.close()
        workbook_units, workbook_error, _ = _read_xlsx(workbook_path)

        document_path = root / "runtime-check.docx"
        document = Document()
        document.add_paragraph("Word runtime check")
        document.save(document_path)
        document_units, document_error, _ = _read_docx(document_path)

        large_document_path = root / "runtime-large-word-check.docx"
        large_document = Document()
        for index in range(600):
            large_document.add_paragraph(f"Runtime Word ingestion record {index + 1}.")
        large_document.save(large_document_path)
        large_records, large_skipped, large_confirmation = prepare_dropped_files(
            [large_document_path]
        )

    if workbook_error or not any("Workbook check" in text for _, text in workbook_units):
        raise RuntimeError(f"Excel extraction validation failed: {workbook_error or 'sample text missing'}")
    if document_error or not any("Word runtime check" in text for _, text in document_units):
        raise RuntimeError(f"Word extraction validation failed: {document_error or 'sample text missing'}")
    if (
        large_skipped
        or large_confirmation
        or len(large_records) != 600
        or not any("record 600" in record["content"] for record in large_records)
    ):
        raise RuntimeError(
            "Word ingestion validation failed for a document with more than 500 records."
        )
    return openpyxl_version, docx_version


def _validate_office_archive(path: Path) -> str | None:
    try:
        with zipfile.ZipFile(path) as archive:
            members = archive.infolist()
            if len(members) > MAX_OFFICE_ARCHIVE_MEMBERS:
                return "Office package exceeds the archive member limit"
            if any(member.flag_bits & 0x1 for member in members):
                return "password-protected Office files are not supported"
            if sum(member.file_size for member in members) > MAX_OFFICE_ARCHIVE_BYTES:
                return "Office package expands beyond the 256 MiB safety limit"
            if path.suffix.casefold() == ".xlsx" and any(
                name.casefold().endswith("vbaproject.bin")
                or "macrosheets/" in name.casefold()
                for name in archive.namelist()
            ):
                return "macro-enabled workbooks are not ingested"
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile, ValueError):
        return "could not read Office file (invalid or password-encrypted file)"
    return None


def _office_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value).strip()


def _read_xlsx(
    path: Path,
) -> tuple[list[tuple[str, str]], str | None, str]:
    archive_error = _validate_office_archive(path)
    if archive_error:
        return [], archive_error, ""

    try:
        from openpyxl import load_workbook

        workbook = load_workbook(
            path, read_only=True, data_only=False, keep_links=False
        )
        try:
            if len(workbook.sheetnames) > 10**9:
                return [], "workbook exceeds the 256-sheet processing limit", ""
            units: list[tuple[str, str]] = []
            raw_values: list[str] = []
            extracted_characters = 0
            record_count = 0
            processed_rows = 0
            truncated = False
            for worksheet in workbook.worksheets:
                headers: list[str] = []
                for row_number, row in enumerate(
                    worksheet.iter_rows(values_only=True), start=1
                ):
                    if not any(value is not None and str(value).strip() for value in row):
                        continue
                    processed_rows += 1
                    if processed_rows > MAX_OFFICE_ROWS:
                        truncated = True
                        break
                    values = [_office_value(value) for value in row]
                    raw_values.extend(value for value in values if value)
                    if row_number == 1:
                        headers = values
                    if row_number == 1 and headers:
                        fields = [
                            f"Column {index}: {value}"
                            for index, value in enumerate(values, start=1)
                            if value
                        ]
                    else:
                        fields = []
                        for index, value in enumerate(values, start=1):
                            if not value:
                                continue
                            header = (
                                headers[index - 1]
                                if index <= len(headers) and headers[index - 1]
                                else f"Column {index}"
                            )
                            fields.append(f"{header}: {value}")
                    if not fields:
                        continue
                    content = "\n".join(fields)
                    extracted_characters += len(content)
                    if extracted_characters > MAX_EXTRACTED_OFFICE_CHARACTERS:
                        return [], "workbook extracted text exceeds the per-file processing limit", ""
                    record_count += len(_split_into_chunks(content))
                    if record_count > MAX_RECORDS_PER_DROP:
                        return [], f"workbook exceeds the {MAX_RECORDS_PER_DROP}-record safety limit", ""
                    units.append(
                        (f"sheet {worksheet.title!r}; row {row_number}", content)
                    )
                if truncated:
                    break
        finally:
            workbook.close()
    except ImportError:
        return [], "Excel support is unavailable because openpyxl is not installed", ""
    except Exception as error:
        return [], f"could not read Excel workbook ({type(error).__name__})", ""

    warning = (
        f"workbook was limited to the first {MAX_OFFICE_ROWS} non-empty rows"
        if truncated
        else None
    )
    return units, warning, "\n".join(raw_values)


def _read_docx(
    path: Path,
) -> tuple[list[tuple[str, str]], str | None, str]:
    archive_error = _validate_office_archive(path)
    if archive_error:
        return [], archive_error, ""

    try:
        from docx import Document
        from docx.oxml.text.paragraph import CT_P
        from docx.oxml.table import CT_Tbl
        from docx.table import Table
        from docx.text.paragraph import Paragraph

        document = Document(path)
        units: list[tuple[str, str]] = []
        raw_values: list[str] = []
        extracted_characters = 0
        record_count = 0
        section_number = 1
        paragraph_number = 0
        table_number = 0
        truncated = False
        for child in document.element.body.iterchildren():
            if isinstance(child, CT_P):
                paragraph_number += 1
                text = Paragraph(child, document).text.strip()
                if text:
                    raw_values.append(text)
                    content = text
                    extracted_characters += len(content)
                    if extracted_characters > MAX_EXTRACTED_OFFICE_CHARACTERS:
                        return [], "Word document extracted text exceeds the per-file processing limit", ""
                    record_count += len(_split_into_chunks(content))
                    if record_count > MAX_RECORDS_PER_DROP:
                        return [], f"Word document exceeds the {MAX_RECORDS_PER_DROP}-record safety limit", ""
                    units.append(
                        (
                            f"section {section_number}; paragraph {paragraph_number}",
                            content,
                        )
                    )
                if child.pPr is not None and child.pPr.sectPr is not None:
                    section_number += 1
            elif isinstance(child, CT_Tbl):
                table_number += 1
                table = Table(child, document)
                for row_number, row in enumerate(table.rows, start=1):
                    if row_number > MAX_OFFICE_ROWS:
                        truncated = True
                        break
                    cells = [_office_value(cell.text) for cell in row.cells]
                    raw_values.extend(value for value in cells if value)
                    content = "\n".join(
                        f"Column {index}: {value}"
                        for index, value in enumerate(cells, start=1)
                        if value
                    )
                    if not content:
                        continue
                    extracted_characters += len(content)
                    if extracted_characters > MAX_EXTRACTED_OFFICE_CHARACTERS:
                        return [], "Word document extracted text exceeds the per-file processing limit", ""
                    record_count += len(_split_into_chunks(content))
                    if record_count > MAX_RECORDS_PER_DROP:
                        return [], f"Word document exceeds the {MAX_RECORDS_PER_DROP}-record safety limit", ""
                    units.append(
                        (
                            f"section {section_number}; table {table_number}; row {row_number}",
                            content,
                        )
                    )
                if truncated:
                    break
    except ImportError:
        return [], "Word support is unavailable because python-docx is not installed", ""
    except Exception as error:
        return [], f"could not read Word document ({type(error).__name__})", ""

    warning = (
        f"document tables were limited to {MAX_OFFICE_ROWS} rows per table"
        if truncated
        else None
    )
    return units, warning, "\n".join(raw_values)


def _file_fingerprint(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for block in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def prepare_dropped_files(
    paths: list[str | Path],
) -> tuple[list[dict[str, str]], list[str], list[dict[str, object]]]:
    """Read only explicitly dropped regular files; never execute or recurse."""
    records: list[dict[str, str]] = []
    skipped: list[str] = []
    confirmation_required: list[dict[str, object]] = []
    if len(paths) > MAX_FILES_PER_DROP:
        skipped.append(
            f"Only the first {MAX_FILES_PER_DROP} dropped files are considered."
        )
    for dropped_path in paths[:MAX_FILES_PER_DROP]:
        path = Path(dropped_path).expanduser()
        label = path.name or str(path)
        if path.is_symlink():
            skipped.append(f"{label}: symbolic links are not ingested.")
            continue
        if not path.is_file():
            skipped.append(f"{label}: folders and non-files are not ingested.")
            continue
        sensitive_reason = _sensitive_reason(path, "")
        if sensitive_reason:
            skipped.append(f"{label}: skipped because {sensitive_reason}.")
            continue
        if path.suffix.casefold() not in SUPPORTED_EXTENSIONS:
            skipped.append(f"{label}: unsupported file type.")
            continue
        try:
            size = path.stat().st_size
            suffix = path.suffix.casefold()
            size_limit = MAX_PDF_FILE_BYTES if suffix == ".pdf" else (
                MAX_OFFICE_FILE_BYTES
                if suffix in {".xlsx", ".docx"}
                else MAX_TEXT_FILE_BYTES
            )
            if size > size_limit:
                limit_label = "100 MB" if suffix in {".pdf", ".xlsx", ".docx"} else "5 MiB"
                skipped.append(f"{label}: file exceeds the {limit_label} limit.")
                continue
        except OSError as error:
            skipped.append(f"{label}: could not inspect file ({type(error).__name__}).")
            continue

        try:
            file_fingerprint = _file_fingerprint(path)
        except OSError as error:
            skipped.append(f"{label}: could not fingerprint file ({type(error).__name__}).")
            continue

        if path.suffix.casefold() == ".pdf":
            pages, pdf_error = _read_pdf_pages(path)
            if not pages:
                skipped.append(f"{label}: {pdf_error or 'no readable PDF text was found'}.")
                continue
            if pdf_error:
                skipped.append(f"{label}: {pdf_error}.")
            pdf_sensitive_reason = _sensitive_reason(
                path, "\n".join(content for _page_number, content in pages)
            )
            if pdf_sensitive_reason == "a credential-like literal was detected":
                sensitive_records = make_file_records(
                    label, pages, file_fingerprint=file_fingerprint
                )
                if len(records) + sum(
                    len(candidate["records"])
                    for candidate in confirmation_required
                ) + len(sensitive_records) > MAX_RECORDS_PER_DROP:
                    skipped.append(
                        f"{label}: skipped because this drop would exceed "
                        f"{MAX_RECORDS_PER_DROP} searchable records."
                    )
                    continue
                confirmation_required.append(
                    {
                        "file_name": label,
                        "reason": "credential-like literal detected",
                        "records": sensitive_records,
                    }
                )
                continue
            if pdf_sensitive_reason:
                skipped.append(f"{label}: skipped because {pdf_sensitive_reason}.")
                continue
            extracted_pages: list[tuple[int | None, str]] = pages
        elif path.suffix.casefold() in {".xlsx", ".docx"}:
            if path.suffix.casefold() == ".xlsx":
                office_units, office_warning, raw_content = _read_xlsx(path)
            else:
                office_units, office_warning, raw_content = _read_docx(path)
            if not office_units:
                skipped.append(f"{label}: {office_warning or 'file contains no readable text'}.")
                continue
            if office_warning:
                skipped.append(f"{label}: {office_warning}.")
            office_sensitive_reason = _sensitive_reason(path, raw_content)
            if office_sensitive_reason == "a credential-like literal was detected":
                sensitive_records = make_structured_file_records(
                    label, office_units, file_fingerprint=file_fingerprint
                )
                if len(records) + sum(
                    len(candidate["records"])
                    for candidate in confirmation_required
                ) + len(sensitive_records) > MAX_RECORDS_PER_DROP:
                    skipped.append(
                        f"{label}: skipped because this drop would exceed "
                        f"{MAX_RECORDS_PER_DROP} searchable records."
                    )
                    continue
                confirmation_required.append(
                    {
                        "file_name": label,
                        "reason": "credential-like literal detected",
                        "records": sensitive_records,
                    }
                )
                continue
            if office_sensitive_reason:
                skipped.append(f"{label}: skipped because {office_sensitive_reason}.")
                continue
            file_records = make_structured_file_records(
                label, office_units, file_fingerprint=file_fingerprint
            )
            if not file_records:
                skipped.append(f"{label}: no readable text was found.")
                continue
            if (
                len(records)
                + sum(len(candidate["records"]) for candidate in confirmation_required)
                + len(file_records)
                > MAX_RECORDS_PER_DROP
            ):
                skipped.append(
                    f"{label}: skipped because this drop would exceed "
                    f"{MAX_RECORDS_PER_DROP} searchable records."
                )
                continue
            records.extend(file_records)
            continue
        else:
            try:
                content = path.read_text(encoding="utf-8-sig")
            except (OSError, UnicodeError) as error:
                skipped.append(f"{label}: could not read UTF-8 text ({type(error).__name__}).")
                continue
            if not content.strip():
                skipped.append(f"{label}: file is empty.")
                continue
            sensitive_reason = _sensitive_reason(path, content)
            if sensitive_reason:
                skipped.append(f"{label}: skipped because {sensitive_reason}.")
                continue
            extracted_pages = [(None, content)]

        file_records = make_file_records(
            label, extracted_pages, file_fingerprint=file_fingerprint
        )

        if not file_records:
            skipped.append(f"{label}: no readable text was found.")
            continue
        if (
            len(records)
            + sum(len(candidate["records"]) for candidate in confirmation_required)
            + len(file_records)
            > MAX_RECORDS_PER_DROP
        ):
            skipped.append(
                f"{label}: skipped because this drop would exceed "
                f"{MAX_RECORDS_PER_DROP} searchable records."
            )
            continue
        records.extend(file_records)
    return records, skipped, confirmation_required


def make_file_records(
    label: str,
    pages: list[tuple[int | None, str]],
    file_fingerprint: str | None = None,
) -> list[dict[str, str]]:
    file_records: list[dict[str, str]] = []
    ingest_id = uuid.uuid4().hex if file_fingerprint else None
    for page_number, content in pages:
        page_chunks = _split_into_chunks(content)
        for index, (chunk, first_line, last_line) in enumerate(page_chunks, start=1):
            line_range = (
                f"line {first_line}"
                if first_line == last_line
                else f"lines {first_line}-{last_line}"
            )
            page_metadata = f"page {page_number}; " if page_number is not None else ""
            record = {
                "title": (
                    f"{label} ({page_metadata}{line_range}; "
                    f"chunk {index}/{len(page_chunks)})"
                ),
                "source": f"file-drop:{label}",
                "content": chunk,
            }
            if file_fingerprint:
                record["file_fingerprint"] = file_fingerprint
                record["ingest_id"] = ingest_id
            file_records.append(record)
    return file_records


def make_structured_file_records(
    label: str,
    units: list[tuple[str, str]],
    file_fingerprint: str | None = None,
) -> list[dict[str, str]]:
    file_records: list[dict[str, str]] = []
    ingest_id = uuid.uuid4().hex if file_fingerprint else None
    for metadata, content in units:
        chunks = _split_into_chunks(content)
        for index, (chunk, first_line, last_line) in enumerate(chunks, start=1):
            line_range = (
                f"line {first_line}"
                if first_line == last_line
                else f"lines {first_line}-{last_line}"
            )
            record = {
                "title": (
                    f"{label} ({metadata}; {line_range}; "
                    f"chunk {index}/{len(chunks)})"
                ),
                "source": f"file-drop:{label}",
                "content": chunk,
            }
            if file_fingerprint:
                record["file_fingerprint"] = file_fingerprint
                record["ingest_id"] = ingest_id
            file_records.append(record)
    return file_records
