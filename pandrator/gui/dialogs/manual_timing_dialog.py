import os
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl
from PyQt6.QtGui import QColor
from PyQt6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from ...logic.dubbing.manual_timing import (
    BoundaryEditorController,
    BoundaryEditorModel,
    BoundaryEditorPersistence,
    load_srt_timing_segments,
)

try:
    from PyQt6.QtMultimedia import QAudioOutput, QMediaPlayer
except Exception:  # pragma: no cover - depends on the local Qt multimedia build
    QAudioOutput = None
    QMediaPlayer = None


def _format_time(value: float) -> str:
    return f"{float(value or 0.0):.3f}"


def _readonly_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class ManualTimingDialog(QDialog):
    HEADERS = ("#", "Start", "End", "Gap", "Text")

    def __init__(self, srt_file: str, audio_file: str, session_dir: str, parent=None):
        super().__init__(parent)
        self.srt_file = os.path.abspath(srt_file)
        self.audio_file = os.path.abspath(audio_file) if audio_file else ""
        self.session_dir = os.path.abspath(session_dir)
        self.saved_srt_path: str | None = None
        self.saved_json_path: str | None = None
        self._updating_table = False
        self._updating_controls = False
        self._playback_stop_ms = 0

        segments = load_srt_timing_segments(self.srt_file)
        persistence = BoundaryEditorPersistence(
            save_folder=self.session_dir,
            json_path=str(Path(self.srt_file).with_suffix(".json")),
            default_srt_output_path=self.srt_file,
        )
        self.controller = BoundaryEditorController(BoundaryEditorModel(segments, corrections=[]), persistence)

        self.setWindowTitle("Subtitle Timing Editor")
        self.resize(1120, 760)

        self._player = None
        self._audio_output = None
        self._playback_timer = QTimer(self)
        self._playback_timer.setInterval(50)
        self._playback_timer.timeout.connect(self._on_playback_tick)
        self._build_player()
        self._build_ui()
        self._populate_table()
        if self.controller.segments:
            self.table.selectRow(0)
            self._sync_controls_to_selected_row()

    def _build_player(self) -> None:
        if not self.audio_file or not os.path.exists(self.audio_file):
            return
        if QMediaPlayer is None or QAudioOutput is None:
            return
        self._audio_output = QAudioOutput(self)
        self._audio_output.setVolume(0.8)
        self._player = QMediaPlayer(self)
        self._player.setAudioOutput(self._audio_output)
        self._player.setSource(QUrl.fromLocalFile(self.audio_file))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel(f"SRT: {self.srt_file}", self), 1)
        header_layout.addWidget(QLabel(f"Audio: {self.audio_file or 'None'}", self), 1)
        layout.addLayout(header_layout)

        toolbar = QHBoxLayout()
        self.play_button = QPushButton("Play", self)
        self.play_button.setEnabled(self._player is not None)
        self.play_button.clicked.connect(self._play_selected_segment)
        toolbar.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(self._player is not None)
        self.stop_button.clicked.connect(self._stop_playback)
        toolbar.addWidget(self.stop_button)

        self.show_modified_only_check = QCheckBox("Show modified only", self)
        self.show_modified_only_check.stateChanged.connect(lambda _state: self._apply_row_filter())
        toolbar.addWidget(self.show_modified_only_check)
        toolbar.addStretch(1)
        layout.addLayout(toolbar)

        self.table = QTableWidget(0, len(self.HEADERS), self)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.itemChanged.connect(self._on_item_changed)
        self.table.itemSelectionChanged.connect(self._sync_controls_to_selected_row)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setColumnWidth(0, 52)
        self.table.setColumnWidth(1, 90)
        self.table.setColumnWidth(2, 90)
        self.table.setColumnWidth(3, 90)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        controls = QWidget(self)
        controls_layout = QHBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)

        timing_form = QFormLayout()
        self.start_spin = self._make_time_spin()
        self.end_spin = self._make_time_spin()
        self.allow_adjacent_shift_check = QCheckBox("Shift adjacent subtitle", self)
        self.allow_adjacent_shift_check.setChecked(True)
        self.start_spin.valueChanged.connect(lambda value: self._update_selected_boundary("start", value))
        self.end_spin.valueChanged.connect(lambda value: self._update_selected_boundary("end", value))
        timing_form.addRow("Start:", self.start_spin)
        timing_form.addRow("End:", self.end_spin)
        timing_form.addRow("", self.allow_adjacent_shift_check)
        controls_layout.addLayout(timing_form, 1)

        split_form = QFormLayout()
        self.split_word_spin = QSpinBox(self)
        self.split_word_spin.setRange(1, 1)
        self.split_time_spin = self._make_time_spin()
        self.split_button = QPushButton("Split", self)
        self.split_button.clicked.connect(self._split_selected_segment)
        split_form.addRow("Split after word:", self.split_word_spin)
        split_form.addRow("Split at:", self.split_time_spin)
        split_form.addRow("", self.split_button)
        controls_layout.addLayout(split_form, 1)

        merge_layout = QVBoxLayout()
        self.merge_up_button = QPushButton("Merge Up", self)
        self.merge_down_button = QPushButton("Merge Down", self)
        self.merge_up_button.clicked.connect(lambda: self._merge_selected("up"))
        self.merge_down_button.clicked.connect(lambda: self._merge_selected("down"))
        merge_layout.addWidget(self.merge_up_button)
        merge_layout.addWidget(self.merge_down_button)
        merge_layout.addStretch(1)
        controls_layout.addLayout(merge_layout)

        layout.addWidget(controls)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        self.buttons.accepted.connect(self._save_and_accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @staticmethod
    def _make_time_spin() -> QDoubleSpinBox:
        spin = QDoubleSpinBox()
        spin.setRange(0.0, 999999.0)
        spin.setDecimals(3)
        spin.setSingleStep(0.050)
        spin.setSuffix(" s")
        return spin

    def _populate_table(self, selected_segment_index: int | None = None) -> None:
        self._updating_table = True
        try:
            self.table.setRowCount(len(self.controller.segments))
            modified_color = QColor("#fff2b8")
            for row, segment in enumerate(self.controller.segments):
                row_view = self.controller.get_row_view(row) or {}
                was_modified = bool(row_view.get("was_modified"))

                number_item = _readonly_item(f"{row + 1}")
                number_item.setData(Qt.ItemDataRole.UserRole, row)
                start_item = _readonly_item(_format_time(float(segment.get("start") or 0.0)))
                end_item = _readonly_item(_format_time(float(segment.get("end") or 0.0)))
                gap_item = _readonly_item(str(row_view.get("gap_text") or ""))
                text_item = QTableWidgetItem(str(segment.get("text") or ""))
                text_item.setData(Qt.ItemDataRole.UserRole, row)

                for column, item in enumerate((number_item, start_item, end_item, gap_item, text_item)):
                    if was_modified:
                        item.setBackground(modified_color)
                    self.table.setItem(row, column, item)
        finally:
            self._updating_table = False

        self._apply_row_filter()
        if selected_segment_index is not None and 0 <= selected_segment_index < self.table.rowCount():
            self.table.selectRow(selected_segment_index)

    def _apply_row_filter(self) -> None:
        modified_only = self.show_modified_only_check.isChecked()
        for row in range(self.table.rowCount()):
            row_view = self.controller.get_row_view(row) or {}
            self.table.setRowHidden(row, modified_only and not bool(row_view.get("was_modified")))

    def _selected_segment_index(self) -> int | None:
        selected_items = self.table.selectedItems()
        if not selected_items:
            return None
        item = self.table.item(selected_items[0].row(), 0)
        if item is None:
            return None
        value = item.data(Qt.ItemDataRole.UserRole)
        return int(value) if isinstance(value, int) else None

    def _sync_controls_to_selected_row(self) -> None:
        segment_index = self._selected_segment_index()
        has_selection = segment_index is not None and 0 <= segment_index < len(self.controller.segments)
        for widget in (
            self.start_spin,
            self.end_spin,
            self.split_word_spin,
            self.split_time_spin,
            self.split_button,
            self.merge_up_button,
            self.merge_down_button,
        ):
            widget.setEnabled(has_selection)

        if not has_selection or segment_index is None:
            return

        segment = self.controller.segments[segment_index]
        start = float(segment.get("start") or 0.0)
        end = float(segment.get("end") or start)
        words = str(segment.get("text") or "").strip().split()

        self._updating_controls = True
        try:
            self.start_spin.setValue(start)
            self.end_spin.setValue(end)
            self.split_time_spin.setValue(start + max(0.0, end - start) / 2.0)
            if len(words) >= 2:
                self.split_word_spin.setRange(1, len(words) - 1)
                self.split_word_spin.setValue(max(1, min(len(words) - 1, len(words) // 2)))
                self.split_button.setEnabled(True)
            else:
                self.split_word_spin.setRange(1, 1)
                self.split_word_spin.setValue(1)
                self.split_button.setEnabled(False)
            self.merge_up_button.setEnabled(segment_index > 0)
            self.merge_down_button.setEnabled(segment_index < len(self.controller.segments) - 1)
        finally:
            self._updating_controls = False

    def _on_item_changed(self, item: QTableWidgetItem) -> None:
        if self._updating_table or item.column() != 4:
            return
        segment_index = item.data(Qt.ItemDataRole.UserRole)
        if not isinstance(segment_index, int):
            return
        if self.controller.update_text(segment_index, item.text()):
            self._populate_table(segment_index)

    def _update_selected_boundary(self, boundary_type: str, value: float) -> None:
        if self._updating_controls:
            return
        segment_index = self._selected_segment_index()
        if segment_index is None:
            return
        if self.controller.update_boundary(
            segment_index,
            boundary_type,
            float(value),
            allow_adjacent_shift=self.allow_adjacent_shift_check.isChecked(),
        ):
            self._populate_table(segment_index)
            self._sync_controls_to_selected_row()

    def _split_selected_segment(self) -> None:
        segment_index = self._selected_segment_index()
        if segment_index is None:
            return
        if not self.controller.split_subtitle(
            segment_index,
            self.split_word_spin.value() - 1,
            self.split_time_spin.value(),
        ):
            QMessageBox.warning(self, "Split Subtitle", "The selected subtitle cannot be split at that point.")
            return
        self._populate_table(segment_index)
        self._sync_controls_to_selected_row()

    def _merge_selected(self, direction: str) -> None:
        segment_index = self._selected_segment_index()
        if segment_index is None:
            return
        if direction == "up":
            success = self.controller.merge_up(segment_index)
            selected_index = max(0, segment_index - 1)
        else:
            success = self.controller.merge_down(segment_index)
            selected_index = segment_index
        if not success:
            return
        self._populate_table(selected_index)
        self._sync_controls_to_selected_row()

    def _play_selected_segment(self) -> None:
        if self._player is None:
            QMessageBox.warning(self, "Audio Playback", "Audio playback is unavailable in this Qt build.")
            return
        segment_index = self._selected_segment_index()
        if segment_index is None:
            return
        segment = self.controller.segments[segment_index]
        start_ms = max(0, int(round((float(segment.get("start") or 0.0) - 0.15) * 1000)))
        end_ms = max(start_ms + 50, int(round((float(segment.get("end") or 0.0) + 0.15) * 1000)))
        self._playback_stop_ms = end_ms
        self._player.setPosition(start_ms)
        self._player.play()
        self._playback_timer.start()

    def _on_playback_tick(self) -> None:
        if self._player is None:
            self._playback_timer.stop()
            return
        if self._playback_stop_ms and self._player.position() >= self._playback_stop_ms:
            self._stop_playback()

    def _stop_playback(self) -> None:
        self._playback_timer.stop()
        if self._player is not None:
            self._player.stop()

    def _save_and_accept(self) -> None:
        try:
            self.saved_srt_path = self.controller.save_srt()
            self.saved_json_path = self.controller.save_json()
        except Exception as error:
            QMessageBox.critical(self, "Save Failed", f"Could not save subtitle timing changes: {error}")
            return
        self.accept()

    def reject(self) -> None:
        self._stop_playback()
        super().reject()

    def accept(self) -> None:
        self._stop_playback()
        super().accept()


def open_manual_timing_dialog(srt_file: str, audio_file: str, session_dir: str, parent=None) -> str | None:
    srt_path = os.path.abspath(srt_file) if srt_file else ""
    audio_path = os.path.abspath(audio_file) if audio_file else ""
    session_path = os.path.abspath(session_dir) if session_dir else ""
    if not srt_path or not os.path.exists(srt_path):
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None
    if not session_path:
        return None

    os.makedirs(session_path, exist_ok=True)
    dialog = ManualTimingDialog(srt_path, audio_path, session_path, parent=parent)
    result = dialog.exec()
    if result == QDialog.DialogCode.Accepted and dialog.saved_srt_path:
        return dialog.saved_srt_path
    return srt_path
