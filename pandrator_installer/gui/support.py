"""Qt workers, logging adapters, and small dialogs used by the installer GUI."""

import logging
import traceback

from PyQt6.QtCore import QByteArray, QObject, QSize, QThread, Qt, QUrl, pyqtSignal
from PyQt6.QtGui import QColor, QDesktopServices, QIcon, QPainter, QPen, QPixmap
from PyQt6.QtSvg import QSvgRenderer
from PyQt6.QtWidgets import QCheckBox, QDialog, QPushButton, QTextBrowser, QVBoxLayout


GITHUB_URL = "https://github.com/lukaszliniewicz/Pandrator"
GITHUB_MARK_SVG = b"""
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16">
  <path fill="#f4f1fa" d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47
  7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09
  -.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72
  1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95
  0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82A7.65
  7.65 0 0 1 8 3.8c.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16
  1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54
  1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.013 8.013 0 0 0 16
  8c0-4.42-3.58-8-8-8z"/>
</svg>
"""


def create_github_icon(size=18):
    renderer = QSvgRenderer(QByteArray(GITHUB_MARK_SVG))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter)
    painter.end()
    return QIcon(pixmap)


class GitHubLinkButton(QPushButton):
    def __init__(self, text="See on GitHub", parent=None):
        super().__init__(create_github_icon(), text, parent)
        self.setObjectName("githubButton")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setIconSize(QSize(17, 17))
        self.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(GITHUB_URL)))


class ToggleSwitch(QCheckBox):
    """Compact switch control that preserves standard checkbox behavior."""

    TRACK_WIDTH = 38
    TRACK_HEIGHT = 20
    KNOB_SIZE = 16
    TEXT_GAP = 10

    def __init__(self, text="", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMinimumHeight(28)

    def sizeHint(self):
        text_width = self.fontMetrics().horizontalAdvance(self.text())
        return QSize(self.TRACK_WIDTH + self.TEXT_GAP + text_width + 4, 30)

    def paintEvent(self, event):
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)

        track_top = (self.height() - self.TRACK_HEIGHT) / 2
        track_rect = self.rect().adjusted(0, int(track_top), 0, -int(track_top))
        track_rect.setWidth(self.TRACK_WIDTH)

        if not self.isEnabled():
            track_color = QColor("#45464b")
            knob_color = QColor("#777980")
            text_color = QColor("#85878e")
        elif self.isChecked():
            track_color = QColor("#7e57c2")
            knob_color = QColor("#ffffff")
            text_color = QColor("#f4f1fa")
        else:
            track_color = QColor("#4a4b52")
            knob_color = QColor("#c9c9ce")
            text_color = QColor("#f4f1fa")

        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(track_color)
        painter.drawRoundedRect(track_rect, self.TRACK_HEIGHT / 2, self.TRACK_HEIGHT / 2)

        knob_margin = (self.TRACK_HEIGHT - self.KNOB_SIZE) / 2
        knob_x = (
            self.TRACK_WIDTH - self.KNOB_SIZE - knob_margin
            if self.isChecked()
            else knob_margin
        )
        painter.setBrush(knob_color)
        painter.drawEllipse(
            int(knob_x),
            int(track_top + knob_margin),
            self.KNOB_SIZE,
            self.KNOB_SIZE,
        )

        if self.hasFocus():
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor("#a98cdb"), 1))
            painter.drawRoundedRect(track_rect.adjusted(-2, -2, 2, 2), 12, 12)

        text_rect = self.rect().adjusted(
            self.TRACK_WIDTH + self.TEXT_GAP,
            0,
            0,
            0,
        )
        painter.setPen(text_color)
        painter.drawText(
            text_rect,
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
            self.text(),
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

        info_text = QTextBrowser()
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

        github_button = GitHubLinkButton()
        layout.addWidget(github_button, alignment=Qt.AlignmentFlag.AlignRight)

        close_button = QPushButton("Close")
        close_button.clicked.connect(self.accept)
        layout.addWidget(close_button, alignment=Qt.AlignmentFlag.AlignRight)
