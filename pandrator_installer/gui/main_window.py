"""Main Qt window for the Pandrator installer and launcher."""

import atexit
import json
import logging
import os
import sqlite3
import sys
import webbrowser

from PyQt6.QtCore import QThread, Qt, QTimer
from PyQt6.QtGui import QAction, QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QFileDialog, QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar,
    QPushButton, QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget, QComboBox, QDialog, QMenu, QSystemTrayIcon,
    QLineEdit, QSpinBox,
)

from ..backend_catalog import (
    TTS_BACKENDS,
    formatted_crispasr_languages,
    formatted_crispasr_model_licences,
)
from ..catalog import (
    COMPONENTS,
    LINUX_DEFERRED_INSTALL_COMPONENT_KEYS,
    LINUX_DEFERRED_REASON_BY_COMPONENT,
)
from ..components import ComponentOperationsMixin
from ..constants import (
    CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG,
    KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG,
    KOKORO_GPU_SUPPORT_CONFIG_FLAG,
    MAGPIE_GPU_SUPPORT_CONFIG_FLAG,
    RVC_GPU_SUPPORT_CONFIG_FLAG,
)
from ..models import (
    InstallSelection,
    LaunchSelection,
    normalize_password_scope,
    qwen_effective_model_size,
)
from ..operations import OperationsMixin
from ..pixi import PixiEnvironmentMixin
from ..platforms import (
    is_appimage_environment,
    is_linux,
    is_windows,
    remember_launcher_workspace,
)
from ..reporting import NullReporter
from ..runtime import RuntimeMixin
from ..storage import StorageMixin
from ..workflows import WorkflowMixin
from ..crispasr import detect_compute_backends
from .actions import GuiActionsMixin
from .backend_card import BackendOptionCard
from .support import (
    GitHubLinkButton,
    InfoDialog,
    QtLogEmitter,
    QtLogHandler,
    ToggleSwitch,
)


class PandratorInstaller(
    StorageMixin,
    OperationsMixin,
    PixiEnvironmentMixin,
    ComponentOperationsMixin,
    WorkflowMixin,
    RuntimeMixin,
    GuiActionsMixin,
    QMainWindow,
):
    def __init__(self, headless=False, working_dir=None, skip_space_warning=False):
        super().__init__()
        self.headless = bool(headless)
        self.initial_working_dir = os.path.abspath(working_dir or os.getcwd())

        # Check for spaces in the working directory
        if ' ' in self.initial_working_dir and not skip_space_warning and not self.headless:
            self.show_space_warning()

        # Define instance variables for checkboxes
        # Installation options
        self.pandrator_var = False
        self.xtts_var = False
        self.xtts_cpu_var = False
        self.voxcpm_var = False
        self.fishs2_var = False
        self.fishs2_cpu_var = False
        self.fishs2_backend = "auto"
        self.fishs2_model_quant = "q6_k"
        self.fishs2_model_quant_manually_set = False
        self.silero_var = False
        self.voxtral_var = False
        self.kokoro_var = False
        self.kokoro_cpu_var = False
        self.rvc_var = False
        self.rvc_cpu_var = False
        self.crispasr_var = False
        self.crispasr_backend = "auto"
        self.crispasr_engine = "whisper-large-v3"
        self.crispasr_model_quantization = "f16"
        self.crispasr_backend_manually_set = False
        self.xtts_finetuning_var = False
        self.chatterbox_var = False
        self.chatterbox_cpu_var = False
        self.kobold_qwen_var = False
        self.kobold_qwen_cpu_var = False
        self.kobold_qwen_backend = "auto"
        self.kobold_qwen_model_size = "0.6b"
        self.kobold_qwen_quantization = "f16"
        self.kobold_qwen_initial_model = "base"
        self.kobold_qwen_settings_manually_set = False
        self.magpie_var = False
        self.magpie_cpu_var = False

        # Launch options
        self.launch_pandrator_var = True
        self.pandrator_owner_password = None
        self.pandrator_password_scope_var = "none"
        self.launch_rvc_var = False
        self.rvc_cpu_launch_var = False
        self.launch_xtts_var = False
        self.disable_deepspeed_var = False
        self.xtts_cpu_launch_var = False
        self.launch_voxcpm_var = False
        self.launch_fishs2_var = False
        self.fishs2_cpu_launch_var = False
        self.launch_voxtral_var = False
        self.launch_kokoro_var = False
        self.kokoro_cpu_launch_var = False
        self.launch_silero_var = False
        self.launch_chatterbox_var = False
        self.chatterbox_cpu_launch_var = False
        self.launch_kobold_qwen_var = False
        self.kobold_qwen_cpu_launch_var = False
        self.launch_magpie_var = False
        self.magpie_cpu_launch_var = False

        # Initialize process attributes
        self.xtts_process = None
        self.voxcpm_process = None
        self.fishs2_process = None
        self.pandrator_process = None
        self.silero_process = None
        self.voxtral_process = None
        self.kokoro_process = None
        self.chatterbox_process = None
        self.kobold_qwen_process = None
        self.magpie_process = None
        self.rvc_process = None
        self.backend_stop_targets = []

        # Worker thread
        self.worker = None
        self.reporter = NullReporter()

        # Set up the main window
        self.setWindowTitle("Pandrator Installer & Launcher")

        # Full-width backend cards remain readable at ordinary laptop resolutions.
        screen_size = QApplication.primaryScreen().availableGeometry().size()
        width = min(960, int(screen_size.width() * 0.86))
        height = min(720, int(screen_size.height() * 0.82))
        self.setMinimumSize(min(720, width), min(560, height))
        self.resize(width, height)

        # Create central widget and main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)

        # Create header with title and info button
        header_layout = QHBoxLayout()

        # Title
        self.title_label = QLabel("Pandrator Installer & Launcher")
        self.title_label.setObjectName("titleLabel")
        title_font = QFont("Arial", 17, QFont.Weight.Bold)
        self.title_label.setFont(title_font)
        header_layout.addWidget(self.title_label)
        header_layout.addStretch()

        self.github_button = GitHubLinkButton()
        header_layout.addWidget(self.github_button)

        # Info button
        self.info_button = QPushButton("About")
        self.info_button.setObjectName("secondaryButton")
        self.info_button.clicked.connect(self.show_info)
        header_layout.addWidget(self.info_button)

        self.main_layout.addLayout(header_layout)

        # Create tab widget
        self.tabs = QTabWidget()
        self.main_layout.addWidget(self.tabs)

        # Create Install and Launch tabs
        self.install_tab = QWidget()
        self.launch_tab = QWidget()
        self.logs_tab = QWidget()

        self.tabs.addTab(self.install_tab, "Install")
        self.tabs.addTab(self.launch_tab, "Launch")
        self.tabs.addTab(self.logs_tab, "Logs")

        # Set up tabs
        self.setup_install_tab()
        self.setup_launch_tab()
        self.setup_logs_tab()

        # Progress bar and status label at the bottom
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(False)
        self.main_layout.addWidget(self.progress_bar)

        self.status_label = QLabel("Ready")
        self.status_label.setObjectName("statusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.status_label.setWordWrap(True)
        self.status_label.setMinimumWidth(0)
        self.status_label.setMaximumHeight(44)
        self.status_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        status_font = QFont("Arial", 11)
        self.status_label.setFont(status_font)
        self.main_layout.addWidget(self.status_label)

        # Initialize log file path
        self.log_filename = None
        self.log_emitter = QtLogEmitter()
        self.log_emitter.message_logged.connect(self.append_log_message)
        self.tls_configured = False
        self.ca_bundle_path = None

        # Initialize state
        self.refresh_ui_state()
        self.set_startup_tab()
        self.backend_status_timer = QTimer(self)
        self.backend_status_timer.timeout.connect(self.update_backend_runtime_controls)
        self.backend_status_timer.start(2000)
        self._quit_requested = False
        self.setup_system_tray()
        atexit.register(self.shutdown_apps)

    def setup_system_tray(self):
        """Keep locally supervised services available when the launcher is minimized."""
        self.tray_icon = QSystemTrayIcon(self.windowIcon(), self)
        menu = QMenu(self)
        show_action = QAction("Show Pandrator Launcher", self)
        show_action.triggered.connect(self.restore_from_tray)
        stop_action = QAction("Stop Everything", self)
        stop_action.triggered.connect(self.stop_everything_from_tray)
        quit_action = QAction("Quit Launcher", self)
        quit_action.triggered.connect(self.quit_from_tray)
        menu.addAction(show_action)
        menu.addAction(stop_action)
        menu.addSeparator()
        menu.addAction(quit_action)
        self.tray_icon.setContextMenu(menu)
        self.tray_icon.activated.connect(
            lambda reason: self.restore_from_tray()
            if reason == QSystemTrayIcon.ActivationReason.Trigger
            else None
        )
        self.tray_icon.show()

    def restore_from_tray(self):
        self.showNormal()
        self.raise_()
        self.activateWindow()

    def stop_everything_from_tray(self):
        self.shutdown_apps()
        self.update_status("All supervised processes stopped.")

    def quit_from_tray(self):
        self._quit_requested = True
        self.shutdown_apps()
        QApplication.instance().quit()

    def set_startup_tab(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        install_markers = (
            os.path.join(pandrator_path, 'config.json'),
            os.path.join(pandrator_path, 'Pandrator', 'pyproject.toml'),
            os.path.join(pandrator_path, 'Pandrator', 'pandrator', 'web', 'static', 'index.html'),
        )
        if all(os.path.exists(marker) for marker in install_markers):
            self.tabs.setCurrentWidget(self.launch_tab)
        else:
            self.tabs.setCurrentWidget(self.install_tab)

    def show_space_warning(self):
        """Show warning when path contains spaces"""
        if self.headless:
            logging.warning(
                "Installation path contains spaces in headless mode: %s. "
                "Third-party tooling may fail in this location.",
                self.initial_working_dir,
            )
            return

        suggested_path = r"C:\Pandrator" if is_windows() else os.path.expanduser("~/pandrator-workspace")
        warning_message = (
            f"⚠️ WARNING: Your installation path contains spaces:\n\n"
            f"{self.initial_working_dir}\n\n"
            f"Some third-party tools still have trouble when installed from paths with spaces.\n\n"
            f"It's strongly recommended to move this installer to a path without spaces, such as:\n"
            f"{suggested_path}\n\n"
            f"Would you like to exit the installer so you can move it to a better location?"
        )

        reply = QMessageBox.warning(
            self,
            "Path Contains Spaces",
            warning_message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes
        )

        if reply == QMessageBox.StandardButton.Yes:
            sys.exit(0)
        else:
            QMessageBox.information(
                self,
                "Continuing With Risk",
                "Installation will continue, but you may encounter errors.\n"
                "If installation fails, please restart the installer from a path without spaces."
            )

    def show_info(self):
        """Show the information dialog"""
        dialog = InfoDialog(self)
        dialog.exec()

    def _create_scrollable_tab_content(self, tab):
        tab_layout = QVBoxLayout(tab)
        tab_layout.setContentsMargins(0, 0, 0, 0)

        scroll_area = QScrollArea()
        scroll_area.setObjectName("installerScrollArea")
        scroll_area.setWidgetResizable(True)
        scroll_area.setFrameShape(QFrame.Shape.NoFrame)
        scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )

        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(12, 12, 12, 12)
        content_layout.setSpacing(12)
        scroll_area.setWidget(content)
        tab_layout.addWidget(scroll_area)
        return tab_layout, content_layout

    @staticmethod
    def _create_option_card(
        control,
        description,
        extra_controls=(),
        voice_capability="",
        details="",
        languages="",
        models="",
        voice_cloning=None,
        prebuilt_voices=None,
    ):
        # Keep compatibility for non-TTS cards while TTS cards use explicit,
        # paired capability states from the tested backend catalogue.
        if voice_cloning is None and prebuilt_voices is None and voice_capability:
            capabilities = (
                (voice_capability,)
                if isinstance(voice_capability, str)
                else tuple(voice_capability)
            )
            voice_cloning = "Voice cloning" in capabilities
            prebuilt_voices = "Pre-built voices" in capabilities
        return BackendOptionCard(
            control,
            description,
            extra_controls=extra_controls,
            details=details,
            languages=languages,
            models=models,
            voice_cloning=voice_cloning,
            prebuilt_voices=prebuilt_voices,
        )

    @staticmethod
    def _add_option_cards(grid, cards):
        grid.setContentsMargins(8, 10, 8, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 0)
        for index, card in enumerate(cards):
            grid.addWidget(card, index, 0, 1, 2)

    @staticmethod
    def _create_intro(text):
        label = QLabel(text)
        label.setObjectName("introLabel")
        label.setWordWrap(True)
        label.setMinimumWidth(0)
        return label

    def setup_install_tab(self):
        """Set up the Install tab"""
        layout, content_layout = self._create_scrollable_tab_content(self.install_tab)
        content_layout.addWidget(
            self._create_intro(
                "Choose the local services and optional tools you want. "
                "You can return later to add more."
            )
        )

        location_group = QFrame()
        location_group.setObjectName("installLocationRow")
        location_layout = QHBoxLayout(location_group)
        location_layout.setContentsMargins(13, 7, 8, 7)
        location_heading = QLabel("INSTALL TO")
        location_heading.setObjectName("installLocationHeading")
        location_layout.addWidget(location_heading)
        self.install_location_label = QLabel()
        self.install_location_label.setObjectName("installLocationPath")
        self.install_location_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        location_layout.addWidget(self.install_location_label, 1)
        self.change_install_location_button = QPushButton("Change")
        self.change_install_location_button.setObjectName("linkButton")
        self.change_install_location_button.clicked.connect(self.choose_install_workspace)
        location_layout.addWidget(self.change_install_location_button)
        content_layout.addWidget(location_group)
        self.update_install_location_label()

        core_group = QGroupBox("Core application")
        core_layout = QVBoxLayout(core_group)
        core_layout.setContentsMargins(8, 10, 8, 8)
        self.pandrator_checkbox = ToggleSwitch("Install Pandrator")
        self.pandrator_checkbox.setChecked(True)
        core_layout.addWidget(
            self._create_option_card(
                self.pandrator_checkbox,
                "The desktop app and its required runtime.",
            )
        )
        content_layout.addWidget(core_group)

        engines_group = QGroupBox("Text-to-speech engines")
        engines_grid = QGridLayout(engines_group)

        self.xtts_checkbox = ToggleSwitch("Install XTTS")
        self.xtts_cpu_checkbox = QCheckBox("Use CPU-only runtime")
        self.voxcpm_checkbox = ToggleSwitch("Install VoxCPM2")
        self.fishs2_checkbox = ToggleSwitch("Install FishS2")
        self.silero_checkbox = ToggleSwitch("Install Silero")
        self.voxtral_checkbox = ToggleSwitch("Install Voxtral")
        self.kokoro_checkbox = ToggleSwitch("Install Kokoro")
        self.kokoro_cpu_checkbox = QCheckBox("Use CPU-only runtime")
        self.chatterbox_checkbox = ToggleSwitch("Install Chatterbox")
        self.chatterbox_cpu_checkbox = QCheckBox("Use CPU-only runtime")
        self.kobold_qwen_checkbox = ToggleSwitch("Install Qwen3 TTS")
        self.kobold_qwen_cpu_checkbox = QCheckBox("Use CPU-only runtime")
        self.kobold_qwen_settings_button = QPushButton("Runtime and model settings…")
        self.kobold_qwen_settings_button.clicked.connect(
            lambda: self.show_kobold_qwen_config_dialog(force=True)
        )
        self.magpie_checkbox = ToggleSwitch("Install Magpie")
        self.magpie_cpu_checkbox = QCheckBox("Use CPU-only runtime")

        def tts_card(key, control, extra_controls=()):
            presentation = TTS_BACKENDS[key]
            card = self._create_option_card(
                control,
                presentation.summary,
                extra_controls,
                details=presentation.note,
                languages=presentation.formatted_languages,
                models=presentation.formatted_model_licences,
                voice_cloning=presentation.voice_cloning,
                prebuilt_voices=presentation.prebuilt_voices,
            )
            card.setProperty("backendKey", key)
            return card

        engine_cards = (
            tts_card("kokoro", self.kokoro_checkbox, (self.kokoro_cpu_checkbox,)),
            tts_card(
                "kobold_qwen",
                self.kobold_qwen_checkbox,
                (self.kobold_qwen_cpu_checkbox, self.kobold_qwen_settings_button),
            ),
            tts_card("xtts", self.xtts_checkbox, (self.xtts_cpu_checkbox,)),
            tts_card("voxcpm", self.voxcpm_checkbox),
            tts_card("fishs2", self.fishs2_checkbox),
            tts_card("voxtral", self.voxtral_checkbox),
            tts_card("silero", self.silero_checkbox),
            tts_card(
                "chatterbox",
                self.chatterbox_checkbox,
                (self.chatterbox_cpu_checkbox,),
            ),
            tts_card("magpie", self.magpie_checkbox, (self.magpie_cpu_checkbox,)),
        )
        self.tts_engine_cards = engine_cards
        self._add_option_cards(engines_grid, engine_cards)
        content_layout.addWidget(engines_group)

        stt_group = QGroupBox("STT backends")
        stt_grid = QGridLayout(stt_group)
        self.crispasr_checkbox = QCheckBox("CrispASR transcription")
        self.crispasr_settings_button = QPushButton("Runtime settings…")
        self.crispasr_settings_button.clicked.connect(
            lambda: self.show_crispasr_config_dialog(force=True)
        )
        stt_cards = (
            self._create_option_card(
                self.crispasr_checkbox,
                "One native runtime for Whisper large-v3 and Parakeet 0.6B v3 with CPU, CUDA, Vulkan, and Apple Metal support.",
                (self.crispasr_settings_button,),
                languages=formatted_crispasr_languages(),
                models=formatted_crispasr_model_licences(),
                details="Choose FP16 or a supported quantized model, plus an explicit accelerator when automatic detection is not appropriate. VAD, decoding, chunking, hotwords, and language identification remain adjustable per session in Pandrator.",
            ),
        )
        self._add_option_cards(stt_grid, stt_cards)
        content_layout.addWidget(stt_group)

        speech_to_speech_group = QGroupBox("Speech to Speech")
        speech_to_speech_grid = QGridLayout(speech_to_speech_group)
        self.rvc_checkbox = QCheckBox("RVC voice conversion")
        self.rvc_cpu_checkbox = QCheckBox("Use CPU-only runtime")
        speech_to_speech_cards = (
            self._create_option_card(
                self.rvc_checkbox,
                "Reshapes generated speech with an RVC voice model for voice conversion and post-processing.",
                (self.rvc_cpu_checkbox,),
                details=(
                    "No RVC voice model is bundled. Uploaded .pth and .index files retain "
                    "their own terms, which the user is responsible for reviewing."
                ),
            ),
        )
        self._add_option_cards(speech_to_speech_grid, speech_to_speech_cards)
        content_layout.addWidget(speech_to_speech_group)

        tools_group = QGroupBox("Training tools")
        tools_grid = QGridLayout(tools_group)
        self.xtts_finetuning_checkbox = QCheckBox("XTTS fine-tuning")
        tool_cards = (
            self._create_option_card(
                self.xtts_finetuning_checkbox,
                "Adds tools for training custom XTTS voices.",
                models=TTS_BACKENDS["xtts"].formatted_model_licences,
                details=(
                    "Training uses XTTS v2 and does not change the model licence. Rights in "
                    "training recordings and resulting voices remain the user's responsibility."
                ),
            ),
        )
        self._add_option_cards(tools_grid, tool_cards)
        content_layout.addWidget(tools_group)
        content_layout.addStretch()

        buttons_layout = QHBoxLayout()
        buttons_layout.setContentsMargins(12, 8, 12, 8)
        buttons_layout.addStretch()

        self.install_button = QPushButton("Install selected")
        self.install_button.setObjectName("installButton")
        self.install_button.clicked.connect(self.install_pandrator)
        buttons_layout.addWidget(self.install_button)

        self.update_button = QPushButton("Update Pandrator")
        self.update_button.setObjectName("secondaryButton")
        self.update_button.clicked.connect(self.update_pandrator)
        buttons_layout.addWidget(self.update_button)

        layout.addLayout(buttons_layout)

        self.bind_cpu_install_option(
            self.xtts_checkbox,
            self.xtts_cpu_checkbox,
        )
        self.bind_cpu_install_option(
            self.kokoro_checkbox,
            self.kokoro_cpu_checkbox,
        )
        self.bind_cpu_install_option(
            self.chatterbox_checkbox,
            self.chatterbox_cpu_checkbox,
        )
        self.bind_cpu_install_option(
            self.kobold_qwen_checkbox,
            self.kobold_qwen_cpu_checkbox,
        )
        self.bind_cpu_install_option(
            self.magpie_checkbox,
            self.magpie_cpu_checkbox,
        )
        self.bind_cpu_install_option(
            self.rvc_checkbox,
            self.rvc_cpu_checkbox,
        )
        self.fishs2_checkbox.stateChanged.connect(self._handle_fishs2_toggle)
        self.crispasr_checkbox.stateChanged.connect(self._handle_crispasr_toggle)
        self.kobold_qwen_checkbox.stateChanged.connect(self._handle_kobold_qwen_toggle)
        self.kokoro_checkbox.stateChanged.connect(self._handle_kokoro_toggle)

        for checkbox in self.install_tab.findChildren(QCheckBox):
            checkbox.stateChanged.connect(self.update_install_button_state)

    def get_pandrator_install_path(self):
        return os.path.join(self.initial_working_dir, 'Pandrator')

    def update_install_location_label(self):
        if not hasattr(self, 'install_location_label'):
            return

        self.install_location_label.setText(
            "Pandrator will be created or reused at:\n"
            f"{self.get_pandrator_install_path()}"
        )

    def choose_install_workspace(self):
        selected_dir = QFileDialog.getExistingDirectory(
            self,
            "Choose where Pandrator should be installed",
            self.initial_working_dir,
        )
        if not selected_dir:
            return

        self.initial_working_dir = os.path.abspath(selected_dir)
        if is_linux() and is_appimage_environment():
            try:
                remember_launcher_workspace(self.initial_working_dir)
            except (OSError, ValueError) as error:
                logging.warning("Could not remember the selected installer workspace: %s", error)
                QMessageBox.warning(
                    self,
                    "Installation Location",
                    "The selected location will be used for this session, but could not be "
                    f"remembered for the next launch:\n\n{error}",
                )
        self.update_install_location_label()
        self.refresh_ui_state()
        self.set_startup_tab()

    def _runtime_state(self):
        path = os.path.join(self.initial_working_dir, "Pandrator", "runtime-processes.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
            pid = int(payload.get("supervisor_pid") or 0)
            if pid <= 0:
                return None
            try:
                import psutil

                if not psutil.pid_exists(pid):
                    return None
            except ImportError:
                try:
                    os.kill(pid, 0)
                except OSError:
                    return None
            return payload
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return None

    @staticmethod
    def _runtime_port(payload):
        api = (payload.get("processes") or {}).get("api") or {}
        command = list(api.get("command") or [])
        try:
            return int(command[command.index("--port") + 1])
        except (ValueError, IndexError, TypeError):
            return 8097

    def open_running_webui(self):
        runtime = self._runtime_state()
        if not runtime:
            QMessageBox.information(self, "Pandrator is not running", "Launch Pandrator first.")
            self.refresh_ui_state()
            return
        url = f"http://127.0.0.1:{self._runtime_port(runtime)}/"
        if not webbrowser.open_new_tab(url):
            QMessageBox.warning(self, "Could not open browser", f"Open this address manually:\n\n{url}")

    def _owner_password_initialized(self):
        database = os.path.join(self.initial_working_dir, "Pandrator", "pandrator.sqlite3")
        if not os.path.isfile(database):
            return False
        try:
            with sqlite3.connect(database) as connection:
                row = connection.execute(
                    "SELECT COUNT(*) FROM owner_account"
                ).fetchone()
            return bool(row and row[0])
        except sqlite3.Error:
            return False

    def _selected_password_scope(self):
        if not hasattr(self, "pandrator_password_scope_combo"):
            return str(getattr(self, "pandrator_password_scope_var", "none") or "none")
        return str(self.pandrator_password_scope_combo.currentData() or "none")

    def _configure_password_scope_options(self, network_access, selected_scope=None):
        selected = normalize_password_scope(
            selected_scope or self._selected_password_scope(),
            network_access=bool(network_access),
        )
        options = (
            (
                ("Password on other devices; automatic here", "remote"),
                ("Require password everywhere", "all"),
            )
            if network_access
            else (
                ("Automatic local sign-in", "none"),
                ("Require password on this computer", "local"),
            )
        )
        self.pandrator_password_scope_combo.blockSignals(True)
        self.pandrator_password_scope_combo.clear()
        for label, value in options:
            self.pandrator_password_scope_combo.addItem(label, value)
        self.pandrator_password_scope_combo.setCurrentIndex(
            max(0, self.pandrator_password_scope_combo.findData(selected))
        )
        self.pandrator_password_scope_combo.blockSignals(False)
        self.pandrator_password_scope_var = selected
        self._update_password_status()

    def _handle_network_access_toggle(self, state):
        network_access = state == Qt.CheckState.Checked.value
        self._configure_password_scope_options(
            network_access,
            self._selected_password_scope(),
        )

    def _handle_password_scope_changed(self):
        self.pandrator_password_scope_var = self._selected_password_scope()
        self._update_password_status()

    def _password_is_configured(self):
        return bool(self.pandrator_owner_password) or self._owner_password_initialized()

    def _update_password_status(self):
        if not hasattr(self, "pandrator_password_status_label"):
            return
        scope = self._selected_password_scope()
        configured = self._password_is_configured()
        pending = bool(self.pandrator_owner_password)
        if scope == "none":
            text = "The launcher uses a short-lived token; no password is requested locally."
        elif pending:
            text = "New password ready; it will be hashed and saved when Pandrator launches."
        elif configured:
            text = "Owner password configured. Pandrator stores only its secure hash."
        else:
            text = "Set an owner password before launching."
        self.pandrator_password_status_label.setText(text)
        self.pandrator_password_button.setText(
            "Change password" if configured else "Set password"
        )
        self.pandrator_password_button.setEnabled(scope != "none")

    def configure_owner_password(self):
        dialog = OwnerPasswordDialog(
            self,
            replacing=self._owner_password_initialized(),
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return False
        self.pandrator_owner_password = dialog.password()
        self._update_password_status()
        return True

    def prepare_launch_security(self, selection):
        if not selection.pandrator or selection.pandrator_password_scope == "none":
            return True
        if self._password_is_configured():
            return True
        return self.configure_owner_password()

    def persist_launch_preferences(self, selection):
        pandrator_path = os.path.join(self.initial_working_dir, "Pandrator")
        config = self.load_install_config(pandrator_path)
        config.update(
            {
                "pandrator_network_access": bool(selection.pandrator_network_access),
                "pandrator_password_scope": str(selection.pandrator_password_scope),
                "pandrator_port": int(selection.pandrator_port),
            }
        )
        self.save_install_config(pandrator_path, config)

    def setup_launch_tab(self):
        """Set up the Launch tab"""
        layout, content_layout = self._create_scrollable_tab_content(self.launch_tab)
        content_layout.addWidget(
            self._create_intro(
                "Choose what to start. Pandrator can run on its own or with one local speech service."
            )
        )

        launch_group = QGroupBox("Applications")
        launch_grid = QGridLayout(launch_group)

        self.launch_pandrator_checkbox = ToggleSwitch("Launch Pandrator")
        self.launch_pandrator_checkbox.setChecked(True)
        self.pandrator_network_checkbox = QCheckBox("Allow access from other devices on this network")
        self.pandrator_network_checkbox.setToolTip(
            "Binds the web server to all interfaces. An owner password is required. "
            "For Internet exposure, use HTTPS through a reverse proxy instead."
        )
        self.pandrator_password_scope_combo = QComboBox()
        self.pandrator_password_scope_combo.setMinimumWidth(285)
        self.pandrator_password_scope_combo.setToolTip(
            "Choose whether the launcher signs in automatically on this computer or asks for the owner password."
        )
        self.pandrator_password_button = QPushButton("Set password")
        self.pandrator_password_button.setObjectName("secondaryButton")
        self.pandrator_password_button.clicked.connect(self.configure_owner_password)
        self.pandrator_password_status_label = QLabel()
        self.pandrator_password_status_label.setObjectName("mutedLabel")
        self.pandrator_password_status_label.setWordWrap(True)

        security_controls = QWidget()
        security_layout = QGridLayout(security_controls)
        security_layout.setContentsMargins(0, 0, 0, 0)
        security_layout.setHorizontalSpacing(10)
        security_layout.setVerticalSpacing(7)
        security_layout.addWidget(self.pandrator_network_checkbox, 0, 0, 1, 3)
        security_layout.addWidget(QLabel("Password protection"), 1, 0)
        security_layout.addWidget(self.pandrator_password_scope_combo, 1, 1)
        security_layout.addWidget(self.pandrator_password_button, 1, 2)
        security_layout.addWidget(self.pandrator_password_status_label, 2, 0, 1, 3)

        self._configure_password_scope_options(False, "none")
        self.pandrator_network_checkbox.stateChanged.connect(self._handle_network_access_toggle)
        self.pandrator_password_scope_combo.currentIndexChanged.connect(
            self._handle_password_scope_changed
        )
        self.pandrator_port_spin = QSpinBox()
        self.pandrator_port_spin.setRange(1024, 65535)
        self.pandrator_port_spin.setValue(8097)
        self.pandrator_port_spin.setPrefix("Web port: ")
        self.launch_rvc_checkbox = ToggleSwitch("Launch RVC")
        self.rvc_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_xtts_checkbox = ToggleSwitch("Launch XTTS")
        self.xtts_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.deepspeed_checkbox = QCheckBox("Turn off DeepSpeed")
        self.launch_voxcpm_checkbox = ToggleSwitch("Launch VoxCPM2")
        self.launch_fishs2_checkbox = ToggleSwitch("Launch FishS2")
        self.fishs2_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_voxtral_checkbox = ToggleSwitch("Launch Voxtral")
        self.launch_kokoro_checkbox = ToggleSwitch("Launch Kokoro")
        self.kokoro_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_silero_checkbox = ToggleSwitch("Launch Silero")
        self.launch_chatterbox_checkbox = ToggleSwitch("Launch Chatterbox")
        self.chatterbox_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_kobold_qwen_checkbox = ToggleSwitch("Launch Qwen3 TTS")
        self.kobold_qwen_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_magpie_checkbox = ToggleSwitch("Launch Magpie")
        self.magpie_cpu_launch_checkbox = QCheckBox("Use CPU")

        launch_cards = (
            self._create_option_card(
                self.launch_pandrator_checkbox,
                "Start the local Pandrator web application and open it in your browser.",
                (security_controls, self.pandrator_port_spin),
                details="By default Pandrator listens only on 127.0.0.1. You can require the owner password locally, expose it to trusted devices on your LAN, or do both. LAN access is always password-protected; use an HTTPS reverse proxy for access beyond a trusted network.",
            ),
            self._create_option_card(
                self.launch_rvc_checkbox,
                "Start RVC voice conversion alongside Pandrator and the selected speech service.",
                (self.rvc_cpu_launch_checkbox,),
            ),
            self._create_option_card(
                self.launch_kokoro_checkbox,
                "Start the installed Kokoro speech service.",
                (self.kokoro_cpu_launch_checkbox,),
            ),
            self._create_option_card(
                self.launch_kobold_qwen_checkbox,
                "Start Qwen3 TTS using the backend, model size, and precision selected during installation.",
                (self.kobold_qwen_cpu_launch_checkbox,),
            ),
            self._create_option_card(
                self.launch_xtts_checkbox,
                "Start the installed XTTS speech service.",
                (self.xtts_cpu_launch_checkbox, self.deepspeed_checkbox),
            ),
            self._create_option_card(
                self.launch_voxcpm_checkbox,
                "Start the installed VoxCPM2 speech service.",
            ),
            self._create_option_card(
                self.launch_fishs2_checkbox,
                "Start the installed FishS2 speech service.",
                (self.fishs2_cpu_launch_checkbox,),
            ),
            self._create_option_card(
                self.launch_voxtral_checkbox,
                "Start the installed Voxtral speech service.",
            ),
            self._create_option_card(
                self.launch_silero_checkbox,
                "Start the installed Silero speech service.",
            ),
            self._create_option_card(
                self.launch_chatterbox_checkbox,
                "Start the installed Chatterbox speech service.",
                (self.chatterbox_cpu_launch_checkbox,),
            ),
            self._create_option_card(
                self.launch_magpie_checkbox,
                "Start the installed Magpie speech service.",
                (self.magpie_cpu_launch_checkbox,),
            ),
        )
        launch_card_keys = (
            None,
            "rvc",
            "kokoro",
            "kobold_qwen",
            "xtts",
            "voxcpm",
            "fishs2",
            "voxtral",
            "silero",
            "chatterbox",
            "magpie",
        )
        self.launch_backend_cards = {
            key: card for key, card in zip(launch_card_keys, launch_cards) if key
        }
        self.launch_backend_controls = {
            "rvc": self.launch_rvc_checkbox,
            "kokoro": self.launch_kokoro_checkbox,
            "kobold_qwen": self.launch_kobold_qwen_checkbox,
            "xtts": self.launch_xtts_checkbox,
            "voxcpm": self.launch_voxcpm_checkbox,
            "fishs2": self.launch_fishs2_checkbox,
            "voxtral": self.launch_voxtral_checkbox,
            "silero": self.launch_silero_checkbox,
            "chatterbox": self.launch_chatterbox_checkbox,
            "magpie": self.launch_magpie_checkbox,
        }
        for key, card in self.launch_backend_cards.items():
            card.stop_requested.connect(
                lambda backend_key=key: self.stop_running_backend(backend_key)
            )
        self._add_option_cards(launch_grid, launch_cards)
        content_layout.addWidget(launch_group)

        backend_runtime_group = QGroupBox("Backend Runtime")
        backend_runtime_layout = QVBoxLayout(backend_runtime_group)

        self.active_backend_value_label = QLabel("No backend running")
        self.active_backend_value_label.setWordWrap(True)
        backend_runtime_layout.addWidget(self.active_backend_value_label)

        backend_runtime_hint_label = QLabel(
            "Tip: Stop the running backend before starting another one to free RAM/VRAM. "
            "Pandrator and your active session stay open."
        )
        backend_runtime_hint_label.setWordWrap(True)
        backend_runtime_hint_label.setStyleSheet("color: #B8B8B8;")
        backend_runtime_layout.addWidget(backend_runtime_hint_label)

        backend_runtime_buttons_layout = QHBoxLayout()
        self.stop_backend_button = QPushButton("Stop All Running Backends")
        self.stop_backend_button.clicked.connect(self.stop_running_backends)
        backend_runtime_buttons_layout.addWidget(self.stop_backend_button)

        self.refresh_backend_status_button = QPushButton("Refresh Status")
        self.refresh_backend_status_button.clicked.connect(self.update_backend_runtime_controls)
        backend_runtime_buttons_layout.addWidget(self.refresh_backend_status_button)
        backend_runtime_buttons_layout.addStretch()

        backend_runtime_layout.addLayout(backend_runtime_buttons_layout)
        content_layout.addWidget(backend_runtime_group)
        content_layout.addStretch()

        launch_buttons_layout = QHBoxLayout()
        launch_buttons_layout.setContentsMargins(12, 8, 12, 8)
        launch_buttons_layout.addStretch()
        self.launch_button = QPushButton("Launch selected")
        self.launch_button.setObjectName("primaryButton")
        self.launch_button.clicked.connect(self.launch_apps)
        self.launch_button.setMinimumHeight(38)
        launch_buttons_layout.addWidget(self.launch_button)
        self.open_webui_button = QPushButton("Open Web UI")
        self.open_webui_button.setObjectName("secondaryButton")
        self.open_webui_button.clicked.connect(self.open_running_webui)
        self.open_webui_button.setEnabled(False)
        launch_buttons_layout.addWidget(self.open_webui_button)
        layout.addLayout(launch_buttons_layout)

    def setup_logs_tab(self):
        """Set up the Logs tab for realtime log viewing."""
        layout = QVBoxLayout(self.logs_tab)

        controls_layout = QHBoxLayout()

        self.clear_log_view_button = QPushButton("Clear View")
        self.clear_log_view_button.clicked.connect(self.clear_log_view)
        controls_layout.addWidget(self.clear_log_view_button)

        self.open_log_from_tab_button = QPushButton("Open Log File")
        self.open_log_from_tab_button.clicked.connect(self.open_log_file)
        self.open_log_from_tab_button.setEnabled(False)
        controls_layout.addWidget(self.open_log_from_tab_button)

        controls_layout.addStretch()
        layout.addLayout(controls_layout)

        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        self.log_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.log_view.setMaximumBlockCount(5000)
        self.log_view.setPlaceholderText("Logs will appear here during install, update, and launch.")
        self.log_view.setFont(QFont("Consolas", 10))
        layout.addWidget(self.log_view)

    def append_log_message(self, message):
        if not hasattr(self, 'log_view'):
            return

        self.log_view.appendPlainText(message)
        scrollbar = self.log_view.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def clear_log_view(self):
        if hasattr(self, 'log_view'):
            self.log_view.clear()

    def refresh_ui_state(self):
        self.update_install_location_label()
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config = self.load_install_config(pandrator_path, detect_rvc=True)

        # Helper function to set widget state
        def set_widget_state(widget, state, value=None):
            widget.setEnabled(state)
            if isinstance(widget, QCheckBox) and value is not None:
                widget.setChecked(value)

        # Pandrator
        pandrator_installed = os.path.exists(pandrator_path)
        pandrator_running = self._runtime_state() is not None
        set_widget_state(self.pandrator_checkbox, not pandrator_installed, False if pandrator_installed else True)
        set_widget_state(
            self.launch_pandrator_checkbox,
            pandrator_installed and not pandrator_running,
            pandrator_installed and not pandrator_running,
        )
        network_access = bool(config.get("pandrator_network_access", False))
        password_scope = normalize_password_scope(
            config.get("pandrator_password_scope"),
            network_access=network_access,
        )
        self.pandrator_network_checkbox.blockSignals(True)
        self.pandrator_network_checkbox.setChecked(network_access)
        self.pandrator_network_checkbox.blockSignals(False)
        self._configure_password_scope_options(network_access, password_scope)
        launch_controls_enabled = pandrator_installed and not pandrator_running
        self.pandrator_network_checkbox.setEnabled(launch_controls_enabled)
        self.pandrator_password_scope_combo.setEnabled(launch_controls_enabled)
        self.pandrator_password_button.setEnabled(
            launch_controls_enabled and password_scope != "none"
        )
        try:
            configured_port = int(config.get("pandrator_port", 8097))
        except (TypeError, ValueError):
            configured_port = 8097
        self.pandrator_port_spin.setValue(max(1024, min(65535, configured_port)))
        self.pandrator_port_spin.setEnabled(launch_controls_enabled)
        if hasattr(self, 'open_webui_button'):
            self.open_webui_button.setEnabled(pandrator_running)
            self.open_webui_button.setToolTip(
                "Open the running Pandrator web interface" if pandrator_running else "Pandrator is not running"
            )

        # XTTS
        xtts_support = config.get('xtts_support', False)
        xtts_cuda_support = config.get('cuda_support', False)

        # Disable XTTS checkboxes if XTTS is installed in any form
        set_widget_state(self.xtts_checkbox, not xtts_support, False)
        set_widget_state(self.xtts_cpu_checkbox, not xtts_support, False)
        set_widget_state(self.launch_xtts_checkbox, xtts_support, False)

        # Set CPU/GPU options based on XTTS installation
        if xtts_support:
            if xtts_cuda_support:
                set_widget_state(self.xtts_cpu_launch_checkbox, True, False)
                set_widget_state(self.deepspeed_checkbox, True, False)
            else:
                set_widget_state(self.xtts_cpu_launch_checkbox, True, True)
                set_widget_state(self.deepspeed_checkbox, False, False)
        else:
            set_widget_state(self.xtts_cpu_launch_checkbox, False, False)
            set_widget_state(self.deepspeed_checkbox, False, False)

        # VoxCPM
        voxcpm_support = config.get('voxcpm_support', False)
        set_widget_state(self.voxcpm_checkbox, not voxcpm_support, False)
        set_widget_state(self.launch_voxcpm_checkbox, voxcpm_support, False)

        # FishS2
        fishs2_support = config.get('fishs2_support', False)
        fishs2_gpu_support = config.get('fishs2_gpu_support', False)
        self.fishs2_backend = config.get('fishs2_backend', 'auto')
        self.fishs2_model_quant = config.get('fishs2_model_quant', 'q6_k')

        set_widget_state(self.fishs2_checkbox, not fishs2_support, False)

        set_widget_state(self.launch_fishs2_checkbox, fishs2_support, False)
        if fishs2_support:
            if fishs2_gpu_support:
                set_widget_state(self.fishs2_cpu_launch_checkbox, True, False)
            else:
                set_widget_state(self.fishs2_cpu_launch_checkbox, False, True)
        else:
            set_widget_state(self.fishs2_cpu_launch_checkbox, False, False)

        # Voxtral
        voxtral_support = config.get('voxtral_support', False)
        set_widget_state(self.voxtral_checkbox, not voxtral_support, False)
        set_widget_state(self.launch_voxtral_checkbox, voxtral_support, False)

        # Kokoro
        kokoro_support = config.get('kokoro_support', False)
        kokoro_gpu_support = config.get(KOKORO_GPU_SUPPORT_CONFIG_FLAG, False)
        set_widget_state(self.kokoro_checkbox, not kokoro_support, False)
        set_widget_state(self.kokoro_cpu_checkbox, not kokoro_support, False)
        set_widget_state(self.launch_kokoro_checkbox, kokoro_support, False)
        if kokoro_support:
            if kokoro_gpu_support:
                set_widget_state(self.kokoro_cpu_launch_checkbox, True, False)
            else:
                set_widget_state(self.kokoro_cpu_launch_checkbox, False, True)
        else:
            set_widget_state(self.kokoro_cpu_launch_checkbox, False, False)

        # Silero
        silero_support = config.get('silero_support', False)
        set_widget_state(self.silero_checkbox, not silero_support, False)
        set_widget_state(self.launch_silero_checkbox, silero_support, False)

        # Chatterbox
        chatterbox_support = config.get('chatterbox_support', False)
        chatterbox_gpu_support = config.get(CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG, False)
        set_widget_state(self.chatterbox_checkbox, not chatterbox_support, False)
        set_widget_state(self.chatterbox_cpu_checkbox, not chatterbox_support, False)
        set_widget_state(self.launch_chatterbox_checkbox, chatterbox_support, False)
        if chatterbox_support:
            if chatterbox_gpu_support:
                set_widget_state(self.chatterbox_cpu_launch_checkbox, True, False)
            else:
                set_widget_state(self.chatterbox_cpu_launch_checkbox, False, True)
        else:
            set_widget_state(self.chatterbox_cpu_launch_checkbox, False, False)

        # Qwen3 TTS
        kobold_qwen_support = config.get('kobold_qwen_support', False)
        kobold_qwen_gpu_support = config.get(KOBOLD_QWEN_GPU_SUPPORT_CONFIG_FLAG, False)
        self.kobold_qwen_backend = config.get('kobold_qwen_backend', 'auto')
        installed_qwen_models = set(config.get('kobold_qwen_installed_models') or [])
        stored_qwen_selection = config.get('kobold_qwen_model_selection')
        if not isinstance(stored_qwen_selection, str) or stored_qwen_selection not in {
            'base', 'customvoice', 'both'
        }:
            legacy_qwen_selection = config.get('kobold_qwen_initial_model', 'base')
            if not isinstance(legacy_qwen_selection, str):
                legacy_qwen_selection = 'base'
            stored_qwen_selection = (
                'both'
                if {'base', 'customvoice'}.issubset(installed_qwen_models)
                else legacy_qwen_selection
            )
        self.kobold_qwen_initial_model = stored_qwen_selection
        self.kobold_qwen_model_size = qwen_effective_model_size(
            stored_qwen_selection,
            config.get('kobold_qwen_model_size', '0.6b'),
        )
        self.kobold_qwen_quantization = config.get('kobold_qwen_quantization', 'f16')
        set_widget_state(self.kobold_qwen_checkbox, not kobold_qwen_support, False)
        set_widget_state(self.kobold_qwen_cpu_checkbox, not kobold_qwen_support, False)
        set_widget_state(self.launch_kobold_qwen_checkbox, kobold_qwen_support, False)
        if kobold_qwen_support:
            if kobold_qwen_gpu_support:
                set_widget_state(self.kobold_qwen_cpu_launch_checkbox, True, False)
            else:
                set_widget_state(self.kobold_qwen_cpu_launch_checkbox, False, True)
        else:
            set_widget_state(self.kobold_qwen_cpu_launch_checkbox, False, False)

        # Magpie
        magpie_support = config.get('magpie_support', False)
        magpie_gpu_support = config.get(MAGPIE_GPU_SUPPORT_CONFIG_FLAG, False)
        set_widget_state(self.magpie_checkbox, not magpie_support, False)
        set_widget_state(self.magpie_cpu_checkbox, not magpie_support, False)
        set_widget_state(self.launch_magpie_checkbox, magpie_support, False)
        if magpie_support:
            if magpie_gpu_support:
                set_widget_state(self.magpie_cpu_launch_checkbox, True, False)
            else:
                set_widget_state(self.magpie_cpu_launch_checkbox, False, True)
        else:
            set_widget_state(self.magpie_cpu_launch_checkbox, False, False)

        # RVC
        rvc_support = config.get('rvc_support', False)
        rvc_gpu_support = config.get(RVC_GPU_SUPPORT_CONFIG_FLAG, False)
        set_widget_state(self.rvc_checkbox, not rvc_support, False)
        set_widget_state(self.rvc_cpu_checkbox, not rvc_support, False)
        set_widget_state(self.launch_rvc_checkbox, rvc_support, False)
        if rvc_support:
            if rvc_gpu_support:
                set_widget_state(self.rvc_cpu_launch_checkbox, True, False)
            else:
                set_widget_state(self.rvc_cpu_launch_checkbox, False, True)
        else:
            set_widget_state(self.rvc_cpu_launch_checkbox, False, False)

        # XTTS Fine-tuning
        xtts_finetuning_support = config.get('xtts_finetuning_support', False)
        set_widget_state(self.xtts_finetuning_checkbox, not xtts_finetuning_support, False)

        # CrispASR (legacy STT flags are treated as pending migration).
        crispasr_support = config.get('crispasr_support', False)
        self.crispasr_backend = config.get('crispasr_backend', self.crispasr_backend)
        self.crispasr_engine = config.get('crispasr_engine', self.crispasr_engine)
        self.crispasr_model_quantization = config.get(
            'crispasr_model_quantization', self.crispasr_model_quantization
        )
        legacy_stt_support = config.get('whisperx_support', False) or config.get('parakeet_onnx_support', False)
        set_widget_state(self.crispasr_checkbox, not crispasr_support, legacy_stt_support and not crispasr_support)
        self.crispasr_backend = str(config.get('crispasr_backend') or self.crispasr_backend or 'auto')
        self.crispasr_settings_button.setEnabled(not crispasr_support or legacy_stt_support)

        # Update launch and install buttons state
        self.apply_platform_install_availability()
        self.launch_button.setEnabled(pandrator_installed)
        self.update_install_button_state()
        self.update_button.setEnabled(pandrator_installed)

        self.update_backend_runtime_controls()

    def apply_platform_install_availability(self):
        if is_windows():
            return

        controls_by_component = {
            'xtts': (self.xtts_checkbox, self.xtts_cpu_checkbox),
            'xtts_cpu': (self.xtts_checkbox, self.xtts_cpu_checkbox),
            'voxcpm': (self.voxcpm_checkbox,),
            'fishs2': (self.fishs2_checkbox,),
            'fishs2_cpu': (self.fishs2_checkbox,),
            'silero': (self.silero_checkbox,),
            'voxtral': (self.voxtral_checkbox,),
            'rvc': (self.rvc_checkbox, self.rvc_cpu_checkbox),
            'rvc_cpu': (self.rvc_checkbox, self.rvc_cpu_checkbox),
            'crispasr': (self.crispasr_checkbox, self.crispasr_settings_button),
            'xtts_finetuning': (self.xtts_finetuning_checkbox,),
            'chatterbox': (self.chatterbox_checkbox, self.chatterbox_cpu_checkbox),
            'chatterbox_cpu': (self.chatterbox_checkbox, self.chatterbox_cpu_checkbox),
            'kobold_qwen': (self.kobold_qwen_checkbox, self.kobold_qwen_cpu_checkbox, self.kobold_qwen_settings_button),
            'kobold_qwen_cpu': (self.kobold_qwen_checkbox, self.kobold_qwen_cpu_checkbox, self.kobold_qwen_settings_button),
            'magpie': (self.magpie_checkbox, self.magpie_cpu_checkbox),
            'magpie_cpu': (self.magpie_checkbox, self.magpie_cpu_checkbox),
        }
        seen_controls = set()
        for component_key in LINUX_DEFERRED_INSTALL_COMPONENT_KEYS:
            component = COMPONENTS[component_key]
            reason = LINUX_DEFERRED_REASON_BY_COMPONENT.get(
                component.packaging_key,
                "pending qualification",
            )
            tooltip = f"Unavailable on Linux: {reason}."
            for control in controls_by_component.get(component_key, ()):
                if control in seen_controls:
                    continue
                seen_controls.add(control)
                control.blockSignals(True)
                control.setChecked(False)
                control.blockSignals(False)
                control.setEnabled(False)
                control.setToolTip(tooltip)

    def bind_cpu_install_option(self, service_toggle, cpu_checkbox):
        cpu_checkbox.stateChanged.connect(
            lambda state, toggle=service_toggle: self._handle_cpu_install_option(
                state,
                toggle,
            )
        )
        service_toggle.stateChanged.connect(
            lambda state, option=cpu_checkbox: self._handle_install_service_toggle(
                state,
                option,
            )
        )

    def _handle_cpu_install_option(self, state, service_toggle):
        if state == Qt.CheckState.Checked.value and not service_toggle.isChecked():
            service_toggle.setChecked(True)

    def _handle_install_service_toggle(self, state, cpu_checkbox):
        if state == Qt.CheckState.Checked.value or not cpu_checkbox.isChecked():
            return

        cpu_checkbox.blockSignals(True)
        cpu_checkbox.setChecked(False)
        cpu_checkbox.blockSignals(False)
        self.update_install_button_state()

    def update_install_button_state(self):
        has_selected_component = any(
            checkbox.isChecked() for checkbox in self.install_tab.findChildren(QCheckBox)
        )
        self.install_button.setEnabled(has_selected_component)

    def get_installed_components(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config = self.load_install_config(pandrator_path, detect_rvc=True)

        return {
            'xtts': config.get('xtts_support', False),
            'voxcpm': config.get('voxcpm_support', False),
            'fishs2': config.get('fishs2_support', False),
            'voxtral': config.get('voxtral_support', False),
            'kokoro': config.get('kokoro_support', False),
            'silero': config.get('silero_support', False),
            'rvc': config.get('rvc_support', False),
            'crispasr': config.get('crispasr_support', False),
            'xtts_finetuning': config.get('xtts_finetuning_support', False),
            'chatterbox': config.get('chatterbox_support', False),
            'kobold_qwen': config.get('kobold_qwen_support', False),
            'magpie': config.get('magpie_support', False)
        }

    def disable_buttons(self):
        """Disable all buttons during processing"""
        self.install_button.setEnabled(False)
        self.update_button.setEnabled(False)
        self.launch_button.setEnabled(False)
        if hasattr(self, 'stop_backend_button'):
            self.stop_backend_button.setEnabled(False)
        if hasattr(self, 'refresh_backend_status_button'):
            self.refresh_backend_status_button.setEnabled(False)
        if hasattr(self, 'change_install_location_button'):
            self.change_install_location_button.setEnabled(False)

        # Disable checkboxes in install tab
        for child in self.install_tab.findChildren(QCheckBox):
            child.setEnabled(False)

        # Disable checkboxes in launch tab
        for child in self.launch_tab.findChildren(QCheckBox):
            child.setEnabled(False)
        for child in (
            self.pandrator_password_scope_combo,
            self.pandrator_password_button,
            self.pandrator_port_spin,
        ):
            child.setEnabled(False)

    def enable_buttons(self):
        """Re-enable buttons after processing"""
        self.refresh_ui_state()
        if hasattr(self, 'change_install_location_button'):
            self.change_install_location_button.setEnabled(True)

    def update_progress(self, value):
        """Update progress bar value (0.0 to 1.0)"""
        self.progress_bar.setValue(int(value * 100))

    def update_status(self, text):
        """Update status label text"""
        self.status_label.setText(text)
        self.status_label.setToolTip(text)
        logging.info(text)

    def notify_error(self, title, message):
        QMessageBox.critical(self, title, message)

    def notify_warning(self, title, message):
        def show_message():
            QMessageBox.warning(self, title, message)

        app = QApplication.instance()
        if app is not None and QThread.currentThread() is not app.thread():
            QTimer.singleShot(0, show_message)
            return
        show_message()

    def create_gui_log_handler(self):
        return QtLogHandler(self.log_emitter)

    def snapshot_install_selection(self):
        """Read installation choices on the GUI thread before work starts."""
        qwen_model_size = qwen_effective_model_size(
            self.kobold_qwen_initial_model,
            self.kobold_qwen_model_size,
        )
        return InstallSelection(
            pandrator=self.pandrator_checkbox.isChecked(),
            xtts=self.xtts_checkbox.isChecked() and not self.xtts_cpu_checkbox.isChecked(),
            xtts_cpu=self.xtts_checkbox.isChecked() and self.xtts_cpu_checkbox.isChecked(),
            voxcpm=self.voxcpm_checkbox.isChecked(),
            fishs2=self.fishs2_checkbox.isChecked() and self.fishs2_backend != "cpu",
            fishs2_cpu=self.fishs2_checkbox.isChecked() and self.fishs2_backend == "cpu",
            fishs2_backend=self.fishs2_backend,
            fishs2_model_quant=self.fishs2_model_quant,
            silero=self.silero_checkbox.isChecked(),
            voxtral=self.voxtral_checkbox.isChecked(),
            kokoro=self.kokoro_checkbox.isChecked() and not self.kokoro_cpu_checkbox.isChecked(),
            kokoro_cpu=self.kokoro_checkbox.isChecked() and self.kokoro_cpu_checkbox.isChecked(),
            rvc=self.rvc_checkbox.isChecked() and not self.rvc_cpu_checkbox.isChecked(),
            rvc_cpu=self.rvc_checkbox.isChecked() and self.rvc_cpu_checkbox.isChecked(),
            crispasr=self.crispasr_checkbox.isChecked(),
            crispasr_backend=self.crispasr_backend,
            crispasr_engine=self.crispasr_engine,
            crispasr_model_quantization=self.crispasr_model_quantization,
            xtts_finetuning=self.xtts_finetuning_checkbox.isChecked(),
            chatterbox=(
                self.chatterbox_checkbox.isChecked()
                and not self.chatterbox_cpu_checkbox.isChecked()
            ),
            chatterbox_cpu=(
                self.chatterbox_checkbox.isChecked()
                and self.chatterbox_cpu_checkbox.isChecked()
            ),
            kobold_qwen=(
                self.kobold_qwen_checkbox.isChecked()
                and not self.kobold_qwen_cpu_checkbox.isChecked()
            ),
            kobold_qwen_cpu=(
                self.kobold_qwen_checkbox.isChecked()
                and self.kobold_qwen_cpu_checkbox.isChecked()
            ),
            kobold_qwen_backend=self.kobold_qwen_backend,
            kobold_qwen_model_size=qwen_model_size,
            kobold_qwen_quantization=self.kobold_qwen_quantization,
            kobold_qwen_initial_model=self.kobold_qwen_initial_model,
            magpie=self.magpie_checkbox.isChecked() and not self.magpie_cpu_checkbox.isChecked(),
            magpie_cpu=self.magpie_checkbox.isChecked() and self.magpie_cpu_checkbox.isChecked(),
        )

    def apply_install_selection(self, selection):
        """Reflect a typed installation selection in the GUI."""
        self.pandrator_checkbox.setChecked(selection.pandrator)
        for component in (
            "voxcpm",
            "silero",
            "voxtral",
            "crispasr",
            "xtts_finetuning",
        ):
            getattr(self, f"{component}_checkbox").setChecked(getattr(selection, component))
        for component in ("xtts", "kokoro", "chatterbox", "kobold_qwen", "magpie", "rvc"):
            cpu_selected = getattr(selection, f"{component}_cpu")
            getattr(self, f"{component}_checkbox").setChecked(
                getattr(selection, component) or cpu_selected
            )
            getattr(self, f"{component}_cpu_checkbox").setChecked(cpu_selected)
        
        # FishS2 is handled without a CPU checkbox on card
        fishs2_selected = selection.fishs2 or selection.fishs2_cpu
        self.fishs2_checkbox.setChecked(fishs2_selected)
        self.fishs2_backend = getattr(selection, "fishs2_backend", "auto")
        self.fishs2_model_quant = getattr(selection, "fishs2_model_quant", "q6_k")
        self.crispasr_backend = getattr(selection, "crispasr_backend", "auto")
        self.crispasr_engine = getattr(selection, "crispasr_engine", "whisper-large-v3")
        self.crispasr_model_quantization = getattr(selection, "crispasr_model_quantization", "f16")
        self.kobold_qwen_backend = getattr(selection, "kobold_qwen_backend", "auto")
        self.kobold_qwen_model_size = getattr(selection, "kobold_qwen_model_size", "0.6b")
        self.kobold_qwen_quantization = getattr(selection, "kobold_qwen_quantization", "f16")
        self.kobold_qwen_initial_model = getattr(selection, "kobold_qwen_initial_model", "base")

    def snapshot_launch_selection(self):
        """Read launch choices on the GUI thread before work starts."""
        return LaunchSelection(
            pandrator=self.launch_pandrator_checkbox.isChecked(),
            pandrator_network_access=self.pandrator_network_checkbox.isChecked(),
            pandrator_password_scope=normalize_password_scope(
                self._selected_password_scope(),
                network_access=self.pandrator_network_checkbox.isChecked(),
            ),
            pandrator_port=self.pandrator_port_spin.value(),
            rvc=self.launch_rvc_checkbox.isChecked(),
            rvc_cpu=self.rvc_cpu_launch_checkbox.isChecked(),
            xtts=self.launch_xtts_checkbox.isChecked(),
            disable_deepspeed=self.deepspeed_checkbox.isChecked(),
            xtts_cpu=self.xtts_cpu_launch_checkbox.isChecked(),
            voxcpm=self.launch_voxcpm_checkbox.isChecked(),
            fishs2=self.launch_fishs2_checkbox.isChecked() and not self.fishs2_cpu_launch_checkbox.isChecked(),
            fishs2_cpu=self.launch_fishs2_checkbox.isChecked() and self.fishs2_cpu_launch_checkbox.isChecked(),
            voxtral=self.launch_voxtral_checkbox.isChecked(),
            kokoro=self.launch_kokoro_checkbox.isChecked(),
            kokoro_cpu=self.kokoro_cpu_launch_checkbox.isChecked(),
            silero=self.launch_silero_checkbox.isChecked(),
            chatterbox=self.launch_chatterbox_checkbox.isChecked(),
            chatterbox_cpu=self.chatterbox_cpu_launch_checkbox.isChecked(),
            kobold_qwen=self.launch_kobold_qwen_checkbox.isChecked(),
            kobold_qwen_cpu=self.kobold_qwen_cpu_launch_checkbox.isChecked(),
            magpie=self.launch_magpie_checkbox.isChecked(),
            magpie_cpu=self.magpie_cpu_launch_checkbox.isChecked(),
        )

    def _handle_fishs2_toggle(self, state):
        is_checked = (state == Qt.CheckState.Checked.value)
        if is_checked:
            self.show_fishs2_config_dialog(force=False)
        self.update_install_button_state()

    def _handle_crispasr_toggle(self, state):
        is_checked = state == Qt.CheckState.Checked.value
        if is_checked:
            self.show_crispasr_config_dialog(force=False)
        self.update_install_button_state()

    def _handle_kobold_qwen_toggle(self, state):
        is_checked = state == Qt.CheckState.Checked.value
        if is_checked and self.isVisible():
            self.show_kobold_qwen_config_dialog(force=False)
        self.update_install_button_state()

    def _handle_kokoro_toggle(self, state):
        if state != Qt.CheckState.Checked.value:
            return
        if not detect_compute_backends()["cuda"]["available"]:
            self.kokoro_cpu_checkbox.setChecked(True)
            self.kokoro_cpu_checkbox.setToolTip(
                "CPU selected automatically because CUDA was not detected."
            )

    def show_crispasr_config_dialog(self, force=False):
        if not force and self.crispasr_backend_manually_set:
            return
        dialog = CrispASRConfigDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.crispasr_backend = dialog.get_selected_backend()
            self.crispasr_engine = dialog.get_selected_engine()
            self.crispasr_model_quantization = dialog.get_selected_quantization()
            self.crispasr_backend_manually_set = True
            logging.info(
                "CrispASR configured: backend=%s, engine=%s, quantization=%s",
                self.crispasr_backend,
                self.crispasr_engine,
                self.crispasr_model_quantization,
            )
        elif not force:
            self.crispasr_checkbox.blockSignals(True)
            self.crispasr_checkbox.setChecked(False)
            self.crispasr_checkbox.blockSignals(False)
            self.update_install_button_state()

    def show_kobold_qwen_config_dialog(self, force=False):
        if not force and self.kobold_qwen_settings_manually_set:
            return
        dialog = QwenConfigDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.kobold_qwen_backend = dialog.get_selected_backend()
            self.kobold_qwen_model_size = dialog.get_selected_model_size()
            self.kobold_qwen_quantization = dialog.get_selected_quantization()
            self.kobold_qwen_initial_model = dialog.get_selected_initial_model()
            self.kobold_qwen_settings_manually_set = True
            self.kobold_qwen_cpu_checkbox.setChecked(self.kobold_qwen_backend == "cpu")
            logging.info(
                "Qwen3 TTS configured: backend=%s, model=%s/%s/%s",
                self.kobold_qwen_backend,
                self.kobold_qwen_initial_model,
                self.kobold_qwen_model_size,
                self.kobold_qwen_quantization,
            )
        elif not force:
            self.kobold_qwen_checkbox.blockSignals(True)
            self.kobold_qwen_checkbox.setChecked(False)
            self.kobold_qwen_checkbox.blockSignals(False)
            self.update_install_button_state()

    def show_fishs2_config_dialog(self, force=False):
        if not force and getattr(self, "fishs2_model_quant_manually_set", False):
            return
            
        dialog = FishS2ConfigDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.fishs2_backend = dialog.get_selected_backend()
            self.fishs2_model_quant = dialog.get_selected_quant()
            self.fishs2_model_quant_manually_set = True
            logging.info(f"FishS2 configured: backend={self.fishs2_backend}, quant={self.fishs2_model_quant}")
        else:
            if not force:
                self.fishs2_checkbox.blockSignals(True)
                self.fishs2_checkbox.setChecked(False)
                self.fishs2_checkbox.blockSignals(False)
                self.update_install_button_state()

class OwnerPasswordDialog(QDialog):
    """Small password editor that never persists or logs plaintext input."""

    def __init__(self, parent=None, *, replacing=False):
        super().__init__(parent)
        self.setWindowTitle("Change owner password" if replacing else "Set owner password")
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel(
            "<h2>Change owner password</h2>"
            if replacing
            else "<h2>Protect Pandrator</h2>"
        )
        explanation = QLabel(
            "Use at least 10 characters. The installer keeps the password in memory only until launch; "
            "Pandrator stores an Argon2 password hash, never the plaintext password."
        )
        explanation.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(explanation)

        self.password_edit = QLineEdit()
        self.password_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.password_edit.setPlaceholderText("Owner password")
        self.confirmation_edit = QLineEdit()
        self.confirmation_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.confirmation_edit.setPlaceholderText("Confirm owner password")
        self.confirmation_edit.returnPressed.connect(self.accept_password)
        layout.addWidget(self.password_edit)
        layout.addWidget(self.confirmation_edit)

        show_password = QCheckBox("Show password")
        show_password.toggled.connect(
            lambda shown: self._set_password_visible(bool(shown))
        )
        layout.addWidget(show_password)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #ef9a9a;")
        layout.addWidget(self.error_label)

        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        save = QPushButton("Save password")
        save.setObjectName("primaryButton")
        save.setDefault(True)
        cancel.clicked.connect(self.reject)
        save.clicked.connect(self.accept_password)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(save)
        layout.addLayout(buttons)

    def _set_password_visible(self, visible):
        mode = QLineEdit.EchoMode.Normal if visible else QLineEdit.EchoMode.Password
        self.password_edit.setEchoMode(mode)
        self.confirmation_edit.setEchoMode(mode)

    def accept_password(self):
        password = self.password_edit.text()
        if len(password) < 10:
            self.error_label.setText("Use at least 10 characters.")
            self.password_edit.setFocus()
            return
        if password != self.confirmation_edit.text():
            self.error_label.setText("The passwords do not match.")
            self.confirmation_edit.selectAll()
            self.confirmation_edit.setFocus()
            return
        self.error_label.clear()
        self.accept()

    def password(self):
        return self.password_edit.text()


class CrispASRConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure CrispASR Runtime")
        self.setMinimumSize(560, 520)
        self.selected_backend = getattr(parent, "crispasr_backend", "auto")
        self.selected_engine = getattr(parent, "crispasr_engine", "whisper-large-v3")
        self.selected_quantization = getattr(parent, "crispasr_model_quantization", "f16")
        statuses = detect_compute_backends()

        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        title = QLabel("<h2>CrispASR compute backend</h2>")
        description = QLabel(
            "Automatic chooses the best native release detected on this machine. "
            "Choose an explicit backend to install and force that runtime—for example Vulkan on an AMD GPU."
        )
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        detection_group = QGroupBox("Detection")
        detection_layout = QVBoxLayout(detection_group)
        for backend in ("cuda", "vulkan", "metal", "cpu"):
            status = statuses[backend]
            marker = "Available" if status["available"] else "Not detected"
            label = QLabel(f"<b>{backend.upper()}</b> — {marker}. {status['reason']}")
            label.setWordWrap(True)
            detection_layout.addWidget(label)
        layout.addWidget(detection_group)

        self.backend_combo = QComboBox()
        labels = {
            "auto": "Automatic (recommended)",
            "cpu": "CPU",
            "cuda": "CUDA (NVIDIA)",
            "vulkan": "Vulkan (AMD / Intel / NVIDIA)",
            "metal": "Metal (Apple Silicon)",
        }
        self.backend_values = []
        for backend, label in labels.items():
            self.backend_combo.addItem(label, backend)
            self.backend_values.append(backend)
        selected_index = self.backend_values.index(self.selected_backend) if self.selected_backend in self.backend_values else 0
        self.backend_combo.setCurrentIndex(selected_index)
        layout.addWidget(self.backend_combo)

        model_group = QGroupBox("Default model downloaded on first use")
        model_layout = QVBoxLayout(model_group)
        self.engine_combo = QComboBox()
        self.engine_combo.addItem("Whisper large-v3 — multilingual, DTW word timestamps", "whisper-large-v3")
        self.engine_combo.addItem("Parakeet TDT 0.6B v3 — English, word timestamps", "parakeet-tdt-0.6b-v3")
        engine_index = self.engine_combo.findData(self.selected_engine)
        self.engine_combo.setCurrentIndex(max(0, engine_index))
        self.quantization_combo = QComboBox()
        self.engine_combo.currentIndexChanged.connect(self._refresh_quantizations)
        model_layout.addWidget(self.engine_combo)
        model_layout.addWidget(self.quantization_combo)
        layout.addWidget(model_group)
        self._refresh_quantizations()

        note = QLabel(
            "FP16 preserves full model precision. Quantized variants reduce RAM/VRAM use and may trade a little accuracy. "
            "The selected model downloads into Pandrator's cache on first use; other variants remain available later. "
            "VAD, beam/decoder, chunks, hotwords, and language identification are configured per session in the web app."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        accept = QPushButton("Use this backend")
        cancel.clicked.connect(self.reject)
        accept.clicked.connect(self.accept)
        accept.setDefault(True)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)

    def get_selected_backend(self):
        return str(self.backend_combo.currentData() or "auto")

    def _refresh_quantizations(self):
        selected = str(self.engine_combo.currentData() or "whisper-large-v3")
        options = (
            (("FP16 — full precision", "f16"), ("Q5_0 — lower memory", "q5_0"))
            if selected == "whisper-large-v3"
            else (
                ("FP16 — full precision", "f16"),
                ("Q8_0 — near-full precision", "q8_0"),
                ("Q5_0 — lower memory", "q5_0"),
                ("Q4_K — lowest memory", "q4_k"),
            )
        )
        previous = self.selected_quantization or str(self.quantization_combo.currentData() or "f16")
        self.quantization_combo.clear()
        for label, value in options:
            self.quantization_combo.addItem(label, value)
        index = self.quantization_combo.findData(previous)
        self.quantization_combo.setCurrentIndex(max(0, index))

    def get_selected_engine(self):
        return str(self.engine_combo.currentData() or "whisper-large-v3")

    def get_selected_quantization(self):
        return str(self.quantization_combo.currentData() or "f16")


class QwenConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure Qwen3 TTS")
        self.setMinimumSize(560, 500)
        statuses = detect_compute_backends()
        layout = QVBoxLayout(self)
        layout.setSpacing(14)

        title = QLabel("<h2>Qwen3-TTS runtime and startup model</h2>")
        description = QLabel(
            "Choose the accelerator and whether to download the Base model, CustomVoice model, or both. "
            "Pandrator can download another supported model later when you request its capability."
        )
        description.setWordWrap(True)
        layout.addWidget(title)
        layout.addWidget(description)

        detection_group = QGroupBox("Detected compute backends")
        detection_layout = QVBoxLayout(detection_group)
        for backend in ("cuda", "vulkan", "metal", "cpu"):
            status = statuses[backend]
            marker = "Available" if status["available"] else "Not detected"
            label = QLabel(f"<b>{backend.upper()}</b> — {marker}. {status['reason']}")
            label.setWordWrap(True)
            detection_layout.addWidget(label)
        layout.addWidget(detection_group)

        self.backend_combo = QComboBox()
        for label, value in (
            ("Automatic (recommended)", "auto"),
            ("CUDA (NVIDIA)", "cuda"),
            ("Vulkan (AMD / Intel / NVIDIA)", "vulkan"),
            ("Metal (Apple Silicon)", "metal"),
            ("CPU", "cpu"),
        ):
            self.backend_combo.addItem(label, value)
        self.backend_combo.setCurrentIndex(max(0, self.backend_combo.findData(getattr(parent, "kobold_qwen_backend", "auto"))))
        layout.addWidget(self.backend_combo)

        model_group = QGroupBox("Initial capability and model")
        model_layout = QVBoxLayout(model_group)
        self.initial_model_combo = QComboBox()
        self.initial_model_combo.addItem("Voice cloning — Base model", "base")
        self.initial_model_combo.addItem("Pre-built voices — CustomVoice model", "customvoice")
        self.initial_model_combo.addItem("Voice cloning + pre-built voices — download both", "both")
        self.initial_model_combo.setCurrentIndex(max(0, self.initial_model_combo.findData(getattr(parent, "kobold_qwen_initial_model", "base"))))
        self.model_size_combo = QComboBox()
        self.model_size_combo.addItem("0.6B — lower memory, Base only", "0.6b")
        self.model_size_combo.addItem("1.7B — higher capacity", "1.7b")
        self.model_size_combo.setCurrentIndex(max(0, self.model_size_combo.findData(getattr(parent, "kobold_qwen_model_size", "0.6b"))))
        self.quantization_combo = QComboBox()
        self.quantization_combo.addItem("FP16 — full precision (default)", "f16")
        self.quantization_combo.addItem("Q8_0 — lower memory", "q8_0")
        self.quantization_combo.setCurrentIndex(max(0, self.quantization_combo.findData(getattr(parent, "kobold_qwen_quantization", "f16"))))
        self.initial_model_combo.currentIndexChanged.connect(self._sync_model_size)
        model_layout.addWidget(self.initial_model_combo)
        model_layout.addWidget(self.model_size_combo)
        model_layout.addWidget(self.quantization_combo)
        layout.addWidget(model_group)
        self._sync_model_size()

        note = QLabel(
            "FP16 is the default for both sizes; Q8_0 remains available when memory is constrained. "
            "CustomVoice provides named pre-built voices and is available as 1.7B; Base provides reference-audio voice cloning. "
            "Selecting both downloads the 1.7B Base and CustomVoice variants."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()

        buttons = QHBoxLayout()
        cancel = QPushButton("Cancel")
        accept = QPushButton("Use these settings")
        cancel.clicked.connect(self.reject)
        accept.clicked.connect(self.accept)
        accept.setDefault(True)
        buttons.addStretch()
        buttons.addWidget(cancel)
        buttons.addWidget(accept)
        layout.addLayout(buttons)

    def _sync_model_size(self):
        custom = self.initial_model_combo.currentData() in {"customvoice", "both"}
        if custom:
            self.model_size_combo.setCurrentIndex(self.model_size_combo.findData("1.7b"))
        self.model_size_combo.setEnabled(not custom)

    def get_selected_backend(self):
        return str(self.backend_combo.currentData() or "auto")

    def get_selected_model_size(self):
        return str(self.model_size_combo.currentData() or "0.6b")

    def get_selected_quantization(self):
        return str(self.quantization_combo.currentData() or "f16")

    def get_selected_initial_model(self):
        return str(self.initial_model_combo.currentData() or "base")


class FishS2ConfigDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Configure FishS2 GPU Runtime")
        self.setMinimumSize(420, 320)
        
        self.selected_backend = getattr(parent, "fishs2_backend", "auto")
        self.selected_quant = getattr(parent, "fishs2_model_quant", "q6_k")
        
        layout = QVBoxLayout(self)
        layout.setSpacing(14)
        layout.setContentsMargins(16, 16, 16, 16)
        
        title_label = QLabel("<h2>Configure FishS2 GPU</h2>")
        desc_label = QLabel(
            "FishS2 is a high-quality neural voice cloning engine. "
            "Please select your hardware backend and model quantization size."
        )
        desc_label.setWordWrap(True)
        layout.addWidget(title_label)
        layout.addWidget(desc_label)
        
        gpu_info_group = QGroupBox("Detected Hardware")
        gpu_info_layout = QVBoxLayout(gpu_info_group)
        
        gpu_vram = self._get_gpu_vram_gb()
        if gpu_vram is not None:
            gpu_text = f"Detected GPU VRAM: <b>{gpu_vram:.1f} GB</b>.<br>"
            if gpu_vram <= 8.0:
                gpu_text += "Recommendation: We suggest using the <b>6-bit (q6_k)</b> model to fit inside VRAM."
                default_quant = "q6_k"
            else:
                gpu_text += "Recommendation: We suggest using the high-fidelity <b>8-bit (q8_0)</b> model."
                default_quant = "q8_0"
        else:
            gpu_text = "Could not autodetect GPU VRAM.<br>Recommendation: Defaulting to <b>6-bit (q6_k)</b> for safety."
            default_quant = "q6_k"
            
        if not getattr(parent, "fishs2_model_quant_manually_set", False):
            self.selected_quant = default_quant
            
        gpu_label = QLabel(gpu_text)
        gpu_label.setWordWrap(True)
        gpu_info_layout.addWidget(gpu_label)
        layout.addWidget(gpu_info_group)
        
        form_widget = QWidget()
        form_layout = QGridLayout(form_widget)
        form_layout.setContentsMargins(0, 0, 0, 0)
        
        backend_label = QLabel("Hardware Backend:")
        self.backend_combo = QComboBox()
        self.backend_combo.addItems(["auto", "cuda", "vulkan", "cpu"])
        self.backend_combo.setCurrentText(self.selected_backend)
        form_layout.addWidget(backend_label, 0, 0)
        form_layout.addWidget(self.backend_combo, 0, 1)
        
        quant_label = QLabel("Model Quantization:")
        self.quant_combo = QComboBox()
        self.quant_map = {
            "8-bit (q8_0) - Best quality, requires > 8 GB VRAM": "q8_0",
            "6-bit (q6_k) - Great compromise, fits 8 GB cards (Recommended)": "q6_k",
            "5-bit (q5_k_m) - Light and fast": "q5_k_m",
            "4-bit (q4_k_m) - Very light, fits 6 GB cards": "q4_k_m",
            "2-bit (q2_k) - Lowest memory, fast": "q2_k",
        }
        for display_text in self.quant_map.keys():
            self.quant_combo.addItem(display_text)
            
        for display_text, val in self.quant_map.items():
            if val == self.selected_quant:
                self.quant_combo.setCurrentText(display_text)
                break
                
        form_layout.addWidget(quant_label, 1, 0)
        form_layout.addWidget(self.quant_combo, 1, 1)
        
        layout.addWidget(form_widget)
        
        buttons_layout = QHBoxLayout()
        cancel_button = QPushButton("Cancel")
        ok_button = QPushButton("OK")
        ok_button.setDefault(True)
        
        cancel_button.clicked.connect(self.reject)
        ok_button.clicked.connect(self.accept_selection)
        
        buttons_layout.addStretch()
        buttons_layout.addWidget(cancel_button)
        buttons_layout.addWidget(ok_button)
        layout.addLayout(buttons_layout)

    def _get_gpu_vram_gb(self) -> float | None:
        import subprocess
        try:
            res = subprocess.run(
                ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
                capture_output=True, text=True, check=True
            )
            vram_mb = float(res.stdout.strip().split("\n")[0])
            return vram_mb / 1024.0
        except Exception:
            pass

        if is_windows():
            try:
                res = subprocess.run(
                    ["wmic", "path", "win32_VideoController", "get", "AdapterRAM"],
                    capture_output=True, text=True, check=True
                )
                lines = [l.strip() for l in res.stdout.strip().split("\n") if l.strip()]
                if len(lines) > 1:
                    bytes_ram = int(lines[1])
                    return bytes_ram / (1024.0 ** 3)
            except Exception:
                pass
        else:
            for card in ("/sys/class/drm/card0/device/mem_info_vram_total", 
                         "/sys/class/drm/card1/device/mem_info_vram_total"):
                if os.path.exists(card):
                    try:
                        with open(card, "r") as f:
                            bytes_ram = int(f.read().strip())
                            return bytes_ram / (1024.0 ** 3)
                    except Exception:
                        pass
        return None

    def accept_selection(self):
        self.selected_backend = self.backend_combo.currentText()
        self.selected_quant = self.quant_map[self.quant_combo.currentText()]
        self.accept()

    def get_selected_backend(self):
        return self.selected_backend

    def get_selected_quant(self):
        return self.selected_quant
