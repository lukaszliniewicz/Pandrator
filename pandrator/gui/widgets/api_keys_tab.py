from PyQt6.QtWidgets import (
    QWidget,
    QVBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QGridLayout,
    QToolButton,
    QHBoxLayout,
)

class ApiKeysTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(10)

        note_text = ("Note: API keys are saved as environment variables. "
                     "If they don't work immediately, restart Pandrator.")
        note_label = QLabel(note_text)
        note_label.setWordWrap(True)
        main_layout.addWidget(note_label)

        hint_label = QLabel("Keys are masked. Press Enter in a field to save it.")
        hint_label.setWordWrap(True)
        main_layout.addWidget(hint_label)

        self.api_key_labels = {
            "ANTHROPIC_API_KEY": "Anthropic API Key:",
            "OPENAI_API_KEY": "OpenAI API Key:",
            "DEEPL_API_KEY": "DeepL API Key:",
            "GEMINI_API_KEY": "Gemini API Key:",
            "OPENROUTER_API_KEY": "OpenRouter API Key:",
            "HF_TOKEN": "Hugging Face Token:",
        }
        self.api_key_edits: dict[str, QLineEdit] = {}

        grid_layout = QGridLayout()
        grid_layout.setColumnStretch(1, 1)

        for i, (key, label_text) in enumerate(self.api_key_labels.items()):
            line_edit = QLineEdit()
            line_edit.setEchoMode(QLineEdit.EchoMode.Password)
            line_edit.setPlaceholderText("Paste API key")
            line_edit.setClearButtonEnabled(True)
            line_edit.returnPressed.connect(
                lambda k=key, le=line_edit: self._save_key(k, le.text())
            )
            self.api_key_edits[key] = line_edit

            show_button = QToolButton()
            show_button.setText("Show")
            show_button.setCheckable(True)
            show_button.toggled.connect(
                lambda checked, le=line_edit, b=show_button: self._toggle_visibility(checked, le, b)
            )

            save_button = QPushButton("Save")
            save_button.clicked.connect(
                lambda _, k=key, le=line_edit: self._save_key(k, le.text())
            )

            grid_layout.addWidget(QLabel(label_text), i, 0)
            grid_layout.addWidget(line_edit, i, 1)
            grid_layout.addWidget(show_button, i, 2)
            grid_layout.addWidget(save_button, i, 3)

        main_layout.addLayout(grid_layout)

        actions_layout = QHBoxLayout()
        self.save_all_button = QPushButton("Save All")
        self.save_all_button.clicked.connect(self._save_all_keys)
        actions_layout.addWidget(self.save_all_button)
        actions_layout.addStretch(1)
        main_layout.addLayout(actions_layout)

        self.feedback_label = QLabel("")
        self.feedback_label.setWordWrap(True)
        main_layout.addWidget(self.feedback_label)

        main_layout.addStretch()

        self._load_keys()

    def _save_key(self, key_name, key_value):
        normalized_value = (key_value or "").strip()
        if key_name in self.api_key_edits and self.api_key_edits[key_name].text() != normalized_value:
            self.api_key_edits[key_name].setText(normalized_value)

        if self.logic.save_api_key(key_name, normalized_value):
            self.feedback_label.setText(f"Saved {key_name}.")

    def _save_all_keys(self):
        saved_count = 0
        for key_name, line_edit in self.api_key_edits.items():
            normalized_value = (line_edit.text() or "").strip()
            if line_edit.text() != normalized_value:
                line_edit.setText(normalized_value)
            if self.logic.save_api_key(key_name, normalized_value):
                saved_count += 1

        self.feedback_label.setText(f"Saved {saved_count} key(s).")

    def _toggle_visibility(self, checked: bool, line_edit: QLineEdit, button: QToolButton):
        line_edit.setEchoMode(
            QLineEdit.EchoMode.Normal if checked else QLineEdit.EchoMode.Password
        )
        button.setText("Hide" if checked else "Show")

    def _load_keys(self):
        for key, line_edit in self.api_key_edits.items():
            line_edit.setText(self.logic.get_api_key(key))
