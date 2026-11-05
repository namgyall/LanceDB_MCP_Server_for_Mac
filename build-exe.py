import os
import site
import sys
import json
import importlib.metadata
import re
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
SITE_PACKAGES = APP_DIR / "runtime" / "Lib" / "site-packages"
site.addsitedir(str(SITE_PACKAGES))
sys.path.remove(str(SITE_PACKAGES))
sys.path.insert(0, str(SITE_PACKAGES))

REQUIRED_PACKAGES = {
    re.sub(r"[-_.]+", "-", re.split(r"[<>=!~; ]", line.strip(), maxsplit=1)[0]).lower()
    for line in (APP_DIR / "requirements.txt").read_text(encoding="utf-8").splitlines()
    if line.strip() and not line.lstrip().startswith("#")
}

import PySide6
import mcp
import pyarrow

for package in (PySide6, mcp, pyarrow):
    if not Path(package.__file__).resolve().is_relative_to(SITE_PACKAGES.resolve()):
        raise RuntimeError(f"{package.__name__} was not loaded from the app runtime.")

PACKAGE_VERSION_MANIFEST = Path(
    os.environ.get(
        "LANCEDB_BUILD_WORK",
        str(Path(os.environ["TEMP"]) / "lancedb-mcp-pyinstaller"),
    )
) / "package_versions.json"
PACKAGE_VERSION_MANIFEST.parent.mkdir(parents=True, exist_ok=True)
PACKAGE_VERSION_MANIFEST.write_text(
    json.dumps(
        {
            re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower(): distribution.version
            for distribution in importlib.metadata.distributions(path=[str(SITE_PACKAGES)])
            if distribution.metadata.get("Name")
            and re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower() in REQUIRED_PACKAGES
            and re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower() != "lancedb"
            and not re.sub(r"[-_.]+", "-", distribution.metadata["Name"]).lower().startswith("lance-")
        },
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)

from PyInstaller.__main__ import run


name = os.environ.get("LANCEDB_BUILD_NAME", "LanceDB MCP App")
run(
    [
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--name",
        name,
        "--icon",
        str(APP_DIR / "assets" / "lancedb-mcp-server.ico"),
        "--distpath",
        os.environ.get("LANCEDB_BUILD_DIST", "D:\\Desktop"),
        "--workpath",
        os.environ.get("LANCEDB_BUILD_WORK", str(Path(os.environ["TEMP"]) / "lancedb-mcp-pyinstaller")),
        "--specpath",
        os.environ.get("LANCEDB_BUILD_WORK", str(Path(os.environ["TEMP"]) / "lancedb-mcp-pyinstaller")),
        "--paths",
        str(SITE_PACKAGES),
        "--hidden-import",
        "server",
        "--hidden-import",
        "packaging",
        "--hidden-import",
        "symtable",
        "--collect-all",
        "packaging",
        "--collect-all",
        "colorama",
        "--collect-all",
        "deprecation",
        "--collect-all",
        "pydantic",
        "--collect-all",
        "tqdm",
        "--collect-all",
        "dateutil",
        "--collect-all",
        "typing_extensions",
        "--collect-all",
        "urllib3",
        "--exclude-module",
        "lancedb",
        "--exclude-module",
        "lance_namespace",
        "--exclude-module",
        "lance_namespace_urllib3_client",
        "--add-data",
        f"{PACKAGE_VERSION_MANIFEST};.",
        "--add-data",
        f"{APP_DIR / 'assets' / 'lancedb-mcp-server.png'};assets",
        "--collect-submodules",
        "mcp.server",
        "--collect-all",
        "pyarrow",
        "--collect-all",
        "pymupdf",
        "--collect-all",
        "openpyxl",
        "--collect-all",
        "et_xmlfile",
        "--collect-all",
        "docx",
        "--collect-all",
        "lxml",
        str(APP_DIR / "gui.py"),
    ]
)
