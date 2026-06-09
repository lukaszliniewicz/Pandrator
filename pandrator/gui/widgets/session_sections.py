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

from ...constants import LANGUAGE_DISPLAY_NAMES, WHISPER_LANGUAGES, XTTS_LANGUAGES


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

        self.select_file_button = QPushButton("Add Source")
        self.paste_text_button = QPushButton("Paste or Write")
        self.download_url_button = QPushButton("Download from URL")
        self.selected_file_label = QLabel("No file selected")
        self.selected_file_label.setObjectName("secondaryInfoLabel")
        self.selected_file_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.selected_file_label.setMinimumWidth(180)

        layout.addWidget(self.select_file_button)
        layout.addWidget(self.paste_text_button)
        layout.addWidget(self.download_url_button)
        layout.addWidget(self.selected_file_label, 1)


class TtsSettingsSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("groupFrame")

        layout = QGridLayout(self)
        layout.setHorizontalSpacing(10)
        layout.setVerticalSpacing(8)

        self.tts_service_combo = QComboBox()
        self.tts_service_combo.addItems(["XTTS", "VoxCPM", "FishS2", "Voxtral", "Kokoro", "Silero", "Chatterbox", "OpenAI-Compatible"])
        layout.addWidget(QLabel("TTS Service:"), 0, 0)
        layout.addWidget(self.tts_service_combo, 0, 1)

        self.connect_server_button = QPushButton("Connect to Server")
        self.connect_server_button.setObjectName("primaryButton")
        layout.addWidget(self.connect_server_button, 0, 2, 1, 2)

        self.cloud_provider_label = QLabel("Cloud Provider:")
        self.cloud_provider_combo = QComboBox()
        layout.addWidget(self.cloud_provider_label, 1, 0)
        layout.addWidget(self.cloud_provider_combo, 1, 1, 1, 3)

        self.use_external_server_checkbox = QCheckBox("Use External Server")
        self.external_server_url_edit = QLineEdit()
        self.external_server_url_edit.setPlaceholderText("http://localhost:8020")
        layout.addWidget(self.use_external_server_checkbox, 2, 0)
        layout.addWidget(self.external_server_url_edit, 2, 1, 1, 3)

        self.xtts_model_label = QLabel("XTTS Model:")
        self.xtts_model_combo = QComboBox()
        self.xtts_model_combo.setEditable(True)
        layout.addWidget(self.xtts_model_label, 3, 0)
        layout.addWidget(self.xtts_model_combo, 3, 1)

        self.language_label = QLabel("Language:")
        self.language_combo = QComboBox()
        layout.addWidget(self.language_label, 4, 0)
        layout.addWidget(self.language_combo, 4, 1)

        self.speaker_label = QLabel("Speaker Voice:")
        self.speaker_combo = QComboBox()
        self.speaker_combo.setEditable(True)
        layout.addWidget(self.speaker_label, 5, 0)
        layout.addWidget(self.speaker_combo, 5, 1)

        self.upload_voice_button = QPushButton("Manage Voices")
        layout.addWidget(self.upload_voice_button, 5, 2)
        self.browse_voices_button = QPushButton("Browse Voices")
        layout.addWidget(self.browse_voices_button, 5, 3)

        self.voice_mode_hint_label = QLabel("")
        self.voice_mode_hint_label.setWordWrap(True)
        self.voice_mode_hint_label.setObjectName("secondaryInfoLabel")
        layout.addWidget(self.voice_mode_hint_label, 6, 0, 1, 4)

        layout.addWidget(QLabel("Speed:"), 7, 0)
        speed_layout = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Orientation.Horizontal)
        self.speed_slider.setRange(20, 200)
        self.speed_label = QLabel("1.00")
        self.speed_label.setObjectName("secondaryInfoLabel")
        speed_layout.addWidget(self.speed_slider)
        speed_layout.addWidget(self.speed_label)
        layout.addLayout(speed_layout, 7, 1, 1, 3)

        self.advanced_tts_checkbox = QCheckBox("Advanced XTTS Settings")
        layout.addWidget(self.advanced_tts_checkbox, 8, 0)

        self.cloud_provider_hint = QLabel(
            "Cloud voices use providers configured in the Providers tab."
        )
        self.cloud_provider_hint.setWordWrap(True)
        self.cloud_provider_hint.setObjectName("secondaryInfoLabel")
        layout.addWidget(self.cloud_provider_hint, 9, 0, 1, 4)

        self.openai_audio_instructions_label = QLabel("Voice Instructions:")
        self.openai_audio_instructions_edit = QLineEdit()
        self.openai_audio_instructions_edit.setPlaceholderText(
            "Optional style guidance for OpenAI speech"
        )
        layout.addWidget(self.openai_audio_instructions_label, 10, 0)
        layout.addWidget(self.openai_audio_instructions_edit, 10, 1, 1, 3)


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
        self.adv_tts_temp_send_checkbox = QCheckBox("Send")

        self.xtts_send_hint_label = QLabel(
            "Check Send only for parameters you want to include in XTTS requests."
        )
        self.xtts_send_hint_label.setWordWrap(True)
        self.xtts_send_hint_label.setObjectName("secondaryInfoLabel")
        xtts_layout.addWidget(self.xtts_send_hint_label, 0, 0, 1, 3)
        xtts_layout.addWidget(self.adv_tts_temp_send_checkbox, 1, 0)
        xtts_layout.addWidget(QLabel("Temperature:"), 1, 1)
        xtts_layout.addWidget(self.adv_tts_temp_spinbox, 1, 2)

        self.adv_tts_len_penalty_spinbox = QDoubleSpinBox()
        self.adv_tts_len_penalty_spinbox.setRange(-10.0, 10.0)
        self.adv_tts_len_penalty_spinbox.setSingleStep(0.1)
        self.adv_tts_len_penalty_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_len_penalty_send_checkbox, 2, 0)
        xtts_layout.addWidget(QLabel("Length Penalty:"), 2, 1)
        xtts_layout.addWidget(self.adv_tts_len_penalty_spinbox, 2, 2)

        self.adv_tts_rep_penalty_spinbox = QDoubleSpinBox()
        self.adv_tts_rep_penalty_spinbox.setRange(0.0, 100.0)
        self.adv_tts_rep_penalty_spinbox.setSingleStep(0.1)
        self.adv_tts_rep_penalty_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_rep_penalty_send_checkbox, 3, 0)
        xtts_layout.addWidget(QLabel("Repetition Penalty:"), 3, 1)
        xtts_layout.addWidget(self.adv_tts_rep_penalty_spinbox, 3, 2)

        self.adv_tts_top_k_spinbox = QSpinBox()
        self.adv_tts_top_k_spinbox.setRange(0, 500)
        self.adv_tts_top_k_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_top_k_send_checkbox, 4, 0)
        xtts_layout.addWidget(QLabel("Top K:"), 4, 1)
        xtts_layout.addWidget(self.adv_tts_top_k_spinbox, 4, 2)

        self.adv_tts_top_p_spinbox = QDoubleSpinBox()
        self.adv_tts_top_p_spinbox.setRange(0.0, 1.0)
        self.adv_tts_top_p_spinbox.setSingleStep(0.05)
        self.adv_tts_top_p_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_top_p_send_checkbox, 5, 0)
        xtts_layout.addWidget(QLabel("Top P:"), 5, 1)
        xtts_layout.addWidget(self.adv_tts_top_p_spinbox, 5, 2)

        self.adv_tts_do_sample_checkbox = QCheckBox("Enable Sampling (do_sample)")
        self.adv_tts_do_sample_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_do_sample_send_checkbox, 6, 0)
        xtts_layout.addWidget(self.adv_tts_do_sample_checkbox, 6, 1, 1, 2)

        self.adv_tts_num_beams_spinbox = QSpinBox()
        self.adv_tts_num_beams_spinbox.setRange(1, 16)
        self.adv_tts_num_beams_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_num_beams_send_checkbox, 7, 0)
        xtts_layout.addWidget(QLabel("Num Beams:"), 7, 1)
        xtts_layout.addWidget(self.adv_tts_num_beams_spinbox, 7, 2)

        self.adv_tts_chunk_size_spinbox = QSpinBox()
        self.adv_tts_chunk_size_spinbox.setRange(1, 500)
        self.adv_tts_chunk_size_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_chunk_size_send_checkbox, 8, 0)
        xtts_layout.addWidget(QLabel("Stream Chunk Size:"), 8, 1)
        xtts_layout.addWidget(self.adv_tts_chunk_size_spinbox, 8, 2)

        self.adv_tts_text_split_checkbox = QCheckBox("Enable Text Splitting")
        self.adv_tts_text_split_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_text_split_send_checkbox, 9, 0)
        xtts_layout.addWidget(self.adv_tts_text_split_checkbox, 9, 1, 1, 2)

        self.adv_tts_gpt_cond_len_spinbox = QSpinBox()
        self.adv_tts_gpt_cond_len_spinbox.setRange(1, 60)
        self.adv_tts_gpt_cond_len_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_gpt_cond_len_send_checkbox, 10, 0)
        xtts_layout.addWidget(QLabel("GPT Cond Len (s):"), 10, 1)
        xtts_layout.addWidget(self.adv_tts_gpt_cond_len_spinbox, 10, 2)

        self.adv_tts_gpt_cond_chunk_len_spinbox = QSpinBox()
        self.adv_tts_gpt_cond_chunk_len_spinbox.setRange(1, 60)
        self.adv_tts_gpt_cond_chunk_len_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_gpt_cond_chunk_len_send_checkbox, 11, 0)
        xtts_layout.addWidget(QLabel("GPT Cond Chunk Len (s):"), 11, 1)
        xtts_layout.addWidget(self.adv_tts_gpt_cond_chunk_len_spinbox, 11, 2)

        self.adv_tts_max_ref_len_spinbox = QSpinBox()
        self.adv_tts_max_ref_len_spinbox.setRange(1, 60)
        self.adv_tts_max_ref_len_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_max_ref_len_send_checkbox, 12, 0)
        xtts_layout.addWidget(QLabel("Max Ref Len (s):"), 12, 1)
        xtts_layout.addWidget(self.adv_tts_max_ref_len_spinbox, 12, 2)

        self.adv_tts_sound_norm_refs_checkbox = QCheckBox("Normalize Reference Audio")
        self.adv_tts_sound_norm_refs_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_sound_norm_refs_send_checkbox, 13, 0)
        xtts_layout.addWidget(self.adv_tts_sound_norm_refs_checkbox, 13, 1, 1, 2)

        self.adv_tts_overlap_wav_len_spinbox = QSpinBox()
        self.adv_tts_overlap_wav_len_spinbox.setRange(0, 100000)
        self.adv_tts_overlap_wav_len_send_checkbox = QCheckBox("Send")
        xtts_layout.addWidget(self.adv_tts_overlap_wav_len_send_checkbox, 14, 0)
        xtts_layout.addWidget(QLabel("Overlap WAV Len:"), 14, 1)
        xtts_layout.addWidget(self.adv_tts_overlap_wav_len_spinbox, 14, 2)

        self.adv_tts_apply_button = QPushButton("Save Advanced Settings")
        xtts_layout.addWidget(self.adv_tts_apply_button, 15, 0, 1, 3)

        layout.addWidget(self.xtts_advanced_settings_frame)

        self.voxcpm_advanced_settings_frame = QFrame()
        self.voxcpm_advanced_settings_frame.setObjectName("subGroupFrame")
        voxcpm_layout = QGridLayout(self.voxcpm_advanced_settings_frame)
        voxcpm_layout.setHorizontalSpacing(10)
        voxcpm_layout.setVerticalSpacing(8)

        def _format_voxcpm_tooltip(title: str, lines: list[str], note: str = "") -> str:
            tooltip = f"<b>{title}</b><br><br>"
            tooltip += "<br>".join(f"- {line}" for line in lines)
            if note:
                tooltip += f"<br><br><i>{note}</i>"
            return tooltip

        def _apply_tooltip(widgets: list, text: str):
            for widget in widgets:
                widget.setToolTip(text)

        self.voxcpm_advanced_hint_label = QLabel(
            "VoxCPM inference controls are sent in the request voxcpm object."
        )
        self.voxcpm_advanced_hint_label.setWordWrap(True)
        self.voxcpm_advanced_hint_label.setObjectName("secondaryInfoLabel")
        voxcpm_layout.addWidget(self.voxcpm_advanced_hint_label, 0, 0, 1, 2)
        self.voxcpm_advanced_hint_label.setToolTip(
            _format_voxcpm_tooltip(
                "VoxCPM Advanced Settings",
                [
                    "All controls in this section are sent as the voxcpm object.",
                    "Use conservative values first, then tune one parameter at a time.",
                ],
            )
        )

        self.voxcpm_cfg_value_spinbox = QDoubleSpinBox()
        self.voxcpm_cfg_value_spinbox.setDecimals(2)
        self.voxcpm_cfg_value_spinbox.setRange(0.1, 20.0)
        self.voxcpm_cfg_value_spinbox.setSingleStep(0.1)
        self.voxcpm_cfg_value_label = QLabel("CFG Value:")
        voxcpm_layout.addWidget(self.voxcpm_cfg_value_label, 1, 0)
        voxcpm_layout.addWidget(self.voxcpm_cfg_value_spinbox, 1, 1)
        _apply_tooltip(
            [self.voxcpm_cfg_value_label, self.voxcpm_cfg_value_spinbox],
            _format_voxcpm_tooltip(
                "CFG Value",
                [
                    "Higher values enforce stronger conditioning.",
                    "Lower values allow looser, more varied outputs.",
                    "Range: 0.1 to 20.0 (default: 1.5).",
                ],
                note="Start near 1.5 to 3.0 for balanced cloning.",
            ),
        )

        self.voxcpm_inference_timesteps_spinbox = QSpinBox()
        self.voxcpm_inference_timesteps_spinbox.setRange(1, 200)
        self.voxcpm_inference_timesteps_label = QLabel("Inference Steps:")
        voxcpm_layout.addWidget(self.voxcpm_inference_timesteps_label, 2, 0)
        voxcpm_layout.addWidget(self.voxcpm_inference_timesteps_spinbox, 2, 1)
        _apply_tooltip(
            [self.voxcpm_inference_timesteps_label, self.voxcpm_inference_timesteps_spinbox],
            _format_voxcpm_tooltip(
                "Inference Steps",
                [
                    "More steps can improve detail but take longer.",
                    "Fewer steps are faster but may reduce quality.",
                    "Range: 1 to 200 (default: 15).",
                ],
            ),
        )

        self.voxcpm_normalize_checkbox = QCheckBox("Normalize Audio")
        voxcpm_layout.addWidget(self.voxcpm_normalize_checkbox, 3, 0, 1, 2)
        self.voxcpm_normalize_checkbox.setToolTip(
            _format_voxcpm_tooltip(
                "Normalize Audio",
                [
                    "Applies loudness normalization to generated speech.",
                    "Useful when sentence volume varies too much.",
                ],
            )
        )

        self.voxcpm_denoise_checkbox = QCheckBox("Denoise Audio")
        voxcpm_layout.addWidget(self.voxcpm_denoise_checkbox, 4, 0, 1, 2)
        self.voxcpm_denoise_checkbox.setToolTip(
            _format_voxcpm_tooltip(
                "Denoise Audio",
                [
                    "Applies denoising during synthesis.",
                    "Can reduce background noise but may soften fine detail.",
                ],
            )
        )

        self.voxcpm_retry_badcase_checkbox = QCheckBox("Retry Bad Cases")
        voxcpm_layout.addWidget(self.voxcpm_retry_badcase_checkbox, 5, 0, 1, 2)
        self.voxcpm_retry_badcase_checkbox.setToolTip(
            _format_voxcpm_tooltip(
                "Retry Bad Cases",
                [
                    "Automatically retries outputs detected as low quality.",
                    "Helps with unstable lines and difficult prompts.",
                ],
            )
        )

        self.voxcpm_retry_badcase_max_times_spinbox = QSpinBox()
        self.voxcpm_retry_badcase_max_times_spinbox.setRange(1, 20)
        self.voxcpm_retry_badcase_max_times_label = QLabel("Retry Max Times:")
        voxcpm_layout.addWidget(self.voxcpm_retry_badcase_max_times_label, 6, 0)
        voxcpm_layout.addWidget(self.voxcpm_retry_badcase_max_times_spinbox, 6, 1)
        _apply_tooltip(
            [
                self.voxcpm_retry_badcase_max_times_label,
                self.voxcpm_retry_badcase_max_times_spinbox,
            ],
            _format_voxcpm_tooltip(
                "Retry Max Times",
                [
                    "Maximum retries when badcase retry is enabled.",
                    "Higher values may recover quality but increase latency.",
                    "Range: 1 to 20 (default: 3).",
                ],
            ),
        )

        self.voxcpm_retry_badcase_ratio_threshold_spinbox = QDoubleSpinBox()
        self.voxcpm_retry_badcase_ratio_threshold_spinbox.setDecimals(2)
        self.voxcpm_retry_badcase_ratio_threshold_spinbox.setRange(0.1, 50.0)
        self.voxcpm_retry_badcase_ratio_threshold_spinbox.setSingleStep(0.1)
        self.voxcpm_retry_badcase_ratio_threshold_label = QLabel("Retry Ratio Threshold:")
        voxcpm_layout.addWidget(self.voxcpm_retry_badcase_ratio_threshold_label, 7, 0)
        voxcpm_layout.addWidget(self.voxcpm_retry_badcase_ratio_threshold_spinbox, 7, 1)
        _apply_tooltip(
            [
                self.voxcpm_retry_badcase_ratio_threshold_label,
                self.voxcpm_retry_badcase_ratio_threshold_spinbox,
            ],
            _format_voxcpm_tooltip(
                "Retry Ratio Threshold",
                [
                    "Sensitivity used by badcase detection.",
                    "Lower values trigger retries more often.",
                    "Range: 0.1 to 50.0 (default: 6.0).",
                ],
            ),
        )

        self.voxcpm_min_len_spinbox = QSpinBox()
        self.voxcpm_min_len_spinbox.setRange(1, 12000)
        self.voxcpm_min_len_label = QLabel("Min Length:")
        voxcpm_layout.addWidget(self.voxcpm_min_len_label, 8, 0)
        voxcpm_layout.addWidget(self.voxcpm_min_len_spinbox, 8, 1)
        _apply_tooltip(
            [self.voxcpm_min_len_label, self.voxcpm_min_len_spinbox],
            _format_voxcpm_tooltip(
                "Min Length",
                [
                    "Minimum backend generation length hint.",
                    "Useful for avoiding too-short outputs in edge cases.",
                    "Range: 1 to 12000 (default: 2).",
                ],
            ),
        )

        self.voxcpm_max_len_spinbox = QSpinBox()
        self.voxcpm_max_len_spinbox.setRange(1, 12000)
        self.voxcpm_max_len_label = QLabel("Max Length:")
        voxcpm_layout.addWidget(self.voxcpm_max_len_label, 9, 0)
        voxcpm_layout.addWidget(self.voxcpm_max_len_spinbox, 9, 1)
        _apply_tooltip(
            [self.voxcpm_max_len_label, self.voxcpm_max_len_spinbox],
            _format_voxcpm_tooltip(
                "Max Length",
                [
                    "Maximum backend generation length hint.",
                    "Allows longer outputs but can increase compute cost.",
                    "Range: 1 to 12000 (default: 4096).",
                ],
                note="If Max Length is below Min Length, Min Length is used.",
            ),
        )

        layout.addWidget(self.voxcpm_advanced_settings_frame)

        self.fishs2_advanced_settings_frame = QFrame()
        self.fishs2_advanced_settings_frame.setObjectName("subGroupFrame")
        fishs2_layout = QGridLayout(self.fishs2_advanced_settings_frame)
        fishs2_layout.setHorizontalSpacing(10)
        fishs2_layout.setVerticalSpacing(8)

        def _format_fishs2_tooltip(title: str, lines: list[str], note: str = "") -> str:
            tooltip = f"<b>{title}</b><br><br>"
            tooltip += "<br>".join(f"- {line}" for line in lines)
            if note:
                tooltip += f"<br><br><i>{note}</i>"
            return tooltip

        self.fishs2_advanced_hint_label = QLabel(
            "FishS2 controls tune chunking, sampling, latency, and prosody."
        )
        self.fishs2_advanced_hint_label.setWordWrap(True)
        self.fishs2_advanced_hint_label.setObjectName("secondaryInfoLabel")
        fishs2_layout.addWidget(self.fishs2_advanced_hint_label, 0, 0, 1, 2)
        self.fishs2_advanced_hint_label.setToolTip(
            _format_fishs2_tooltip(
                "FishS2 Advanced Settings",
                [
                    "Temperature and top-p affect variation and diversity.",
                    "Chunk length and latency trade first response time against quality.",
                    "Prosody volume is sent in the Fish prosody object.",
                ],
            )
        )

        self.fishs2_temperature_spinbox = QDoubleSpinBox()
        self.fishs2_temperature_spinbox.setDecimals(2)
        self.fishs2_temperature_spinbox.setRange(0.0, 1.0)
        self.fishs2_temperature_spinbox.setSingleStep(0.05)
        self.fishs2_temperature_label = QLabel("Temperature:")
        fishs2_layout.addWidget(self.fishs2_temperature_label, 1, 0)
        fishs2_layout.addWidget(self.fishs2_temperature_spinbox, 1, 1)
        _apply_tooltip(
            [self.fishs2_temperature_label, self.fishs2_temperature_spinbox],
            _format_fishs2_tooltip(
                "Temperature",
                [
                    "Controls expressiveness/randomness.",
                    "Lower values are more consistent; higher values are more varied.",
                    "Range: 0.0 to 1.0 (default: 0.7).",
                ],
            ),
        )

        self.fishs2_top_p_spinbox = QDoubleSpinBox()
        self.fishs2_top_p_spinbox.setDecimals(2)
        self.fishs2_top_p_spinbox.setRange(0.0, 1.0)
        self.fishs2_top_p_spinbox.setSingleStep(0.05)
        self.fishs2_top_p_label = QLabel("Top P:")
        fishs2_layout.addWidget(self.fishs2_top_p_label, 2, 0)
        fishs2_layout.addWidget(self.fishs2_top_p_spinbox, 2, 1)
        _apply_tooltip(
            [self.fishs2_top_p_label, self.fishs2_top_p_spinbox],
            _format_fishs2_tooltip(
                "Top P",
                [
                    "Controls nucleus sampling diversity.",
                    "Lower values narrow choices; higher values allow more variation.",
                    "Range: 0.0 to 1.0 (default: 0.7).",
                ],
            ),
        )

        self.fishs2_chunk_length_spinbox = QSpinBox()
        self.fishs2_chunk_length_spinbox.setRange(100, 300)
        self.fishs2_chunk_length_label = QLabel("Chunk Length:")
        fishs2_layout.addWidget(self.fishs2_chunk_length_label, 3, 0)
        fishs2_layout.addWidget(self.fishs2_chunk_length_spinbox, 3, 1)
        _apply_tooltip(
            [self.fishs2_chunk_length_label, self.fishs2_chunk_length_spinbox],
            _format_fishs2_tooltip(
                "Chunk Length",
                [
                    "Characters per generation chunk.",
                    "Smaller chunks can respond sooner; larger chunks can improve continuity.",
                    "Range: 100 to 300 (default: 200).",
                ],
            ),
        )

        self.fishs2_latency_combo = QComboBox()
        self.fishs2_latency_combo.addItems(["balanced", "normal"])
        self.fishs2_latency_label = QLabel("Latency:")
        fishs2_layout.addWidget(self.fishs2_latency_label, 4, 0)
        fishs2_layout.addWidget(self.fishs2_latency_combo, 4, 1)
        _apply_tooltip(
            [self.fishs2_latency_label, self.fishs2_latency_combo],
            _format_fishs2_tooltip(
                "Latency",
                [
                    "balanced is faster and is the documented default.",
                    "normal favors quality over latency.",
                ],
            ),
        )

        self.fishs2_normalize_checkbox = QCheckBox("Normalize Text")
        fishs2_layout.addWidget(self.fishs2_normalize_checkbox, 5, 0, 1, 2)
        self.fishs2_normalize_checkbox.setToolTip(
            _format_fishs2_tooltip(
                "Normalize Text",
                [
                    "Enables Fish text normalization before synthesis.",
                    "Useful for numbers, punctuation, and symbols.",
                ],
            )
        )

        self.fishs2_prosody_volume_spinbox = QDoubleSpinBox()
        self.fishs2_prosody_volume_spinbox.setDecimals(1)
        self.fishs2_prosody_volume_spinbox.setRange(-20.0, 20.0)
        self.fishs2_prosody_volume_spinbox.setSingleStep(0.5)
        self.fishs2_prosody_volume_label = QLabel("Prosody Volume (dB):")
        fishs2_layout.addWidget(self.fishs2_prosody_volume_label, 6, 0)
        fishs2_layout.addWidget(self.fishs2_prosody_volume_spinbox, 6, 1)
        _apply_tooltip(
            [self.fishs2_prosody_volume_label, self.fishs2_prosody_volume_spinbox],
            _format_fishs2_tooltip(
                "Prosody Volume",
                [
                    "Output volume adjustment in decibels.",
                    "Positive values increase volume; negative values decrease it.",
                    "Range: -20.0 to 20.0 (default: 0.0).",
                ],
            ),
        )

        self.fishs2_normalize_loudness_checkbox = QCheckBox("Normalize Loudness")
        fishs2_layout.addWidget(self.fishs2_normalize_loudness_checkbox, 7, 0, 1, 2)
        self.fishs2_normalize_loudness_checkbox.setToolTip(
            _format_fishs2_tooltip(
                "Normalize Loudness",
                [
                    "Adds normalize_loudness to Fish prosody settings.",
                    "Helps keep sentence loudness more consistent.",
                ],
            )
        )

        layout.addWidget(self.fishs2_advanced_settings_frame)

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
        for language_code in XTTS_LANGUAGES:
            self.dub_to_lang_combo.addItem(
                LANGUAGE_DISPLAY_NAMES.get(language_code, language_code),
                language_code,
            )
        transl_layout.addWidget(self.dub_to_lang_combo, 1, 3)

        self.dub_glossary_check = QCheckBox("Enable glossary")
        transl_layout.addWidget(self.dub_glossary_check, 2, 0, 1, 4)

        transl_layout.addWidget(QLabel("Translation Provider:"), 3, 0)
        self.dub_trans_provider_combo = QComboBox()
        transl_layout.addWidget(self.dub_trans_provider_combo, 3, 1, 1, 3)

        transl_layout.addWidget(QLabel("Translation Model:"), 4, 0)
        self.dub_trans_model_combo = QComboBox()
        self.dub_trans_model_combo.setEditable(True)
        self.dub_trans_model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        model_line_edit = self.dub_trans_model_combo.lineEdit()
        if model_line_edit is not None:
            model_line_edit.setPlaceholderText("Type model ID if not listed")
        transl_layout.addWidget(self.dub_trans_model_combo, 4, 1, 1, 3)

        self.dub_trans_model_hint = QLabel(
            "Manage provider catalogs in the Providers tab."
        )
        self.dub_trans_model_hint.setWordWrap(True)
        self.dub_trans_model_hint.setObjectName("secondaryInfoLabel")
        transl_layout.addWidget(self.dub_trans_model_hint, 5, 0, 1, 4)

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
        self.generate_dub_audio_button.setObjectName("generateDubAudioButton")
        self.generate_dub_audio_button.setProperty("accentActive", False)
        self.add_dub_to_video_button = QPushButton("Add Dubbing to Video")
        self.only_transcribe_button = QPushButton("Only Transcribe")
        self.only_correct_button = QPushButton("Only Correct")
        self.only_translate_button = QPushButton("Only Translate")
        self.fine_tune_timings_button = QPushButton("Fine-Tune Timings (Subdub GUI)")

        buttons_layout.addWidget(self.generate_dub_audio_button, 0, 0, 1, 3)
        buttons_layout.addWidget(self.add_dub_to_video_button, 1, 0, 1, 3)
        buttons_layout.addWidget(self.only_transcribe_button, 2, 0)
        buttons_layout.addWidget(self.only_correct_button, 2, 1)
        buttons_layout.addWidget(self.only_translate_button, 2, 2)
        buttons_layout.addWidget(self.fine_tune_timings_button, 3, 0, 1, 3)


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


class TaskStatusPanel(QFrame):
    DUBBING_STAGE_DEFINITIONS = (
        ("transcribe", "Transcribe"),
        ("correct", "Correct"),
        ("translate", "Translate"),
        ("speech_blocks", "Speech Blocks"),
        ("tts_generation", "Generate Audio"),
        ("sync", "Sync"),
        ("equalize", "Equalize"),
        ("render", "Render"),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("taskStatusFrame")
        self._stage_badges: dict[str, QLabel] = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(8)

        self.title_label = QLabel("Ready")
        title_font = QFont()
        title_font.setPointSize(11)
        title_font.setBold(True)
        self.title_label.setFont(title_font)
        self.title_label.setObjectName("taskStatusTitleLabel")
        layout.addWidget(self.title_label)

        self.detail_label = QLabel("Add a source and start generation when you're ready.")
        self.detail_label.setWordWrap(True)
        self.detail_label.setObjectName("taskStatusDetailLabel")
        layout.addWidget(self.detail_label)

        self.stage_heading_label = QLabel("Dubbing Stages")
        self.stage_heading_label.setObjectName("taskStatusMetaLabel")
        layout.addWidget(self.stage_heading_label)

        self.stage_badges_frame = QFrame()
        self.stage_badges_frame.setObjectName("taskStageGridFrame")
        stage_layout = QGridLayout(self.stage_badges_frame)
        stage_layout.setContentsMargins(0, 0, 0, 0)
        stage_layout.setHorizontalSpacing(6)
        stage_layout.setVerticalSpacing(6)
        layout.addWidget(self.stage_badges_frame)

        for index, (stage_key, stage_label) in enumerate(self.DUBBING_STAGE_DEFINITIONS):
            badge = QLabel(stage_label)
            badge.setObjectName("taskStageBadge")
            badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
            badge.setProperty("stepState", "pending")
            stage_layout.addWidget(badge, index // 4, index % 4)
            self._stage_badges[stage_key] = badge

        self.set_activity("Ready", "Add a source and start generation when you're ready.", "idle")
        self.set_dubbing_stage_states({}, visible=False)

    def set_activity(self, headline: str, detail: str = "", tone: str = "idle"):
        self.title_label.setText(str(headline or "").strip() or "Ready")
        normalized_detail = str(detail or "").strip()
        self.detail_label.setText(normalized_detail)
        self.detail_label.setVisible(bool(normalized_detail))

        normalized_tone = str(tone or "idle").strip().lower()
        if normalized_tone not in {"idle", "active", "success", "warning", "error"}:
            normalized_tone = "idle"

        if self.property("activityTone") != normalized_tone:
            self.setProperty("activityTone", normalized_tone)
            style = self.style()
            if style is not None:
                style.unpolish(self)
                style.polish(self)
            self.update()

    def set_dubbing_stage_states(self, stage_states: dict[str, str], visible: bool):
        self.stage_heading_label.setVisible(bool(visible))
        self.stage_badges_frame.setVisible(bool(visible))

        normalized_states = stage_states if isinstance(stage_states, dict) else {}
        for stage_key, badge in self._stage_badges.items():
            state = str(normalized_states.get(stage_key) or "pending").strip().lower()
            if state not in {"pending", "running", "completed", "failed"}:
                state = "pending"

            if badge.property("stepState") != state:
                badge.setProperty("stepState", state)
                style = badge.style()
                if style is not None:
                    style.unpolish(badge)
                    style.polish(badge)
                badge.update()


class GenerationSection(QFrame):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("groupFrame")

        layout = QGridLayout(self)

        self.start_button = QPushButton("Start Generation")
        self.start_button.setObjectName("startGenerationButton")
        self.start_button.setProperty("accentActive", True)
        self.resume_button = QPushButton("Resume Generation")
        self.stop_button = QPushButton("Stop Generation")
        self.cancel_button = QPushButton("Cancel Generation")
        self.cancel_button.setObjectName("dangerButton")

        layout.addWidget(self.start_button, 0, 0)
        layout.addWidget(self.resume_button, 0, 1)
        layout.addWidget(self.stop_button, 0, 2)
        layout.addWidget(self.cancel_button, 0, 3)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setTextVisible(True)
        self.progress_bar.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress_bar.setFormat("0.00%")
        layout.addWidget(self.progress_bar, 1, 0, 1, 4)

        self.task_status_panel = TaskStatusPanel()
        layout.addWidget(self.task_status_panel, 2, 0, 1, 4)

        self.remaining_time_label = QLabel("N/A")
        self.remaining_time_label.setObjectName("secondaryInfoLabel")
        layout.addWidget(
            QLabel("Estimated Remaining Time:"),
            3,
            0,
            1,
            3,
            alignment=Qt.AlignmentFlag.AlignRight,
        )
        layout.addWidget(self.remaining_time_label, 3, 3)
