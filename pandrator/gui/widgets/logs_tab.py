import os

from PyQt6.QtCore import QTimer
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import QWidget, QVBoxLayout, QPlainTextEdit, QPushButton

class LogsTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        self._active_log_path = ""
        self._last_read_position = 0

        main_layout = QVBoxLayout(self)

        self.logs_text_edit = QPlainTextEdit()
        self.logs_text_edit.setReadOnly(True)
        self.logs_text_edit.document().setMaximumBlockCount(5000)
        main_layout.addWidget(self.logs_text_edit)

        self.update_button = QPushButton("Update Logs")
        self.update_button.clicked.connect(self.update_logs)
        main_layout.addWidget(self.update_button)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.update_logs)
        self.timer.start(2000)

        self.update_logs()

    def _reset_reader_state(self, log_path: str):
        self._active_log_path = log_path or ""
        self._last_read_position = 0
        self.logs_text_edit.clear()

    def _append_log_chunk(self, chunk: str):
        if not chunk:
            return

        cursor = self.logs_text_edit.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(chunk)
        self.logs_text_edit.setTextCursor(cursor)
        self.logs_text_edit.ensureCursorVisible()

    def update_logs(self):
        log_path = self.logic.log_file_path
        if not log_path or not self.isVisible():
            return

        if log_path != self._active_log_path:
            self._reset_reader_state(log_path)

        try:
            file_size = os.path.getsize(log_path)
            if file_size < self._last_read_position:
                self._reset_reader_state(log_path)

            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                f.seek(self._last_read_position)
                chunk = f.read()
                self._last_read_position = f.tell()

            self._append_log_chunk(chunk)
        except FileNotFoundError:
            self._reset_reader_state(log_path)
            self.logs_text_edit.setPlainText(f"Log file not found: {log_path}")
        except Exception as e:
            self._reset_reader_state(log_path)
            self.logs_text_edit.setPlainText(f"Error reading log file: {e}")
