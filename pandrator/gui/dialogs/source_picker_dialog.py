import os

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
)


class SourcePickerDialog(QDialog):
    HEADERS = ["Name", "Type", "Last Used", "Session", "Path"]

    def __init__(self, source_rows: list[dict], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Select Existing Source")
        self.resize(980, 520)

        self._all_rows = list(source_rows or [])
        self._filtered_rows: list[dict] = []
        self._selected_source_path = ""

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        controls_row = QHBoxLayout()
        controls_row.addWidget(QLabel("Search:"))
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter by source name, type, session, or path...")
        controls_row.addWidget(self.search_edit, 1)
        layout.addLayout(controls_row)

        self.table = QTableWidget(0, len(self.HEADERS), self)
        self.table.setHorizontalHeaderLabels(self.HEADERS)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.horizontalHeader().setStretchLastSection(True)
        layout.addWidget(self.table, 1)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("secondaryInfoLabel")
        layout.addWidget(self.summary_label)

        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Use Selected Source")
        self.buttons.accepted.connect(self._on_accept_clicked)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

        self.search_edit.textChanged.connect(self._refresh_table)
        self.table.itemSelectionChanged.connect(self._on_selection_changed)
        self.table.itemDoubleClicked.connect(lambda _item: self._on_accept_clicked())

        self._refresh_table()

    @staticmethod
    def _readonly_item(text: str) -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        return item

    def selected_source_path(self) -> str:
        return str(self._selected_source_path or "")

    def _refresh_table(self):
        query = str(self.search_edit.text() or "").strip().lower()
        tokens = [token for token in query.split() if token]

        def _matches(row: dict) -> bool:
            if not tokens:
                return True
            haystack = " ".join(
                [
                    str(row.get("name") or ""),
                    str(row.get("source_type") or ""),
                    str(row.get("session_name") or ""),
                    str(row.get("source_path") or ""),
                ]
            ).lower()
            return all(token in haystack for token in tokens)

        self._filtered_rows = [row for row in self._all_rows if _matches(row)]
        self.table.setRowCount(len(self._filtered_rows))

        for row_index, row in enumerate(self._filtered_rows):
            source_path = str(row.get("source_path") or "")
            name_item = self._readonly_item(str(row.get("name") or os.path.basename(source_path)))
            name_item.setData(Qt.ItemDataRole.UserRole, source_path)

            self.table.setItem(row_index, 0, name_item)
            self.table.setItem(row_index, 1, self._readonly_item(str(row.get("source_type") or "")))
            self.table.setItem(row_index, 2, self._readonly_item(str(row.get("last_used_at") or "")))
            self.table.setItem(row_index, 3, self._readonly_item(str(row.get("session_name") or "")))
            self.table.setItem(row_index, 4, self._readonly_item(source_path))

        total_count = len(self._all_rows)
        shown_count = len(self._filtered_rows)
        self.summary_label.setText(f"Showing {shown_count} of {total_count} source file(s).")

        if self._filtered_rows:
            self.table.selectRow(0)
        else:
            self._selected_source_path = ""
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)

    def _on_selection_changed(self):
        selected_rows = self.table.selectionModel().selectedRows()
        if not selected_rows:
            self._selected_source_path = ""
            self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
            return

        row_index = selected_rows[0].row()
        row_item = self.table.item(row_index, 0)
        selected_path = str(row_item.data(Qt.ItemDataRole.UserRole) or "") if row_item else ""

        self._selected_source_path = selected_path
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(bool(selected_path))

    def _on_accept_clicked(self):
        selected_path = self.selected_source_path()
        if not selected_path:
            return

        if not os.path.isfile(selected_path):
            QMessageBox.warning(
                self,
                "Source Missing",
                "The selected source file no longer exists. Please choose another one.",
            )
            return

        self.accept()
