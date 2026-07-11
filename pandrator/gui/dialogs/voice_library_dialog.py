import os
import shutil
import subprocess
import tempfile
import threading
import time

from PyQt6.QtCore import QTimer, QUrl, Qt, pyqtSignal
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

try:
    from PyQt6.QtMultimedia import QAudioInput, QMediaCaptureSession, QMediaDevices, QMediaRecorder
except ImportError:  # pragma: no cover - optional runtime capability
    QAudioInput = QMediaCaptureSession = QMediaDevices = QMediaRecorder = None

from ...constants import LANGUAGE_DISPLAY_NAMES, XTTS_LANGUAGES
from ...logic.dubbing.stt_backends import (
    detect_stt_backend_statuses,
    language_options_for_backend,
)


VOICE_UPLOAD_SERVICES = {"XTTS", "VoxCPM", "FishS2", "Chatterbox", "Qwen3 TTS"}
TEXT_FILE_ENCODINGS = ("utf-8", "utf-8-sig", "cp1250", "cp1252", "latin-1")
VOICE_LANGUAGE_DEFAULT_LABEL = "Use Current Service Language"


class VoiceLibraryDialog(QDialog):
    transcription_finished = pyqtSignal(object)
    transcription_progress = pyqtSignal(int, int, str)

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._voices: list[dict] = []
        self._updating_tables = False

        self.setWindowTitle("Manage Voices")
        screen = self.screen() or (parent.screen() if parent is not None else None)
        available = screen.availableGeometry() if screen is not None else None
        if available is not None:
            self.resize(min(1320, available.width()), max(680, available.height() - 40))
        else:
            self.resize(1180, 820)
        self._stt_running = False
        self._capture_session = None
        self._audio_input = None
        self._media_recorder = None
        self._recorded_source_path = ""
        self._recorded_wav_path = ""
        self._recording_dir = tempfile.mkdtemp(prefix="pandrator_voice_recording_")
        self._recording_started_at = 0.0
        self._recording_timer = QTimer(self)
        self._recording_timer.setInterval(250)
        self._recording_timer.timeout.connect(self._update_recording_timer)

        self._build_ui()
        self._connect_signals()
        self.transcription_finished.connect(self._on_transcription_finished)
        self.transcription_progress.connect(self._on_transcription_progress)
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
        self.play_sample_button = QPushButton("Play")
        self.stop_sample_button = QPushButton("Stop")
        self.upload_button = QPushButton("Upload")
        sample_actions_layout.addWidget(self.add_sample_button)
        sample_actions_layout.addWidget(self.remove_sample_button)
        sample_actions_layout.addWidget(self.play_sample_button)
        sample_actions_layout.addWidget(self.stop_sample_button)
        sample_actions_layout.addStretch(1)
        sample_actions_layout.addWidget(self.upload_button)
        right_layout.addLayout(sample_actions_layout)

        stt_actions_layout = QHBoxLayout()
        self.stt_backend_combo = QComboBox()
        for backend, status in detect_stt_backend_statuses().items():
            if status.installed:
                self.stt_backend_combo.addItem(status.label, backend)
        self.stt_language_combo = QComboBox()
        self.transcribe_sample_button = QPushButton("Transcribe Selected")
        self.transcribe_missing_button = QPushButton("Transcribe Missing")
        stt_actions_layout.addWidget(QLabel("Local STT:"))
        stt_actions_layout.addWidget(self.stt_backend_combo)
        stt_actions_layout.addWidget(QLabel("Language:"))
        stt_actions_layout.addWidget(self.stt_language_combo)
        stt_actions_layout.addWidget(self.transcribe_sample_button)
        stt_actions_layout.addWidget(self.transcribe_missing_button)
        right_layout.addLayout(stt_actions_layout)

        recording_layout = QHBoxLayout()
        self.microphone_combo = QComboBox()
        if QMediaDevices is not None:
            for device in QMediaDevices.audioInputs():
                self.microphone_combo.addItem(device.description(), device)
        self.record_button = QPushButton("Record Sample")
        self.stop_record_button = QPushButton("Stop Recording")
        self.stop_record_button.setEnabled(False)
        self.play_recording_button = QPushButton("Play Recording")
        self.save_recording_button = QPushButton("Save Recording")
        self.discard_recording_button = QPushButton("Discard")
        self.recording_status_label = QLabel("Ready")
        self.recording_status_label.setObjectName("secondaryInfoLabel")
        recording_layout.addWidget(QLabel("Microphone:"))
        recording_layout.addWidget(self.microphone_combo, 1)
        recording_layout.addWidget(self.record_button)
        recording_layout.addWidget(self.stop_record_button)
        recording_layout.addWidget(self.play_recording_button)
        recording_layout.addWidget(self.save_recording_button)
        recording_layout.addWidget(self.discard_recording_button)
        right_layout.addLayout(recording_layout)
        right_layout.addWidget(self.recording_status_label)

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
        self.stt_backend_combo.currentIndexChanged.connect(self._refresh_stt_language_options)
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
        self.play_sample_button.clicked.connect(self._on_play_sample)
        self.stop_sample_button.clicked.connect(self.logic.stop_playback)
        self.transcribe_sample_button.clicked.connect(self._on_transcribe_selected)
        self.transcribe_missing_button.clicked.connect(self._on_transcribe_missing)
        self.record_button.clicked.connect(self._on_start_recording)
        self.stop_record_button.clicked.connect(self._on_stop_recording)
        self.play_recording_button.clicked.connect(
            lambda: self.logic.play_audio_file(self._recorded_wav_path)
        )
        self.save_recording_button.clicked.connect(self._on_save_recording)
        self.discard_recording_button.clicked.connect(self._discard_recording)
        self.upload_button.clicked.connect(self._on_upload)

        self.logic.state_changed.connect(self._refresh_action_state)
        self._refresh_stt_language_options()

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
        self.play_sample_button.setEnabled(bool(selected_sample_id))
        self.stop_sample_button.setEnabled(bool(selected_sample_id))
        stt_available = self.stt_backend_combo.count() > 0 and not self._stt_running
        self.stt_backend_combo.setEnabled(stt_available)
        self.stt_language_combo.setEnabled(stt_available and self.stt_language_combo.count() > 0)
        self.transcribe_sample_button.setEnabled(bool(selected_sample_id) and stt_available)
        self.transcribe_missing_button.setEnabled(
            bool(selected_voice is not None and sample_count and stt_available)
        )
        ffmpeg_available = bool(shutil.which("ffmpeg"))
        can_record = (
            QMediaRecorder is not None
            and self.microphone_combo.count() > 0
            and ffmpeg_available
        )
        self.record_button.setEnabled(bool(selected_voice is not None and can_record and self._media_recorder is None))
        if not ffmpeg_available:
            self.record_button.setToolTip("FFmpeg is required to normalize microphone recordings.")
        elif self.microphone_combo.count() == 0:
            self.record_button.setToolTip("No microphone input is available.")
        else:
            self.record_button.setToolTip("")
        self.play_recording_button.setEnabled(bool(self._recorded_wav_path and os.path.isfile(self._recorded_wav_path)))
        self.save_recording_button.setEnabled(
            bool(selected_voice is not None and self._recorded_wav_path and os.path.isfile(self._recorded_wav_path))
        )
        self.discard_recording_button.setEnabled(bool(self._recorded_source_path or self._recorded_wav_path))
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

    def _on_play_sample(self):
        voice_id = self._selected_voice_id()
        sample_id = self._selected_sample_id()
        path = self.logic.get_voice_library_sample_path(voice_id, sample_id)
        if path:
            self.logic.play_audio_file(path)

    def _refresh_stt_language_options(self):
        backend = str(self.stt_backend_combo.currentData() or "")
        current = str(self.stt_language_combo.currentData() or "")
        self.stt_language_combo.blockSignals(True)
        self.stt_language_combo.clear()
        for option in language_options_for_backend(backend):
            self.stt_language_combo.addItem(option.name, option.code or option.name)
        preferred = str(self.voice_language_combo.currentData() or current or "")
        index = self.stt_language_combo.findData(preferred)
        self.stt_language_combo.setCurrentIndex(index if index >= 0 else 0)
        self.stt_language_combo.blockSignals(False)

    def _transcription_targets(self, missing_only: bool) -> list[str]:
        voice = self._selected_voice()
        if voice is None:
            return []
        if not missing_only:
            selected = self._selected_sample_id()
            return [selected] if selected else []
        return [
            str(sample.get("id") or "")
            for sample in voice.get("samples", [])
            if not str(sample.get("transcript") or "").strip()
        ]

    def _start_transcription(self, missing_only: bool):
        targets = self._transcription_targets(missing_only)
        if not targets:
            QMessageBox.information(self, "Voice Transcription", "No matching samples need transcription.")
            return
        voice_id = self._selected_voice_id()
        backend = str(self.stt_backend_combo.currentData() or "")
        language = str(self.stt_language_combo.currentData() or "en")
        self._stt_running = True
        self.recording_status_label.setText(f"Transcribing {len(targets)} sample(s)…")
        self._refresh_action_state()

        def worker():
            errors = []
            transcripts = []
            total = len(targets)
            for index, sample_id in enumerate(targets, start=1):
                try:
                    transcript = self.logic.transcribe_voice_library_sample(
                        voice_id,
                        sample_id,
                        backend=backend,
                        language=language,
                    )
                    transcripts.append({"sample_id": sample_id, "transcript": transcript})
                except Exception as error:
                    errors.append(f"{sample_id}: {error}")
                self.transcription_progress.emit(index, total, sample_id)
            self.transcription_finished.emit(
                {"voice_id": voice_id, "transcripts": transcripts, "errors": errors}
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_transcribe_selected(self):
        self._start_transcription(False)

    def _on_transcribe_missing(self):
        self._start_transcription(True)

    def _on_transcription_progress(self, current: int, total: int, sample_id: str):
        self.recording_status_label.setText(
            f"Transcribing sample {current}/{total}: {sample_id}"
        )

    def _on_transcription_finished(self, payload: dict):
        self._stt_running = False
        errors = list(payload.get("errors") or [])
        voice_id = str(payload.get("voice_id") or "")
        saved = 0
        for draft in payload.get("transcripts") or []:
            sample_id = str(draft.get("sample_id") or "")
            transcript, accepted = QInputDialog.getMultiLineText(
                self,
                "Review Voice Transcript",
                f"Review transcript for {sample_id}. Save only if it is correct:",
                str(draft.get("transcript") or ""),
            )
            if not accepted:
                continue
            try:
                self.logic.update_voice_library_sample(
                    voice_id,
                    sample_id,
                    transcript=transcript.strip(),
                )
                saved += 1
            except Exception as error:
                errors.append(f"{sample_id}: {error}")
        self.reload_library(preferred_voice_id=voice_id)
        self.recording_status_label.setText(f"Saved {saved} reviewed transcript(s).")
        if errors:
            QMessageBox.warning(self, "Voice Transcription", "\n".join(errors))

    def _on_start_recording(self):
        if QMediaRecorder is None or QMediaCaptureSession is None or QAudioInput is None:
            QMessageBox.warning(self, "Recording", "Qt Multimedia recording is unavailable.")
            return
        device = self.microphone_combo.currentData()
        if device is None:
            QMessageBox.warning(self, "Recording", "No microphone is available.")
            return
        self._discard_recording()
        os.makedirs(self._recording_dir, exist_ok=True)
        target = os.path.join(self._recording_dir, "capture.m4a")
        try:
            self._capture_session = QMediaCaptureSession(self)
            self._audio_input = QAudioInput(device, self)
            self._media_recorder = QMediaRecorder(self)
            self._capture_session.setAudioInput(self._audio_input)
            self._capture_session.setRecorder(self._media_recorder)
            self._media_recorder.setOutputLocation(QUrl.fromLocalFile(target))
            if hasattr(self._media_recorder, "errorOccurred"):
                self._media_recorder.errorOccurred.connect(self._on_recording_error)
            if hasattr(self._media_recorder, "actualLocationChanged"):
                self._media_recorder.actualLocationChanged.connect(
                    lambda url: setattr(self, "_recorded_source_path", url.toLocalFile())
                )
            self._recorded_source_path = target
            self._media_recorder.record()
            self._recording_started_at = time.monotonic()
            self._recording_timer.start()
        except Exception as error:
            self._media_recorder = None
            QMessageBox.warning(self, "Recording", f"Could not start recording: {error}")
            return
        self.record_button.setEnabled(False)
        self.stop_record_button.setEnabled(True)
        self.recording_status_label.setText("Recording 00:00")

    def _update_recording_timer(self):
        if not self._recording_started_at:
            return
        elapsed = max(0, int(time.monotonic() - self._recording_started_at))
        minutes, seconds = divmod(elapsed, 60)
        self.recording_status_label.setText(f"Recording {minutes:02d}:{seconds:02d}")

    def _on_recording_error(self, *_args):
        message = self._media_recorder.errorString() if self._media_recorder is not None else ""
        self.recording_status_label.setText(
            f"Recording error: {message or 'the audio device reported an error.'}"
        )

    def _on_stop_recording(self):
        if self._media_recorder is None:
            return
        try:
            location = self._media_recorder.outputLocation().toLocalFile()
            if location:
                self._recorded_source_path = location
            self._media_recorder.stop()
        finally:
            self._recording_timer.stop()
            self._recording_started_at = 0.0
            self.stop_record_button.setEnabled(False)
            self.recording_status_label.setText("Finalizing recording…")
            QTimer.singleShot(600, self._finish_recording)

    def _finish_recording(self):
        candidates = [
            os.path.join(self._recording_dir, name)
            for name in os.listdir(self._recording_dir)
            if os.path.isfile(os.path.join(self._recording_dir, name))
        ]
        if self._recorded_source_path and os.path.isfile(self._recorded_source_path):
            source = self._recorded_source_path
        elif candidates:
            source = max(candidates, key=os.path.getmtime)
        else:
            source = ""
        self._media_recorder = None
        self._capture_session = None
        self._audio_input = None
        if not source:
            self.recording_status_label.setText("Recording failed: no audio file was created.")
            self._refresh_action_state()
            return
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            self.recording_status_label.setText("FFmpeg is required to normalize the recording.")
            self._refresh_action_state()
            return
        output_path = os.path.join(self._recording_dir, "recorded_sample.wav")
        process = subprocess.run(
            [ffmpeg, "-y", "-i", source, "-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", output_path],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if process.returncode != 0 or not os.path.isfile(output_path):
            self.recording_status_label.setText("Could not convert the recording to WAV.")
            self._refresh_action_state()
            return
        self._recorded_wav_path = output_path
        self.recording_status_label.setText("Recording ready. Play, save, or discard it.")
        self._refresh_action_state()

    def _on_save_recording(self):
        voice_id = self._selected_voice_id()
        if not voice_id or not self._recorded_wav_path:
            return
        try:
            added = self.logic.add_voice_library_samples(voice_id, [self._recorded_wav_path])
        except Exception as error:
            QMessageBox.warning(self, "Recording", str(error))
            return
        sample_id = str(added[0].get("id") or "") if added else ""
        self._discard_recording()
        self.reload_library(preferred_voice_id=voice_id, preferred_sample_id=sample_id)

    def _discard_recording(self):
        self._recording_timer.stop()
        self._recording_started_at = 0.0
        self.logic.stop_playback()
        for path in {self._recorded_source_path, self._recorded_wav_path}:
            if path and os.path.isfile(path):
                try:
                    os.remove(path)
                except OSError:
                    pass
        self._recorded_source_path = ""
        self._recorded_wav_path = ""
        self.recording_status_label.setText("Ready")
        self._refresh_action_state()

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

    def closeEvent(self, event):
        if self._media_recorder is not None:
            try:
                self._media_recorder.stop()
            except Exception:
                pass
        self.logic.stop_playback()
        shutil.rmtree(self._recording_dir, ignore_errors=True)
        super().closeEvent(event)
