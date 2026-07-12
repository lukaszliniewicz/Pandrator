from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QGroupBox,
    QLabel,
    QLineEdit,
    QScrollArea,
    QSpinBox,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...logic.dubbing.stt_backends import (
    STT_BACKEND_LABELS,
    STT_BACKEND_PARAKEET_ONNX,
    STT_BACKEND_WHISPERX,
    normalize_stt_backend,
)


def _state_number(dubbing_state, field_name: str, default):
    value = getattr(dubbing_state, field_name, None)
    return default if value is None else value


class DubbingAdvancedDialog(QDialog):
    def __init__(self, dubbing_state, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Advanced Dubbing Settings")
        self.resize(680, 580)
        self.stt_backend = normalize_stt_backend(
            getattr(dubbing_state, "stt_backend", "")
        )

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)

        self._build_transcription_tab(dubbing_state)
        self._build_llm_tab(dubbing_state)
        self._build_sync_tab(dubbing_state)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _build_transcription_tab(self, dubbing_state):
        tab = QWidget(self)
        outer_layout = QVBoxLayout(tab)
        scroll_area = QScrollArea(tab)
        scroll_area.setWidgetResizable(True)
        form_container = QWidget(scroll_area)
        content_layout = QVBoxLayout(form_container)
        scroll_area.setWidget(form_container)
        outer_layout.addWidget(scroll_area)

        backend_name = STT_BACKEND_LABELS.get(self.stt_backend, self.stt_backend)
        self.backend_summary_label = QLabel(f"Backend: {backend_name}", form_container)
        content_layout.addWidget(self.backend_summary_label)

        shared_group = QGroupBox("Shared subtitle output", form_container)
        shared_form = QFormLayout(shared_group)
        self.merge_threshold_spin = QSpinBox(shared_group)
        self.merge_threshold_spin.setRange(0, 5000)
        self.merge_threshold_spin.setSingleStep(25)
        self.merge_threshold_spin.setValue(
            int(_state_number(dubbing_state, "subtitle_merge_threshold", 250))
        )
        shared_form.addRow("Merge threshold (ms):", self.merge_threshold_spin)
        content_layout.addWidget(shared_group)

        self.whisper_group = QGroupBox("WhisperX", form_container)
        whisper_form = QFormLayout(self.whisper_group)
        self.whisper_prompt_edit = QTextEdit(self.whisper_group)
        self.whisper_prompt_edit.setPlainText(
            str(getattr(dubbing_state, "whisper_prompt", "") or "")
        )
        self.whisper_prompt_edit.setMinimumHeight(110)
        whisper_form.addRow("Initial prompt:", self.whisper_prompt_edit)

        self.align_model_edit = QLineEdit(self.whisper_group)
        self.align_model_edit.setText(
            str(getattr(dubbing_state, "whisper_align_model", "") or "")
        )
        whisper_form.addRow("Align model:", self.align_model_edit)

        self.chunk_size_spin = QSpinBox(self.whisper_group)
        self.chunk_size_spin.setRange(1, 120)
        self.chunk_size_spin.setValue(
            int(_state_number(dubbing_state, "whisper_chunk_size", 15))
        )
        whisper_form.addRow("Chunk size:", self.chunk_size_spin)

        self.diarization_check = QCheckBox("Enable diarization", self.whisper_group)
        self.diarization_check.setChecked(
            bool(getattr(dubbing_state, "diarization_enabled", False))
        )
        whisper_form.addRow("", self.diarization_check)

        self.boundary_correction_check = QCheckBox(
            "Enable automatic boundary correction",
            self.whisper_group,
        )
        self.boundary_correction_check.setChecked(
            bool(getattr(dubbing_state, "boundary_correction_enabled", False))
        )
        whisper_form.addRow("", self.boundary_correction_check)

        self.save_txt_check = QCheckBox("Save WhisperX TXT output", self.whisper_group)
        self.save_txt_check.setChecked(
            bool(getattr(dubbing_state, "whisper_save_txt", False))
        )
        whisper_form.addRow("", self.save_txt_check)
        content_layout.addWidget(self.whisper_group)

        self.parakeet_group = QGroupBox("ONNX Parakeet", form_container)
        parakeet_layout = QVBoxLayout(self.parakeet_group)
        parakeet_form_container = QWidget(self.parakeet_group)
        parakeet_form = QFormLayout(parakeet_form_container)

        self.parakeet_model_edit = QLineEdit(parakeet_form_container)
        self.parakeet_model_edit.setText(
            str(
                getattr(dubbing_state, "parakeet_model", "")
                or "nemo-parakeet-tdt-0.6b-v3"
            )
        )
        parakeet_form.addRow("Model:", self.parakeet_model_edit)

        self.parakeet_quant_combo = QComboBox(parakeet_form_container)
        self.parakeet_quant_combo.addItem("FP32", "")
        self.parakeet_quant_combo.addItem("int8", "int8")
        quantization = str(getattr(dubbing_state, "parakeet_quantization", "") or "")
        quant_index = self.parakeet_quant_combo.findData(quantization)
        if quant_index >= 0:
            self.parakeet_quant_combo.setCurrentIndex(quant_index)
        parakeet_form.addRow("Quantization:", self.parakeet_quant_combo)
        parakeet_layout.addWidget(parakeet_form_container)

        self.parakeet_vad_check = QGroupBox("Enable Silero VAD", self.parakeet_group)
        self.parakeet_vad_check.setCheckable(True)
        self.parakeet_vad_check.setChecked(
            bool(getattr(dubbing_state, "parakeet_vad_enabled", True))
        )
        vad_form = QFormLayout(self.parakeet_vad_check)

        self.parakeet_vad_max_speech_spin = QDoubleSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_max_speech_spin.setRange(1.0, 120.0)
        self.parakeet_vad_max_speech_spin.setSingleStep(1.0)
        self.parakeet_vad_max_speech_spin.setDecimals(1)
        self.parakeet_vad_max_speech_spin.setValue(
            float(_state_number(dubbing_state, "parakeet_vad_max_speech_seconds", 15.0))
        )
        vad_form.addRow("Maximum speech (s):", self.parakeet_vad_max_speech_spin)

        self.parakeet_vad_threshold_spin = QDoubleSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_threshold_spin.setRange(0.01, 0.99)
        self.parakeet_vad_threshold_spin.setSingleStep(0.05)
        self.parakeet_vad_threshold_spin.setDecimals(2)
        self.parakeet_vad_threshold_spin.setValue(
            float(_state_number(dubbing_state, "parakeet_vad_threshold", 0.5))
        )
        vad_form.addRow("Speech threshold:", self.parakeet_vad_threshold_spin)

        self.parakeet_vad_neg_threshold_spin = QDoubleSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_neg_threshold_spin.setRange(0.0, 0.99)
        self.parakeet_vad_neg_threshold_spin.setSingleStep(0.05)
        self.parakeet_vad_neg_threshold_spin.setDecimals(2)
        self.parakeet_vad_neg_threshold_spin.setValue(
            float(_state_number(dubbing_state, "parakeet_vad_neg_threshold", 0.0))
        )
        vad_form.addRow("Negative threshold:", self.parakeet_vad_neg_threshold_spin)

        self.parakeet_vad_min_silence_spin = QDoubleSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_min_silence_spin.setRange(0.0, 5000.0)
        self.parakeet_vad_min_silence_spin.setSingleStep(50.0)
        self.parakeet_vad_min_silence_spin.setDecimals(0)
        self.parakeet_vad_min_silence_spin.setValue(
            float(_state_number(dubbing_state, "parakeet_vad_min_silence_ms", 100.0))
        )
        vad_form.addRow("Minimum silence (ms):", self.parakeet_vad_min_silence_spin)

        self.parakeet_vad_min_speech_spin = QDoubleSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_min_speech_spin.setRange(0.0, 5000.0)
        self.parakeet_vad_min_speech_spin.setSingleStep(50.0)
        self.parakeet_vad_min_speech_spin.setDecimals(0)
        self.parakeet_vad_min_speech_spin.setValue(
            float(_state_number(dubbing_state, "parakeet_vad_min_speech_ms", 250.0))
        )
        vad_form.addRow("Minimum speech (ms):", self.parakeet_vad_min_speech_spin)

        self.parakeet_vad_speech_pad_spin = QDoubleSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_speech_pad_spin.setRange(0.0, 1000.0)
        self.parakeet_vad_speech_pad_spin.setSingleStep(10.0)
        self.parakeet_vad_speech_pad_spin.setDecimals(0)
        self.parakeet_vad_speech_pad_spin.setValue(
            float(_state_number(dubbing_state, "parakeet_vad_speech_pad_ms", 30.0))
        )
        vad_form.addRow("Speech padding (ms):", self.parakeet_vad_speech_pad_spin)

        self.parakeet_vad_batch_spin = QSpinBox(self.parakeet_vad_check)
        self.parakeet_vad_batch_spin.setRange(1, 64)
        self.parakeet_vad_batch_spin.setValue(
            int(_state_number(dubbing_state, "parakeet_vad_batch_size", 8))
        )
        vad_form.addRow("Batch size:", self.parakeet_vad_batch_spin)
        parakeet_layout.addWidget(self.parakeet_vad_check)

        self.parakeet_save_txt_check = QCheckBox(
            "Save Parakeet TXT output",
            self.parakeet_group,
        )
        self.parakeet_save_txt_check.setChecked(
            bool(getattr(dubbing_state, "parakeet_save_txt", False))
        )
        parakeet_layout.addWidget(self.parakeet_save_txt_check)
        content_layout.addWidget(self.parakeet_group)
        content_layout.addStretch(1)

        self.whisper_group.setVisible(self.stt_backend == STT_BACKEND_WHISPERX)
        self.parakeet_group.setVisible(self.stt_backend == STT_BACKEND_PARAKEET_ONNX)
        self.tabs.addTab(tab, "Transcription")

    def _build_llm_tab(self, dubbing_state):
        tab = QWidget(self)
        form = QFormLayout(tab)

        self.llm_char_spin = QSpinBox(tab)
        self.llm_char_spin.setRange(500, 50000)
        self.llm_char_spin.setSingleStep(500)
        self.llm_char_spin.setValue(int(_state_number(dubbing_state, "llm_char", 6000)))
        form.addRow("LLM block characters:", self.llm_char_spin)

        self.max_line_length_spin = QSpinBox(tab)
        self.max_line_length_spin.setRange(20, 120)
        self.max_line_length_spin.setValue(
            int(_state_number(dubbing_state, "max_line_length", 42))
        )
        form.addRow("Max subtitle line length:", self.max_line_length_spin)

        self.context_check = QCheckBox("Pass previous block as context", tab)
        self.context_check.setChecked(bool(getattr(dubbing_state, "context", True)))
        form.addRow("", self.context_check)

        self.no_remove_check = QCheckBox("Do not allow subtitle removal", tab)
        self.no_remove_check.setChecked(
            bool(getattr(dubbing_state, "no_remove_subtitles", False))
        )
        form.addRow("", self.no_remove_check)

        self.translate_prompt_edit = QTextEdit(tab)
        self.translate_prompt_edit.setPlainText(
            str(getattr(dubbing_state, "translate_prompt", "") or "")
        )
        self.translate_prompt_edit.setMinimumHeight(140)
        form.addRow("Translation instructions:", self.translate_prompt_edit)
        self.tabs.addTab(tab, "LLM")

    def _build_sync_tab(self, dubbing_state):
        tab = QWidget(self)
        form = QFormLayout(tab)

        self.sync_delay_spin = QSpinBox(tab)
        self.sync_delay_spin.setRange(0, 10000)
        self.sync_delay_spin.setSingleStep(100)
        self.sync_delay_spin.setValue(
            int(_state_number(dubbing_state, "sync_delay_start_ms", 2000))
        )
        form.addRow("Start delay cap (ms):", self.sync_delay_spin)

        self.sync_speed_spin = QSpinBox(tab)
        self.sync_speed_spin.setRange(100, 250)
        self.sync_speed_spin.setSuffix("%")
        self.sync_speed_spin.setValue(
            int(_state_number(dubbing_state, "sync_speed_up_percent", 115))
        )
        form.addRow("Maximum speed-up:", self.sync_speed_spin)
        self.tabs.addTab(tab, "Sync")

    def apply_to_state(self, dubbing_state):
        dubbing_state.whisper_prompt = self.whisper_prompt_edit.toPlainText().strip()
        dubbing_state.whisper_align_model = self.align_model_edit.text().strip()
        dubbing_state.whisper_chunk_size = self.chunk_size_spin.value()
        dubbing_state.subtitle_merge_threshold = self.merge_threshold_spin.value()
        dubbing_state.speech_block_merge_threshold = self.merge_threshold_spin.value()
        dubbing_state.diarization_enabled = self.diarization_check.isChecked()
        dubbing_state.boundary_correction_enabled = self.boundary_correction_check.isChecked()
        dubbing_state.whisper_save_txt = self.save_txt_check.isChecked()
        dubbing_state.parakeet_model = self.parakeet_model_edit.text().strip()
        dubbing_state.parakeet_quantization = self.parakeet_quant_combo.currentData()
        dubbing_state.parakeet_vad_enabled = self.parakeet_vad_check.isChecked()
        dubbing_state.parakeet_vad_max_speech_seconds = self.parakeet_vad_max_speech_spin.value()
        dubbing_state.parakeet_vad_threshold = self.parakeet_vad_threshold_spin.value()
        dubbing_state.parakeet_vad_neg_threshold = self.parakeet_vad_neg_threshold_spin.value()
        dubbing_state.parakeet_vad_min_silence_ms = self.parakeet_vad_min_silence_spin.value()
        dubbing_state.parakeet_vad_min_speech_ms = self.parakeet_vad_min_speech_spin.value()
        dubbing_state.parakeet_vad_speech_pad_ms = self.parakeet_vad_speech_pad_spin.value()
        dubbing_state.parakeet_vad_batch_size = self.parakeet_vad_batch_spin.value()
        dubbing_state.parakeet_save_txt = self.parakeet_save_txt_check.isChecked()
        dubbing_state.llm_char = self.llm_char_spin.value()
        dubbing_state.max_line_length = self.max_line_length_spin.value()
        dubbing_state.context = self.context_check.isChecked()
        dubbing_state.no_remove_subtitles = self.no_remove_check.isChecked()
        dubbing_state.translate_prompt = self.translate_prompt_edit.toPlainText().strip()
        dubbing_state.sync_delay_start_ms = self.sync_delay_spin.value()
        dubbing_state.sync_speed_up_percent = self.sync_speed_spin.value()
