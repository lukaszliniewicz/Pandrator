import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ...constants import LANGUAGE_DISPLAY_NAMES, XTTS_LANGUAGES


VOICE_UPLOAD_SERVICES = {"XTTS", "VoxCPM", "FishS2", "Chatterbox", "Qwen3 TTS"}
TEXT_FILE_ENCODINGS = ("utf-8", "utf-8-sig", "cp1250", "cp1252", "latin-1")
VOICE_LANGUAGE_DEFAULT_LABEL = "Use Current Service Language"


class VoiceLibraryDialog(QDialog):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._voices: list[dict] = []
        self._updating_tables = False

        self.setWindowTitle("Manage Voices")
        self.resize(1080, 680)

        self._build_ui()
        self._connect_signals()
        self.reload_library()

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        self.description_label = QLabel(
            "Save voice samples with optional transcript/notes, attach a voice prompt from .txt, "
            "review durations, and upload to the active TTS service."
        )
        self.description_label.setWordWrap(True)
        self.description_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.description_label)

        self.service_hint_label = QLabel("")
        self.service_hint_label.setWordWrap(True)
        self.service_hint_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.service_hint_label)

        content_layout = QGridLayout()
        content_layout.setHorizontalSpacing(10)
        content_layout.setVerticalSpacing(8)
        main_layout.addLayout(content_layout, 1)

        left_panel = QWidget(self)
        left_layout = QVBoxLayout(left_panel)
        left_layout.setSpacing(6)

        left_layout.addWidget(QLabel("Voices"))
        self.voice_table = QTableWidget(0, 3, self)
        self.voice_table.setHorizontalHeaderLabels(["Name", "Samples", "Duration"])
        self.voice_table.verticalHeader().setVisible(False)
        self.voice_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.voice_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.voice_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.voice_table.setAlternatingRowColors(True)
        voice_header = self.voice_table.horizontalHeader()
        voice_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        voice_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        voice_header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        left_layout.addWidget(self.voice_table, 1)

        voice_actions_layout = QHBoxLayout()
        self.add_voice_button = QPushButton("New Voice")
        self.rename_voice_button = QPushButton("Rename")
        self.delete_voice_button = QPushButton("Delete")
        voice_actions_layout.addWidget(self.add_voice_button)
        voice_actions_layout.addWidget(self.rename_voice_button)
        voice_actions_layout.addWidget(self.delete_voice_button)
        voice_actions_layout.addStretch(1)
        left_layout.addLayout(voice_actions_layout)

        left_layout.addWidget(QLabel("Voice Notes"))
        self.voice_notes_edit = QTextEdit(self)
        self.voice_notes_edit.setPlaceholderText("Optional notes for this voice entry")
        self.voice_notes_edit.setFixedHeight(120)
        left_layout.addWidget(self.voice_notes_edit)

        notes_actions_layout = QHBoxLayout()
        self.save_voice_notes_button = QPushButton("Save Notes")
        notes_actions_layout.addWidget(self.save_voice_notes_button)
        notes_actions_layout.addStretch(1)
        left_layout.addLayout(notes_actions_layout)

        left_layout.addWidget(QLabel("Voice Language"))
        self.voice_language_combo = QComboBox(self)
        self.voice_language_combo.addItem(VOICE_LANGUAGE_DEFAULT_LABEL, "")
        for language_code in XTTS_LANGUAGES:
            normalized_language_code = str(language_code or "").strip().lower()
            if not normalized_language_code:
                continue
            self.voice_language_combo.addItem(
                LANGUAGE_DISPLAY_NAMES.get(normalized_language_code, normalized_language_code.upper()),
                normalized_language_code,
            )
        left_layout.addWidget(self.voice_language_combo)

        left_layout.addWidget(QLabel("Upload Prompt (TXT)"))
        self.voice_prompt_edit = QTextEdit(self)
        self.voice_prompt_edit.setPlaceholderText(
            "Optional prompt text used when uploading this voice to VoxCPM/FishS2/Chatterbox. Qwen3 TTS uses the selected sample only."
        )
        self.voice_prompt_edit.setFixedHeight(100)
        left_layout.addWidget(self.voice_prompt_edit)

        prompt_actions_layout = QHBoxLayout()
        self.load_voice_prompt_button = QPushButton("Load TXT Prompt")
        self.save_voice_prompt_button = QPushButton("Save Prompt")
        self.clear_voice_prompt_button = QPushButton("Clear Prompt")
        prompt_actions_layout.addWidget(self.load_voice_prompt_button)
        prompt_actions_layout.addWidget(self.save_voice_prompt_button)
        prompt_actions_layout.addWidget(self.clear_voice_prompt_button)
        prompt_actions_layout.addStretch(1)
        left_layout.addLayout(prompt_actions_layout)

        right_panel = QWidget(self)
        right_layout = QVBoxLayout(right_panel)
        right_layout.setSpacing(6)

        right_layout.addWidget(QLabel("Samples"))
        self.sample_table = QTableWidget(0, 4, self)
        self.sample_table.setHorizontalHeaderLabels(["File", "Duration", "Transcript", "Notes"])
        self.sample_table.verticalHeader().setVisible(False)
        self.sample_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sample_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.sample_table.setAlternatingRowColors(True)
        sample_header = self.sample_table.horizontalHeader()
        sample_header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        sample_header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        sample_header.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        sample_header.setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.sample_table.setWordWrap(True)
        right_layout.addWidget(self.sample_table, 1)

        sample_actions_layout = QHBoxLayout()
        self.add_sample_button = QPushButton("Add WAV Sample(s)")
        self.remove_sample_button = QPushButton("Remove Sample")
        self.upload_button = QPushButton("Upload")
        sample_actions_layout.addWidget(self.add_sample_button)
        sample_actions_layout.addWidget(self.remove_sample_button)
        sample_actions_layout.addStretch(1)
        sample_actions_layout.addWidget(self.upload_button)
        right_layout.addLayout(sample_actions_layout)

        self.sample_hint_label = QLabel(
            "Tip: if a WAV has a same-name .txt file next to it, transcript is imported automatically."
        )
        self.sample_hint_label.setWordWrap(True)
        self.sample_hint_label.setObjectName("secondaryInfoLabel")
        right_layout.addWidget(self.sample_hint_label)

        content_layout.addWidget(left_panel, 0, 0)
        content_layout.addWidget(right_panel, 0, 1)
        content_layout.setColumnStretch(0, 1)
        content_layout.setColumnStretch(1, 2)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        main_layout.addWidget(buttons)

    def _connect_signals(self):
        self.voice_table.itemSelectionChanged.connect(self._on_voice_selection_changed)
        self.sample_table.itemSelectionChanged.connect(self._refresh_action_state)
        self.sample_table.itemChanged.connect(self._on_sample_item_changed)
        self.voice_prompt_edit.textChanged.connect(self._refresh_action_state)
        self.voice_language_combo.currentIndexChanged.connect(self._on_voice_language_changed)

        self.add_voice_button.clicked.connect(self._on_add_voice)
        self.rename_voice_button.clicked.connect(self._on_rename_voice)
        self.delete_voice_button.clicked.connect(self._on_delete_voice)
        self.save_voice_notes_button.clicked.connect(self._on_save_voice_notes)
        self.load_voice_prompt_button.clicked.connect(self._on_load_voice_prompt)
        self.save_voice_prompt_button.clicked.connect(self._on_save_voice_prompt)
        self.clear_voice_prompt_button.clicked.connect(self._on_clear_voice_prompt)

        self.add_sample_button.clicked.connect(self._on_add_samples)
        self.remove_sample_button.clicked.connect(self._on_remove_sample)
        self.upload_button.clicked.connect(self._on_upload)

        self.logic.state_changed.connect(self._refresh_action_state)

    @staticmethod
    def _format_duration(seconds: float) -> str:
        total_seconds = int(round(max(0.0, float(seconds or 0.0))))
        hours, remainder = divmod(total_seconds, 3600)
        minutes, secs = divmod(remainder, 60)
        if hours:
            return f"{hours}:{minutes:02d}:{secs:02d}"
        return f"{minutes}:{secs:02d}"

    def _current_service(self) -> str:
        return str(self.logic.state.tts.service or "").strip()

    def _selected_voice_id(self) -> str:
        selected_ranges = self.voice_table.selectedRanges()
        if not selected_ranges:
            return ""

        row = selected_ranges[0].topRow()
        item = self.voice_table.item(row, 0)
        if item is None:
            return ""

        return str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

    def _selected_sample_id(self) -> str:
        selected_ranges = self.sample_table.selectedRanges()
        if not selected_ranges:
            return ""

        row = selected_ranges[0].topRow()
        item = self.sample_table.item(row, 0)
        if item is None:
            return ""

        return str(item.data(Qt.ItemDataRole.UserRole) or "").strip()

    def _selected_voice(self) -> dict | None:
        selected_voice_id = self._selected_voice_id()
        if not selected_voice_id:
            return None

        for voice in self._voices:
            if str(voice.get("id") or "").strip() == selected_voice_id:
                return voice

        return None

    def _populate_voice_table(self, preferred_voice_id: str = ""):
        self._updating_tables = True
        self.voice_table.setRowCount(len(self._voices))

        selected_row = -1
        for row, voice in enumerate(self._voices):
            voice_id = str(voice.get("id") or "").strip()
            voice_name = str(voice.get("name") or voice_id).strip() or voice_id
            sample_count = int(voice.get("sample_count") or len(voice.get("samples") or []))
            total_duration = float(voice.get("total_duration_seconds") or 0.0)

            name_item = QTableWidgetItem(voice_name)
            name_item.setData(Qt.ItemDataRole.UserRole, voice_id)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.voice_table.setItem(row, 0, name_item)

            sample_count_item = QTableWidgetItem(str(sample_count))
            sample_count_item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            sample_count_item.setFlags(sample_count_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.voice_table.setItem(row, 1, sample_count_item)

            duration_item = QTableWidgetItem(self._format_duration(total_duration))
            duration_item.setFlags(duration_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.voice_table.setItem(row, 2, duration_item)

            if preferred_voice_id and preferred_voice_id == voice_id:
                selected_row = row

        self._updating_tables = False

        if selected_row < 0 and self.voice_table.rowCount() > 0:
            selected_row = 0

        if selected_row >= 0:
            self.voice_table.selectRow(selected_row)

    def _populate_sample_table(self, voice: dict | None, preferred_sample_id: str = ""):
        samples = list(voice.get("samples") or []) if voice else []

        self._updating_tables = True
        self.sample_table.setRowCount(len(samples))

        selected_row = -1
        for row, sample in enumerate(samples):
            sample_id = str(sample.get("id") or "").strip()
            relative_path = str(sample.get("relative_path") or "").strip()
            file_name = str(sample.get("file_name") or "").strip() or os.path.basename(relative_path)
            if not bool(sample.get("path_exists", True)):
                file_name = f"{file_name} (missing)"

            file_item = QTableWidgetItem(file_name)
            file_item.setData(Qt.ItemDataRole.UserRole, sample_id)
            file_item.setFlags(file_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.sample_table.setItem(row, 0, file_item)

            duration_item = QTableWidgetItem(
                self._format_duration(float(sample.get("duration_seconds") or 0.0))
            )
            duration_item.setFlags(duration_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.sample_table.setItem(row, 1, duration_item)

            transcript_item = QTableWidgetItem(str(sample.get("transcript") or ""))
            self.sample_table.setItem(row, 2, transcript_item)

            notes_item = QTableWidgetItem(str(sample.get("notes") or ""))
            self.sample_table.setItem(row, 3, notes_item)

            if preferred_sample_id and preferred_sample_id == sample_id:
                selected_row = row

        self._updating_tables = False

        if selected_row < 0 and self.sample_table.rowCount() > 0:
            selected_row = 0

        if selected_row >= 0:
            self.sample_table.selectRow(selected_row)

    def _set_voice_language_combo(self, language_code: str):
        normalized_language_code = str(language_code or "").strip().lower()
        target_index = self.voice_language_combo.findData(normalized_language_code)
        if target_index < 0:
            target_index = 0

        self.voice_language_combo.blockSignals(True)
        self.voice_language_combo.setCurrentIndex(target_index)
        self.voice_language_combo.blockSignals(False)

    def reload_library(self, preferred_voice_id: str = "", preferred_sample_id: str = ""):
        try:
            self._voices = self.logic.list_voice_library()
        except Exception as e:
            QMessageBox.critical(self, "Voice Library Error", f"Could not load voice library: {e}")
            self._voices = []

        self._populate_voice_table(preferred_voice_id)
        selected_voice = self._selected_voice()
        self._populate_sample_table(selected_voice, preferred_sample_id)

        if selected_voice is not None:
            self.voice_notes_edit.setPlainText(str(selected_voice.get("notes") or ""))
            self.voice_prompt_edit.setPlainText(str(selected_voice.get("prompt_text") or ""))
            self._set_voice_language_combo(str(selected_voice.get("language_code") or ""))
        else:
            self.voice_notes_edit.clear()
            self.voice_prompt_edit.clear()
            self._set_voice_language_combo("")

        self._refresh_action_state()

    def _refresh_action_state(self):
        service = self._current_service()
        selected_voice = self._selected_voice()
        selected_sample_id = self._selected_sample_id()
        sample_count = len(selected_voice.get("samples") or []) if selected_voice else 0

        self.rename_voice_button.setEnabled(selected_voice is not None)
        self.delete_voice_button.setEnabled(selected_voice is not None)
        self.save_voice_notes_button.setEnabled(selected_voice is not None)
        self.add_sample_button.setEnabled(selected_voice is not None)
        self.remove_sample_button.setEnabled(bool(selected_sample_id))
        self.voice_notes_edit.setEnabled(selected_voice is not None)
        self.voice_language_combo.setEnabled(selected_voice is not None)
        self.voice_prompt_edit.setEnabled(selected_voice is not None)
        self.load_voice_prompt_button.setEnabled(selected_voice is not None)
        self.save_voice_prompt_button.setEnabled(selected_voice is not None)
        self.clear_voice_prompt_button.setEnabled(
            selected_voice is not None and bool(self.voice_prompt_edit.toPlainText().strip())
        )

        if service == "XTTS":
            self.upload_button.setText("Upload Voice to XTTS")
            can_upload = selected_voice is not None and sample_count > 0
            self.service_hint_label.setText(
                "Active service: XTTS. Upload sends all samples from the selected voice entry."
            )
        elif service in {"VoxCPM", "FishS2", "Chatterbox", "Qwen3 TTS"}:
            self.upload_button.setText(f"Upload Selected Sample to {service}")
            can_upload = selected_voice is not None and bool(selected_sample_id)
            if service == "Qwen3 TTS":
                self.service_hint_label.setText(
                    "Active service: Qwen3 TTS. Upload uses selected sample audio as a reference voice."
                )
            else:
                self.service_hint_label.setText(
                    f"Active service: {service}. Upload uses selected sample audio and uses saved voice prompt text when set."
                )
        else:
            self.upload_button.setText("Upload")
            can_upload = False
            self.service_hint_label.setText(
                "Upload from library is available only for XTTS, VoxCPM, FishS2, Chatterbox, and Qwen3 TTS."
            )

        self.upload_button.setEnabled(can_upload)

    def _on_voice_selection_changed(self):
        if self._updating_tables:
            return

        selected_voice = self._selected_voice()
        self._populate_sample_table(selected_voice)
        if selected_voice is None:
            self.voice_notes_edit.clear()
            self.voice_prompt_edit.clear()
            self._set_voice_language_combo("")
        else:
            self.voice_notes_edit.setPlainText(str(selected_voice.get("notes") or ""))
            self.voice_prompt_edit.setPlainText(str(selected_voice.get("prompt_text") or ""))
            self._set_voice_language_combo(str(selected_voice.get("language_code") or ""))
        self._refresh_action_state()

    def _on_add_voice(self):
        name, ok = QInputDialog.getText(self, "New Voice", "Voice name:")
        if not ok:
            return

        normalized_name = str(name or "").strip()
        if not normalized_name:
            return

        try:
            created_voice = self.logic.create_voice_library_voice(normalized_name)
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(created_voice.get("id") or ""))

    def _on_rename_voice(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        current_name = str(selected_voice.get("name") or "").strip()
        new_name, ok = QInputDialog.getText(self, "Rename Voice", "Voice name:", text=current_name)
        if not ok:
            return

        normalized_name = str(new_name or "").strip()
        if not normalized_name or normalized_name == current_name:
            return

        try:
            self.logic.update_voice_library_voice(
                str(selected_voice.get("id") or ""),
                name=normalized_name,
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    def _on_delete_voice(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        voice_name = str(selected_voice.get("name") or "").strip() or "this voice"
        answer = QMessageBox.question(
            self,
            "Delete Voice",
            f"Delete '{voice_name}' and all its saved samples?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            self.logic.delete_voice_library_voice(str(selected_voice.get("id") or ""))
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library()

    def _on_save_voice_notes(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        try:
            self.logic.update_voice_library_voice(
                str(selected_voice.get("id") or ""),
                notes=self.voice_notes_edit.toPlainText(),
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    @staticmethod
    def _read_text_file(file_path: str) -> str:
        for encoding in TEXT_FILE_ENCODINGS:
            try:
                with open(file_path, "r", encoding=encoding) as handle:
                    return handle.read()
            except UnicodeDecodeError:
                continue

        with open(file_path, "r", encoding="utf-8", errors="ignore") as handle:
            return handle.read()

    def _on_load_voice_prompt(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        file_path, _ = QFileDialog.getOpenFileName(
            self,
            "Load Prompt TXT",
            "",
            "Text files (*.txt);;All files (*.*)",
        )
        if not file_path:
            return

        try:
            prompt_text = self._read_text_file(file_path)
            self.logic.update_voice_library_voice(
                str(selected_voice.get("id") or ""),
                prompt_text=prompt_text,
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    def _on_save_voice_prompt(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        try:
            self.logic.update_voice_library_voice(
                str(selected_voice.get("id") or ""),
                prompt_text=self.voice_prompt_edit.toPlainText(),
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    def _on_clear_voice_prompt(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        try:
            self.logic.update_voice_library_voice(
                str(selected_voice.get("id") or ""),
                prompt_text="",
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    def _on_voice_language_changed(self, _index: int = -1):
        if self._updating_tables:
            return

        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        language_code = str(self.voice_language_combo.currentData() or "").strip().lower()
        existing_language_code = str(selected_voice.get("language_code") or "").strip().lower()
        if language_code == existing_language_code:
            return

        try:
            self.logic.update_voice_library_voice(
                str(selected_voice.get("id") or ""),
                language_code=language_code,
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    def _on_add_samples(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        file_paths, _ = QFileDialog.getOpenFileNames(
            self,
            "Add WAV Samples",
            "",
            "WAV files (*.wav)",
        )
        if not file_paths:
            return

        try:
            added_samples = self.logic.add_voice_library_samples(
                str(selected_voice.get("id") or ""),
                list(file_paths),
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        preferred_sample_id = ""
        if added_samples:
            preferred_sample_id = str(added_samples[0].get("id") or "")

        self.reload_library(
            preferred_voice_id=str(selected_voice.get("id") or ""),
            preferred_sample_id=preferred_sample_id,
        )

    def _on_remove_sample(self):
        selected_voice = self._selected_voice()
        selected_sample_id = self._selected_sample_id()
        if selected_voice is None or not selected_sample_id:
            return

        answer = QMessageBox.question(
            self,
            "Remove Sample",
            "Delete selected sample from this voice entry?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return

        try:
            self.logic.delete_voice_library_sample(
                str(selected_voice.get("id") or ""),
                selected_sample_id,
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            return

        self.reload_library(preferred_voice_id=str(selected_voice.get("id") or ""))

    def _on_sample_item_changed(self, changed_item: QTableWidgetItem):
        if self._updating_tables:
            return

        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        row = changed_item.row()
        sample_item = self.sample_table.item(row, 0)
        if sample_item is None:
            return

        sample_id = str(sample_item.data(Qt.ItemDataRole.UserRole) or "").strip()
        if not sample_id:
            return

        transcript_item = self.sample_table.item(row, 2)
        notes_item = self.sample_table.item(row, 3)
        transcript_text = transcript_item.text() if transcript_item is not None else ""
        notes_text = notes_item.text() if notes_item is not None else ""

        try:
            self.logic.update_voice_library_sample(
                str(selected_voice.get("id") or ""),
                sample_id,
                transcript=transcript_text,
                notes=notes_text,
            )
        except Exception as e:
            QMessageBox.warning(self, "Voice Library", str(e))
            self.reload_library(
                preferred_voice_id=str(selected_voice.get("id") or ""),
                preferred_sample_id=sample_id,
            )

    def _on_upload(self):
        selected_voice = self._selected_voice()
        if selected_voice is None:
            return

        service = self._current_service()
        if service not in VOICE_UPLOAD_SERVICES:
            QMessageBox.warning(
                self,
                "Upload Voice",
                "Voice library upload is only supported for XTTS, VoxCPM, FishS2, Chatterbox, and Qwen3 TTS.",
            )
            return

        sample_id = None
        if service in {"VoxCPM", "FishS2", "Chatterbox", "Qwen3 TTS"}:
            selected_sample_id = self._selected_sample_id()
            if not selected_sample_id:
                QMessageBox.warning(
                    self,
                    "Upload Voice",
                    "Select a sample row to upload for this service.",
                )
                return
            sample_id = selected_sample_id

        success, uploaded_voice_name = self.logic.upload_voice_library_voice(
            str(selected_voice.get("id") or ""),
            sample_id=sample_id,
        )
        if success:
            QMessageBox.information(
                self,
                "Upload Voice",
                f"Uploaded successfully as '{uploaded_voice_name}'.",
            )
