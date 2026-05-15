import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QVBoxLayout, QWidget

from ...constants import SILERO_LANGUAGES, XTTS_LANGUAGES
from ..dialogs.custom_prompt_dialog import CustomPromptDialog
from ..dialogs.metadata_dialog import MetadataDialog
from ..dialogs.paste_text_dialog import PasteTextDialog
from ..dialogs.review_text_dialog import ReviewTextDialog
from .session_sections import (
    AdvancedTtsSettingsSection,
    DubbingSection,
    GenerationSection,
    OutputOptionsSection,
    SessionControlsSection,
    SessionHeader,
    SourceFileSection,
    TtsSettingsSection,
    create_section_label,
)


LEGACY_DUBBING_MODEL_UI_MAP = {
    "haiku": "Custom (LiteLLM)",
    "sonnet": "Sonnet 4.6",
    "sonnet thinking": "Sonnet 4.6",
    "gpt-4o-mini": "GPT 5.4-mini",
    "gpt-4o": "GPT 5.4",
    "gemini-flash": "Gemini 3.0 Flash",
    "gemini-pro": "Gemini 3.1 Pro",
    "deepl": "DeepL",
    "local": "Custom (LiteLLM)",
    "deepseek-r1": "Custom (LiteLLM)",
    "qwq-32b": "Custom (LiteLLM)",
}


class SessionTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        main_layout.setSpacing(8)

        self._build_layout(main_layout)
        self._bind_section_widgets()

        self._connect_signals()
        self.update_ui_from_state()
        self.logic.state_changed.connect(self.update_ui_from_state)
        self.logic.tts_connection_running_changed.connect(
            self._on_tts_connection_running_changed
        )

        self.advanced_tts_frame.setVisible(False)

    def _build_layout(self, main_layout: QVBoxLayout):
        self.header_section = SessionHeader(self)
        main_layout.addWidget(self.header_section)

        self.session_label = create_section_label("Session")
        main_layout.addWidget(self.session_label)
        self.session_section = SessionControlsSection(self)
        main_layout.addWidget(self.session_section)

        self.source_file_label = create_section_label("Source File")
        main_layout.addWidget(self.source_file_label)
        self.source_file_section = SourceFileSection(self)
        main_layout.addWidget(self.source_file_section)

        self.tts_label = create_section_label("TTS Settings")
        main_layout.addWidget(self.tts_label)
        self.tts_section = TtsSettingsSection(self)
        main_layout.addWidget(self.tts_section)

        self.advanced_tts_frame = AdvancedTtsSettingsSection(self)
        main_layout.addWidget(self.advanced_tts_frame)

        self.dubbing_label = create_section_label("Dubbing")
        main_layout.addWidget(self.dubbing_label)
        self.dubbing_frame = DubbingSection(self)
        main_layout.addWidget(self.dubbing_frame)

        self.output_options_label = create_section_label("Output Options")
        main_layout.addWidget(self.output_options_label)
        self.output_options_frame = OutputOptionsSection(self)
        main_layout.addWidget(self.output_options_frame)

        self.generation_label = create_section_label("Generation")
        main_layout.addWidget(self.generation_label)
        self.generation_section = GenerationSection(self)
        main_layout.addWidget(self.generation_section)

    def _bind_section_widgets(self):
        # Header
        self.session_name_label = self.header_section.session_name_label
        self.lifecycle_status_label = self.header_section.lifecycle_status_label

        # Session controls
        self.new_session_button = self.session_section.new_session_button
        self.load_session_button = self.session_section.load_session_button
        self.view_session_folder_button = self.session_section.view_session_folder_button
        self.delete_session_button = self.session_section.delete_session_button

        # Source controls
        self.select_file_button = self.source_file_section.select_file_button
        self.paste_text_button = self.source_file_section.paste_text_button
        self.download_url_button = self.source_file_section.download_url_button
        self.selected_file_label = self.source_file_section.selected_file_label

        # TTS controls
        self.tts_service_combo = self.tts_section.tts_service_combo
        self.connect_server_button = self.tts_section.connect_server_button
        self.use_external_server_checkbox = self.tts_section.use_external_server_checkbox
        self.external_server_url_edit = self.tts_section.external_server_url_edit
        self.xtts_model_label = self.tts_section.xtts_model_label
        self.xtts_model_combo = self.tts_section.xtts_model_combo
        self.language_label = self.tts_section.language_label
        self.language_combo = self.tts_section.language_combo
        self.speaker_label = self.tts_section.speaker_label
        self.speaker_combo = self.tts_section.speaker_combo
        self.upload_voice_button = self.tts_section.upload_voice_button
        self.speed_slider = self.tts_section.speed_slider
        self.speed_label = self.tts_section.speed_label
        self.advanced_tts_checkbox = self.tts_section.advanced_tts_checkbox
        self.cloud_provider_hint = self.tts_section.cloud_provider_hint
        self.openai_audio_instructions_label = self.tts_section.openai_audio_instructions_label
        self.openai_audio_instructions_edit = self.tts_section.openai_audio_instructions_edit

        # Advanced TTS controls
        self.xtts_advanced_settings_frame = self.advanced_tts_frame.xtts_advanced_settings_frame
        self.voxtral_advanced_settings_frame = self.advanced_tts_frame.voxtral_advanced_settings_frame
        self.adv_tts_temp_spinbox = self.advanced_tts_frame.adv_tts_temp_spinbox
        self.adv_tts_len_penalty_spinbox = self.advanced_tts_frame.adv_tts_len_penalty_spinbox
        self.adv_tts_rep_penalty_spinbox = self.advanced_tts_frame.adv_tts_rep_penalty_spinbox
        self.adv_tts_top_k_spinbox = self.advanced_tts_frame.adv_tts_top_k_spinbox
        self.adv_tts_top_p_spinbox = self.advanced_tts_frame.adv_tts_top_p_spinbox
        self.adv_tts_do_sample_checkbox = self.advanced_tts_frame.adv_tts_do_sample_checkbox
        self.adv_tts_num_beams_spinbox = self.advanced_tts_frame.adv_tts_num_beams_spinbox
        self.adv_tts_chunk_size_spinbox = self.advanced_tts_frame.adv_tts_chunk_size_spinbox
        self.adv_tts_text_split_checkbox = self.advanced_tts_frame.adv_tts_text_split_checkbox
        self.adv_tts_gpt_cond_len_spinbox = self.advanced_tts_frame.adv_tts_gpt_cond_len_spinbox
        self.adv_tts_gpt_cond_chunk_len_spinbox = self.advanced_tts_frame.adv_tts_gpt_cond_chunk_len_spinbox
        self.adv_tts_max_ref_len_spinbox = self.advanced_tts_frame.adv_tts_max_ref_len_spinbox
        self.adv_tts_sound_norm_refs_checkbox = self.advanced_tts_frame.adv_tts_sound_norm_refs_checkbox
        self.adv_tts_overlap_wav_len_spinbox = self.advanced_tts_frame.adv_tts_overlap_wav_len_spinbox
        self.adv_tts_apply_button = self.advanced_tts_frame.adv_tts_apply_button
        self.voxtral_max_frames_spinbox = self.advanced_tts_frame.voxtral_max_frames_spinbox
        self.voxtral_euler_steps_spinbox = self.advanced_tts_frame.voxtral_euler_steps_spinbox
        self.voxtral_chunk_checkbox = self.advanced_tts_frame.voxtral_chunk_checkbox
        self.voxtral_max_chunk_chars_spinbox = self.advanced_tts_frame.voxtral_max_chunk_chars_spinbox
        self.voxtral_chunk_silence_ms_spinbox = self.advanced_tts_frame.voxtral_chunk_silence_ms_spinbox
        self.voxtral_strip_quotes_checkbox = self.advanced_tts_frame.voxtral_strip_quotes_checkbox
        self.voxtral_strip_diacritics_checkbox = self.advanced_tts_frame.voxtral_strip_diacritics_checkbox
        self.voxtral_level_audio_checkbox = self.advanced_tts_frame.voxtral_level_audio_checkbox

        # Dubbing controls
        self.transcription_frame = self.dubbing_frame.transcription_frame
        self.video_file_frame = self.dubbing_frame.video_file_frame
        self.dub_whisper_lang_combo = self.dubbing_frame.dub_whisper_lang_combo
        self.dub_whisper_model_combo = self.dubbing_frame.dub_whisper_model_combo
        self.dub_correct_transcription_check = self.dubbing_frame.dub_correct_transcription_check
        self.dub_custom_prompt_button = self.dubbing_frame.dub_custom_prompt_button
        self.dub_translate_check = self.dubbing_frame.dub_translate_check
        self.dub_from_lang_combo = self.dubbing_frame.dub_from_lang_combo
        self.dub_to_lang_combo = self.dubbing_frame.dub_to_lang_combo
        self.dub_cot_check = self.dubbing_frame.dub_cot_check
        self.dub_glossary_check = self.dubbing_frame.dub_glossary_check
        self.dub_trans_model_combo = self.dubbing_frame.dub_trans_model_combo
        self.dub_custom_model_edit = self.dubbing_frame.dub_custom_model_edit
        self.dub_custom_api_base_edit = self.dubbing_frame.dub_custom_api_base_edit
        self.selected_video_file_label = self.dubbing_frame.selected_video_file_label
        self.select_video_file_button = self.dubbing_frame.select_video_file_button
        self.generate_dub_audio_button = self.dubbing_frame.generate_dub_audio_button
        self.add_dub_to_video_button = self.dubbing_frame.add_dub_to_video_button
        self.only_transcribe_button = self.dubbing_frame.only_transcribe_button
        self.only_correct_button = self.dubbing_frame.only_correct_button
        self.only_translate_button = self.dubbing_frame.only_translate_button

        # Output controls
        self.format_combo = self.output_options_frame.format_combo
        self.bitrate_combo = self.output_options_frame.bitrate_combo
        self.upload_cover_button = self.output_options_frame.upload_cover_button
        self.metadata_button = self.output_options_frame.metadata_button

        # Generation controls
        self.start_button = self.generation_section.start_button
        self.resume_button = self.generation_section.resume_button
        self.stop_button = self.generation_section.stop_button
        self.cancel_button = self.generation_section.cancel_button
        self.progress_bar = self.generation_section.progress_bar
        self.progress_label = self.generation_section.progress_label
        self.remaining_time_label = self.generation_section.remaining_time_label

    def _connect_signals(self):
        self._connect_session_signals()
        self._connect_tts_signals()
        self._connect_output_signals()
        self._connect_dubbing_signals()
        self._connect_generation_signals()
        self.logic.progress_updated.connect(self._on_progress_updated)

    def _connect_session_signals(self):
        self.new_session_button.clicked.connect(self._on_new_session)
        self.load_session_button.clicked.connect(self._on_load_session)
        self.view_session_folder_button.clicked.connect(self.logic.view_session_folder)
        self.delete_session_button.clicked.connect(self._on_delete_session)
        self.select_file_button.clicked.connect(self._on_select_file)
        self.paste_text_button.clicked.connect(self._on_paste_text)
        self.download_url_button.clicked.connect(self._on_download_url)

    def _connect_tts_signals(self):
        self.connect_server_button.clicked.connect(self.logic.connect_tts_server)
        self.use_external_server_checkbox.toggled.connect(
            self._on_use_external_server_toggled
        )
        self.external_server_url_edit.textChanged.connect(
            lambda t: setattr(self.logic.state.tts, "external_server_url", t)
        )
        self.tts_service_combo.currentTextChanged.connect(self._on_tts_service_changed)
        self.xtts_model_combo.currentTextChanged.connect(
            lambda t: t and self.logic.on_tts_model_changed(t)
        )
        self.language_combo.currentTextChanged.connect(
            lambda t: t and self.logic.on_tts_language_changed(t)
        )
        self.speaker_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.tts, "speaker", t)
        )
        self.openai_audio_instructions_edit.textChanged.connect(
            lambda t: setattr(self.logic.state.tts, "openai_audio_instructions", t)
        )

        self.speed_slider.valueChanged.connect(self._on_speed_changed)
        self.advanced_tts_checkbox.toggled.connect(self.advanced_tts_frame.setVisible)
        self.upload_voice_button.clicked.connect(self._on_upload_voice)

        self.adv_tts_temp_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "temperature", v)
        )
        self.adv_tts_len_penalty_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "length_penalty", v)
        )
        self.adv_tts_rep_penalty_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "repetition_penalty", v)
        )
        self.adv_tts_top_k_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "top_k", v)
        )
        self.adv_tts_top_p_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "top_p", v)
        )
        self.adv_tts_do_sample_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "do_sample",
                self.adv_tts_do_sample_checkbox.isChecked(),
            )
        )
        self.adv_tts_num_beams_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "num_beams", v)
        )
        self.adv_tts_chunk_size_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "stream_chunk_size", v)
        )
        self.adv_tts_text_split_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "enable_text_splitting",
                self.adv_tts_text_split_checkbox.isChecked(),
            )
        )
        self.adv_tts_gpt_cond_len_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "gpt_cond_len", v)
        )
        self.adv_tts_gpt_cond_chunk_len_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "gpt_cond_chunk_len", v)
        )
        self.adv_tts_max_ref_len_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "max_ref_len", v)
        )
        self.adv_tts_sound_norm_refs_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "sound_norm_refs",
                self.adv_tts_sound_norm_refs_checkbox.isChecked(),
            )
        )
        self.adv_tts_overlap_wav_len_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "overlap_wav_len", v)
        )

        self.voxtral_max_frames_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "voxtral_max_frames", v)
        )
        self.voxtral_euler_steps_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "voxtral_euler_steps", v)
        )
        self.voxtral_chunk_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "voxtral_chunk",
                self.voxtral_chunk_checkbox.isChecked(),
            )
        )
        self.voxtral_max_chunk_chars_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "voxtral_max_chunk_chars", v)
        )
        self.voxtral_chunk_silence_ms_spinbox.valueChanged.connect(
            lambda v: setattr(self.logic.state.tts, "voxtral_chunk_silence_ms", v)
        )
        self.voxtral_strip_quotes_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "voxtral_strip_quotes",
                self.voxtral_strip_quotes_checkbox.isChecked(),
            )
        )
        self.voxtral_strip_diacritics_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "voxtral_strip_diacritics",
                self.voxtral_strip_diacritics_checkbox.isChecked(),
            )
        )
        self.voxtral_level_audio_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "voxtral_level_audio",
                self.voxtral_level_audio_checkbox.isChecked(),
            )
        )
        self.adv_tts_apply_button.clicked.connect(self.logic.save_xtts_settings)

    def _connect_output_signals(self):
        self.format_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.audio_processing, "output_format", t)
        )
        self.bitrate_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.audio_processing, "bitrate", t)
        )
        self.upload_cover_button.clicked.connect(self._on_upload_cover)
        self.metadata_button.clicked.connect(self._on_metadata)

    def _connect_dubbing_signals(self):
        self.dub_whisper_lang_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.dubbing, "whisper_language", t)
        )
        self.dub_whisper_model_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.dubbing, "whisper_model", t)
        )
        self.dub_correct_transcription_check.stateChanged.connect(
            lambda: setattr(
                self.logic.state.dubbing,
                "correction_enabled",
                self.dub_correct_transcription_check.isChecked(),
            )
        )
        self.dub_custom_prompt_button.clicked.connect(self._on_open_custom_prompt)
        self.dub_translate_check.stateChanged.connect(
            lambda: setattr(
                self.logic.state.dubbing,
                "translation_enabled",
                self.dub_translate_check.isChecked(),
            )
        )
        self.dub_from_lang_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.dubbing, "original_language", t)
        )
        self.dub_to_lang_combo.currentTextChanged.connect(
            lambda t: setattr(self.logic.state.dubbing, "target_language", t)
        )
        self.dub_cot_check.stateChanged.connect(
            lambda: setattr(
                self.logic.state.dubbing,
                "chain_of_thought_enabled",
                self.dub_cot_check.isChecked(),
            )
        )
        self.dub_glossary_check.stateChanged.connect(
            lambda: setattr(
                self.logic.state.dubbing,
                "glossary_enabled",
                self.dub_glossary_check.isChecked(),
            )
        )
        self.dub_trans_model_combo.currentTextChanged.connect(
            self._on_dub_translation_model_changed
        )
        self.dub_custom_model_edit.textChanged.connect(
            lambda t: setattr(self.logic.state.dubbing, "custom_translation_model", t)
        )
        self.dub_custom_api_base_edit.textChanged.connect(
            lambda t: setattr(self.logic.state.dubbing, "custom_api_base", t)
        )
        self.select_video_file_button.clicked.connect(self._on_select_video_file)
        self.generate_dub_audio_button.clicked.connect(
            lambda: self.logic.run_dubbing_task("generate_audio")
        )
        self.add_dub_to_video_button.clicked.connect(
            lambda: self.logic.run_dubbing_task("add_to_video")
        )
        self.only_transcribe_button.clicked.connect(
            lambda: self.logic.run_dubbing_task("transcribe")
        )
        self.only_correct_button.clicked.connect(
            lambda: self.logic.run_dubbing_task("correct")
        )
        self.only_translate_button.clicked.connect(
            lambda: self.logic.run_dubbing_task("translate")
        )

    def _on_dub_translation_model_changed(self, model_name: str):
        self.logic.state.dubbing.translation_model = model_name

    def _connect_generation_signals(self):
        self.start_button.clicked.connect(self.logic.start_generation)
        self.resume_button.clicked.connect(self.logic.start_generation)
        self.stop_button.clicked.connect(self.logic.stop_generation)
        self.cancel_button.clicked.connect(self.logic.cancel_generation)

    def _on_progress_updated(self, current: int, total: int, elapsed_time: float):
        if total > 0 and total >= current:
            progress_percent = (current / total) * 100
            self.progress_bar.setValue(int(progress_percent))
            self.progress_label.setText(f"{progress_percent:.2f}%")

            if current > 0:
                time_per_item = elapsed_time / current
                remaining_items = total - current
                remaining_time = remaining_items * time_per_item

                hours, rem = divmod(remaining_time, 3600)
                minutes, seconds = divmod(rem, 60)
                self.remaining_time_label.setText(
                    f"{int(hours):02}:{int(minutes):02}:{int(seconds):02}"
                )
            else:
                self.remaining_time_label.setText("N/A")
        else:
            self.progress_bar.setValue(0)
            self.progress_label.setText("0.00%")
            self.remaining_time_label.setText("N/A")

    def _on_download_url(self):
        url, ok = QInputDialog.getText(self, "Download from URL", "Enter YouTube URL:")
        if ok and url:
            if (
                not self.logic.state.session_name
                or self.logic.state.session_name == "Untitled Session"
            ):
                QMessageBox.warning(
                    self,
                    "No Session",
                    "Please create or load a session before downloading.",
                )
                return
            self.logic.download_from_url(url)

    def _on_use_external_server_toggled(self, checked: bool):
        self.logic.state.tts.use_external_server = checked
        self.logic.state_changed.emit()

    def _update_lifecycle_indicator(self):
        status = self.logic.get_lifecycle_status()
        labels = {
            "Idle": "Idle",
            "Generating": "Generating",
            "Regenerating": "Regenerating",
            "Stopping": "Stopping",
            "Cancelling": "Cancelling",
        }
        self.lifecycle_status_label.setText(labels.get(status, status))

    def _on_tts_connection_running_changed(self, _running: bool):
        self.update_ui_from_state()

    def update_ui_from_state(self):
        state = self.logic.state
        self._update_lifecycle_indicator()
        self._update_session_state(state)
        self._update_source_state(state)
        self._update_tts_state(state)
        self._update_output_state(state)
        self._update_dubbing_state(state)
        self._update_language_dropdown()
        self._update_visibility_and_control_state(state)

    def _update_session_state(self, state):
        self.session_name_label.setText(state.session_name)

    def _update_source_state(self, state):
        if state.source_file_path:
            filename = state.source_file_path.split("/")[-1].split("\\")[-1]
            self.selected_file_label.setText(
                filename if len(filename) < 25 else f"...{filename[-22:]}"
            )
        else:
            self.selected_file_label.setText("No file selected")

    def _update_tts_state(self, state):
        tts_state = state.tts
        current_service = tts_state.service
        if current_service == "OpenAI-Compatible":
            endpoint = str(tts_state.openai_audio_endpoint or "").strip().lower()
            current_service = "Gemini" if endpoint == "gemini" else "OpenAI"
            tts_state.service = current_service

        if self.tts_service_combo.currentText() != current_service:
            self.tts_service_combo.setCurrentText(current_service)

        self.use_external_server_checkbox.setChecked(tts_state.use_external_server)

        if self.external_server_url_edit.text() != tts_state.external_server_url:
            self.external_server_url_edit.blockSignals(True)
            self.external_server_url_edit.setText(tts_state.external_server_url)
            self.external_server_url_edit.blockSignals(False)

        if self.openai_audio_instructions_edit.text() != tts_state.openai_audio_instructions:
            self.openai_audio_instructions_edit.blockSignals(True)
            self.openai_audio_instructions_edit.setText(
                tts_state.openai_audio_instructions
            )
            self.openai_audio_instructions_edit.blockSignals(False)

        self.xtts_model_combo.blockSignals(True)
        current_models = [
            self.xtts_model_combo.itemText(i)
            for i in range(self.xtts_model_combo.count())
        ]
        if tts_state.tts_models != current_models:
            self.xtts_model_combo.clear()
            self.xtts_model_combo.addItems(tts_state.tts_models)
        self.xtts_model_combo.setCurrentText(tts_state.xtts_model)
        self.xtts_model_combo.blockSignals(False)

        self.speaker_combo.blockSignals(True)
        current_speakers = [
            self.speaker_combo.itemText(i)
            for i in range(self.speaker_combo.count())
        ]
        if tts_state.tts_speakers != current_speakers:
            self.speaker_combo.clear()
            self.speaker_combo.addItems(tts_state.tts_speakers)
        self.speaker_combo.setCurrentText(tts_state.speaker)
        self.speaker_combo.blockSignals(False)

        self.speed_slider.setValue(int(tts_state.speed * 100))
        self.speed_label.setText(f"{tts_state.speed:.2f}")

        self.adv_tts_temp_spinbox.setValue(tts_state.temperature)
        self.adv_tts_len_penalty_spinbox.setValue(tts_state.length_penalty)
        self.adv_tts_rep_penalty_spinbox.setValue(tts_state.repetition_penalty)
        self.adv_tts_top_k_spinbox.setValue(tts_state.top_k)
        self.adv_tts_top_p_spinbox.setValue(tts_state.top_p)
        self.adv_tts_do_sample_checkbox.setChecked(tts_state.do_sample)
        self.adv_tts_num_beams_spinbox.setValue(tts_state.num_beams)
        self.adv_tts_chunk_size_spinbox.setValue(tts_state.stream_chunk_size)
        self.adv_tts_text_split_checkbox.setChecked(tts_state.enable_text_splitting)
        self.adv_tts_gpt_cond_len_spinbox.setValue(tts_state.gpt_cond_len)
        self.adv_tts_gpt_cond_chunk_len_spinbox.setValue(tts_state.gpt_cond_chunk_len)
        self.adv_tts_max_ref_len_spinbox.setValue(tts_state.max_ref_len)
        self.adv_tts_sound_norm_refs_checkbox.setChecked(tts_state.sound_norm_refs)
        self.adv_tts_overlap_wav_len_spinbox.setValue(tts_state.overlap_wav_len)
        self.voxtral_max_frames_spinbox.setValue(tts_state.voxtral_max_frames)
        self.voxtral_euler_steps_spinbox.setValue(tts_state.voxtral_euler_steps)
        self.voxtral_chunk_checkbox.setChecked(tts_state.voxtral_chunk)
        self.voxtral_max_chunk_chars_spinbox.setValue(tts_state.voxtral_max_chunk_chars)
        self.voxtral_chunk_silence_ms_spinbox.setValue(tts_state.voxtral_chunk_silence_ms)
        self.voxtral_strip_quotes_checkbox.setChecked(tts_state.voxtral_strip_quotes)
        self.voxtral_strip_diacritics_checkbox.setChecked(tts_state.voxtral_strip_diacritics)
        self.voxtral_level_audio_checkbox.setChecked(tts_state.voxtral_level_audio)

    def _update_output_state(self, state):
        self.format_combo.setCurrentText(state.audio_processing.output_format)
        self.bitrate_combo.setCurrentText(state.audio_processing.bitrate)
        self.upload_cover_button.setText(
            "Cover Uploaded" if state.cover_image_path else "Upload Cover"
        )

    def _update_dubbing_state(self, state):
        dub_state = state.dubbing
        self.dub_whisper_lang_combo.setCurrentText(dub_state.whisper_language)
        self.dub_whisper_model_combo.setCurrentText(dub_state.whisper_model)
        self.dub_correct_transcription_check.setChecked(dub_state.correction_enabled)
        self.dub_translate_check.setChecked(dub_state.translation_enabled)
        self.dub_from_lang_combo.setCurrentText(dub_state.original_language)
        self.dub_to_lang_combo.setCurrentText(dub_state.target_language)
        self.dub_cot_check.setChecked(dub_state.chain_of_thought_enabled)
        self.dub_glossary_check.setChecked(dub_state.glossary_enabled)

        combo_items = {
            self.dub_trans_model_combo.itemText(i)
            for i in range(self.dub_trans_model_combo.count())
        }
        selected_model = dub_state.translation_model
        custom_model_text = dub_state.custom_translation_model
        if selected_model not in combo_items:
            mapped_model = LEGACY_DUBBING_MODEL_UI_MAP.get(str(selected_model).strip().lower())
            if mapped_model:
                selected_model = mapped_model
                if mapped_model == "Custom (LiteLLM)" and not custom_model_text:
                    custom_model_text = dub_state.translation_model
            elif "/" in str(selected_model):
                custom_model_text = custom_model_text or str(selected_model)
                selected_model = "Custom (LiteLLM)"
            else:
                selected_model = "Sonnet 4.6"

        self.dub_trans_model_combo.blockSignals(True)
        self.dub_trans_model_combo.setCurrentText(selected_model)
        self.dub_trans_model_combo.blockSignals(False)

        self.dub_custom_model_edit.blockSignals(True)
        self.dub_custom_model_edit.setText(custom_model_text)
        self.dub_custom_model_edit.blockSignals(False)

        self.dub_custom_api_base_edit.blockSignals(True)
        self.dub_custom_api_base_edit.setText(dub_state.custom_api_base)
        self.dub_custom_api_base_edit.blockSignals(False)

        if dub_state.video_file_path:
            filename = dub_state.video_file_path.split("/")[-1].split("\\")[-1]
            self.selected_video_file_label.setText(filename)
        else:
            self.selected_video_file_label.setText("No video selected")

    def _update_visibility_and_control_state(self, state):
        source_ext = ""
        if state.source_file_path:
            source_ext = state.source_file_path.split(".")[-1].lower()

        is_video = source_ext in ["mp4", "mkv", "webm", "avi", "mov"]
        is_srt = source_ext == "srt"
        is_dubbing_source = is_video or is_srt

        generation_running = self.logic.is_generation_running()
        regeneration_running = self.logic.is_regeneration_running()
        generation_busy = generation_running or regeneration_running
        tts_connecting = self.logic.is_tts_connection_running()
        stop_requested = self.logic.stop_generation_flag.is_set()
        cancel_requested = self.logic.cancel_generation_flag.is_set()

        is_xtts = state.tts.service == "XTTS"
        is_voxtral = state.tts.service == "Voxtral"
        is_openai = state.tts.service == "OpenAI"
        is_gemini = state.tts.service == "Gemini"
        is_legacy_openai_compatible = state.tts.service == "OpenAI-Compatible"
        is_cloud_tts = is_openai or is_gemini or is_legacy_openai_compatible
        is_model_based_tts = is_xtts or is_voxtral or is_cloud_tts
        show_xtts_advanced_settings = self.logic.should_show_xtts_advanced_settings()
        show_voxtral_advanced_settings = is_voxtral
        show_advanced_tts_controls = show_xtts_advanced_settings or show_voxtral_advanced_settings
        show_openai_instructions = is_openai and not show_xtts_advanced_settings

        self.connect_server_button.setText(
            "Connecting..." if tts_connecting else "Connect to Server"
        )

        self.use_external_server_checkbox.setVisible(is_xtts or is_voxtral)
        self.external_server_url_edit.setVisible(
            (is_xtts or is_voxtral) and state.tts.use_external_server
        )
        self.external_server_url_edit.setPlaceholderText(
            "http://localhost:8000" if is_voxtral else "http://localhost:8020"
        )
        self.advanced_tts_checkbox.setText(
            "Advanced Voxtral Settings"
            if show_voxtral_advanced_settings
            else "Advanced XTTS Settings"
        )
        self.advanced_tts_checkbox.setVisible(show_advanced_tts_controls)

        self.xtts_model_label.setText("XTTS Model:" if is_xtts else "Model:")
        self.xtts_model_label.setVisible(is_model_based_tts)
        self.xtts_model_combo.setVisible(is_model_based_tts)

        self.speaker_label.setText("Speaker Voice:" if is_xtts else "Voice:")
        self.upload_voice_button.setVisible(is_xtts)

        self.cloud_provider_hint.setVisible(is_cloud_tts)
        self.openai_audio_instructions_label.setVisible(show_openai_instructions)
        self.openai_audio_instructions_edit.setVisible(show_openai_instructions)
        self.adv_tts_apply_button.setVisible(is_xtts)
        self.xtts_advanced_settings_frame.setVisible(show_xtts_advanced_settings)
        self.voxtral_advanced_settings_frame.setVisible(show_voxtral_advanced_settings)
        self.advanced_tts_frame.setVisible(
            show_advanced_tts_controls and self.advanced_tts_checkbox.isChecked()
        )

        self.dubbing_label.setVisible(is_dubbing_source)
        self.dubbing_frame.setVisible(is_dubbing_source)
        self.output_options_label.setVisible(not is_dubbing_source)
        self.output_options_frame.setVisible(not is_dubbing_source)

        can_start_or_resume = (not generation_busy) and (not is_dubbing_source)
        self.start_button.setEnabled(can_start_or_resume)
        self.resume_button.setEnabled(can_start_or_resume)
        self.stop_button.setEnabled(
            generation_running and not stop_requested and not cancel_requested
        )
        self.cancel_button.setEnabled(generation_running and not cancel_requested)

        session_controls_enabled = not generation_busy
        for widget in (
            self.new_session_button,
            self.load_session_button,
            self.delete_session_button,
            self.select_file_button,
            self.paste_text_button,
            self.download_url_button,
        ):
            widget.setEnabled(session_controls_enabled)

        tts_controls_enabled = (not generation_busy) and (not tts_connecting)
        for widget in (
            self.tts_service_combo,
            self.connect_server_button,
            self.use_external_server_checkbox,
            self.external_server_url_edit,
            self.xtts_model_combo,
            self.language_combo,
            self.speaker_combo,
            self.upload_voice_button,
            self.openai_audio_instructions_edit,
            self.speed_slider,
            self.advanced_tts_checkbox,
            self.adv_tts_temp_spinbox,
            self.adv_tts_len_penalty_spinbox,
            self.adv_tts_rep_penalty_spinbox,
            self.adv_tts_top_k_spinbox,
            self.adv_tts_top_p_spinbox,
            self.adv_tts_do_sample_checkbox,
            self.adv_tts_num_beams_spinbox,
            self.adv_tts_chunk_size_spinbox,
            self.adv_tts_text_split_checkbox,
            self.adv_tts_gpt_cond_len_spinbox,
            self.adv_tts_gpt_cond_chunk_len_spinbox,
            self.adv_tts_max_ref_len_spinbox,
            self.adv_tts_sound_norm_refs_checkbox,
            self.adv_tts_overlap_wav_len_spinbox,
            self.voxtral_max_frames_spinbox,
            self.voxtral_euler_steps_spinbox,
            self.voxtral_chunk_checkbox,
            self.voxtral_max_chunk_chars_spinbox,
            self.voxtral_chunk_silence_ms_spinbox,
            self.voxtral_strip_quotes_checkbox,
            self.voxtral_strip_diacritics_checkbox,
            self.voxtral_level_audio_checkbox,
            self.adv_tts_apply_button,
        ):
            widget.setEnabled(tts_controls_enabled)

        dubbing_controls_enabled = (not generation_busy) and is_dubbing_source
        for widget in (
            self.select_video_file_button,
            self.generate_dub_audio_button,
            self.add_dub_to_video_button,
            self.only_transcribe_button,
            self.only_correct_button,
            self.only_translate_button,
        ):
            widget.setEnabled(dubbing_controls_enabled)

        if is_dubbing_source:
            self.transcription_frame.setVisible(is_video)
            self.video_file_frame.setVisible(is_srt)

    def _update_language_dropdown(self):
        service = self.logic.state.tts.service
        current_lang = self.logic.state.tts.language

        self.language_combo.blockSignals(True)
        self.language_combo.clear()

        if service in {"XTTS", "Voxtral", "OpenAI", "Gemini", "OpenAI-Compatible"}:
            self.language_combo.addItems(XTTS_LANGUAGES)
            if current_lang in XTTS_LANGUAGES:
                self.language_combo.setCurrentText(current_lang)
            else:
                self.logic.state.tts.language = "en"
                self.language_combo.setCurrentText("en")
        elif service == "Silero":
            lang_names = [lang["name"] for lang in SILERO_LANGUAGES]
            self.language_combo.addItems(lang_names)
            if current_lang in lang_names:
                self.language_combo.setCurrentText(current_lang)
            else:
                self.logic.state.tts.language = "English (v3)"
                self.language_combo.setCurrentText("English (v3)")

        self.language_combo.blockSignals(False)

    def _on_new_session(self):
        text, ok = QInputDialog.getText(
            self,
            "New Session",
            "Enter a name for the new session:",
        )
        if ok and text:
            self.logic.new_session(text)

    def _on_load_session(self):
        dir_name = QFileDialog.getExistingDirectory(self, "Load Session", "Outputs")
        if dir_name:
            session_name = dir_name.split("/")[-1].split("\\")[-1]
            self.logic.load_session(session_name)

    def _on_delete_session(self):
        reply = QMessageBox.question(
            self,
            "Delete Session",
            f"Are you sure you want to delete the session '{self.logic.state.session_name}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply == QMessageBox.StandardButton.Yes:
            self.logic.delete_session(self.logic.state.session_name)

    def _on_paste_text(self):
        dialog = PasteTextDialog(self)
        if dialog.exec():
            data = dialog.get_data()
            if data["text"]:
                self.logic.save_pasted_text(data["text"], data["mark_paragraphs"])

    def _on_select_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Source File",
            "",
            "Supported Files (*.txt *.srt *.pdf *.epub *.docx *.mobi *.mp4 *.mkv *.webm *.avi *.mov);;All files (*.*)",
        )
        if not file_path:
            return

        selected_path = file_path
        selected_ext = os.path.splitext(file_path)[1].lower()

        if selected_ext == ".pdf":
            crop_choice = QMessageBox.question(
                self,
                "PDF Preprocessing",
                "Would you like to open PyCropPDF for manual cropping before importing?",
                QMessageBox.StandardButton.Yes
                | QMessageBox.StandardButton.No
                | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No,
            )
            if crop_choice == QMessageBox.StandardButton.Cancel:
                return
            if crop_choice == QMessageBox.StandardButton.Yes:
                selected_path = self.logic.run_pdf_crop_tool(file_path)

        if not self.logic.select_source_file(selected_path):
            return

        reviewable_extensions = {".pdf", ".epub", ".docx", ".mobi"}
        if selected_ext in reviewable_extensions and self.logic.state.raw_text:
            review_dialog = ReviewTextDialog(self.logic.state.raw_text, self)
            if review_dialog.exec():
                review_data = review_dialog.get_data()
                self.logic.apply_reviewed_text(
                    review_data["text"],
                    mark_pdf_preprocessed=(selected_ext == ".pdf"),
                )
            else:
                self.logic.clear_source_file_selection()

    def _on_metadata(self):
        metadata_to_edit = self.logic.state.metadata.copy()
        if not metadata_to_edit.get("album"):
            metadata_to_edit["album"] = self.logic.state.session_name

        dialog = MetadataDialog(metadata_to_edit, self)
        if dialog.exec():
            new_metadata = dialog.get_metadata()
            self.logic.save_metadata(new_metadata)

    def _on_upload_cover(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Cover Image",
            "",
            "Image Files (*.png *.jpg *.jpeg);;All files (*.*)",
        )
        if file_path:
            self.logic.select_cover_image(file_path)

    def _on_upload_voice(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Upload Speaker Voice",
            "",
            "WAV files (*.wav)",
        )
        if file_path:
            self.logic.upload_speaker_voice(file_path)

    def _on_open_custom_prompt(self):
        dialog = CustomPromptDialog(
            self.logic.state.dubbing.custom_correction_prompt,
            self,
        )
        if dialog.exec():
            new_prompt = dialog.get_prompt_text()
            self.logic.state.dubbing.custom_correction_prompt = new_prompt

    def _on_select_video_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Video File",
            "",
            "Video Files (*.mp4 *.mkv *.webm *.avi *.mov);;All files (*.*)",
        )
        if file_path:
            self.logic.select_dubbing_video_file(file_path)

    def _on_tts_service_changed(self, service: str):
        self.logic.state.tts.service = service
        self.logic.state.tts.tts_models = []
        self.logic.state.tts.tts_speakers = []
        self.logic.state.tts.xtts_model = ""
        self.logic.state.tts.speaker = ""

        if service == "OpenAI":
            self.logic.state.tts.openai_audio_endpoint = "openai"
        elif service == "Gemini":
            self.logic.state.tts.openai_audio_endpoint = "gemini"

        if service in {"OpenAI", "Gemini"}:
            self.logic.populate_cloud_tts_catalogs(
                use_remote=False,
                emit_state=False,
            )

        self._update_language_dropdown()
        self.logic.state_changed.emit()

    def _on_speed_changed(self, value: int):
        speed = value / 100.0
        self.speed_label.setText(f"{speed:.2f}")
        self.logic.state.tts.speed = speed
