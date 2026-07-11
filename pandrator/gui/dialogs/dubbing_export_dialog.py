from PyQt6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QVBoxLayout


class DubbingExportDialog(QDialog):
    def __init__(self, *, has_translation: bool, has_generated_audio: bool, has_video: bool, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Export Dubbing Workflow")
        self.setMinimumWidth(560)
        layout = QVBoxLayout(self)
        summary = QLabel(
            "Export can preserve the source audio and does not require generated dubbing audio. "
            "Soft subtitles remain selectable; burned subtitles are always visible."
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)
        form = QFormLayout()
        layout.addLayout(form)
        self.audio_combo = QComboBox()
        self.audio_combo.addItem("Preserve source audio", "source")
        if has_generated_audio:
            self.audio_combo.addItem("Mix source and dubbing", "mixed")
            self.audio_combo.addItem("Dubbing audio only", "dubbed")
        form.addRow("Audio:", self.audio_combo)
        self.subtitle_mode_combo = QComboBox()
        self.subtitle_mode_combo.addItem("No subtitles", "none")
        self.subtitle_mode_combo.addItem("Inject soft subtitles", "soft")
        self.subtitle_mode_combo.addItem("Burn subtitles", "burned")
        if not has_video:
            self.subtitle_mode_combo.setCurrentIndex(0)
            self.subtitle_mode_combo.setEnabled(False)
        form.addRow("Video subtitles:", self.subtitle_mode_combo)
        self.track_combo = QComboBox()
        if has_translation:
            self.track_combo.addItem("Translation only", "translation")
            self.track_combo.addItem("Source/corrected + translation", "both")
        else:
            self.track_combo.addItem("Source/corrected", "source")
        form.addRow("Subtitle tracks:", self.track_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def options(self) -> dict:
        return {
            "audio_mode": str(self.audio_combo.currentData() or "source"),
            "subtitle_mode": str(self.subtitle_mode_combo.currentData() or "none"),
            "track_mode": str(self.track_combo.currentData() or "source"),
        }
