from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QProgressBar,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ...constants import WHISPER_LANGUAGES, XTTS_LANGUAGES


def create_section_label(text: str) -> QLabel:
    label = QLabel(text)
    font = QFont()
    font.setPointSize(13)
    font.setBold(True)
    label.setFont(font)
    label.setObjectName("sessionSectionLabel")
    return label


class SessionHeader(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        self.session_name_label = QLabel("Untitled Session")
        session_name_font = QFont()
        session_name_font.setPointSize(20)
        session_name_font.setBold(True)
        self.session_name_label.setFont(session_name_font)
        self.session_name_label.setObjectName("sessionNameLabel")

        self.lifecycle_status_label = QLabel("Idle")
        lifecycle_font = QFont()
        lifecycle_font.setPointSize(10)
        lifecycle_font.setBold(True)
        self.lifecycle_status_label.setFont(lifecycle_font)
        self.lifecycle_status_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.lifecycle_status_label.setMinimumWidth(120)
        self.lifecycle_status_label.setObjectName("sessionLifecycleLabel")

        layout.addWidget(self.session_name_label)
        layout.addStretch()
        layout.addWidget(self.lifecycle_status_label)


class SessionControlsSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rowFrame")

        layout = QHBoxLayout(self)
        layout.setSpacing(8)

        self.new_session_button = QPushButton("New Session")
        self.load_session_button = QPushButton("Load Session")
        self.view_session_folder_button = QPushButton("View Session Folder")
        self.delete_session_button = QPushButton("Delete Session")
        self.delete_session_button.setObjectName("dangerButton")

        layout.addWidget(self.new_session_button)
        layout.addWidget(self.load_session_button)
        layout.addWidget(self.view_session_folder_button)
        layout.addWidget(self.delete_session_button)


class SourceFileSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rowFrame")

        layout = QHBoxLayout(self)
        layout.setSpacing(8)

        self.select_file_button = QPushButton("Select File")
        self.paste_text_button = QPushButton("Paste or Write")
        self.download_url_button = QPushButton("Download from URL")
        self.selected_file_label = QLabel("No file selected")
        self.selected_file_label.setObjectName("secondaryInfoLabel")

        layout.addWidget(self.select_file_button)
        layout.addWidget(self.paste_text_button)
        layout.addWidget(self.download_url_button)
        layout.addStretch()
        layout.addWidget(self.selected_file_label)


class TtsSettingsSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("groupFrame")

        layout = QGridLayout(self)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.tts_service_combo = QComboBox()
        self.tts_service_combo.addItems(["XTTS", "Voxtral", "Silero", "Gemini", "OpenAI"])
        layout.addWidget(QLabel("TTS Service:"), 0, 0)
        layout.addWidget(self.tts_service_combo, 0, 1)

        self.connect_server_button = QPushButton("Connect to Server")
        self.connect_server_button.setObjectName("primaryButton")
        layout.addWidget(self.connect_server_button, 0, 2, 1, 2)

        self.use_external_server_checkbox = QCheckBox("Use External Server")
        self.external_server_url_edit = QLineEdit()
        self.external_server_url_edit.setPlaceholderText("http://localhost:8020")
        layout.addWidget(self.use_external_server_checkbox, 1, 0)
        layout.addWidget(self.external_server_url_edit, 1, 1, 1, 3)

        self.xtts_model_label = QLabel("XTTS Model:")
        self.xtts_model_combo = QComboBox()
        self.xtts_model_combo.setEditable(True)
        layout.addWidget(self.xtts_model_label, 2, 0)
        layout.addWidget(self.xtts_model_combo, 2, 1)

        self.language_label = QLabel("Language:")
        self.language_combo = QComboBox()
        layout.addWidget(self.language_label, 3, 0)
        layout.addWidget(self.language_combo, 3, 1)

        self.speaker_label = QLabel("Speaker Voice:")
        self.speaker_combo = QComboBox()
        self.speaker_combo.setEditable(True)
        layout.addWidget(self.speaker_label, 4, 0)
        layout.addWidget(self.speaker_combo, 4, 1)

        self.upload_voice_button = QPushButton("Upload New Voices")
        layout.addWidget(self.upload_voice_button, 4, 2)

        layout.addWidget(QLabel("Speed:"), 5, 0)
        speed_layout = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(20, 200)
        self.speed_label = QLabel("1.00")
        self.speed_label.setObjectName("secondaryInfoLabel")
        speed_layout.addWidget(self.speed_slider)
        speed_layout.addWidget(self.speed_label)
        layout.addLayout(speed_layout, 5, 1, 1, 3)

        self.advanced_tts_checkbox = QCheckBox("Advanced XTTS Settings")
        layout.addWidget(self.advanced_tts_checkbox, 6, 0)

        self.cloud_provider_hint = QLabel(
            "OpenAI and Gemini use API keys from the API Keys tab."
        )
        self.cloud_provider_hint.setWordWrap(True)
        self.cloud_provider_hint.setObjectName("secondaryInfoLabel")
        layout.addWidget(self.cloud_provider_hint, 7, 0, 1, 4)

        self.openai_audio_instructions_label = QLabel("Voice Instructions:")
        self.openai_audio_instructions_edit = QLineEdit()
        self.openai_audio_instructions_edit.setPlaceholderText(
            "Optional style guidance for OpenAI speech"
        )
        layout.addWidget(self.openai_audio_instructions_label, 8, 0)
        layout.addWidget(self.openai_audio_instructions_edit, 8, 1, 1, 3)


class AdvancedTtsSettingsSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("groupFrame")

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.xtts_advanced_settings_frame = QFrame()
        self.xtts_advanced_settings_frame.setObjectName("subGroupFrame")
        xtts_layout = QGridLayout(self.xtts_advanced_settings_frame)
        xtts_layout.setHorizontalSpacing(10)
        xtts_layout.setVerticalSpacing(8)

        self.adv_tts_temp_spinbox = QDoubleSpinBox()
        self.adv_tts_temp_spinbox.setRange(0.0, 2.0)
        self.adv_tts_temp_spinbox.setSingleStep(0.05)
        xtts_layout.addWidget(QLabel("Temperature:"), 0, 0)
        xtts_layout.addWidget(self.adv_tts_temp_spinbox, 0, 1)

        self.adv_tts_len_penalty_spinbox = QDoubleSpinBox()
        self.adv_tts_len_penalty_spinbox.setRange(-10.0, 10.0)
        self.adv_tts_len_penalty_spinbox.setSingleStep(0.1)
        xtts_layout.addWidget(QLabel("Length Penalty:"), 1, 0)
        xtts_layout.addWidget(self.adv_tts_len_penalty_spinbox, 1, 1)

        self.adv_tts_rep_penalty_spinbox = QDoubleSpinBox()
        self.adv_tts_rep_penalty_spinbox.setRange(0.0, 100.0)
        self.adv_tts_rep_penalty_spinbox.setSingleStep(0.1)
        xtts_layout.addWidget(QLabel("Repetition Penalty:"), 2, 0)
        xtts_layout.addWidget(self.adv_tts_rep_penalty_spinbox, 2, 1)

        self.adv_tts_top_k_spinbox = QSpinBox()
        self.adv_tts_top_k_spinbox.setRange(0, 500)
        xtts_layout.addWidget(QLabel("Top K:"), 3, 0)
        xtts_layout.addWidget(self.adv_tts_top_k_spinbox, 3, 1)

        self.adv_tts_top_p_spinbox = QDoubleSpinBox()
        self.adv_tts_top_p_spinbox.setRange(0.0, 1.0)
        self.adv_tts_top_p_spinbox.setSingleStep(0.05)
        xtts_layout.addWidget(QLabel("Top P:"), 4, 0)
        xtts_layout.addWidget(self.adv_tts_top_p_spinbox, 4, 1)

        self.adv_tts_do_sample_checkbox = QCheckBox("Enable Sampling (do_sample)")
        xtts_layout.addWidget(self.adv_tts_do_sample_checkbox, 5, 0, 1, 2)

        self.adv_tts_num_beams_spinbox = QSpinBox()
        self.adv_tts_num_beams_spinbox.setRange(1, 16)
        xtts_layout.addWidget(QLabel("Num Beams:"), 6, 0)
        xtts_layout.addWidget(self.adv_tts_num_beams_spinbox, 6, 1)

        self.adv_tts_chunk_size_spinbox = QSpinBox()
        self.adv_tts_chunk_size_spinbox.setRange(1, 500)
        xtts_layout.addWidget(QLabel("Stream Chunk Size:"), 7, 0)
        xtts_layout.addWidget(self.adv_tts_chunk_size_spinbox, 7, 1)

        self.adv_tts_text_split_checkbox = QCheckBox("Enable Text Splitting")
        xtts_layout.addWidget(self.adv_tts_text_split_checkbox, 8, 0, 1, 2)

        self.adv_tts_gpt_cond_len_spinbox = QSpinBox()
        self.adv_tts_gpt_cond_len_spinbox.setRange(1, 60)
        xtts_layout.addWidget(QLabel("GPT Cond Len (s):"), 9, 0)
        xtts_layout.addWidget(self.adv_tts_gpt_cond_len_spinbox, 9, 1)

        self.adv_tts_gpt_cond_chunk_len_spinbox = QSpinBox()
        self.adv_tts_gpt_cond_chunk_len_spinbox.setRange(1, 60)
        xtts_layout.addWidget(QLabel("GPT Cond Chunk Len (s):"), 10, 0)
        xtts_layout.addWidget(self.adv_tts_gpt_cond_chunk_len_spinbox, 10, 1)

        self.adv_tts_max_ref_len_spinbox = QSpinBox()
        self.adv_tts_max_ref_len_spinbox.setRange(1, 60)
        xtts_layout.addWidget(QLabel("Max Ref Len (s):"), 11, 0)
        xtts_layout.addWidget(self.adv_tts_max_ref_len_spinbox, 11, 1)

        self.adv_tts_sound_norm_refs_checkbox = QCheckBox("Normalize Reference Audio")
        xtts_layout.addWidget(self.adv_tts_sound_norm_refs_checkbox, 12, 0, 1, 2)

        self.adv_tts_overlap_wav_len_spinbox = QSpinBox()
        self.adv_tts_overlap_wav_len_spinbox.setRange(0, 100000)
        xtts_layout.addWidget(QLabel("Overlap WAV Len:"), 13, 0)
        xtts_layout.addWidget(self.adv_tts_overlap_wav_len_spinbox, 13, 1)

        self.adv_tts_apply_button = QPushButton("Save Advanced Settings")
        xtts_layout.addWidget(self.adv_tts_apply_button, 14, 0, 1, 2)

        layout.addWidget(self.xtts_advanced_settings_frame)

        self.voxtral_advanced_settings_frame = QFrame()
        self.voxtral_advanced_settings_frame.setObjectName("subGroupFrame")
        voxtral_layout = QGridLayout(self.voxtral_advanced_settings_frame)
        voxtral_layout.setHorizontalSpacing(10)
        voxtral_layout.setVerticalSpacing(8)

        self.voxtral_advanced_hint_label = QLabel(
            "Optional Voxtral settings are sent via instructions automatically."
        )
        self.voxtral_advanced_hint_label.setWordWrap(True)
        self.voxtral_advanced_hint_label.setObjectName("secondaryInfoLabel")
        voxtral_layout.addWidget(self.voxtral_advanced_hint_label, 0, 0, 1, 2)

        self.voxtral_max_frames_spinbox = QSpinBox()
        self.voxtral_max_frames_spinbox.setRange(1, 4000)
        voxtral_layout.addWidget(QLabel("Max Frames:"), 1, 0)
        voxtral_layout.addWidget(self.voxtral_max_frames_spinbox, 1, 1)

        self.voxtral_euler_steps_spinbox = QSpinBox()
        self.voxtral_euler_steps_spinbox.setRange(1, 32)
        voxtral_layout.addWidget(QLabel("Euler Steps:"), 2, 0)
        voxtral_layout.addWidget(self.voxtral_euler_steps_spinbox, 2, 1)

        self.voxtral_chunk_checkbox = QCheckBox("Enable Chunking")
        voxtral_layout.addWidget(self.voxtral_chunk_checkbox, 3, 0, 1, 2)

        self.voxtral_max_chunk_chars_spinbox = QSpinBox()
        self.voxtral_max_chunk_chars_spinbox.setRange(50, 5000)
        voxtral_layout.addWidget(QLabel("Max Chunk Chars:"), 4, 0)
        voxtral_layout.addWidget(self.voxtral_max_chunk_chars_spinbox, 4, 1)

        self.voxtral_chunk_silence_ms_spinbox = QSpinBox()
        self.voxtral_chunk_silence_ms_spinbox.setRange(0, 10000)
        voxtral_layout.addWidget(QLabel("Chunk Silence (ms):"), 5, 0)
        voxtral_layout.addWidget(self.voxtral_chunk_silence_ms_spinbox, 5, 1)

        self.voxtral_strip_quotes_checkbox = QCheckBox("Strip Quotes")
        voxtral_layout.addWidget(self.voxtral_strip_quotes_checkbox, 6, 0, 1, 2)

        self.voxtral_strip_diacritics_checkbox = QCheckBox("Strip Diacritics")
        voxtral_layout.addWidget(self.voxtral_strip_diacritics_checkbox, 7, 0, 1, 2)

        self.voxtral_level_audio_checkbox = QCheckBox("Level Audio")
        voxtral_layout.addWidget(self.voxtral_level_audio_checkbox, 8, 0, 1, 2)

        layout.addWidget(self.voxtral_advanced_settings_frame)


class DubbingSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("groupFrame")

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.transcription_heading = QLabel("Transcription Options")
        self.transcription_heading.setObjectName("subSectionLabel")
        layout.addWidget(self.transcription_heading)

        self.transcription_frame = QFrame()
        self.transcription_frame.setObjectName("subGroupFrame")
        trans_layout = QGridLayout(self.transcription_frame)
        layout.addWidget(self.transcription_frame)

        trans_layout.addWidget(QLabel("Language:"), 0, 0)
        self.dub_whisper_lang_combo = QComboBox()
        self.dub_whisper_lang_combo.addItems(WHISPER_LANGUAGES)
        trans_layout.addWidget(self.dub_whisper_lang_combo, 0, 1)

        trans_layout.addWidget(QLabel("Model:"), 0, 2)
        self.dub_whisper_model_combo = QComboBox()
        self.dub_whisper_model_combo.addItems([
            "small",
            "small.en",
            "medium",
            "medium.en",
            "large-v2",
            "large-v3",
        ])
        trans_layout.addWidget(self.dub_whisper_model_combo, 0, 3)

        self.dub_correct_transcription_check = QCheckBox("Correct transcription with LLM")
        trans_layout.addWidget(self.dub_correct_transcription_check, 1, 0, 1, 2)

        self.dub_custom_prompt_button = QPushButton("Custom Correction Prompt")
        trans_layout.addWidget(self.dub_custom_prompt_button, 1, 2, 1, 2)

        self.translation_heading = QLabel("Translation Options")
        self.translation_heading.setObjectName("subSectionLabel")
        layout.addWidget(self.translation_heading)

        self.translation_frame = QFrame()
        self.translation_frame.setObjectName("subGroupFrame")
        transl_layout = QGridLayout(self.translation_frame)
        layout.addWidget(self.translation_frame)

        self.dub_translate_check = QCheckBox("Translate subtitles")
        transl_layout.addWidget(self.dub_translate_check, 0, 0, 1, 4)

        transl_layout.addWidget(QLabel("From:"), 1, 0)
        self.dub_from_lang_combo = QComboBox()
        self.dub_from_lang_combo.addItems(WHISPER_LANGUAGES)
        transl_layout.addWidget(self.dub_from_lang_combo, 1, 1)

        transl_layout.addWidget(QLabel("To:"), 1, 2)
        self.dub_to_lang_combo = QComboBox()
        self.dub_to_lang_combo.addItems(XTTS_LANGUAGES)
        transl_layout.addWidget(self.dub_to_lang_combo, 1, 3)

        self.dub_cot_check = QCheckBox("Enable chain-of-thought")
        transl_layout.addWidget(self.dub_cot_check, 2, 0, 1, 2)

        self.dub_glossary_check = QCheckBox("Enable glossary")
        transl_layout.addWidget(self.dub_glossary_check, 2, 2, 1, 2)

        transl_layout.addWidget(QLabel("Translation/Correction Model:"), 3, 0)
        self.dub_trans_model_combo = QComboBox()
        self.dub_trans_model_combo.addItems([
            "GPT 5.4",
            "GPT 5.4-mini",
            "Gemini 3.1 Pro",
            "Gemini 3.0 Flash",
            "Opus 4.7",
            "Sonnet 4.6",
            "DeepL",
            "Custom (LiteLLM)",
        ])
        transl_layout.addWidget(self.dub_trans_model_combo, 3, 1, 1, 3)

        transl_layout.addWidget(QLabel("Custom LiteLLM Model:"), 4, 0)
        self.dub_custom_model_edit = QLineEdit()
        self.dub_custom_model_edit.setPlaceholderText("provider/model (for example openai/gpt-5.4-mini)")
        transl_layout.addWidget(self.dub_custom_model_edit, 4, 1, 1, 3)

        transl_layout.addWidget(QLabel("Custom API Base:"), 5, 0)
        self.dub_custom_api_base_edit = QLineEdit()
        self.dub_custom_api_base_edit.setPlaceholderText("Optional for OpenAI-compatible models (for example http://localhost:1234/v1)")
        transl_layout.addWidget(self.dub_custom_api_base_edit, 5, 1, 1, 3)

        self.video_file_frame = QFrame()
        self.video_file_frame.setObjectName("rowFrame")
        video_file_layout = QHBoxLayout(self.video_file_frame)
        layout.addWidget(self.video_file_frame)

        video_file_layout.addWidget(QLabel("Video File:"))
        self.selected_video_file_label = QLabel("No video selected")
        self.selected_video_file_label.setObjectName("secondaryInfoLabel")
        video_file_layout.addWidget(self.selected_video_file_label)
        self.select_video_file_button = QPushButton("Select Video")
        video_file_layout.addWidget(self.select_video_file_button)

        self.buttons_frame = QFrame()
        self.buttons_frame.setObjectName("rowFrame")
        buttons_layout = QGridLayout(self.buttons_frame)
        layout.addWidget(self.buttons_frame)

        self.generate_dub_audio_button = QPushButton("Generate Dubbing Audio")
        self.add_dub_to_video_button = QPushButton("Add Dubbing to Video")
        self.only_transcribe_button = QPushButton("Only Transcribe")
        self.only_correct_button = QPushButton("Only Correct")
        self.only_translate_button = QPushButton("Only Translate")

        buttons_layout.addWidget(self.generate_dub_audio_button, 0, 0, 1, 3)
        buttons_layout.addWidget(self.add_dub_to_video_button, 1, 0, 1, 3)
        buttons_layout.addWidget(self.only_transcribe_button, 2, 0)
        buttons_layout.addWidget(self.only_correct_button, 2, 1)
        buttons_layout.addWidget(self.only_translate_button, 2, 2)


class OutputOptionsSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("rowFrame")

        layout = QHBoxLayout(self)
        layout.setSpacing(8)

        layout.addWidget(QLabel("Format:"))
        self.format_combo = QComboBox()
        self.format_combo.addItems(["m4b", "opus", "mp3", "wav"])
        layout.addWidget(self.format_combo)

        layout.addWidget(QLabel("Bitrate:"))
        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["16k", "32k", "64k", "128k", "196k", "312k"])
        layout.addWidget(self.bitrate_combo)

        self.upload_cover_button = QPushButton("Upload Cover")
        layout.addWidget(self.upload_cover_button)

        self.metadata_button = QPushButton("Metadata")
        layout.addWidget(self.metadata_button)

        layout.addStretch()


class GenerationSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("groupFrame")

        layout = QGridLayout(self)

        self.start_button = QPushButton("Start Generation")
        self.start_button.setObjectName("primaryButton")
        self.resume_button = QPushButton("Resume Generation")
        self.stop_button = QPushButton("Stop Generation")
        self.cancel_button = QPushButton("Cancel Generation")
        self.cancel_button.setObjectName("dangerButton")

        layout.addWidget(self.start_button, 0, 0)
        layout.addWidget(self.resume_button, 0, 1)
        layout.addWidget(self.stop_button, 0, 2)
        layout.addWidget(self.cancel_button, 0, 3)

        self.progress_bar = QProgressBar()
        layout.addWidget(self.progress_bar, 1, 0, 1, 4)

        self.progress_label = QLabel("0.00%")
        self.remaining_time_label = QLabel("N/A")
        self.remaining_time_label.setObjectName("secondaryInfoLabel")
        layout.addWidget(QLabel("Progress:"), 2, 0)
        layout.addWidget(self.progress_label, 2, 1)
        layout.addWidget(
            QLabel("Estimated Remaining Time:"),
            2,
            2,
            alignment=Qt.AlignmentFlag.AlignRight,
        )
        layout.addWidget(self.remaining_time_label, 2, 3)
