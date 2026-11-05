import os
from pathlib import Path


def application_data_directory(
    app_directory: Path,
    executable: Path,
    *,
    frozen: bool,
    platform: str,
    environ: dict[str, str] | None = None,
) -> Path:
    if not frozen:
        return app_directory
    if platform == "darwin":
        return app_directory / "DATA"

    legacy_directory = executable.with_name(f"{executable.stem}.data")
    if legacy_directory.is_dir():
        return legacy_directory

    environment = os.environ if environ is None else environ
    local_app_data = environment.get("LOCALAPPDATA")
    if local_app_data:
        return Path(local_app_data) / "LANCEDB MCP SERVER"
    return Path.home() / "AppData" / "Local" / "LANCEDB MCP SERVER"
