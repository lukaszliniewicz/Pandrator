from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QCheckBox, QDialogButtonBox

class PasteTextDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Paste or Write Text")
        self.resize(600, 450)

        layout = QVBoxLayout(self)

        self.text_edit = QTextEdit()
        layout.addWidget(self.text_edit)

        self.mark_paragraphs_checkbox = QCheckBox("Mark paragraphs only for multiple new lines")
        layout.addWidget(self.mark_paragraphs_checkbox)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def get_data(self) -> dict:
        """Returns the dialog's data if accepted."""
        return {
            "text": self.text_edit.toPlainText(),
            "mark_paragraphs": self.mark_paragraphs_checkbox.isChecked()
        }
