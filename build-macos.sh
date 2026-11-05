#!/bin/bash
set -Eeuo pipefail

APP_NAME="LANCEDB MCP SERVER"
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
OUTPUT_DIR="${1:-$HOME/Desktop/LANCE MCP SERVER macOS}"
PYTHON="${PYTHON:-}"

usage() {
    cat <<'EOF'
Usage:
  ./build-macos.sh [OUTPUT_DIRECTORY]

Builds a native macOS .app for the current Mac architecture. The output
directory must not already exist. DATA and RUNTIME are created empty beside
the app; existing user databases are never copied, replaced, or removed.
EOF
}

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
    usage
    exit 0
fi
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo "ERROR: Native macOS app builds must run on macOS." >&2
    exit 2
fi

if [[ -z "$PYTHON" ]]; then
    for candidate in python3.14 python3.13 python3; do
        if command -v "$candidate" >/dev/null 2>&1 &&
            "$candidate" -c 'import sys; raise SystemExit(0 if (3, 13) <= sys.version_info[:2] < (3, 15) else 1)'; then
            PYTHON="$(command -v "$candidate")"
            break
        fi
    done
fi
if [[ -z "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; raise SystemExit(0 if (3, 13) <= sys.version_info[:2] < (3, 15) else 1)'; then
    echo "ERROR: Python 3.13 or 3.14 is required. Set PYTHON to its executable path." >&2
    exit 2
fi
for tool in rsync xcrun codesign; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "ERROR: Required macOS build tool '$tool' is unavailable." >&2
        exit 2
    fi
done

OUTPUT_DIR="$("$PYTHON" -c 'from pathlib import Path; import sys; print(Path(sys.argv[1]).expanduser().resolve())' "$OUTPUT_DIR")"
"$PYTHON" - "$ROOT" "$OUTPUT_DIR" <<'PY'
from pathlib import Path
import sys

source, output = map(Path, sys.argv[1:3])
if source == output or source in output.parents or output in source.parents:
    raise SystemExit("ERROR: Source and output directories must not contain one another.")
PY
if [[ -e "$OUTPUT_DIR" ]]; then
    echo "ERROR: Output directory already exists; refusing to overwrite it: $OUTPUT_DIR" >&2
    exit 2
fi

BUILD_DIR="$OUTPUT_DIR/.build"
BUILD_SOURCE="$BUILD_DIR/source"
VENV="$BUILD_DIR/venv"
WORK_DIR="$BUILD_DIR/pyinstaller"
APP_PATH="$OUTPUT_DIR/$APP_NAME.app"
mkdir -p "$BUILD_SOURCE" "$WORK_DIR"

echo "Copying source files while excluding databases, secrets, and local environments..."
rsync -a \
    --exclude '/.git/' \
    --exclude '/.DS_Store' \
    --exclude '/.venv-macos/' \
    --exclude '/.venv-tests/' \
    --exclude '/__pycache__/' \
    --exclude '/.pytest_cache/' \
    --exclude '/DATA/' \
    --exclude '/RUNTIME/' \
    --exclude '/logs/' \
    --exclude '*.lance/' \
    --exclude '*.runtime/' \
    --exclude '.env' \
    --exclude '.env.*' \
    --exclude '*.settings.json' \
    --exclude '*.pyc' \
    "$ROOT/" "$BUILD_SOURCE/"

for required in gui.py server.py file_ingestion.py lancedb_runtime.py requirements.txt; do
    if [[ ! -f "$BUILD_SOURCE/$required" ]]; then
        echo "ERROR: Required source file is missing: $BUILD_SOURCE/$required" >&2
        exit 2
    fi
done
if [[ ! -f "$BUILD_SOURCE/assets/lancedb-mcp-server.icns" ]]; then
    echo "ERROR: The macOS application icon is missing." >&2
    exit 2
fi

"$PYTHON" -m venv "$VENV"
"$VENV/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$VENV/bin/python" -m pip install --disable-pip-version-check \
    -r "$BUILD_SOURCE/requirements.txt" \
    "pyinstaller==6.22.3"
"$VENV/bin/python" -m compileall -q \
    "$BUILD_SOURCE/gui.py" \
    "$BUILD_SOURCE/server.py" \
    "$BUILD_SOURCE/file_ingestion.py" \
    "$BUILD_SOURCE/lancedb_runtime.py"

"$VENV/bin/python" - "$BUILD_SOURCE" "$BUILD_DIR/package_versions.json" <<'PY'
from importlib import metadata
from pathlib import Path
import json
import re
import sys

source, destination = map(Path, sys.argv[1:3])
required = set()
for line in (source / "requirements.txt").read_text(encoding="utf-8").splitlines():
    line = line.strip()
    if line and not line.startswith("#"):
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

echo "Building $APP_PATH for $(uname -m)..."
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
        --distpath "$OUTPUT_DIR" \
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
) 2>&1 | tee "$BUILD_DIR/build.log"

if [[ ! -d "$APP_PATH" ]]; then
    echo "ERROR: PyInstaller did not produce $APP_PATH." >&2
    exit 1
fi
"$APP_PATH/Contents/MacOS/$APP_NAME" --validate-pdf-runtime
"$APP_PATH/Contents/MacOS/$APP_NAME" --validate-office-runtime
codesign --verify --deep --strict "$APP_PATH"
mkdir "$OUTPUT_DIR/DATA" "$OUTPUT_DIR/RUNTIME"

echo "Build completed."
echo "App:       $APP_PATH"
echo "Database:  $OUTPUT_DIR/DATA"
echo "Runtime:   $OUTPUT_DIR/RUNTIME"
echo "Arch:      $(uname -m)"
echo "The app is ad-hoc signed; it is not notarized for public distribution."
