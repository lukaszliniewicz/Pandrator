from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QVBoxLayout,
)


class WorkflowPresetDialog(QDialog):
    def __init__(self, workflow_kind: str = "subtitles", preset: str = "transcribe", parent=None):
        super().__init__(parent)
        self.setWindowTitle("Choose Workflow")
        self.setMinimumWidth(520)
        layout = QVBoxLayout(self)
        intro = QLabel(
            "Choose the outcome you want. You can still include, exclude, or run individual stages in the workspace."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        form = QFormLayout()
        layout.addLayout(form)
        self.kind_combo = QComboBox()
        self.kind_combo.addItem("Create subtitles", "subtitles")
        self.kind_combo.addItem("Create a voiceover", "voiceover")
        self.kind_combo.addItem("Create an audiobook", "audiobook")
        index = self.kind_combo.findData(workflow_kind)
        self.kind_combo.setCurrentIndex(index if index >= 0 else 0)
        form.addRow("Task:", self.kind_combo)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Transcribe", "transcribe")
        self.preset_combo.addItem("Clean subtitles", "clean_subtitles")
        self.preset_combo.addItem("Translate subtitles", "translate_subtitles")
        self.preset_combo.addItem("Create voiceover", "voiceover")
        self.preset_combo.addItem("Custom", "custom")
        preset_index = self.preset_combo.findData(preset)
        self.preset_combo.setCurrentIndex(preset_index if preset_index >= 0 else 0)
        form.addRow("Outcome:", self.preset_combo)
        self.translate_voiceover_check = QCheckBox("Translate before generating the voiceover")
        layout.addWidget(self.translate_voiceover_check)
        self.preset_combo.currentIndexChanged.connect(self._update_state)
        self.kind_combo.currentIndexChanged.connect(self._update_state)
        self._update_state()
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_state(self):
        is_audiobook = self.kind_combo.currentData() == "audiobook"
        self.preset_combo.setEnabled(not is_audiobook)
        self.translate_voiceover_check.setVisible(
            not is_audiobook and self.preset_combo.currentData() == "voiceover"
        )

    def selection(self) -> tuple[str, str, bool]:
        kind = str(self.kind_combo.currentData() or "subtitles")
        preset = "custom" if kind == "audiobook" else str(self.preset_combo.currentData() or "custom")
        return kind, preset, bool(self.translate_voiceover_check.isChecked())
