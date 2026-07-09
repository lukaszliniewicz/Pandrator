import os

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QDoubleValidator, QFont, QIntValidator
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)
from .responsive_page import ScrollableSettingsPage, configure_form_grid


class TrainXttsTab(ScrollableSettingsPage):
    def __init__(self, logic, parent=None):
        super().__init__(parent, page_object_name="trainXttsPage")
        self.logic = logic

        main_layout = self.content_layout

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
        self._update_ctc_option_states()
        self._update_preprocessing_option_states()
        self._on_training_running_changed(self.logic.is_xtts_training_running())

    def _create_group_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        return label

    def _create_editable_integer_combo(
        self,
        options: list[int],
        default_value: int,
        min_value: int,
        max_value: int,
    ) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.addItems([str(option) for option in options])
        combo.setCurrentText(str(default_value))

        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setValidator(QIntValidator(min_value, max_value, combo))
            line_edit.setAlignment(Qt.AlignmentFlag.AlignRight)

        return combo

    def _create_editable_float_combo(
        self,
        options: list[float],
        default_value: float,
        min_value: float,
        max_value: float,
        decimals: int,
    ) -> QComboBox:
        combo = QComboBox()
        combo.setEditable(True)
        combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        combo.addItems([f"{option:.{decimals}f}" for option in options])
        combo.setCurrentText(f"{default_value:.{decimals}f}")

        validator = QDoubleValidator(min_value, max_value, decimals, combo)
        validator.setNotation(QDoubleValidator.Notation.StandardNotation)

        line_edit = combo.lineEdit()
        if line_edit is not None:
            line_edit.setValidator(validator)
            line_edit.setAlignment(Qt.AlignmentFlag.AlignRight)

        return combo

    def _create_source_audio_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = configure_form_grid(
            QGridLayout(frame), label_width=240, trailing_column=3
        )

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
        layout = configure_form_grid(QGridLayout(frame), label_width=240)

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
        layout = configure_form_grid(
            QGridLayout(frame), label_width=280, trailing_column=3
        )

        self.max_duration_combo = self._create_editable_float_combo(
            options=[8.0, 10.0, 11.0, 12.0, 14.0, 16.0],
            default_value=11.0,
            min_value=5.0,
            max_value=30.0,
            decimals=1,
        )

        self.max_text_length_combo = self._create_editable_integer_combo(
            options=[120, 160, 200, 240, 280, 320],
            default_value=200,
            min_value=100,
            max_value=500,
        )

        self.training_split_combo = QComboBox()
        self.training_split_combo.addItems(["6_4", "7_3", "8_2", "9_1"])
        self.training_split_combo.setCurrentText("8_2")

        self.method_proportion_combo = QComboBox()
        self.method_proportion_combo.addItems(["4_6", "5_5", "6_4", "7_3", "8_2"])
        self.method_proportion_combo.setCurrentText("6_4")

        self.sample_method_combo = QComboBox()
        self.sample_method_combo.addItem("Mixed", "mixed")
        self.sample_method_combo.addItem("Maximise Punctuation", "maximise-punctuation")
        self.sample_method_combo.addItem("Punctuation Only", "punctuation-only")
        self.sample_method_combo.setCurrentText("Mixed")

        self.alignment_model_edit = QLineEdit()

        self.text_source_path_edit = QLineEdit()
        self.text_source_path_edit.setPlaceholderText(
            "Optional .txt or .epub source for CTC alignment"
        )
        self.text_source_path_edit.textChanged.connect(self._update_ctc_option_states)

        self.browse_text_source_button = QPushButton("Browse")
        self.browse_text_source_button.clicked.connect(self._browse_text_source)

        self.chapter_per_audio_label = QLabel("EPUB chapters per audio file:")
        self.chapter_per_audio_combo = self._create_editable_integer_combo(
            options=[1, 2, 3, 4, 5],
            default_value=1,
            min_value=1,
            max_value=50,
        )
        self.chapter_per_audio_combo.setToolTip(
            "Used only when the selected CTC source text is an EPUB file."
        )

        layout.addWidget(QLabel("Max training segment duration (s):"), 0, 0)
        layout.addWidget(self.max_duration_combo, 0, 1, 1, 2)
        layout.addWidget(QLabel("Max training segment text length:"), 1, 0)
        layout.addWidget(self.max_text_length_combo, 1, 1, 1, 2)
        layout.addWidget(QLabel("Training/Evaluation split ratio:"), 2, 0)
        layout.addWidget(self.training_split_combo, 2, 1, 1, 2)
        layout.addWidget(QLabel("Maximise/Punctuation methods ratio:"), 3, 0)
        layout.addWidget(self.method_proportion_combo, 3, 1, 1, 2)
        layout.addWidget(QLabel("Sample generation method:"), 4, 0)
        layout.addWidget(self.sample_method_combo, 4, 1, 1, 2)
        layout.addWidget(QLabel("Custom Alignment Model (Optional):"), 5, 0)
        layout.addWidget(self.alignment_model_edit, 5, 1, 1, 2)
        layout.addWidget(QLabel("CTC Text Source (.txt/.epub, Optional):"), 6, 0)
        layout.addWidget(self.text_source_path_edit, 6, 1)
        layout.addWidget(self.browse_text_source_button, 6, 2)
        layout.addWidget(self.chapter_per_audio_label, 7, 0)
        layout.addWidget(self.chapter_per_audio_combo, 7, 1, 1, 2)

        return frame

    def _create_voice_sample_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = configure_form_grid(QGridLayout(frame), label_width=240)

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
        layout = configure_form_grid(QGridLayout(frame), label_width=240)

        self.epochs_combo = self._create_editable_integer_combo(
            options=[4, 6, 8, 10, 12, 16],
            default_value=6,
            min_value=1,
            max_value=100,
        )

        self.batches_combo = self._create_editable_integer_combo(
            options=[1, 2, 3, 4, 6, 8, 12, 16],
            default_value=2,
            min_value=1,
            max_value=16,
        )

        self.gradient_combo = self._create_editable_integer_combo(
            options=[1, 2, 4, 8, 16],
            default_value=1,
            min_value=1,
            max_value=100,
        )

        layout.addWidget(QLabel("Epochs:"), 0, 0)
        layout.addWidget(self.epochs_combo, 0, 1)
        layout.addWidget(QLabel("Batches:"), 1, 0)
        layout.addWidget(self.batches_combo, 1, 1)
        layout.addWidget(QLabel("Gradient Accumulation:"), 2, 0)
        layout.addWidget(self.gradient_combo, 2, 1)

        return frame

    def _create_audio_preprocessing_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = configure_form_grid(QGridLayout(frame), label_width=200)

        self.denoise_checkbox = QCheckBox("Denoise")
        self.breath_checkbox = QCheckBox("Remove breath sounds")
        self.normalize_checkbox = QCheckBox("Normalize")
        self.normalize_checkbox.toggled.connect(self._update_preprocessing_option_states)

        self.compress_checkbox = QCheckBox("Compress")
        self.compress_checkbox.toggled.connect(self._update_preprocessing_option_states)

        self.dess_checkbox = QCheckBox("De-ess")

        self.lufs_combo = self._create_editable_integer_combo(
            options=[-22, -20, -18, -16, -14, -12],
            default_value=-16,
            min_value=-30,
            max_value=-8,
        )

        self.compress_profile_combo = QComboBox()
        self.compress_profile_combo.addItems(["male", "female", "neutral"])
        self.compress_profile_combo.setCurrentText("neutral")

        layout.addWidget(self.denoise_checkbox, 0, 0)
        layout.addWidget(self.breath_checkbox, 0, 1)
        layout.addWidget(self.dess_checkbox, 1, 0)
        layout.addWidget(self.normalize_checkbox, 2, 0)

        normalize_options = QHBoxLayout()
        normalize_options.addWidget(QLabel("Target LUFS:"))
        normalize_options.addWidget(self.lufs_combo)
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
            self.max_duration_combo,
            self.max_text_length_combo,
            self.training_split_combo,
            self.method_proportion_combo,
            self.sample_method_combo,
            self.alignment_model_edit,
            self.text_source_path_edit,
            self.browse_text_source_button,
            self.chapter_per_audio_combo,
            self.voice_sample_mode_combo,
            self.voice_samples_count_combo,
            self.voice_sample_only_sentence_checkbox,
            self.epochs_combo,
            self.batches_combo,
            self.gradient_combo,
            self.denoise_checkbox,
            self.breath_checkbox,
            self.normalize_checkbox,
            self.compress_checkbox,
            self.dess_checkbox,
            self.lufs_combo,
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

    def _browse_text_source(self):
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Select CTC Text Source",
            "",
            "Text Sources (*.txt *.epub);;Text Files (*.txt);;EPUB Files (*.epub);;All files (*.*)",
        )

        if path:
            self.text_source_path_edit.setText(path)

    def _on_voice_sample_mode_changed(self, mode: str):
        show_sample_count = mode != "basic"
        self.voice_samples_count_label.setVisible(show_sample_count)
        self.voice_samples_count_combo.setVisible(show_sample_count)

    def _update_ctc_option_states(self):
        source_text_path = self.text_source_path_edit.text().strip().lower()
        is_epub_source = source_text_path.endswith(".epub")

        self.chapter_per_audio_label.setEnabled(is_epub_source)
        self.chapter_per_audio_combo.setEnabled(is_epub_source)

    def _update_preprocessing_option_states(self):
        self.lufs_combo.setEnabled(self.normalize_checkbox.isChecked())
        self.compress_profile_combo.setEnabled(self.compress_checkbox.isChecked())

    def _read_integer_setting(
        self,
        widget: QComboBox,
        label: str,
        minimum: int,
        maximum: int,
    ) -> int | None:
        raw_value = widget.currentText().strip()
        try:
            value = int(raw_value)
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid Value",
                f"{label} must be a whole number between {minimum} and {maximum}.",
            )
            return None

        if value < minimum or value > maximum:
            QMessageBox.warning(
                self,
                "Invalid Value",
                f"{label} must be between {minimum} and {maximum}.",
            )
            return None

        return value

    def _read_float_setting(
        self,
        widget: QComboBox,
        label: str,
        minimum: float,
        maximum: float,
    ) -> float | None:
        raw_value = widget.currentText().strip().replace(",", ".")
        try:
            value = float(raw_value)
        except ValueError:
            QMessageBox.warning(
                self,
                "Invalid Value",
                f"{label} must be a number between {minimum:.1f} and {maximum:.1f}.",
            )
            return None

        if value < minimum or value > maximum:
            QMessageBox.warning(
                self,
                "Invalid Value",
                f"{label} must be between {minimum:.1f} and {maximum:.1f}.",
            )
            return None

        return value

    def _on_start_training(self):
        source_path = self.source_audio_path_edit.text().strip()
        model_name = self.model_name_edit.text().strip()
        source_text_path = self.text_source_path_edit.text().strip()

        if not source_path or not model_name:
            QMessageBox.warning(
                self,
                "Missing Information",
                "Source audio path and model name are required.",
            )
            return

        if source_text_path:
            if not os.path.isfile(source_text_path):
                QMessageBox.warning(
                    self,
                    "Missing Information",
                    "The selected CTC text source file does not exist.",
                )
                return

            if not source_text_path.lower().endswith((".txt", ".epub")):
                QMessageBox.warning(
                    self,
                    "Invalid File",
                    "CTC text source must be a .txt or .epub file.",
                )
                return

        max_duration = self._read_float_setting(
            self.max_duration_combo,
            "Max training segment duration",
            5.0,
            30.0,
        )
        if max_duration is None:
            return

        max_text_length = self._read_integer_setting(
            self.max_text_length_combo,
            "Max training segment text length",
            100,
            500,
        )
        if max_text_length is None:
            return

        epochs = self._read_integer_setting(self.epochs_combo, "Epochs", 1, 100)
        if epochs is None:
            return

        batches = self._read_integer_setting(self.batches_combo, "Batches", 1, 16)
        if batches is None:
            return

        gradient = self._read_integer_setting(self.gradient_combo, "Gradient accumulation", 1, 100)
        if gradient is None:
            return

        lufs_value = -16
        if self.normalize_checkbox.isChecked():
            parsed_lufs = self._read_integer_setting(self.lufs_combo, "Target LUFS", -30, -8)
            if parsed_lufs is None:
                return
            lufs_value = parsed_lufs

        chapter_per_audio = 1
        if source_text_path.lower().endswith(".epub"):
            parsed_chapter_count = self._read_integer_setting(
                self.chapter_per_audio_combo,
                "EPUB chapters per audio file",
                1,
                50,
            )
            if parsed_chapter_count is None:
                return
            chapter_per_audio = parsed_chapter_count

        sample_method = self.sample_method_combo.currentData()
        if not isinstance(sample_method, str) or not sample_method:
            sample_method = self.sample_method_combo.currentText().strip().lower().replace(" ", "-")

        settings = {
            "source_audio_path": source_path,
            "model_name": model_name,
            "model_language": self.model_language_combo.currentText(),
            "whisper_model": self.whisper_model_combo.currentText(),
            "sample_rate": int(self.sample_rate_combo.currentText()),
            "max_duration": max_duration,
            "max_text_length": max_text_length,
            "training_split": self.training_split_combo.currentText(),
            "method_proportion": self.method_proportion_combo.currentText(),
            "sample_method": sample_method,
            "alignment_model": self.alignment_model_edit.text().strip(),
            "source_text_path": source_text_path,
            "chapter_per_audio": chapter_per_audio,
            "voice_sample_mode": self.voice_sample_mode_combo.currentText(),
            "voice_samples_count": int(self.voice_samples_count_combo.currentText()),
            "voice_sample_only_sentence": self.voice_sample_only_sentence_checkbox.isChecked(),
            "epochs": epochs,
            "batches": batches,
            "gradient": gradient,
            "enable_denoise": self.denoise_checkbox.isChecked(),
            "enable_breath_removal": self.breath_checkbox.isChecked(),
            "enable_normalize": self.normalize_checkbox.isChecked(),
            "enable_compress": self.compress_checkbox.isChecked(),
            "enable_dess": self.dess_checkbox.isChecked(),
            "lufs_value": str(lufs_value),
            "compress_profile": self.compress_profile_combo.currentText(),
        }

        self.logic.start_xtts_training(settings)

    def _on_training_running_changed(self, running: bool):
        for widget in self._all_setting_widgets():
            widget.setEnabled(not running)
        self.start_training_button.setEnabled(not running)

        if not running:
            self._update_ctc_option_states()
            self._update_preprocessing_option_states()

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
