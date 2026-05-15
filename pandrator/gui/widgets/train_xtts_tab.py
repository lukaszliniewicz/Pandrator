from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)


class TrainXttsTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        main_layout.addWidget(self._create_group_label("Source Audio"))
        main_layout.addWidget(self._create_source_audio_frame())

        main_layout.addWidget(self._create_group_label("Model Configuration"))
        main_layout.addWidget(self._create_model_config_frame())

        main_layout.addWidget(self._create_group_label("Training Configuration"))
        main_layout.addWidget(self._create_training_config_frame())

        main_layout.addWidget(self._create_group_label("Voice Sample Options"))
        main_layout.addWidget(self._create_voice_sample_frame())

        main_layout.addWidget(self._create_group_label("Training Parameters"))
        main_layout.addWidget(self._create_training_params_frame())

        main_layout.addWidget(self._create_group_label("Audio Preprocessing"))
        main_layout.addWidget(self._create_audio_preprocessing_frame())

        main_layout.addWidget(self._create_group_label("Training Control"))
        main_layout.addWidget(self._create_control_frame())

        self._connect_logic_signals()
        self._on_voice_sample_mode_changed(self.voice_sample_mode_combo.currentText())
        self._update_preprocessing_option_states()
        self._on_training_running_changed(self.logic.is_xtts_training_running())

    def _create_group_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        return label

    def _create_source_audio_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.source_audio_path_edit = QLineEdit()
        self.browse_source_audio_button = QPushButton("Browse")
        self.browse_source_audio_button.clicked.connect(self._browse_source_audio)

        layout.addWidget(QLabel("Path to source audio:"), 0, 0)
        layout.addWidget(self.source_audio_path_edit, 0, 1)
        layout.addWidget(self.browse_source_audio_button, 0, 2)
        return frame

    def _create_model_config_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.model_name_edit = QLineEdit()

        self.model_language_combo = QComboBox()
        self.model_language_combo.addItems([
            "en", "es", "fr", "de", "it", "pt", "pl", "tr", "ru", "nl", "cs", "ar", "zh-cn", "ja", "hu", "ko"
        ])

        self.whisper_model_combo = QComboBox()
        self.whisper_model_combo.addItems(["medium", "medium.en", "large-v2", "large-v3"])
        self.whisper_model_combo.setCurrentText("large-v3")

        self.sample_rate_combo = QComboBox()
        self.sample_rate_combo.addItems(["22050", "44100"])

        layout.addWidget(QLabel("Model Name:"), 0, 0)
        layout.addWidget(self.model_name_edit, 0, 1)
        layout.addWidget(QLabel("Model Language:"), 1, 0)
        layout.addWidget(self.model_language_combo, 1, 1)
        layout.addWidget(QLabel("Whisper Model:"), 2, 0)
        layout.addWidget(self.whisper_model_combo, 2, 1)
        layout.addWidget(QLabel("Sample Rate:"), 3, 0)
        layout.addWidget(self.sample_rate_combo, 3, 1)
        return frame

    def _create_training_config_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.max_duration_spinbox = QDoubleSpinBox()
        self.max_duration_spinbox.setRange(5.0, 30.0)
        self.max_duration_spinbox.setValue(11.0)
        self.max_duration_spinbox.setSingleStep(0.5)

        self.max_text_length_spinbox = QSpinBox()
        self.max_text_length_spinbox.setRange(100, 500)
        self.max_text_length_spinbox.setValue(200)

        self.training_split_combo = QComboBox()
        self.training_split_combo.addItems(["6_4", "7_3", "8_2", "9_1"])
        self.training_split_combo.setCurrentText("9_1")

        self.method_proportion_combo = QComboBox()
        self.method_proportion_combo.addItems(["4_5", "5_5", "6_4", "7_3"])
        self.method_proportion_combo.setCurrentText("6_4")

        self.sample_method_combo = QComboBox()
        self.sample_method_combo.addItems(["Mixed", "Maximise Punctuation", "Punctuation"])
        self.sample_method_combo.setCurrentText("Mixed")

        self.alignment_model_edit = QLineEdit()

        layout.addWidget(QLabel("Max training segment duration (s):"), 0, 0)
        layout.addWidget(self.max_duration_spinbox, 0, 1)
        layout.addWidget(QLabel("Max training segment text length:"), 1, 0)
        layout.addWidget(self.max_text_length_spinbox, 1, 1)
        layout.addWidget(QLabel("Training/Evaluation split ratio:"), 2, 0)
        layout.addWidget(self.training_split_combo, 2, 1)
        layout.addWidget(QLabel("Maximise/Punctuation methods ratio:"), 3, 0)
        layout.addWidget(self.method_proportion_combo, 3, 1)
        layout.addWidget(QLabel("Sample generation method:"), 4, 0)
        layout.addWidget(self.sample_method_combo, 4, 1)
        layout.addWidget(QLabel("Custom Alignment Model (Optional):"), 5, 0)
        layout.addWidget(self.alignment_model_edit, 5, 1)

        return frame

    def _create_voice_sample_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.voice_sample_mode_combo = QComboBox()
        self.voice_sample_mode_combo.addItems(["basic", "extended", "dynamic"])
        self.voice_sample_mode_combo.currentTextChanged.connect(self._on_voice_sample_mode_changed)

        self.voice_samples_count_label = QLabel("Number of Voice Samples:")
        self.voice_samples_count_combo = QComboBox()
        self.voice_samples_count_combo.addItems(["3", "4"])
        self.voice_samples_count_combo.setCurrentText("3")

        self.voice_sample_only_sentence_checkbox = QCheckBox(
            "Only use complete sentences for voice samples"
        )

        layout.addWidget(QLabel("Voice Sample Mode:"), 0, 0)
        layout.addWidget(self.voice_sample_mode_combo, 0, 1)
        layout.addWidget(self.voice_samples_count_label, 1, 0)
        layout.addWidget(self.voice_samples_count_combo, 1, 1)
        layout.addWidget(self.voice_sample_only_sentence_checkbox, 2, 0, 1, 2)

        return frame

    def _create_training_params_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.epochs_spinbox = QSpinBox()
        self.epochs_spinbox.setRange(1, 100)
        self.epochs_spinbox.setValue(6)

        self.batches_spinbox = QSpinBox()
        self.batches_spinbox.setRange(1, 16)
        self.batches_spinbox.setValue(2)

        self.gradient_spinbox = QSpinBox()
        self.gradient_spinbox.setRange(1, 100)
        self.gradient_spinbox.setValue(1)

        layout.addWidget(QLabel("Epochs:"), 0, 0)
        layout.addWidget(self.epochs_spinbox, 0, 1)
        layout.addWidget(QLabel("Batches:"), 1, 0)
        layout.addWidget(self.batches_spinbox, 1, 1)
        layout.addWidget(QLabel("Gradient Accumulation:"), 2, 0)
        layout.addWidget(self.gradient_spinbox, 2, 1)

        return frame

    def _create_audio_preprocessing_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)

        self.denoise_checkbox = QCheckBox("Denoise")
        self.breath_checkbox = QCheckBox("Remove breath sounds")
        self.normalize_checkbox = QCheckBox("Normalize")
        self.normalize_checkbox.toggled.connect(self._update_preprocessing_option_states)

        self.compress_checkbox = QCheckBox("Compress")
        self.compress_checkbox.toggled.connect(self._update_preprocessing_option_states)

        self.dess_checkbox = QCheckBox("De-ess")

        self.lufs_spinbox = QSpinBox()
        self.lufs_spinbox.setRange(-30, -8)
        self.lufs_spinbox.setValue(-16)

        self.compress_profile_combo = QComboBox()
        self.compress_profile_combo.addItems(["male", "female", "neutral"])
        self.compress_profile_combo.setCurrentText("neutral")

        layout.addWidget(self.denoise_checkbox, 0, 0)
        layout.addWidget(self.breath_checkbox, 0, 1)
        layout.addWidget(self.dess_checkbox, 1, 0)
        layout.addWidget(self.normalize_checkbox, 2, 0)

        normalize_options = QHBoxLayout()
        normalize_options.addWidget(QLabel("Target LUFS:"))
        normalize_options.addWidget(self.lufs_spinbox)
        normalize_options.addStretch(1)
        layout.addLayout(normalize_options, 2, 1)

        layout.addWidget(self.compress_checkbox, 3, 0)
        compress_options = QHBoxLayout()
        compress_options.addWidget(QLabel("Profile:"))
        compress_options.addWidget(self.compress_profile_combo)
        compress_options.addStretch(1)
        layout.addLayout(compress_options, 3, 1)

        return frame

    def _create_control_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QVBoxLayout(frame)

        self.start_training_button = QPushButton("Start Training")
        self.start_training_button.clicked.connect(self._on_start_training)

        self.training_progress_bar = QProgressBar()
        self.training_progress_bar.setRange(0, 100)
        self.training_progress_bar.setValue(0)

        self.training_status_label = QLabel("Ready to train")

        layout.addWidget(self.start_training_button)
        layout.addWidget(self.training_progress_bar)
        layout.addWidget(self.training_status_label)
        return frame

    def _connect_logic_signals(self):
        self.logic.xtts_training_running_changed.connect(self._on_training_running_changed)
        self.logic.xtts_training_status_updated.connect(self._on_training_status_updated)
        self.logic.xtts_training_progress_updated.connect(self._on_training_progress_updated)

    def _all_setting_widgets(self) -> list[QWidget]:
        return [
            self.source_audio_path_edit,
            self.browse_source_audio_button,
            self.model_name_edit,
            self.model_language_combo,
            self.whisper_model_combo,
            self.sample_rate_combo,
            self.max_duration_spinbox,
            self.max_text_length_spinbox,
            self.training_split_combo,
            self.method_proportion_combo,
            self.sample_method_combo,
            self.alignment_model_edit,
            self.voice_sample_mode_combo,
            self.voice_samples_count_combo,
            self.voice_sample_only_sentence_checkbox,
            self.epochs_spinbox,
            self.batches_spinbox,
            self.gradient_spinbox,
            self.denoise_checkbox,
            self.breath_checkbox,
            self.normalize_checkbox,
            self.compress_checkbox,
            self.dess_checkbox,
            self.lufs_spinbox,
            self.compress_profile_combo,
        ]

    def _browse_source_audio(self):
        chooser = QMessageBox(self)
        chooser.setWindowTitle("Select Source Audio")
        chooser.setText("Choose input type for XTTS training source:")
        folder_button = chooser.addButton("Folder", QMessageBox.ButtonRole.YesRole)
        file_button = chooser.addButton("File", QMessageBox.ButtonRole.NoRole)
        chooser.addButton(QMessageBox.StandardButton.Cancel)
        chooser.exec()

        clicked = chooser.clickedButton()
        if clicked == folder_button:
            path = QFileDialog.getExistingDirectory(self, "Select Audio Folder")
        elif clicked == file_button:
            path, _ = QFileDialog.getOpenFileName(
                self,
                "Select Audio File",
                "",
                "Audio Files (*.wav *.mp3 *.ogg *.flac *.m4a *.aac *.wma *.aiff);;All files (*.*)",
            )
        else:
            return

        if path:
            self.source_audio_path_edit.setText(path)

    def _on_voice_sample_mode_changed(self, mode: str):
        show_sample_count = mode != "basic"
        self.voice_samples_count_label.setVisible(show_sample_count)
        self.voice_samples_count_combo.setVisible(show_sample_count)

    def _update_preprocessing_option_states(self):
        self.lufs_spinbox.setEnabled(self.normalize_checkbox.isChecked())
        self.compress_profile_combo.setEnabled(self.compress_checkbox.isChecked())

    def _on_start_training(self):
        source_path = self.source_audio_path_edit.text().strip()
        model_name = self.model_name_edit.text().strip()

        if not source_path or not model_name:
            QMessageBox.warning(
                self,
                "Missing Information",
                "Source audio path and model name are required.",
            )
            return

        settings = {
            "source_audio_path": source_path,
            "model_name": model_name,
            "model_language": self.model_language_combo.currentText(),
            "whisper_model": self.whisper_model_combo.currentText(),
            "sample_rate": int(self.sample_rate_combo.currentText()),
            "max_duration": self.max_duration_spinbox.value(),
            "max_text_length": self.max_text_length_spinbox.value(),
            "training_split": self.training_split_combo.currentText(),
            "method_proportion": self.method_proportion_combo.currentText(),
            "sample_method": self.sample_method_combo.currentText(),
            "alignment_model": self.alignment_model_edit.text().strip(),
            "voice_sample_mode": self.voice_sample_mode_combo.currentText(),
            "voice_samples_count": int(self.voice_samples_count_combo.currentText()),
            "voice_sample_only_sentence": self.voice_sample_only_sentence_checkbox.isChecked(),
            "epochs": self.epochs_spinbox.value(),
            "batches": self.batches_spinbox.value(),
            "gradient": self.gradient_spinbox.value(),
            "enable_denoise": self.denoise_checkbox.isChecked(),
            "enable_breath_removal": self.breath_checkbox.isChecked(),
            "enable_normalize": self.normalize_checkbox.isChecked(),
            "enable_compress": self.compress_checkbox.isChecked(),
            "enable_dess": self.dess_checkbox.isChecked(),
            "lufs_value": str(self.lufs_spinbox.value()),
            "compress_profile": self.compress_profile_combo.currentText(),
        }

        self.logic.start_xtts_training(settings)

    def _on_training_running_changed(self, running: bool):
        for widget in self._all_setting_widgets():
            widget.setEnabled(not running)
        self.start_training_button.setEnabled(not running)

        if running:
            self.training_progress_bar.setRange(0, 0)
        else:
            if self.training_progress_bar.minimum() == 0 and self.training_progress_bar.maximum() == 0:
                self.training_progress_bar.setRange(0, 100)

    def _on_training_status_updated(self, message: str):
        self.training_status_label.setText(message or "")

    def _on_training_progress_updated(self, progress: int):
        if self.training_progress_bar.minimum() == 0 and self.training_progress_bar.maximum() == 0:
            self.training_progress_bar.setRange(0, 100)
        self.training_progress_bar.setValue(max(0, min(100, int(progress))))
