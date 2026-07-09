import logging
import os
from collections import OrderedDict
from collections.abc import Callable
from pathlib import Path

from PyQt6.QtCore import QProcess, QTemporaryDir, Qt
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
    build_segment_preview_command,
    load_srt_timing_segments,
)

def _format_time(value: float) -> str:
    return f"{float(value or 0.0):.3f}"


def _readonly_item(text: str) -> QTableWidgetItem:
    item = QTableWidgetItem(text)
    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
    return item


class ManualTimingDialog(QDialog):
    HEADERS = ("#", "Start", "End", "Gap", "Text")
    PREVIEW_CACHE_LIMIT = 24

    def __init__(
        self,
        srt_file: str,
        audio_file: str,
        session_dir: str,
        parent=None,
        *,
        play_audio_callback: Callable[[str], bool] | None = None,
        stop_audio_callback: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self.srt_file = os.path.abspath(srt_file)
        self.audio_file = os.path.abspath(audio_file) if audio_file else ""
        self.session_dir = os.path.abspath(session_dir)
        self.saved_srt_path: str | None = None
        self.saved_json_path: str | None = None
        self._updating_table = False
        self._updating_controls = False
        self._play_audio_callback = play_audio_callback
        self._stop_audio_callback = stop_audio_callback
        self._preview_process: QProcess | None = None
        self._pending_preview_key: tuple[int, int] | None = None
        self._pending_preview_path = ""
        self._preview_cache: OrderedDict[tuple[int, int], str] = OrderedDict()
        self._preview_dir = QTemporaryDir()
        self._playback_cleaned_up = False

        segments = load_srt_timing_segments(self.srt_file)
        persistence = BoundaryEditorPersistence(
            save_folder=self.session_dir,
            json_path=str(Path(self.srt_file).with_suffix(".json")),
            default_srt_output_path=self.srt_file,
        )
        self.controller = BoundaryEditorController(BoundaryEditorModel(segments, corrections=[]), persistence)

        self.setWindowTitle("Subtitle Timing Editor")
        self.resize(1120, 760)

        self._build_ui()
        self._populate_table()
        if self.controller.segments:
            self.table.selectRow(0)
            self._sync_controls_to_selected_row()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)

        header_layout = QHBoxLayout()
        header_layout.addWidget(QLabel(f"SRT: {self.srt_file}", self), 1)
        header_layout.addWidget(QLabel(f"Audio: {self.audio_file or 'None'}", self), 1)
        layout.addLayout(header_layout)

        toolbar = QHBoxLayout()
        self.play_button = QPushButton("Play", self)
        playback_available = bool(
            self._play_audio_callback
            and self.audio_file
            and os.path.exists(self.audio_file)
            and self._preview_dir.isValid()
        )
        self.play_button.setEnabled(playback_available)
        self.play_button.clicked.connect(self._play_selected_segment)
        toolbar.addWidget(self.play_button)

        self.stop_button = QPushButton("Stop", self)
        self.stop_button.setEnabled(playback_available)
        self.stop_button.clicked.connect(self._stop_playback)
        toolbar.addWidget(self.stop_button)

        self.playback_status_label = QLabel("", self)
        self.playback_status_label.setObjectName("secondaryInfoLabel")
        toolbar.addWidget(self.playback_status_label)

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
        if self._play_audio_callback is None or not self._preview_dir.isValid():
            QMessageBox.warning(self, "Audio Playback", "Audio playback is unavailable.")
            return
        segment_index = self._selected_segment_index()
        if segment_index is None:
            return
        segment = self.controller.segments[segment_index]
        start_ms = max(0, int(round((float(segment.get("start") or 0.0) - 0.15) * 1000)))
        end_ms = max(start_ms + 50, int(round((float(segment.get("end") or 0.0) + 0.15) * 1000)))
        preview_key = (start_ms, end_ms)

        self._cancel_preview_process()
        if self._stop_audio_callback is not None:
            self._stop_audio_callback()

        cached_path = self._preview_cache.get(preview_key)
        if cached_path and os.path.exists(cached_path):
            self._preview_cache.move_to_end(preview_key)
            self._play_preview_file(cached_path)
            return

        preview_path = str(
            Path(self._preview_dir.path())
            / f"segment_{start_ms}_{end_ms}.wav"
        )
        command = build_segment_preview_command(
            self.audio_file,
            preview_path,
            start_ms=start_ms,
            end_ms=end_ms,
        )

        process = QProcess(self)
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        process.finished.connect(self._on_preview_process_finished)
        process.errorOccurred.connect(self._on_preview_process_error)
        self._preview_process = process
        self._pending_preview_key = preview_key
        self._pending_preview_path = preview_path
        self.play_button.setEnabled(False)
        self.play_button.setText("Preparing…")
        self.playback_status_label.setText("Preparing selected segment…")
        process.start(command[0], command[1:])

    def _on_preview_process_finished(
        self,
        exit_code: int,
        exit_status: QProcess.ExitStatus,
    ) -> None:
        process = self.sender()
        if process is not self._preview_process:
            return

        output = bytes(process.readAllStandardOutput()).decode("utf-8", errors="replace").strip()
        preview_key = self._pending_preview_key
        preview_path = self._pending_preview_path
        self._preview_process = None
        self._pending_preview_key = None
        self._pending_preview_path = ""
        process.deleteLater()
        self._reset_preview_controls()

        succeeded = (
            exit_status == QProcess.ExitStatus.NormalExit
            and exit_code == 0
            and preview_key is not None
            and os.path.isfile(preview_path)
            and os.path.getsize(preview_path) > 0
        )
        if not succeeded:
            detail = output.splitlines()[-1] if output else "FFmpeg did not create a playable preview."
            logging.error("Manual timing preview extraction failed: %s", detail)
            self.playback_status_label.setText("Could not prepare audio preview.")
            QMessageBox.warning(self, "Audio Playback", f"Could not prepare the selected segment:\n{detail}")
            return

        self._cache_preview(preview_key, preview_path)
        self._play_preview_file(preview_path)

    def _on_preview_process_error(self, _error: QProcess.ProcessError) -> None:
        process = self.sender()
        if process is not self._preview_process:
            return

        error_text = process.errorString() or "Could not start FFmpeg."
        logging.error("Manual timing preview process error: %s", error_text)
        self._preview_process = None
        self._pending_preview_key = None
        self._pending_preview_path = ""
        process.deleteLater()
        self._reset_preview_controls()
        self.playback_status_label.setText("Audio preview is unavailable.")
        QMessageBox.warning(self, "Audio Playback", f"Could not prepare audio preview:\n{error_text}")

    def _cache_preview(self, preview_key: tuple[int, int], preview_path: str) -> None:
        self._preview_cache[preview_key] = preview_path
        self._preview_cache.move_to_end(preview_key)
        while len(self._preview_cache) > self.PREVIEW_CACHE_LIMIT:
            _, stale_path = self._preview_cache.popitem(last=False)
            try:
                os.remove(stale_path)
            except OSError:
                pass

    def _play_preview_file(self, preview_path: str) -> None:
        try:
            played = bool(self._play_audio_callback and self._play_audio_callback(preview_path))
        except Exception as error:
            logging.error("Manual timing preview playback failed: %s", error, exc_info=True)
            played = False

        if played:
            self.playback_status_label.setText("Playing selected segment.")
            return

        self.playback_status_label.setText("Could not play selected segment.")
        QMessageBox.warning(self, "Audio Playback", "Could not play the selected segment.")

    def _reset_preview_controls(self) -> None:
        self.play_button.setText("Play")
        self.play_button.setEnabled(
            bool(self._play_audio_callback and self._preview_dir.isValid())
        )

    def _cancel_preview_process(self) -> None:
        process = self._preview_process
        if process is None:
            return
        self._preview_process = None
        self._pending_preview_key = None
        self._pending_preview_path = ""
        process.blockSignals(True)
        if process.state() != QProcess.ProcessState.NotRunning:
            process.kill()
            process.waitForFinished(1000)
        process.deleteLater()
        self._reset_preview_controls()

    def _stop_playback(self) -> None:
        self._cancel_preview_process()
        if self._stop_audio_callback is not None:
            self._stop_audio_callback()
        self.playback_status_label.setText("")

    def _cleanup_playback(self) -> None:
        if self._playback_cleaned_up:
            return
        self._playback_cleaned_up = True
        self._stop_playback()
        self._preview_cache.clear()
        self._preview_dir.remove()

    def _save_and_accept(self) -> None:
        try:
            self.saved_srt_path = self.controller.save_srt()
            self.saved_json_path = self.controller.save_json()
        except Exception as error:
            QMessageBox.critical(self, "Save Failed", f"Could not save subtitle timing changes: {error}")
            return
        self.accept()

    def reject(self) -> None:
        self._cleanup_playback()
        super().reject()

    def accept(self) -> None:
        self._cleanup_playback()
        super().accept()


def open_manual_timing_dialog(
    srt_file: str,
    audio_file: str,
    session_dir: str,
    parent=None,
    *,
    play_audio_callback: Callable[[str], bool] | None = None,
    stop_audio_callback: Callable[[], None] | None = None,
) -> str | None:
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
    dialog = ManualTimingDialog(
        srt_path,
        audio_path,
        session_path,
        parent=parent,
        play_audio_callback=play_audio_callback,
        stop_audio_callback=stop_audio_callback,
    )
    result = dialog.exec()
    if result == QDialog.DialogCode.Accepted and dialog.saved_srt_path:
        return dialog.saved_srt_path
    return srt_path
