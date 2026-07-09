from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
)

from .metadata_dialog import MetadataDialog


class OutputOptionsDialog(QDialog):
    """Modal editor for final audio export settings."""

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic

        self.setWindowTitle("Output Options")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        layout.setSpacing(12)

        description = QLabel(
            "Choose the format used for automatic and manual exports. "
            "Metadata and cover art are applied to supported formats."
        )
        description.setObjectName("secondaryInfoLabel")
        description.setWordWrap(True)
        layout.addWidget(description)

        form_layout = QFormLayout()
        form_layout.setHorizontalSpacing(12)
        form_layout.setVerticalSpacing(10)

        self.format_combo = QComboBox()
        self.format_combo.addItems(["m4b", "opus", "mp3", "wav"])
        form_layout.addRow("Format:", self.format_combo)

        self.bitrate_combo = QComboBox()
        self.bitrate_combo.addItems(["16k", "32k", "64k", "128k", "196k", "312k"])
        form_layout.addRow("Bitrate:", self.bitrate_combo)
        layout.addLayout(form_layout)

        asset_layout = QHBoxLayout()
        self.upload_cover_button = QPushButton("Upload Cover")
        self.metadata_button = QPushButton("Metadata")
        asset_layout.addWidget(self.upload_cover_button)
        asset_layout.addWidget(self.metadata_button)
        asset_layout.addStretch(1)
        layout.addLayout(asset_layout)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.format_combo.currentTextChanged.connect(self._set_output_format)
        self.bitrate_combo.currentTextChanged.connect(self._set_bitrate)
        self.upload_cover_button.clicked.connect(self._on_upload_cover)
        self.metadata_button.clicked.connect(self._on_metadata)
        self.logic.state_changed.connect(self._update_from_state)

        self._update_from_state()

    def _update_from_state(self):
        state = self.logic.state
        self.format_combo.blockSignals(True)
        self.bitrate_combo.blockSignals(True)
        self.format_combo.setCurrentText(state.audio_processing.output_format)
        self.bitrate_combo.setCurrentText(state.audio_processing.bitrate)
        self.format_combo.blockSignals(False)
        self.bitrate_combo.blockSignals(False)

        has_cover = bool(state.cover_image_path)
        self.upload_cover_button.setText(
            "Replace Cover" if has_cover else "Upload Cover"
        )
        self.upload_cover_button.setToolTip(state.cover_image_path or "")

    def _set_output_format(self, output_format: str):
        self.logic.state.audio_processing.output_format = output_format

    def _set_bitrate(self, bitrate: str):
        self.logic.state.audio_processing.bitrate = bitrate

    def _on_upload_cover(self):
        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Cover Image",
            "",
            "Image Files (*.png *.jpg *.jpeg);;All files (*.*)",
        )
        if file_path:
            self.logic.select_cover_image(file_path)

    def _on_metadata(self):
        metadata_to_edit = dict(self.logic.state.metadata or {})
        dialog = MetadataDialog(metadata_to_edit, self)
        if dialog.exec():
            self.logic.save_metadata(dialog.get_metadata())
