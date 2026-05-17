import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

class ProvidersTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        main_layout.addWidget(self._create_group_label("LLM Providers"))
        main_layout.addWidget(self._create_llm_frame())

        main_layout.addWidget(self._create_group_label("TTS Providers"))
        main_layout.addWidget(self._create_tts_frame())

        self._connect_signals()
        self.update_ui_from_state()
        self.logic.state_changed.connect(self.update_ui_from_state)

    def _create_group_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        return label

    def _create_llm_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.llm_provider_combo = QComboBox()
        self.llm_provider_type_combo = QComboBox()
        self.llm_provider_type_combo.setEditable(True)
        self.llm_provider_type_combo.addItems(
            [
                "openai",
                "anthropic",
                "gemini",
                "openrouter",
                "ollama",
                "groq",
                "mistral",
                "vertex_ai",
                "azure",
                "bedrock",
            ]
        )

        self.llm_provider_name_edit = QLineEdit()
        self.llm_provider_name_edit.setPlaceholderText("My Provider")
        self.llm_provider_api_base_edit = QLineEdit()
        self.llm_provider_api_base_edit.setPlaceholderText("http://localhost:11434/v1")
        self.llm_provider_api_key_edit = QLineEdit()
        self.llm_provider_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_provider_api_key_edit.setPlaceholderText("Optional API key")
        self.llm_provider_models_edit = QTextEdit()
        self.llm_provider_models_edit.setPlaceholderText("One model per line or comma-separated")
        self.llm_provider_models_edit.setFixedHeight(90)

        self.llm_new_provider_button = QPushButton("New")
        self.llm_save_provider_button = QPushButton("Save")
        self.llm_remove_provider_button = QPushButton("Remove")
        self.llm_refresh_builtin_models_button = QPushButton("Refresh Built-in Models")

        self.llm_feedback_label = QLabel("")
        self.llm_feedback_label.setWordWrap(True)

        layout.addWidget(QLabel("Provider:"), 0, 0)
        layout.addWidget(self.llm_provider_combo, 0, 1)
        layout.addWidget(self.llm_new_provider_button, 0, 2)
        layout.addWidget(self.llm_remove_provider_button, 0, 3)

        layout.addWidget(QLabel("Display Name:"), 1, 0)
        layout.addWidget(self.llm_provider_name_edit, 1, 1, 1, 3)

        layout.addWidget(QLabel("LiteLLM Provider:"), 2, 0)
        layout.addWidget(self.llm_provider_type_combo, 2, 1, 1, 3)

        layout.addWidget(QLabel("API Base URL:"), 3, 0)
        layout.addWidget(self.llm_provider_api_base_edit, 3, 1, 1, 3)

        layout.addWidget(QLabel("API Key:"), 4, 0)
        layout.addWidget(self.llm_provider_api_key_edit, 4, 1, 1, 3)

        layout.addWidget(QLabel("Models:"), 5, 0)
        layout.addWidget(self.llm_provider_models_edit, 5, 1, 1, 3)

        layout.addWidget(self.llm_save_provider_button, 6, 2)
        layout.addWidget(self.llm_refresh_builtin_models_button, 6, 3)
        layout.addWidget(self.llm_feedback_label, 7, 0, 1, 4)

        return frame

    def _create_tts_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.tts_provider_combo = QComboBox()
        self.tts_provider_type_combo = QComboBox()
        self.tts_provider_type_combo.addItems(["openai", "gemini"])
        self.tts_provider_name_edit = QLineEdit()
        self.tts_provider_name_edit.setPlaceholderText("My TTS Provider")
        self.tts_provider_api_base_edit = QLineEdit()
        self.tts_provider_api_base_edit.setPlaceholderText("https://api.example.com/v1")
        self.tts_provider_api_key_edit = QLineEdit()
        self.tts_provider_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tts_provider_api_key_edit.setPlaceholderText("Optional API key")

        self.tts_provider_models_edit = QTextEdit()
        self.tts_provider_models_edit.setPlaceholderText("One model per line or comma-separated")
        self.tts_provider_models_edit.setFixedHeight(70)
        self.tts_provider_voices_edit = QTextEdit()
        self.tts_provider_voices_edit.setPlaceholderText("One voice per line or comma-separated")
        self.tts_provider_voices_edit.setFixedHeight(70)

        self.tts_new_provider_button = QPushButton("New")
        self.tts_save_provider_button = QPushButton("Save")
        self.tts_remove_provider_button = QPushButton("Remove")
        self.tts_test_connection_button = QPushButton("Test Connection")
        self.tts_discover_catalog_button = QPushButton("Discover Models/Voices")

        self.tts_feedback_label = QLabel("")
        self.tts_feedback_label.setWordWrap(True)

        layout.addWidget(QLabel("Provider:"), 0, 0)
        layout.addWidget(self.tts_provider_combo, 0, 1)
        layout.addWidget(self.tts_new_provider_button, 0, 2)
        layout.addWidget(self.tts_remove_provider_button, 0, 3)

        layout.addWidget(QLabel("Display Name:"), 1, 0)
        layout.addWidget(self.tts_provider_name_edit, 1, 1, 1, 3)

        layout.addWidget(QLabel("Provider Type:"), 2, 0)
        layout.addWidget(self.tts_provider_type_combo, 2, 1, 1, 3)

        layout.addWidget(QLabel("API Base URL:"), 3, 0)
        layout.addWidget(self.tts_provider_api_base_edit, 3, 1, 1, 3)

        layout.addWidget(QLabel("API Key:"), 4, 0)
        layout.addWidget(self.tts_provider_api_key_edit, 4, 1, 1, 3)

        layout.addWidget(QLabel("Models:"), 5, 0)
        layout.addWidget(self.tts_provider_models_edit, 5, 1, 1, 3)

        layout.addWidget(QLabel("Voices:"), 6, 0)
        layout.addWidget(self.tts_provider_voices_edit, 6, 1, 1, 3)

        layout.addWidget(self.tts_test_connection_button, 7, 1)
        layout.addWidget(self.tts_discover_catalog_button, 7, 2)
        layout.addWidget(self.tts_save_provider_button, 7, 3)
        layout.addWidget(self.tts_feedback_label, 8, 0, 1, 4)

        return frame

    def _connect_signals(self):
        self.llm_provider_combo.currentIndexChanged.connect(self._on_llm_provider_selected)
        self.llm_new_provider_button.clicked.connect(self._on_new_llm_provider)
        self.llm_save_provider_button.clicked.connect(self._on_save_llm_provider)
        self.llm_remove_provider_button.clicked.connect(self._on_remove_llm_provider)
        self.llm_refresh_builtin_models_button.clicked.connect(self._on_refresh_builtin_llm_models)

        self.tts_provider_combo.currentIndexChanged.connect(self._on_tts_provider_selected)
        self.tts_new_provider_button.clicked.connect(self._on_new_tts_provider)
        self.tts_save_provider_button.clicked.connect(self._on_save_tts_provider)
        self.tts_remove_provider_button.clicked.connect(self._on_remove_tts_provider)
        self.tts_test_connection_button.clicked.connect(self._on_test_tts_provider)
        self.tts_discover_catalog_button.clicked.connect(self._on_discover_tts_provider_catalog)

    @staticmethod
    def _parse_items(raw_text: str) -> list[str]:
        chunks = re.split(r"[,\n;]", str(raw_text or ""))
        seen: set[str] = set()
        values: list[str] = []
        for chunk in chunks:
            item = chunk.strip()
            if item and item not in seen:
                values.append(item)
                seen.add(item)
        return values

    @staticmethod
    def _format_items(items) -> str:
        if not isinstance(items, list):
            return ""
        return "\n".join(str(item).strip() for item in items if str(item).strip())

    def _llm_providers(self) -> list[dict]:
        return self.logic.list_llm_provider_configs()

    def _tts_providers(self) -> list[dict]:
        return self.logic.list_tts_provider_configs()

    def _refresh_llm_provider_dropdown(self, target_provider_id: str = ""):
        if not target_provider_id:
            target_provider_id = str(self.llm_provider_combo.currentData() or "")

        providers = self._llm_providers()
        self.llm_provider_combo.blockSignals(True)
        self.llm_provider_combo.clear()
        self.llm_provider_combo.addItem("New Custom Provider", "")
        for provider in providers:
            provider_id = str(provider.get("id") or "")
            provider_name = str(provider.get("name") or provider_id)
            self.llm_provider_combo.addItem(provider_name, provider_id)

        index = self.llm_provider_combo.findData(target_provider_id)
        self.llm_provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.llm_provider_combo.blockSignals(False)
        self._load_llm_provider_form(str(self.llm_provider_combo.currentData() or ""))

    def _refresh_tts_provider_dropdown(self, target_provider_id: str = ""):
        if not target_provider_id:
            target_provider_id = str(self.tts_provider_combo.currentData() or "")

        providers = self._tts_providers()
        self.tts_provider_combo.blockSignals(True)
        self.tts_provider_combo.clear()
        self.tts_provider_combo.addItem("New Custom Provider", "")
        for provider in providers:
            provider_id = str(provider.get("id") or "")
            provider_name = str(provider.get("name") or provider_id)
            self.tts_provider_combo.addItem(provider_name, provider_id)

        index = self.tts_provider_combo.findData(target_provider_id)
        self.tts_provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.tts_provider_combo.blockSignals(False)
        self._load_tts_provider_form(str(self.tts_provider_combo.currentData() or ""))

    def _load_llm_provider_form(self, provider_id: str):
        provider = next(
            (
                item
                for item in self._llm_providers()
                if str(item.get("id") or "") == provider_id
            ),
            None,
        )

        if provider is None:
            self.llm_provider_name_edit.setText("")
            self.llm_provider_type_combo.setCurrentText("openai")
            self.llm_provider_api_base_edit.setText("")
            self.llm_provider_api_key_edit.setText("")
            self.llm_provider_models_edit.setPlainText("")
            self.llm_remove_provider_button.setEnabled(False)
            return

        self.llm_provider_name_edit.setText(str(provider.get("name") or provider.get("id") or ""))
        self.llm_provider_type_combo.setCurrentText(str(provider.get("provider") or "openai"))
        self.llm_provider_api_base_edit.setText(str(provider.get("api_base") or ""))
        self.llm_provider_api_key_edit.setText(str(provider.get("api_key") or ""))
        self.llm_provider_models_edit.setPlainText(self._format_items(provider.get("models", [])))
        self.llm_remove_provider_button.setEnabled(bool(provider.get("is_custom", False)))

    def _load_tts_provider_form(self, provider_id: str):
        provider = next(
            (
                item
                for item in self._tts_providers()
                if str(item.get("id") or "") == provider_id
            ),
            None,
        )

        if provider is None:
            self.tts_provider_name_edit.setText("")
            self.tts_provider_type_combo.setCurrentText("openai")
            self.tts_provider_api_base_edit.setText("")
            self.tts_provider_api_key_edit.setText("")
            self.tts_provider_models_edit.setPlainText("")
            self.tts_provider_voices_edit.setPlainText("")
            self.tts_remove_provider_button.setEnabled(False)
            self.tts_test_connection_button.setEnabled(False)
            self.tts_discover_catalog_button.setEnabled(False)
            return

        self.tts_provider_name_edit.setText(str(provider.get("name") or provider.get("id") or ""))
        self.tts_provider_type_combo.setCurrentText(str(provider.get("provider") or "openai"))
        self.tts_provider_api_base_edit.setText(str(provider.get("api_base") or ""))
        self.tts_provider_api_key_edit.setText(str(provider.get("api_key") or ""))
        self.tts_provider_models_edit.setPlainText(self._format_items(provider.get("models", [])))
        self.tts_provider_voices_edit.setPlainText(self._format_items(provider.get("voices", [])))
        self.tts_remove_provider_button.setEnabled(bool(provider.get("is_custom", False)))
        self.tts_test_connection_button.setEnabled(True)
        self.tts_discover_catalog_button.setEnabled(True)

    def _on_new_llm_provider(self):
        self.llm_provider_combo.setCurrentIndex(0)
        self._load_llm_provider_form("")

    def _on_new_tts_provider(self):
        self.tts_provider_combo.setCurrentIndex(0)
        self._load_tts_provider_form("")

    def _on_llm_provider_selected(self):
        self._load_llm_provider_form(str(self.llm_provider_combo.currentData() or ""))

    def _on_tts_provider_selected(self):
        self._load_tts_provider_form(str(self.tts_provider_combo.currentData() or ""))

    def _on_save_llm_provider(self):
        provider_id = str(self.llm_provider_combo.currentData() or "")
        provider_name = self.llm_provider_name_edit.text().strip()
        provider_key = self.llm_provider_type_combo.currentText().strip()
        api_base = self.llm_provider_api_base_edit.text().strip()
        api_key = self.llm_provider_api_key_edit.text().strip()
        models = self._parse_items(self.llm_provider_models_edit.toPlainText())

        success, resolved_provider_id, error_message = self.logic.save_llm_provider(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_key=provider_key,
            api_base=api_base,
            api_key=api_key,
            models=models,
        )
        if not success:
            self.llm_feedback_label.setText(error_message or "Could not save LLM provider.")
            return

        self._refresh_llm_provider_dropdown(resolved_provider_id)
        self.llm_feedback_label.setText(f"Saved LLM provider '{provider_name or resolved_provider_id}'.")

    def _on_remove_llm_provider(self):
        provider_id = str(self.llm_provider_combo.currentData() or "")
        if not provider_id:
            self.llm_feedback_label.setText("Select a custom LLM provider to remove.")
            return

        success, error_message = self.logic.remove_llm_provider(provider_id)
        if not success:
            self.llm_feedback_label.setText(error_message or "Could not remove LLM provider.")
            return
        self._refresh_llm_provider_dropdown("")
        self.llm_feedback_label.setText(f"Removed LLM provider '{provider_id}'.")

    def _on_refresh_builtin_llm_models(self):
        status_lines = self.logic.refresh_llm_builtin_models()
        self._refresh_llm_provider_dropdown(str(self.llm_provider_combo.currentData() or ""))
        if status_lines:
            self.llm_feedback_label.setText(" ".join(status_lines))
        else:
            self.llm_feedback_label.setText("Finished refreshing built-in model catalogs.")

    def _on_save_tts_provider(self):
        provider_id = str(self.tts_provider_combo.currentData() or "")
        provider_name = self.tts_provider_name_edit.text().strip()
        provider_type = self.tts_provider_type_combo.currentText().strip()
        api_base = self.tts_provider_api_base_edit.text().strip()
        api_key = self.tts_provider_api_key_edit.text().strip()
        models = self._parse_items(self.tts_provider_models_edit.toPlainText())
        voices = self._parse_items(self.tts_provider_voices_edit.toPlainText())

        success, resolved_provider_id, error_message = self.logic.save_tts_provider(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_type=provider_type,
            api_base=api_base,
            api_key=api_key,
            models=models,
            voices=voices,
        )
        if not success:
            self.tts_feedback_label.setText(error_message or "Could not save TTS provider.")
            return

        self._refresh_tts_provider_dropdown(resolved_provider_id)
        self.tts_feedback_label.setText(f"Saved TTS provider '{provider_name or resolved_provider_id}'.")

    def _on_remove_tts_provider(self):
        provider_id = str(self.tts_provider_combo.currentData() or "")
        if not provider_id:
            self.tts_feedback_label.setText("Select a custom TTS provider to remove.")
            return

        success, error_message = self.logic.remove_tts_provider(provider_id)
        if not success:
            self.tts_feedback_label.setText(error_message or "Could not remove TTS provider.")
            return

        self._refresh_tts_provider_dropdown("")
        self.tts_feedback_label.setText(f"Removed TTS provider '{provider_id}'.")

    def _on_test_tts_provider(self):
        provider_id = str(self.tts_provider_combo.currentData() or "").strip()
        if not provider_id:
            self.tts_feedback_label.setText("Save and select a provider before testing connection.")
            return

        success, message = self.logic.test_tts_provider_connection(provider_id)
        if success:
            self.tts_feedback_label.setText(message or "Connection successful.")
            return

        self.tts_feedback_label.setText(message or "Connection failed.")

    def _on_discover_tts_provider_catalog(self):
        provider_id = str(self.tts_provider_combo.currentData() or "").strip()
        if not provider_id:
            self.tts_feedback_label.setText("Save and select a provider before discovery.")
            return

        success, models, voices, message = self.logic.discover_tts_provider_catalog(provider_id)
        if not success:
            self.tts_feedback_label.setText(message or "Could not discover provider catalog.")
            return

        self.tts_provider_models_edit.setPlainText(self._format_items(models))
        self.tts_provider_voices_edit.setPlainText(self._format_items(voices))
        self.tts_feedback_label.setText(
            f"{message} Review and click Save to persist discovered values."
        )

    def update_ui_from_state(self):
        self._refresh_llm_provider_dropdown(str(self.llm_provider_combo.currentData() or ""))
        self._refresh_tts_provider_dropdown(str(self.tts_provider_combo.currentData() or ""))
