from __future__ import annotations

from collections.abc import Callable

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)

from ...logic.dubbing.subtitle_comparison import comparison_rows


def _time(ms: int) -> str:
    seconds, millis = divmod(max(0, int(ms)), 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{seconds:02}.{millis:03}"


class SubtitlePreviewDialog(QDialog):
    HEADERS = ("Time", "Source / Transcription", "Correction", "Translation")

    def __init__(
        self,
        paths: dict[str, str],
        parent=None,
        *,
        lineage_paths: dict[str, str] | None = None,
        edit_callback: Callable[[], None] | None = None,
    ):
        super().__init__(parent)
        self.paths = dict(paths or {})
        self.rows = comparison_rows(self.paths, lineage_paths=lineage_paths)
        self.edit_callback = edit_callback
        self.setWindowTitle("Subtitle Preview and Comparison")
        self.resize(1380, 820)

        layout = QVBoxLayout(self)
        summary = QLabel(
            "Compare stage outputs by timing. Merged and split subtitles are grouped by their overlapping time ranges."
        )
        summary.setWordWrap(True)
        summary.setObjectName("secondaryInfoLabel")
        layout.addWidget(summary)

        controls = QHBoxLayout()
        self.changed_only = QCheckBox("Show changed ranges only")
        self.changed_only.toggled.connect(self._apply_filter)
        controls.addWidget(self.changed_only)
        controls.addStretch(1)
        self.edit_button = QPushButton("Edit Current Subtitles")
        self.edit_button.setEnabled(edit_callback is not None)
        self.edit_button.clicked.connect(self._edit)
        controls.addWidget(self.edit_button)
        layout.addLayout(controls)

        self.table = QTableWidget(len(self.rows), len(self.HEADERS))
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setAlternatingRowColors(True)
        self.table.setWordWrap(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 165)
        for row_index, row in enumerate(self.rows):
            values = (
                f"{_time(row['start_ms'])} – {_time(row['end_ms'])}",
                row.get("source", ""),
                row.get("corrected", ""),
                row.get("translated", ""),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(Qt.ItemDataRole.UserRole, bool(row.get("changed")))
                self.table.setItem(row_index, column, item)
        self.table.resizeRowsToContents()
        layout.addWidget(self.table, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        layout.addWidget(buttons)

    def _apply_filter(self, enabled: bool):
        for row in range(self.table.rowCount()):
            item = self.table.item(row, 0)
            changed = bool(item.data(Qt.ItemDataRole.UserRole)) if item else False
            self.table.setRowHidden(row, bool(enabled and not changed))

    def _edit(self):
        if self.edit_callback:
            self.accept()
            self.edit_callback()
