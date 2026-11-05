import asyncio
import importlib.util
import json
import sys
import tempfile
import unittest
import os
import threading
from unittest.mock import patch
from pathlib import Path

import pyarrow as pa
import lancedb

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtWidgets import QApplication, QMainWindow
from PySide6.QtCore import Qt, QUrl, QPoint, QPointF
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtCore import QMimeData

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_paths
import gui
import server


class DatabaseFolderValidationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.qt_app = QApplication.instance() or QApplication([])

    def test_dashboard_uses_glossy_app_icon(self):
        self.assertTrue(gui.APP_ICON_PATH.is_file())
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(gui, "APP_SETTINGS", root / "settings.json"),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "CONFIG", root / "copilot-mcp.json"),
                patch.object(gui, "DATABASE", root / "DATA"),
                patch.object(gui, "LOGS", root / "logs"),
                patch.object(gui.LanceDBWindow, "ensure_local_files"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    self.assertFalse(window.windowIcon().isNull())
                finally:
                    window.close()

    def test_initial_maximize_keeps_a_monitor_sized_restore_geometry(self):
        window = QMainWindow()
        try:
            gui.prepare_window_geometry(window, self.qt_app.primaryScreen().availableGeometry())
            expected_width = min(
                1100,
                max(720, int(self.qt_app.primaryScreen().availableGeometry().width() * 0.8)),
            )
            expected_height = min(
                760,
                max(480, int(self.qt_app.primaryScreen().availableGeometry().height() * 0.8)),
            )
            self.assertEqual(window.size().width(), expected_width)
            self.assertEqual(window.size().height(), expected_height)
        finally:
            window.close()

    def test_existing_lancedb_folder_is_accepted(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory)
            database = lancedb.connect(str(path))
            database.create_table(
                "documents",
                schema=pa.schema([pa.field("content", pa.string())]),
            )

            gui.LanceDBWindow.validate_database_folder(path)

    def test_gui_and_server_default_to_the_app_root_data_folder(self):
        self.assertEqual(gui.DATABASE, gui.APP_DIR / "DATA")
        expected_server_database = (
            Path(os.environ["LANCEDB_URI"]).expanduser().resolve()
            if os.environ.get("LANCEDB_URI")
            else server.APP_DIR / "DATA"
        )
        self.assertEqual(server.DATABASE_URI, expected_server_database)

    def test_frozen_windows_settings_do_not_create_adjacent_data_sidecar(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "LanceDB MCP App.exe"
            local_app_data = root / "LocalAppData"
            settings_directory = app_paths.application_data_directory(
                root,
                executable,
                frozen=True,
                platform="win32",
                environ={"LOCALAPPDATA": str(local_app_data)},
            )

            self.assertEqual(settings_directory, local_app_data / "LANCEDB MCP SERVER")
            self.assertFalse((root / "LanceDB MCP App.data").exists())
            self.assertFalse(local_app_data.exists())

    def test_existing_legacy_sidecar_remains_the_settings_location(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            executable = root / "LanceDB MCP App.exe"
            legacy_directory = root / "LanceDB MCP App.data"
            legacy_directory.mkdir()

            self.assertEqual(
                app_paths.application_data_directory(
                    root,
                    executable,
                    frozen=True,
                    platform="win32",
                    environ={"LOCALAPPDATA": str(root / "LocalAppData")},
                ),
                legacy_directory,
            )

    def test_empty_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(ValueError, "does not contain any LanceDB tables"):
                gui.LanceDBWindow.validate_database_folder(Path(temporary_directory))

    def test_empty_default_data_folder_is_a_valid_database_target(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_folder = Path(temporary_directory) / "DATA"
            data_folder.mkdir()
            with patch.object(gui, "DATABASE", data_folder):
                gui.LanceDBWindow.validate_database_folder(data_folder)

    def test_missing_folder_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            missing_path = Path(temporary_directory) / "missing"

            with self.assertRaisesRegex(ValueError, "Choose an existing LanceDB database folder"):
                gui.LanceDBWindow.validate_database_folder(missing_path)

    def test_macos_bundle_directory_resolves_to_sidecar_parent(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle_executable = (
                Path(temporary_directory)
                / "LanceDB MCP App.app"
                / "Contents"
                / "MacOS"
                / "LanceDB MCP App"
            )
            with (
                patch.object(gui.sys, "frozen", True, create=True),
                patch.object(gui.sys, "platform", "darwin"),
                patch.object(gui.sys, "executable", str(bundle_executable)),
            ):
                self.assertEqual(
                    gui.application_directory(), Path(temporary_directory).resolve()
                )
                self.assertEqual(
                    server.application_directory(), Path(temporary_directory).resolve()
                )

    def test_fresh_dashboard_uses_sibling_data_folder(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            settings = root / "settings.json"
            default_database = root / "DATA"
            with (
                patch.object(gui, "APP_SETTINGS", settings),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "DATABASE", default_database),
                patch.object(gui, "LOGS", root / "logs"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    self.assertEqual(window.database_path, default_database)
                    self.assertEqual(window.database_path_label.text(), str(default_database))
                    self.assertEqual(window.database_path_label.cursorPosition(), 0)
                    self.assertTrue(default_database.is_dir())
                finally:
                    window.close()

    def test_saved_database_selection_is_displayed_on_launch(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            selected_database = root / "selected"
            selected_database.mkdir()
            database = lancedb.connect(str(selected_database))
            database.create_table(
                "documents",
                schema=pa.schema([pa.field("content", pa.string())]),
            )
            settings = root / "settings.json"
            settings.write_text(
                json.dumps({"database_path": str(selected_database)}),
                encoding="utf-8",
            )
            with (
                patch.object(gui, "APP_SETTINGS", settings),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "DATABASE", root / "DATA"),
                patch.object(gui, "LOGS", root / "logs"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    self.assertEqual(window.database_path, selected_database.resolve())
                    self.assertEqual(
                        window.database_path_label.text(), str(selected_database.resolve())
                    )
                    self.assertIn(
                        "Using the saved database selection",
                        window.log_view.toPlainText(),
                    )
                    self.assertEqual(
                        json.loads(settings.read_text(encoding="utf-8"))["database_path"],
                        str(selected_database),
                    )
                finally:
                    window.close()

    def test_dashboard_panels_and_browse_button_are_in_expected_layout(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(gui, "APP_SETTINGS", root / "settings.json"),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "CONFIG", root / "copilot-mcp.json"),
                patch.object(gui, "DATABASE", root / "DATA"),
                patch.object(gui, "LOGS", root / "logs"),
                patch.object(gui.LanceDBWindow, "ensure_local_files"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    layout = window.dashboard_layout
                    self.assertIs(
                        window.dashboard_scroll_area.widget(), layout.parentWidget()
                    )
                    self.assertTrue(window.dashboard_scroll_area.widgetResizable())
                    self.assertEqual(window.minimumWidth(), 720)
                    self.assertEqual(window.minimumHeight(), 480)
                    clients_panel = window.findChild(gui.QGroupBox, "clientsPanel")
                    database_panel = window.findChild(gui.QGroupBox, "databasePanel")
                    documents_panel = window.findChild(gui.QGroupBox, "documentsPanel")
                    connected_panels = clients_panel.parentWidget()
                    connected_layout = connected_panels.layout()
                    self.assertIs(window.browse_database_button.parentWidget(), database_panel)
                    self.assertIs(
                        window.file_drop_panel.parentWidget(), documents_panel
                    )
                    self.assertTrue(window.file_drop_panel.acceptDrops())
                    self.assertIn("Drop text/code, PDF, Excel (.xlsx), or Word (.docx)", window.file_drop_panel.label.text())
                    self.assertIn("Excel", window.file_drop_panel.label.text())
                    self.assertIn("Word", window.file_drop_panel.label.text())
                    self.assertIn("100 MB max", window.file_drop_panel.label.text())
                    self.assertIn("5 MiB max", window.file_drop_panel.label.text())
                    connected_position = layout.getItemPosition(
                        next(
                            index
                            for index in range(layout.count())
                            if layout.itemAt(index).widget() is connected_panels
                        )
                    )
                    self.assertEqual(connected_position, (1, 1, 9, 1))
                    log_panel = window.findChild(gui.QGroupBox, "progressLogPanel")
                    log_position = layout.getItemPosition(
                        next(
                            index
                            for index in range(layout.count())
                            if layout.itemAt(index).widget() is log_panel
                        )
                    )
                    self.assertEqual(log_position, (7, 0, 3, 1))
                    self.assertEqual(connected_layout.indexOf(clients_panel), 0)
                    self.assertEqual(connected_layout.indexOf(documents_panel), 1)
                    self.assertEqual(connected_layout.stretch(0), 1)
                    self.assertEqual(connected_layout.stretch(1), 1)
                    self.assertEqual(clients_panel.minimumHeight(), documents_panel.minimumHeight())
                    self.assertEqual(window.browse_database_button.text(), "Browse")
                    self.assertGreaterEqual(
                        window.browse_database_button.minimumWidth(), 100
                    )
                    self.assertEqual(window.windowTitle(), "LANCEDB MCP SERVER")
                    self.assertEqual(
                        window.findChild(gui.QLabel, "title").text(),
                        "LANCEDB MCP SERVER",
                    )
                    self.assertIn(
                        "QGroupBox#databasePanel",
                        window.styleSheet(),
                    )
                    self.assertIn(
                        "background-color: #f7fbf8",
                        window.styleSheet(),
                    )
                    self.assertIn(
                        "background-color: #102a1a",
                        window.styleSheet(),
                    )
                    self.assertIn("QMessageBox {", window.styleSheet())
                    self.assertIn("background-color: #ffffff", window.styleSheet())
                    self.assertIn("QMessageBox QLabel", window.styleSheet())
                    self.assertIn("QMessageBox QPushButton", window.styleSheet())
                    self.assertIn("font-size: 22px", window.styleSheet())
                    self.assertIn("QLabel#health { font-size: 14px", window.styleSheet())
                    self.assertIn("QGroupBox#databasePanel::title", window.styleSheet())
                    self.assertIn("font-size: 12px", window.styleSheet())
                    self.assertIn("font-size: 11px", window.styleSheet())
                    self.assertIn("font-size: 10px", window.styleSheet())
                    self.assertIn("font-size: 14px", window.styleSheet())
                    self.assertIn("font-size: 22px; font-weight: 700", window.styleSheet())
                    self.assertEqual(window.refresh_documents_button.text(), "Refresh")
                    document_buttons = window.findChild(
                        gui.QGroupBox, "documentsPanel"
                    ).findChildren(gui.QPushButton)
                    self.assertEqual(
                        [button.text() for button in document_buttons],
                        ["Save Important Item", "Refresh"],
                    )
                    self.assertTrue(callable(window.save_important_item))
                    self.assertIsNotNone(window.save_item_button)
                    self.assertFalse(hasattr(window, "delete_documents_button"))
                    self.assertFalse(hasattr(window, "delete_selected_documents"))
                    self.assertTrue(callable(server.delete_ingested_documents))
                    self.assertEqual(window.log_view.objectName(), "progressLog")
                    self.assertEqual(
                        window.log_view.minimumHeight(), gui.PROGRESS_LOG_MIN_HEIGHT
                    )
                    self.assertEqual(
                        window.log_view.maximumBlockCount(),
                        gui.PROGRESS_LOG_MAX_LINES,
                    )
                    window.resize(1280, 1200)
                    window.show()
                    self.qt_app.processEvents()
                    self.assertLessEqual(
                        abs(clients_panel.height() - documents_panel.height()), 1
                    )
                    central_bottom = (
                        window.dashboard_scroll_area.widget().height()
                        - layout.contentsMargins().bottom()
                        - 1
                    )
                    self.assertLessEqual(
                        abs(connected_panels.geometry().bottom() - central_bottom), 2
                    )
                    self.assertEqual(
                        connected_panels.geometry().bottom(),
                        log_panel.geometry().bottom(),
                    )
                    self.assertTrue(window.log_view.isReadOnly())
                    self.assertIn("Dashboard ready.", window.log_view.toPlainText())
                    self.assertEqual(window.browse_database_button.objectName(), "capsuleButton")
                    self.assertIn("border-radius: 22px", window.styleSheet())
                    self.assertIn("color: #ffffff", window.styleSheet())
                    self.assertIn("rgba(255, 255, 255, 77)", window.styleSheet())
                    self.assertEqual(gui.WHITE_TINT_30_PERCENT, 0x4DFFFFFF)
                finally:
                    window.close()

    def test_dashboard_fits_small_monitor_with_scrollable_content(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(gui, "APP_SETTINGS", root / "settings.json"),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "CONFIG", root / "copilot-mcp.json"),
                patch.object(gui, "DATABASE", root / "DATA"),
                patch.object(gui, "LOGS", root / "logs"),
                patch.object(gui.LanceDBWindow, "ensure_local_files"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    screen = self.qt_app.primaryScreen().availableGeometry()
                    width = min(800, screen.width())
                    height = min(600, screen.height())
                    window.showNormal()
                    window.resize(width, height)
                    self.qt_app.processEvents()

                    self.assertLessEqual(window.width(), width)
                    self.assertLessEqual(window.height(), height)
                    self.assertIsNotNone(window.dashboard_scroll_area.widget())
                    self.assertGreater(
                        window.dashboard_scroll_area.verticalScrollBar().maximum(), 0
                    )
                finally:
                    window.close()

    def test_progress_log_has_bounded_history_and_truncated_lines(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(gui, "APP_SETTINGS", root / "settings.json"),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "DATABASE", root / "data"),
                patch.object(gui, "LOGS", root / "logs"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    runtime_log = root / "logs" / "runtime.log"
                    runtime_log.write_text(
                        "".join(
                            f"runtime-entry-{index:04d} " + ("x" * 400) + "\n"
                            for index in range(300)
                        ),
                        encoding="utf-8",
                    )
                    for _ in range(4):
                        window.refresh_progress_logs()

                    lines = window.log_view.toPlainText().splitlines()
                    self.assertEqual(len(lines), gui.PROGRESS_LOG_MAX_LINES)
                    self.assertTrue(lines[-1].startswith("[runtime.log] runtime-entry-0299"))
                    self.assertTrue(
                        all(len(line) <= gui.PROGRESS_LOG_MAX_LINE_LENGTH for line in lines)
                    )
                    self.assertTrue(lines[-1].endswith("..."))
                finally:
                    window.close()

    def test_progress_log_redacts_credentials_and_shows_runtime_events(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            with (
                patch.object(gui, "APP_SETTINGS", root / "settings.json"),
                patch.object(gui, "APP_DATA_DIR", root),
                patch.object(gui, "DATABASE", root / "data"),
                patch.object(gui, "LOGS", root / "logs"),
                patch.dict(os.environ, {"LANCEDB_GUI_NO_SERVER": "1"}),
            ):
                window = gui.LanceDBWindow()
                try:
                    window.record_progress("Request failed: api_key=private-value")
                    gui.append_progress_log(
                        root / "logs" / "runtime.log",
                        "Installed compatible LanceDB runtime.",
                    )
                    window.refresh_progress_logs()

                    visible_log = window.log_view.toPlainText()
                    persisted_log = (root / "logs" / "app.log").read_text(
                        encoding="utf-8"
                    )
                    self.assertIn("Installed compatible LanceDB runtime.", visible_log)
                    self.assertIn("api_key=[redacted]", visible_log)
                    self.assertNotIn("private-value", visible_log)
                    self.assertNotIn("private-value", persisted_log)
                finally:
                    window.close()

    def test_api_request_worker_keeps_network_work_off_the_ui_thread(self):
        request_threads = []

        def fake_request(_path, _payload=None):
            request_threads.append(threading.current_thread())
            return {"documents": []}

        with patch.object(gui.LanceDBWindow, "_request_json", side_effect=fake_request):
            worker = gui.LocalApiRequestWorker("/documents")
            worker.start()
            self.assertTrue(worker.wait(2000))

        self.assertEqual(len(request_threads), 1)
        self.assertIsNot(request_threads[0], threading.main_thread())

    def test_file_drop_panel_emits_dropped_local_file_paths(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "drop-me.txt"
            path.write_text("explicit drop", encoding="utf-8")
            panel = gui.FileDropPanel()
            dropped = []
            panel.files_dropped.connect(dropped.append)
            mime_data = QMimeData()
            mime_data.setUrls([QUrl.fromLocalFile(str(path))])
            drag_enter = QDragEnterEvent(
                QPoint(2, 2),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            drag_move = QDragMoveEvent(
                QPoint(2, 2),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            event = QDropEvent(
                QPointF(2, 2),
                Qt.DropAction.CopyAction,
                mime_data,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )

            QApplication.sendEvent(panel.label, drag_enter)
            QApplication.sendEvent(panel.label, drag_move)
            QApplication.sendEvent(panel.label, event)

        self.assertEqual(len(dropped), 1)
        self.assertEqual(len(dropped[0]), 1)
        self.assertEqual(Path(dropped[0][0]), path)
        self.assertTrue(drag_enter.isAccepted())
        self.assertTrue(drag_move.isAccepted())
        self.assertTrue(event.isAccepted())

    @unittest.skipUnless(importlib.util.find_spec("pymupdf"), "PyMuPDF is not installed")
    def test_dragged_manual_style_pdf_is_stored_page_by_page_in_bounded_batches(self):
        import pymupdf

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manual_path = root / "manual.pdf"
            document = pymupdf.open()
            for page_number in range(1, 51):
                document.new_page().insert_text(
                    (30, 30),
                    f"Manual section {page_number}. " + ("local searchable manual text " * 600),
                )
            document.save(manual_path)
            document.close()

            appendix_path = root / "appendix.pdf"
            document = pymupdf.open()
            document.new_page().insert_text(
                (72, 72), "Appendix local retrieval marker."
            )
            document.save(appendix_path)
            document.close()

            database = server.lancedb.connect(str(root / "isolated-db"))
            app_data_path = root / "isolated-app-data"
            with (
                patch.object(server, "db", database),
                patch.object(server, "APP_DATA_DIR", app_data_path),
            ):
                server._store_records(
                    [
                        {
                            "title": "Preserved marker",
                            "content": "Preexisting temporary-database record.",
                            "source": "test",
                        }
                    ]
                )

                panel = gui.FileDropPanel()
                dropped = []
                panel.files_dropped.connect(dropped.append)
                mime_data = QMimeData()
                mime_data.setUrls(
                    [
                        QUrl.fromLocalFile(str(manual_path)),
                        QUrl.fromLocalFile(str(appendix_path)),
                    ]
                )
                drag_enter = QDragEnterEvent(
                    QPoint(2, 2),
                    Qt.DropAction.CopyAction,
                    mime_data,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                drag_move = QDragMoveEvent(
                    QPoint(2, 2),
                    Qt.DropAction.CopyAction,
                    mime_data,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                drop = QDropEvent(
                    QPointF(2, 2),
                    Qt.DropAction.CopyAction,
                    mime_data,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
                QApplication.sendEvent(panel.label, drag_enter)
                QApplication.sendEvent(panel.label, drag_move)
                QApplication.sendEvent(panel.label, drop)
                self.assertTrue(drop.isAccepted())
                self.assertEqual(len(dropped), 1)

                prepared_results = []
                preparation = gui.FileDropIngestWorker(dropped[0])
                preparation.prepared.connect(prepared_results.append)
                preparation.start()
                self.assertTrue(preparation.wait(10_000))
                QApplication.processEvents()

                self.assertEqual(len(prepared_results), 1)
                prepared = prepared_results[0]
                self.assertEqual(prepared["skipped"], [])
                self.assertEqual(prepared["confirmation_required"], [])
                self.assertEqual(len(prepared["records"]), 51)
                self.assertIn("page 1", prepared["records"][0]["title"])
                self.assertIn("page 50", prepared["records"][49]["title"])
                self.assertIn("page 1", prepared["records"][50]["title"])

                batch_sizes = []

                def store_batch(route, payload):
                    self.assertEqual(route, "/documents/store-batch")
                    documents = payload["documents"]
                    batch_sizes.append(len(documents))

                    class FakeRequest:
                        async def json(self):
                            return {"documents": documents}

                    response = asyncio.run(
                        server.store_documents_batch_route(FakeRequest())
                    )
                    if response.status_code != 200:
                        raise RuntimeError(response.body.decode("utf-8"))
                    return json.loads(response.body)

                uploaded_results = []
                upload = gui.FileDropUploadWorker(prepared["records"], [])
                upload.completed.connect(uploaded_results.append)
                with patch.object(
                    gui.LanceDBWindow, "_request_json", side_effect=store_batch
                ):
                    upload.start()
                    self.assertTrue(upload.wait(10_000))
                QApplication.processEvents()

                saved = server.list_ingested_documents(database)
                search_results = server._search_database(
                    database, "Appendix local retrieval marker", 3
                )
                panel.deleteLater()

        self.assertEqual(batch_sizes, [50, 1])
        self.assertEqual(uploaded_results[0]["stored"], 51)
        self.assertEqual(len(saved), 52)
        self.assertTrue(any(item["title"] == "Preserved marker" for item in saved))
        self.assertTrue(any("page 50" in (item["title"] or "") for item in saved))
        self.assertTrue(
            any("Appendix local retrieval marker." in item["content"] for item in search_results)
        )

    def test_file_drop_upload_reports_partial_success_if_later_batch_fails(self):
        records = [
            {"title": f"chunk-{index}", "source": "file-drop:test", "content": "text"}
            for index in range(51)
        ]
        completed = []
        failures = []

        def fake_request(_route, payload):
            if len(payload["documents"]) == 1:
                raise RuntimeError("local server unavailable")
            return {"stored": len(payload["documents"]), "duplicates": 0}

        worker = gui.FileDropUploadWorker(records, [])
        worker.completed.connect(completed.append)
        worker.failed.connect(lambda message, stored: failures.append((message, stored)))
        with patch.object(gui.LanceDBWindow, "_request_json", side_effect=fake_request):
            worker.start()
            self.assertTrue(worker.wait(5000))
        QApplication.processEvents()

        self.assertEqual(completed, [])
        self.assertEqual(failures[0][1], 50)
        self.assertIn("earlier batches remain in the database", failures[0][0])

    def test_file_drop_upload_keeps_batches_at_50_for_more_than_500_records(self):
        records = [
            {"title": f"word-row-{index}", "source": "file-drop:large.docx", "content": "text"}
            for index in range(601)
        ]
        batch_sizes = []
        completed = []

        def fake_request(_route, payload):
            batch_size = len(payload["documents"])
            batch_sizes.append(batch_size)
            return {"stored": batch_size, "duplicates": 0}

        worker = gui.FileDropUploadWorker(records, [])
        worker.completed.connect(completed.append)
        with patch.object(gui.LanceDBWindow, "_request_json", side_effect=fake_request):
            worker.start()
            self.assertTrue(worker.wait(5000))
        QApplication.processEvents()

        self.assertEqual(batch_sizes, [50] * 12 + [1])
        self.assertEqual(completed[0]["stored"], 601)

    def test_ingestion_skip_reason_is_logged_without_file_name(self):
        progress_messages = []

        class FakeDropPanel:
            def set_status(self, _message):
                pass

            def setEnabled(self, _enabled):
                pass

        class FakeWindow:
            file_drop_panel = FakeDropPanel()

            def record_progress(self, message):
                progress_messages.append(message)

            def _finish_file_ingestion(self):
                pass

        with patch.object(gui.QMessageBox, "warning"):
            gui.LanceDBWindow._file_ingestion_completed(
                FakeWindow(),
                {
                    "stored": 0,
                    "skipped": ["manual.pdf: unsupported file type."],
                },
            )

        logged = "\n".join(progress_messages)
        self.assertIn("Dropped file skipped: unsupported file type.", logged)
        self.assertNotIn("manual.pdf", logged)

    def test_duplicate_count_is_visible_in_file_drop_status(self):
        statuses = []
        progress_messages = []

        class FakeDropPanel:
            def set_status(self, message):
                statuses.append(message)

            def setEnabled(self, _enabled):
                pass

        class FakeWindow:
            file_drop_panel = FakeDropPanel()

            def record_progress(self, message):
                progress_messages.append(message)

            def _finish_file_ingestion(self):
                pass

            def refresh_documents(self):
                pass

        with patch.object(gui.QMessageBox, "information"):
            gui.LanceDBWindow._file_ingestion_completed(
                FakeWindow(),
                {"stored": 2, "duplicates": 3, "skipped": []},
            )

        self.assertIn("skipped 3 duplicate(s)", statuses[0])
        self.assertTrue(
            any("3 exact duplicate(s) skipped" in message for message in progress_messages)
        )

    def test_file_ingestion_shows_duplicate_alert_for_duplicates(self):
        alerts = []
        statuses = []

        class FakeDropPanel:
            def set_status(self, message):
                statuses.append(message)

            def setEnabled(self, _enabled):
                pass

        class FakeWindow:
            file_drop_panel = FakeDropPanel()

            def record_progress(self, _message):
                pass

            def refresh_documents(self):
                pass

            def _finish_file_ingestion(self):
                pass

        with patch.object(
            gui.QMessageBox,
            "information",
            side_effect=lambda *args: alerts.append(args[1:]),
        ):
            gui.LanceDBWindow._file_ingestion_completed(
                FakeWindow(),
                {"stored": 0, "duplicates": 2, "skipped": []},
            )

        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0][0], "Duplicate content skipped")
        self.assertIn("Duplicate content skipped: 2 item(s) already exist.", alerts[0][1])
        self.assertNotIn("Saved ", alerts[0][1])
        self.assertIn("skipped 2 duplicate(s)", statuses[0])

    def test_mixed_ingestion_alert_reports_saved_and_duplicate_counts(self):
        alerts = []

        class FakeDropPanel:
            def set_status(self, _message):
                pass

            def setEnabled(self, _enabled):
                pass

        class FakeWindow:
            file_drop_panel = FakeDropPanel()

            def record_progress(self, _message):
                pass

            def refresh_documents(self):
                pass

            def _finish_file_ingestion(self):
                pass

        with patch.object(
            gui.QMessageBox,
            "information",
            side_effect=lambda *args: alerts.append(args[1:]),
        ):
            gui.LanceDBWindow._file_ingestion_completed(
                FakeWindow(),
                {"stored": 4, "duplicates": 3, "skipped": []},
            )

        self.assertEqual(len(alerts), 1)
        self.assertIn("Saved 4 item(s).", alerts[0][1])
        self.assertIn("Duplicate content skipped: 3 item(s) already exist.", alerts[0][1])

    def test_file_ingestion_does_not_show_duplicate_alert_when_none(self):
        alerts = []

        class FakeDropPanel:
            def set_status(self, _message):
                pass

            def setEnabled(self, _enabled):
                pass

        class FakeWindow:
            file_drop_panel = FakeDropPanel()

            def record_progress(self, _message):
                pass

            def refresh_documents(self):
                pass

            def _finish_file_ingestion(self):
                pass

        with patch.object(
            gui.QMessageBox,
            "information",
            side_effect=lambda *args: alerts.append(args),
        ):
            gui.LanceDBWindow._file_ingestion_completed(
                FakeWindow(),
                {"stored": 4, "duplicates": 0, "skipped": []},
            )

        self.assertEqual(alerts, [])

    def test_file_ingestion_worker_prepares_files_off_the_ui_thread(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory) / "snippet.py"
            path.write_text("print('safe to store; not executed')", encoding="utf-8")
            preparation_threads = []
            worker = gui.FileDropIngestWorker([str(path)])
            prepared = []
            worker.prepared.connect(prepared.append)

            original_prepare = gui.prepare_dropped_files

            def tracked_prepare(paths):
                preparation_threads.append(threading.current_thread())
                return original_prepare(paths)

            with patch.object(gui, "prepare_dropped_files", side_effect=tracked_prepare):
                worker.start()
                self.assertTrue(worker.wait(3000))

        self.assertEqual(len(preparation_threads), 1)
        self.assertIsNot(preparation_threads[0], threading.main_thread())
        gui.QApplication.processEvents()
        self.assertEqual(len(prepared), 1)
        self.assertIn("print(", prepared[0]["records"][0]["content"])
        self.assertEqual(prepared[0]["skipped"], [])
        self.assertEqual(prepared[0]["confirmation_required"], [])

    def test_pdf_credential_confirmation_asks_without_showing_detected_secret(self):
        secret = "must-never-be-shown"
        parent = gui.QWidget()
        try:
            for answer, expected in (
                (gui.QMessageBox.StandardButton.Yes, True),
                (gui.QMessageBox.StandardButton.No, False),
            ):
                with patch.object(gui.QMessageBox, "question", return_value=answer) as prompt:
                    accepted = gui.confirm_pdf_credential_ingestion(
                        parent, "private-report.pdf"
                    )

                self.assertEqual(accepted, expected)
                prompt_text = prompt.call_args.args[2]
                self.assertIn("private-report.pdf", prompt_text)
                self.assertIn("literal password, API key, or token", prompt_text)
                self.assertIn("heuristic", prompt_text)
                self.assertNotIn(secret, prompt_text)
                self.assertEqual(
                    prompt.call_args.args[4],
                    gui.QMessageBox.StandardButton.No,
                )
        finally:
            parent.deleteLater()

    def test_office_credential_confirmation_names_document_type(self):
        parent = gui.QWidget()
        try:
            for file_name, expected_type in (
                ("budget.xlsx", "Excel workbook"),
                ("minutes.docx", "Word document"),
            ):
                with patch.object(
                    gui.QMessageBox,
                    "question",
                    return_value=gui.QMessageBox.StandardButton.No,
                ) as prompt:
                    accepted = gui.confirm_pdf_credential_ingestion(parent, file_name)

                self.assertFalse(accepted)
                self.assertIn(expected_type, prompt.call_args.args[2])
                self.assertIn(file_name, prompt.call_args.args[2])
        finally:
            parent.deleteLater()

    def test_pdf_credential_approval_or_decline_controls_records_without_logging_secret(self):
        secret = "local-test-api-secret"
        candidates = [
            {
                "file_name": "account.pdf",
                "reason": "credential-like literal detected",
                "records": [{"content": f"api_key = {secret}", "source": "file-drop:account.pdf", "title": "account.pdf"}],
            }
        ]
        progress_messages = []

        approved, skipped = gui.resolve_pdf_credential_confirmations(
            candidates, lambda _name: True, progress_messages.append
        )
        self.assertEqual(len(approved), 1)
        self.assertEqual(skipped, [])

        declined_records, declined = gui.resolve_pdf_credential_confirmations(
            candidates, lambda _name: False, progress_messages.append
        )
        self.assertEqual(declined_records, [])
        self.assertEqual(declined, ["account.pdf: not ingested; user declined credential warning."])
        self.assertTrue(progress_messages)
        self.assertNotIn("account.pdf", "\n".join(progress_messages))
        self.assertNotIn(secret, "\n".join(progress_messages))


if __name__ == "__main__":
    unittest.main()
