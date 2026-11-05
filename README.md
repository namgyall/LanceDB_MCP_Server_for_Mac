# LanceDB MCP Server for macOS

A native macOS desktop app that runs a local LanceDB-backed Model Context Protocol (MCP) server. The dashboard starts maximized with a monitor-sized restore geometry and scrolls on smaller displays. The MCP endpoint binds to loopback only: `http://127.0.0.1:8765/mcp`.

The app searches local LanceDB records first. On a local miss, it tells the connected MCP client that external fallback may be used if the client has web, cloud, or AI sources configured. The server itself does not make external requests.

## Requirements

- macOS with Xcode Command Line Tools (`xcode-select --install`)
- Python 3.13 or 3.14
- Network access on first launch, to install/update the compatible LanceDB runtime

The build produces an app for the architecture of the Mac that runs the build script. The GitHub Actions workflow builds an unsigned, ad-hoc-signed artifact and does not notarize it.

## Run from source

```sh
chmod +x run-macos.sh
./run-macos.sh
```

The launcher creates `.venv-macos`, installs the pinned GUI/server dependencies, and starts the dashboard. The source-mode database is in `DATA/`; the independently updated LanceDB runtime is kept in the sibling `LANCEDB.runtime/` directory. Both are excluded from Git. The app preserves existing records and saved database selections.

## Build the native app

```sh
chmod +x build-macos.sh
./build-macos.sh "$HOME/Desktop/LANCE MCP SERVER macOS"
```

The optional argument is the output directory. The script refuses to overwrite an existing output directory. It creates `LANCEDB MCP SERVER.app`, `DATA/`, and `RUNTIME/` together; keep these three items together when moving or launching the app. It does not copy, replace, or delete database files. It validates the bundled PDF and Office extractors and the app signature before reporting success.

The app is ad-hoc signed, not Developer ID signed or notarized. macOS may require the user to approve opening an unsigned app downloaded from elsewhere. Do not bypass an operating-system warning if it identifies the app as unsafe or untrusted.

## Tests

```sh
chmod +x test-macos.sh
./test-macos.sh
```

The script creates or reuses `.venv-macos`, installs test dependencies, uses a temporary database, runs the server/storage/search/file-ingestion tests, and checks the monitor-sized window restore geometry. Temporary test data is removed on exit; the app's `DATA/` is not used.

GitHub Actions runs the macOS tests and builds the native app on pushes, pull requests, and manual dispatch. The build job uploads a 14-day ZIP artifact containing the `.app` bundle and empty `DATA/` and `RUNTIME/` directories. The build runs independently of the test job so an artifact is still available for diagnosis if a test fails. This workflow does not publish a GitHub Release.

## Connect GitHub Copilot

Add an HTTP MCP server in Copilot using:

```text
http://127.0.0.1:8765/mcp
```

The example client configuration is in `copilot-mcp.json`. The app does not edit Copilot settings automatically.

## Database and search

The default database stores LanceDB files directly in `DATA/`. In the dashboard's **Database Folder** panel, use **Browse** to select an existing database; the app remembers that choice and does not migrate or merge data.

The MCP tools include:

- `search_documents` — search local records first and return matches; on a miss, signal whether configured client-side external fallback may be used.
- `store_documents` — explicitly store a batch of text documents.
- `store_important_item` and `store_qa_item` — explicitly store notes/scripts or question-and-answer pairs.
- `lancedb_status`, `list_search_indexes`, and `ensure_search_indexes` — inspect database and index state or create missing full-text indexes.
- `list_tables` — list tables in the active database.

The app also supports dropping local text/code files, text PDFs, `.xlsx`, and `.docx` files onto the dashboard. It reads only files explicitly dropped by the user, never executes them, and does not crawl directories. Sensitive or credential-like content is skipped or requires explicit consent according to the file type; review the progress log.

The server runs `table.optimize()` once at startup for a non-empty `copilot_documents` table. It does not optimize after every insert. LanceDB's default optimization may prune snapshots older than seven days.

The LanceDB runtime is downloaded from official PyPI metadata and wheel hashes are verified before activation. Builds do not bundle a database or LanceDB runtime; the app installs/updates the runtime separately on first launch.

PyMuPDF is bundled at version 1.28.2 for PDF text extraction and is dual-licensed under AGPL-3.0 or a commercial license. Anyone redistributing the app should verify which license obligations apply. This repository does not include a license grant; check with the project owner before redistributing.
