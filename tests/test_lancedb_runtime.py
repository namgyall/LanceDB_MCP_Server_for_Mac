import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from pathlib import Path
from packaging.version import Version
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import lancedb_runtime


def release_metadata(version):
    filename = f"lancedb-{version}-py3-none-any.whl"
    wheel = {
        "filename": filename,
        "version": version,
        "packagetype": "bdist_wheel",
        "url": "https://files.pythonhosted.org/lancedb.whl",
        "digests": {"sha256": "0" * 64},
    }
    return {
        "info": {
            "name": "lancedb",
            "requires_dist": [],
        },
        "releases": {version: [wheel]},
        "wheel": wheel,
    }


def write_fake_wheel(destination, version):
    with zipfile.ZipFile(destination, "w") as archive:
        archive.writestr(
            "lancedb/__init__.py",
            f"__version__ = {version!r}\n"
            "class _Tables:\n"
            "    tables = []\n"
            "class _Database:\n"
            "    def list_tables(self): return _Tables()\n"
            "def connect(path): return _Database()\n",
        )
        archive.writestr(
            f"lancedb-{version}.dist-info/METADATA",
            f"Metadata-Version: 2.1\nName: lancedb\nVersion: {version}\n\n",
        )


class LanceDBRuntimeUpdaterTests(unittest.TestCase):
    def setUp(self):
        self.original_sys_path = sys.path.copy()
        self.original_runtime_environment = {
            key: os.environ.get(key)
            for key in ("LANCEDB_RUNTIME_PATH", "LANCEDB_RUNTIME_VERSIONS")
        }
        self.original_lancedb_modules = {
            name: module
            for name, module in sys.modules.items()
            if name == "lancedb"
            or name.startswith("lancedb.")
            or name == "lance_namespace"
            or name.startswith("lance_namespace.")
            or name.startswith("lance_namespace_")
        }

    def tearDown(self):
        for name in tuple(sys.modules):
            if (
                name == "lancedb"
                or name.startswith("lancedb.")
                or name == "lance_namespace"
                or name.startswith("lance_namespace.")
                or name.startswith("lance_namespace_")
            ):
                del sys.modules[name]
        sys.modules.update(self.original_lancedb_modules)
        sys.path[:] = self.original_sys_path
        for key, value in self.original_runtime_environment.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def test_frozen_runtime_is_external_sibling_of_executable(self):
        executable_directory = Path(r"D:\Desktop")

        self.assertEqual(
            lancedb_runtime.runtime_root(executable_directory, True),
            executable_directory / "RUNTIME",
        )

    def create_active_runtime(self, root, version="1.0.0"):
        package_path = root / "versions" / f"lancedb-{version}" / "Lib" / "site-packages"
        (package_path / "lancedb").mkdir(parents=True)
        (package_path / "lancedb" / "__init__.py").write_text(
            f"__version__ = {version!r}\n", encoding="utf-8"
        )
        (package_path / "lance_namespace").mkdir()
        (package_path / "lance_namespace" / "__init__.py").write_text("", encoding="utf-8")
        (package_path / "lance_namespace_urllib3_client").mkdir()
        (package_path / "lance_namespace_urllib3_client" / "__init__.py").write_text(
            "", encoding="utf-8"
        )
        (root / "current.json").write_text(
            json.dumps(
                {
                    "path": str(package_path.relative_to(root)),
                    "versions": {"lancedb": version},
                }
            ),
            encoding="utf-8",
        )
        return package_path

    @staticmethod
    def provide_fake_download(file_info, destination):
        version = file_info["version"]
        write_fake_wheel(destination, version)

    def test_latest_release_install_replaces_active_pointer_atomically(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            app_dir.mkdir()
            runtime = root / "app.runtime"
            previous = self.create_active_runtime(runtime)
            metadata = release_metadata("2.0.0")
            with (
                patch.object(lancedb_runtime, "_read_pypi_metadata", return_value=metadata),
                patch.object(lancedb_runtime, "_download_verified_wheel", self.provide_fake_download),
                patch.object(lancedb_runtime, "_validate_candidate"),
            ):
                result = lancedb_runtime.ensure_lancedb_runtime(app_dir, False)

            active = lancedb_runtime._read_active_runtime(runtime)
            self.assertEqual(result, "Updated LanceDB runtime to 2.0.0.")
            self.assertNotEqual(active[0], previous)
            self.assertEqual(active[1], {"lancedb": "2.0.0"})
            self.assertEqual(Path(os.environ["LANCEDB_RUNTIME_PATH"]), active[0])

    def test_same_release_uses_existing_runtime_without_downloading(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            app_dir.mkdir()
            runtime = root / "app.runtime"
            package_path = self.create_active_runtime(runtime, "2.0.0")
            metadata = release_metadata("2.0.0")
            with (
                patch.object(lancedb_runtime, "_read_pypi_metadata", return_value=metadata),
                patch.object(
                    lancedb_runtime,
                    "_download_verified_wheel",
                    side_effect=AssertionError("matching runtime should not download"),
                ),
            ):
                result = lancedb_runtime.ensure_lancedb_runtime(app_dir, False)
            self.assertEqual(result, "LanceDB 2.0.0 is current.")
            self.assertEqual(lancedb_runtime._read_active_runtime(runtime)[0], package_path)

    def test_offline_update_keeps_previous_runtime_pointer(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            app_dir.mkdir()
            runtime = root / "app.runtime"
            package_path = self.create_active_runtime(runtime)
            pointer_before = (runtime / "current.json").read_bytes()
            with patch.object(
                lancedb_runtime,
                "_read_pypi_metadata",
                side_effect=urllib.error.URLError("offline"),
            ):
                result = lancedb_runtime.ensure_lancedb_runtime(app_dir, False)
            self.assertIn("using LanceDB 1.0.0", result)
            self.assertEqual((runtime / "current.json").read_bytes(), pointer_before)
            self.assertEqual(lancedb_runtime._read_active_runtime(runtime)[0], package_path)

    def test_failed_candidate_validation_rolls_back_to_previous_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            app_dir.mkdir()
            runtime = root / "app.runtime"
            previous = self.create_active_runtime(runtime)
            pointer_before = (runtime / "current.json").read_bytes()
            metadata = release_metadata("2.0.0")
            with (
                patch.object(lancedb_runtime, "_read_pypi_metadata", return_value=metadata),
                patch.object(lancedb_runtime, "_download_verified_wheel", self.provide_fake_download),
                patch.object(
                    lancedb_runtime,
                    "_validate_candidate",
                    side_effect=[None, RuntimeError("test import failure")],
                ),
            ):
                result = lancedb_runtime.ensure_lancedb_runtime(app_dir, False)
            self.assertIn("using LanceDB 1.0.0", result)
            self.assertEqual((runtime / "current.json").read_bytes(), pointer_before)
            self.assertEqual(lancedb_runtime._read_active_runtime(runtime)[0], previous)
            self.assertEqual(list(runtime.glob(".staging-*")), [])

    def test_download_digest_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "runtime.whl"
            payload = b"wheel bytes"

            class Response(io.BytesIO):
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    self.close()

            with patch.object(
                lancedb_runtime.urllib.request,
                "urlopen",
                return_value=Response(payload),
            ):
                with self.assertRaisesRegex(ValueError, "SHA-256 verification failed"):
                    lancedb_runtime._download_verified_wheel(
                        {
                            "filename": "runtime.whl",
                            "url": "https://example.test/runtime.whl",
                            "digests": {"sha256": hashlib.sha256(b"wrong").hexdigest()},
                        },
                        destination,
                    )
            self.assertFalse(destination.exists())

    def test_incompatible_new_release_keeps_verified_runtime(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            app_dir = root / "app"
            app_dir.mkdir()
            runtime = root / "app.runtime"
            package_path = self.create_active_runtime(runtime)
            pointer_before = (runtime / "current.json").read_bytes()
            metadata = release_metadata("2.0.0")
            metadata["info"]["requires_dist"] = ["pyarrow>=999"]
            with (
                patch.object(lancedb_runtime, "_read_pypi_metadata", return_value=metadata),
                patch.object(lancedb_runtime, "_installed_versions", return_value={"pyarrow": Version("26.0.0")}),
                patch.object(
                    lancedb_runtime,
                    "_download_verified_wheel",
                    side_effect=AssertionError("incompatible release should not download"),
                ),
            ):
                result = lancedb_runtime.ensure_lancedb_runtime(app_dir, False)
            self.assertIn("using LanceDB 1.0.0", result)
            self.assertEqual((runtime / "current.json").read_bytes(), pointer_before)
            self.assertEqual(lancedb_runtime._read_active_runtime(runtime)[0], package_path)

    def test_wheel_archive_rejects_parent_traversal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            wheel = root / "unsafe.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("../outside.txt", "not allowed")
            with self.assertRaisesRegex(ValueError, "Unsafe path"):
                lancedb_runtime._extract_wheel(wheel, root / "target")
            self.assertFalse((root / "outside.txt").exists())

    def test_resolves_transitive_lance_namespace_client_from_pypi_metadata(self):
        lancedb_metadata = release_metadata("2.0.0")
        lancedb_metadata["info"]["requires_dist"] = ["lance-namespace>=0.13.0"]
        namespace_metadata = release_metadata("0.13.0")
        namespace_metadata["info"]["name"] = "lance-namespace"
        namespace_metadata["info"]["requires_dist"] = [
            "lance-namespace-urllib3-client==0.13.0"
        ]
        client_metadata = release_metadata("0.13.0")
        client_metadata["info"]["name"] = "lance-namespace-urllib3-client"
        client_metadata["info"]["requires_dist"] = ["urllib3>=1.25.3,<3"]
        urllib3_metadata = release_metadata("2.8.0")
        urllib3_metadata["info"]["name"] = "urllib3"
        with patch.object(
            lancedb_runtime,
            "_read_pypi_metadata",
            side_effect=[namespace_metadata, client_metadata, urllib3_metadata],
        ):
            packages = lancedb_runtime._resolve_runtime_packages(
                lancedb_metadata,
                Version("2.0.0"),
                lancedb_metadata["releases"]["2.0.0"],
                {},
                lambda _message: None,
            )
        self.assertEqual(
            {name: str(version) for name, version, _, _ in packages},
            {
                "lancedb": "2.0.0",
                "lance-namespace": "0.13.0",
                "lance-namespace-urllib3-client": "0.13.0",
                "urllib3": "2.8.0",
            },
        )

    def test_runtime_directory_is_named_runtime_next_to_frozen_exe(self):
        self.assertEqual(
            lancedb_runtime.runtime_root(Path(r"D:\Moved App"), True),
            Path(r"D:\Moved App\RUNTIME"),
        )


if __name__ == "__main__":
    unittest.main()
