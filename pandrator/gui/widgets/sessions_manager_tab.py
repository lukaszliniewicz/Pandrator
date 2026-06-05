import os
from datetime import datetime

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QHeaderView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


class SessionsManagerTab(QWidget):
    HEADERS = [
        "Name",
        "Workflow",
        "Source File",
        "Progress",
        "Output",
        "Audio Versions",
        "Size",
        "Modified",
    ]

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._last_rows: list[dict] = []

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        top_row = QHBoxLayout()
        self.search_label = QLabel("Search:")
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Session name, source, status, final output...")
        self.include_trashed_checkbox = QCheckBox("Show Trash")
        self.refresh_button = QPushButton("Refresh")
        self.reindex_button = QPushButton("Reindex")
        top_row.addWidget(self.search_label)
        top_row.addWidget(self.search_edit, 1)
        top_row.addWidget(self.include_trashed_checkbox)
        top_row.addWidget(self.refresh_button)
        top_row.addWidget(self.reindex_button)
        layout.addLayout(top_row)

        actions_row = QHBoxLayout()
        self.load_button = QPushButton("Load")
        self.open_folder_button = QPushButton("Open Folder")
        self.move_to_trash_button = QPushButton("Move to Trash")
        self.restore_button = QPushButton("Restore")
        self.delete_permanently_button = QPushButton("Delete Permanently")
        self.empty_expired_trash_button = QPushButton("Empty Expired Trash")
        actions_row.addWidget(self.load_button)
        actions_row.addWidget(self.open_folder_button)
        actions_row.addWidget(self.move_to_trash_button)
        actions_row.addWidget(self.restore_button)
        actions_row.addWidget(self.delete_permanently_button)
        actions_row.addWidget(self.empty_expired_trash_button)
        actions_row.addStretch(1)
        layout.addLayout(actions_row)

        self.sessions_table = QTableWidget(0, len(self.HEADERS))
        self.sessions_table.setHorizontalHeaderLabels(self.HEADERS)
        self.sessions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.sessions_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.sessions_table.verticalHeader().setVisible(False)
        self.sessions_table.setAlternatingRowColors(True)
        self.sessions_table.setSortingEnabled(False)
        self.sessions_table.horizontalHeader().setStretchLastSection(True)
        self.sessions_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.sessions_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.sessions_table, 1)

        self.preview_box = QPlainTextEdit()
        self.preview_box.setReadOnly(True)
        self.preview_box.setPlaceholderText("Select a session to preview source, audio versions, runs, and trash state.")
        self.preview_box.setMaximumBlockCount(4000)
        layout.addWidget(self.preview_box, 1)

        self._connect_signals()
        self.refresh_sessions()

    def _connect_signals(self):
        self.search_edit.returnPressed.connect(self.refresh_sessions)
        self.include_trashed_checkbox.toggled.connect(self.refresh_sessions)
        self.refresh_button.clicked.connect(self.refresh_sessions)
        self.reindex_button.clicked.connect(self._on_reindex)
        self.sessions_table.itemSelectionChanged.connect(self._on_selection_changed)
        self.load_button.clicked.connect(self._on_load)
        self.open_folder_button.clicked.connect(self._on_open_folder)
        self.move_to_trash_button.clicked.connect(self._on_move_to_trash)
        self.restore_button.clicked.connect(self._on_restore)
        self.delete_permanently_button.clicked.connect(self._on_delete_permanently)
        self.empty_expired_trash_button.clicked.connect(self._on_empty_expired_trash)

    @staticmethod
    def _bytes_to_text(size_bytes: int) -> str:
        size = float(max(0, int(size_bytes)))
        units = ["B", "KB", "MB", "GB", "TB"]
        unit_index = 0
        while size >= 1024.0 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1
        return f"{size:.1f} {units[unit_index]}"

    @staticmethod
    def _readonly_item(text: str, tooltip: str = "") -> QTableWidgetItem:
        item = QTableWidgetItem(text)
        item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
        if tooltip:
            item.setToolTip(tooltip)
        return item

    @staticmethod
    def _is_trashed(row: dict) -> bool:
        return bool(str(row.get("trashed_at") or "").strip())

    @staticmethod
    def _source_file_label(row: dict) -> str:
        display_name = str(row.get("source_display_name") or "").strip()
        if display_name:
            return display_name

        for key in ("source_display_path", "source_path"):
            value = str(row.get(key) or "").strip()
            if value:
                return os.path.basename(value.rstrip("/\\")) or value
        return "(none)"

    @staticmethod
    def _source_tooltip(row: dict) -> str:
        source_type = str(row.get("source_type") or "").strip()
        display_path = str(row.get("source_display_path") or "").strip()
        source_path = str(row.get("source_path") or "").strip()
        parts = []
        if source_type:
            parts.append(f"Type: {source_type}")
        if display_path:
            parts.append(f"Source: {display_path}")
        if source_path and source_path != display_path:
            parts.append(f"Internal source: {source_path}")
        return "\n".join(parts)

    @staticmethod
    def _output_status_label(row: dict) -> str:
        final_status = str(row.get("final_output_status") or "").strip()
        dubbing_status = str(row.get("dubbing_status") or "").strip()
        if bool(row.get("dubbing_mode")):
            if final_status and final_status != "missing":
                return f"Final {final_status}"
            return dubbing_status or "Dubbing"
        return final_status or str(row.get("status") or "").strip() or "Idle"

    @staticmethod
    def _audio_versions_label(row: dict) -> str:
        summary = str(row.get("audio_version_summary") or "").strip()
        if summary:
            return summary
        count = int(row.get("audio_version_count") or 0)
        return "No audio" if count <= 0 else f"{count} version{'s' if count != 1 else ''}"

    @staticmethod
    def _format_timestamp(value: str) -> str:
        timestamp = str(value or "").strip()
        if not timestamp:
            return ""

        try:
            normalized = timestamp.replace("Z", "+00:00")
            parsed = datetime.fromisoformat(normalized)
            return parsed.strftime("%Y-%m-%d, %H:%M")
        except ValueError:
            return timestamp

    @staticmethod
    def _session_type_label(row: dict) -> str:
        is_dubbing = bool(row.get("dubbing_mode"))
        if not is_dubbing:
            return "TTS"

        dubbing_status = str(row.get("dubbing_status") or "").strip().lower()
        if "legacy" in dubbing_status:
            return "Dubbing (Legacy)"
        return "Dubbing"

    def _build_preview_text(self, preview_payload: dict) -> str:
        session = preview_payload.get("session", {}) if isinstance(preview_payload, dict) else {}
        payload_index = preview_payload.get("payload_index", {}) if isinstance(preview_payload, dict) else {}
        runs = preview_payload.get("runs", []) if isinstance(preview_payload, dict) else []
        audio_versions = preview_payload.get("audio_versions", []) if isinstance(preview_payload, dict) else []
        trash_entry = preview_payload.get("trash_entry", {}) if isinstance(preview_payload, dict) else {}

        session_name = str(session.get("session_name") or "")
        session_type = self._session_type_label(session if isinstance(session, dict) else {})
        source_type = str(session.get("source_type") or "")
        source_path = str(session.get("source_path") or "")
        source_display_path = str(session.get("source_display_path") or "").strip()
        source_display_name = str(session.get("source_display_name") or "").strip() or self._source_file_label(session)
        session_path = str(session.get("session_path") or "")
        status = str(session.get("status") or "")
        final_output_status = str(session.get("final_output_status") or "")
        dubbing_status = str(session.get("dubbing_status") or "")
        progress_percent = float(session.get("progress_percent") or 0.0)
        generated_sentences = int(session.get("generated_sentences") or payload_index.get("generated_sentences") or 0)
        total_sentences = int(session.get("total_sentences") or payload_index.get("sentences_count") or 0)
        size_text = self._bytes_to_text(int(session.get("session_size_bytes") or 0))
        audio_size_text = self._bytes_to_text(int(session.get("audio_version_size_bytes") or 0))
        audio_summary = str(session.get("audio_version_summary") or "").strip() or self._audio_versions_label(session)
        modified_raw = str(session.get("config_modified_at") or session.get("indexed_at") or "")
        modified = self._format_timestamp(modified_raw)
        trashed_at = self._format_timestamp(str(session.get("trashed_at") or ""))
        expires_at = self._format_timestamp(str(trash_entry.get("expires_at") or ""))
        trash_path = str(session.get("trash_path") or trash_entry.get("trash_path") or "")

        lines = [
            f"Name: {session_name}",
            f"Workflow: {session_type}",
            f"Status: {status}",
            f"Source File: {source_display_name}",
            f"Source Type: {source_type or '(unknown)'}",
            f"Source Path: {source_display_path or source_path or '(none)'}",
            f"Session Path: {session_path or '(none)'}",
            f"Progress: {progress_percent:.1f}% ({generated_sentences}/{total_sentences} sentences)",
            f"Final Output: {final_output_status or '(unknown)'}",
            f"Dubbing: {dubbing_status or '(none)'}",
            f"Size: {size_text}",
            f"Audio Versions: {audio_summary} ({audio_size_text})",
            f"Modified: {modified or '(unknown)'}",
        ]

        if source_path and source_display_path and source_path != source_display_path:
            lines.append(f"Internal Source: {source_path}")

        if trashed_at:
            lines.append(f"Trashed At: {trashed_at}")
        if trash_path:
            lines.append(f"Trash Path: {trash_path}")
        if expires_at:
            lines.append(f"Trash Expires: {expires_at}")

        lines.append("")
        lines.append("Audio Versions:")
        if audio_versions:
            for version in audio_versions:
                label = str(version.get("label") or version.get("variant_id") or "")
                kind = str(version.get("kind") or "")
                sentence_count = int(version.get("sentence_count") or 0)
                version_total = int(version.get("total_sentences") or 0)
                size = self._bytes_to_text(int(version.get("size_bytes") or 0))
                partial = bool(int(version.get("partial") or 0))
                model_name = str(version.get("model_name") or "").strip()
                suffix = " partial" if partial else ""
                model_suffix = f" | model={model_name}" if model_name else ""
                count_text = f"{sentence_count}/{version_total}" if version_total else str(sentence_count)
                lines.append(f"- {label} | {kind or 'audio'} | {count_text} sentences | {size}{suffix}{model_suffix}")
        else:
            lines.append("No indexed audio versions.")

        lines.append("")
        lines.append(f"Dubbing Runs: {len(runs)}")

        if not runs:
            lines.append("No runs indexed.")
            lines.append("")
            lines.append("Session config JSON is hidden here to keep this view focused.")
            return "\n".join(lines)

        for run in runs:
            run_id = str(run.get("run_id") or "")
            run_status = str(run.get("status") or "")
            run_active = bool(run.get("active"))
            run_legacy = bool(run.get("legacy"))
            run_updated = self._format_timestamp(str(run.get("updated_at") or ""))
            run_dir = str(run.get("run_dir") or "")
            steps = run.get("steps", []) if isinstance(run.get("steps"), list) else []
            artifacts = run.get("artifacts", []) if isinstance(run.get("artifacts"), list) else []

            completed_steps = sum(
                1 for step in steps if str(step.get("status") or "").strip().lower() == "completed"
            )
            total_steps = len(steps)
            current_artifacts = [item for item in artifacts if bool(item.get("is_current"))]

            lines.append(
                f"- {run_id} | status={run_status or 'unknown'} | active={'yes' if run_active else 'no'}"
                f" | legacy={'yes' if run_legacy else 'no'} | updated={run_updated or 'n/a'}"
            )
            lines.append(f"  dir: {run_dir or '(none)'}")
            lines.append(f"  steps: {completed_steps}/{total_steps} completed")

            if current_artifacts:
                lines.append("  current artifacts:")
                for artifact in current_artifacts:
                    role = str(artifact.get("role") or "")
                    artifact_path = str(artifact.get("path") or "")
                    lines.append(f"    - {role}: {os.path.basename(artifact_path) if artifact_path else '(none)'}")
            else:
                lines.append("  current artifacts: none")

        lines.append("")
        lines.append("Session config JSON is hidden here to keep this view focused.")
        return "\n".join(lines)

    def _selected_records(self) -> list[dict]:
        selected_indexes = self.sessions_table.selectionModel().selectedRows()
        if not selected_indexes:
            return []
        records: list[dict] = []
        for index in sorted(selected_indexes, key=lambda item: item.row()):
            row = index.row()
            if 0 <= row < len(self._last_rows):
                records.append(self._last_rows[row])
        return records

    def _selected_record(self) -> dict | None:
        records = self._selected_records()
        return records[0] if records else None

    def _build_multi_preview_text(self, records: list[dict]) -> str:
        trashed_count = sum(1 for record in records if self._is_trashed(record))
        live_count = len(records) - trashed_count
        total_size = sum(int(record.get("session_size_bytes") or 0) for record in records)
        audio_size = sum(int(record.get("audio_version_size_bytes") or 0) for record in records)
        names = [str(record.get("session_name") or "(unnamed)") for record in records[:20]]

        lines = [
            f"{len(records)} sessions selected",
            f"Live: {live_count}",
            f"Trash: {trashed_count}",
            f"Total Size: {self._bytes_to_text(total_size)}",
            f"Audio Version Size: {self._bytes_to_text(audio_size)}",
            "",
            "Selected Sessions:",
        ]
        lines.extend(f"- {name}" for name in names)
        if len(records) > len(names):
            lines.append(f"- ...and {len(records) - len(names)} more")
        return "\n".join(lines)

    def refresh_sessions(self):
        search_query = self.search_edit.text().strip()
        include_trashed = bool(self.include_trashed_checkbox.isChecked())

        rows = self.logic.list_indexed_sessions(
            search_query=search_query,
            include_trashed=include_trashed,
        )
        self._last_rows = rows

        self.sessions_table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            session_name = str(row.get("session_name") or "")
            session_type = self._session_type_label(row)
            source_file = self._source_file_label(row)
            source_tooltip = self._source_tooltip(row)
            progress_value = float(row.get("progress_percent") or 0.0)
            output_status = self._output_status_label(row)
            audio_versions = self._audio_versions_label(row)
            audio_tooltip = f"Audio version files: {self._bytes_to_text(int(row.get('audio_version_size_bytes') or 0))}"
            size_text = self._bytes_to_text(int(row.get("session_size_bytes") or 0))
            size_tooltip = f"Total session folder size: {size_text}"
            modified = self._format_timestamp(str(row.get("config_modified_at") or row.get("indexed_at") or ""))

            self.sessions_table.setItem(row_index, 0, self._readonly_item(session_name))
            self.sessions_table.setItem(row_index, 1, self._readonly_item(session_type))
            self.sessions_table.setItem(row_index, 2, self._readonly_item(source_file, source_tooltip))
            self.sessions_table.setItem(row_index, 3, self._readonly_item(f"{progress_value:.1f}%"))
            self.sessions_table.setItem(row_index, 4, self._readonly_item(output_status))
            self.sessions_table.setItem(row_index, 5, self._readonly_item(audio_versions, audio_tooltip))
            self.sessions_table.setItem(row_index, 6, self._readonly_item(size_text, size_tooltip))
            self.sessions_table.setItem(row_index, 7, self._readonly_item(modified))

        if rows:
            self.sessions_table.selectRow(0)
        else:
            self.preview_box.clear()

        self._update_action_states()

    def _update_action_states(self):
        records = self._selected_records()
        if not records:
            self.load_button.setEnabled(False)
            self.open_folder_button.setEnabled(False)
            self.move_to_trash_button.setEnabled(False)
            self.restore_button.setEnabled(False)
            self.delete_permanently_button.setEnabled(False)
            return

        trashed_records = [record for record in records if self._is_trashed(record)]
        live_records = [record for record in records if not self._is_trashed(record)]
        single_live = len(records) == 1 and bool(live_records)
        single_any = len(records) == 1
        all_live = bool(records) and len(live_records) == len(records)
        all_trashed = bool(records) and len(trashed_records) == len(records)

        self.load_button.setEnabled(single_live)
        self.open_folder_button.setEnabled(single_any)
        self.move_to_trash_button.setEnabled(all_live)
        self.restore_button.setEnabled(all_trashed)
        self.delete_permanently_button.setEnabled(all_trashed)

    def _on_selection_changed(self):
        records = self._selected_records()
        self._update_action_states()
        if not records:
            self.preview_box.clear()
            return
        if len(records) > 1:
            self.preview_box.setPlainText(self._build_multi_preview_text(records))
            return

        record = records[0]
        session_name = str(record.get("session_name") or "")
        preview_payload = self.logic.get_session_index_preview(session_name)
        if not preview_payload:
            self.preview_box.setPlainText(f"No preview available for '{session_name}'.")
            return

        self.preview_box.setPlainText(self._build_preview_text(preview_payload))

    def _on_reindex(self):
        records = [record for record in self._selected_records() if not self._is_trashed(record)]
        if records:
            for record in records:
                self.logic.reindex_sessions(str(record.get("session_name") or ""))
        else:
            self.logic.reindex_sessions()
        self.refresh_sessions()

    def _on_load(self):
        record = self._selected_record()
        if record is None:
            return

        if str(record.get("trashed_at") or "").strip():
            QMessageBox.information(self, "Session In Trash", "Restore this session before loading.")
            return

        session_name = str(record.get("session_name") or "")
        if not session_name:
            return
        self.logic.load_session(session_name)

    def _on_open_folder(self):
        record = self._selected_record()
        if record is None:
            return

        candidate_paths = [
            str(record.get("trash_path") or "").strip(),
            str(record.get("session_path") or "").strip(),
        ]
        for candidate_path in candidate_paths:
            if candidate_path and os.path.exists(candidate_path):
                self.logic.open_folder_path(candidate_path)
                return

        QMessageBox.warning(self, "Open Folder Failed", "No valid folder path was found for this session.")

    def _on_move_to_trash(self):
        records = [record for record in self._selected_records() if not self._is_trashed(record)]
        if not records:
            return

        names = [str(record.get("session_name") or "") for record in records if str(record.get("session_name") or "")]
        if not names:
            return
        name_text = names[0] if len(names) == 1 else f"{len(names)} sessions"

        reply = QMessageBox.question(
            self,
            "Move Session To Trash",
            f"Move {name_text} to trash?\n\nThey can be restored later.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        failures: list[str] = []
        for session_name in names:
            moved, details = self.logic.move_session_to_trash(session_name)
            if not moved:
                failures.append(f"{session_name}: {details}")
        if failures:
            QMessageBox.warning(self, "Move To Trash Failed", "\n".join(failures))
        self.refresh_sessions()

    def _on_restore(self):
        records = [record for record in self._selected_records() if self._is_trashed(record)]
        if not records:
            return

        failures: list[str] = []
        for record in records:
            session_name = str(record.get("session_name") or "")
            trash_path = str(record.get("trash_path") or "")
            if not session_name:
                continue
            restored, details = self.logic.restore_session_from_trash(session_name, trash_path=trash_path)
            if not restored:
                failures.append(f"{session_name}: {details}")

        if failures:
            QMessageBox.warning(self, "Restore Failed", "\n".join(failures))
        self.refresh_sessions()

    def _on_delete_permanently(self):
        records = [record for record in self._selected_records() if self._is_trashed(record)]
        if not records:
            return

        names = [str(record.get("session_name") or "(unnamed)") for record in records]
        total_size = sum(int(record.get("session_size_bytes") or 0) for record in records)
        reply = QMessageBox.question(
            self,
            "Delete Permanently",
            (
                f"Permanently delete {len(records)} trashed session"
                f"{'s' if len(records) != 1 else ''}?\n\n"
                f"This removes {self._bytes_to_text(total_size)} from trash and cannot be undone."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return

        failures: list[str] = []
        for record in records:
            session_name = str(record.get("session_name") or "")
            trash_path = str(record.get("trash_path") or "")
            deleted, details = self.logic.permanently_delete_trashed_session(
                session_name=session_name,
                trash_path=trash_path,
            )
            if not deleted:
                failures.append(f"{session_name or trash_path}: {details}")

        if failures:
            QMessageBox.warning(self, "Permanent Delete Failed", "\n".join(failures))
        self.refresh_sessions()

    def _on_empty_expired_trash(self):
        removed_count, removed_paths = self.logic.empty_expired_trash()
        if removed_count > 0:
            QMessageBox.information(
                self,
                "Expired Trash Removed",
                f"Removed {removed_count} expired entr{'y' if removed_count == 1 else 'ies'}."
            )
        else:
            QMessageBox.information(self, "Expired Trash", "No expired trash entries were found.")
        self.refresh_sessions()
