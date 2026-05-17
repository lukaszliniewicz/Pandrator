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
        self.cloud_provider_label = self.tts_section.cloud_provider_label
        self.cloud_provider_combo = self.tts_section.cloud_provider_combo
        self.openai_audio_instructions_label = self.tts_section.openai_audio_instructions_label
        self.openai_audio_instructions_edit = self.tts_section.openai_audio_instructions_edit

        # Advanced TTS controls
        self.xtts_advanced_settings_frame = self.advanced_tts_frame.xtts_advanced_settings_frame
        self.voxtral_advanced_settings_frame = self.advanced_tts_frame.voxtral_advanced_settings_frame
        self.xtts_send_hint_label = self.advanced_tts_frame.xtts_send_hint_label
        self.adv_tts_temp_send_checkbox = self.advanced_tts_frame.adv_tts_temp_send_checkbox
        self.adv_tts_temp_spinbox = self.advanced_tts_frame.adv_tts_temp_spinbox
        self.adv_tts_len_penalty_send_checkbox = self.advanced_tts_frame.adv_tts_len_penalty_send_checkbox
        self.adv_tts_len_penalty_spinbox = self.advanced_tts_frame.adv_tts_len_penalty_spinbox
        self.adv_tts_rep_penalty_send_checkbox = self.advanced_tts_frame.adv_tts_rep_penalty_send_checkbox
        self.adv_tts_rep_penalty_spinbox = self.advanced_tts_frame.adv_tts_rep_penalty_spinbox
        self.adv_tts_top_k_send_checkbox = self.advanced_tts_frame.adv_tts_top_k_send_checkbox
        self.adv_tts_top_k_spinbox = self.advanced_tts_frame.adv_tts_top_k_spinbox
        self.adv_tts_top_p_send_checkbox = self.advanced_tts_frame.adv_tts_top_p_send_checkbox
        self.adv_tts_top_p_spinbox = self.advanced_tts_frame.adv_tts_top_p_spinbox
        self.adv_tts_do_sample_send_checkbox = self.advanced_tts_frame.adv_tts_do_sample_send_checkbox
        self.adv_tts_do_sample_checkbox = self.advanced_tts_frame.adv_tts_do_sample_checkbox
        self.adv_tts_num_beams_send_checkbox = self.advanced_tts_frame.adv_tts_num_beams_send_checkbox
        self.adv_tts_num_beams_spinbox = self.advanced_tts_frame.adv_tts_num_beams_spinbox
        self.adv_tts_chunk_size_send_checkbox = self.advanced_tts_frame.adv_tts_chunk_size_send_checkbox
        self.adv_tts_chunk_size_spinbox = self.advanced_tts_frame.adv_tts_chunk_size_spinbox
        self.adv_tts_text_split_send_checkbox = self.advanced_tts_frame.adv_tts_text_split_send_checkbox
        self.adv_tts_text_split_checkbox = self.advanced_tts_frame.adv_tts_text_split_checkbox
        self.adv_tts_gpt_cond_len_send_checkbox = self.advanced_tts_frame.adv_tts_gpt_cond_len_send_checkbox
        self.adv_tts_gpt_cond_len_spinbox = self.advanced_tts_frame.adv_tts_gpt_cond_len_spinbox
        self.adv_tts_gpt_cond_chunk_len_send_checkbox = self.advanced_tts_frame.adv_tts_gpt_cond_chunk_len_send_checkbox
        self.adv_tts_gpt_cond_chunk_len_spinbox = self.advanced_tts_frame.adv_tts_gpt_cond_chunk_len_spinbox
        self.adv_tts_max_ref_len_send_checkbox = self.advanced_tts_frame.adv_tts_max_ref_len_send_checkbox
        self.adv_tts_max_ref_len_spinbox = self.advanced_tts_frame.adv_tts_max_ref_len_spinbox
        self.adv_tts_sound_norm_refs_send_checkbox = self.advanced_tts_frame.adv_tts_sound_norm_refs_send_checkbox
        self.adv_tts_sound_norm_refs_checkbox = self.advanced_tts_frame.adv_tts_sound_norm_refs_checkbox
        self.adv_tts_overlap_wav_len_send_checkbox = self.advanced_tts_frame.adv_tts_overlap_wav_len_send_checkbox
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
        self.dub_trans_provider_combo = self.dubbing_frame.dub_trans_provider_combo
        self.dub_trans_model_combo = self.dubbing_frame.dub_trans_model_combo
        self.dub_trans_model_hint = self.dubbing_frame.dub_trans_model_hint
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
        self.cloud_provider_combo.currentIndexChanged.connect(self._on_cloud_provider_changed)
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

        self.adv_tts_temp_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_temperature",
                self.adv_tts_temp_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_len_penalty_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_length_penalty",
                self.adv_tts_len_penalty_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_rep_penalty_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_repetition_penalty",
                self.adv_tts_rep_penalty_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_top_k_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_top_k",
                self.adv_tts_top_k_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_top_p_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_top_p",
                self.adv_tts_top_p_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_do_sample_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_do_sample",
                self.adv_tts_do_sample_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_num_beams_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_num_beams",
                self.adv_tts_num_beams_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_chunk_size_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_stream_chunk_size",
                self.adv_tts_chunk_size_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_text_split_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_enable_text_splitting",
                self.adv_tts_text_split_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_gpt_cond_len_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_gpt_cond_len",
                self.adv_tts_gpt_cond_len_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_gpt_cond_chunk_len_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_gpt_cond_chunk_len",
                self.adv_tts_gpt_cond_chunk_len_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_max_ref_len_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_max_ref_len",
                self.adv_tts_max_ref_len_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_sound_norm_refs_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_sound_norm_refs",
                self.adv_tts_sound_norm_refs_send_checkbox.isChecked(),
            )
        )
        self.adv_tts_overlap_wav_len_send_checkbox.stateChanged.connect(
            lambda: setattr(
                self.logic.state.tts,
                "xtts_send_overlap_wav_len",
                self.adv_tts_overlap_wav_len_send_checkbox.isChecked(),
            )
        )

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
        self.dub_trans_provider_combo.currentIndexChanged.connect(
            self._on_dub_translation_provider_changed
        )
        self.dub_trans_model_combo.currentTextChanged.connect(
            self._on_dub_translation_model_changed
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
        self.logic.state.dubbing.translation_model = model_name.strip()

    def _on_dub_translation_provider_changed(self, _index: int = -1):
        provider_id = str(self.dub_trans_provider_combo.currentData() or "").strip()
        if not provider_id:
            return

        self.logic.state.dubbing.translation_provider = provider_id
        if provider_id == "deepl":
            self.logic.state.dubbing.translation_model = ""
        else:
            models = self.logic.list_dubbing_translation_models(provider_id)
            current_model = str(self.logic.state.dubbing.translation_model or "").strip()
            if models and current_model not in models:
                self.logic.state.dubbing.translation_model = models[0]
            elif not current_model and models:
                self.logic.state.dubbing.translation_model = models[0]

        self.logic.normalize_dubbing_translation_state(self.logic.state.dubbing)
        self.logic.state_changed.emit()

    def _connect_generation_signals(self):
        self.start_button.clicked.connect(self.logic.start_generation)
        self.resume_button.clicked.connect(self.logic.start_generation)
        self.stop_button.clicked.connect(self.logic.stop_generation)
        self.cancel_button.clicked.connect(self.logic.cancel_generation)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        safe_seconds = max(0, int(seconds))
        hours, rem = divmod(safe_seconds, 3600)
        minutes, secs = divmod(rem, 60)
        return f"{hours:02}:{minutes:02}:{secs:02}"

    def _on_progress_updated(self, current: int, total: int, elapsed_time: float):
        if total > 0 and total >= current:
            progress_percent = (current / total) * 100
            self.progress_bar.setValue(int(progress_percent))
            progress_text = f"{progress_percent:.2f}%"
            if current == total and total > 0:
                progress_text = f"{progress_text} ({self._format_duration(elapsed_time)})"
            self.progress_bar.setFormat(progress_text)

            if 0 < current < total:
                time_per_item = elapsed_time / current
                remaining_items = total - current
                remaining_time = remaining_items * time_per_item

                self.remaining_time_label.setText(self._format_duration(remaining_time))
            elif current == total and total > 0:
                self.remaining_time_label.setText("00:00:00")
            else:
                self.remaining_time_label.setText("N/A")
        else:
            self.progress_bar.setValue(0)
            self.progress_bar.setFormat("0.00%")
            self.remaining_time_label.setText("N/A")

    def _on_download_url(self):
        url, ok = QInputDialog.getText(self, "Download from URL", "Enter YouTube URL:")
        normalized_url = url.strip() if ok and url else ""
        if not normalized_url:
            return

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

        should_continue, reset_session = self._confirm_source_replacement()
        if not should_continue:
            return

        self.logic.download_from_url(normalized_url, reset_session=reset_session)

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

    def _set_button_accent(self, button, accent_active: bool):
        if bool(button.property("accentActive")) == accent_active:
            return

        button.setProperty("accentActive", accent_active)
        style = button.style()
        if style is not None:
            style.unpolish(button)
            style.polish(button)
        button.update()

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

        if self.tts_service_combo.currentText() != current_service:
            self.tts_service_combo.setCurrentText(current_service)

        cloud_providers = self.logic.list_tts_provider_configs()
        selected_provider_id = str(tts_state.openai_audio_endpoint or "").strip()

        self.cloud_provider_combo.blockSignals(True)
        self.cloud_provider_combo.clear()
        for provider in cloud_providers:
            provider_id = str(provider.get("id") or "")
            provider_name = str(provider.get("name") or provider_id)
            if provider_id:
                self.cloud_provider_combo.addItem(provider_name, provider_id)

        if self.cloud_provider_combo.count() > 0:
            target_index = self.cloud_provider_combo.findData(selected_provider_id)
            if target_index < 0:
                target_index = 0
                selected_provider_id = str(self.cloud_provider_combo.itemData(0) or "")
                if selected_provider_id and selected_provider_id != tts_state.openai_audio_endpoint:
                    tts_state.openai_audio_endpoint = selected_provider_id
            self.cloud_provider_combo.setCurrentIndex(target_index)

        self.cloud_provider_combo.blockSignals(False)

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

        self.adv_tts_temp_send_checkbox.setChecked(tts_state.xtts_send_temperature)
        self.adv_tts_len_penalty_send_checkbox.setChecked(tts_state.xtts_send_length_penalty)
        self.adv_tts_rep_penalty_send_checkbox.setChecked(tts_state.xtts_send_repetition_penalty)
        self.adv_tts_top_k_send_checkbox.setChecked(tts_state.xtts_send_top_k)
        self.adv_tts_top_p_send_checkbox.setChecked(tts_state.xtts_send_top_p)
        self.adv_tts_do_sample_send_checkbox.setChecked(tts_state.xtts_send_do_sample)
        self.adv_tts_num_beams_send_checkbox.setChecked(tts_state.xtts_send_num_beams)
        self.adv_tts_chunk_size_send_checkbox.setChecked(tts_state.xtts_send_stream_chunk_size)
        self.adv_tts_text_split_send_checkbox.setChecked(tts_state.xtts_send_enable_text_splitting)
        self.adv_tts_gpt_cond_len_send_checkbox.setChecked(tts_state.xtts_send_gpt_cond_len)
        self.adv_tts_gpt_cond_chunk_len_send_checkbox.setChecked(tts_state.xtts_send_gpt_cond_chunk_len)
        self.adv_tts_max_ref_len_send_checkbox.setChecked(tts_state.xtts_send_max_ref_len)
        self.adv_tts_sound_norm_refs_send_checkbox.setChecked(tts_state.xtts_send_sound_norm_refs)
        self.adv_tts_overlap_wav_len_send_checkbox.setChecked(tts_state.xtts_send_overlap_wav_len)

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
        self.logic.normalize_dubbing_translation_state(dub_state)

        self.dub_whisper_lang_combo.setCurrentText(dub_state.whisper_language)
        self.dub_whisper_model_combo.setCurrentText(dub_state.whisper_model)
        self.dub_correct_transcription_check.setChecked(dub_state.correction_enabled)
        self.dub_translate_check.setChecked(dub_state.translation_enabled)
        self.dub_from_lang_combo.setCurrentText(dub_state.original_language)
        self.dub_to_lang_combo.setCurrentText(dub_state.target_language)
        self.dub_cot_check.setChecked(dub_state.chain_of_thought_enabled)
        self.dub_glossary_check.setChecked(dub_state.glossary_enabled)

        provider_options = self.logic.list_dubbing_translation_provider_configs()
        selected_provider_id = str(dub_state.translation_provider or "").strip()

        self.dub_trans_provider_combo.blockSignals(True)
        self.dub_trans_provider_combo.clear()
        for provider in provider_options:
            provider_id = str(provider.get("id") or "").strip()
            provider_name = str(provider.get("name") or provider_id).strip()
            if provider_id:
                self.dub_trans_provider_combo.addItem(provider_name, provider_id)

        if self.dub_trans_provider_combo.count() > 0:
            target_index = self.dub_trans_provider_combo.findData(selected_provider_id)
            if target_index < 0:
                target_index = 0
                selected_provider_id = str(self.dub_trans_provider_combo.itemData(0) or "")
                if selected_provider_id != str(dub_state.translation_provider or ""):
                    dub_state.translation_provider = selected_provider_id
            self.dub_trans_provider_combo.setCurrentIndex(target_index)

        self.dub_trans_provider_combo.blockSignals(False)

        provider_models = self.logic.list_dubbing_translation_models(selected_provider_id)
        selected_model = str(dub_state.translation_model or "").strip()

        self.dub_trans_model_combo.blockSignals(True)
        self.dub_trans_model_combo.clear()
        self.dub_trans_model_combo.addItems(provider_models)

        if selected_provider_id != "deepl":
            if selected_model and self.dub_trans_model_combo.findText(selected_model) == -1:
                self.dub_trans_model_combo.addItem(selected_model)
            self.dub_trans_model_combo.setCurrentText(selected_model)
        else:
            self.dub_trans_model_combo.setCurrentText("")

        self.dub_trans_model_combo.blockSignals(False)

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
        is_deepl_provider = (
            str(state.dubbing.translation_provider or "").strip().lower() == "deepl"
        )

        generation_running = self.logic.is_generation_running()
        regeneration_running = self.logic.is_regeneration_running()
        generation_busy = generation_running or regeneration_running
        tts_connecting = self.logic.is_tts_connection_running()
        stop_requested = self.logic.stop_generation_flag.is_set()
        cancel_requested = self.logic.cancel_generation_flag.is_set()

        is_xtts = state.tts.service == "XTTS"
        is_voxtral = state.tts.service == "Voxtral"
        is_cloud_tts = state.tts.service in {
            "OpenAI-Compatible",
            "OpenAI",
            "Gemini",
        }
        is_model_based_tts = is_xtts or is_voxtral or is_cloud_tts
        show_xtts_advanced_settings = self.logic.should_show_xtts_advanced_settings()
        show_voxtral_advanced_settings = is_voxtral
        show_advanced_tts_controls = show_xtts_advanced_settings or show_voxtral_advanced_settings
        show_openai_instructions = is_cloud_tts and not show_xtts_advanced_settings

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

        self.cloud_provider_label.setVisible(is_cloud_tts)
        self.cloud_provider_combo.setVisible(is_cloud_tts)
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
        self.dub_trans_model_hint.setVisible(is_dubbing_source and not is_deepl_provider)
        self.output_options_label.setVisible(not is_dubbing_source)
        self.output_options_frame.setVisible(not is_dubbing_source)

        can_start_or_resume = (not generation_busy) and (not is_dubbing_source)
        self.start_button.setEnabled(can_start_or_resume)
        self.resume_button.setEnabled(can_start_or_resume)
        self._set_button_accent(self.start_button, not is_dubbing_source)
        self._set_button_accent(self.generate_dub_audio_button, is_dubbing_source)
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
            self.cloud_provider_combo,
            self.use_external_server_checkbox,
            self.external_server_url_edit,
            self.xtts_model_combo,
            self.language_combo,
            self.speaker_combo,
            self.upload_voice_button,
            self.openai_audio_instructions_edit,
            self.speed_slider,
            self.advanced_tts_checkbox,
            self.adv_tts_temp_send_checkbox,
            self.adv_tts_temp_spinbox,
            self.adv_tts_len_penalty_send_checkbox,
            self.adv_tts_len_penalty_spinbox,
            self.adv_tts_rep_penalty_send_checkbox,
            self.adv_tts_rep_penalty_spinbox,
            self.adv_tts_top_k_send_checkbox,
            self.adv_tts_top_k_spinbox,
            self.adv_tts_top_p_send_checkbox,
            self.adv_tts_top_p_spinbox,
            self.adv_tts_do_sample_send_checkbox,
            self.adv_tts_do_sample_checkbox,
            self.adv_tts_num_beams_send_checkbox,
            self.adv_tts_num_beams_spinbox,
            self.adv_tts_chunk_size_send_checkbox,
            self.adv_tts_chunk_size_spinbox,
            self.adv_tts_text_split_send_checkbox,
            self.adv_tts_text_split_checkbox,
            self.adv_tts_gpt_cond_len_send_checkbox,
            self.adv_tts_gpt_cond_len_spinbox,
            self.adv_tts_gpt_cond_chunk_len_send_checkbox,
            self.adv_tts_gpt_cond_chunk_len_spinbox,
            self.adv_tts_max_ref_len_send_checkbox,
            self.adv_tts_max_ref_len_spinbox,
            self.adv_tts_sound_norm_refs_send_checkbox,
            self.adv_tts_sound_norm_refs_checkbox,
            self.adv_tts_overlap_wav_len_send_checkbox,
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
            self.dub_whisper_lang_combo,
            self.dub_whisper_model_combo,
            self.dub_correct_transcription_check,
            self.dub_custom_prompt_button,
            self.dub_translate_check,
            self.dub_from_lang_combo,
            self.dub_to_lang_combo,
            self.dub_cot_check,
            self.dub_glossary_check,
            self.dub_trans_provider_combo,
            self.select_video_file_button,
            self.generate_dub_audio_button,
            self.add_dub_to_video_button,
            self.only_transcribe_button,
            self.only_correct_button,
            self.only_translate_button,
        ):
            widget.setEnabled(dubbing_controls_enabled)

        self.dub_trans_model_combo.setEnabled(
            dubbing_controls_enabled and not is_deepl_provider
        )

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

    def _prompt_for_new_session_name(self) -> str | None:
        text, ok = QInputDialog.getText(
            self,
            "New Session",
            "Enter a name for the new session:",
        )
        session_name = text.strip() if ok and text else ""
        return session_name or None

    def _confirm_source_replacement(self, selected_file_path: str | None = None) -> tuple[bool, bool]:
        if not self.logic.should_warn_before_source_change(selected_file_path):
            return True, False

        chooser = QMessageBox(self)
        chooser.setIcon(QMessageBox.Icon.Warning)
        chooser.setWindowTitle("Replace Session Source")
        chooser.setText(
            "This session already has generated files. Switching source will remove everything from this session."
        )
        chooser.setInformativeText(
            "Create a new session to keep existing files, or proceed to clear this one first."
        )
        new_session_button = chooser.addButton("Create New Session", QMessageBox.ButtonRole.AcceptRole)
        proceed_button = chooser.addButton("Proceed", QMessageBox.ButtonRole.DestructiveRole)
        chooser.addButton(QMessageBox.StandardButton.Cancel)
        chooser.setDefaultButton(new_session_button)
        chooser.exec()

        clicked_button = chooser.clickedButton()
        if clicked_button == new_session_button:
            session_name = self._prompt_for_new_session_name()
            if not session_name:
                return False, False
            self.logic.new_session(session_name)
            return True, False

        if clicked_button == proceed_button:
            return True, True

        return False, False

    def _on_new_session(self):
        session_name = self._prompt_for_new_session_name()
        if session_name:
            self.logic.new_session(session_name)

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
                should_continue, reset_session = self._confirm_source_replacement()
                if not should_continue:
                    return
                self.logic.save_pasted_text(
                    data["text"],
                    data["mark_paragraphs"],
                    reset_session=reset_session,
                )

    def _on_select_file(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Source File",
            "",
            "Supported Files (*.txt *.srt *.pdf *.epub *.docx *.mobi *.mp4 *.mkv *.webm *.avi *.mov);;All files (*.*)",
        )
        if not file_path:
            return

        should_continue, reset_session = self._confirm_source_replacement(file_path)
        if not should_continue:
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

        if not self.logic.select_source_file(selected_path, reset_session=reset_session):
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
        normalized_service = service
        if service in {"OpenAI", "Gemini"}:
            normalized_service = "OpenAI-Compatible"

        self.logic.state.tts.service = normalized_service
        self.logic.state.tts.tts_models = []
        self.logic.state.tts.tts_speakers = []
        self.logic.state.tts.xtts_model = ""
        self.logic.state.tts.speaker = ""

        if service == "OpenAI":
            self.logic.state.tts.openai_audio_endpoint = "openai"
        elif service == "Gemini":
            self.logic.state.tts.openai_audio_endpoint = "gemini"
        elif normalized_service == "OpenAI-Compatible":
            selected_provider_id = str(
                self.cloud_provider_combo.currentData()
                or self.logic.state.tts.openai_audio_endpoint
                or "openai"
            )
            self.logic.state.tts.openai_audio_endpoint = selected_provider_id

        if normalized_service == "OpenAI-Compatible":
            self.logic.populate_cloud_tts_catalogs(
                use_remote=False,
                provider_id=self.logic.state.tts.openai_audio_endpoint,
                emit_state=False,
            )

        self._update_language_dropdown()
        self.logic.state_changed.emit()

    def _on_cloud_provider_changed(self, _index: int = -1):
        selected_provider_id = str(self.cloud_provider_combo.currentData() or "").strip()
        if not selected_provider_id:
            return

        self.logic.state.tts.openai_audio_endpoint = selected_provider_id
        if self.logic.state.tts.service == "OpenAI-Compatible":
            self.logic.state.tts.tts_models = []
            self.logic.state.tts.tts_speakers = []
            self.logic.state.tts.xtts_model = ""
            self.logic.state.tts.speaker = ""
            self.logic.populate_cloud_tts_catalogs(
                use_remote=False,
                provider_id=selected_provider_id,
                emit_state=False,
            )

        self.logic.state_changed.emit()

    def _on_speed_changed(self, value: int):
        speed = value / 100.0
        self.speed_label.setText(f"{speed:.2f}")
        self.logic.state.tts.speed = speed
