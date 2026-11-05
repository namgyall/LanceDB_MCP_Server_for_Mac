# LANCEDB MCP SERVER

A cross-platform app with a PySide6 dashboard and local MCP server. On Windows, the launcher provides a portable Python runtime; macOS builds are native `.app` bundles. The dashboard opens maximized with a monitor-sized normal window geometry, so the native title-bar zoom/restore action returns to a usable size; its contents scroll on smaller displays. On Windows it uses a white background over the acrylic backdrop, dark readable labels, glossy dark-green capsule buttons with white text, and a right-side panel for connected MCP clients.

## Start the app

Run `Run.bat`. On first launch, the launcher automatically downloads the Python embeddable runtime and installs the MCP server, PySide6, and their dependencies from `requirements.txt` into this folder if needed; this requires an internet connection. LanceDB itself is kept outside the app runtime and is downloaded/updated by the app into the shared sidecar described below. If the local GUI runtime is already installed and valid, setup verifies it without downloading packages. Setup progress and errors are shown in the launcher console. To force a dependency repair from PowerShell, run `.\setup.ps1 -Force`. The UI is a native PySide6/Qt for Python application running on the app-local `pythonw.exe`; no global Python, .NET, or Qt install is needed. `build.ps1` prepares or repairs these app-local dependencies.

At each app launch, a small startup dialog checks official PyPI metadata for the latest stable compatible LanceDB and its Lance runtime dependencies. The runtime is not downloaded when installed versions are current. When missing or outdated, compatible wheels are downloaded over HTTPS, checked against PyPI's SHA-256 digests and dependency metadata, and import-tested before the active pointer is atomically switched. The standalone EXE does not bundle LanceDB: its external runtime lives in the `RUNTIME` folder beside the EXE (`D:\Desktop\RUNTIME\versions\...\Lib\site-packages`). In folder/source mode, the matching runtime is a sibling `<app-folder>.runtime`. Previous runtime versions are retained for rollback. If the network is unavailable or a release fails validation, the app continues with the last verified runtime; on a first install with no verified runtime, it shows an error rather than starting with a broken package.

The desktop app starts a local Streamable HTTP MCP server automatically. It binds only to `127.0.0.1` and uses `http://127.0.0.1:8765/mcp`. The **Connect LanceDB** button starts or checks the connection; the dashboard reports whether LanceDB is connected and working, along with the saved document count. In the **Database Folder** panel, select **Browse** to choose an existing LanceDB database folder. The address display is blank until a folder is selected; the app validates the chosen folder, switches the MCP server to it without moving or copying data, and remembers the selection for the next launch. With no saved selection, the server uses its app-local data folder internally. Keep the app open while GitHub Copilot is using the database.

## Build a standalone Windows executable

On the Windows build machine, run `.\build-exe.ps1` after installing PyInstaller in the build Python environment. By default it creates `D:\Desktop\LanceDB MCP App.exe`, a self-extracting executable that bundles the GUI, MCP server, Python, PySide6, and the non-LanceDB dependencies. To rebuild the existing `LanceDB MCP Server` distribution in place while preserving its adjacent data/runtime folders, run:

```powershell
$env:LANCEDB_BUILD_NAME = "LanceDB MCP Server"
.\build-exe.ps1 -OutputDirectory "D:\Desktop\LANCEDB1"
Remove-Item Env:LANCEDB_BUILD_NAME
```

The resulting executable is `D:\Desktop\LANCEDB1\LanceDB MCP Server.exe`. Windows does not need a separate Python installation to run it. Executable size varies with the bundled dependency versions.

Keep the adjacent `DATA` and `RUNTIME` folders with the executable. The LanceDB database is stored directly in `DATA` (not in a nested folder), while `RUNTIME` stores independently updateable LanceDB software. New frozen Windows installs keep settings, logs, and Copilot endpoint configuration under `%LOCALAPPDATA%\LANCEDB MCP SERVER`; if the matching executable-adjacent legacy `.data` folder already exists, it is reused without replacing or deleting it. The app does not create a new `.data` folder beside the executable. PyInstaller one-file extraction uses its normal transient `_MEI...` directory under the operating system's temporary folder, not beside the executable; this folder is separate from the persistent `DATA` and `RUNTIME` directories. Move or back up the executable and `DATA`/`RUNTIME` folders together. The build script does not copy or replace database contents. Existing saved database selections remain active; the app does not silently migrate or merge databases.

## Build the native macOS app

Native `.app` bundles must be built on a Mac. Copy this project folder to the Mac, keep the build script beside `gui.py`, `server.py`, `lancedb_runtime.py`, and `requirements.txt`, and install Python 3.13 or 3.14 plus the Xcode Command Line Tools. Then run from Terminal:

```sh
cd "$HOME/Desktop/lancedb mcp app"
chmod +x "Build LanceDB MCP Server for macOS.sh"
./Build\ LanceDB\ MCP\ Server\ for\ macOS.sh
```

The script defaults to `~/Desktop/LANCE MCP SERVER`; optional arguments select the source project folder and output folder. It refuses to overwrite an existing output folder. The output contains `LANCEDB MCP SERVER.app`, `DATA`, and `RUNTIME`; keep them together. LanceDB stores tables directly under `DATA` without a nested database directory. The build excludes source databases and Windows runtime files, and does not delete, copy, or replace any existing database. First launch needs network access to install the compatible LanceDB runtime. Existing saved database selections remain active; to intentionally switch databases, use **Browse** and choose a valid existing LanceDB folder after backing up its contents.

## Connect GitHub Copilot

In the GitHub Copilot desktop app, add an HTTP MCP server with the endpoint shown in the app. The app can copy the endpoint. The same configuration is also written to `copilot-mcp.json`:

```json
{
  "servers": {
    "lancedb-local": {
      "type": "http",
      "url": "http://127.0.0.1:8765/mcp"
    }
  }
}
```

If Copilot asks for the server URL directly, use `http://127.0.0.1:8765/mcp`. Copilot's MCP settings are managed by Copilot; this app does not edit your user configuration automatically.

## Data and tools

The app's default LanceDB database is stored directly in `DATA` beside the executable (or beside the `.app` bundle on macOS); source mode uses the app folder's `DATA`. The app creates `DATA` automatically if it is missing. Existing saved database selections are intentionally preserved and displayed in the **Database Folder** field instead of being silently replaced or migrated; the active URI in the connection health check and the field show the actual folder being used. The Database Folder panel uses a solid light background, a high-contrast dark path field, and a readable Browse button. On frozen Windows builds, new settings/logs/configuration are stored under `%LOCALAPPDATA%\LANCEDB MCP SERVER`; an existing adjacent legacy `.data` folder is reused if present, but no new adjacent `.data` folder is created. The PySide6 GUI starts and stops the local MCP server and exposes connection health, database location, document count, active MCP clients, an **Ingested Documents** panel, and a live **Progress Log**. The equal-height connected-client and ingested-document panels form a right-side stack that extends to the bottom of the window; the taller progress log occupies the bottom of the left column. The log shows runtime preparation, server startup/health, database changes, and errors; the live display retains at most 250 lines and truncates each displayed line to 240 characters, while persisted log files remain unchanged. Log entries omit document text and redact common credential fields. The MCP server exposes:

- `store_important_item`: explicitly save one script, code sample, or important note with a title and source label.
- `store_qa_item`: save a question and its answer together so a later local search can find the answer after a local miss.
- `store_documents`: explicitly save up to 50 text documents per call.
- File drop: drag supported local text/code files, text-based PDFs, `.xlsx` workbooks, or `.docx` documents into the Ingested Documents panel.
- `search_documents`: search the active local LanceDB database first; local results take precedence, and a miss tells the connected client to use its configured web, cloud, or AI sources if available.
- `lancedb_status`: return the database location, table, document count, and index metadata.
- `list_search_indexes`: list local table indexes without modifying the database.
- `ensure_search_indexes`: explicitly create any missing FTS indexes for existing searchable text columns; existing indexes are never replaced.
- `list_tables`: list database tables.

The server output and errors are written to `logs\mcp-server.log` and `logs\mcp-server-error.log`; the dashboard's bounded live progress view reads `logs\app.log` and `logs\runtime.log`. On frozen Windows builds, these configuration and log files are under `%LOCALAPPDATA%\LANCEDB MCP SERVER`, unless an existing adjacent legacy `.data` folder is being reused; on macOS they are in `DATA` alongside the database's LanceDB files. Use **Restart server** in the app if you change the server or database settings. Closing the app stops its MCP server.

The **Ingested Documents** panel provides a **Drop text/code/PDF files here to ingest** drop zone, **Save Important Item**, and **Refresh** controls. Drop files over the panel text or its border; the app accepts local-file drag enter, move, and drop events. Dropping files is the explicit request to ingest them; the app reads only dropped files, never executes them, and never crawls directories. UTF-8 text/code formats include `.txt`, `.md`, `.py`, `.js`/`.jsx`, `.ts`/`.tsx`, `.json`, `.yaml`/`.yml`, `.ps1`, `.sh`, `.bat`, `.csv`, `.html`, `.css`, `.xml`, `.toml`, `.ini`, `.sql`, `.c`/`.h`, `.cpp`/`.hpp`, `.java`, `.go`, and `.rs`. PDFs are read locally by the bundled PyMuPDF dependency; only embedded/selectable text is extracted, with filename, page number, and chunk metadata. Password-protected, malformed, and textless (typically scanned/image-only) PDFs are skipped or their textless pages omitted and reported. No OCR is performed or downloaded. Each drop accepts at most 10 files; text/code files are limited to 5 MiB each, and PDFs to 100 MB (100 * 1024 * 1024 bytes) each. A drop can create up to 20,000 records, uploaded to the local server in sequential batches of at most 50 records per request. PDFs are also capped at 250 pages and 900,000 extracted characters each; extracted text is split into at most 18,000-character chunks. Each file is fingerprinted with SHA-256 independent of its basename, and normalized exact duplicate chunks are skipped across files, PDF wrappers/pages, and prior ingestions; the progress status reports the duplicate count. `store_documents`, `store_important_item`, and `store_qa_item` also skip exact duplicate content. Unsupported, unreadable, oversized, empty, environment (`.env`), credential/private-key named files, key/certificate formats, and private-key blocks are skipped and reported in the progress log without recording file contents or names. Literal credential-like assignments in text/code files are skipped. If the PDF scanner detects text resembling a literal password, API key, or token, the app pauses before writing any file from the batch and asks whether to ingest that PDF; choosing **Yes** includes it, while **No** skips only that PDF. The prompt names the PDF but never displays or logs the matched value. The scanner is heuristic and may produce false positives or miss credentials; review PDFs before consenting. Do not drop sensitive/private files; review skipped-file messages.

PyMuPDF is bundled at version 1.28.2 for PDF text extraction. It is dual-licensed under AGPL-3.0 or a commercial license; anyone redistributing the app should verify which license obligations apply to their distribution.

Office ingestion supports `.xlsx` and `.docx` only. Excel sheets and rows and Word paragraphs, sections, and table rows are extracted locally with workbook/document metadata; formulas are stored as formula text and are never evaluated. Office packages are limited to 100 MB and a 256 MiB expanded archive, and extracted Office content is capped to 900,000 characters. The previous 500-record processing cap is removed; a single drop is now bounded at 20,000 records to keep memory use predictable, and upload requests remain limited to 50 records each. Word table extraction is limited to 20,000 rows per table. Password-encrypted or corrupt Office files and macro-enabled workbooks are rejected. Legacy `.xls` and `.doc` files are unsupported; save them as `.xlsx` or `.docx` first. The pinned `openpyxl` and `python-docx` dependencies are included in both native build paths. Literal credential-like text found in Office content requires the same explicit per-file consent as a credential-flagged PDF; no document content is sent to cloud services or logged.

Save Important Item opens a form for a title, source label, and pasted script, code, or note; it saves only after the user submits the form. It does not read local files or chat history. Copilot clients should save reusable scripts/code and important notes they create or receive by default using `store_important_item` (or `store_documents` for a batch), and tell the user after a successful save; they should not retain sensitive/private content without consent. After a local search miss, if a client later answers a question from a source allowed by its own configuration, it can call `store_qa_item(question, answer, source)` so future local searches can return that Q&A. The MCP server cannot intercept every chat, answer questions, access a cloud/web/AI source, or force the Copilot client to call the save tool. The panel is otherwise read-only and has no delete button; the local server's record-deletion route remains available to integrations that explicitly call it. Build and startup never clear stored records. Message, warning, error, and confirmation dialogs use white backgrounds with dark text and readable controls; dashboard fonts are sized to keep headings, labels, and controls within their panels.

The dashboard's connected-clients list shows an MCP client as soon as it completes the initialize handshake, including apps that have not called a tool yet. Further MCP requests refresh its last-active time, and an explicit MCP disconnect removes it. Clients that disconnect without sending the MCP DELETE request age out after five minutes. Merely configuring the endpoint in Copilot does not count as a connection. The `connected_lancedb_apps` MCP tool reports the same active/recent client list. This is a list of clients that actually connected to this MCP endpoint, not OS-wide discovery of unrelated Windows apps or processes.

## Use the database from other apps

The database in `DATA` is a standard LanceDB database, not a private app-specific format. Any program with a compatible LanceDB client/library can open that folder directly and use the `copilot_documents` table. For example, from Python in the app folder:

Use a compatible LanceDB Python installation and pass the actual folder shown in the app's **Database Folder** field to `lancedb.connect(...)`.

The table stores `content`, `source`, `created_at`, `title`, and deduplication metadata (`dedupe_key`, `file_fingerprint`, and `ingest_id`). Older tables gain nullable metadata columns only when a new item is explicitly stored; existing records are preserved. `store_documents` leaves `title` empty; `store_important_item`, `store_qa_item`, and file-drop chunks include searchable title/source metadata. The table is created when the first document is stored. Apps that do not use a LanceDB-compatible client can instead integrate through the app's documented MCP tools; an unrelated database format or API requires an adapter. Do not have separate processes modify the same local database concurrently unless their LanceDB client versions and access pattern support it.

The app uses LanceDB's native FTS configuration to create separate full-text indexes for `content` and, when present, `title`. A newly created table receives its indexes after its first successful insert; later ingestions append records without replacing or rebuilding indexes. LanceDB can search newly appended rows while reporting them as unindexed until its index maintenance catches up. Older tables are not modified merely because the app starts. Use `list_search_indexes` or `lancedb_status` to inspect them, then call `ensure_search_indexes` to create only missing applicable FTS indexes. This operation preserves rows and existing indexes, and reports creation failures instead of silently continuing.

When the MCP server starts, it runs `table.optimize()` once for a non-empty `copilot_documents` table. This compacts data fragments and updates indexes after prior inserts; it skips missing or empty tables and logs optimization failures without preventing the server from starting. `lancedb_status` and `list_search_indexes` report indexed and unindexed row counts so you can monitor the result. LanceDB's default optimization also prunes versions older than seven days; keep this in mind if you depend on older table snapshots. Optimization does not run after every insert.

No scalar indexes are created by default: current retrieval uses full-text search rather than scalar filters, so these extra indexes would add write/storage overhead without helping the app's current query path. The current schema has no `pc_cc` column; values that happen to appear inside `content` are text, not a structured scalar field, and are not indexed as such. LanceDB 0.41.0 has removed the old `use_tantivy=True` option; the app uses the supported native `create_index(..., config=FTS())` API.

## Local-only search

Call the MCP `search_documents` tool before responding to a retrieval request. It searches the active local LanceDB database first and returns local matches when available. On a local miss it returns `found: false` and `fallback_allowed: true`, directing the connected client to continue with its configured web, cloud, or AI sources if available and identify those external sources. The MCP server itself does not query LanceDB Cloud, the web, or an AI model; external fallback uses the connected client's configured capabilities, which this server cannot intercept or guarantee. After an externally sourced answer, the client can call `store_qa_item(question, answer, source)` so a later local search may find it.

The local database must contain the needed information. To retain a script or important item for later retrieval, explicitly submit it with `store_important_item`, `store_qa_item`, use **Save Important Item**, or drop supported files onto the dashboard. An empty search does not cause the server to obtain information from external sources. After an allowed answer is obtained by the connected client, it should call `store_qa_item` with the question and answer when appropriate; this requires explicit client cooperation and is not automatic capture of every answer.

## Portability notes

Copy the executable together with its adjacent `DATA` and `RUNTIME` folders to a writable location on another compatible PC. The app derives its database and runtime locations from its own folder at launch, so it does not depend on the original drive letter or path. Windows settings and logs live under `%LOCALAPPDATA%\LANCEDB MCP SERVER` unless an existing executable-adjacent legacy `.data` folder is reused. The portable GUI/Python runtime targets 64-bit Windows. First launch needs internet access to install the external LanceDB runtime; after a verified runtime is installed, offline launches continue using that version. Source mode stores that same independently updatable runtime in a sibling `<app-folder>.runtime` directory.

The app's MCP HTTP endpoint binds only to `127.0.0.1`, so MCP clients must run on the same PC. To use the database on another PC, copy the folder there or use another compatible LanceDB client with an appropriately transferred database; this app does not expose a network database service or provide multi-PC synchronization. The endpoint and port can be changed with `LANCEDB_MCP_PORT`; if changed, update the URL in Copilot's MCP settings as well.
