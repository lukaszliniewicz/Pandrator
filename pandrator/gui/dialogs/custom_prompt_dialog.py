from PyQt6.QtWidgets import QDialog, QVBoxLayout, QTextEdit, QDialogButtonBox, QLabel

class CustomPromptDialog(QDialog):
    def __init__(self, prompt_text: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Custom Correction Prompt")
        self.resize(600, 400)

        layout = QVBoxLayout(self)

        label = QLabel("Enter additional context for correction (e.g., proper names, terminology):")
        label.setWordWrap(True)
        layout.addWidget(label)

        self.text_edit = QTextEdit()
        self.text_edit.setPlainText(prompt_text)
        layout.addWidget(self.text_edit)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def get_prompt_text(self) -> str:
        """Returns the text from the text edit widget."""
        return self.text_edit.toPlainText()
