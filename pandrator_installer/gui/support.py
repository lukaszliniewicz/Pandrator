"""Qt workers, logging adapters, and small dialogs used by the installer GUI."""

import logging
import traceback

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal
from PyQt6.QtWidgets import QDialog, QPushButton, QTextEdit, QVBoxLayout

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

class HeadlessSignalEmitter:
    def __init__(self, callback=None):
        self.callback = callback

    def emit(self, value):
        if self.callback is None:
            return
        self.callback(value)

class HeadlessWorkerProxy:
    def __init__(self):
        self.update_progress = HeadlessSignalEmitter(self.on_progress)
        self.update_status = HeadlessSignalEmitter(self.on_status)

    @staticmethod
    def on_progress(value):
        try:
            percentage = int(float(value) * 100)
        except Exception:
            return

        percentage = max(0, min(100, percentage))
        logging.info(f"Progress: {percentage}%")

    @staticmethod
    def on_status(text):
        message = str(text)
        logging.info(message)
        print(message)

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

