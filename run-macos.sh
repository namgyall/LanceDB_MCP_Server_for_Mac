#!/bin/bash
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
VENV="$ROOT/.venv-macos"
PYTHON="${PYTHON:-}"

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

if [[ ! -x "$VENV/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV"
fi

"$VENV/bin/python" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt"
exec "$VENV/bin/python" "$ROOT/gui.py" "$@"
