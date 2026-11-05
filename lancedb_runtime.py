from __future__ import annotations

import hashlib
import importlib
import importlib.metadata
import json
import os
import re
import shutil
import sys
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from pathlib import Path, PurePosixPath
from typing import Callable
from contextlib import contextmanager

from packaging.requirements import Requirement
from packaging.tags import sys_tags
from packaging.utils import parse_wheel_filename
from packaging.version import Version


PYPI_JSON = "https://pypi.org/pypi/{package}/json"
REQUEST_TIMEOUT = 15
Progress = Callable[[str], None]


def runtime_root(app_dir: Path, frozen: bool) -> Path:
    if frozen:
        return app_dir / "RUNTIME"
    return app_dir.parent / f"{app_dir.name}.runtime"


def _read_pypi_metadata(package: str) -> dict:
    request = urllib.request.Request(
        PYPI_JSON.format(package=package),
        headers={"User-Agent": "LanceDB-MCP-Server-runtime-updater"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
        data = json.load(response)
    if data.get("info", {}).get("name", "").lower() != package:
        raise ValueError(f"PyPI returned unexpected metadata for {package}.")
    return data


def _latest_stable_release(
    metadata: dict, specifier=None
) -> tuple[Version, list[dict]]:
    releases = metadata.get("releases", {})
    candidates = []
    for version_text, files in releases.items():
        try:
            version = Version(version_text)
        except Exception:
            continue
        if version.is_prerelease or version.is_devrelease:
            continue
        if specifier and version not in specifier:
            continue
        wheels = [item for item in files if item.get("packagetype") == "bdist_wheel"]
        if wheels:
            try:
                _select_wheel(wheels)
            except ValueError:
                continue
            candidates.append((version, wheels))
    if not candidates:
        raise ValueError(f"No stable compatible wheel was published for {metadata['info']['name']}.")
    return max(candidates, key=lambda item: item[0])


def _select_wheel(files: list[dict]) -> dict:
    supported = {tag: index for index, tag in enumerate(sys_tags())}
    compatible = []
    for item in files:
        filename = item.get("filename", "")
        if not filename.endswith(".whl") or item.get("yanked", False):
            continue
        try:
            _, _, _, tags = parse_wheel_filename(filename)
        except (ValueError, TypeError):
            continue
        ranks = [supported[tag] for tag in tags if tag in supported]
        if ranks:
            compatible.append((min(ranks), item))
    if not compatible:
        raise ValueError("PyPI has no wheel compatible with this OS/Python runtime.")
    return min(compatible, key=lambda item: item[0])[1]


def _installed_versions() -> dict[str, Version]:
    versions: dict[str, Version] = {}
    for distribution in importlib.metadata.distributions():
        name = distribution.metadata.get("Name")
        if not name:
            continue
        normalized_name = re.sub(r"[-_.]+", "-", name).lower()
        if normalized_name == "lancedb" or normalized_name.startswith("lance-"):
            continue
        try:
            versions[normalized_name] = Version(distribution.version)
        except Exception:
            continue
    manifest_path = Path(getattr(sys, "_MEIPASS", "")) / "package_versions.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            for name, version_text in manifest.items():
                normalized_name = re.sub(r"[-_.]+", "-", name).lower()
                if normalized_name == "lancedb" or normalized_name.startswith("lance-"):
                    continue
                versions.setdefault(
                    normalized_name, Version(version_text)
                )
        except (OSError, ValueError, TypeError):
            pass
    return versions


def _validate_wheel_requirements(
    metadata: dict, available_versions: dict[str, Version], sidecar_versions: dict[str, Version]
) -> None:
    for requirement_text in metadata.get("requires_dist", []) or []:
        try:
            requirement = Requirement(requirement_text)
        except Exception as error:
            raise ValueError(f"Could not validate dependency {requirement_text!r}: {error}") from error
        if requirement.marker and not requirement.marker.evaluate():
            continue
        name = re.sub(r"[-_.]+", "-", requirement.name).lower()
        version = sidecar_versions.get(name) or available_versions.get(name)
        if version is None or (requirement.specifier and version not in requirement.specifier):
            found = str(version) if version is not None else "not bundled"
            raise ValueError(
                f"LanceDB requires {requirement}, but this app has {found}; "
                "this release is not compatible with the bundled dependencies."
            )


def _resolve_runtime_packages(
    lancedb_metadata: dict,
    lancedb_version: Version,
    lancedb_files: list[dict],
    app_versions: dict[str, Version],
    report: Progress,
) -> list[tuple[str, Version, list[dict], dict]]:
    packages = [
        ("lancedb", lancedb_version, lancedb_files, lancedb_metadata)
    ]
    selected = {"lancedb": lancedb_version}
    processed: set[str] = set()
    while True:
        pending = [package for package in packages if package[0] not in processed]
        if not pending:
            break
        package_name, _, _, metadata = pending[0]
        processed.add(package_name)
        for requirement_text in metadata["info"].get("requires_dist", []) or []:
            requirement = Requirement(requirement_text)
            if requirement.marker and not requirement.marker.evaluate():
                continue
            name = re.sub(r"[-_.]+", "-", requirement.name).lower()
            local_version = app_versions.get(name)
            selected_version = selected.get(name)
            if (
                not name.startswith("lance-")
                and local_version is not None
                and (not requirement.specifier or local_version in requirement.specifier)
            ):
                continue
            if selected_version is not None:
                if requirement.specifier and selected_version not in requirement.specifier:
                    raise ValueError(
                        f"Selected {name} {selected_version} does not satisfy {requirement}."
                    )
                continue
            report(f"Checking the {name} runtime...")
            dependency_metadata = _read_pypi_metadata(name)
            dependency_version, dependency_files = _latest_stable_release(
                dependency_metadata, requirement.specifier
            )
            selected[name] = dependency_version
            packages.append(
                (name, dependency_version, dependency_files, dependency_metadata)
            )
    return packages


def _distribution_from_path(package_name: str, package_path: Path):
    normalized_name = re.sub(r"[-_.]+", "-", package_name).lower()
    for distribution in importlib.metadata.distributions(path=[str(package_path)]):
        name = distribution.metadata.get("Name", "")
        if re.sub(r"[-_.]+", "-", name).lower() == normalized_name:
            return distribution
    raise importlib.metadata.PackageNotFoundError(package_name)


def _download_verified_wheel(file_info: dict, destination: Path) -> None:
    expected_hash = file_info.get("digests", {}).get("sha256")
    if not expected_hash or not re.fullmatch(r"[a-fA-F0-9]{64}", expected_hash):
        raise ValueError(f"PyPI did not provide a valid SHA-256 digest for {file_info.get('filename')}.")
    request = urllib.request.Request(
        file_info["url"],
        headers={"User-Agent": "LanceDB-MCP-Server-runtime-updater"},
    )
    digest = hashlib.sha256()
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest().lower() != expected_hash.lower():
        destination.unlink(missing_ok=True)
        raise ValueError(f"SHA-256 verification failed for {file_info['filename']}.")


def _extract_wheel(wheel: Path, target: Path) -> None:
    with zipfile.ZipFile(wheel) as archive:
        for item in archive.infolist():
            member = PurePosixPath(item.filename)
            if member.is_absolute() or ".." in member.parts or not member.parts:
                raise ValueError(f"Unsafe path in downloaded wheel: {item.filename}")
            destination = target.joinpath(*member.parts)
            if not destination.resolve().is_relative_to(target.resolve()):
                raise ValueError(f"Unsafe path in downloaded wheel: {item.filename}")
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item) as source, destination.open("wb") as output:
                    shutil.copyfileobj(source, output)


def _validate_candidate(candidate: Path, versions: dict[str, Version]) -> None:
    version_text = {name: str(version) for name, version in versions.items()}
    if getattr(sys, "frozen", False):
        command = [
            sys.executable,
            "--validate-lancedb-runtime",
            str(candidate),
            json.dumps(version_text),
        ]
    else:
        module_dir = str(Path(__file__).resolve().parent)
        validate_code = (
            "import json,sys; "
            f"sys.path.insert(0, {module_dir!r}); "
            "from lancedb_runtime import _activate_and_validate; "
            "_activate_and_validate(Path(sys.argv[1]), json.loads(sys.argv[2]))"
        )
        command = [
            sys.executable,
            "-c",
            f"from pathlib import Path; {validate_code}",
            str(candidate),
            json.dumps(version_text),
        ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=180,
            cwd=str(Path(__file__).resolve().parent),
            creationflags=(
                subprocess.CREATE_NO_WINDOW
                if sys.platform == "win32" and getattr(sys, "frozen", False)
                else 0
            ),
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("The isolated LanceDB runtime import check timed out.") from error
    if result.returncode:
        details = (result.stderr or result.stdout).strip()
        raise RuntimeError(
            "The isolated LanceDB runtime import check failed"
            + (f": {details[-1200:]}" if details else f" (exit code {result.returncode}).")
        )


def _read_active_runtime(root: Path) -> tuple[Path, dict[str, str]] | None:
    pointer = root / "current.json"
    if not pointer.is_file():
        return None
    try:
        document = json.loads(pointer.read_text(encoding="utf-8"))
        relative = Path(document["path"])
        if relative.is_absolute() or ".." in relative.parts:
            return None
        package_path = (root / relative).resolve()
        if not package_path.is_relative_to(root.resolve()) or not package_path.is_dir():
            return None
        versions = document["versions"]
        if not isinstance(versions, dict) or not versions.get("lancedb"):
            return None
        if not (package_path / "lancedb").is_dir():
            return None
        return package_path, versions
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _activate_runtime(package_path: Path, versions: dict[str, str]) -> None:
    for name in tuple(sys.modules):
        if (
            name == "lancedb"
            or name.startswith("lancedb.")
            or name == "lance_namespace"
            or name.startswith("lance_namespace.")
            or name.startswith("lance_namespace_")
        ):
            del sys.modules[name]
    sys.path[:] = [
        entry
        for entry in sys.path
        if Path(entry or ".").resolve() != package_path.resolve()
    ]
    sys.path.insert(0, str(package_path))
    os.environ["LANCEDB_RUNTIME_PATH"] = str(package_path)
    os.environ["LANCEDB_RUNTIME_VERSIONS"] = json.dumps(versions)


def _activate_and_validate(package_path: Path, versions: dict[str, str]) -> None:
    _activate_runtime(package_path, versions)
    try:
        module = importlib.import_module("lancedb")
        if Version(module.__version__) != Version(versions["lancedb"]):
            raise RuntimeError(
                f"Expected LanceDB {versions['lancedb']}, imported {module.__version__}."
            )
        importlib.import_module("lance_namespace")
        for package_name in versions:
            if package_name.startswith("lance-"):
                importlib.import_module(package_name.replace("-", "_"))
    except Exception as error:
        for name in tuple(sys.modules):
            if (
                name == "lancedb"
                or name.startswith("lancedb.")
                or name == "lance_namespace"
                or name.startswith("lance_namespace.")
                or name.startswith("lance_namespace_")
            ):
                del sys.modules[name]
        raise RuntimeError(f"The active LanceDB runtime failed its import check: {error}") from error


@contextmanager
def _exclusive_update_lock(root: Path):
    lock_path = root / ".update.lock"
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
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
                    raise TimeoutError("Another app instance is still updating the LanceDB runtime.")
                time.sleep(0.25)
        try:
            yield
        finally:
            release()


def ensure_lancedb_runtime(app_dir: Path, frozen: bool, progress: Progress | None = None) -> str:
    root = runtime_root(app_dir, frozen)
    root.mkdir(parents=True, exist_ok=True)
    with _exclusive_update_lock(root):
        return _ensure_lancedb_runtime_locked(app_dir, frozen, progress, root)


def _ensure_lancedb_runtime_locked(
    app_dir: Path,
    frozen: bool,
    progress: Progress | None,
    root: Path,
) -> str:
    report = progress or (lambda _message: None)
    active = _read_active_runtime(root)
    app_versions = _installed_versions()
    if active:
        try:
            _validate_candidate(
                active[0],
                {name: Version(version) for name, version in active[1].items()},
            )
            _activate_runtime(*active)
        except RuntimeError:
            active = None

    try:
        report("Checking PyPI for the latest stable LanceDB release...")
        lancedb_metadata = _read_pypi_metadata("lancedb")
        lancedb_version, lancedb_files = _latest_stable_release(lancedb_metadata)
        packages = _resolve_runtime_packages(
            lancedb_metadata,
            lancedb_version,
            lancedb_files,
            app_versions,
            report,
        )
        sidecar_versions = {name: version for name, version, _, _ in packages}
        metadata_by_package = {name: metadata for name, _, _, metadata in packages}
        for package_name, version, _, package_metadata in packages:
            _validate_wheel_requirements(
                package_metadata["info"],
                app_versions,
                sidecar_versions,
            )

        version_text = {key: str(value) for key, value in sidecar_versions.items()}
        if active and active[1] == version_text:
            _activate_runtime(*active)
            return f"LanceDB {lancedb_version} is current."

        staging = Path(tempfile.mkdtemp(prefix=".staging-", dir=root))
        package_path = staging / "Lib" / "site-packages"
        package_path.mkdir(parents=True)
        try:
            for package_name, version, files, package_metadata in packages:
                report(f"Downloading {package_name} {version}...")
                wheel_info = _select_wheel(files)
                if Version(wheel_info.get("version", str(version))) != version:
                    raise ValueError(f"PyPI wheel version did not match {package_name} {version}.")
                wheel_path = staging / wheel_info["filename"]
                _download_verified_wheel(wheel_info, wheel_path)
                _extract_wheel(wheel_path, package_path)
                wheel_path.unlink()
                distribution = _distribution_from_path(package_name, package_path)
                if Version(distribution.version) != version:
                    raise ValueError(f"Downloaded {package_name} wheel has unexpected package metadata.")
                if distribution.requires != package_metadata["info"].get("requires_dist"):
                    _validate_wheel_requirements(
                        {"requires_dist": distribution.requires or []},
                        app_versions,
                        sidecar_versions,
                    )
            report("Validating the downloaded LanceDB runtime...")
            _validate_candidate(package_path, sidecar_versions)

            version_label = "__".join(
                f"{name.replace('-', '_')}-{version}" for name, version in version_text.items()
            )
            final_directory = root / "versions" / version_label
            final_directory.parent.mkdir(parents=True, exist_ok=True)
            if final_directory.exists():
                final_directory = final_directory.with_name(
                    f"{final_directory.name}-{uuid.uuid4().hex[:8]}"
                )
            shutil.move(str(staging), final_directory)
            final_package_path = final_directory / "Lib" / "site-packages"
            relative = final_package_path.relative_to(root)
            _activate_runtime(final_package_path, version_text)
            pointer_tmp = root / "current.json.tmp"
            pointer_tmp.write_text(
                json.dumps({"path": str(relative), "versions": version_text}, indent=2) + "\n",
                encoding="utf-8",
            )
            pointer_tmp.replace(root / "current.json")
            return f"Updated LanceDB runtime to {lancedb_version}."
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    except (
        OSError,
        urllib.error.URLError,
        TimeoutError,
        ValueError,
        RuntimeError,
        KeyError,
        importlib.metadata.PackageNotFoundError,
    ) as error:
        if active:
            _activate_runtime(*active)
            return f"Update check failed; using LanceDB {active[1]['lancedb']}: {error}"
        raise RuntimeError(
            f"Could not install or verify the LanceDB runtime, and no existing runtime is available: {error}"
        ) from error


def activate_from_environment() -> None:
    runtime_path = os.environ.get("LANCEDB_RUNTIME_PATH")
    versions = os.environ.get("LANCEDB_RUNTIME_VERSIONS", "{}")
    if not runtime_path:
        raise RuntimeError("The external LanceDB runtime path was not supplied by the app launcher.")
    package_path = Path(runtime_path).resolve()
    if not (package_path / "lancedb").is_dir():
        raise RuntimeError(f"The external LanceDB runtime is missing: {package_path}")
    _activate_runtime(package_path, json.loads(versions))
