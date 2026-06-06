from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSpinBox,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


DEFAULT_LITELLM_MODEL = "openai/gpt-5.4-mini"


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

        self.combined_label = self._create_group_label("LLM Prompt")
        self.combined_frame = self._create_prompt_frame("combined_prompt")
        main_layout.addWidget(self.combined_label)
        main_layout.addWidget(self.combined_frame)

        self.first_label = self._create_group_label("Stage 1 Prompt")
        self.first_frame = self._create_prompt_frame("first_prompt")
        main_layout.addWidget(self.first_label)
        main_layout.addWidget(self.first_frame)

        self.second_label = self._create_group_label("Stage 2 Prompt")
        self.second_frame = self._create_prompt_frame("second_prompt")
        main_layout.addWidget(self.second_label)
        main_layout.addWidget(self.second_frame)

        self.third_label = self._create_group_label("Stage 3 Prompt")
        self.third_frame = self._create_prompt_frame("third_prompt")
        main_layout.addWidget(self.third_label)
        main_layout.addWidget(self.third_frame)

        main_layout.addStretch(1)

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
        self.remove_quotation_marks_checkbox = QCheckBox("Remove Quotation Marks")
        self.disable_paragraph_detection_checkbox = QCheckBox("Disable Paragraph Detection")

        layout.addWidget(self.split_sentences_checkbox, 0, 0)
        layout.addWidget(QLabel("Max Sentence Length:"), 1, 0)
        layout.addWidget(self.max_length_spinbox, 1, 1)
        layout.addWidget(self.append_sentences_checkbox, 2, 0)
        layout.addWidget(self.remove_diacritics_checkbox, 3, 0)
        layout.addWidget(self.remove_quotation_marks_checkbox, 4, 0)
        layout.addWidget(self.disable_paragraph_detection_checkbox, 5, 0)

        return frame

    def _create_llm_processing_frame(self):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.enable_llm_checkbox = QCheckBox("Enable LLM Processing")
        self.load_models_button = QPushButton("Refresh Built-in Models")
        self.use_multi_stage_checkbox = QCheckBox("Divide into Multiple Stages")

        self.default_llm_model_combo = QComboBox()
        self.default_llm_model_combo.setEditable(True)
        self.default_llm_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        default_model_line_edit = self.default_llm_model_combo.lineEdit()
        if default_model_line_edit is not None:
            default_model_line_edit.setPlaceholderText(DEFAULT_LITELLM_MODEL)

        self.reasoning_effort_combo = QComboBox()
        self.reasoning_effort_combo.addItems(["None (model default)", "Low", "Medium", "High"])
        self.reasoning_effort_combo.setToolTip(
            "Reasoning effort to request from the LLM.\n"
            "'None' sends no reasoning_effort parameter — the model uses its own default.\n"
            "Low/Medium/High map to reasoning_effort=low/medium/high via LiteLLM, which\n"
            "translates them for each provider (OpenAI, Anthropic, Gemini, etc.).\n"
            "Providers that don't support reasoning_effort will silently ignore it."
        )

        self.providers_hint = QLabel(
            "Manage LLM providers and model catalogs in the Providers tab."
        )
        self.providers_hint.setWordWrap(True)

        self.llm_feedback_label = QLabel("")
        self.llm_feedback_label.setWordWrap(True)

        layout.addWidget(self.enable_llm_checkbox, 0, 0)
        layout.addWidget(self.load_models_button, 0, 1, 1, 2)
        layout.addWidget(self.use_multi_stage_checkbox, 1, 0, 1, 3)

        layout.addWidget(QLabel("Default LiteLLM Model:"), 2, 0)
        layout.addWidget(self.default_llm_model_combo, 2, 1, 1, 2)

        layout.addWidget(QLabel("Reasoning Effort:"), 3, 0)
        layout.addWidget(self.reasoning_effort_combo, 3, 1)

        layout.addWidget(self.providers_hint, 4, 0, 1, 3)
        layout.addWidget(self.llm_feedback_label, 5, 0, 1, 3)

        return frame

    def _create_prompt_frame(self, prompt_key):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QVBoxLayout(frame)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        prompt_edit = QTextEdit()
        prompt_edit.setMinimumHeight(120)
        prompt_edit.setPlaceholderText("Enter LLM prompt text here...")

        options_widget = QWidget()
        options_layout = QHBoxLayout(options_widget)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.setSpacing(16)

        enable_checkbox = QCheckBox("Enable Prompt")
        evaluate_checkbox = QCheckBox("Enable Evaluation")
        options_layout.addWidget(enable_checkbox)
        options_layout.addWidget(evaluate_checkbox)

        options_layout.addStretch(1)

        model_label = QLabel("Model:")
        model_combo = QComboBox()
        model_combo.addItem("default")
        model_combo.setEditable(True)
        model_combo.setMinimumWidth(180)
        model_combo.setMaximumWidth(250)

        options_layout.addWidget(model_label)
        options_layout.addWidget(model_combo)

        layout.addWidget(prompt_edit, 1)
        layout.addWidget(options_widget)

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
        self.remove_quotation_marks_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.text_processing,
                "remove_quotation_marks",
                self.remove_quotation_marks_checkbox.isChecked(),
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
        self.use_multi_stage_checkbox.stateChanged.connect(self._on_use_multi_stage_changed)
        self.reasoning_effort_combo.currentIndexChanged.connect(self._on_reasoning_effort_changed)

        # Prompts
        for key in ["combined_prompt", "first_prompt", "second_prompt", "third_prompt"]:
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

    def _set_feedback(self, message: str, is_error: bool = False):
        self.llm_feedback_label.setText(message)
        self.llm_feedback_label.setStyleSheet("")

    def _update_prompt_visibility(self):
        use_multi = self.logic.state.llm.use_multi_stage
        self.combined_label.setVisible(not use_multi)
        self.combined_frame.setVisible(not use_multi)
        self.first_label.setVisible(use_multi)
        self.first_frame.setVisible(use_multi)
        self.second_label.setVisible(use_multi)
        self.second_frame.setVisible(use_multi)
        self.third_label.setVisible(use_multi)
        self.third_frame.setVisible(use_multi)

    def _on_use_multi_stage_changed(self):
        self.logic.state.llm.use_multi_stage = self.use_multi_stage_checkbox.isChecked()
        self._update_prompt_visibility()

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

        for key in ["combined_prompt", "first_prompt", "second_prompt", "third_prompt"]:
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

    def _on_reasoning_effort_changed(self, index: int):
        # 0 = None (empty), 1 = low, 2 = medium, 3 = high
        mapping = ["", "low", "medium", "high"]
        value = mapping[index] if 0 <= index < len(mapping) else ""
        if hasattr(self.logic.state.llm, "reasoning_effort"):
            self.logic.state.llm.reasoning_effort = value
        # Persist immediately — this is a global setting.
        persist = getattr(self.logic, "_persist_global_settings", None)
        if callable(persist):
            try:
                persist()
            except Exception:
                pass

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
        self.remove_quotation_marks_checkbox.setChecked(tp_state.remove_quotation_marks)
        self.disable_paragraph_detection_checkbox.setChecked(tp_state.disable_paragraph_detection)

        # LLM Settings
        llm_state = self.logic.state.llm
        self.enable_llm_checkbox.setChecked(llm_state.processing_enabled)

        self.use_multi_stage_checkbox.blockSignals(True)
        self.use_multi_stage_checkbox.setChecked(llm_state.use_multi_stage)
        self.use_multi_stage_checkbox.blockSignals(False)

        # Reasoning effort combo
        effort_map = {"": 0, "low": 1, "medium": 2, "high": 3}
        effort_value = str(getattr(llm_state, "reasoning_effort", "") or "")
        effort_index = effort_map.get(effort_value, 0)
        self.reasoning_effort_combo.blockSignals(True)
        self.reasoning_effort_combo.setCurrentIndex(effort_index)
        self.reasoning_effort_combo.blockSignals(False)

        self._update_prompt_visibility()
        self._update_model_dropdowns()

        # Prompts
        for key in ["combined_prompt", "first_prompt", "second_prompt", "third_prompt"]:
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

            model_combo = getattr(self, f"{key}_model_combo")
            target_model = prompt_state.model or "default"
            if model_combo.currentText() != target_model:
                model_combo.blockSignals(True)
                model_combo.setCurrentText(target_model)
                model_combo.blockSignals(False)
