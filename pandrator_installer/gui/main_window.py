"""Main Qt window for the Pandrator installer and launcher."""

import atexit
import logging
import os
import sys

from PyQt6.QtCore import QThread, Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QApplication, QCheckBox, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
    QLabel, QMainWindow, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QScrollArea, QSizePolicy, QTabWidget, QVBoxLayout, QWidget,
)

from ..components import ComponentOperationsMixin
from ..constants import (
    CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG,
    KOKORO_GPU_SUPPORT_CONFIG_FLAG,
    MAGPIE_GPU_SUPPORT_CONFIG_FLAG,
    RVC_GPU_SUPPORT_CONFIG_FLAG,
)
from ..models import InstallSelection, LaunchSelection
from ..operations import OperationsMixin
from ..pixi import PixiEnvironmentMixin
from ..reporting import NullReporter
from ..runtime import RuntimeMixin
from ..storage import StorageMixin
from ..workflows import WorkflowMixin
from .actions import GuiActionsMixin
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
        self.silero_var = False
        self.voxtral_var = False
        self.kokoro_var = False
        self.kokoro_cpu_var = False
        self.rvc_var = False
        self.rvc_cpu_var = False
        self.whisperx_var = False
        self.xtts_finetuning_var = False
        self.chatterbox_var = False
        self.chatterbox_cpu_var = False
        self.magpie_var = False
        self.magpie_cpu_var = False

        # Launch options
        self.launch_pandrator_var = True
        self.launch_rvc_var = False
        self.rvc_cpu_launch_var = False
        self.launch_xtts_var = False
        self.disable_deepspeed_var = False
        self.xtts_cpu_launch_var = False
        self.launch_voxcpm_var = False
        self.launch_fishs2_var = False
        self.launch_voxtral_var = False
        self.launch_kokoro_var = False
        self.kokoro_cpu_launch_var = False
        self.launch_silero_var = False
        self.launch_chatterbox_var = False
        self.chatterbox_cpu_launch_var = False
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
        self.magpie_process = None
        self.rvc_process = None
        self.backend_stop_targets = []

        # Worker thread
        self.worker = None
        self.reporter = NullReporter()

        # Set up the main window
        self.setWindowTitle("Pandrator Installer & Launcher")

        # Keep the installer compact while leaving enough room for two option columns.
        screen_size = QApplication.primaryScreen().availableGeometry().size()
        width = min(860, int(screen_size.width() * 0.82))
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
        atexit.register(self.shutdown_apps)

    def set_startup_tab(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        install_markers = (
            os.path.join(pandrator_path, 'config.json'),
            os.path.join(pandrator_path, 'Pandrator', 'main.py'),
            os.path.join(pandrator_path, 'Pandrator', 'pandrator.py'),
        )
        if any(os.path.exists(marker) for marker in install_markers):
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

        warning_message = (
            f"⚠️ WARNING: Your installation path contains spaces:\n\n"
            f"{self.initial_working_dir}\n\n"
            f"Some third-party tools still have trouble when installed from paths with spaces.\n\n"
            f"It's strongly recommended to move this installer to a path without spaces, such as:\n"
            f"C:\\Pandrator\n\n"
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
    ):
        card = QFrame()
        card.setObjectName("optionCard")
        card.setSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )

        layout = QVBoxLayout(card)
        layout.setContentsMargins(14, 11, 14, 11)
        layout.setSpacing(5)
        layout.addWidget(control)

        description_label = QLabel(description)
        description_label.setObjectName("mutedLabel")
        description_label.setWordWrap(True)
        description_label.setMinimumWidth(0)
        description_label.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Preferred,
        )
        layout.addWidget(description_label)

        if voice_capability:
            capability_label = QLabel(voice_capability)
            capability_label.setObjectName("voiceCapabilityBadge")
            capability_label.setSizePolicy(
                QSizePolicy.Policy.Maximum,
                QSizePolicy.Policy.Preferred,
            )
            layout.addWidget(capability_label)

        for extra_control in extra_controls:
            layout.addWidget(extra_control)

        return card

    @staticmethod
    def _add_option_cards(grid, cards):
        grid.setContentsMargins(8, 10, 8, 8)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(10)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        for index, card in enumerate(cards):
            grid.addWidget(card, index // 2, index % 2)

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
        self.magpie_checkbox = ToggleSwitch("Install Magpie")
        self.magpie_cpu_checkbox = QCheckBox("Use CPU-only runtime")

        engine_cards = (
            self._create_option_card(
                self.kokoro_checkbox,
                "Fast multilingual speech generation with a large built-in voice catalog.",
                (self.kokoro_cpu_checkbox,),
                "Pre-built voices",
            ),
            self._create_option_card(
                self.xtts_checkbox,
                "Multilingual speech generation from uploaded reference recordings.",
                (self.xtts_cpu_checkbox,),
                "Voice cloning",
            ),
            self._create_option_card(
                self.voxcpm_checkbox,
                "A local neural speech service that can follow an uploaded reference voice.",
                voice_capability="Voice cloning",
            ),
            self._create_option_card(
                self.fishs2_checkbox,
                "A local Fish Audio S2 service that can follow an uploaded reference voice.",
                voice_capability="Voice cloning",
            ),
            self._create_option_card(
                self.voxtral_checkbox,
                "A GPU-based multilingual service with a catalog of preset voices.",
                voice_capability="Pre-built voices",
            ),
            self._create_option_card(
                self.silero_checkbox,
                "A lightweight local engine with language-specific preset speakers.",
                voice_capability="Pre-built voices",
            ),
            self._create_option_card(
                self.chatterbox_checkbox,
                "Expressive local speech generated from uploaded reference recordings.",
                (self.chatterbox_cpu_checkbox,),
                "Voice cloning",
            ),
            self._create_option_card(
                self.magpie_checkbox,
                "A multilingual local service with several preset speakers per language.",
                (self.magpie_cpu_checkbox,),
                "Pre-built voices",
            ),
        )
        self._add_option_cards(engines_grid, engine_cards)
        content_layout.addWidget(engines_group)

        tools_group = QGroupBox("Optional tools")
        tools_grid = QGridLayout(tools_group)
        self.rvc_checkbox = QCheckBox("RVC voice conversion")
        self.rvc_cpu_checkbox = QCheckBox("Use CPU-only runtime")
        self.whisperx_checkbox = QCheckBox("WhisperX transcription")
        self.xtts_finetuning_checkbox = QCheckBox("XTTS fine-tuning")
        self.xtts_finetuning_checkbox.stateChanged.connect(self.update_whisperx_checkbox)
        tool_cards = (
            self._create_option_card(
                self.rvc_checkbox,
                "Reshapes generated speech with an RVC voice model for voice conversion and post-processing.",
                (self.rvc_cpu_checkbox,),
            ),
            self._create_option_card(
                self.whisperx_checkbox,
                "Adds transcription and alignment used by dubbing and XTTS training.",
            ),
            self._create_option_card(
                self.xtts_finetuning_checkbox,
                "Adds tools for training custom XTTS voices. WhisperX will be included.",
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
            self.magpie_checkbox,
            self.magpie_cpu_checkbox,
        )
        self.bind_cpu_install_option(
            self.rvc_checkbox,
            self.rvc_cpu_checkbox,
        )

        for checkbox in self.install_tab.findChildren(QCheckBox):
            checkbox.stateChanged.connect(self.update_install_button_state)

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
        self.launch_rvc_checkbox = ToggleSwitch("Launch RVC")
        self.rvc_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_xtts_checkbox = ToggleSwitch("Launch XTTS")
        self.xtts_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.deepspeed_checkbox = QCheckBox("Turn off DeepSpeed")
        self.launch_voxcpm_checkbox = ToggleSwitch("Launch VoxCPM2")
        self.launch_fishs2_checkbox = ToggleSwitch("Launch FishS2")
        self.launch_voxtral_checkbox = ToggleSwitch("Launch Voxtral")
        self.launch_kokoro_checkbox = ToggleSwitch("Launch Kokoro")
        self.kokoro_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_silero_checkbox = ToggleSwitch("Launch Silero")
        self.launch_chatterbox_checkbox = ToggleSwitch("Launch Chatterbox")
        self.chatterbox_cpu_launch_checkbox = QCheckBox("Use CPU")
        self.launch_magpie_checkbox = ToggleSwitch("Launch Magpie")
        self.magpie_cpu_launch_checkbox = QCheckBox("Use CPU")

        launch_cards = (
            self._create_option_card(
                self.launch_pandrator_checkbox,
                "Open the Pandrator desktop application.",
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
        self.stop_backend_button = QPushButton("Stop Running Backend")
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
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config = self.load_install_config(pandrator_path, detect_rvc=True)

        # Helper function to set widget state
        def set_widget_state(widget, state, value=None):
            widget.setEnabled(state)
            if isinstance(widget, QCheckBox) and value is not None:
                widget.setChecked(value)

        # Pandrator
        pandrator_installed = os.path.exists(pandrator_path)
        set_widget_state(self.pandrator_checkbox, not pandrator_installed, False if pandrator_installed else True)
        set_widget_state(self.launch_pandrator_checkbox, pandrator_installed, pandrator_installed)

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
        set_widget_state(self.fishs2_checkbox, not fishs2_support, False)
        set_widget_state(self.launch_fishs2_checkbox, fishs2_support, False)

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

        # WhisperX
        whisperx_support = config.get('whisperx_support', False)
        if whisperx_support:
            set_widget_state(self.whisperx_checkbox, False, False)
        elif xtts_finetuning_support:
            # XTTS Fine-tuning is installed
            set_widget_state(self.whisperx_checkbox, False, False)
        elif self.xtts_finetuning_checkbox.isChecked():
            # XTTS Fine-tuning is not installed but selected
            set_widget_state(self.whisperx_checkbox, False, True)
        else:
            set_widget_state(self.whisperx_checkbox, True, False)

        # Update launch and install buttons state
        self.launch_button.setEnabled(pandrator_installed)
        self.update_install_button_state()
        self.update_button.setEnabled(pandrator_installed)

        self.update_backend_runtime_controls()

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

    def update_whisperx_checkbox(self):
        """Update WhisperX checkbox when XTTS Fine-tuning is toggled"""
        installed_components = self.get_installed_components()
        whisperx_support = installed_components.get('whisperx', False)
        xtts_finetuning_support = installed_components.get('xtts_finetuning', False)

        if whisperx_support:
            self.whisperx_checkbox.setChecked(False)
            self.whisperx_checkbox.setEnabled(False)
        elif self.xtts_finetuning_checkbox.isChecked() and not xtts_finetuning_support:
            self.whisperx_checkbox.setChecked(True)
            self.whisperx_checkbox.setEnabled(False)
        elif xtts_finetuning_support:
            self.whisperx_checkbox.setChecked(False)
            self.whisperx_checkbox.setEnabled(False)
        else:
            self.whisperx_checkbox.setEnabled(True)

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
            'whisperx': config.get('whisperx_support', False),
            'xtts_finetuning': config.get('xtts_finetuning_support', False),
            'chatterbox': config.get('chatterbox_support', False),
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

        # Disable checkboxes in install tab
        for child in self.install_tab.findChildren(QCheckBox):
            child.setEnabled(False)

        # Disable checkboxes in launch tab
        for child in self.launch_tab.findChildren(QCheckBox):
            child.setEnabled(False)

    def enable_buttons(self):
        """Re-enable buttons after processing"""
        self.refresh_ui_state()

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
        return InstallSelection(
            pandrator=self.pandrator_checkbox.isChecked(),
            xtts=self.xtts_checkbox.isChecked() and not self.xtts_cpu_checkbox.isChecked(),
            xtts_cpu=self.xtts_checkbox.isChecked() and self.xtts_cpu_checkbox.isChecked(),
            voxcpm=self.voxcpm_checkbox.isChecked(),
            fishs2=self.fishs2_checkbox.isChecked(),
            silero=self.silero_checkbox.isChecked(),
            voxtral=self.voxtral_checkbox.isChecked(),
            kokoro=self.kokoro_checkbox.isChecked() and not self.kokoro_cpu_checkbox.isChecked(),
            kokoro_cpu=self.kokoro_checkbox.isChecked() and self.kokoro_cpu_checkbox.isChecked(),
            rvc=self.rvc_checkbox.isChecked() and not self.rvc_cpu_checkbox.isChecked(),
            rvc_cpu=self.rvc_checkbox.isChecked() and self.rvc_cpu_checkbox.isChecked(),
            whisperx=self.whisperx_checkbox.isChecked(),
            xtts_finetuning=self.xtts_finetuning_checkbox.isChecked(),
            chatterbox=(
                self.chatterbox_checkbox.isChecked()
                and not self.chatterbox_cpu_checkbox.isChecked()
            ),
            chatterbox_cpu=(
                self.chatterbox_checkbox.isChecked()
                and self.chatterbox_cpu_checkbox.isChecked()
            ),
            magpie=self.magpie_checkbox.isChecked() and not self.magpie_cpu_checkbox.isChecked(),
            magpie_cpu=self.magpie_checkbox.isChecked() and self.magpie_cpu_checkbox.isChecked(),
        )

    def apply_install_selection(self, selection):
        """Reflect a typed installation selection in the GUI."""
        self.pandrator_checkbox.setChecked(selection.pandrator)
        for component in (
            "voxcpm",
            "fishs2",
            "silero",
            "voxtral",
            "whisperx",
            "xtts_finetuning",
        ):
            getattr(self, f"{component}_checkbox").setChecked(getattr(selection, component))
        for component in ("xtts", "kokoro", "chatterbox", "magpie", "rvc"):
            cpu_selected = getattr(selection, f"{component}_cpu")
            getattr(self, f"{component}_checkbox").setChecked(
                getattr(selection, component) or cpu_selected
            )
            getattr(self, f"{component}_cpu_checkbox").setChecked(cpu_selected)
        self.update_whisperx_checkbox()

    def snapshot_launch_selection(self):
        """Read launch choices on the GUI thread before work starts."""
        return LaunchSelection(
            pandrator=self.launch_pandrator_checkbox.isChecked(),
            rvc=self.launch_rvc_checkbox.isChecked(),
            rvc_cpu=self.rvc_cpu_launch_checkbox.isChecked(),
            xtts=self.launch_xtts_checkbox.isChecked(),
            disable_deepspeed=self.deepspeed_checkbox.isChecked(),
            xtts_cpu=self.xtts_cpu_launch_checkbox.isChecked(),
            voxcpm=self.launch_voxcpm_checkbox.isChecked(),
            fishs2=self.launch_fishs2_checkbox.isChecked(),
            voxtral=self.launch_voxtral_checkbox.isChecked(),
            kokoro=self.launch_kokoro_checkbox.isChecked(),
            kokoro_cpu=self.kokoro_cpu_launch_checkbox.isChecked(),
            silero=self.launch_silero_checkbox.isChecked(),
            chatterbox=self.launch_chatterbox_checkbox.isChecked(),
            chatterbox_cpu=self.chatterbox_cpu_launch_checkbox.isChecked(),
            magpie=self.launch_magpie_checkbox.isChecked(),
            magpie_cpu=self.magpie_cpu_launch_checkbox.isChecked(),
        )
