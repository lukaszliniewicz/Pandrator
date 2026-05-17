import os
import subprocess
import logging
import time
import shutil
import hashlib
import requests
import sys
import atexit
import psutil
import json
import re
import traceback
import tempfile
import ctypes
import winreg
from datetime import datetime
from PyQt6.QtWidgets import (QApplication, QMainWindow, QWidget, QTabWidget, 
                            QVBoxLayout, QHBoxLayout, QLabel, QPushButton, 
                            QCheckBox, QTextEdit, QPlainTextEdit, QProgressBar, QScrollArea,
                            QMessageBox, QGroupBox, QFrame, QSplitter,
                            QDialog, QGridLayout)
from PyQt6.QtCore import Qt, QThread, pyqtSignal, QTimer, QCoreApplication, QSize, QEventLoop, QObject
from PyQt6.QtGui import QIcon, QFont, QPixmap
from PyQt6.QtGui import QColor
from PyQt6.QtGui import QColor, QPalette


PIXI_BINARY_NAME = 'pixi.exe'
PIXI_DOWNLOAD_URL = 'https://github.com/prefix-dev/pixi/releases/latest/download/pixi-x86_64-pc-windows-msvc.exe'
PIXI_HOME_DIRNAME = '.pixi-home'
PIXI_CACHE_DIRNAME = '.pixi-cache'
PANDRATOR_PYTHON_VERSION = '3.11'
SILERO_PYTHON_VERSION = '3.10'
XTTS_FINETUNING_PYTHON_VERSION = '3.10'
PYQT6_RUNTIME_PIN = 'PyQt6==6.7.1'
WHISPERX_PYTHON_VERSION = '3.13'
WHISPERX_VERSION = '3.8.5'
WHISPERX_CTRANSLATE2_VERSION = '4.7.1'
WHISPERX_TORCH_VERSION = '2.8.0'
WHISPERX_TORCHVISION_VERSION = '0.23.0'
WHISPERX_TORCHAUDIO_VERSION = '2.8.0'
WHISPERX_TORCH_INDEX_URL = 'https://download.pytorch.org/whl/cu128'

XTTS_API_REPO_URL = 'https://github.com/lukaszliniewicz/xtts2_api.git'
XTTS_API_REPO_DIRNAME = 'xtts2_api'
VOXTRAL_API_REPO_URL = 'https://github.com/lukaszliniewicz/voxtral-fastapi.git'
VOXTRAL_API_REPO_DIRNAME = 'voxtral-fastapi'
PANDRATOR_REPO_URL = 'https://github.com/lukaszliniewicz/Pandrator.git'
SUBDUB_REPO_URL = 'https://github.com/lukaszliniewicz/Subdub.git'
PYCROPPDF_REPO_URL = 'https://github.com/lukaszliniewicz/PyCropPDF.git'
EASY_XTTS_TRAINER_REPO_URL = 'https://github.com/lukaszliniewicz/easy_xtts_trainer.git'

INSTALLER_STATE_FILENAME = 'installer_state.json'
RVC_PYTHON_FORK_INSTALL_SPEC = 'git+https://github.com/JarodMica/rvc-python@782467ababe17698a4b5100aedfe16e69cebaa56'
RVC_PYTHON_FORK_SOURCE_FRAGMENT = 'github.com/jarodmica/rvc-python'
RVC_FAIRSEQ_WHEEL_URL_BY_PYTHON = {
    '3.10': 'https://huggingface.co/Jmica/rvc/resolve/main/fairseq-0.12.2-cp310-cp310-win_amd64.whl?download=true',
    '3.11': 'https://huggingface.co/Jmica/rvc/resolve/main/fairseq-0.12.4-cp311-cp311-win_amd64.whl?download=true',
}
RVC_TORCH_VERSION = '2.3.1'
RVC_TORCHVISION_VERSION = '0.18.1'
RVC_TORCHAUDIO_VERSION = '2.3.1'
RVC_NUMPY_VERSION = '1.23.5'
RVC_TORCH_INDEX_URL = 'https://download.pytorch.org/whl/cu121'
RVC_REQUIRED_PACKAGE_SPECS = (
    'rvc-python',
    'fairseq',
    f'numpy=={RVC_NUMPY_VERSION}',
    f'torch=={RVC_TORCH_VERSION}',
    f'torchvision=={RVC_TORCHVISION_VERSION}',
    f'torchaudio=={RVC_TORCHAUDIO_VERSION}',
)
SILERO_REQUIRED_PACKAGE_SPECS = (
    'requests',
    'silero-api-server',
)
WHISPERX_REQUIRED_PACKAGE_SPECS = (
    f'whisperx=={WHISPERX_VERSION}',
    f'ctranslate2=={WHISPERX_CTRANSLATE2_VERSION}',
    f'torch=={WHISPERX_TORCH_VERSION}',
    f'torchvision=={WHISPERX_TORCHVISION_VERSION}',
    f'torchaudio=={WHISPERX_TORCHAUDIO_VERSION}',
)
XTTS_FINETUNING_TORCH_PACKAGE_SPECS = (
    'torch==2.2.0+cu118',
    'torchaudio==2.2.0+cu118',
)



class Worker(QThread):
    """Worker thread for running background processes"""
    update_progress = pyqtSignal(float)
    update_status = pyqtSignal(str)
    finished = pyqtSignal()
    error = pyqtSignal(str)

    def __init__(self, function, *args, **kwargs):
        super().__init__()
        self.function = function
        self.args = args
        self.kwargs = kwargs
        
    def run(self):
        try:
            self.function(*self.args, **self.kwargs)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))
            logging.error(f"Error in worker thread: {str(e)}")
            logging.error(traceback.format_exc())


class QtLogEmitter(QObject):
    message_logged = pyqtSignal(str)


class QtLogHandler(logging.Handler):
    def __init__(self, emitter):
        super().__init__()
        self.emitter = emitter

    def emit(self, record):
        try:
            message = self.format(record)
            self.emitter.message_logged.emit(message)
        except Exception:
            self.handleError(record)


class InfoDialog(QDialog):
    """Dialog for showing application information"""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pandrator Installer Information")
        self.setMinimumSize(500, 300)
        
        layout = QVBoxLayout(self)
        
        info_text = QTextEdit()
        info_text.setReadOnly(True)
        info_text.setHtml("""
        <h2>Pandrator Installer & Launcher</h2>
        <p>This tool helps you set up and run Pandrator as well as TTS engines and tools.</p>
        <p>It will install:</p>
        <ul>
            <li>Pandrator</li>
            <li>Pixi</li>
            <li>Required Python packages</li>
            <li>Dependencies (Calibre)</li>
        </ul>
        <p>To uninstall Pandrator, simply delete the Pandrator folder.</p>
        <p>The installation will take between 3 and 30GB of disk space depending on the number of selected options.</p>
        """)
        
        layout.addWidget(info_text)
        
        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)


class PandratorInstaller(QMainWindow):
    def __init__(self):
        super().__init__()
        self.initial_working_dir = os.getcwd()
        
        # Check for spaces in the working directory
        if ' ' in self.initial_working_dir:
            self.show_space_warning()
        
        # Define instance variables for checkboxes
        # Installation options
        self.pandrator_var = False
        self.xtts_var = False
        self.xtts_cpu_var = False
        self.silero_var = False
        self.voxtral_var = False
        self.rvc_var = False
        self.whisperx_var = False
        self.xtts_finetuning_var = False

        # Launch options
        self.launch_pandrator_var = True
        self.launch_xtts_var = False
        self.disable_deepspeed_var = False
        self.xtts_cpu_launch_var = False
        self.launch_voxtral_var = False
        self.launch_silero_var = False

        # Initialize process attributes
        self.xtts_process = None
        self.pandrator_process = None
        self.silero_process = None
        self.voxtral_process = None

        # Worker thread
        self.worker = None
        
        # Set up the main window
        self.setWindowTitle("Pandrator Installer & Launcher")
        
        # Calculate window size
        screen_size = QApplication.primaryScreen().size()
        width = int(screen_size.width() * 0.5)
        height = int(screen_size.height() * 0.6)
        self.resize(width, height)
        
        # Create central widget and main layout
        self.central_widget = QWidget()
        self.setCentralWidget(self.central_widget)
        self.main_layout = QVBoxLayout(self.central_widget)
        
        # Create header with title and info button
        header_layout = QHBoxLayout()
        
        # Title
        self.title_label = QLabel("Pandrator Installer & Launcher")
        title_font = QFont("Arial", 18, QFont.Weight.Bold)
        self.title_label.setFont(title_font)
        header_layout.addWidget(self.title_label)
        
        # Info button
        self.info_button = QPushButton("ℹ️ Info")
        self.info_button.setFixedWidth(100)
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
        self.main_layout.addWidget(self.progress_bar)
        
        self.status_label = QLabel("Ready")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
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

    def setup_install_tab(self):
        """Set up the Install tab"""
        layout = QVBoxLayout(self.install_tab)
        
        # Core components section
        components_group = QGroupBox("Components")
        components_layout = QVBoxLayout(components_group)
        
        # Pandrator checkbox
        self.pandrator_checkbox = QCheckBox("Pandrator")
        self.pandrator_checkbox.setChecked(True)
        components_layout.addWidget(self.pandrator_checkbox)
        
        # TTS Engines section - with BOLD label
        tts_engines_label = QLabel("TTS Engines")
        tts_engines_label.setStyleSheet("font-weight: bold;")
        components_layout.addWidget(tts_engines_label)
        
        components_layout.addWidget(QLabel("You can select and install new engines and tools after the initial installation."))
        
        engines_layout = QHBoxLayout()
        
        self.xtts_checkbox = QCheckBox("XTTS")
        engines_layout.addWidget(self.xtts_checkbox)
        
        self.xtts_cpu_checkbox = QCheckBox("XTTS CPU only")
        engines_layout.addWidget(self.xtts_cpu_checkbox)
        
        self.silero_checkbox = QCheckBox("Silero")
        engines_layout.addWidget(self.silero_checkbox)

        self.voxtral_checkbox = QCheckBox("Voxtral (GPU only)")
        engines_layout.addWidget(self.voxtral_checkbox)
        
        components_layout.addLayout(engines_layout)
        
        # Other tools section - with BOLD label
        other_tools_label = QLabel("Other tools")
        other_tools_label.setStyleSheet("font-weight: bold;")
        components_layout.addWidget(other_tools_label)
        
        self.rvc_checkbox = QCheckBox("RVC (rvc-python fork)")
        components_layout.addWidget(self.rvc_checkbox)
        
        self.whisperx_checkbox = QCheckBox("WhisperX (needed for dubbing and XTTS training)")
        components_layout.addWidget(self.whisperx_checkbox)
        
        self.xtts_finetuning_checkbox = QCheckBox("XTTS Fine-tuning")
        self.xtts_finetuning_checkbox.stateChanged.connect(self.update_whisperx_checkbox)
        components_layout.addWidget(self.xtts_finetuning_checkbox)
        
        layout.addWidget(components_group)
        
        # Buttons section
        buttons_layout = QHBoxLayout()
        
        self.install_button = QPushButton("Install")
        self.install_button.clicked.connect(self.install_pandrator)
        buttons_layout.addWidget(self.install_button)
        
        self.update_button = QPushButton("Update Pandrator")
        self.update_button.clicked.connect(self.update_pandrator)
        buttons_layout.addWidget(self.update_button)
        
        self.open_log_button = QPushButton("View Installation Log")
        self.open_log_button.clicked.connect(self.open_log_file)
        self.open_log_button.setEnabled(False)
        buttons_layout.addWidget(self.open_log_button)
        
        layout.addLayout(buttons_layout)
        
        # Add stretch to push everything to the top
        layout.addStretch()

    def setup_launch_tab(self):
        """Set up the Launch tab"""
        layout = QVBoxLayout(self.launch_tab)
        
        # Launch options group
        launch_group = QGroupBox("Launch Options")
        launch_layout = QVBoxLayout(launch_group)
        
        # Pandrator checkbox
        self.launch_pandrator_checkbox = QCheckBox("Pandrator")
        self.launch_pandrator_checkbox.setChecked(True)
        launch_layout.addWidget(self.launch_pandrator_checkbox)
        
        # XTTS options
        xtts_frame = QWidget()
        xtts_layout = QGridLayout(xtts_frame)
        xtts_layout.setContentsMargins(0, 0, 0, 0)
        
        self.launch_xtts_checkbox = QCheckBox("XTTS")
        xtts_layout.addWidget(self.launch_xtts_checkbox, 0, 0)
        
        self.xtts_cpu_launch_checkbox = QCheckBox("Use CPU")
        xtts_layout.addWidget(self.xtts_cpu_launch_checkbox, 0, 1)
        
        self.deepspeed_checkbox = QCheckBox("Turn off DeepSpeed")
        xtts_layout.addWidget(self.deepspeed_checkbox, 0, 2)
        
        launch_layout.addWidget(xtts_frame)

        # Voxtral checkbox
        self.launch_voxtral_checkbox = QCheckBox("Voxtral")
        launch_layout.addWidget(self.launch_voxtral_checkbox)
        
        # Silero checkbox
        self.launch_silero_checkbox = QCheckBox("Silero")
        launch_layout.addWidget(self.launch_silero_checkbox)
        
        layout.addWidget(launch_group)
        
        # Launch button
        self.launch_button = QPushButton("Launch")
        self.launch_button.clicked.connect(self.launch_apps)
        self.launch_button.setMinimumHeight(40)
        layout.addWidget(self.launch_button)
        
        # Add stretch to push everything to the top
        layout.addStretch()

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

    # Utility functions
    def refresh_ui_state(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config_path = os.path.join(pandrator_path, 'config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
        else:
            config = {}

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

        # Voxtral
        voxtral_support = config.get('voxtral_support', False)
        set_widget_state(self.voxtral_checkbox, not voxtral_support, False)
        set_widget_state(self.launch_voxtral_checkbox, voxtral_support, False)

        # Silero
        silero_support = config.get('silero_support', False)
        set_widget_state(self.silero_checkbox, not silero_support, False)
        set_widget_state(self.launch_silero_checkbox, silero_support, False)

        # RVC
        rvc_support = config.get('rvc_support', False)
        set_widget_state(self.rvc_checkbox, not rvc_support, False)

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
        self.install_button.setEnabled(True)
        self.update_button.setEnabled(pandrator_installed)

    def update_whisperx_checkbox(self):
        """Update WhisperX checkbox when XTTS Fine-tuning is toggled"""
        installed_components = self.get_installed_components()
        xtts_finetuning_support = installed_components.get('xtts_finetuning', False)
        
        if self.xtts_finetuning_checkbox.isChecked() and not xtts_finetuning_support:
            self.whisperx_checkbox.setChecked(True)
            self.whisperx_checkbox.setEnabled(False)
        elif xtts_finetuning_support:
            self.whisperx_checkbox.setChecked(False)
            self.whisperx_checkbox.setEnabled(False)
        else:
            self.whisperx_checkbox.setEnabled(True)

    def get_installed_components(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        config_path = os.path.join(pandrator_path, 'config.json')
        
        if os.path.exists(config_path):
            with open(config_path, 'r') as f:
                config = json.load(f)
        else:
            config = {}
        
        return {
            'xtts': config.get('xtts_support', False),
            'voxtral': config.get('voxtral_support', False),
            'silero': config.get('silero_support', False),
            'rvc': config.get('rvc_support', False),
            'whisperx': config.get('whisperx_support', False),
            'xtts_finetuning': config.get('xtts_finetuning_support', False)
        }

    def disable_buttons(self):
        """Disable all buttons during processing"""
        self.install_button.setEnabled(False)
        self.update_button.setEnabled(False)
        self.launch_button.setEnabled(False)
        
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
        logging.info(text)

    # Installation methods
    def initialize_logging(self):
        """Initialize robust file, console, and GUI logging."""
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        os.makedirs(pandrator_path, exist_ok=True)
        logs_path = os.path.join(pandrator_path, 'Logs')
        os.makedirs(logs_path, exist_ok=True)

        current_time = datetime.now().strftime("%Y%m%d_%H%M%S")
        self.log_filename = os.path.join(logs_path, f'pandrator_installation_log_{current_time}.log')

        logger = logging.getLogger()
        logger.setLevel(logging.DEBUG)

        for handler in list(logger.handlers):
            if getattr(handler, '_pandrator_managed_handler', False):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

        formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

        file_handler = logging.FileHandler(self.log_filename, encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(formatter)
        file_handler._pandrator_managed_handler = True

        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.INFO)
        console_handler.setFormatter(formatter)
        console_handler._pandrator_managed_handler = True

        gui_handler = QtLogHandler(self.log_emitter)
        gui_handler.setLevel(logging.INFO)
        gui_handler.setFormatter(formatter)
        gui_handler._pandrator_managed_handler = True

        logger.addHandler(file_handler)
        logger.addHandler(console_handler)
        logger.addHandler(gui_handler)

        self.open_log_button.setEnabled(True)
        self.open_log_from_tab_button.setEnabled(True)

        logging.info(f"Logging initialized. Writing to: {self.log_filename}")

    def configure_tls_certificates(self, force=False):
        if self.tls_configured and not force:
            return

        self.tls_configured = True

        for env_name in ('SSL_CERT_FILE', 'REQUESTS_CA_BUNDLE', 'CURL_CA_BUNDLE'):
            value = os.environ.get(env_name)
            if value and not os.path.exists(value):
                logging.warning(f"Ignoring invalid {env_name} path: {value}")
                os.environ.pop(env_name, None)

        try:
            import certifi

            ca_bundle = certifi.where()
            if ca_bundle and os.path.exists(ca_bundle):
                os.environ['SSL_CERT_FILE'] = ca_bundle
                os.environ['REQUESTS_CA_BUNDLE'] = ca_bundle
                os.environ['CURL_CA_BUNDLE'] = ca_bundle
                self.ca_bundle_path = ca_bundle
                logging.info(f"Configured TLS certificate bundle: {ca_bundle}")
            else:
                logging.warning("certifi did not provide a usable certificate bundle path.")
        except Exception as e:
            logging.warning(f"Could not configure TLS certificate bundle via certifi: {str(e)}")

    def is_certificate_error(self, error):
        error_text = str(error).lower()
        return (
            'certificate verify failed' in error_text
            or 'sslcertverificationerror' in error_text
            or 'unable to get local issuer certificate' in error_text
        )

    def shutdown_logging(self):
        logger = logging.getLogger()
        for handler in list(logger.handlers):
            if getattr(handler, '_pandrator_managed_handler', False):
                logger.removeHandler(handler)
                try:
                    handler.close()
                except Exception:
                    pass

    def get_network_subprocess_env(self):
        env = os.environ.copy()
        if self.ca_bundle_path and os.path.exists(self.ca_bundle_path):
            env['SSL_CERT_FILE'] = self.ca_bundle_path
            env['REQUESTS_CA_BUNDLE'] = self.ca_bundle_path
            env['CURL_CA_BUNDLE'] = self.ca_bundle_path
            env['GIT_SSL_CAINFO'] = self.ca_bundle_path
        return env

    def is_admin(self):
        """Check if the current process has admin privileges."""
        try:
            return ctypes.windll.shell32.IsUserAnAdmin() != 0
        except:
            return False

    def install_pytorch_for_xtts_finetuning(self, pandrator_path, env_name):
        logging.info(f"Installing PyTorch for XTTS Fine-tuning in {env_name}...")
        try:
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install']
                + list(XTTS_FINETUNING_TORCH_PACKAGE_SPECS)
                + ['--index-url', 'https://download.pytorch.org/whl/cu118']
            )
            logging.info("PyTorch for XTTS Fine-tuning installed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install PyTorch for XTTS Fine-tuning in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_pandrator(self):
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_already_installed = os.path.exists(pandrator_path)
        
        installed_components = self.get_installed_components()
        
        # Get checkbox states
        self.pandrator_var = self.pandrator_checkbox.isChecked()
        self.xtts_var = self.xtts_checkbox.isChecked()
        self.xtts_cpu_var = self.xtts_cpu_checkbox.isChecked()
        self.silero_var = self.silero_checkbox.isChecked()
        self.voxtral_var = self.voxtral_checkbox.isChecked()
        self.rvc_var = self.rvc_checkbox.isChecked()
        self.whisperx_var = self.whisperx_checkbox.isChecked()
        self.xtts_finetuning_var = self.xtts_finetuning_checkbox.isChecked()
        
        new_components_selected = (
            (self.xtts_var or self.xtts_cpu_var) and not installed_components['xtts'] or
            self.silero_var and not installed_components['silero'] or
            self.voxtral_var and not installed_components['voxtral'] or
            self.rvc_var and not installed_components['rvc'] or
            self.whisperx_var and not installed_components['whisperx']
        )
        
        if pandrator_already_installed and not self.pandrator_var:
            if not new_components_selected:
                QMessageBox.information(self, "Info", "No new components selected for installation.")
                return
        elif not pandrator_already_installed and not self.pandrator_var:
            QMessageBox.critical(self, "Error", "Pandrator must be installed first before adding new components.")
            return

        self.disable_buttons()
        self.progress_bar.setValue(0)
        self.status_label.setText("Installing...")

        self.initialize_logging()

        logging.info("Installation process started.")

        # Create worker thread to run the installation
        self.worker = Worker(self.install_process)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_installation_finished)
        self.worker.error.connect(self.on_installation_error)
        self.worker.start()

    def on_installation_finished(self):
        """Handle completion of installation process"""
        self.update_status("Installation complete!")
        self.tabs.setCurrentWidget(self.launch_tab)
        self.enable_buttons()
        QMessageBox.information(self, "Success", "Installation completed successfully!")

    def on_installation_error(self, error_message):
        """Handle installation errors"""
        self.update_status(f"Installation failed: {error_message}")
        self.enable_buttons()
        QMessageBox.critical(self, "Installation Error", f"Installation failed:\n\n{error_message}\n\nCheck the log for more details.")

    def open_log_file(self):
        """Open the log file with the default system application"""
        if hasattr(self, 'log_filename') and self.log_filename and os.path.exists(self.log_filename):
            os.startfile(self.log_filename)
        else:
            QMessageBox.warning(self, "Log Not Available", "No log file is available yet.")

    def run_command(self, command, use_shell=False, cwd=None, env=None, log_errors=True):
        try:
            if use_shell:
                process = subprocess.Popen(
                    command if isinstance(command, str) else " ".join(command),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    cwd=cwd,
                    env=env,
                    encoding='utf-8',
                    errors='replace'
                )
            else:
                process = subprocess.Popen(
                    command,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=cwd,
                    env=env,
                    encoding='utf-8',
                    errors='replace'
                )
            
            stdout, stderr = process.communicate()
            
            if process.returncode != 0:
                raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
            
            logging.info(f"Command executed: {command if isinstance(command, str) else ' '.join(command)}")
            logging.debug(f"STDOUT: {stdout}")
            logging.debug(f"STDERR: {stderr}")
            
            return stdout, stderr
        except subprocess.CalledProcessError as e:
            log = logging.error if log_errors else logging.debug
            log(f"Error executing command: {command if isinstance(command, str) else ' '.join(command)}")
            log(f"Error message: {str(e)}")
            log(f"STDOUT: {e.stdout}")
            log(f"STDERR: {e.stderr}")
            raise

    def check_program_installed(self, program):
        try:
            self.run_command(['where', program], log_errors=False)
            return True
        except subprocess.CalledProcessError:
            return False

    def install_chocolatey(self):
        """Install Chocolatey using PowerShell's Invoke-WebRequest (no deprecated WebClient).

        Returns True on success, False otherwise.  Requires elevated process.
        """
        logging.info("Installing Chocolatey...")
        try:
            ps_script = """
    $ProgressPreference = 'SilentlyContinue'
    $ErrorActionPreference = 'Stop'
    $installer = Join-Path $env:TEMP 'choco_install.ps1'
Invoke-WebRequest -Uri 'https://community.chocolatey.org/install.ps1' -OutFile $installer
powershell -ExecutionPolicy Bypass -File $installer
Remove-Item $installer -Force -ErrorAction SilentlyContinue
    """
            process = subprocess.Popen(
                ["powershell", "-Command", ps_script],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            stdout, stderr = process.communicate()

            if process.returncode == 0:
                logging.info("Chocolatey installed successfully.")
                # Enable global confirmation
                subprocess.run(
                    ["powershell", "-Command", "choco feature enable -n=allowGlobalConfirmation"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                logging.info("Global confirmation enabled for Chocolatey.")
                # Refresh env vars so choco.exe is on PATH for subsequent calls
                self.refresh_environment_variables()
                return True
            else:
                logging.error(f"Failed to install Chocolatey. Exit code: {process.returncode}")
                logging.error(f"STDOUT: {stdout}")
                logging.error(f"STDERR: {stderr}")
                return False
        except Exception as e:
            logging.error(f"An error occurred during Chocolatey installation: {str(e)}")
            logging.error(traceback.format_exc())
            return False

    def refresh_environment_variables(self):
        """Refresh environment variables from the Windows registry for the current process.

        Reads machine and user-level environment variables from the registry and injects
        them into os.environ AND the current process environment block via
        SetEnvironmentVariableW.  This ensures child processes spawned by subprocess
        inherit the updated PATH/COMSPEC etc. without rebooting or broadcasting
        WM_SETTINGCHANGE (which is unreliable from a non-elevated context).
        """
        try:
            logging.info("Refreshing environment variables from registry...")

            def _read_registry_env(key_path, root=winreg.HKEY_LOCAL_MACHINE):
                try:
                    with winreg.OpenKey(
                        root, key_path,
                        0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
                    ) as key:
                        i = 0
                        while True:
                            try:
                                name, value, value_type = winreg.EnumValue(key, i)
                                if value_type in (
                                    winreg.REG_SZ,
                                    winreg.REG_EXPAND_SZ,
                                ):
                                    os.environ[name] = value
                                    ctypes.windll.kernel32.SetEnvironmentVariableW(
                                        name, value
                                    )
                                i += 1
                            except OSError:
                                break
                except Exception as e:
                    logging.warning(
                        f"Could not read env vars from {key_path}: {e}"
                    )

            # Machine-level environment variables
            _read_registry_env(
                r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
            )
            # User-level environment variables
            _read_registry_env(r"Environment", root=winreg.HKEY_CURRENT_USER)

            logging.info("Environment variables refreshed from registry.")
        except Exception as e:
            logging.error(f"Failed to refresh environment variables: {str(e)}")
            logging.error(traceback.format_exc())
            raise

    def install_dependencies(self):
        return self.install_calibre()
            
    def show_calibre_installation_message(self):
        message = ("Calibre installation failed. Please install Calibre manually.\n"
                   "You can download it from: https://calibre-ebook.com/download_windows")
        QMessageBox.warning(self, "Calibre Installation Required", message)

    def install_with_chocolatey(self, package_name, args=""):
        logging.info(f"Attempting to install {package_name} with Chocolatey...")
        
        # First, try using 'choco' command
        try:
            process = subprocess.Popen(
                f"choco install {package_name} -y {args}",
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=True,
                text=True
            )
            
            stdout, stderr = process.communicate()
            logging.info(stdout)
            
            if process.returncode == 0:
                logging.info(f"{package_name} installed successfully using 'choco' command.")
                return True
        except Exception as e:
            logging.error(f"Error using 'choco' command: {str(e)}")
        
        # If 'choco' command fails, try using the Chocolatey executable directly
        try:
            choco_exe = os.path.join(os.environ.get('ProgramData', ''), 'chocolatey', 'bin', 'choco.exe')
            if os.path.exists(choco_exe):
                process = subprocess.Popen(
                    f'"{choco_exe}" install {package_name} -y {args}',
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    shell=True,
                    text=True
                )
                
                stdout, stderr = process.communicate()
                logging.info(stdout)
                
                if process.returncode == 0:
                    logging.info(f"{package_name} installed successfully using Chocolatey executable.")
                    return True
            else:
                logging.error("Chocolatey executable not found.")
        except Exception as e:
            logging.error(f"Error using Chocolatey executable: {str(e)}")
        
        logging.error(f"Failed to install {package_name} using Chocolatey.")
        return False

    def install_calibre(self):
        """Install Calibre.  Prefers winget, falls back to Chocolatey."""
        logging.info("Checking installation for Calibre")
        if not self.check_program_installed('calibre'):
            logging.info("Installing Calibre...")

            # Try winget first (built-in on Windows 10+/11)
            winget_exe = os.path.join(os.environ.get('LOCALAPPDATA', r'C:\Program Files\WindowsApps'),
                                      'Microsoft.DesktopAppInstaller_8wekyb3d8bbwe', 'winget.exe')
            winget_alt = r'C:\Program Files (x86)\Microsoft\WinGet\winget.exe'
            winget_found = os.path.exists(winget_exe) or os.path.exists(winget_alt)

            if winget_found:
                try:
                    self.update_status("Installing Calibre via winget...")
                    process = subprocess.Popen(
                        ["winget", "install", "--id", "calibre",
                         "--accept-package-agreements", "--accept-source-agreements"],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                    )
                    stdout, stderr = process.communicate(timeout=300)
                    if process.returncode == 0:
                        logging.info("Calibre installed via winget.")
                        self.refresh_environment_variables()
                        if self.check_program_installed('calibre'):
                            return True
                        logging.warning("Calibre installed via winget but not detected on PATH yet.")
                        return True
                    else:
                        logging.warning(f"winget calibre install returned {process.returncode}: {stderr}")
                except subprocess.TimeoutExpired:
                    logging.warning("winget calibre install timed out, falling back to Chocolatey.")
                except Exception as e:
                    logging.warning(f"winget calibre install failed: {e}, falling back to Chocolatey.")

            # Fallback: Chocolatey
            if self.install_with_chocolatey('calibre'):
                self.refresh_environment_variables()
                if self.check_program_installed('calibre'):
                    logging.info("Calibre installed successfully.")
                    return True
                else:
                    logging.warning("Calibre installation not detected after installation attempt.")
                    return False
            else:
                self.show_calibre_installation_message()
                return False
        else:
            logging.info("Calibre is already installed.")
            return True

    def get_pixi_executable(self, pandrator_path):
        return os.path.join(pandrator_path, 'bin', PIXI_BINARY_NAME)

    def get_pixi_env_dir(self, pandrator_path, env_name):
        return os.path.join(pandrator_path, 'envs', env_name)

    def get_pixi_manifest_path(self, pandrator_path, env_name):
        return os.path.join(self.get_pixi_env_dir(pandrator_path, env_name), 'pixi.toml')

    def get_pixi_subprocess_env(self, pandrator_path):
        pixi_home = os.path.join(pandrator_path, PIXI_HOME_DIRNAME)
        pixi_cache = os.path.join(pandrator_path, PIXI_CACHE_DIRNAME)
        rattler_cache = os.path.join(pixi_cache, 'rattler')

        os.makedirs(pixi_home, exist_ok=True)
        os.makedirs(pixi_cache, exist_ok=True)
        os.makedirs(rattler_cache, exist_ok=True)

        env = os.environ.copy()
        env['PIXI_HOME'] = pixi_home
        env['PIXI_CACHE_DIR'] = pixi_cache
        env['RATTLER_CACHE_DIR'] = rattler_cache
        return env

    def run_pixi_command(self, pandrator_path, arguments, cwd=None, log_errors=True):
        pixi_executable = self.get_pixi_executable(pandrator_path)
        if not os.path.exists(pixi_executable):
            raise FileNotFoundError(
                f"Pixi executable not found at {pixi_executable}. Run Install or Update to set up Pixi."
            )

        return self.run_command(
            [pixi_executable] + arguments,
            cwd=cwd,
            env=self.get_pixi_subprocess_env(pandrator_path),
            log_errors=log_errors,
        )

    def build_pixi_run_command(self, pandrator_path, env_name, command):
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        if not os.path.exists(manifest_path):
            raise FileNotFoundError(
                f"Pixi environment manifest not found for {env_name}: {manifest_path}. "
                "Run Install or Update to migrate this installation."
            )

        return [
            self.get_pixi_executable(pandrator_path),
            'run',
            '--manifest-path', manifest_path,
            '--executable',
        ] + command

    def run_pixi_in_env(self, pandrator_path, env_name, command, cwd=None, log_errors=True):
        return self.run_command(
            self.build_pixi_run_command(pandrator_path, env_name, command),
            cwd=cwd,
            env=self.get_pixi_subprocess_env(pandrator_path),
            log_errors=log_errors,
        )

    def check_pixi(self, pandrator_path):
        return os.path.exists(self.get_pixi_executable(pandrator_path))

    def install_pixi(self, pandrator_path):
        logging.info("Installing Pixi...")
        self.configure_tls_certificates()
        bin_path = os.path.join(pandrator_path, 'bin')
        os.makedirs(bin_path, exist_ok=True)

        pixi_executable = self.get_pixi_executable(pandrator_path)
        if os.path.exists(pixi_executable):
            logging.info("Pixi is already installed.")
            return

        temp_pixi_path = os.path.join(tempfile.gettempdir(), 'pandrator_pixi.exe')

        try:
            response = requests.get(
                PIXI_DOWNLOAD_URL,
                stream=True,
                timeout=60,
                verify=self.ca_bundle_path if self.ca_bundle_path else True,
            )
            response.raise_for_status()

            with open(temp_pixi_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            shutil.move(temp_pixi_path, pixi_executable)
            self.run_pixi_command(pandrator_path, ['--version'])
            logging.info("Pixi installed successfully.")
        finally:
            if os.path.exists(temp_pixi_path):
                os.remove(temp_pixi_path)

    def update_manifest_python_dependency(self, manifest_path, python_version):
        desired_python_line = f'python = "{python_version}.*"'

        try:
            with open(manifest_path, 'r', encoding='utf-8', errors='replace') as f:
                manifest_contents = f.read()
        except OSError as e:
            logging.warning(f"Could not read manifest for Python migration ({manifest_path}): {str(e)}")
            return False

        python_line_pattern = r'(?mi)^[ \t]*python[ \t]*=[ \t]*"[^"]*"[ \t]*$'
        if re.search(python_line_pattern, manifest_contents):
            updated_contents = re.sub(python_line_pattern, desired_python_line, manifest_contents, count=1)
        elif '[dependencies]' in manifest_contents:
            updated_contents = manifest_contents.replace('[dependencies]', f'[dependencies]\n{desired_python_line}', 1)
        else:
            newline = '' if manifest_contents.endswith('\n') else '\n'
            updated_contents = f"{manifest_contents}{newline}\n[dependencies]\n{desired_python_line}\n"

        if updated_contents == manifest_contents:
            return False

        with open(manifest_path, 'w', encoding='utf-8') as f:
            f.write(updated_contents)

        logging.info(f"Updated Python dependency for {manifest_path} to {python_version}.*")
        return True

    def ensure_pixi_manifest(self, pandrator_path, env_name, python_version):
        env_dir = self.get_pixi_env_dir(pandrator_path, env_name)
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)

        os.makedirs(env_dir, exist_ok=True)

        if not os.path.exists(manifest_path):
            manifest_contents = (
                "[workspace]\n"
                f"name = \"{env_name}\"\n"
                "channels = [\"conda-forge\"]\n"
                "platforms = [\"win-64\"]\n\n"
                "[dependencies]\n"
                f"python = \"{python_version}.*\"\n"
                "pip = \"*\"\n"
            )

            with open(manifest_path, 'w', encoding='utf-8') as f:
                f.write(manifest_contents)
        else:
            self.update_manifest_python_dependency(manifest_path, python_version)

        return manifest_path

    def get_env_python_version(self, pandrator_path, env_name):
        stdout, _ = self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-c', 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'],
            log_errors=False,
        )

        python_version = stdout.strip().splitlines()[-1] if stdout.strip() else ''
        if not python_version:
            raise RuntimeError(f"Could not detect Python version in {env_name}")

        return python_version

    def get_rvc_fairseq_wheel_url(self, python_version):
        wheel_url = RVC_FAIRSEQ_WHEEL_URL_BY_PYTHON.get(python_version)
        if wheel_url:
            return wheel_url

        raise RuntimeError(
            f"No fairseq wheel URL configured for Python {python_version}. "
            "Supported versions are 3.10 and 3.11."
        )

    def create_pixi_env(self, pandrator_path, env_name, python_version):
        logging.info(f"Creating pixi environment {env_name}...")
        manifest_path = self.ensure_pixi_manifest(pandrator_path, env_name, python_version)

        try:
            self.run_pixi_command(
                pandrator_path,
                ['install', '--manifest-path', manifest_path],
                cwd=self.get_pixi_env_dir(pandrator_path, env_name)
            )
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to create or set up pixi environment {env_name}")
            logging.error(f"Error output: {e.stderr}")
            raise

    def add_pixi_conda_package(self, pandrator_path, env_name, package_spec):
        logging.info(f"Adding {package_spec} to {env_name} via pixi...")
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        package_name, separator, package_version = package_spec.partition('=')
        package_name = package_name.strip()
        package_version = package_version.strip() if separator else None

        if os.path.exists(manifest_path):
            with open(manifest_path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if line.strip().startswith(f'{package_name} ='):
                        if package_version is None or f'"{package_version}"' in line:
                            logging.info(f"{package_name} is already present in {env_name}, skipping pixi add.")
                            return
                        logging.info(f"{package_name} is present with a different version in {env_name}, updating it.")
                        break

        self.run_pixi_command(
            pandrator_path,
            ['add', '--manifest-path', manifest_path, package_spec],
            cwd=self.get_pixi_env_dir(pandrator_path, env_name)
        )

    def get_installer_state_path(self, pandrator_path):
        return os.path.join(pandrator_path, INSTALLER_STATE_FILENAME)

    def load_installer_state(self, pandrator_path):
        state_path = self.get_installer_state_path(pandrator_path)
        default_state = {'requirements_hashes': {}}

        if not os.path.exists(state_path):
            return default_state

        try:
            with open(state_path, 'r', encoding='utf-8', errors='replace') as f:
                state = json.load(f)

            if not isinstance(state, dict):
                raise ValueError("installer state root must be a dictionary")
        except Exception as e:
            logging.warning(f"Failed to load installer state from {state_path}: {str(e)}")
            return default_state

        requirements_hashes = state.get('requirements_hashes')
        if not isinstance(requirements_hashes, dict):
            state['requirements_hashes'] = {}

        return state

    def save_installer_state(self, pandrator_path, state):
        state_path = self.get_installer_state_path(pandrator_path)
        os.makedirs(pandrator_path, exist_ok=True)

        serializable_state = state if isinstance(state, dict) else {'requirements_hashes': {}}
        if not isinstance(serializable_state.get('requirements_hashes'), dict):
            serializable_state['requirements_hashes'] = {}

        try:
            with open(state_path, 'w', encoding='utf-8') as f:
                json.dump(serializable_state, f, indent=2, sort_keys=True)
        except Exception as e:
            logging.warning(f"Failed to save installer state to {state_path}: {str(e)}")

    def calculate_file_sha256(self, file_path):
        digest = hashlib.sha256()
        with open(file_path, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    def build_requirements_state_key(self, pandrator_path, env_name, requirements_file):
        try:
            relative_path = os.path.relpath(requirements_file, pandrator_path)
        except ValueError:
            relative_path = os.path.abspath(requirements_file)

        normalized_relative_path = relative_path.replace('\\', '/')
        return f"{env_name}:{normalized_relative_path}"

    def record_requirements_hash(self, pandrator_path, env_name, requirements_file):
        if not os.path.exists(requirements_file):
            return

        state = self.load_installer_state(pandrator_path)
        requirements_hashes = state.setdefault('requirements_hashes', {})
        state_key = self.build_requirements_state_key(pandrator_path, env_name, requirements_file)
        requirements_hashes[state_key] = self.calculate_file_sha256(requirements_file)
        self.save_installer_state(pandrator_path, state)

    def normalize_package_name(self, package_name):
        return package_name.strip().lower().replace('_', '-').replace('.', '-')

    def parse_package_spec(self, package_spec):
        spec = package_spec.split(';', 1)[0].strip()
        if not spec:
            return '', None, ''

        if '@' in spec:
            package_before_at = spec.split('@', 1)[0].strip()
            if package_before_at:
                spec = package_before_at

        package_name = spec
        comparator = None
        version = ''

        for candidate_comparator in ('===', '==', '>=', '<=', '~=', '!=', '>', '<', '='):
            if candidate_comparator in spec:
                package_name, version = spec.split(candidate_comparator, 1)
                package_name = package_name.strip()
                version = version.strip()
                comparator = candidate_comparator
                break

        package_name = package_name.split('[', 1)[0].strip()
        return package_name, comparator, version

    def get_installed_pip_packages(self, pandrator_path, env_name):
        try:
            stdout, _ = self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'freeze'],
                log_errors=False,
            )
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Failed to inspect pip packages in {env_name}; package checks will require reinstall. STDERR: {e.stderr}"
            )
            return None

        installed_packages = {}
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or line.startswith('-e '):
                continue

            if ' @ ' in line:
                package_name = line.split(' @ ', 1)[0].strip()
                if package_name:
                    installed_packages[self.normalize_package_name(package_name)] = None
                continue

            if '==' not in line:
                continue

            package_name, version = line.split('==', 1)
            installed_packages[self.normalize_package_name(package_name)] = version.strip()

        return installed_packages

    def get_installed_pip_freeze_entries(self, pandrator_path, env_name):
        try:
            stdout, _ = self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'freeze'],
                log_errors=False,
            )
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Failed to inspect pip freeze entries in {env_name}; source checks will require reinstall. STDERR: {e.stderr}"
            )
            return None

        freeze_entries = {}
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line or line.startswith('#') or line.startswith('-e '):
                continue

            package_name = ''
            if ' @ ' in line:
                package_name = line.split(' @ ', 1)[0].strip()
            elif '==' in line:
                package_name = line.split('==', 1)[0].strip()

            if package_name:
                freeze_entries[self.normalize_package_name(package_name)] = line

        return freeze_entries

    def find_unsatisfied_package_specs(self, package_specs, installed_packages):
        if installed_packages is None:
            return list(package_specs)

        unsatisfied_specs = []
        for package_spec in package_specs:
            package_name, comparator, expected_version = self.parse_package_spec(package_spec)
            if not package_name:
                continue

            normalized_package_name = self.normalize_package_name(package_name)
            if normalized_package_name not in installed_packages:
                unsatisfied_specs.append(package_spec)
                continue

            installed_version = installed_packages.get(normalized_package_name)
            if comparator in ('==', '===', '=') and expected_version:
                if installed_version != expected_version:
                    unsatisfied_specs.append(package_spec)

        return unsatisfied_specs

    def format_package_specs(self, package_specs, max_items=5):
        if not package_specs:
            return ''

        preview = ', '.join(package_specs[:max_items])
        if len(package_specs) > max_items:
            preview += ', ...'
        return preview

    def should_install_requirements(self, pandrator_path, env_name, requirements_file):
        if not os.path.exists(requirements_file):
            raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        if not os.path.exists(manifest_path):
            return True, "Pixi manifest is missing"

        state = self.load_installer_state(pandrator_path)
        requirements_hashes = state.setdefault('requirements_hashes', {})
        state_key = self.build_requirements_state_key(pandrator_path, env_name, requirements_file)
        current_hash = self.calculate_file_sha256(requirements_file)
        previous_hash = requirements_hashes.get(state_key)

        _, requirement_specs, unsupported_lines = self.load_pypi_requirements(requirements_file)
        has_non_exact_constraints = any(
            self.parse_package_spec(requirement_spec)[1] not in (None, '==', '===', '=')
            for requirement_spec in requirement_specs
        )
        installed_packages = self.get_installed_pip_packages(pandrator_path, env_name)
        unsatisfied_specs = self.find_unsatisfied_package_specs(requirement_specs, installed_packages)

        if unsatisfied_specs:
            return True, (
                "missing or mismatched packages "
                f"({self.format_package_specs(unsatisfied_specs)})"
            )

        if previous_hash == current_hash:
            return False, "requirements unchanged and package checks passed"

        if unsupported_lines:
            return True, "requirements changed and include entries that require pip -r"

        if has_non_exact_constraints:
            return True, "requirements changed and include non-exact version constraints"

        requirements_hashes[state_key] = current_hash
        self.save_installer_state(pandrator_path, state)
        return False, "requirements changed but package checks passed"

    def component_needs_package_sync(self, pandrator_path, env_name, package_specs):
        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        if not os.path.exists(manifest_path):
            return True, "Pixi manifest is missing"

        installed_packages = self.get_installed_pip_packages(pandrator_path, env_name)
        unsatisfied_specs = self.find_unsatisfied_package_specs(package_specs, installed_packages)
        if unsatisfied_specs:
            return True, (
                "missing or mismatched packages "
                f"({self.format_package_specs(unsatisfied_specs)})"
            )

        return False, "package checks passed"

    def package_source_matches(self, pandrator_path, env_name, package_name, expected_source_fragment):
        freeze_entries = self.get_installed_pip_freeze_entries(pandrator_path, env_name)
        if freeze_entries is None:
            return False, "pip freeze inspection failed"

        normalized_package_name = self.normalize_package_name(package_name)
        freeze_entry = freeze_entries.get(normalized_package_name)
        if not freeze_entry:
            return False, f"{package_name} is not installed"

        if ' @ ' not in freeze_entry:
            return False, f"{package_name} is not installed from an explicit source"

        if expected_source_fragment.lower() not in freeze_entry.lower():
            return False, f"{package_name} is installed from a different source"

        return True, "source check passed"

    def rvc_needs_package_sync(self, pandrator_path, env_name):
        needs_sync, reason = self.component_needs_package_sync(
            pandrator_path,
            env_name,
            RVC_REQUIRED_PACKAGE_SPECS,
        )
        if needs_sync:
            return True, reason

        source_ok, source_reason = self.package_source_matches(
            pandrator_path,
            env_name,
            'rvc-python',
            RVC_PYTHON_FORK_SOURCE_FRAGMENT,
        )
        if not source_ok:
            return True, source_reason

        return False, "package and source checks passed"

    def extract_import_candidates(self, requirements_file):
        candidates = []
        seen = set()
        import_aliases = {
            'google-genai': 'google.genai',
            'pymupdf': 'fitz',
            'ffmpeg-python': 'ffmpeg',
            'beautifulsoup4': 'bs4',
            'pillow': 'PIL',
        }

        with open(requirements_file, 'r', encoding='utf-8-sig', errors='replace') as f:
            for raw_line in f:
                line = raw_line.split('#', 1)[0].strip()
                if not line or line.startswith(('-', 'git+', 'http://', 'https://', '.', '/')):
                    continue

                requirement = line.split(';', 1)[0].strip()
                requirement = requirement.split('@', 1)[0].strip()

                package_name = requirement
                for separator in ('[', '==', '>=', '<=', '~=', '!=', '>', '<', '='):
                    package_name = package_name.split(separator, 1)[0].strip()

                normalized_package_name = package_name.lower().replace('_', '-').replace('.', '-')
                import_name = import_aliases.get(normalized_package_name, package_name.replace('-', '_'))
                if import_name and import_name not in seen:
                    seen.add(import_name)
                    candidates.append(import_name)

        return candidates

    def load_pypi_requirements(self, requirements_file):
        requirement_specs = []
        unsupported_lines = []

        with open(requirements_file, 'rb') as f:
            requirements_text = f.read().decode('utf-8-sig', errors='replace')

        for raw_line in requirements_text.splitlines():
            line = raw_line.split('#', 1)[0].strip()
            if not line:
                continue

            lower_line = line.lower()
            has_direct_reference = ' @ ' in line and any(
                marker in lower_line for marker in ('git+', 'http://', 'https://', 'file://')
            )

            if line.startswith(('-', 'git+', 'http://', 'https://', 'file://', '.', '/')) or has_direct_reference:
                unsupported_lines.append(line)
                continue

            requirement_specs.append(line)

        return requirements_text, requirement_specs, unsupported_lines

    def add_pypi_requirements(self, pandrator_path, env_name, requirement_specs):
        if not requirement_specs:
            return []

        manifest_path = self.get_pixi_manifest_path(pandrator_path, env_name)
        env_dir = self.get_pixi_env_dir(pandrator_path, env_name)

        try:
            self.run_pixi_command(
                pandrator_path,
                ['add', '--manifest-path', manifest_path, '--pypi'] + requirement_specs,
                cwd=env_dir,
                log_errors=False,
            )
            return []
        except subprocess.CalledProcessError as e:
            logging.warning(
                f"Bulk pixi add failed for {env_name}; retrying requirements one-by-one. STDERR: {e.stderr}"
            )

        failed_specs = []
        for requirement_spec in requirement_specs:
            try:
                self.run_pixi_command(
                    pandrator_path,
                    ['add', '--manifest-path', manifest_path, '--pypi', requirement_spec],
                    cwd=env_dir,
                    log_errors=False,
                )
            except subprocess.CalledProcessError as e:
                failed_specs.append(requirement_spec)
                logging.warning(
                    f"pixi add failed for '{requirement_spec}' in {env_name}. "
                    f"Will try pip fallback for this requirement. STDERR: {e.stderr}"
                )

        return failed_specs

    def install_requirement_specs_with_pip(self, pandrator_path, env_name, requirement_specs):
        for requirement_spec in requirement_specs:
            logging.info(f"Installing requirement via pip fallback in {env_name}: {requirement_spec}")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install', requirement_spec]
            )

    def ensure_pyqt6_runtime(self, pandrator_path, env_name):
        if env_name != 'pandrator_installer':
            return

        check_command = ['python', '-c', 'from PyQt6.QtWidgets import QApplication']
        logging.info("Checking PyQt6 QtWidgets runtime in pandrator_installer...")

        try:
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                check_command,
                log_errors=False,
            )
            logging.info("PyQt6 runtime check passed.")
            return
        except subprocess.CalledProcessError as e:
            logging.warning(
                "PyQt6 runtime check failed in pandrator_installer. "
                f"Reinstalling {PYQT6_RUNTIME_PIN}. STDERR: {e.stderr}"
            )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-m', 'pip', 'install', '--force-reinstall', PYQT6_RUNTIME_PIN]
        )

        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            check_command,
            log_errors=False,
        )
        logging.info(f"PyQt6 runtime repaired successfully using {PYQT6_RUNTIME_PIN}.")

    def try_import_requirements(self, pandrator_path, env_name, requirements_file):
        logging.info(f"Running best-effort import checks for {requirements_file}...")

        for import_name in self.extract_import_candidates(requirements_file):
            try:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-c', f'import importlib; importlib.import_module("{import_name}")'],
                    log_errors=False,
                )
            except subprocess.CalledProcessError as e:
                logging.warning(
                    f"Import check failed for {import_name} after installing {requirements_file}. "
                    f"This is best-effort only and may be expected on Windows. STDERR: {e.stderr}"
                )

    def install_requirements(self, pandrator_path, env_name, requirements_file):
        logging.info(f"Installing requirements for {env_name}...")

        requirements_text, requirement_specs, unsupported_lines = self.load_pypi_requirements(requirements_file)
        logging.info(f"Requirements file contents:\n{requirements_text}")

        failed_pixi_specs = []

        if requirement_specs:
            failed_pixi_specs = self.add_pypi_requirements(pandrator_path, env_name, requirement_specs)
        else:
            logging.info(f"No installable requirements found in {requirements_file}")

        if failed_pixi_specs:
            logging.warning(
                f"Falling back to pip install for requirements that pixi could not add in {env_name}: "
                f"{failed_pixi_specs}"
            )
            self.install_requirement_specs_with_pip(pandrator_path, env_name, failed_pixi_specs)

        if unsupported_lines:
            logging.warning(
                "Unsupported requirement lines for pixi add detected; "
                f"falling back to pip install -r for {requirements_file}: {unsupported_lines}"
            )
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install', '-r', requirements_file]
            )

        self.ensure_pyqt6_runtime(pandrator_path, env_name)

        self.try_import_requirements(pandrator_path, env_name, requirements_file)

        if env_name == 'pandrator_installer':
            logging.info("Checking if dulwich is installed in pandrator_installer environment...")
            try:
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-c', 'import dulwich; print(f"Dulwich version {dulwich.__version__} is installed")'],
                    log_errors=False,
                )
                logging.info("Dulwich check completed successfully")
            except subprocess.CalledProcessError:
                logging.warning("Dulwich not found in pandrator_installer environment, installing separately...")
                try:
                    self.run_pixi_in_env(
                        pandrator_path,
                        env_name,
                        ['python', '-m', 'pip', 'install', 'dulwich']
                    )
                    logging.info("Dulwich installed successfully in pandrator_installer environment")
                except subprocess.CalledProcessError as e:
                    logging.error(f"Failed to install dulwich in pandrator_installer environment: {str(e)}")
                    raise

        self.record_requirements_hash(pandrator_path, env_name, requirements_file)

    def install_package(self, pandrator_path, env_name, package):
        logging.info(f"Installing {package} in {env_name}...")
        self.run_pixi_in_env(
            pandrator_path,
            env_name,
            ['python', '-m', 'pip', 'install', package]
        )

    def build_xtts_launcher_command(self, use_cpu=False, pixi_path=None):
        command = ['cmd', '/c', 'run.bat']
        if use_cpu:
            command.append('--cpu')
        else:
            command.extend(['--backend', 'cuda'])

        if pixi_path:
            command.extend(['--pixi-path', pixi_path])

        return command

    def _read_text_if_exists(self, file_path):
        if not os.path.exists(file_path):
            return ""
        try:
            with open(file_path, 'r', encoding='utf-8', errors='replace') as handle:
                return handle.read()
        except OSError:
            return ""

    def _read_log_tail_if_exists(self, file_path, max_lines=40):
        content = self._read_text_if_exists(file_path)
        if not content:
            return ""

        lines = content.splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)

        return "\n".join(lines[-max_lines:])

    def get_xtts_pixi_argument(self, xtts_repo_path, pixi_path):
        if not pixi_path:
            return None

        run_bat_contents = self._read_text_if_exists(os.path.join(xtts_repo_path, 'run.bat')).lower()
        run_py_contents = self._read_text_if_exists(os.path.join(xtts_repo_path, 'run.py')).lower()
        if '--pixi-path' in run_bat_contents or '--pixi-path' in run_py_contents:
            return pixi_path

        logging.info("XTTS launcher does not advertise --pixi-path, skipping shared Pixi argument.")
        return None

    def terminate_process_tree(self, process, timeout=10):
        if process is None:
            return

        try:
            parent = psutil.Process(process.pid)
        except psutil.NoSuchProcess:
            return

        try:
            for child in parent.children(recursive=True):
                try:
                    child.terminate()
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
            parent.terminate()
            process.wait(timeout=timeout)
            return
        except (psutil.TimeoutExpired, subprocess.TimeoutExpired):
            logging.warning(f"Process tree did not terminate in {timeout}s, forcing kill")
        except psutil.NoSuchProcess:
            return

        try:
            for child in parent.children(recursive=True):
                try:
                    child.kill()
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when killing child process with PID: {child.pid}")
            parent.kill()
        except psutil.NoSuchProcess:
            return

    def is_xtts_runtime_ready(self, xtts_repo_path):
        run_bat_path = os.path.join(xtts_repo_path, 'run.bat')
        env_python_path = os.path.join(xtts_repo_path, '.pixi', 'envs', 'default', 'python.exe')
        return all(os.path.exists(path) for path in (run_bat_path, env_python_path))

    def install_xtts_api_server(self, xtts_repo_path, use_cpu=False, pixi_path=None):
        logging.info(f"Bootstrapping XTTS2 API server in {xtts_repo_path}...")
        logging.info(
            "XTTS bootstrap starts the server temporarily to validate runtime and will stop it after health checks."
        )

        run_script_path = os.path.join(xtts_repo_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"XTTS2 run script not found at: {run_script_path}")

        if self.is_port_in_use(8020):
            raise RuntimeError("XTTS server cannot be bootstrapped because port 8020 is already in use.")

        xtts_install_log_file = os.path.join(xtts_repo_path, 'xtts_install.log')
        command = self.build_xtts_launcher_command(
            use_cpu=use_cpu,
            pixi_path=self.get_xtts_pixi_argument(xtts_repo_path, pixi_path),
        )

        log_handle = open(xtts_install_log_file, 'a', encoding='utf-8')
        process = None
        try:
            process = subprocess.Popen(
                command,
                cwd=xtts_repo_path,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )

            if not self.check_xtts_server_online(
                'http://127.0.0.1:8020',
                max_attempts=360,
                wait_interval=5,
                process=process,
            ):
                return_code = process.poll()
                if return_code is not None:
                    raise RuntimeError(
                        f"XTTS2 bootstrap process exited before server was ready (exit code {return_code}). "
                        f"See log: {xtts_install_log_file}"
                    )
                raise RuntimeError(
                    "XTTS2 bootstrap did not bring the server online in time. "
                    f"See log: {xtts_install_log_file}"
                )
        finally:
            if process is not None:
                logging.info("Stopping temporary XTTS2 bootstrap process.")
                self.terminate_process_tree(process)
            log_handle.close()

    def is_voxtral_runtime_ready(self, voxtral_repo_path):
        venv_python_path = os.path.join(voxtral_repo_path, '.runtime', 'venv', 'Scripts', 'python.exe')
        return os.path.exists(venv_python_path)

    def install_voxtral_api_server(self, voxtral_repo_path):
        logging.info(f"Bootstrapping Voxtral API server in {voxtral_repo_path}...")
        run_script_path = os.path.join(voxtral_repo_path, 'run.ps1')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Voxtral run script not found at: {run_script_path}")

        command = [
            'powershell',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            run_script_path,
            '-ProjectRoot',
            voxtral_repo_path,
            '-NoStart',
            '-Model',
            'gguf',
        ]

        self.run_command(
            command,
            cwd=voxtral_repo_path,
        )

    def replace_files(self, repo_path, file_mappings):
        for src_file, dest_file in file_mappings.items():
            src_path = os.path.join(repo_path, src_file)
            dest_path = os.path.join(repo_path, dest_file)
            try:
                shutil.copy2(src_path, dest_path)
                logging.info(f"Replaced file: {dest_file}")
            except Exception as e:
                logging.error(f"Failed to replace file: {dest_file}")
                logging.error(f"Error message: {str(e)}")
                logging.error(traceback.format_exc())
                raise

    def install_silero_api_server(self, pandrator_path, env_name):
        logging.info(f"Installing Silero API server in {env_name}...")
        try:
            self.install_package(pandrator_path, env_name, 'requests')
            self.install_package(pandrator_path, env_name, 'silero-api-server')
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install Silero API server in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def run_git_command(self, arguments, cwd=None):
        self.configure_tls_certificates()
        git_executable = shutil.which('git')
        if not git_executable:
            raise FileNotFoundError("git.exe was not found on PATH")

        git_command = [git_executable]
        if os.name == 'nt':
            git_command.extend(['-c', 'http.sslBackend=schannel'])

        return self.run_command(
            git_command + arguments,
            cwd=cwd,
            env=self.get_network_subprocess_env()
        )

    def get_dulwich_porcelain(self):
        try:
            from dulwich import porcelain
            return porcelain
        except ImportError as e:
            raise RuntimeError(
                "Git failed and Dulwich is not available in the launcher runtime."
            ) from e

    def clone_repo(self, repo_url, target_dir):
        logging.info(f"Cloning repository {repo_url} to {target_dir}...")
        self.configure_tls_certificates()
        try:
            self.run_git_command(['clone', repo_url, target_dir])
            logging.info("Repository cloned successfully with git.")
        except Exception as git_error:
            logging.warning(f"git clone failed, falling back to Dulwich: {str(git_error)}")
            try:
                porcelain = self.get_dulwich_porcelain()
                porcelain.clone(repo_url, target_dir)
                logging.info("Repository cloned successfully with Dulwich.")
            except Exception as dulwich_error:
                if self.is_certificate_error(dulwich_error):
                    logging.warning(
                        "TLS certificate verification failed during Dulwich clone. "
                        "Retrying after reloading certificate bundle..."
                    )
                    self.configure_tls_certificates(force=True)
                    try:
                        porcelain = self.get_dulwich_porcelain()
                        porcelain.clone(repo_url, target_dir)
                        logging.info("Repository cloned successfully with Dulwich after certificate refresh.")
                        logging.info("Pulling latest changes...")
                        self.pull_repo(target_dir)
                        return
                    except Exception as retry_error:
                        logging.error(f"Failed to clone repository after certificate refresh: {str(retry_error)}")
                        raise RuntimeError(
                            "TLS certificate verification failed while downloading from GitHub. "
                            "Check Windows certificates and proxy TLS settings."
                        ) from retry_error
                logging.error(f"Failed to clone repository: {str(dulwich_error)}")
                raise

        logging.info("Pulling latest changes...")
        self.pull_repo(target_dir)

    def pull_repo(self, repo_path):
        logging.info(f"Pulling updates for repository at {repo_path}...")
        self.configure_tls_certificates()
        try:
            self.run_git_command(['pull', '--ff-only'], cwd=repo_path)
            logging.info("Repository updated successfully with git.")
        except Exception as git_error:
            logging.warning(f"git pull failed, falling back to Dulwich: {str(git_error)}")
            try:
                porcelain = self.get_dulwich_porcelain()
                repo = porcelain.open_repo(repo_path)
                porcelain.pull(repo)
                logging.info("Repository updated successfully with Dulwich.")
            except Exception as dulwich_error:
                if self.is_certificate_error(dulwich_error):
                    logging.warning(
                        "TLS certificate verification failed during Dulwich pull. "
                        "Retrying after reloading certificate bundle..."
                    )
                    self.configure_tls_certificates(force=True)
                    try:
                        porcelain = self.get_dulwich_porcelain()
                        repo = porcelain.open_repo(repo_path)
                        porcelain.pull(repo)
                        logging.info("Repository updated successfully with Dulwich after certificate refresh.")
                        return
                    except Exception as retry_error:
                        logging.error(f"Failed to update repository after certificate refresh: {str(retry_error)}")
                        raise RuntimeError(
                            "TLS certificate verification failed while downloading from GitHub. "
                            "Check Windows certificates and proxy TLS settings."
                        ) from retry_error
                logging.error(f"Failed to update repository: {str(dulwich_error)}")
                raise

    def install_pycroppdf_requirements(self, pandrator_path, env_name, pycroppdf_repo_path):
        logging.info(f"Installing PyCropPDF requirements in {env_name}...")
        try:
            requirements_file = os.path.join(pycroppdf_repo_path, 'requirements.txt')
            self.install_requirements(pandrator_path, env_name, requirements_file)
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install PyCropPDF requirements in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_rvc_python(self, pandrator_path, env_name):
        logging.info("Starting RVC Python installation")
        try:
            logging.info("Installing specific pip version...")
            # Keep the older pip pin here because newer pip versions break this toolchain.
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                ['python', '-m', 'pip', 'install', 'pip==24']
            )

            python_version = self.get_env_python_version(pandrator_path, env_name)
            fairseq_wheel_url = self.get_rvc_fairseq_wheel_url(python_version)

            logging.info(f"Installing RVC Python fork for Python {python_version}...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    '--upgrade', '--force-reinstall',
                    RVC_PYTHON_FORK_INSTALL_SPEC,
                ]
            )

            logging.info("Installing fairseq wheel required by the RVC fork...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    '--upgrade', '--force-reinstall',
                    fairseq_wheel_url,
                ]
            )

            logging.info("Installing PyTorch stack for RVC...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install', '--upgrade',
                    f'torch=={RVC_TORCH_VERSION}',
                    f'torchvision=={RVC_TORCHVISION_VERSION}',
                    f'torchaudio=={RVC_TORCHAUDIO_VERSION}',
                    '--index-url', RVC_TORCH_INDEX_URL,
                ]
            )

            logging.info("Pinning NumPy to a 1.x build for faiss/RVC compatibility...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    '--upgrade', '--force-reinstall',
                    f'numpy=={RVC_NUMPY_VERSION}',
                ]
            )

            logging.info("Verifying RVC runtime imports...")
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-c',
                    (
                        'import numpy as np; '
                        'import fairseq, rvc_python, torch, torchvision, torchaudio; '
                        'assert int(np.__version__.split(".")[0]) < 2, '
                        'f"NumPy must be <2 for RVC compatibility, got {np.__version__}"'
                    ),
                ]
            )

            logging.info("RVC Python installation completed successfully.")

        except Exception as e:
            error_msg = f"An error occurred during RVC Python installation: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            raise

    def install_whisperx(self, pandrator_path, env_name):
        logging.info(f"Installing WhisperX in {env_name}...")
        try:
            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    f'torch=={WHISPERX_TORCH_VERSION}',
                    f'torchvision=={WHISPERX_TORCHVISION_VERSION}',
                    f'torchaudio=={WHISPERX_TORCHAUDIO_VERSION}',
                    '--index-url', WHISPERX_TORCH_INDEX_URL
                ]
            )

            self.add_pixi_conda_package(pandrator_path, env_name, 'cudnn=8.9.7.29')
            self.add_pixi_conda_package(pandrator_path, env_name, 'ffmpeg')

            self.run_pixi_in_env(
                pandrator_path,
                env_name,
                [
                    'python', '-m', 'pip', 'install',
                    f'whisperx=={WHISPERX_VERSION}',
                    f'ctranslate2=={WHISPERX_CTRANSLATE2_VERSION}'
                ]
            )
            
            logging.info("WhisperX installation completed successfully.")
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to install WhisperX in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def install_subdub_requirements(self, pandrator_path, env_name, subdub_repo_path):
        logging.info(f"Installing Subdub package in {env_name}...")
        try:
            if not os.path.exists(subdub_repo_path):
                self.clone_repo(SUBDUB_REPO_URL, subdub_repo_path)

            pyproject_file = os.path.join(subdub_repo_path, 'pyproject.toml')
            if os.path.exists(pyproject_file):
                logging.info("Detected refactored Subdub package layout (pyproject.toml). Installing editable package...")
                self.run_pixi_in_env(
                    pandrator_path,
                    env_name,
                    ['python', '-m', 'pip', 'install', '-e', '.'],
                    cwd=subdub_repo_path,
                )
                return

            requirements_file = os.path.join(subdub_repo_path, 'requirements.txt')
            if not os.path.exists(requirements_file):
                raise FileNotFoundError(
                    f"Subdub dependency manifest not found. Expected either {pyproject_file} or {requirements_file}."
                )

            logging.info("Using legacy Subdub requirements.txt installation path.")
            self.install_requirements(pandrator_path, env_name, requirements_file)
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            logging.error(f"Failed to install Subdub package in {env_name}")
            logging.error(f"Error message: {str(e)}")
            raise

    def set_permissive_permissions(self, path):
        """Set permissive file permissions on installation directories"""
        if not self.is_admin():
            logging.info(f"Skipping permission setting on {path} (not running as admin)")
            return False
            
        try:
            self.update_status(f"Setting permissions on {os.path.basename(path)}...")
            logging.info(f"Setting permissive permissions on: {path}")
            
            # Use icacls to give Users full control (F) with inheritance flags (OI)(CI)
            # OI = Object Inherit, CI = Container Inherit, F = Full Control
            command = f'icacls "{path}" /grant:r Users:(OI)(CI)F /T /Q'
            subprocess.run(command, shell=True, check=True)
            
            logging.info(f"Successfully set permissions on: {path}")
            return True
        except subprocess.CalledProcessError as e:
            logging.error(f"Failed to set permissions on {path}: {str(e)}")
            logging.error(traceback.format_exc())
            return False

    def install_process(self):
        """Main installation process - runs in a worker thread"""
        # Get checkbox states to local variables for thread safety
        pandrator_var = self.pandrator_checkbox.isChecked()
        xtts_var = self.xtts_checkbox.isChecked()
        xtts_cpu_var = self.xtts_cpu_checkbox.isChecked()
        silero_var = self.silero_checkbox.isChecked()
        voxtral_var = self.voxtral_checkbox.isChecked()
        rvc_var = self.rvc_checkbox.isChecked()
        whisperx_var = self.whisperx_checkbox.isChecked()
        xtts_finetuning_var = self.xtts_finetuning_checkbox.isChecked()
        
        pandrator_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_already_installed = os.path.exists(pandrator_path)
        pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
        subdub_repo_path = os.path.join(pandrator_path, 'Subdub')
        xtts_repo_path = os.path.join(pandrator_path, XTTS_API_REPO_DIRNAME)
        voxtral_repo_path = os.path.join(pandrator_path, VOXTRAL_API_REPO_DIRNAME)
        easy_xtts_trainer_path = os.path.join(pandrator_path, 'easy_xtts_trainer')

        pandrator_repo_missing = not os.path.exists(pandrator_repo_path)
        subdub_repo_missing = not os.path.exists(subdub_repo_path)
        pandrator_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_path, 'pandrator_installer'))
        needs_pandrator_environment = pandrator_var or not pandrator_already_installed or pandrator_env_missing
        
        # Check admin status
        is_admin = self.is_admin()
        if not is_admin:
            logging.warning("Running installer without admin privileges - some features may not work correctly")
            
        try:
            self.configure_tls_certificates()

            # Create Pandrator directory if it doesn't exist
            if not pandrator_already_installed:
                os.makedirs(pandrator_path, exist_ok=True)
                if is_admin:
                    self.set_permissive_permissions(pandrator_path)
            
            self.worker.update_progress.emit(0.1)
            self.worker.update_status.emit("Installing Chocolatey...")
            if is_admin:
                self.install_chocolatey()
            else:
                self.worker.update_status.emit("Skipping Chocolatey installation (requires admin)")
                logging.warning("Skipping Chocolatey installation (requires admin)")

            self.worker.update_progress.emit(0.2)
            self.worker.update_status.emit("Installing dependencies...")
            try:
                if is_admin:
                    self.install_dependencies()
                else:
                    self.worker.update_status.emit("Checking for Calibre...")
                    if not self.check_program_installed('calibre'):
                        QTimer.singleShot(0, self.show_calibre_installation_message)
                    else:
                        logging.info("Calibre is already installed.")
            except Exception as e:
                logging.error(f"Error during dependency installation: {str(e)}")
                if is_admin:
                    QTimer.singleShot(0, self.show_calibre_installation_message)

            self.worker.update_progress.emit(0.35)
            self.worker.update_status.emit("Installing Pixi...")
            if not self.check_pixi(pandrator_path):
                self.install_pixi(pandrator_path)
                if is_admin:
                    self.set_permissive_permissions(os.path.join(pandrator_path, 'bin'))

            if not self.check_pixi(pandrator_path):
                self.worker.update_status.emit("Pixi installation failed")
                logging.error("Pixi installation failed")
                return

            shared_pixi_path = self.get_pixi_executable(pandrator_path)
            
            self.worker.update_progress.emit(0.45)
            self.worker.update_status.emit("Cloning repositories...")
            
            if pandrator_var or not pandrator_already_installed or pandrator_repo_missing:
                self.clone_repo(PANDRATOR_REPO_URL, pandrator_repo_path)
            if pandrator_var or not pandrator_already_installed or subdub_repo_missing:
                self.clone_repo(SUBDUB_REPO_URL, subdub_repo_path)

            if (xtts_var or xtts_cpu_var) and not os.path.exists(xtts_repo_path):
                self.clone_repo(XTTS_API_REPO_URL, xtts_repo_path)

            if voxtral_var and not os.path.exists(voxtral_repo_path):
                self.clone_repo(VOXTRAL_API_REPO_URL, voxtral_repo_path)

            if needs_pandrator_environment:
                self.worker.update_progress.emit(0.6)
                self.worker.update_status.emit("Creating Pandrator Pixi environment...")
                self.create_pixi_env(pandrator_path, 'pandrator_installer', PANDRATOR_PYTHON_VERSION)
                self.add_pixi_conda_package(pandrator_path, 'pandrator_installer', 'ffmpeg')

                self.worker.update_progress.emit(0.7)
                self.worker.update_status.emit("Installing Pandrator, Subdub, and PyCropPDF dependencies...")
                self.install_requirements(pandrator_path, 'pandrator_installer', os.path.join(pandrator_repo_path, 'requirements.txt'))
                
                pycroppdf_repo_path = os.path.join(pandrator_repo_path, 'PyCropPDF')
                if not os.path.exists(pycroppdf_repo_path):
                    self.clone_repo(PYCROPPDF_REPO_URL, pycroppdf_repo_path)
                self.install_pycroppdf_requirements(pandrator_path, 'pandrator_installer', pycroppdf_repo_path)
                
                self.install_subdub_requirements(pandrator_path, 'pandrator_installer', subdub_repo_path)

            if xtts_var or xtts_cpu_var:
                self.worker.update_progress.emit(0.8)
                self.worker.update_status.emit("Bootstrapping XTTS2 API server (temporary startup)...")
                self.worker.update_progress.emit(0.9)
                self.install_xtts_api_server(
                    xtts_repo_path,
                    use_cpu=xtts_cpu_var,
                    pixi_path=shared_pixi_path,
                )

            if silero_var:
                self.worker.update_progress.emit(0.8)
                self.worker.update_status.emit("Creating Silero Pixi environment...")
                self.create_pixi_env(pandrator_path, 'silero_api_server_installer', SILERO_PYTHON_VERSION)

                self.worker.update_progress.emit(0.9)
                self.worker.update_status.emit("Installing Silero API server...")
                self.install_silero_api_server(pandrator_path, 'silero_api_server_installer')

            if voxtral_var:
                self.worker.update_progress.emit(0.9)
                self.worker.update_status.emit("Bootstrapping Voxtral API server...")
                self.install_voxtral_api_server(voxtral_repo_path)

            if rvc_var:
                self.worker.update_progress.emit(0.8)
                self.worker.update_status.emit("Installing RVC Python fork...")
                self.install_rvc_python(pandrator_path, 'pandrator_installer')

            if whisperx_var:
                self.worker.update_progress.emit(0.85)
                self.worker.update_status.emit("Creating WhisperX Pixi environment...")
                self.create_pixi_env(pandrator_path, 'whisperx_installer', WHISPERX_PYTHON_VERSION)
                self.worker.update_progress.emit(0.90)
                self.worker.update_status.emit("Installing WhisperX...")
                self.install_whisperx(pandrator_path, 'whisperx_installer')

            if xtts_finetuning_var:
                self.worker.update_progress.emit(0.85)
                self.worker.update_status.emit("Cloning XTTS Fine-tuning repository...")
                self.clone_repo(EASY_XTTS_TRAINER_REPO_URL, easy_xtts_trainer_path)

                self.worker.update_progress.emit(0.90)
                self.worker.update_status.emit("Creating XTTS Fine-tuning Pixi environment...")
                self.create_pixi_env(pandrator_path, 'easy_xtts_trainer', XTTS_FINETUNING_PYTHON_VERSION)

                self.worker.update_progress.emit(0.95)
                self.worker.update_status.emit("Installing XTTS Fine-tuning requirements...")
                self.install_requirements(pandrator_path, 'easy_xtts_trainer', os.path.join(easy_xtts_trainer_path, 'requirements.txt'))

                self.worker.update_status.emit("Installing PyTorch for XTTS Fine-tuning...")
                self.install_pytorch_for_xtts_finetuning(pandrator_path, 'easy_xtts_trainer')

            # Create or update config file
            config_path = os.path.join(pandrator_path, 'config.json')
            if os.path.exists(config_path):
                with open(config_path, 'r') as f:
                    config = json.load(f)
            else:
                config = {}

            # Update config based on what was installed or already exists
            config['cuda_support'] = config.get('cuda_support', False) or xtts_var
            config['xtts_support'] = config.get('xtts_support', False) or xtts_var or xtts_cpu_var
            config['silero_support'] = config.get('silero_support', False) or silero_var
            config['voxtral_support'] = config.get('voxtral_support', False) or voxtral_var
            config['whisperx_support'] = config.get('whisperx_support', False) or whisperx_var
            config['xtts_finetuning_support'] = config.get('xtts_finetuning_support', False) or xtts_finetuning_var
            config['rvc_support'] = config.get('rvc_support', False) or rvc_var

            with open(config_path, 'w') as f:
                json.dump(config, f)

            # Set final permissions if admin
            if is_admin:
                self.worker.update_progress.emit(0.98)
                self.worker.update_status.emit("Finalizing permissions...")
                self.set_permissive_permissions(pandrator_path)

            self.worker.update_progress.emit(1.0)
            self.worker.update_status.emit("Installation complete!")
            logging.info("Installation completed successfully.")

        except Exception as e:
            logging.error(f"Installation failed: {str(e)}")
            logging.error(traceback.format_exc())
            self.worker.update_status.emit("Installation failed. Check the log for details.")
            raise

    def update_pandrator(self):
        """Update Pandrator and components"""
        pandrator_base_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_repo_path = os.path.join(pandrator_base_path, 'Pandrator')
        
        # Check admin status
        is_admin = self.is_admin()
        if not is_admin:
            logging.info("Running update without admin privileges - file permission changes won't be applied")
        
        logging.info(f"Checking for Pandrator at: {pandrator_repo_path}")
        
        if not os.path.exists(pandrator_repo_path):
            error_msg = f"Pandrator directory not found at: {pandrator_repo_path}"
            logging.error(error_msg)
            self.update_status(error_msg)
            QMessageBox.critical(self, "Update Error", error_msg)
            return

        self.disable_buttons()
        self.initialize_logging()
        
        self.update_status("Updating Pandrator and components...")
        logging.info("Starting update process")
        
        # Create worker thread to run the update
        self.worker = Worker(self.update_process)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_update_finished)
        self.worker.error.connect(self.on_update_error)
        self.worker.start()

    def update_process(self):
        """Main update process - runs in a worker thread"""
        pandrator_base_path = os.path.join(self.initial_working_dir, 'Pandrator')
        pandrator_repo_path = os.path.join(pandrator_base_path, 'Pandrator')
        pycroppdf_repo_path = os.path.join(pandrator_repo_path, 'PyCropPDF')
        subdub_repo_path = os.path.join(pandrator_base_path, 'Subdub')
        xtts_repo_path = os.path.join(pandrator_base_path, XTTS_API_REPO_DIRNAME)
        voxtral_repo_path = os.path.join(pandrator_base_path, VOXTRAL_API_REPO_DIRNAME)
        easy_xtts_trainer_path = os.path.join(pandrator_base_path, 'easy_xtts_trainer')
        config_path = os.path.join(pandrator_base_path, 'config.json')

        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8', errors='replace') as f:
                config = json.load(f)
        else:
            config = {}

        pandrator_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_base_path, 'pandrator_installer'))
        silero_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_base_path, 'silero_api_server_installer'))
        whisperx_env_missing = not os.path.exists(self.get_pixi_manifest_path(pandrator_base_path, 'whisperx_installer'))
        
        # Check admin status
        is_admin = self.is_admin()
        
        try:
            self.configure_tls_certificates()

            self.worker.update_status.emit("Installing Pixi...")
            if not self.check_pixi(pandrator_base_path):
                self.install_pixi(pandrator_base_path)
                if is_admin:
                    self.set_permissive_permissions(os.path.join(pandrator_base_path, 'bin'))

            if not self.check_pixi(pandrator_base_path):
                raise FileNotFoundError("Pixi installation failed during update.")

            shared_pixi_path = self.get_pixi_executable(pandrator_base_path)

            # Update Pandrator
            self.worker.update_status.emit("Updating Pandrator repository...")
            logging.info(f"Updating Pandrator in: {pandrator_repo_path}")
            self.pull_repo(pandrator_repo_path)

            self.worker.update_status.emit("Checking Pandrator environment...")
            self.create_pixi_env(pandrator_base_path, 'pandrator_installer', PANDRATOR_PYTHON_VERSION)
            self.add_pixi_conda_package(pandrator_base_path, 'pandrator_installer', 'ffmpeg')

            requirements_file = os.path.join(pandrator_repo_path, 'requirements.txt')
            logging.info(f"Checking requirements from: {requirements_file}")

            if not os.path.exists(requirements_file):
                logging.error(f"Requirements file not found at: {requirements_file}")
                raise FileNotFoundError(f"Requirements file not found: {requirements_file}")

            self.worker.update_status.emit("Checking Pandrator dependencies...")
            needs_pandrator_requirements, pandrator_requirements_reason = self.should_install_requirements(
                pandrator_base_path,
                'pandrator_installer',
                requirements_file,
            )
            if needs_pandrator_requirements:
                self.worker.update_status.emit("Updating Pandrator dependencies...")
                logging.info(f"Installing Pandrator requirements because {pandrator_requirements_reason}")
                self.install_requirements(pandrator_base_path, 'pandrator_installer', requirements_file)
            else:
                logging.info(f"Skipping Pandrator requirements install: {pandrator_requirements_reason}")

            if os.path.exists(pycroppdf_repo_path):
                self.worker.update_status.emit("Updating PyCropPDF repository...")
                logging.info(f"Updating PyCropPDF in: {pycroppdf_repo_path}")
                self.pull_repo(pycroppdf_repo_path)
            else:
                self.worker.update_status.emit("Cloning PyCropPDF repository...")
                self.clone_repo(PYCROPPDF_REPO_URL, pycroppdf_repo_path)

            pycroppdf_requirements_file = os.path.join(pycroppdf_repo_path, 'requirements.txt')
            if os.path.exists(pycroppdf_requirements_file):
                self.worker.update_status.emit("Checking PyCropPDF dependencies...")
                needs_pycroppdf_requirements, pycroppdf_requirements_reason = self.should_install_requirements(
                    pandrator_base_path,
                    'pandrator_installer',
                    pycroppdf_requirements_file,
                )
                if needs_pycroppdf_requirements:
                    self.worker.update_status.emit("Updating PyCropPDF dependencies...")
                    logging.info(f"Installing PyCropPDF requirements because {pycroppdf_requirements_reason}")
                    self.install_pycroppdf_requirements(
                        pandrator_base_path,
                        'pandrator_installer',
                        pycroppdf_repo_path,
                    )
                else:
                    logging.info(f"Skipping PyCropPDF requirements install: {pycroppdf_requirements_reason}")
            else:
                logging.warning(f"PyCropPDF requirements file not found at: {pycroppdf_requirements_file}")

            # Update Subdub
            if os.path.exists(subdub_repo_path):
                self.worker.update_status.emit("Updating Subdub repository...")
                logging.info(f"Updating Subdub in: {subdub_repo_path}")
                self.pull_repo(subdub_repo_path)
            else:
                self.worker.update_status.emit("Cloning Subdub repository...")
                self.clone_repo(SUBDUB_REPO_URL, subdub_repo_path)

            self.worker.update_status.emit("Checking Subdub dependencies...")
            self.install_subdub_requirements(pandrator_base_path, 'pandrator_installer', subdub_repo_path)

            if config.get('rvc_support', False):
                rvc_needs_install = pandrator_env_missing
                rvc_reason = "Pixi manifest is missing"
                if not rvc_needs_install:
                    rvc_needs_install, rvc_reason = self.rvc_needs_package_sync(
                        pandrator_base_path,
                        'pandrator_installer',
                    )

                if rvc_needs_install:
                    self.worker.update_status.emit("Installing/upgrading RVC fork dependencies...")
                    logging.info(f"Installing RVC packages because {rvc_reason}")
                    self.install_rvc_python(pandrator_base_path, 'pandrator_installer')
                else:
                    logging.info(f"Skipping RVC reinstall: {rvc_reason}")

            if config.get('xtts_support', False):
                if os.path.exists(xtts_repo_path):
                    self.worker.update_status.emit("Updating XTTS2 API server repository...")
                    self.pull_repo(xtts_repo_path)
                else:
                    self.worker.update_status.emit("Cloning XTTS2 API server repository...")
                    self.clone_repo(XTTS_API_REPO_URL, xtts_repo_path)

                if not self.is_xtts_runtime_ready(xtts_repo_path):
                    self.worker.update_status.emit("Bootstrapping XTTS2 API server (temporary startup)...")
                    self.install_xtts_api_server(
                        xtts_repo_path,
                        use_cpu=not config.get('cuda_support', False),
                        pixi_path=shared_pixi_path,
                    )

            if config.get('silero_support', False):
                silero_needs_install = silero_env_missing
                silero_reason = "Pixi manifest is missing"
                if not silero_needs_install:
                    silero_needs_install, silero_reason = self.component_needs_package_sync(
                        pandrator_base_path,
                        'silero_api_server_installer',
                        SILERO_REQUIRED_PACKAGE_SPECS,
                    )

                if silero_needs_install:
                    self.worker.update_status.emit("Installing/upgrading Silero dependencies...")
                    logging.info(f"Installing Silero packages because {silero_reason}")
                    self.create_pixi_env(pandrator_base_path, 'silero_api_server_installer', SILERO_PYTHON_VERSION)
                    self.install_silero_api_server(pandrator_base_path, 'silero_api_server_installer')
                else:
                    logging.info(f"Skipping Silero reinstall: {silero_reason}")

            if config.get('voxtral_support', False):
                if os.path.exists(voxtral_repo_path):
                    self.worker.update_status.emit("Updating Voxtral API server repository...")
                    self.pull_repo(voxtral_repo_path)
                else:
                    self.worker.update_status.emit("Cloning Voxtral API server repository...")
                    self.clone_repo(VOXTRAL_API_REPO_URL, voxtral_repo_path)

                if not self.is_voxtral_runtime_ready(voxtral_repo_path):
                    self.worker.update_status.emit("Bootstrapping Voxtral API server...")
                    self.install_voxtral_api_server(voxtral_repo_path)

            if config.get('whisperx_support', False):
                whisperx_needs_install = whisperx_env_missing
                whisperx_reason = "Pixi manifest is missing"
                if not whisperx_needs_install:
                    whisperx_needs_install, whisperx_reason = self.component_needs_package_sync(
                        pandrator_base_path,
                        'whisperx_installer',
                        WHISPERX_REQUIRED_PACKAGE_SPECS,
                    )

                if whisperx_needs_install:
                    self.worker.update_status.emit("Installing/upgrading WhisperX dependencies...")
                    logging.info(f"Installing WhisperX packages because {whisperx_reason}")
                    self.create_pixi_env(pandrator_base_path, 'whisperx_installer', WHISPERX_PYTHON_VERSION)
                    self.install_whisperx(pandrator_base_path, 'whisperx_installer')
                else:
                    logging.info(f"Skipping WhisperX reinstall: {whisperx_reason}")
            
            # Update easy XTTS trainer (repo and requirements)
            if os.path.exists(easy_xtts_trainer_path):
                self.worker.update_status.emit("Updating easy XTTS trainer...")
                logging.info(f"Updating easy XTTS trainer in: {easy_xtts_trainer_path}")
                self.pull_repo(easy_xtts_trainer_path)

                xtts_requirements_file = os.path.join(easy_xtts_trainer_path, 'requirements.txt')
                if os.path.exists(xtts_requirements_file):
                    self.create_pixi_env(pandrator_base_path, 'easy_xtts_trainer', XTTS_FINETUNING_PYTHON_VERSION)

                    self.worker.update_status.emit("Checking easy XTTS trainer dependencies...")
                    needs_easy_xtts_requirements, easy_xtts_requirements_reason = self.should_install_requirements(
                        pandrator_base_path,
                        'easy_xtts_trainer',
                        xtts_requirements_file,
                    )
                    if needs_easy_xtts_requirements:
                        self.worker.update_status.emit("Updating easy XTTS trainer dependencies...")
                        logging.info(
                            "Installing easy XTTS trainer requirements because %s",
                            easy_xtts_requirements_reason,
                        )
                        self.install_requirements(
                            pandrator_base_path,
                            'easy_xtts_trainer',
                            xtts_requirements_file,
                        )
                    else:
                        logging.info(
                            "Skipping easy XTTS trainer requirements install: %s",
                            easy_xtts_requirements_reason,
                        )

                    needs_xtts_torch, xtts_torch_reason = self.component_needs_package_sync(
                        pandrator_base_path,
                        'easy_xtts_trainer',
                        XTTS_FINETUNING_TORCH_PACKAGE_SPECS,
                    )
                    if needs_xtts_torch:
                        self.worker.update_status.emit("Updating XTTS fine-tuning PyTorch packages...")
                        logging.info(f"Installing XTTS fine-tuning torch packages because {xtts_torch_reason}")
                        self.install_pytorch_for_xtts_finetuning(pandrator_base_path, 'easy_xtts_trainer')
                    else:
                        logging.info(f"Skipping XTTS fine-tuning torch reinstall: {xtts_torch_reason}")
                else:
                    logging.warning(f"XTTS trainer requirements file not found at: {xtts_requirements_file}")
            elif config.get('xtts_finetuning_support', False):
                self.worker.update_status.emit("Migrating easy XTTS trainer to Pixi...")
                self.clone_repo(EASY_XTTS_TRAINER_REPO_URL, easy_xtts_trainer_path)
                self.create_pixi_env(pandrator_base_path, 'easy_xtts_trainer', XTTS_FINETUNING_PYTHON_VERSION)
                xtts_requirements_file = os.path.join(easy_xtts_trainer_path, 'requirements.txt')
                self.install_requirements(pandrator_base_path, 'easy_xtts_trainer', xtts_requirements_file)
                self.install_pytorch_for_xtts_finetuning(pandrator_base_path, 'easy_xtts_trainer')
            else:
                logging.info("easy XTTS trainer not installed, skipping update.")

            # Set permissions if running as admin
            if is_admin:
                self.worker.update_status.emit("Setting permissions after update...")
                self.set_permissive_permissions(pandrator_base_path)
            
            self.worker.update_status.emit("Update completed successfully!")
            logging.info("Update process completed successfully")
        
        except Exception as e:
            error_msg = f"Failed to update: {str(e)}"
            logging.error(error_msg)
            logging.error(traceback.format_exc())
            self.worker.update_status.emit(f"Update failed: {error_msg}")
            raise

    def on_update_finished(self):
        """Handle completion of update process"""
        self.update_status("Update complete!")
        self.enable_buttons()
        QMessageBox.information(self, "Success", "Update completed successfully!")

    def on_update_error(self, error_message):
        """Handle update errors"""
        self.update_status(f"Update failed: {error_message}")
        self.enable_buttons()
        QMessageBox.critical(self, "Update Error", f"Update failed:\n\n{error_message}\n\nCheck the log for more details.")

    # Launch methods
    def launch_apps(self):
        """Launch the selected applications"""
        self.initialize_logging()

        # Get checkbox states
        self.launch_pandrator_var = self.launch_pandrator_checkbox.isChecked()
        self.launch_xtts_var = self.launch_xtts_checkbox.isChecked()
        self.disable_deepspeed_var = self.deepspeed_checkbox.isChecked()
        self.xtts_cpu_launch_var = self.xtts_cpu_launch_checkbox.isChecked()
        self.launch_voxtral_var = self.launch_voxtral_checkbox.isChecked()
        self.launch_silero_var = self.launch_silero_checkbox.isChecked()
        
        # Create worker thread to run the launch process
        self.worker = Worker(self.launch_process)
        self.worker.update_progress.connect(self.update_progress)
        self.worker.update_status.connect(self.update_status)
        self.worker.finished.connect(self.on_launch_finished)
        self.worker.error.connect(self.on_launch_error)
        self.worker.start()

    def launch_process(self):
        """Main launch process - runs in a worker thread"""
        base_path = os.path.abspath(self.initial_working_dir)
        pandrator_path = os.path.join(base_path, 'Pandrator')

        self.worker.update_progress.emit(0.3)
        self.worker.update_status.emit("Preparing to launch...")
        logging.info(f"Launch process started. Base directory: {base_path}")
        logging.info(f"Pandrator path: {pandrator_path}")
        logging.info(f"Pixi path: {self.get_pixi_executable(pandrator_path)}")

        if not self.check_pixi(pandrator_path):
            raise FileNotFoundError(
                "Pixi runtime not found. Run Install or Update to migrate this installation."
            )

        shared_pixi_path = self.get_pixi_executable(pandrator_path)

        pandrator_args = []
        tts_engine_launched = False

        if self.launch_xtts_var:
            self.worker.update_progress.emit(0.4)
            self.worker.update_status.emit("Starting XTTS server...")
            xtts_server_path = os.path.join(pandrator_path, XTTS_API_REPO_DIRNAME)
            logging.info(f"XTTS server path: {xtts_server_path}")
            
            if not os.path.exists(xtts_server_path):
                error_msg = f"XTTS server path not found: {xtts_server_path}"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                raise FileNotFoundError(error_msg)
            
            try:
                use_cpu = self.xtts_cpu_launch_var
                xtts_process = self.run_xtts_api_server(
                    xtts_server_path,
                    use_cpu,
                    pixi_path=shared_pixi_path,
                )
            except Exception as e:
                error_msg = f"Failed to start XTTS server: {str(e)}"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                raise

            self.xtts_process = xtts_process
            
            xtts_server_url = 'http://127.0.0.1:8020'
            if not self.check_xtts_server_online(xtts_server_url, process=xtts_process):
                error_msg = "XTTS server failed to come online"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                self.shutdown_xtts()
                raise RuntimeError(error_msg)
            
            pandrator_args = ['-connect', '-xtts']
            tts_engine_launched = True

        if self.launch_voxtral_var and not tts_engine_launched:
            self.worker.update_progress.emit(0.55)
            self.worker.update_status.emit("Starting Voxtral server...")
            voxtral_server_path = os.path.join(pandrator_path, VOXTRAL_API_REPO_DIRNAME)
            logging.info(f"Voxtral server path: {voxtral_server_path}")

            if not os.path.exists(voxtral_server_path):
                error_msg = f"Voxtral server path not found: {voxtral_server_path}"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                raise FileNotFoundError(error_msg)

            try:
                self.voxtral_process = self.run_voxtral_api_server(voxtral_server_path)
            except Exception as e:
                error_msg = f"Failed to start Voxtral server: {str(e)}"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                raise

            voxtral_server_url = 'http://127.0.0.1:8000/health'
            if not self.check_voxtral_server_online(voxtral_server_url):
                error_msg = "Voxtral server failed to come online"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                self.shutdown_voxtral()
                raise RuntimeError(error_msg)

            pandrator_args = ['-connect', '-voxtral']
            tts_engine_launched = True

        if self.launch_silero_var and not tts_engine_launched:
            self.worker.update_progress.emit(0.6)
            self.worker.update_status.emit("Starting Silero server...")
            
            try:
                self.silero_process = self.run_silero_api_server(pandrator_path, 'silero_api_server_installer')
            except Exception as e:
                error_msg = f"Failed to start Silero server: {str(e)}"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                raise
            
            silero_server_url = 'http://127.0.0.1:8001/docs'
            if not self.check_silero_server_online(silero_server_url):
                error_msg = "Silero server failed to come online"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                self.shutdown_silero()
                raise RuntimeError(error_msg)
            
            pandrator_args = ['-connect', '-silero']
            tts_engine_launched = True

        if self.launch_pandrator_var:
            self.worker.update_progress.emit(0.9)
            self.worker.update_status.emit("Starting Pandrator...")
            pandrator_repo_path = os.path.join(pandrator_path, 'Pandrator')
            pandrator_script_candidates = [
                os.path.join(pandrator_repo_path, 'main.py'),
                os.path.join(pandrator_repo_path, 'pandrator.py'),
            ]
            pandrator_script_path = next(
                (candidate for candidate in pandrator_script_candidates if os.path.exists(candidate)),
                '',
            )

            if pandrator_script_path:
                logging.info(f"Pandrator script path: {pandrator_script_path}")
            else:
                logging.error(
                    "Pandrator script not found. Checked candidates: %s",
                    ", ".join(pandrator_script_candidates),
                )
                error_msg = (
                    "Pandrator script not found. Checked: "
                    + ", ".join(pandrator_script_candidates)
                )
                self.worker.update_status.emit(error_msg)
                raise FileNotFoundError(error_msg)

            try:
                self.pandrator_process = self.run_script(pandrator_path, 'pandrator_installer', pandrator_script_path, pandrator_args)
                self.ensure_process_started(
                    self.pandrator_process,
                    'Pandrator',
                    getattr(self.pandrator_process, 'log_file_path', ''),
                )
            except Exception as e:
                error_msg = f"Failed to start Pandrator: {str(e)}"
                self.worker.update_status.emit(error_msg)
                logging.error(error_msg)
                logging.exception("Exception details:")
                raise

        self.worker.update_progress.emit(1.0)
        self.worker.update_status.emit("Apps are running!")

    def on_launch_finished(self):
        """Handle successful launch"""
        self.update_status("Applications launched successfully")
        self.enable_buttons()
        # Start process monitoring
        QTimer.singleShot(5000, self.check_processes_status)

    def on_launch_error(self, error_message):
        """Handle launch errors"""
        self.update_status(f"Launch failed: {error_message}")
        self.enable_buttons()
        QMessageBox.critical(self, "Launch Error", f"Failed to launch applications:\n\n{error_message}\n\nCheck the log for more details.")

    def is_port_in_use(self, port):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            return s.connect_ex(('localhost', port)) == 0

    def run_script(self, pandrator_path, env_name, script_path, additional_args=None):
        if additional_args is None:
            additional_args = []

        logging.info(f"Running script {script_path} in {env_name} with args: {additional_args}")
        
        script_dir = os.path.dirname(script_path)
        command = self.build_pixi_run_command(
            pandrator_path,
            env_name,
            ['python', script_path] + additional_args
        )

        pandrator_log_file = os.path.join(script_dir, 'pandrator_startup.log')
        log_handle = open(pandrator_log_file, 'a', encoding='utf-8')

        creationflags = 0
        if os.name == 'nt':
            creationflags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)

        try:
            process = subprocess.Popen(
                command,
                cwd=script_dir,
                env=self.get_pixi_subprocess_env(pandrator_path),
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        process.log_file_path = pandrator_log_file
        logging.info(f"Pandrator startup log: {pandrator_log_file}")
        return process

    def ensure_process_started(self, process, process_name, startup_log_file, grace_period_seconds=2):
        if process is None:
            raise RuntimeError(f"{process_name} process was not created.")

        time.sleep(grace_period_seconds)
        return_code = process.poll()
        if return_code is None:
            return

        if hasattr(process, 'log_handle') and process.log_handle:
            process.log_handle.flush()
            process.log_handle.close()
            process.log_handle = None

        details = f"{process_name} exited immediately with code {return_code}."
        if startup_log_file:
            details += f" See log: {startup_log_file}"

        log_tail = self._read_log_tail_if_exists(startup_log_file)
        if log_tail:
            details += f" Last output:\n{log_tail}"

        raise RuntimeError(details)

    def run_xtts_api_server(self, xtts_server_path, use_cpu=False, pixi_path=None):
        """Run the XTTS2 API server via its upstream launcher script."""
        logging.info("Attempting to run XTTS API server...")
        logging.info(f"XTTS server path: {xtts_server_path}")
        logging.info(f"Use CPU: {use_cpu}")

        if not os.path.exists(xtts_server_path):
            raise FileNotFoundError(f"XTTS server path not found: {xtts_server_path}")

        if self.is_port_in_use(8020):
            error_msg = "XTTS server cannot be started because port 8020 is already in use."
            logging.error(error_msg)
            QMessageBox.critical(self, "Error", error_msg)
            return None

        run_script_path = os.path.join(xtts_server_path, 'run.bat')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"XTTS run script not found at: {run_script_path}")

        xtts_log_file = os.path.join(xtts_server_path, 'xtts_server.log')
        command = self.build_xtts_launcher_command(
            use_cpu=use_cpu,
            pixi_path=self.get_xtts_pixi_argument(xtts_server_path, pixi_path),
        )
        xtts_env = os.environ.copy()
        if use_cpu:
            xtts_env['XTTS_DEVICE'] = 'cpu'
            xtts_env['XTTS_USE_DEEPSPEED'] = 'false'
        elif self.disable_deepspeed_var:
            xtts_env['XTTS_USE_DEEPSPEED'] = 'false'
        else:
            xtts_env.pop('XTTS_USE_DEEPSPEED', None)

        log_handle = open(xtts_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                command,
                cwd=xtts_server_path,
                env=xtts_env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        logging.info(f"XTTS API server process started with PID: {process.pid}")
        return process

    def check_xtts_server_online(self, base_url, max_attempts=120, wait_interval=5, process=None):
        """Check if the XTTS server is online and responding."""
        probe_paths = ['/health', '/v1/models', '/docs']
        for attempt in range(1, max_attempts + 1):
            if process is not None and process.poll() is not None:
                logging.error("XTTS server process exited before coming online.")
                return False

            for probe_path in probe_paths:
                try:
                    response = requests.get(f"{base_url}{probe_path}", timeout=5)
                    if response.status_code == 404:
                        continue
                    if response.status_code < 400:
                        logging.info("XTTS server is online.")
                        return True
                except requests.exceptions.RequestException:
                    continue

            logging.info("XTTS server is not online yet. Waiting... (Attempt %s/%s)", attempt, max_attempts)
            time.sleep(wait_interval)

        logging.error("XTTS server failed to come online within the specified attempts.")
        return False

    def run_voxtral_api_server(self, voxtral_server_path):
        """Run the Voxtral API server via its upstream launcher script."""
        logging.info(f"Running Voxtral API server from {voxtral_server_path}...")

        if self.is_port_in_use(8000):
            error_msg = "Voxtral server cannot be started because port 8000 is already in use."
            logging.error(error_msg)
            QMessageBox.critical(self, "Error", error_msg)
            return None

        run_script_path = os.path.join(voxtral_server_path, 'run.ps1')
        if not os.path.exists(run_script_path):
            raise FileNotFoundError(f"Voxtral run script not found at: {run_script_path}")

        voxtral_log_file = os.path.join(voxtral_server_path, 'voxtral_server.log')
        log_handle = open(voxtral_log_file, 'a', encoding='utf-8')

        command = [
            'powershell',
            '-ExecutionPolicy',
            'Bypass',
            '-File',
            run_script_path,
            '-ProjectRoot',
            voxtral_server_path,
            '-BindHost',
            '127.0.0.1',
            '-Port',
            '8000',
            '-Model',
            'gguf',
        ]

        try:
            process = subprocess.Popen(
                command,
                cwd=voxtral_server_path,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.voxtral_process = process
        return process

    def check_voxtral_server_online(self, url, max_attempts=60, wait_interval=5):
        """Check if the Voxtral server is online and responding."""
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.get(url, timeout=5)
                if response.status_code == 200:
                    logging.info("Voxtral server is online.")
                    return True
            except requests.exceptions.RequestException:
                pass

            logging.info("Voxtral server is not online. Waiting... (Attempt %s/%s)", attempt, max_attempts)
            time.sleep(wait_interval)

        logging.error("Voxtral server failed to come online within the specified attempts.")
        return False

    def run_silero_api_server(self, pandrator_path, env_name):
        """Run the Silero API server"""
        logging.info(f"Running Silero API server in {env_name}...")

        if self.is_port_in_use(8001):
            error_msg = "Silero server cannot be started because port 8001 is already in use."
            logging.error(error_msg)
            QMessageBox.critical(self, "Error", error_msg)
            return None

        silero_log_file = os.path.join(pandrator_path, 'silero_server.log')
        silero_server_command = self.build_pixi_run_command(
            pandrator_path,
            env_name,
            ['python', '-m', 'silero_api_server']
        )

        log_handle = open(silero_log_file, 'a', encoding='utf-8')
        try:
            process = subprocess.Popen(
                silero_server_command,
                cwd=pandrator_path,
                env=self.get_pixi_subprocess_env(pandrator_path),
                stdout=log_handle,
                stderr=subprocess.STDOUT
            )
        except Exception:
            log_handle.close()
            raise

        process.log_handle = log_handle
        self.silero_process = process
        return process

    def check_silero_server_online(self, url, max_attempts=30, wait_interval=10):
        """Check if the Silero server is online and responding"""
        attempt = 1
        while attempt <= max_attempts:
            try:
                response = requests.get(url)
                if response.status_code == 200:
                    logging.info("Silero server is online.")
                    return True
            except requests.exceptions.RequestException as e:
                logging.info(f"Silero server is not online. Waiting... (Attempt {attempt}/{max_attempts})")
            
            time.sleep(wait_interval)
            attempt += 1
        
        logging.error("Silero server failed to come online within the specified attempts.")
        return False

    def check_processes_status(self):
        """Check the status of running processes and update UI accordingly"""
        any_process_running = False
        
        # Check Pandrator
        if self.pandrator_process and self.pandrator_process.poll() is not None:
            # Pandrator has exited
            return_code = self.pandrator_process.poll()
            startup_log_file = getattr(self.pandrator_process, 'log_file_path', '')
            if hasattr(self.pandrator_process, 'log_handle') and self.pandrator_process.log_handle:
                self.pandrator_process.log_handle.close()

            if return_code not in (None, 0):
                details = f"Pandrator exited with code {return_code}."
                if startup_log_file:
                    details += f" See log: {startup_log_file}"
                logging.error(details)

            self.pandrator_process = None
            self.shutdown_apps()  # Shut down other apps when Pandrator exits
        elif self.pandrator_process:
            any_process_running = True
            
        # Check XTTS
        if self.xtts_process and self.xtts_process.poll() is not None:
            # XTTS has exited
            self.xtts_process = None
        elif self.xtts_process:
            any_process_running = True

        # Check Voxtral
        if self.voxtral_process and self.voxtral_process.poll() is not None:
            # Voxtral has exited
            if hasattr(self.voxtral_process, 'log_handle') and self.voxtral_process.log_handle:
                self.voxtral_process.log_handle.close()
            self.voxtral_process = None
        elif self.voxtral_process:
            any_process_running = True
            
        # Check Silero
        if self.silero_process and self.silero_process.poll() is not None:
            # Silero has exited
            if hasattr(self.silero_process, 'log_handle') and self.silero_process.log_handle:
                self.silero_process.log_handle.close()
            self.silero_process = None
        elif self.silero_process:
            any_process_running = True
        
        if not any_process_running:
            self.update_status("All processes have exited.")
            self.refresh_ui_state()
        else:
            QTimer.singleShot(5000, self.check_processes_status)  # Schedule next check

    def shutdown_apps(self):
        """Shut down all running applications"""
        self.shutdown_xtts()
        self.shutdown_voxtral()
        self.shutdown_silero()

    def shutdown_xtts(self):
        """Shut down the XTTS server"""
        if self.xtts_process:
            logging.info(f"Terminating XTTS process with PID: {self.xtts_process.pid}")
            try:
                parent = psutil.Process(self.xtts_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.xtts_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("XTTS process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("XTTS process did not terminate, forcing kill")
                parent = psutil.Process(self.xtts_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            if hasattr(self.xtts_process, 'log_handle') and self.xtts_process.log_handle:
                self.xtts_process.log_handle.close()
            self.xtts_process = None

        # Check if any process is using port 8020 and kill it
        seen_pids = set()
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8020:
                if conn.pid in seen_pids:
                    continue
                seen_pids.add(conn.pid)
                try:
                    if conn.pid in (None, 0):
                        continue
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8020: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8020 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_voxtral(self):
        """Shut down the Voxtral server"""
        if self.voxtral_process:
            logging.info(f"Terminating Voxtral process with PID: {self.voxtral_process.pid}")
            try:
                parent = psutil.Process(self.voxtral_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.voxtral_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Voxtral process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Voxtral process did not terminate, forcing kill")
                parent = psutil.Process(self.voxtral_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            if hasattr(self.voxtral_process, 'log_handle') and self.voxtral_process.log_handle:
                self.voxtral_process.log_handle.close()
            self.voxtral_process = None

        # Check if any process is using port 8000 and kill it
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8000:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8000: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8000 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def shutdown_silero(self):
        """Shut down the Silero server"""
        if self.silero_process:
            logging.info(f"Terminating Silero process with PID: {self.silero_process.pid}")
            try:
                parent = psutil.Process(self.silero_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.terminate()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when terminating child process with PID: {child.pid}")
                parent.terminate()
                self.silero_process.wait(timeout=10)
            except psutil.NoSuchProcess:
                logging.info("Silero process already terminated.")
            except psutil.TimeoutExpired:
                logging.warning("Silero process did not terminate, forcing kill")
                parent = psutil.Process(self.silero_process.pid)
                for child in parent.children(recursive=True):
                    try:
                        child.kill()
                    except psutil.AccessDenied:
                        logging.warning(f"Access denied when killing child process with PID: {child.pid}")
                parent.kill()
            if hasattr(self.silero_process, 'log_handle') and self.silero_process.log_handle:
                self.silero_process.log_handle.close()
            self.silero_process = None

        # Check if any process is using port 8001 and kill it
        for conn in psutil.net_connections():
            if hasattr(conn, 'laddr') and hasattr(conn.laddr, 'port') and conn.laddr.port == 8001:
                try:
                    process = psutil.Process(conn.pid)
                    if process.pid != 0:  # Skip System Idle Process
                        process.terminate()
                        logging.info(f"Terminated process using port 8001: PID {conn.pid}")
                except psutil.NoSuchProcess:
                    logging.info(f"Process using port 8001 (PID {conn.pid}) no longer exists")
                except psutil.AccessDenied:
                    logging.warning(f"Access denied when terminating process with PID: {conn.pid}")

    def closeEvent(self, event):
        """Handle window close event"""
        self.shutdown_apps()
        self.shutdown_logging()
        event.accept()


if __name__ == "__main__":
    # Import needed modules
    from PyQt6.QtGui import QColor, QPalette
    
    # Set up application style
    app = QApplication(sys.argv)
    app.setStyle("Fusion")  # Use Fusion style for a modern look
    
    # Define pastel purple color
    pastel_purple = QColor("#A589D9")  # Main button color
    pastel_purple_hover = QColor("#B79AE6")  # Lighter for hover
    pastel_purple_pressed = QColor("#8A6CC7")  # Darker for pressed state
    
    # Create dark palette
    dark_palette = QPalette()
    # Set colors using the QPalette.ColorRole enum
    dark_palette.setColor(QPalette.ColorRole.Window, QColor(45, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.WindowText, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.Base, QColor(30, 30, 30))
    dark_palette.setColor(QPalette.ColorRole.AlternateBase, QColor(45, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.ToolTipText, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.Text, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.Button, QColor(45, 45, 48))
    dark_palette.setColor(QPalette.ColorRole.ButtonText, QColor(255, 255, 255))
    dark_palette.setColor(QPalette.ColorRole.BrightText, QColor(255, 0, 0))
    dark_palette.setColor(QPalette.ColorRole.Link, QColor(0, 155, 255))
    dark_palette.setColor(QPalette.ColorRole.Highlight, pastel_purple)  # Changed to match buttons
    dark_palette.setColor(QPalette.ColorRole.HighlightedText, QColor(255, 255, 255))
    app.setPalette(dark_palette)
    
    # Set stylesheet for custom styling with pastel purple buttons and white checkbox borders
    app.setStyleSheet(f"""
        QMainWindow {{
            background-color: #2D2D30;
        }}
        QWidget {{
            background-color: #2D2D30;
            color: #FFFFFF;
        }}
        QPushButton {{
            background-color: {pastel_purple.name()};
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
        }}
        QPushButton:hover {{
            background-color: {pastel_purple_hover.name()};
        }}
        QPushButton:pressed {{
            background-color: {pastel_purple_pressed.name()};
        }}
        QPushButton:disabled {{
            background-color: #555555;
            color: #888888;
        }}
        QCheckBox {{
            spacing: 8px;
        }}
        QCheckBox::indicator {{
            width: 18px;
            height: 18px;
            border: 1px solid white;
            border-radius: 3px;
        }}
        QCheckBox::indicator:checked {{
            background-color: {pastel_purple.name()};
            border: 1px solid white;
        }}
        QCheckBox::indicator:unchecked {{
            background-color: transparent;
            border: 1px solid white;
        }}
        QGroupBox {{
            border: 1px solid #444444;
            border-radius: 4px;
            margin-top: 12px;
            padding-top: 15px;
        }}
        QGroupBox::title {{
            subcontrol-origin: margin;
            subcontrol-position: top left;
            padding: 0 5px;
            color: {pastel_purple.name()};
        }}
        QTabWidget::pane {{
            border: 1px solid #444444;
            border-radius: 4px;
        }}
        QTabBar::tab {{
            background-color: #2D2D30;
            color: #FFFFFF;
            border: 1px solid #444444;
            border-bottom: none;
            border-top-left-radius: 4px;
            border-top-right-radius: 4px;
            padding: 8px 16px;
        }}
        QTabBar::tab:selected {{
            background-color: {pastel_purple.name()};
        }}
        QTabBar::tab:hover:!selected {{
            background-color: #3D3D40;
        }}
        QProgressBar {{
            border: 1px solid #444444;
            border-radius: 3px;
            text-align: center;
            height: 20px;
        }}
        QProgressBar::chunk {{
            background-color: {pastel_purple.name()};
            width: 10px;
        }}
    """)
    
    # Create and show the main window
    window = PandratorInstaller()
    window.show()
    
    sys.exit(app.exec())
