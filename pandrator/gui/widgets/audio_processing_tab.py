from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QLabel, QCheckBox, QSpinBox,
    QPushButton, QComboBox, QGridLayout, QFileDialog, QDoubleSpinBox,
    QMessageBox
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont

class AudioProcessingTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        
        main_layout = QVBoxLayout(self)
        main_layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        main_layout.addWidget(self._create_group_label("Appended Silence"))
        main_layout.addWidget(self._create_silence_frame())

        main_layout.addWidget(self._create_group_label("RVC"))
        main_layout.addWidget(self._create_rvc_frame())

        main_layout.addWidget(self._create_group_label("Fade"))
        main_layout.addWidget(self._create_fade_frame())

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

    def _create_silence_frame(self):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)
        
        self.sentence_silence_spinbox = QSpinBox()
        self.sentence_silence_spinbox.setRange(0, 10000)
        self.paragraph_silence_spinbox = QSpinBox()
        self.paragraph_silence_spinbox.setRange(0, 10000)

        layout.addWidget(QLabel("Sentence Silence (ms):"), 0, 0)
        layout.addWidget(self.sentence_silence_spinbox, 0, 1)
        layout.addWidget(QLabel("Paragraph Silence (ms):"), 1, 0)
        layout.addWidget(self.paragraph_silence_spinbox, 1, 1)
        return frame

    def _create_rvc_frame(self):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)
        
        self.enable_rvc_checkbox = QCheckBox("Enable RVC")
        self.rvc_model_combo = QComboBox()
        self.refresh_rvc_button = QPushButton("Refresh Models")
        self.upload_rvc_button = QPushButton("Upload New Model")

        layout.addWidget(self.enable_rvc_checkbox, 0, 0, 1, 2)
        layout.addWidget(QLabel("RVC Model:"), 1, 0)
        layout.addWidget(self.rvc_model_combo, 1, 1)
        layout.addWidget(self.refresh_rvc_button, 2, 0)
        layout.addWidget(self.upload_rvc_button, 2, 1)

        # Advanced RVC Settings
        adv_frame = QFrame()
        adv_frame.setObjectName("subGroupFrame")
        adv_layout = QGridLayout(adv_frame)
        layout.addWidget(adv_frame, 3, 0, 1, 2)

        adv_layout.addWidget(QLabel("Advanced RVC Settings"), 0, 0, 1, 2)

        self.rvc_pitch_spinbox = QSpinBox()
        self.rvc_pitch_spinbox.setRange(-24, 24)
        self.rvc_filter_radius_spinbox = QSpinBox()
        self.rvc_filter_radius_spinbox.setRange(0, 10)
        self.rvc_index_rate_spinbox = QDoubleSpinBox()
        self.rvc_index_rate_spinbox.setRange(0.0, 1.0)
        self.rvc_volume_spinbox = QDoubleSpinBox()
        self.rvc_volume_spinbox.setRange(0.0, 1.0)
        self.rvc_protect_spinbox = QDoubleSpinBox()
        self.rvc_protect_spinbox.setRange(0.0, 0.5)
        self.rvc_f0_method_combo = QComboBox()
        self.rvc_f0_method_combo.addItems(["rmvpe", "crepe", "harvest"])

        adv_layout.addWidget(QLabel("Pitch:"), 1, 0)
        adv_layout.addWidget(self.rvc_pitch_spinbox, 1, 1)
        adv_layout.addWidget(QLabel("Filter Radius:"), 2, 0)
        adv_layout.addWidget(self.rvc_filter_radius_spinbox, 2, 1)
        adv_layout.addWidget(QLabel("Index Rate:"), 3, 0)
        adv_layout.addWidget(self.rvc_index_rate_spinbox, 3, 1)
        adv_layout.addWidget(QLabel("Volume Envelope:"), 4, 0)
        adv_layout.addWidget(self.rvc_volume_spinbox, 4, 1)
        adv_layout.addWidget(QLabel("Protect:"), 5, 0)
        adv_layout.addWidget(self.rvc_protect_spinbox, 5, 1)
        adv_layout.addWidget(QLabel("F0 Method:"), 6, 0)
        adv_layout.addWidget(self.rvc_f0_method_combo, 6, 1)

        if not self.logic.is_rvc_available():
            frame.setEnabled(False)
            self.enable_rvc_checkbox.setText("Enable RVC (Not Available)")

        return frame

    def _create_fade_frame(self):
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = QGridLayout(frame)
        
        self.enable_fade_checkbox = QCheckBox("Enable Fade-in and Fade-out")
        self.fade_in_spinbox = QSpinBox()
        self.fade_in_spinbox.setRange(0, 1000)
        self.fade_out_spinbox = QSpinBox()
        self.fade_out_spinbox.setRange(0, 1000)

        layout.addWidget(self.enable_fade_checkbox, 0, 0, 1, 2)
        layout.addWidget(QLabel("Fade-in Duration (ms):"), 1, 0)
        layout.addWidget(self.fade_in_spinbox, 1, 1)
        layout.addWidget(QLabel("Fade-out Duration (ms):"), 2, 0)
        layout.addWidget(self.fade_out_spinbox, 2, 1)
        return frame

    def _connect_signals(self):
        # Silence
        self.sentence_silence_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.audio_processing, 'silence_between_sentences', v))
        self.paragraph_silence_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.audio_processing, 'silence_for_paragraphs', v))

        # RVC
        self.enable_rvc_checkbox.stateChanged.connect(lambda: setattr(self.logic.state.rvc, 'enable_rvc', self.enable_rvc_checkbox.isChecked()))
        self.rvc_model_combo.currentTextChanged.connect(lambda t: setattr(self.logic.state.rvc, 'rvc_model', t))
        self.refresh_rvc_button.clicked.connect(self._on_refresh_rvc_models)
        self.upload_rvc_button.clicked.connect(self._on_upload_rvc_model)
        self.rvc_pitch_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.rvc, 'pitch', v))
        self.rvc_filter_radius_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.rvc, 'filter_radius', v))
        self.rvc_index_rate_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.rvc, 'index_rate', v))
        self.rvc_volume_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.rvc, 'volume_envelope', v))
        self.rvc_protect_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.rvc, 'protect', v))
        self.rvc_f0_method_combo.currentTextChanged.connect(lambda t: setattr(self.logic.state.rvc, 'f0_method', t))

        # Fade
        self.enable_fade_checkbox.stateChanged.connect(lambda: setattr(self.logic.state.audio_processing, 'enable_fade', self.enable_fade_checkbox.isChecked()))
        self.fade_in_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.audio_processing, 'fade_in_duration', v))
        self.fade_out_spinbox.valueChanged.connect(lambda v: setattr(self.logic.state.audio_processing, 'fade_out_duration', v))

    def _on_refresh_rvc_models(self):
        models = self.logic.get_rvc_models()
        current_model = self.rvc_model_combo.currentText()
        self.rvc_model_combo.clear()
        if models:
            self.rvc_model_combo.addItems(models)
            if current_model in models:
                self.rvc_model_combo.setCurrentText(current_model)

    def _on_upload_rvc_model(self):
        pth_file, _ = QFileDialog.getOpenFileName(self, "Select RVC .pth file", "", "PTH Files (*.pth)")
        if not pth_file:
            return
        index_file, _ = QFileDialog.getOpenFileName(self, "Select RVC .index file", "", "Index Files (*.index)")
        if not index_file:
            return
        
        try:
            model_name = self.logic.upload_rvc_model(pth_file, index_file)
            QMessageBox.information(self, "Success", f"Model '{model_name}' uploaded successfully.")
            self._on_refresh_rvc_models()
        except Exception as e:
            QMessageBox.critical(self, "Error", f"Failed to upload model: {e}")

    def update_ui_from_state(self):
        # Silence
        ap_state = self.logic.state.audio_processing
        self.sentence_silence_spinbox.setValue(ap_state.silence_between_sentences)
        self.paragraph_silence_spinbox.setValue(ap_state.silence_for_paragraphs)
        is_dubbing_mode = self.logic.is_dubbing_mode_active()
        silence_controls_enabled = not is_dubbing_mode
        self.sentence_silence_spinbox.setEnabled(silence_controls_enabled)
        self.paragraph_silence_spinbox.setEnabled(silence_controls_enabled)

        silence_tooltip = (
            "Sentence and paragraph silence are forced to 0 while dubbing mode is active."
            if is_dubbing_mode
            else ""
        )
        self.sentence_silence_spinbox.setToolTip(silence_tooltip)
        self.paragraph_silence_spinbox.setToolTip(silence_tooltip)
        
        # RVC
        rvc_state = self.logic.state.rvc
        self.enable_rvc_checkbox.setChecked(rvc_state.enable_rvc)
        if self.rvc_model_combo.findText(rvc_state.rvc_model) == -1:
            self.rvc_model_combo.addItem(rvc_state.rvc_model)
        self.rvc_model_combo.setCurrentText(rvc_state.rvc_model)
        self.rvc_pitch_spinbox.setValue(rvc_state.pitch)
        self.rvc_filter_radius_spinbox.setValue(rvc_state.filter_radius)
        self.rvc_index_rate_spinbox.setValue(rvc_state.index_rate)
        self.rvc_volume_spinbox.setValue(rvc_state.volume_envelope)
        self.rvc_protect_spinbox.setValue(rvc_state.protect)
        self.rvc_f0_method_combo.setCurrentText(rvc_state.f0_method)

        # Fade
        self.enable_fade_checkbox.setChecked(ap_state.enable_fade)
        self.fade_in_spinbox.setValue(ap_state.fade_in_duration)
        self.fade_out_spinbox.setValue(ap_state.fade_out_duration)
