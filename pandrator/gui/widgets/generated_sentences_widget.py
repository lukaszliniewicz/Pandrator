from functools import partial

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QLabel, QPushButton, QTableWidget,
    QHBoxLayout, QTableWidgetItem, QMenu,
    QAbstractItemView, QMessageBox, QFileDialog, QHeaderView
)
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtCore import Qt, QItemSelectionModel

class GeneratedSentencesWidget(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._table_update_in_progress = False
        self._highlight_color = QColor("#4d5863")
        self._inline_edit_triggers = (
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        
        main_layout = QVBoxLayout(self)
        
        label = QLabel("Generated Sentences")
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        main_layout.addWidget(label)

        main_layout.addWidget(self._create_playback_frame())
        
        main_layout.addWidget(QLabel("All Sentences"))
        self.sentences_list = QTableWidget()
        self.sentences_list.setColumnCount(2)
        self.sentences_list.setHorizontalHeaderLabels(["#", "Sentence"])
        self.sentences_list.verticalHeader().setVisible(False)
        self.sentences_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.sentences_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.sentences_list.setColumnWidth(0, 52)
        self.sentences_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.sentences_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.sentences_list.setEditTriggers(self._inline_edit_triggers)
        self.sentences_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.sentences_list.customContextMenuRequested.connect(
            partial(self._show_context_menu, self.sentences_list)
        )
        self.sentences_list.itemChanged.connect(
            partial(self._on_table_item_changed, self.sentences_list)
        )
        main_layout.addWidget(self.sentences_list)

        main_layout.addWidget(QLabel("Marked for Regeneration"))
        self.marked_list = QTableWidget()
        self.marked_list.setColumnCount(2)
        self.marked_list.setHorizontalHeaderLabels(["#", "Sentence"])
        self.marked_list.verticalHeader().setVisible(False)
        self.marked_list.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Fixed)
        self.marked_list.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.marked_list.setColumnWidth(0, 52)
        self.marked_list.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.marked_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.marked_list.setEditTriggers(self._inline_edit_triggers)
        self.marked_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.marked_list.customContextMenuRequested.connect(
            partial(self._show_context_menu, self.marked_list)
        )
        self.marked_list.itemChanged.connect(
            partial(self._on_table_item_changed, self.marked_list)
        )
        main_layout.addWidget(self.marked_list)
        
        main_layout.addWidget(self._create_actions_frame())
        
        self.logic.state_changed.connect(self.update_ui_from_state)

        self.sentences_list.itemSelectionChanged.connect(self._on_main_list_selection)
        self.marked_list.itemSelectionChanged.connect(self._on_marked_list_selection)
        
        self.update_ui_from_state()

    def _create_playback_frame(self):
        frame = QFrame()
        frame.setObjectName("rowFrame")
        layout = QHBoxLayout(frame)
        self.play_button = QPushButton("Play Selected")
        self.play_as_playlist_button = QPushButton("Play as Playlist")
        self.stop_button = QPushButton("Stop")
        self.save_output_button = QPushButton("Save Output")
        
        self.play_button.clicked.connect(self._on_play)
        self.play_as_playlist_button.clicked.connect(self._on_play_playlist)
        self.stop_button.clicked.connect(self.logic.stop_playback)
        self.save_output_button.clicked.connect(self._on_save_output)

        layout.addWidget(self.play_button)
        layout.addWidget(self.play_as_playlist_button)
        layout.addWidget(self.stop_button)
        layout.addWidget(self.save_output_button)
        return frame

    def _create_actions_frame(self):
        frame = QFrame()
        frame.setObjectName("rowFrame")
        layout = QHBoxLayout(frame)
        self.regenerate_selected_button = QPushButton("Regenerate Selected")
        self.regenerate_marked_button = QPushButton("Regenerate Marked")
        self.regenerate_all_button = QPushButton("Regenerate All")
        self.rvc_selected_button = QPushButton("RVC Selected")
        self.rvc_all_button = QPushButton("RVC All")
        self.remove_button = QPushButton("Remove Selected")
        self.edit_button = QPushButton("Edit Selected")
        
        self.regenerate_selected_button.clicked.connect(self._on_regenerate_selected)
        self.regenerate_marked_button.clicked.connect(self._on_regenerate_marked)
        self.regenerate_all_button.clicked.connect(self._on_regenerate_all)
        self.rvc_selected_button.clicked.connect(self._on_rvc_selected)
        self.rvc_all_button.clicked.connect(self._on_rvc_all)
        self.remove_button.clicked.connect(self._on_remove)
        self.edit_button.clicked.connect(self._on_edit)

        layout.addStretch()
        layout.addWidget(self.regenerate_selected_button)
        layout.addWidget(self.regenerate_marked_button)
        layout.addWidget(self.regenerate_all_button)
        layout.addWidget(self.rvc_selected_button)
        layout.addWidget(self.rvc_all_button)
        layout.addWidget(self.remove_button)
        layout.addWidget(self.edit_button)
        return frame

    def _show_context_menu(self, table: QTableWidget, pos):
        if self.logic.is_generation_or_regeneration_running():
            return

        item = table.itemAt(pos)
        if not item:
            return

        row = item.row()
        num_item = table.item(row, 0)
        if not num_item:
            return

        sentence_number = num_item.data(Qt.ItemDataRole.UserRole)
        if sentence_number is None:
            return

        row_selected = any(selected_item.row() == row for selected_item in table.selectedItems())
        if not row_selected:
            table.selectRow(row)

        selected_rows = sorted(list(set(selected_item.row() for selected_item in table.selectedItems())))
        if not selected_rows:
            selected_rows = [row]
        selected_sentence_numbers = self._sentence_numbers_from_rows(table, selected_rows)
        if not selected_sentence_numbers:
            return

        menu = QMenu()
        regenerate_action = menu.addAction("Regenerate")
        mark_action = menu.addAction("Mark for Regeneration")
        unmark_action = menu.addAction("Unmark")
        rvc_action = menu.addAction("Process with RVC")
        if not self.logic.is_rvc_available():
            rvc_action.setEnabled(False)
        edit_action = menu.addAction("Edit")
        action = menu.exec(table.mapToGlobal(pos))

        if action == regenerate_action:
            self.logic.regenerate_sentences(selected_sentence_numbers)
        elif action == mark_action:
            for selected_number in selected_sentence_numbers:
                self.logic.mark_sentence(selected_number, True)
        elif action == unmark_action:
            for selected_number in selected_sentence_numbers:
                self.logic.mark_sentence(selected_number, False)
        elif action == rvc_action:
            self._process_sentence_numbers_with_rvc(selected_sentence_numbers)
        elif action == edit_action:
            if len(selected_sentence_numbers) > 1:
                QMessageBox.information(self, "Multiple Selection", "Please select only one sentence to edit.")
                return
            self._start_inline_edit(table, row)

    def _start_inline_edit(self, table: QTableWidget, row: int):
        if self.logic.is_generation_or_regeneration_running():
            return

        text_item = table.item(row, 1)
        if not text_item:
            return

        table.setCurrentCell(row, 1)
        table.editItem(text_item)

    def _on_table_item_changed(self, table: QTableWidget, item: QTableWidgetItem):
        if self._table_update_in_progress:
            return

        if item.column() != 1:
            return

        row = item.row()
        num_item = table.item(row, 0)
        if not num_item:
            return

        sentence_number = num_item.data(Qt.ItemDataRole.UserRole)
        if sentence_number is None:
            return

        self.logic.update_sentence_text(sentence_number, item.text())

    def _on_main_list_selection(self):
        if self.sentences_list.selectedItems():
            self.marked_list.blockSignals(True)
            self.marked_list.clearSelection()
            self.marked_list.blockSignals(False)

    def _on_marked_list_selection(self):
        if self.marked_list.selectedItems():
            self.sentences_list.blockSignals(True)
            self.sentences_list.clearSelection()
            self.sentences_list.blockSignals(False)

    def _get_selected_rows_and_table(self) -> tuple[list[int], QTableWidget | None]:
        source_table = None
        if self.sentences_list.selectedItems():
            source_table = self.sentences_list
        elif self.marked_list.selectedItems():
            source_table = self.marked_list
        
        if not source_table:
            return [], None

        selected_rows = sorted(list(set(item.row() for item in source_table.selectedItems())))
        return selected_rows, source_table

    def _sentence_numbers_from_rows(self, table: QTableWidget, rows: list[int]) -> list[str]:
        sentence_numbers: list[str] = []
        for row in rows:
            num_item = table.item(row, 0)
            if not num_item:
                continue

            sentence_number = num_item.data(Qt.ItemDataRole.UserRole)
            if sentence_number is None:
                continue

            sentence_numbers.append(str(sentence_number))

        return sentence_numbers

    def _filter_generated_sentence_numbers(self, sentence_numbers: list[str]) -> list[str]:
        available_numbers = {
            str(sentence.get("sentence_number"))
            for sentence in self.logic.get_processed_sentences_snapshot()
            if sentence.get("sentence_number") is not None and sentence.get("tts_generated") == "yes"
        }

        return [
            str(sentence_number)
            for sentence_number in sentence_numbers
            if str(sentence_number) in available_numbers
        ]

    def _capture_selection_snapshot(self) -> tuple[set[str], str | None]:
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not source_table:
            return set(), None

        source_name = "main" if source_table is self.sentences_list else "marked"
        selected_numbers: set[str] = set()
        for row in selected_rows:
            item = source_table.item(row, 0)
            if item:
                sentence_number = item.data(Qt.ItemDataRole.UserRole)
                if sentence_number is not None:
                    selected_numbers.add(str(sentence_number))

        return selected_numbers, source_name

    def _table_sentence_numbers(self, table: QTableWidget) -> list[str]:
        numbers: list[str] = []
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            numbers.append(str(item.data(Qt.ItemDataRole.UserRole)) if item else "")
        return numbers

    def _table_row_map(self, table: QTableWidget) -> dict[str, int]:
        row_map: dict[str, int] = {}
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if not item:
                continue
            sentence_number = item.data(Qt.ItemDataRole.UserRole)
            if sentence_number is not None:
                row_map[str(sentence_number)] = row
        return row_map

    def _build_sentence_row_data(self, sentence: dict, playing_sentence_number: str | None) -> tuple[str, str, bool, bool]:
        sentence_number = str(sentence.get("sentence_number", "?"))
        sentence_text = sentence.get("processed_sentence", sentence.get("original_sentence", ""))
        tts_generated = sentence.get("tts_generated", "no") == "yes"
        is_playing = playing_sentence_number is not None and sentence_number == str(playing_sentence_number)
        return sentence_number, sentence_text, tts_generated, is_playing

    def _upsert_table_row(
        self,
        table: QTableWidget,
        row: int,
        sentence_number: str,
        sentence_text: str,
        tts_generated: bool,
        is_playing: bool,
    ) -> bool:
        num_item = table.item(row, 0)
        if num_item is None:
            num_item = QTableWidgetItem()
            table.setItem(row, 0, num_item)

        if num_item.text() != sentence_number:
            num_item.setText(sentence_number)
        if num_item.data(Qt.ItemDataRole.UserRole) != sentence_number:
            num_item.setData(Qt.ItemDataRole.UserRole, sentence_number)
        num_item.setTextAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        num_item.setFlags(
            (num_item.flags() | Qt.ItemFlag.ItemIsSelectable | Qt.ItemFlag.ItemIsEnabled)
            & ~Qt.ItemFlag.ItemIsEditable
        )

        text_item = table.item(row, 1)
        if text_item is None:
            text_item = QTableWidgetItem()
            table.setItem(row, 1, text_item)

        text_changed = text_item.text() != sentence_text
        if text_changed:
            text_item.setText(sentence_text)
        text_item.setFlags(text_item.flags() | Qt.ItemFlag.ItemIsEditable)

        if tts_generated:
            num_item.setData(Qt.ItemDataRole.ForegroundRole, None)
            text_item.setData(Qt.ItemDataRole.ForegroundRole, None)
        else:
            gray_color = QColor("gray")
            num_item.setData(Qt.ItemDataRole.ForegroundRole, gray_color)
            text_item.setData(Qt.ItemDataRole.ForegroundRole, gray_color)

        if is_playing:
            num_item.setData(Qt.ItemDataRole.BackgroundRole, self._highlight_color)
            text_item.setData(Qt.ItemDataRole.BackgroundRole, self._highlight_color)
        else:
            num_item.setData(Qt.ItemDataRole.BackgroundRole, None)
            text_item.setData(Qt.ItemDataRole.BackgroundRole, None)

        return text_changed

    def _sync_table_rows(self, table: QTableWidget, row_data: list[tuple[str, str, bool, bool]]):
        desired_numbers = [sentence_number for sentence_number, _, _, _ in row_data]
        current_numbers = self._table_sentence_numbers(table)

        if current_numbers != desired_numbers:
            table.setRowCount(len(row_data))
            for row, (sentence_number, sentence_text, tts_generated, is_playing) in enumerate(row_data):
                self._upsert_table_row(
                    table,
                    row,
                    sentence_number,
                    sentence_text,
                    tts_generated,
                    is_playing,
                )
            table.resizeRowsToContents()
            return

        rows_to_resize: list[int] = []
        for row, (sentence_number, sentence_text, tts_generated, is_playing) in enumerate(row_data):
            text_changed = self._upsert_table_row(
                table,
                row,
                sentence_number,
                sentence_text,
                tts_generated,
                is_playing,
            )
            if text_changed:
                rows_to_resize.append(row)

        for row in rows_to_resize:
            table.resizeRowToContents(row)

    def _restore_selection_snapshot(self, selected_numbers: set[str], source_name: str | None):
        self.sentences_list.clearSelection()
        self.marked_list.clearSelection()

        if not selected_numbers or not source_name:
            return

        target_table = self.sentences_list if source_name == "main" else self.marked_list
        row_map = self._table_row_map(target_table)
        target_rows = sorted([row_map[num] for num in selected_numbers if num in row_map])
        if not target_rows:
            return

        selection_model = target_table.selectionModel()
        if selection_model is None:
            return

        for idx, row in enumerate(target_rows):
            selection_flags = QItemSelectionModel.SelectionFlag.Rows
            if idx == 0:
                selection_flags |= QItemSelectionModel.SelectionFlag.ClearAndSelect
            else:
                selection_flags |= QItemSelectionModel.SelectionFlag.Select

            selection_model.select(target_table.model().index(row, 0), selection_flags)

        target_table.setCurrentCell(target_rows[0], 0)
        target_table.setFocus()

    def _ensure_playing_sentence_visible(self, playing_sentence_number: str | None):
        if playing_sentence_number is None:
            return

        target_number = str(playing_sentence_number)
        for table in (self.sentences_list, self.marked_list):
            row = self._table_row_map(table).get(target_number)
            if row is None:
                continue

            target_item = table.item(row, 1) or table.item(row, 0)
            if target_item:
                table.scrollToItem(
                    target_item,
                    QAbstractItemView.ScrollHint.PositionAtCenter,
                )

    def _apply_lifecycle_action_states(self):
        generation_running = self.logic.is_generation_running()
        regeneration_running = self.logic.is_regeneration_running()
        rvc_processing_running = self.logic.is_rvc_processing_running()
        generation_busy = generation_running or regeneration_running or rvc_processing_running
        rvc_available = self.logic.is_rvc_available()

        self.regenerate_selected_button.setEnabled(not generation_busy)
        self.regenerate_marked_button.setEnabled(not generation_busy)
        self.regenerate_all_button.setEnabled(not generation_busy)
        self.rvc_selected_button.setEnabled((not generation_busy) and rvc_available)
        self.rvc_all_button.setEnabled((not generation_busy) and rvc_available)
        self.remove_button.setEnabled(not generation_busy)
        self.edit_button.setEnabled(not generation_busy)

        self.save_output_button.setEnabled(not generation_busy)

        context_policy = (
            Qt.ContextMenuPolicy.CustomContextMenu
            if not generation_busy
            else Qt.ContextMenuPolicy.NoContextMenu
        )
        edit_policy = (
            self._inline_edit_triggers
            if not generation_busy
            else QAbstractItemView.EditTrigger.NoEditTriggers
        )

        self.sentences_list.setContextMenuPolicy(context_policy)
        self.marked_list.setContextMenuPolicy(context_policy)
        self.sentences_list.setEditTriggers(edit_policy)
        self.marked_list.setEditTriggers(edit_policy)

    def _on_play(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select a sentence to play.")
            return
        
        sentence_number = source_table.item(selected_rows[0], 0).data(Qt.ItemDataRole.UserRole)
        self.logic.play_audio_for_sentence(sentence_number)

    def _on_play_playlist(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        start_sentence_number = None
        if selected_rows:
            start_sentence_number = source_table.item(selected_rows[0], 0).data(Qt.ItemDataRole.UserRole)
        self.logic.play_playlist(start_sentence_number)

    def _on_remove(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            return
        
        reply = QMessageBox.question(self, "Remove Sentences", 
                                     f"Are you sure you want to remove {len(selected_rows)} sentence(s)?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            numbers_to_remove = [source_table.item(row, 0).data(Qt.ItemDataRole.UserRole) for row in selected_rows]
            self.logic.remove_sentences(numbers_to_remove)

    def _on_edit(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            return
        if len(selected_rows) > 1:
            QMessageBox.information(self, "Multiple Selection", "Please select only one sentence to edit.")
            return

        self._start_inline_edit(source_table, selected_rows[0])

    def _on_regenerate_selected(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select sentence(s) to regenerate.")
            return
        
        numbers_to_regenerate = self._sentence_numbers_from_rows(source_table, selected_rows)
        self.logic.regenerate_sentences(numbers_to_regenerate)

    def _on_regenerate_marked(self):
        marked_sentences = [
            s for s in self.logic.get_processed_sentences_snapshot() if s.get('marked')
        ]
        if not marked_sentences:
            QMessageBox.information(self, "No Marked Sentences", "There are no sentences marked for regeneration.")
            return

        numbers_to_regenerate = [s.get('sentence_number') for s in marked_sentences]
        self.logic.regenerate_sentences(numbers_to_regenerate)

    def _on_regenerate_all(self):
        sentences = self.logic.get_processed_sentences_snapshot()
        if not sentences:
            QMessageBox.information(self, "No Sentences", "There are no sentences to regenerate.")
            return

        numbers_to_regenerate = [
            sentence.get("sentence_number")
            for sentence in sentences
            if sentence.get("sentence_number") is not None
        ]
        if not numbers_to_regenerate:
            QMessageBox.information(self, "No Sentences", "There are no sentences to regenerate.")
            return

        self.logic.regenerate_sentences(numbers_to_regenerate)

    def _process_sentence_numbers_with_rvc(self, sentence_numbers: list[str]):
        if not self.logic.is_rvc_available():
            QMessageBox.information(
                self,
                "RVC Not Available",
                "RVC dependencies are not installed. Install RVC support to use this action.",
            )
            return

        generated_numbers = self._filter_generated_sentence_numbers(sentence_numbers)
        if not generated_numbers:
            QMessageBox.information(
                self,
                "No Generated Audio",
                "Selected sentence(s) do not have generated audio yet.",
            )
            return

        self.logic.process_sentences_with_rvc(generated_numbers)

    def _on_rvc_selected(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select sentence(s) to process with RVC.")
            return

        sentence_numbers = self._sentence_numbers_from_rows(source_table, selected_rows)
        self._process_sentence_numbers_with_rvc(sentence_numbers)

    def _on_rvc_all(self):
        sentences = self.logic.get_processed_sentences_snapshot()
        if not sentences:
            QMessageBox.information(self, "No Sentences", "There are no sentences to process with RVC.")
            return

        all_numbers = [
            str(sentence.get("sentence_number"))
            for sentence in sentences
            if sentence.get("sentence_number") is not None
        ]
        if not all_numbers:
            QMessageBox.information(self, "No Sentences", "There are no sentences to process with RVC.")
            return

        self._process_sentence_numbers_with_rvc(all_numbers)

    def _on_save_output(self):
        session_name = self.logic.state.session_name
        output_format = self.logic.state.audio_processing.output_format
        
        file_path, _ = QFileDialog.getSaveFileName(
            self, "Save Output", f"Outputs/{session_name}/{session_name}.{output_format}",
            f"{output_format.upper()} Files (*.{output_format});;All Files (*)"
        )
        if file_path:
            self.logic.save_output(file_path)

    def update_ui_from_state(self):
        self._apply_lifecycle_action_states()

        selected_numbers, source_name = self._capture_selection_snapshot()
        sentences = self.logic.get_processed_sentences_snapshot()
        playing_sentence_number = self.logic.get_current_playing_sentence_number()
        all_row_data = [
            self._build_sentence_row_data(sentence, playing_sentence_number)
            for sentence in sentences
        ]
        marked_row_data = [
            self._build_sentence_row_data(sentence, playing_sentence_number)
            for sentence in sentences
            if sentence.get("marked")
        ]

        self._table_update_in_progress = True
        self.sentences_list.blockSignals(True)
        self.marked_list.blockSignals(True)
        self.sentences_list.setUpdatesEnabled(False)
        self.marked_list.setUpdatesEnabled(False)
        try:
            self._sync_table_rows(self.sentences_list, all_row_data)
            self._sync_table_rows(self.marked_list, marked_row_data)
            self._restore_selection_snapshot(selected_numbers, source_name)
            self._ensure_playing_sentence_visible(playing_sentence_number)
        finally:
            self.sentences_list.setUpdatesEnabled(True)
            self.marked_list.setUpdatesEnabled(True)
            self.sentences_list.blockSignals(False)
            self.marked_list.blockSignals(False)
            self._table_update_in_progress = False
