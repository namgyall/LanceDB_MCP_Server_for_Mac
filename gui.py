import ctypes
import importlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from lancedb_runtime import (
    _activate_and_validate,
    activate_from_environment,
    ensure_lancedb_runtime,
)
from app_paths import application_data_directory
from progress_logging import append_progress_log, sanitize_log_message
from file_ingestion import (
    MAX_CHUNKS_PER_UPLOAD_BATCH,
    prepare_dropped_files,
    validate_pdf_runtime,
)
from PySide6.QtCore import QEvent, QRect, QThread, QTimer, Qt, Signal
from PySide6.QtGui import QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)


def application_directory() -> Path:
    if not getattr(sys, "frozen", False):
        return Path(__file__).resolve().parent
    executable = Path(sys.executable).resolve()
    if sys.platform == "darwin":
        return executable.parents[3]
    return executable.parent


APP_DIR = application_directory()
APP_ICON_PATH = (
    Path(getattr(sys, "_MEIPASS", APP_DIR))
    / "assets"
    / ("lancedb-mcp-server.icns" if sys.platform == "darwin" else "lancedb-mcp-server.png")
)
IS_MACOS_BUNDLE = getattr(sys, "frozen", False) and sys.platform == "darwin"
if IS_MACOS_BUNDLE:
    APP_DATA_DIR = application_data_directory(
        APP_DIR, Path(sys.executable), frozen=True, platform=sys.platform
    )
else:
    APP_DATA_DIR = application_data_directory(
        APP_DIR,
        Path(sys.executable),
        frozen=getattr(sys, "frozen", False),
        platform=sys.platform,
    )
RUNTIME_DIR = APP_DIR / ("RUNTIME" if IS_MACOS_BUNDLE else "runtime")
PYTHON = RUNTIME_DIR / "python.exe"
SERVER = APP_DIR / "server.py"
DATABASE = APP_DIR / "DATA"
LOGS = APP_DATA_DIR / "logs"
CONFIG = APP_DATA_DIR / "copilot-mcp.json"
APP_SETTINGS = APP_DATA_DIR / "settings.json"
PORT = int(os.environ.get("LANCEDB_MCP_PORT", "8765"))
ENDPOINT = f"http://127.0.0.1:{PORT}/mcp"

ACCENT_POLICY_ATTRIBUTE = 19
ACCENT_ENABLE_ACRYLIC_BLUR_BEHIND = 4
WHITE_TINT_30_PERCENT = 0x4DFFFFFF
PROGRESS_LOG_MAX_LINES = 250
PROGRESS_LOG_MAX_LINE_LENGTH = 240
PROGRESS_LOG_MIN_HEIGHT = 240


class AccentPolicy(ctypes.Structure):
    _fields_ = [
        ("accent_state", ctypes.c_int),
        ("accent_flags", ctypes.c_int),
        ("gradient_color", ctypes.c_int),
        ("animation_id", ctypes.c_int),
    ]


class WindowCompositionAttributeData(ctypes.Structure):
    _fields_ = [
        ("attribute", ctypes.c_int),
        ("data", ctypes.c_void_p),
        ("size_of_data", ctypes.c_size_t),
    ]


def apply_windows_acrylic(hwnd: int) -> bool:
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        function = user32.SetWindowCompositionAttribute
        function.argtypes = [ctypes.c_void_p, ctypes.POINTER(WindowCompositionAttributeData)]
        function.restype = ctypes.c_int
        policy = AccentPolicy(
            ACCENT_ENABLE_ACRYLIC_BLUR_BEHIND,
            2,
            WHITE_TINT_30_PERCENT,
            0,
        )
        data = WindowCompositionAttributeData(
            ACCENT_POLICY_ATTRIBUTE,
            ctypes.cast(ctypes.pointer(policy), ctypes.c_void_p),
            ctypes.sizeof(policy),
        )
        return function(hwnd, ctypes.byref(data)) != 0
    except (AttributeError, OSError):
        return False


class FileDropPanel(QFrame):
    files_dropped = Signal(list)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("fileDropPanel")
        self.setAcceptDrops(True)
        self.setMinimumHeight(72)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        self.label = QLabel(
            "Drop text/code, PDF, Excel (.xlsx), or Word (.docx) files here to ingest\n"
            "PDF/Office: 100 MB max · text/code: 5 MiB max"
        )
        self.label.setObjectName("fileDropLabel")
        self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.label.setWordWrap(True)
        self.label.setAcceptDrops(True)
        self.label.installEventFilter(self)
        layout.addWidget(self.label)

    def set_status(self, message: str) -> None:
        self.label.setText(
            message
            + "\nDrop supported text/code, PDF, Excel (.xlsx), or Word (.docx) files here; "
            "folders are not scanned."
        )

    def dragEnterEvent(self, event) -> None:
        if self._has_local_file_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:
        if self._has_local_file_urls(event):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:
        paths = self._local_file_paths(event)
        if paths:
            self.files_dropped.emit(paths)
            event.acceptProposedAction()
        else:
            event.ignore()

    def eventFilter(self, watched, event) -> bool:
        if watched is self.label:
            if event.type() == QEvent.Type.DragEnter:
                self.dragEnterEvent(event)
                return True
            if event.type() == QEvent.Type.DragMove:
                self.dragMoveEvent(event)
                return True
            if event.type() == QEvent.Type.Drop:
                self.dropEvent(event)
                return True
        return super().eventFilter(watched, event)

    @staticmethod
    def _has_local_file_urls(event) -> bool:
        return event.mimeData().hasUrls() and any(
            url.isLocalFile() and url.toLocalFile()
            for url in event.mimeData().urls()
        )

    @staticmethod
    def _local_file_paths(event) -> list[str]:
        return [
            url.toLocalFile()
            for url in event.mimeData().urls()
            if url.isLocalFile() and url.toLocalFile()
        ]


def confirm_pdf_credential_ingestion(parent: QWidget, file_name: str) -> bool:
    file_type = {
        ".xlsx": "Excel workbook",
        ".docx": "Word document",
    }.get(Path(file_name).suffix.casefold(), "PDF")
    answer = QMessageBox.question(
        parent,
        "Possible credentials detected",
        f"The {file_type} “{file_name}” contains text that looks like a literal password, "
        "API key, or token. This check is heuristic and can be wrong.\n\n"
        "Do you want to ingest this file into your local database anyway?",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def resolve_pdf_credential_confirmations(
    candidates: list[dict],
    confirm,
    progress,
) -> tuple[list[dict[str, str]], list[str]]:
    approved_records: list[dict[str, str]] = []
    skipped: list[str] = []
    for candidate in candidates:
        file_name = candidate.get("file_name", "Dropped document")
        progress("Credential-like text detected in a dropped file; awaiting explicit user consent.")
        if confirm(file_name):
            approved_records.extend(candidate["records"])
            progress("User approved ingestion of a credential-flagged file.")
        else:
            skipped.append(f"{file_name}: not ingested; user declined credential warning.")
            progress("User declined ingestion of a credential-flagged file.")
    return approved_records, skipped


def prepare_window_geometry(window: QMainWindow, available_geometry: QRect) -> None:
    minimum_width = min(720, available_geometry.width())
    minimum_height = min(480, available_geometry.height())
    window.setMinimumSize(minimum_width, minimum_height)
    window.resize(
        min(1100, max(minimum_width, int(available_geometry.width() * 0.8))),
        min(760, max(minimum_height, int(available_geometry.height() * 0.8))),
    )


class LanceDBWindow(QMainWindow):
    def __init__(self, runtime_status: str = "") -> None:
        super().__init__()
        self.setWindowTitle("LANCEDB MCP SERVER")
        self.setWindowIcon(QIcon(str(APP_ICON_PATH)))
        self.setMinimumSize(720, 480)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, False)
        self.server_process: subprocess.Popen | None = None
        self.stdout_log = None
        self.stderr_log = None
        self.documents_loaded = False
        self.documents_request_pending = False
        self.save_item_request_pending = False
        self.file_ingestion_pending = False
        self._api_workers: set[QThread] = set()
        self.selected_database_path = self.load_database_path()
        self.database_path = self.selected_database_path or DATABASE

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        self.dashboard_scroll_area = QScrollArea()
        self.dashboard_scroll_area.setObjectName("dashboardScrollArea")
        self.dashboard_scroll_area.setWidgetResizable(True)
        self.dashboard_scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        dashboard = QWidget()
        dashboard.setObjectName("central")
        self.dashboard_layout = QGridLayout(dashboard)
        central_layout.addWidget(self.dashboard_scroll_area)
        self.dashboard_scroll_area.setWidget(dashboard)
        self.setCentralWidget(central)
        layout = self.dashboard_layout
        layout.setContentsMargins(28, 24, 28, 24)
        layout.setHorizontalSpacing(20)
        layout.setVerticalSpacing(14)

        title = QLabel("LANCEDB MCP SERVER")
        title.setObjectName("title")
        subtitle = QLabel("A portable local LanceDB database shared with Copilot over MCP.")
        subtitle.setObjectName("subtitle")
        title_layout = QVBoxLayout()
        title_layout.setSpacing(3)
        title_layout.addWidget(title)
        title_layout.addWidget(subtitle)
        layout.addLayout(title_layout, 0, 0, 1, 2)

        self.status = QLabel("Starting MCP server...")
        self.status.setObjectName("status")
        layout.addWidget(self.status, 1, 0)

        health_panel = QGroupBox("LanceDB Connection")
        health_layout = QVBoxLayout(health_panel)
        health_layout.setSpacing(8)
        self.database_health = QLabel("Checking database connection...")
        self.database_health.setObjectName("health")
        health_layout.addWidget(self.database_health)
        self.document_count = QLabel("Documents: --")
        health_layout.addWidget(self.document_count)
        layout.addWidget(health_panel, 2, 0)

        database_panel = QGroupBox("Database Folder")
        database_panel.setObjectName("databasePanel")
        database_layout = QHBoxLayout(database_panel)
        database_layout.setContentsMargins(12, 16, 12, 12)
        self.database_path_label = QLineEdit(str(self.database_path))
        self.database_path_label.setObjectName("databasePath")
        self.database_path_label.setReadOnly(True)
        self.database_path_label.setToolTip(str(self.database_path))
        self.database_path_label.setCursorPosition(0)
        self.database_path_label.setMinimumHeight(38)
        database_layout.addWidget(self.database_path_label, 1)
        self.browse_database_button = self._button("Browse", self.browse_database)
        self.browse_database_button.setMinimumWidth(112)
        database_layout.addWidget(self.browse_database_button)
        layout.addWidget(database_panel, 3, 0)

        endpoint_panel = QGroupBox("MCP Endpoint")
        endpoint_layout = QVBoxLayout(endpoint_panel)
        self.endpoint_label = QLabel(ENDPOINT)
        self.endpoint_label.setObjectName("path")
        endpoint_layout.addWidget(self.endpoint_label)
        layout.addWidget(endpoint_panel, 4, 0)

        actions = QGridLayout()
        actions.setHorizontalSpacing(10)
        actions.setVerticalSpacing(10)
        actions.addWidget(self._button("Connect LanceDB", self.connect_database), 0, 0)
        actions.addWidget(self._button("Copy endpoint", self.copy_endpoint), 0, 1)
        actions.addWidget(self._button("Restart server", self.restart_server), 0, 2)
        layout.addLayout(actions, 5, 0)

        documents_panel = QGroupBox("Ingested Documents")
        documents_panel.setObjectName("documentsPanel")
        documents_layout = QVBoxLayout(documents_panel)
        documents_layout.setContentsMargins(12, 16, 12, 12)
        self.file_drop_panel = FileDropPanel()
        self.file_drop_panel.files_dropped.connect(self.ingest_dropped_files)
        documents_layout.addWidget(self.file_drop_panel)
        self.documents = QListWidget()
        self.documents.setObjectName("documents")
        self.documents.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection
        )
        self.documents.addItem("Select Refresh to load ingested records.")
        documents_layout.addWidget(self.documents)
        document_actions = QHBoxLayout()
        self.save_item_button = self._button(
            "Save Important Item", self.save_important_item
        )
        document_actions.addWidget(self.save_item_button)
        self.refresh_documents_button = self._button(
            "Refresh", self.refresh_documents
        )
        document_actions.addWidget(self.refresh_documents_button)
        documents_layout.addLayout(document_actions)
        documents_panel.setMinimumHeight(220)
        documents_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        clients_panel = QGroupBox("MCP Clients Connected")
        clients_panel.setObjectName("clientsPanel")
        clients_layout = QVBoxLayout(clients_panel)
        clients_layout.setContentsMargins(12, 16, 12, 12)
        self.clients = QListWidget()
        self.clients.setObjectName("clients")
        self.clients.addItem("No MCP clients have connected yet.")
        clients_layout.addWidget(self.clients)
        clients_panel.setMinimumHeight(220)
        clients_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Expanding,
        )
        connected_panels = QWidget()
        connected_panels_layout = QVBoxLayout(connected_panels)
        connected_panels_layout.setContentsMargins(0, 0, 0, 0)
        connected_panels_layout.setSpacing(14)
        connected_panels_layout.addWidget(clients_panel, 1)
        connected_panels_layout.addWidget(self.file_drop_panel)
        layout.addWidget(connected_panels, 1, 1, 9, 1)

        footer = QLabel(
            f"LanceDB runtime: {runtime_status}\n"
            "The local server starts automatically. Connected clients remain visible "
            "for five minutes after their last activity."
        )
        footer.setObjectName("subtitle")
        footer.setWordWrap(True)
        layout.addWidget(footer, 6, 0)

        log_panel = QGroupBox("Progress Log")
        log_panel.setObjectName("progressLogPanel")
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(12, 16, 12, 12)
        self.log_view = QPlainTextEdit()
        self.log_view.setObjectName("progressLog")
        self.log_view.setReadOnly(True)
        self.log_view.setMaximumBlockCount(PROGRESS_LOG_MAX_LINES)
        self.log_view.setMinimumHeight(PROGRESS_LOG_MIN_HEIGHT)
        log_layout.addWidget(self.log_view)
        layout.addWidget(log_panel, 7, 0, 3, 1)
        layout.setColumnStretch(0, 3)
        layout.setColumnStretch(1, 2)
        layout.setRowStretch(0, 0)
        layout.setRowStretch(1, 0)
        layout.setRowStretch(2, 0)
        layout.setRowStretch(3, 0)
        layout.setRowStretch(4, 0)
        layout.setRowStretch(5, 0)
        layout.setRowStretch(6, 0)
        layout.setRowStretch(7, 1)
        layout.setRowStretch(8, 1)
        layout.setRowStretch(9, 2)

        self.setStyleSheet(
            """
            QMainWindow { background: #ffffff; color: #173020; }
            QWidget#central {
                background-color: #ffffff;
                border: 1px solid rgba(200, 200, 200, 200);
                border-radius: 22px;
            }
            QLabel { color: #173020; font-size: 12px; }
            QLabel#title { font-size: 22px; font-weight: 700; }
            QLabel#subtitle { color: #405448; }
            QLabel#status { color: #235d38; font-weight: 650; }
            QLabel#health { font-size: 14px; font-weight: 700; }
            QLabel#path {
                background: #ffffff;
                border: 1px solid rgba(172, 216, 184, 70);
                border-radius: 8px;
                padding: 8px 10px;
                color: #173020;
            }
            QLineEdit#databasePath {
                background-color: #ffffff;
                border: 1px solid #4d795a;
                border-radius: 8px;
                padding: 8px 10px;
                color: #173020;
                selection-background-color: #235d38;
                font-size: 12px;
                font-weight: 600;
            }
            QGroupBox#databasePanel {
                color: #173020;
                background-color: #ffffff;
                border: 1px solid #536e5b;
            }
            QGroupBox#databasePanel::title {
                color: #173020;
                font-size: 12px;
                font-weight: 700;
            }
            QListWidget#clients {
                background: #ffffff;
                border: 1px solid rgba(172, 216, 184, 70);
                border-radius: 10px;
                padding: 8px;
                color: #173020;
                font-size: 12px;
            }
            QListWidget#documents {
                background: #ffffff;
                border: 1px solid rgba(172, 216, 184, 70);
                border-radius: 10px;
                padding: 6px;
                color: #173020;
                font-size: 11px;
            }
            QFrame#fileDropPanel {
                background-color: #e8f5e9;
                border: 2px dashed #536e5b;
                border-radius: 10px;
            }
            QLabel#fileDropLabel {
                color: #173020;
                font-size: 12px;
                font-weight: 650;
            }
            QPlainTextEdit#progressLog {
                background: #ffffff;
                border: 1px solid rgba(172, 216, 184, 70);
                border-radius: 10px;
                padding: 8px;
                color: #173020;
                font-family: Consolas, Menlo, monospace;
                font-size: 10px;
            }
            QGroupBox {
                color: #173020;
                font-size: 12px;
                font-weight: 650;
                border: 1px solid rgba(30, 55, 39, 85);
                border-radius: 14px;
                margin-top: 8px;
                padding-top: 8px;
                background-color: #ffffff;
            }
            QGroupBox::title {
                subcontrol-origin: margin;
                left: 12px;
                padding: 0 5px;
            }
            QMessageBox {
                background-color: #ffffff;
                color: #173020;
                font-size: 12px;
            }
            QMessageBox QLabel {
                background-color: transparent;
                color: #173020;
                font-size: 12px;
            }
            QMessageBox QPushButton {
                color: #173020;
                background-color: #ffffff;
                border: 1px solid #718477;
                border-radius: 5px;
                padding: 6px 14px;
                min-width: 72px;
                min-height: 20px;
                font-size: 12px;
                font-weight: 600;
            }
            QMessageBox QPushButton:hover {
                background-color: #f0f0f0;
            }
            QPushButton {
                color: #ffffff;
                font-weight: 650;
                font-size: 12px;
                padding: 7px 12px;
                min-height: 20px;
                border: 1px solid rgba(156, 224, 172, 210);
                border-radius: 22px;
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(72, 131, 83, 255),
                    stop:0.14 rgba(34, 91, 48, 255),
                    stop:0.48 rgba(12, 58, 30, 255),
                    stop:0.82 rgba(5, 38, 19, 255),
                    stop:1 rgba(2, 23, 11, 255));
            }
            QPushButton:hover {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(94, 157, 105, 255),
                    stop:0.14 rgba(42, 105, 57, 255),
                    stop:0.5 rgba(15, 69, 37, 255),
                    stop:1 rgba(4, 34, 18, 255));
            }
            QPushButton:pressed {
                background: qlineargradient(x1:0, y1:0, x2:0, y2:1,
                    stop:0 rgba(3, 28, 14, 255),
                    stop:0.35 rgba(5, 42, 21, 255),
                    stop:1 rgba(10, 57, 28, 255));
            }
            QPushButton:disabled { color: #bbc9be; }
            """
        )

        self.log_offsets: dict[Path, int] = {}
        self.log_read_errors: set[Path] = set()
        self.log_timer = QTimer(self)
        self.log_timer.setInterval(500)
        self.log_timer.timeout.connect(self.refresh_progress_logs)
        self.log_timer.start()
        self.record_progress("Dashboard ready.")
        self.refresh_progress_logs()

        self.timer = QTimer(self)
        self.timer.setInterval(1500)
        self.timer.timeout.connect(self.refresh_health)
        self.timer.start()
        try:
            DATABASE.mkdir(parents=True, exist_ok=True)
            self.record_progress(f"Default data folder is ready: {DATABASE}")
        except OSError as error:
            self.record_progress(
                f"Could not create default data folder ({type(error).__name__}): {error}"
            )
        if self.selected_database_path is not None and self.database_path != DATABASE:
            self.record_progress(
                f"Using the saved database selection instead of the default DATA folder: "
                f"{self.database_path}"
            )
        self.ensure_local_files()
        self.write_copilot_config()
        self.start_server()

    @staticmethod
    def _button(text: str, callback) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("capsuleButton")
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(callback)
        return button

    def ensure_local_files(self) -> None:
        if getattr(sys, "frozen", False):
            required_files = (Path(sys.executable),)
        elif sys.platform == "darwin":
            required_files = (Path(sys.executable), SERVER)
        else:
            required_files = (PYTHON, SERVER)
        missing = [str(path) for path in required_files if not path.is_file()]
        if missing:
            self.status.setText("App runtime or MCP server file is missing.")
            QMessageBox.critical(
                self,
                "LANCEDB MCP SERVER",
                "The app executable or portable runtime is missing.\n\n"
                + "\n".join(missing),
            )

    def write_copilot_config(self) -> None:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG.write_text(
            json.dumps(
                {"servers": {"lancedb-local": {"type": "http", "url": ENDPOINT}}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def validate_database_folder(path: Path) -> None:
        if not path.is_dir():
            raise ValueError("Choose an existing LanceDB database folder.")
        lancedb = importlib.import_module("lancedb")
        tables = lancedb.connect(str(path)).list_tables().tables
        if not tables:
            if path.resolve() == DATABASE.resolve():
                return
            raise ValueError(
                "The selected folder does not contain any LanceDB tables. "
                "Choose the database folder that contains its .lance table folders."
            )

    def load_database_path(self) -> Path | None:
        if not APP_SETTINGS.is_file():
            return None
        try:
            saved_path = Path(json.loads(APP_SETTINGS.read_text(encoding="utf-8"))["database_path"])
            self.validate_database_folder(saved_path)
            return saved_path.resolve()
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def save_database_path(self) -> None:
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
        temporary_settings = APP_SETTINGS.with_suffix(".tmp")
        temporary_settings.write_text(
            json.dumps({"database_path": str(self.database_path)}, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_settings.replace(APP_SETTINGS)

    def start_server(self) -> None:
        if self.server_process and self.server_process.poll() is None:
            self.status.setText("MCP server is already running.")
            return
        frozen = getattr(sys, "frozen", False)
        if not frozen and (not PYTHON.is_file() or not SERVER.is_file()):
            self.record_progress("MCP server not started: required runtime files are missing.")
            return
        try:
            self.record_progress("Starting local MCP server.")
            LOGS.mkdir(parents=True, exist_ok=True)
            self.database_path.mkdir(parents=True, exist_ok=True)
            self.stdout_log = (LOGS / "mcp-server.log").open("a", encoding="utf-8")
            self.stderr_log = (LOGS / "mcp-server-error.log").open("a", encoding="utf-8")
            env = os.environ.copy()
            if os.environ.get("LANCEDB_GUI_NO_SERVER") != "1":
                env["LANCEDB_URI"] = str(self.database_path)
                env["LANCEDB_MCP_PORT"] = str(PORT)
                command = (
                    [sys.executable, "--mcp-server"]
                    if frozen
                    else [str(PYTHON), str(SERVER)]
                )
                self.server_process = subprocess.Popen(
                    command,
                    cwd=APP_DIR,
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=self.stdout_log,
                    stderr=self.stderr_log,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
                self.status.setText("Starting MCP server...")
                self.record_progress("MCP server process started.")
            else:
                self.record_progress("MCP server startup skipped by configuration.")
        except OSError as error:
            self.status.setText(f"Could not start MCP server: {error}")
            self.record_progress(
                f"MCP server start failed ({type(error).__name__}): {error}"
            )

    def wait_for_database_health(self, path: Path, timeout: float = 10) -> bool:
        deadline = time.monotonic() + timeout
        expected_uri = str(path.resolve())
        while time.monotonic() < deadline:
            if self.server_process and self.server_process.poll() is not None:
                return False
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1) as response:
                    health = json.loads(response.read().decode("utf-8"))
                if (
                    health.get("connected")
                    and health.get("working")
                    and str(Path(health.get("database_uri", "")).resolve()) == expected_uri
                ):
                    return True
            except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError):
                pass
            time.sleep(0.2)
        return False

    def stop_server(self) -> None:
        if self.server_process and self.server_process.poll() is None:
            self.record_progress("Stopping local MCP server.")
            self.server_process.terminate()
            try:
                self.server_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server_process.kill()
                self.server_process.wait(timeout=5)
            self.record_progress("Local MCP server stopped.")
        self.server_process = None
        for log in (self.stdout_log, self.stderr_log):
            if log:
                log.close()
        self.stdout_log = self.stderr_log = None

    def connect_database(self) -> None:
        self.record_progress("Checking the local LanceDB connection.")
        self.start_server()
        self.refresh_health()

    def restart_server(self) -> None:
        self.record_progress("MCP server restart requested.")
        self.stop_server()
        self.start_server()

    def refresh_health(self) -> None:
        if self.server_process and self.server_process.poll() is not None:
            state = f"stopped:{self.server_process.returncode}"
            if getattr(self, "_last_health_state", None) != state:
                self.record_progress(
                    f"MCP server stopped (exit code {self.server_process.returncode})."
                )
                self._last_health_state = state
            self.status.setText(
                f"MCP server stopped (exit code {self.server_process.returncode}). Check logs."
            )
            self.database_health.setText("MCP server unavailable")
            self.database_health.setStyleSheet("color: #a32121;")
            return
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=1.5) as response:
                health = json.loads(response.read().decode("utf-8"))
            if health.get("connected") and health.get("working"):
                if getattr(self, "_last_health_state", None) != "connected":
                    self.record_progress("Database health check passed; LanceDB is connected.")
                    self._last_health_state = "connected"
                self.database_health.setText("Connected and working")
                self.database_health.setStyleSheet("color: #187e4a;")
                if not self.documents_loaded:
                    self.refresh_documents()
            else:
                if getattr(self, "_last_health_state", None) != "unhealthy":
                    self.record_progress("Database health check failed.")
                    self._last_health_state = "unhealthy"
                self.database_health.setText("Connection failed")
                self.database_health.setStyleSheet("color: #a32121;")
            self.document_count.setText(
                f"Documents in {self.database_path.name}: {health.get('documents', 0)}"
            )
            health_path = Path(health.get("database_uri", self.database_path))
            self.database_path_label.setText(str(health_path))
            self.database_path_label.setToolTip(str(health_path))
            self.database_path_label.setCursorPosition(0)
            self.refresh_clients(health.get("connected_apps", []))
            self.status.setText(f"MCP server is running at {ENDPOINT}")
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            if getattr(self, "_last_health_state", None) != "unavailable":
                self.record_progress(
                    f"Database health endpoint unavailable ({type(error).__name__})."
                )
                self._last_health_state = "unavailable"
            self.database_health.setText("MCP server is starting or unavailable.")
            self.database_health.setStyleSheet("color: #405448;")
            if not self.server_process or self.server_process.poll() is None:
                self.status.setText(f"Waiting for MCP health endpoint: {error}")

    def refresh_clients(self, apps: list[dict]) -> None:
        self.clients.clear()
        for app in apps:
            title = app.get("title") or app.get("name") or "MCP client"
            version = app.get("version", "")
            last_seen = app.get("last_seen", "")
            try:
                last_seen = datetime.fromisoformat(last_seen).astimezone().strftime("%H:%M")
            except (TypeError, ValueError):
                pass
            self.clients.addItem(f"{title} {version} — last active {last_seen}")
        if not apps:
            self.clients.addItem("No MCP clients have connected in the last five minutes.")

    def copy_endpoint(self) -> None:
        QApplication.clipboard().setText(ENDPOINT)
        self.status.setText("MCP endpoint copied.")
        self.record_progress("Copied the local MCP endpoint.")

    def record_progress(self, message: str) -> None:
        try:
            append_progress_log(LOGS / "app.log", message)
            self.refresh_progress_logs()
        except OSError as error:
            safe_error = sanitize_log_message(str(error))
            self.status.setText(f"Could not write progress log: {safe_error}")
            self.log_view.appendPlainText(
                f"Progress log write failed ({type(error).__name__}): {safe_error}"
            )

    def refresh_progress_logs(self) -> None:
        scroll_bar = self.log_view.verticalScrollBar()
        follow_tail = scroll_bar.value() >= scroll_bar.maximum()
        for name in ("app.log", "runtime.log"):
            path = LOGS / name
            try:
                if not path.is_file():
                    continue
                with path.open("r", encoding="utf-8", errors="replace") as log_file:
                    offset = self.log_offsets.get(path, 0)
                    size = path.stat().st_size
                    if size < offset:
                        offset = 0
                    if offset == 0 and size > 256 * 1024:
                        log_file.seek(size - 256 * 1024)
                        log_file.readline()
                    else:
                        log_file.seek(offset)
                    content = log_file.read(64 * 1024)
                    self.log_offsets[path] = log_file.tell()
                self.log_read_errors.discard(path)
                for line in content.splitlines():
                    if line:
                        display_line = f"[{name}] {line}"
                        if len(display_line) > PROGRESS_LOG_MAX_LINE_LENGTH:
                            display_line = (
                                display_line[: PROGRESS_LOG_MAX_LINE_LENGTH - 3] + "..."
                            )
                        self.log_view.appendPlainText(display_line)
            except OSError as error:
                if path not in self.log_read_errors:
                    self.log_view.appendPlainText(
                        f"[{name}] Read failed ({type(error).__name__})."
                    )
                    self.log_read_errors.add(path)
        if follow_tail:
            scroll_bar.setValue(scroll_bar.maximum())

    @staticmethod
    def _request_json(path: str, payload: dict | None = None) -> dict:
        body = None if payload is None else json.dumps(payload).encode("utf-8")
        request = urllib.request.Request(
            f"http://127.0.0.1:{PORT}{path}",
            data=body,
            headers={"Content-Type": "application/json"} if body is not None else {},
            method="POST" if body is not None else "GET",
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            try:
                response = json.loads(error.read().decode("utf-8"))
                message = response.get("error", str(error))
            except (UnicodeDecodeError, json.JSONDecodeError):
                message = str(error)
            raise RuntimeError(message) from error

    def _start_api_request(
        self, path: str, payload: dict | None, on_success, on_failure, on_finished
    ) -> None:
        worker = LocalApiRequestWorker(path, payload, self)
        self._api_workers.add(worker)
        worker.succeeded.connect(on_success)
        worker.failed.connect(on_failure)
        worker.finished.connect(on_finished)
        worker.finished.connect(self._release_api_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _release_api_worker(self) -> None:
        worker = self.sender()
        if isinstance(
            worker,
            (LocalApiRequestWorker, FileDropIngestWorker, FileDropUploadWorker),
        ):
            self._api_workers.discard(worker)

    def refresh_documents(self) -> None:
        if self.documents_request_pending:
            return
        self.documents_request_pending = True
        self.documents_loaded = True
        self.refresh_documents_button.setEnabled(False)
        self._start_api_request(
            "/documents",
            None,
            self._show_documents,
            self._documents_failed,
            self._finish_documents_request,
        )

    def _show_documents(self, response: dict) -> None:
        try:
            documents = response.get("documents")
            if not isinstance(documents, list):
                raise ValueError("The server returned an invalid document list.")
        except (ValueError, TypeError) as error:
            self._documents_failed(str(error))
            return

        self.documents.clear()
        for document in documents:
            content = " ".join((document.get("content") or "").split())
            preview = content if len(content) <= 100 else content[:97] + "..."
            source = document.get("source") or "unknown source"
            title = document.get("title")
            timestamp = document.get("created_at") or "unknown date"
            copies = document.get("copies", 1)
            suffix = f" ({copies} identical records)" if copies > 1 else ""
            heading = f"{title} — {source}" if title else source
            item = QListWidgetItem(f"{heading} — {timestamp}{suffix}\n{preview}")
            item.setData(Qt.ItemDataRole.UserRole, document.get("id"))
            item.setData(Qt.ItemDataRole.UserRole + 1, copies)
            item.setToolTip(document.get("content") or "")
            self.documents.addItem(item)
        if not documents:
            self.documents.addItem("No ingested records in this database.")
        self.documents_loaded = True
        record_count = sum(document.get("copies", 1) for document in documents)
        self.record_progress(
            f"Ingested-record list refreshed: {record_count} record(s) across "
            f"{len(documents)} distinct entry/entries."
        )

    def _documents_failed(self, message: str) -> None:
        self.documents_loaded = False
        self.documents.clear()
        self.documents.addItem("Could not load ingested records; see Progress Log.")
        self.record_progress(f"Could not load ingested records: {message}")

    def _finish_documents_request(self) -> None:
        self.documents_request_pending = False
        self.refresh_documents_button.setEnabled(True)

    def save_important_item(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Save Important Item")
        dialog.setMinimumSize(620, 480)
        dialog.setStyleSheet(
            "QDialog { background: #ffffff; color: #173020; }"
            "QLabel { color: #173020; }"
            "QLineEdit, QPlainTextEdit { background: #ffffff; color: #173020; "
            "border: 1px solid #718477; border-radius: 4px; padding: 5px; }"
        )
        dialog_layout = QVBoxLayout(dialog)
        dialog_layout.addWidget(
            QLabel(
                "Save only the content entered here. Nothing is collected "
                "automatically from files or conversations."
            )
        )
        dialog_layout.addWidget(QLabel("Title"))
        title_input = QLineEdit()
        title_input.setPlaceholderText("e.g. Backup script")
        dialog_layout.addWidget(title_input)
        dialog_layout.addWidget(QLabel("Source"))
        source_input = QLineEdit("copilot")
        dialog_layout.addWidget(source_input)
        dialog_layout.addWidget(QLabel("Script, code, or important note"))
        content_input = QPlainTextEdit()
        content_input.setPlaceholderText("Paste the item to save")
        dialog_layout.addWidget(content_input, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        dialog_layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        title = title_input.text().strip()
        content = content_input.toPlainText()
        source = source_input.text().strip()
        if not title or not content.strip():
            QMessageBox.warning(
                self,
                "Cannot save item",
                "Enter a title and non-empty content before saving.",
            )
            return
        if self.save_item_request_pending:
            return

        self.save_item_request_pending = True
        self.save_item_button.setEnabled(False)
        self.record_progress("Saving an explicitly submitted titled item.")
        self._start_api_request(
            "/documents/store",
            {"title": title, "content": content, "source": source},
            self._important_item_saved,
            self._important_item_save_failed,
            self._finish_important_item_save,
        )

    def _important_item_saved(self, _response: dict) -> None:
        stored = _response.get("stored", 0)
        duplicates = _response.get("duplicates", 0)
        if duplicates:
            self.record_progress(
                f"Important item was not saved: {duplicates} exact duplicate detected."
            )
        else:
            self.record_progress("Important item saved to the active local database.")
        self.refresh_documents()

    def _important_item_save_failed(self, message: str) -> None:
        self.record_progress(f"Important-item save failed: {message}")
        QMessageBox.warning(
            self,
            "Could not save item",
            f"The item was not saved: {message}",
        )

    def _finish_important_item_save(self) -> None:
        self.save_item_request_pending = False
        self.save_item_button.setEnabled(True)

    def ingest_dropped_files(self, paths: list[str]) -> None:
        if self.file_ingestion_pending:
            self.record_progress("File drop ignored: an ingestion is already in progress.")
            return
        self.file_ingestion_pending = True
        self.file_drop_panel.set_status("Checking dropped files...")
        self.file_drop_panel.setEnabled(False)
        self.record_progress(f"Checking {len(paths)} explicitly dropped file(s).")
        worker = FileDropIngestWorker(paths, self)
        self._api_workers.add(worker)
        worker.prepared.connect(self._file_ingestion_prepared)
        worker.failed.connect(self._file_ingestion_preparation_failed)
        worker.finished.connect(self._release_api_worker)
        worker.finished.connect(worker.deleteLater)
        worker.start()

    def _file_ingestion_prepared(self, result: dict) -> None:
        records = list(result.get("records", []))
        skipped = list(result.get("skipped", []))
        approved, declined = resolve_pdf_credential_confirmations(
            result.get("confirmation_required", []),
            lambda file_name: confirm_pdf_credential_ingestion(self, file_name),
            self.record_progress,
        )
        records.extend(approved)
        skipped.extend(declined)

        if not records:
            self._file_ingestion_completed({"stored": 0, "skipped": skipped})
            return

        worker = FileDropUploadWorker(records, skipped, self)
        self._api_workers.add(worker)
        worker.completed.connect(self._file_ingestion_completed)
        worker.failed.connect(self._file_ingestion_failed)
        worker.finished.connect(self._release_api_worker)
        worker.finished.connect(worker.deleteLater)
        self.record_progress(f"Uploading {len(records)} explicitly approved file chunk(s).")
        worker.start()

    def _file_ingestion_completed(self, result: dict) -> None:
        stored = result.get("stored", 0)
        duplicates = result.get("duplicates", 0)
        skipped = result.get("skipped", [])
        self.file_drop_panel.set_status(
            f"Saved {stored} chunk(s); skipped {duplicates} duplicate(s), "
            f"{len(skipped)} file(s)."
        )
        self.record_progress(
            f"File ingestion completed: {stored} chunk(s) saved; "
            f"{duplicates} exact duplicate(s) skipped; "
            f"{len(skipped)} file(s) skipped."
        )
        if duplicates:
            self.record_progress(
                f"Skipped {duplicates} exact duplicate file chunk(s); no duplicate records added."
            )
        for item in skipped[:20]:
            reason = item.split(": ", 1)[-1]
            self.record_progress(f"Dropped file skipped: {reason}")
        if stored:
            self.refresh_documents()
        if duplicates:
            summary = (
                f"Duplicate content skipped: {duplicates} item(s) already exist."
            )
            if stored:
                summary = (
                    f"Saved {stored} item(s). "
                    f"Duplicate content skipped: {duplicates} item(s) already exist."
                )
            QMessageBox.information(
                self,
                "Duplicate content skipped",
                summary,
            )
        if skipped:
            QMessageBox.warning(
                self,
                "Some files were not ingested",
                "\n".join(skipped[:20])
                + ("\nAdditional skipped items omitted." if len(skipped) > 20 else ""),
            )
        self._finish_file_ingestion()

    def _file_ingestion_failed(self, message: str, stored: int = 0) -> None:
        if stored:
            failure = (
                f"Ingestion stopped after saving {stored} chunk(s); "
                f"later batches were not saved: {message}"
            )
        else:
            failure = f"No file chunks were saved: {message}"
        self.file_drop_panel.set_status("Ingestion failed. See Progress Log.")
        self.record_progress(f"File ingestion failed: {failure}")
        QMessageBox.warning(
            self, "File ingestion failed", failure
        )
        self._finish_file_ingestion()

    def _file_ingestion_preparation_failed(self, message: str) -> None:
        self._file_ingestion_failed(message)

    def _finish_file_ingestion(self) -> None:
        self.file_ingestion_pending = False
        self.file_drop_panel.setEnabled(True)

    def browse_database(self) -> None:
        dialog = QFileDialog(self, "Select LanceDB database folder", str(self.database_path))
        dialog.setFileMode(QFileDialog.FileMode.Directory)
        dialog.setOption(QFileDialog.Option.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
        if dialog.exec() != QFileDialog.DialogCode.Accepted:
            return
        selected_files = dialog.selectedFiles()
        if not selected_files:
            return

        selected_path = Path(selected_files[0]).expanduser().resolve()
        if selected_path == self.database_path:
            return
        try:
            self.validate_database_folder(selected_path)
        except (OSError, ValueError) as error:
            self.record_progress(
                f"Database folder validation failed ({type(error).__name__}): {error}"
            )
            QMessageBox.warning(self, "Invalid LanceDB folder", str(error))
            return

        previous_path = self.database_path
        previous_selection = self.selected_database_path
        self.documents_loaded = False
        self.record_progress("Switching the active database folder.")
        self.stop_server()
        self.database_path = selected_path
        self.start_server()
        if not self.wait_for_database_health(selected_path):
            self.database_path = previous_path
            self.selected_database_path = previous_selection
            self.stop_server()
            self.start_server()
            QMessageBox.critical(
                self,
                "Could not connect to database",
                "The MCP server could not connect to the selected database folder. "
                "The previous database has been restored.",
            )
            self.record_progress("Database switch failed; restored the previous database.")
            return

        try:
            self.selected_database_path = selected_path
            self.save_database_path()
        except OSError as error:
            self.stop_server()
            self.database_path = previous_path
            self.selected_database_path = previous_selection
            self.start_server()
            QMessageBox.critical(
                self,
                "Could not save database folder",
                f"The database selection could not be saved: {error}",
            )
            self.record_progress(
                f"Could not save database folder selection ({type(error).__name__}): {error}"
            )
            return

        self.database_path_label.setText(str(self.database_path))
        self.database_path_label.setToolTip(str(self.database_path))
        self.database_path_label.setCursorPosition(0)
        self.refresh_health()
        self.status.setText(f"Connected to LanceDB folder: {self.database_path}")
        self.record_progress("Active database folder changed successfully.")

    def showEvent(self, event) -> None:
        super().showEvent(event)
        if sys.platform == "win32" and not apply_windows_acrylic(int(self.winId())):
            self.status.setText("Windows acrylic unavailable; using translucent Qt surface.")

    def closeEvent(self, event) -> None:
        self.timer.stop()
        self.log_timer.stop()
        for worker in tuple(self._api_workers):
            worker.wait(6000)
        self.stop_server()
        super().closeEvent(event)


class LocalApiRequestWorker(QThread):
    succeeded = Signal(dict)
    failed = Signal(str)

    def __init__(
        self, path: str, payload: dict | None = None, parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.path = path
        self.payload = payload

    def run(self) -> None:
        try:
            response = LanceDBWindow._request_json(self.path, self.payload)
        except Exception as error:
            self.failed.emit(
                f"{type(error).__name__}: {sanitize_log_message(str(error))}"
            )
        else:
            self.succeeded.emit(response)


class FileDropIngestWorker(QThread):
    prepared = Signal(dict)
    failed = Signal(str)

    def __init__(self, paths: list[str], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.paths = paths

    def run(self) -> None:
        try:
            records, skipped, confirmation_required = prepare_dropped_files(self.paths)
        except Exception as error:
            self.failed.emit(
                f"{type(error).__name__}: {sanitize_log_message(str(error))}"
            )
        else:
            self.prepared.emit(
                {
                    "records": records,
                    "skipped": skipped,
                    "confirmation_required": confirmation_required,
                }
            )


class FileDropUploadWorker(QThread):
    completed = Signal(dict)
    failed = Signal(str, int)
    MAX_UPLOAD_BATCH_SIZE = MAX_CHUNKS_PER_UPLOAD_BATCH

    def __init__(
        self,
        records: list[dict[str, str]],
        skipped: list[str],
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.records = records
        self.skipped = skipped

    def run(self) -> None:
        stored_total = 0
        duplicates_total = 0
        try:
            for batch_number, start in enumerate(
                range(0, len(self.records), self.MAX_UPLOAD_BATCH_SIZE),
                start=1,
            ):
                batch = self.records[start : start + self.MAX_UPLOAD_BATCH_SIZE]
                try:
                    response = LanceDBWindow._request_json(
                        "/documents/store-batch", {"documents": batch}
                    )
                    stored = response.get("stored")
                    duplicates = response.get("duplicates")
                    if (
                        not isinstance(stored, int)
                        or not isinstance(duplicates, int)
                        or stored + duplicates != len(batch)
                    ):
                        raise ValueError(
                            f"The server returned an invalid count for batch {batch_number}."
                        )
                except Exception as error:
                    if stored_total:
                        raise RuntimeError(
                            f"Batch {batch_number} failed after {stored_total} "
                            "chunk(s) were saved; earlier batches remain in the database."
                        ) from error
                    raise
                stored_total += stored
                duplicates_total += duplicates
        except Exception as error:
            self.failed.emit(
                f"{type(error).__name__}: {sanitize_log_message(str(error))}",
                stored_total,
            )
        else:
            self.completed.emit(
                {
                    "stored": stored_total,
                    "duplicates": duplicates_total,
                    "skipped": self.skipped,
                }
            )


class RuntimeUpdateWorker(QThread):
    progress = Signal(str)
    completed = Signal(str)
    failed = Signal(str)

    def run(self) -> None:
        try:
            append_progress_log(LOGS / "runtime.log", "Checking LanceDB runtime.")
            result = ensure_lancedb_runtime(
                APP_DIR,
                getattr(sys, "frozen", False),
                self.report_progress,
            )
            append_progress_log(
                LOGS / "runtime.log", f"LanceDB runtime ready: {result}"
            )
            self.completed.emit(result)
        except Exception as error:
            safe_error = sanitize_log_message(str(error))
            try:
                append_progress_log(
                    LOGS / "runtime.log",
                    f"Runtime preparation failed ({type(error).__name__}): {safe_error}",
                )
            except OSError:
                pass
            self.failed.emit(safe_error)

    def report_progress(self, message: str) -> None:
        append_progress_log(LOGS / "runtime.log", message)
        self.progress.emit(message)


class RuntimeUpdateDialog(QDialog):
    allow_close = False

    def closeEvent(self, event) -> None:
        if self.allow_close:
            event.accept()
        else:
            event.ignore()


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "--validate-pdf-runtime":
        try:
            version = validate_pdf_runtime()
            print(f"PDF text extraction runtime ready (PyMuPDF {version}).")
            return 0
        except Exception as error:
            print(f"PDF text extraction runtime validation failed: {error}", file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--validate-office-runtime":
        try:
            from file_ingestion import validate_office_runtime

            excel_version, word_version = validate_office_runtime()
            print(
                "Excel/Word text extraction runtime ready "
                f"(openpyxl {excel_version}, python-docx {word_version})."
            )
            return 0
        except Exception as error:
            print(f"Office text extraction runtime validation failed: {error}", file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--validate-lancedb-runtime":
        try:
            _activate_and_validate(
                Path(sys.argv[2]),
                json.loads(sys.argv[3]),
            )
            return 0
        except BaseException as error:
            print(f"LanceDB runtime validation failed: {error}", file=sys.stderr)
            return 1
    if len(sys.argv) > 1 and sys.argv[1] == "--mcp-server":
        activate_from_environment()
        import server

        return server.run_server()

    app = QApplication(sys.argv)
    app.setApplicationName("LANCEDB MCP SERVER")
    app.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    app.setFont(QFont("Segoe UI", 10))

    dialog = RuntimeUpdateDialog()
    dialog.setWindowTitle("LANCEDB MCP SERVER")
    dialog.setWindowIcon(QIcon(str(APP_ICON_PATH)))
    dialog.setMinimumWidth(420)
    dialog.setWindowFlags(
        dialog.windowFlags() & ~Qt.WindowType.WindowContextHelpButtonHint
    )
    dialog_layout = QVBoxLayout(dialog)
    dialog_layout.setContentsMargins(26, 22, 26, 22)
    dialog_layout.setSpacing(12)
    dialog_title = QLabel("Preparing LanceDB runtime")
    dialog_title.setStyleSheet("font-size: 14px; font-weight: 700;")
    dialog_status = QLabel("Checking the latest stable release from PyPI...")
    dialog_status.setWordWrap(True)
    dialog_layout.addWidget(dialog_title)
    dialog_layout.addWidget(dialog_status)
    dialog.show()

    worker = RuntimeUpdateWorker()
    worker.progress.connect(dialog_status.setText)
    window_holder: list[LanceDBWindow] = []

    def open_dashboard(message: str) -> None:
        window = LanceDBWindow(message)
        window_holder.append(window)
        screen = window.screen() or app.primaryScreen()
        if screen is not None:
            prepare_window_geometry(window, screen.availableGeometry())
        window.showMaximized()
        dialog.allow_close = True
        dialog.hide()

    def update_failed(message: str) -> None:
        try:
            append_progress_log(
                LOGS / "runtime.log", f"Runtime update failed: {message}"
            )
        except OSError:
            pass
        QMessageBox.critical(
            dialog,
            "LanceDB runtime unavailable",
            message,
        )
        dialog.allow_close = True
        dialog.close()
        app.quit()

    worker.completed.connect(open_dashboard)
    worker.failed.connect(update_failed)
    worker.start()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
