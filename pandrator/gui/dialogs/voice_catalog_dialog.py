import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)


DEFAULT_PREVIEW_TEXT = "The quick brown fox jumps over the lazy dog."


class VoiceCatalogDialog(QDialog):
    preview_generated = pyqtSignal(str, bool, str, str)
    generation_finished = pyqtSignal()

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._voice_rows: dict[str, int] = {}
        self._preview_paths: dict[str, str] = {}
        self._generation_thread: threading.Thread | None = None

        self.setWindowTitle("Browse Voices")
        self.resize(960, 680)

        self._build_ui()
        self._connect_signals()
        self.reload_voices(use_remote=False)

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        self.description_label = QLabel(
            "Browse built-in voices for the active TTS service and generate fast preview samples for comparison."
        )
        self.description_label.setWordWrap(True)
        self.description_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.description_label)

        self.service_label = QLabel("")
        self.service_label.setWordWrap(True)
        self.service_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.service_label)

        main_layout.addWidget(QLabel("Preview Text"))
        self.preview_text_edit = QTextEdit(self)
        self.preview_text_edit.setPlaceholderText("Type sample text to render with each voice")
        self.preview_text_edit.setPlainText(DEFAULT_PREVIEW_TEXT)
        self.preview_text_edit.setFixedHeight(90)
        main_layout.addWidget(self.preview_text_edit)

        self.voice_table = QTableWidget(0, 3, self)
        self.voice_table.setHorizontalHeaderLabels(["Voice", "Status", "Preview WAV"])
        self.voice_table.verticalHeader().setVisible(False)
        self.voice_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.voice_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.voice_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.voice_table.setAlternatingRowColors(True)
        header = self.voice_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        main_layout.addWidget(self.voice_table, 1)

        actions_layout = QGridLayout()

        self.refresh_button = QPushButton("Refresh Voices")
        self.generate_selected_button = QPushButton("Generate Selected")
        self.generate_all_button = QPushButton("Generate All")
        self.play_selected_button = QPushButton("Play Selected")
        self.use_selected_button = QPushButton("Use Selected Voice")

        actions_layout.addWidget(self.refresh_button, 0, 0)
        actions_layout.addWidget(self.generate_selected_button, 0, 1)
        actions_layout.addWidget(self.generate_all_button, 0, 2)
        actions_layout.addWidget(self.play_selected_button, 1, 1)
        actions_layout.addWidget(self.use_selected_button, 1, 2)

        main_layout.addLayout(actions_layout)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)
        self.close_button = self.buttons.button(QDialogButtonBox.StandardButton.Close)
        main_layout.addWidget(self.buttons)

    def _connect_signals(self):
        self.voice_table.itemSelectionChanged.connect(self._refresh_action_state)

        self.refresh_button.clicked.connect(lambda: self.reload_voices(use_remote=True))
        self.generate_selected_button.clicked.connect(self._on_generate_selected)
        self.generate_all_button.clicked.connect(self._on_generate_all)
        self.play_selected_button.clicked.connect(self._on_play_selected)
        self.use_selected_button.clicked.connect(self._on_use_selected_voice)

        self.preview_generated.connect(self._on_preview_generated)
        self.generation_finished.connect(self._on_generation_finished)

        self.logic.state_changed.connect(self._refresh_header)

    def _refresh_header(self):
        service = str(self.logic.state.tts.service or "").strip() or "Unknown"
        model = str(self.logic.state.tts.xtts_model or "").strip() or "(auto)"
        provider = str(self.logic.state.tts.openai_audio_endpoint or "").strip()

        if service == "OpenAI-Compatible" and provider:
            service_text = f"Active service: {service} ({provider})"
        else:
            service_text = f"Active service: {service}"

        self.service_label.setText(f"{service_text} | Active model: {model}")

    def _is_generating(self) -> bool:
        return self._generation_thread is not None and self._generation_thread.is_alive()

    def _selected_voice_ids(self) -> list[str]:
        selected_rows = sorted({item.row() for item in self.voice_table.selectedItems()})
        voices: list[str] = []
        seen: set[str] = set()
        for row in selected_rows:
            voice_item = self.voice_table.item(row, 0)
            if voice_item is None:
                continue
            voice_id = str(voice_item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not voice_id:
                continue
            lowered = voice_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            voices.append(voice_id)
        return voices

    def _selected_voice_id(self) -> str:
        voices = self._selected_voice_ids()
        return voices[0] if voices else ""

    def _first_row_for_voice(self, voice_id: str) -> int:
        return int(self._voice_rows.get(str(voice_id or "").strip(), -1))

    def reload_voices(self, *, use_remote: bool):
        if self._is_generating():
            return

        self._refresh_header()

        if not self.logic.tts_service_supports_prebuilt_voices():
            self.voice_table.setRowCount(0)
            self._voice_rows = {}
            self.status_label.setText(
                "Current service does not expose built-in voices. Use Manage Voice Samples for voice-cloning services."
            )
            self._refresh_action_state()
            return

        voices = self.logic.list_active_prebuilt_voices(
            use_remote=use_remote,
            update_state=False,
        )

        self.voice_table.setRowCount(len(voices))
        self._voice_rows = {}

        for row, voice_id in enumerate(voices):
            normalized_voice_id = str(voice_id or "").strip()
            self._voice_rows[normalized_voice_id] = row

            voice_item = QTableWidgetItem(normalized_voice_id)
            voice_item.setData(Qt.ItemDataRole.UserRole, normalized_voice_id)
            self.voice_table.setItem(row, 0, voice_item)

            status_item = QTableWidgetItem("Not generated")
            self.voice_table.setItem(row, 1, status_item)

            preview_path = self._preview_paths.get(normalized_voice_id, "")
            preview_item = QTableWidgetItem(preview_path)
            self.voice_table.setItem(row, 2, preview_item)

            if preview_path:
                status_item.setText("Ready")

        if voices:
            current_speaker = str(self.logic.state.tts.speaker or "").strip()
            target_row = self._first_row_for_voice(current_speaker)
            if target_row < 0:
                target_row = 0
            self.voice_table.selectRow(target_row)
            self.status_label.setText(
                "Tip: Select one or more voices, generate previews, then click 'Use Selected Voice' to apply."
            )
        else:
            self.status_label.setText(
                "No voices found. Connect to the TTS service, then click Refresh Voices."
            )

        self._refresh_action_state()

    def _refresh_action_state(self):
        has_rows = self.voice_table.rowCount() > 0
        has_selection = bool(self._selected_voice_ids())
        generating = self._is_generating()
        selected_voice = self._selected_voice_id()
        has_preview = bool(self._preview_paths.get(selected_voice, ""))

        self.refresh_button.setEnabled((not generating) and self.logic.tts_service_supports_prebuilt_voices())
        self.generate_all_button.setEnabled((not generating) and has_rows)
        self.generate_selected_button.setEnabled((not generating) and has_selection)
        self.play_selected_button.setEnabled((not generating) and has_preview)
        self.use_selected_button.setEnabled((not generating) and has_selection)
        if self.close_button is not None:
            self.close_button.setEnabled(not generating)

    def _on_generate_selected(self):
        voices = self._selected_voice_ids()
        if not voices:
            QMessageBox.information(self, "Voice Preview", "Select at least one voice first.")
            return
        self._start_generation(voices)

    def _on_generate_all(self):
        voices: list[str] = []
        for row in range(self.voice_table.rowCount()):
            item = self.voice_table.item(row, 0)
            if item is None:
                continue
            voice_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if voice_id:
                voices.append(voice_id)

        if not voices:
            QMessageBox.information(self, "Voice Preview", "No voices available for generation.")
            return

        self._start_generation(voices)

    def _start_generation(self, voices: list[str]):
        if self._is_generating():
            return

        preview_text = self.preview_text_edit.toPlainText().strip()
        if not preview_text:
            QMessageBox.warning(self, "Voice Preview", "Preview text cannot be empty.")
            return

        for voice_id in voices:
            row = self._first_row_for_voice(voice_id)
            if row < 0:
                continue
            status_item = self.voice_table.item(row, 1)
            if status_item is None:
                status_item = QTableWidgetItem()
                self.voice_table.setItem(row, 1, status_item)
            status_item.setText("Generating...")

        self.status_label.setText(f"Generating {len(voices)} preview sample(s)...")
        self._refresh_action_state()

        def worker():
            for voice_id in voices:
                success, output_path, error_message = self.logic.generate_prebuilt_voice_preview_sample(
                    voice_id,
                    preview_text,
                )
                self.preview_generated.emit(voice_id, success, output_path, error_message)
            self.generation_finished.emit()

        self._generation_thread = threading.Thread(target=worker, daemon=True)
        self._generation_thread.start()

    def _on_preview_generated(self, voice_id: str, success: bool, output_path: str, error_message: str):
        normalized_voice_id = str(voice_id or "").strip()
        row = self._first_row_for_voice(normalized_voice_id)
        if row < 0:
            return

        status_item = self.voice_table.item(row, 1)
        if status_item is None:
            status_item = QTableWidgetItem()
            self.voice_table.setItem(row, 1, status_item)

        preview_item = self.voice_table.item(row, 2)
        if preview_item is None:
            preview_item = QTableWidgetItem()
            self.voice_table.setItem(row, 2, preview_item)

        if success and output_path:
            self._preview_paths[normalized_voice_id] = output_path
            status_item.setText("Ready")
            status_item.setToolTip("")
            preview_item.setText(output_path)
            preview_item.setToolTip(output_path)
        else:
            self._preview_paths.pop(normalized_voice_id, None)
            status_item.setText("Failed")
            status_item.setToolTip(error_message or "Unknown error")
            preview_item.setText("")
            preview_item.setToolTip(error_message or "Unknown error")

        self._refresh_action_state()

    def _on_generation_finished(self):
        self._generation_thread = None
        self.status_label.setText("Preview generation finished.")
        self._refresh_action_state()

    def _on_play_selected(self):
        voice_id = self._selected_voice_id()
        if not voice_id:
            QMessageBox.information(self, "Voice Preview", "Select a voice first.")
            return

        preview_path = str(self._preview_paths.get(voice_id, "")).strip()
        if not preview_path:
            QMessageBox.information(
                self,
                "Voice Preview",
                "Generate a preview for the selected voice first.",
            )
            return

        if not self.logic.play_audio_file(preview_path):
            QMessageBox.warning(self, "Voice Preview", "Could not play the selected preview WAV.")

    def _on_use_selected_voice(self):
        voice_id = self._selected_voice_id()
        if not voice_id:
            QMessageBox.information(self, "Voice Preview", "Select a voice first.")
            return

        self.logic.set_tts_speaker_voice(voice_id)
        self.status_label.setText(f"Selected voice: {voice_id}")

    def closeEvent(self, event):
        if self._is_generating():
            QMessageBox.information(
                self,
                "Voice Preview",
                "Please wait for preview generation to finish before closing this window.",
            )
            event.ignore()
            return
        super().closeEvent(event)
