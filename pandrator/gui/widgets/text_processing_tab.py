import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


DEFAULT_LITELLM_MODEL = "openai/gpt-4o-mini"


class TextProcessingTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        main_layout.addWidget(self._create_group_label("General Settings"))
        main_layout.addWidget(self._create_general_settings_frame())

        main_layout.addWidget(self._create_group_label("LLM Processing"))
        main_layout.addWidget(self._create_llm_processing_frame())

        main_layout.addWidget(self._create_group_label("First Prompt"))
        main_layout.addWidget(self._create_prompt_frame("first_prompt"))

        main_layout.addWidget(self._create_group_label("Second Prompt"))
        main_layout.addWidget(self._create_prompt_frame("second_prompt"))

        main_layout.addWidget(self._create_group_label("Third Prompt"))
        main_layout.addWidget(self._create_prompt_frame("third_prompt"))

        self._connect_signals()
        self.update_ui_from_state()
        self.logic.state_changed.connect(self.update_ui_from_state)

    def _create_group_label(self, text):
        label = QLabel(text)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        return label

    def _create_general_settings_frame(self):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.split_sentences_checkbox = QCheckBox("Split Long Sentences")
        self.max_length_spinbox = QSpinBox()
        self.max_length_spinbox.setRange(50, 500)
        self.append_sentences_checkbox = QCheckBox("Append Short Sentences")
        self.remove_diacritics_checkbox = QCheckBox("Remove Diacritics")
        self.disable_paragraph_detection_checkbox = QCheckBox("Disable Paragraph Detection")

        layout.addWidget(self.split_sentences_checkbox, 0, 0)
        layout.addWidget(QLabel("Max Sentence Length:"), 1, 0)
        layout.addWidget(self.max_length_spinbox, 1, 1)
        layout.addWidget(self.append_sentences_checkbox, 2, 0)
        layout.addWidget(self.remove_diacritics_checkbox, 3, 0)
        layout.addWidget(self.disable_paragraph_detection_checkbox, 4, 0)

        return frame

    def _create_llm_processing_frame(self):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.enable_llm_checkbox = QCheckBox("Enable LLM Processing")
        self.load_models_button = QPushButton("Refresh Built-in Models")

        self.default_llm_model_combo = QComboBox()
        self.default_llm_model_combo.setEditable(True)
        self.default_llm_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        default_model_line_edit = self.default_llm_model_combo.lineEdit()
        if default_model_line_edit is not None:
            default_model_line_edit.setPlaceholderText(DEFAULT_LITELLM_MODEL)

        self.custom_provider_combo = QComboBox()
        self.custom_provider_name_edit = QLineEdit()
        self.custom_provider_name_edit.setPlaceholderText("myollama")
        self.custom_provider_api_base_edit = QLineEdit()
        self.custom_provider_api_base_edit.setPlaceholderText("http://localhost:11434/v1")
        self.custom_provider_api_key_edit = QLineEdit()
        self.custom_provider_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.custom_provider_api_key_edit.setPlaceholderText("Optional API key")
        self.custom_provider_models_edit = QLineEdit()
        self.custom_provider_models_edit.setPlaceholderText("llama3.1:8b, qwen2.5:14b")

        self.save_custom_provider_button = QPushButton("Save Custom Provider")
        self.remove_custom_provider_button = QPushButton("Remove Custom Provider")

        self.custom_provider_hint = QLabel(
            "Built-in providers: OpenAI, Gemini, Anthropic. "
            "Add your own OpenAI-compatible endpoint and save model IDs for dropdown use."
        )
        self.custom_provider_hint.setWordWrap(True)

        self.llm_feedback_label = QLabel("")
        self.llm_feedback_label.setWordWrap(True)

        layout.addWidget(self.enable_llm_checkbox, 0, 0)
        layout.addWidget(self.load_models_button, 0, 1, 1, 2)

        layout.addWidget(QLabel("Default LiteLLM Model:"), 1, 0)
        layout.addWidget(self.default_llm_model_combo, 1, 1, 1, 2)

        layout.addWidget(QLabel("Custom Provider:"), 2, 0)
        layout.addWidget(self.custom_provider_combo, 2, 1)
        layout.addWidget(self.remove_custom_provider_button, 2, 2)

        layout.addWidget(QLabel("Provider Name:"), 3, 0)
        layout.addWidget(self.custom_provider_name_edit, 3, 1, 1, 2)

        layout.addWidget(QLabel("API Base URL:"), 4, 0)
        layout.addWidget(self.custom_provider_api_base_edit, 4, 1, 1, 2)

        layout.addWidget(QLabel("API Key (optional):"), 5, 0)
        layout.addWidget(self.custom_provider_api_key_edit, 5, 1, 1, 2)

        layout.addWidget(QLabel("Saved Models (comma-separated):"), 6, 0)
        layout.addWidget(self.custom_provider_models_edit, 6, 1, 1, 2)

        layout.addWidget(self.save_custom_provider_button, 7, 1, 1, 2)
        layout.addWidget(self.custom_provider_hint, 8, 0, 1, 3)
        layout.addWidget(self.llm_feedback_label, 9, 0, 1, 3)

        return frame

    def _create_prompt_frame(self, prompt_key):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        prompt_edit = QTextEdit()
        prompt_edit.setFixedHeight(100)
        enable_checkbox = QCheckBox("Enable Prompt")
        evaluate_checkbox = QCheckBox("Enable Evaluation")
        model_combo = QComboBox()
        model_combo.addItem("default")
        model_combo.setEditable(True)

        layout.addWidget(prompt_edit, 0, 0, 1, 2)
        layout.addWidget(enable_checkbox, 1, 0)
        layout.addWidget(evaluate_checkbox, 1, 1)
        layout.addWidget(QLabel("Model:"), 2, 0)
        layout.addWidget(model_combo, 2, 1)

        setattr(self, f"{prompt_key}_edit", prompt_edit)
        setattr(self, f"{prompt_key}_enable_checkbox", enable_checkbox)
        setattr(self, f"{prompt_key}_evaluate_checkbox", evaluate_checkbox)
        setattr(self, f"{prompt_key}_model_combo", model_combo)

        return frame

    def _connect_signals(self):
        # General Settings
        self.split_sentences_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.text_processing,
                "enable_sentence_splitting",
                self.split_sentences_checkbox.isChecked(),
            )
        )
        self.max_length_spinbox.valueChanged.connect(
            lambda value: setattr(self.logic.state.text_processing, "max_sentence_length", value)
        )
        self.append_sentences_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.text_processing,
                "enable_sentence_appending",
                self.append_sentences_checkbox.isChecked(),
            )
        )
        self.remove_diacritics_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.text_processing,
                "remove_diacritics",
                self.remove_diacritics_checkbox.isChecked(),
            )
        )
        self.disable_paragraph_detection_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.text_processing,
                "disable_paragraph_detection",
                self.disable_paragraph_detection_checkbox.isChecked(),
            )
        )

        # LLM Settings
        self.enable_llm_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.llm,
                "processing_enabled",
                self.enable_llm_checkbox.isChecked(),
            )
        )
        self.load_models_button.clicked.connect(self._on_load_llm_models)
        self.default_llm_model_combo.currentTextChanged.connect(self._on_default_model_changed)
        self.custom_provider_combo.currentIndexChanged.connect(
            self._on_custom_provider_selected
        )
        self.save_custom_provider_button.clicked.connect(self._on_save_custom_provider)
        self.remove_custom_provider_button.clicked.connect(self._on_remove_custom_provider)

        # Prompts
        for key in ["first_prompt", "second_prompt", "third_prompt"]:
            getattr(self, f"{key}_edit").textChanged.connect(
                lambda key=key: self._on_prompt_changed(key)
            )
            getattr(self, f"{key}_enable_checkbox").stateChanged.connect(
                lambda _, key=key: self._on_prompt_setting_changed(key)
            )
            getattr(self, f"{key}_evaluate_checkbox").stateChanged.connect(
                lambda _, key=key: self._on_prompt_setting_changed(key)
            )
            getattr(self, f"{key}_model_combo").currentTextChanged.connect(
                lambda _, key=key: self._on_prompt_setting_changed(key)
            )

    @staticmethod
    def _parse_models_text(raw_text: str) -> list[str]:
        chunks = re.split(r"[,\n;]", str(raw_text or ""))
        seen: set[str] = set()
        models: list[str] = []
        for chunk in chunks:
            model = chunk.strip()
            if model and model not in seen:
                models.append(model)
                seen.add(model)
        return models

    def _set_feedback(self, message: str, is_error: bool = False):
        self.llm_feedback_label.setText(message)
        self.llm_feedback_label.setStyleSheet("")

    def _all_provider_configs(self) -> list[dict]:
        return self.logic.list_llm_provider_configs()

    def _custom_provider_configs(self) -> list[dict]:
        return [
            provider
            for provider in self._all_provider_configs()
            if provider.get("is_custom", False)
        ]

    def _refresh_custom_provider_dropdown(self, target_provider_id: str = ""):
        if not target_provider_id:
            target_provider_id = str(self.custom_provider_combo.currentData() or "")

        providers = self._custom_provider_configs()
        self.custom_provider_combo.blockSignals(True)
        self.custom_provider_combo.clear()
        self.custom_provider_combo.addItem("New Custom Provider", "")
        for provider in providers:
            self.custom_provider_combo.addItem(
                str(provider.get("name") or provider.get("id") or ""),
                str(provider.get("id") or ""),
            )

        index = self.custom_provider_combo.findData(target_provider_id)
        self.custom_provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.custom_provider_combo.blockSignals(False)

        selected_provider_id = str(self.custom_provider_combo.currentData() or "")
        self._load_custom_provider_into_form(selected_provider_id)

    def _load_custom_provider_into_form(self, provider_id: str):
        provider = None
        for item in self._custom_provider_configs():
            if str(item.get("id") or "") == provider_id:
                provider = item
                break

        if provider is None:
            self.custom_provider_name_edit.setText("")
            self.custom_provider_api_base_edit.setText("")
            self.custom_provider_api_key_edit.setText("")
            self.custom_provider_models_edit.setText("")
            self.remove_custom_provider_button.setEnabled(False)
            return

        self.custom_provider_name_edit.setText(str(provider.get("name") or ""))
        self.custom_provider_api_base_edit.setText(str(provider.get("api_base") or ""))
        self.custom_provider_api_key_edit.setText(str(provider.get("api_key") or ""))
        model_list = provider.get("models") or []
        if isinstance(model_list, list):
            models_text = ", ".join(str(model).strip() for model in model_list if str(model).strip())
        else:
            models_text = ""
        self.custom_provider_models_edit.setText(models_text)
        self.remove_custom_provider_button.setEnabled(True)

    def _update_model_dropdowns(self):
        models = self.logic.list_llm_models()
        if not models:
            models = ["default"]

        llm_state = self.logic.state.llm
        configured_default = (llm_state.default_model or "").strip() or DEFAULT_LITELLM_MODEL
        if configured_default not in models:
            models.insert(1 if models and models[0] == "default" else 0, configured_default)

        self.default_llm_model_combo.blockSignals(True)
        self.default_llm_model_combo.clear()
        self.default_llm_model_combo.addItems(models)
        if self.default_llm_model_combo.findText(configured_default) == -1:
            self.default_llm_model_combo.addItem(configured_default)
        self.default_llm_model_combo.setCurrentText(configured_default)
        self.default_llm_model_combo.blockSignals(False)

        for key in ["first_prompt", "second_prompt", "third_prompt"]:
            combo = getattr(self, f"{key}_model_combo")
            target_model = getattr(llm_state, key).model or "default"
            combo.blockSignals(True)
            combo.clear()
            combo.addItems(models)
            if combo.findText(target_model) == -1:
                combo.addItem(target_model)
            combo.setCurrentText(target_model)
            combo.blockSignals(False)

    def _on_load_llm_models(self):
        status_lines = self.logic.refresh_llm_builtin_models()
        self._update_model_dropdowns()
        if status_lines:
            self._set_feedback(" ".join(status_lines), is_error=False)
        else:
            self._set_feedback("Finished refreshing built-in model catalogs.", is_error=False)

    def _on_default_model_changed(self, value: str):
        normalized = value.strip() or DEFAULT_LITELLM_MODEL
        self.logic.state.llm.default_model = normalized

    def _on_custom_provider_selected(self):
        provider_id = str(self.custom_provider_combo.currentData() or "")
        self._load_custom_provider_into_form(provider_id)

    def _on_save_custom_provider(self):
        name = self.custom_provider_name_edit.text().strip()
        api_base = self.custom_provider_api_base_edit.text().strip()
        api_key = self.custom_provider_api_key_edit.text().strip()
        models = self._parse_models_text(self.custom_provider_models_edit.text())

        success, provider_id, error_message = self.logic.save_llm_custom_provider(
            provider_name=name,
            api_base=api_base,
            api_key=api_key,
            models=models,
        )
        if not success:
            self._set_feedback(error_message or "Could not save custom provider.", is_error=True)
            return

        self._refresh_custom_provider_dropdown(provider_id)
        self._update_model_dropdowns()
        self._set_feedback(f"Saved custom provider '{name}'.")

    def _on_remove_custom_provider(self):
        provider_id = str(self.custom_provider_combo.currentData() or "")
        if not provider_id:
            self._set_feedback("Select a custom provider to remove.", is_error=True)
            return

        success, error_message = self.logic.remove_llm_custom_provider(provider_id)
        if not success:
            self._set_feedback(error_message or "Could not remove custom provider.", is_error=True)
            return

        removed_prefix = f"custom:{provider_id}/"
        llm_state = self.logic.state.llm
        if str(llm_state.default_model or "").startswith(removed_prefix):
            llm_state.default_model = DEFAULT_LITELLM_MODEL

        for key in ["first_prompt", "second_prompt", "third_prompt"]:
            prompt_state = getattr(llm_state, key)
            if str(prompt_state.model or "").startswith(removed_prefix):
                prompt_state.model = "default"

        self.logic.state_changed.emit()

        self._refresh_custom_provider_dropdown("")
        self._update_model_dropdowns()
        self._set_feedback(f"Removed custom provider '{provider_id}'.")

    def _on_prompt_changed(self, key):
        prompt_state = getattr(self.logic.state.llm, key)
        prompt_state.prompt_text = getattr(self, f"{key}_edit").toPlainText()

    def _on_prompt_setting_changed(self, key):
        prompt_state = getattr(self.logic.state.llm, key)
        prompt_state.enabled = getattr(self, f"{key}_enable_checkbox").isChecked()
        prompt_state.evaluation_enabled = getattr(self, f"{key}_evaluate_checkbox").isChecked()
        prompt_state.model = getattr(self, f"{key}_model_combo").currentText().strip() or "default"

    def update_ui_from_state(self):
        # General Settings
        tp_state = self.logic.state.text_processing
        self.split_sentences_checkbox.setChecked(tp_state.enable_sentence_splitting)
        self.max_length_spinbox.setValue(tp_state.max_sentence_length)
        self.append_sentences_checkbox.setChecked(tp_state.enable_sentence_appending)
        self.remove_diacritics_checkbox.setChecked(tp_state.remove_diacritics)
        self.disable_paragraph_detection_checkbox.setChecked(tp_state.disable_paragraph_detection)

        # LLM Settings
        llm_state = self.logic.state.llm
        self.enable_llm_checkbox.setChecked(llm_state.processing_enabled)
        selected_custom_provider = str(self.custom_provider_combo.currentData() or "")
        self._refresh_custom_provider_dropdown(selected_custom_provider)
        self._update_model_dropdowns()

        # Prompts
        for key in ["first_prompt", "second_prompt", "third_prompt"]:
            prompt_state = getattr(llm_state, key)
            prompt_edit = getattr(self, f"{key}_edit")
            if prompt_edit.toPlainText() != prompt_state.prompt_text:
                prompt_edit.blockSignals(True)
                prompt_edit.setPlainText(prompt_state.prompt_text)
                prompt_edit.blockSignals(False)

            enable_checkbox = getattr(self, f"{key}_enable_checkbox")
            if enable_checkbox.isChecked() != prompt_state.enabled:
                enable_checkbox.setChecked(prompt_state.enabled)

            evaluate_checkbox = getattr(self, f"{key}_evaluate_checkbox")
            if evaluate_checkbox.isChecked() != prompt_state.evaluation_enabled:
                evaluate_checkbox.setChecked(prompt_state.evaluation_enabled)
