from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
)

from ...logic import text_preprocessor


class ReviewTextDialog(QDialog):
    def __init__(self, source_text: str, parent=None):
        super().__init__(parent)
        self._source_text = source_text or ""

        self.setWindowTitle("Review Extracted Text")
        self.resize(1000, 800)

        layout = QVBoxLayout(self)

        controls_layout = QHBoxLayout()
        self.remove_double_newlines_checkbox = QCheckBox(
            "Remove Double Newlines (use if paragraphs are not rendered correctly)"
        )
        self.remove_double_newlines_checkbox.toggled.connect(self._refresh_preview)
        controls_layout.addWidget(self.remove_double_newlines_checkbox)
        controls_layout.addStretch(1)

        self.add_chapter_button = QPushButton("Add Chapter Marker")
        self.add_chapter_button.clicked.connect(self._insert_chapter_marker)
        controls_layout.addWidget(self.add_chapter_button)

        layout.addLayout(controls_layout)

        self.text_edit = QTextEdit()
        layout.addWidget(self.text_edit)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Accept")
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self._refresh_preview()

    def _refresh_preview(self):
        processed_text = text_preprocessor.preprocess_text_pdf(
            self._source_text,
            remove_double_newlines=self.remove_double_newlines_checkbox.isChecked(),
        )
        self.text_edit.setPlainText(processed_text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.Start)

    def _insert_chapter_marker(self):
        cursor = self.text_edit.textCursor()
        cursor.insertText("[[Chapter]]")

    def get_data(self) -> dict:
        return {
            "text": self.text_edit.toPlainText(),
            "remove_double_newlines": self.remove_double_newlines_checkbox.isChecked(),
        }
