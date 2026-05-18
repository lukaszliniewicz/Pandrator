import os
import re

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QVBoxLayout, QWidget

from ...constants import (
    KOKORO_LANGUAGES,
    LANGUAGE_DISPLAY_NAMES,
    SILERO_LANGUAGES,
    VOXTRAL_LANGUAGES,
    XTTS_LANGUAGES,
)
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


KOKORO_VOICE_LANGUAGE_GROUPS = {
    "a": "American English",
    "b": "British English",
    "e": "Spanish",
    "f": "French",
    "h": "Hindi",
    "i": "Italian",
    "j": "Japanese",
    "p": "Portuguese",
    "z": "Chinese (Simplified)",
}

VOICE_GENDER_LABELS = {
    "f": "Female",
    "m": "Male",
    "female": "Female",
    "male": "Male",
}

VOXTRAL_STYLE_LABELS = {
    "casual": "Casual",
    "cheerful": "Cheerful",
    "neutral": "Neutral",
}

KOKORO_OPENAI_ALIAS_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
}

SPEAKER_HEADING_VALUE = "__heading__"
KOKORO_MISC_GROUP_ORDER = ["OpenAI Alias Voices", "Voice Blends", "Other Voices"]

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
        self.fine_tune_timings_button = self.dubbing_frame.fine_tune_timings_button

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
        self.language_combo.currentIndexChanged.connect(self._on_tts_language_selected)
        self.speaker_combo.currentTextChanged.connect(self._on_speaker_selected)
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
        self.dub_to_lang_combo.currentIndexChanged.connect(
            self._on_dub_target_language_selected
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
        self.fine_tune_timings_button.clicked.connect(
            lambda: self.logic.run_dubbing_task("fine_tune_timings")
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
            "RVC Processing": "RVC Processing",
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

        speaker_options = self._build_speaker_combo_options(
            current_service,
            tts_state.tts_speakers,
        )

        self.speaker_combo.blockSignals(True)
        current_speaker_options = [
            (
                self.speaker_combo.itemText(i),
                str(self.speaker_combo.itemData(i) or "").strip(),
                self._is_combo_index_enabled(self.speaker_combo, i),
            )
            for i in range(self.speaker_combo.count())
        ]
        if speaker_options != current_speaker_options:
            self.speaker_combo.clear()
            for speaker_label, speaker_id, speaker_enabled in speaker_options:
                self.speaker_combo.addItem(speaker_label, speaker_id)
                self._set_combo_index_enabled(
                    self.speaker_combo,
                    self.speaker_combo.count() - 1,
                    speaker_enabled,
                )

        selected_speaker = str(tts_state.speaker or "").strip()
        selected_speaker_index = self.speaker_combo.findData(selected_speaker)
        if selected_speaker_index >= 0:
            self.speaker_combo.setCurrentIndex(selected_speaker_index)
        else:
            self.speaker_combo.setEditText(selected_speaker)

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
        self._update_dubbing_target_language_dropdown(dub_state.target_language)
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
        rvc_processing_running = self.logic.is_rvc_processing_running()
        generation_busy = generation_running or regeneration_running or rvc_processing_running
        tts_connecting = self.logic.is_tts_connection_running()
        stop_requested = self.logic.stop_generation_flag.is_set()
        cancel_requested = self.logic.cancel_generation_flag.is_set()

        is_xtts = state.tts.service == "XTTS"
        is_voxtral = state.tts.service == "Voxtral"
        is_kokoro = state.tts.service == "Kokoro"
        is_cloud_tts = state.tts.service in {
            "OpenAI-Compatible",
            "OpenAI",
            "Gemini",
        }
        is_model_based_tts = is_xtts or is_voxtral or is_kokoro or is_cloud_tts
        show_xtts_advanced_settings = self.logic.should_show_xtts_advanced_settings()
        show_voxtral_advanced_settings = is_voxtral
        show_advanced_tts_controls = show_xtts_advanced_settings or show_voxtral_advanced_settings
        show_openai_instructions = is_cloud_tts and not show_xtts_advanced_settings

        self.connect_server_button.setText(
            "Connecting..." if tts_connecting else "Connect to Server"
        )

        self.use_external_server_checkbox.setVisible(is_xtts or is_voxtral or is_kokoro)
        self.external_server_url_edit.setVisible(
            (is_xtts or is_voxtral or is_kokoro) and state.tts.use_external_server
        )
        self.external_server_url_edit.setPlaceholderText(
            "http://localhost:8000"
            if is_voxtral
            else ("http://localhost:8880" if is_kokoro else "http://localhost:8020")
        )
        self.advanced_tts_checkbox.setText(
            "Advanced Voxtral Settings"
            if show_voxtral_advanced_settings
            else "Advanced XTTS Settings"
        )
        self.advanced_tts_checkbox.setVisible(show_advanced_tts_controls)

        show_model_selector = is_model_based_tts and not is_kokoro
        self.xtts_model_label.setText("XTTS Model:" if is_xtts else "Model:")
        self.xtts_model_label.setVisible(show_model_selector)
        self.xtts_model_combo.setVisible(show_model_selector)

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

        self.fine_tune_timings_button.setVisible(is_dubbing_source)
        self.fine_tune_timings_button.setEnabled(
            dubbing_controls_enabled and self.logic.has_dubbing_srt_file()
        )

        if is_dubbing_source:
            self.transcription_frame.setVisible(is_video)
            self.video_file_frame.setVisible(is_srt)

    def _update_language_dropdown(self):
        service = self.logic.state.tts.service
        current_lang = str(self.logic.state.tts.language or "").strip()

        self.language_combo.blockSignals(True)
        self.language_combo.clear()

        if service == "Silero":
            lang_names = [lang["name"] for lang in SILERO_LANGUAGES]
            self.language_combo.addItems(lang_names)
            if current_lang in lang_names:
                self.language_combo.setCurrentText(current_lang)
            else:
                self.logic.state.tts.language = "English (v3)"
                self.language_combo.setCurrentText("English (v3)")
            self.language_combo.blockSignals(False)
            return

        language_codes = self._language_codes_for_service(service)
        for language_code in language_codes:
            self.language_combo.addItem(
                self._language_label_for_service(service, language_code),
                language_code,
            )

        normalized_lang = self._normalize_language_code_for_service(service, current_lang)
        if self.language_combo.findData(normalized_lang) == -1:
            fallback_lang = "en"
            normalized_lang = fallback_lang if self.language_combo.findData(fallback_lang) != -1 else ""

        if not normalized_lang and self.language_combo.count() > 0:
            normalized_lang = str(self.language_combo.itemData(0) or "").strip()

        if normalized_lang:
            target_index = self.language_combo.findData(normalized_lang)
            if target_index >= 0:
                self.language_combo.setCurrentIndex(target_index)

            if normalized_lang != current_lang:
                self.logic.state.tts.language = normalized_lang

        self.language_combo.blockSignals(False)

    def _on_tts_language_selected(self, _index: int = -1):
        language_value = self._combo_backend_value(self.language_combo)
        if language_value:
            self.logic.on_tts_language_changed(language_value)

    def _update_dubbing_target_language_dropdown(self, target_language: str):
        current_target = str(target_language or "").strip()

        self.dub_to_lang_combo.blockSignals(True)
        self.dub_to_lang_combo.clear()
        for language_code in XTTS_LANGUAGES:
            self.dub_to_lang_combo.addItem(
                self._language_label_for_service("XTTS", language_code),
                language_code,
            )

        normalized_target = self._normalize_language_code_for_service("XTTS", current_target)
        if self.dub_to_lang_combo.findData(normalized_target) == -1:
            normalized_target = "en" if self.dub_to_lang_combo.findData("en") != -1 else ""

        if not normalized_target and self.dub_to_lang_combo.count() > 0:
            normalized_target = str(self.dub_to_lang_combo.itemData(0) or "").strip()

        if normalized_target:
            target_index = self.dub_to_lang_combo.findData(normalized_target)
            if target_index >= 0:
                self.dub_to_lang_combo.setCurrentIndex(target_index)

            if normalized_target != current_target:
                self.logic.state.dubbing.target_language = normalized_target

        self.dub_to_lang_combo.blockSignals(False)

    def _on_dub_target_language_selected(self, _index: int = -1):
        target_language = self._combo_backend_value(self.dub_to_lang_combo)
        if target_language:
            setattr(self.logic.state.dubbing, "target_language", target_language)

    def _on_speaker_selected(self, _text: str):
        speaker_value = self._combo_backend_value(self.speaker_combo)
        if speaker_value or not self.speaker_combo.currentText().strip():
            setattr(self.logic.state.tts, "speaker", speaker_value)

    @staticmethod
    def _combo_backend_value(combo) -> str:
        current_text = combo.currentText().strip()
        current_index = combo.currentIndex()
        if current_index >= 0:
            current_item_text = combo.itemText(current_index).strip()
            if current_text and current_text != current_item_text:
                return current_text
            if not SessionTab._is_combo_index_enabled(combo, current_index):
                return ""

        combo_data = combo.currentData()
        if isinstance(combo_data, str):
            normalized_data = combo_data.strip()
            if normalized_data == SPEAKER_HEADING_VALUE:
                return ""
            if normalized_data:
                return normalized_data
        elif combo_data is not None:
            normalized_data = str(combo_data).strip()
            if normalized_data:
                return normalized_data

        return current_text

    @staticmethod
    def _set_combo_index_enabled(combo, index: int, enabled: bool):
        model = combo.model()
        if model is None or not hasattr(model, "item"):
            return
        item = model.item(index)
        if item is not None:
            item.setEnabled(bool(enabled))

    @staticmethod
    def _is_combo_index_enabled(combo, index: int) -> bool:
        model = combo.model()
        if model is None or not hasattr(model, "item"):
            return True
        item = model.item(index)
        return True if item is None else item.isEnabled()

    @staticmethod
    def _titleize_identifier(value: str) -> str:
        tokens = [token for token in re.split(r"[_\-]+", str(value or "").strip()) if token]
        return " ".join(token.capitalize() for token in tokens)

    @staticmethod
    def _split_weight_suffix(voice_token: str) -> tuple[str, str]:
        trimmed = str(voice_token or "").strip()
        weighted_match = re.fullmatch(r"(.+?)(\(\s*\d+(?:\.\d+)?\s*\))", trimmed)
        if not weighted_match:
            return trimmed, ""
        return weighted_match.group(1).strip(), f" {weighted_match.group(2)}"

    @staticmethod
    def _parse_kokoro_voice_name(raw_name: str) -> tuple[str, str]:
        normalized_name = str(raw_name or "").strip("_").strip()
        if not normalized_name:
            return "Voice", ""

        version_suffix = ""
        version_match = re.match(r"^v(\d+)_?(.*)$", normalized_name, flags=re.IGNORECASE)
        if version_match and version_match.group(2):
            normalized_name = version_match.group(2).strip("_").strip()
            version_suffix = f" (v{version_match.group(1)})"

        tokens = [token for token in re.split(r"[_\-]+", normalized_name) if token]
        display_name = " ".join(token.capitalize() for token in tokens) if tokens else "Voice"
        return display_name, version_suffix

    def _format_kokoro_voice_component(self, voice_token: str) -> str:
        token_without_weight, weight_suffix = self._split_weight_suffix(voice_token)
        prefix, separator, voice_name = token_without_weight.partition("_")
        normalized_prefix = prefix.lower().strip()

        if separator and len(normalized_prefix) == 2:
            if KOKORO_VOICE_LANGUAGE_GROUPS.get(normalized_prefix[0], "") and VOICE_GENDER_LABELS.get(
                normalized_prefix[1],
                "",
            ):
                display_name, version_suffix = self._parse_kokoro_voice_name(voice_name)
                return f"{display_name}{version_suffix}{weight_suffix}"

        if normalized_prefix in KOKORO_OPENAI_ALIAS_VOICES and not separator:
            alias_name = self._titleize_identifier(normalized_prefix)
            return f"{alias_name}{weight_suffix}"

        fallback_name = self._titleize_identifier(token_without_weight)
        return f"{fallback_name}{weight_suffix}" if fallback_name else token_without_weight

    def _format_kokoro_voice_label(self, voice_id: str) -> str:
        normalized_voice_id = str(voice_id or "").strip()
        if not normalized_voice_id:
            return ""

        parts = [part.strip() for part in normalized_voice_id.split("+") if part.strip()]
        if len(parts) > 1:
            formatted_parts = " + ".join(
                self._format_kokoro_voice_component(part)
                for part in parts
            )
            return f"Blend: {formatted_parts} ({normalized_voice_id})"

        return f"{self._format_kokoro_voice_component(normalized_voice_id)} ({normalized_voice_id})"

    def _kokoro_voice_group(self, voice_id: str) -> tuple[str, str]:
        normalized_voice_id = str(voice_id or "").strip()
        if not normalized_voice_id:
            return "Other Voices", ""

        if "+" in normalized_voice_id:
            return "Voice Blends", ""

        prefix, separator, _ = normalized_voice_id.partition("_")
        normalized_prefix = prefix.lower().strip()
        if separator and len(normalized_prefix) == 2:
            language_label = KOKORO_VOICE_LANGUAGE_GROUPS.get(normalized_prefix[0], "")
            gender_label = VOICE_GENDER_LABELS.get(normalized_prefix[1], "")
            if language_label and gender_label:
                return language_label, gender_label

        if normalized_prefix in KOKORO_OPENAI_ALIAS_VOICES and not separator:
            return "OpenAI Alias Voices", ""

        return "Other Voices", ""

    def _format_voxtral_voice_label(self, voice_id: str) -> str:
        normalized_voice_id = str(voice_id or "").strip()
        if not normalized_voice_id:
            return ""

        normalized = normalized_voice_id.lower()
        left, separator, right = normalized.partition("_")
        if separator:
            if left in VOXTRAL_STYLE_LABELS and right in VOICE_GENDER_LABELS:
                return f"{VOXTRAL_STYLE_LABELS[left]} ({normalized_voice_id})"

            if left in VOXTRAL_LANGUAGES and right in VOICE_GENDER_LABELS:
                return f"Standard ({normalized_voice_id})"

        fallback_name = self._titleize_identifier(normalized_voice_id)
        return f"{fallback_name} ({normalized_voice_id})" if fallback_name else normalized_voice_id

    def _voxtral_voice_group(self, voice_id: str) -> tuple[str, str]:
        normalized_voice_id = str(voice_id or "").strip().lower()
        if not normalized_voice_id:
            return "Other Voices", ""

        left, separator, right = normalized_voice_id.partition("_")
        if not separator:
            return "Other Voices", ""

        gender_label = VOICE_GENDER_LABELS.get(right, "")
        if not gender_label:
            return "Other Voices", ""

        if left in VOXTRAL_STYLE_LABELS:
            return "English", gender_label

        if left in VOXTRAL_LANGUAGES:
            return LANGUAGE_DISPLAY_NAMES.get(left, left.upper()), gender_label

        return "Other Voices", gender_label

    def _build_grouped_speaker_options(
        self,
        grouped_voices: dict[tuple[str, str], list[tuple[str, str]]],
        group_order: dict[str, int],
    ) -> list[tuple[str, str, bool]]:
        gender_order = {"Female": 0, "Male": 1, "": 2}
        sorted_keys = sorted(
            grouped_voices.keys(),
            key=lambda key: (
                group_order.get(key[0], 999),
                key[0],
                gender_order.get(key[1], 99),
                key[1],
            ),
        )

        options: list[tuple[str, str, bool]] = []
        current_language = ""
        for language_label, gender_label in sorted_keys:
            if language_label and language_label != current_language:
                options.append((f"[ {language_label} ]", SPEAKER_HEADING_VALUE, False))
                current_language = language_label

            if gender_label:
                options.append((f"  - {gender_label} -", SPEAKER_HEADING_VALUE, False))

            for voice_label, voice_id in grouped_voices[(language_label, gender_label)]:
                options.append((f"    {voice_label}", voice_id, True))

        return options

    def _build_kokoro_speaker_options(self, speaker_ids: list[str]) -> list[tuple[str, str, bool]]:
        seen: set[str] = set()
        grouped_voices: dict[tuple[str, str], list[tuple[str, str]]] = {}

        for speaker_id in speaker_ids:
            normalized_speaker_id = str(speaker_id or "").strip()
            if not normalized_speaker_id:
                continue

            dedupe_key = normalized_speaker_id.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            group_key = self._kokoro_voice_group(normalized_speaker_id)
            grouped_voices.setdefault(group_key, []).append(
                (
                    self._format_kokoro_voice_label(normalized_speaker_id) or normalized_speaker_id,
                    normalized_speaker_id,
                )
            )

        if not grouped_voices:
            return []

        language_order = {
            label: index
            for index, label in enumerate(
                list(KOKORO_VOICE_LANGUAGE_GROUPS.values()) + KOKORO_MISC_GROUP_ORDER,
            )
        }
        return self._build_grouped_speaker_options(grouped_voices, language_order)

    def _build_voxtral_speaker_options(self, speaker_ids: list[str]) -> list[tuple[str, str, bool]]:
        seen: set[str] = set()
        grouped_voices: dict[tuple[str, str], list[tuple[str, str]]] = {}

        for speaker_id in speaker_ids:
            normalized_speaker_id = str(speaker_id or "").strip()
            if not normalized_speaker_id:
                continue

            dedupe_key = normalized_speaker_id.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            group_key = self._voxtral_voice_group(normalized_speaker_id)
            grouped_voices.setdefault(group_key, []).append(
                (
                    self._format_voxtral_voice_label(normalized_speaker_id) or normalized_speaker_id,
                    normalized_speaker_id,
                )
            )

        if not grouped_voices:
            return []

        language_order = {
            self._language_label_for_service("Voxtral", language_code): index
            for index, language_code in enumerate(VOXTRAL_LANGUAGES)
        }
        language_order["Other Voices"] = len(language_order)
        return self._build_grouped_speaker_options(grouped_voices, language_order)

    def _build_speaker_combo_options(self, service: str, speaker_ids: list[str]) -> list[tuple[str, str, bool]]:
        if service == "Kokoro":
            return self._build_kokoro_speaker_options(speaker_ids)
        if service == "Voxtral":
            return self._build_voxtral_speaker_options(speaker_ids)

        seen: set[str] = set()
        options: list[tuple[str, str, bool]] = []

        for speaker_id in speaker_ids:
            normalized_speaker_id = str(speaker_id or "").strip()
            if not normalized_speaker_id:
                continue

            dedupe_key = normalized_speaker_id.lower()
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)

            options.append((normalized_speaker_id, normalized_speaker_id, True))

        return options

    @staticmethod
    def _language_codes_for_service(service: str) -> list[str]:
        if service == "Kokoro":
            return KOKORO_LANGUAGES
        if service == "Voxtral":
            return VOXTRAL_LANGUAGES
        if service in {"XTTS", "OpenAI", "Gemini", "OpenAI-Compatible"}:
            return XTTS_LANGUAGES
        return []

    @staticmethod
    def _normalize_language_code_for_service(service: str, language_value: str) -> str:
        normalized = str(language_value or "").strip().lower()
        if not normalized:
            return ""

        if service == "Kokoro":
            kokoro_aliases = {
                "en-us": "en",
                "pt-br": "pt",
                "fr-fr": "fr",
                "zh": "zh-cn",
            }
            normalized = kokoro_aliases.get(normalized, normalized)

        return normalized

    @staticmethod
    def _language_label_for_service(service: str, language_code: str) -> str:
        normalized = str(language_code or "").strip().lower()
        if service == "Kokoro" and normalized == "en":
            return "American English"
        if service == "Kokoro" and normalized == "en-gb":
            return "British English"
        return LANGUAGE_DISPLAY_NAMES.get(normalized, str(language_code or "").strip())

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
