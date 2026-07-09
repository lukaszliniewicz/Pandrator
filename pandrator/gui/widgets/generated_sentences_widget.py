from functools import partial

from PyQt6.QtWidgets import (
    QWidget, QVBoxLayout, QFrame, QLabel, QPushButton, QTableWidget,
    QHBoxLayout, QTableWidgetItem, QMenu, QComboBox,
    QAbstractItemView, QMessageBox, QFileDialog, QHeaderView,
    QButtonGroup, QStackedWidget,
)
from PyQt6.QtGui import QFont, QColor
from PyQt6.QtCore import Qt, QItemSelectionModel, pyqtSignal

from ..dialogs.output_options_dialog import OutputOptionsDialog

class GeneratedSentencesWidget(QWidget):
    create_requested = pyqtSignal()
    review_count_changed = pyqtSignal(int)

    MARK_COLUMN = 0
    NUMBER_COLUMN = 1
    TEXT_COLUMN = 2

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._table_update_in_progress = False
        self._audio_variant_update_in_progress = False
        self._filter_mode = "all"
        self._current_sentences: list[dict] = []
        self._playing_sentence_number: str | None = None
        self._highlight_color = QColor("#7e57c2")
        self._inline_edit_triggers = (
            QAbstractItemView.EditTrigger.DoubleClicked
            | QAbstractItemView.EditTrigger.EditKeyPressed
            | QAbstractItemView.EditTrigger.SelectedClicked
        )
        
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(14, 12, 14, 12)
        main_layout.setSpacing(8)

        header_layout = QHBoxLayout()
        label = QLabel("Review Generated Audio")
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        header_layout.addWidget(label)
        header_layout.addStretch(1)

        header_layout.addWidget(QLabel("Audio Version:"))
        self.audio_variant_combo = QComboBox()
        self.audio_variant_combo.currentIndexChanged.connect(self._on_audio_variant_selected)
        header_layout.addWidget(self.audio_variant_combo)

        self.output_options_button = QPushButton("Output Options")
        self.output_options_button.clicked.connect(self._on_output_options)
        header_layout.addWidget(self.output_options_button)

        self.save_output_button = QPushButton("Save Output")
        self.save_output_button.clicked.connect(self._on_save_output)
        header_layout.addWidget(self.save_output_button)
        main_layout.addLayout(header_layout)

        filter_frame = QFrame()
        filter_frame.setObjectName("reviewToolbar")
        filter_layout = QHBoxLayout(filter_frame)
        filter_layout.setContentsMargins(0, 0, 0, 0)
        filter_layout.setSpacing(6)
        filter_layout.addWidget(QLabel("Sentences:"))

        self.filter_group = QButtonGroup(self)
        self.filter_group.setExclusive(True)
        self.all_filter_button = self._create_filter_button("All 0", "all")
        self.marked_filter_button = self._create_filter_button("Marked 0", "marked")
        self.filter_group.addButton(self.all_filter_button)
        self.filter_group.addButton(self.marked_filter_button)
        filter_layout.addWidget(self.all_filter_button)
        filter_layout.addWidget(self.marked_filter_button)
        filter_layout.addStretch(1)
        main_layout.addWidget(filter_frame)

        main_layout.addWidget(self._create_playback_frame())

        self.content_stack = QStackedWidget()
        self.empty_state = self._create_empty_state()
        self.content_stack.addWidget(self.empty_state)

        self.sentences_list = QTableWidget()
        self.sentences_list.setColumnCount(3)
        self.sentences_list.setHorizontalHeaderLabels(["Marked", "#", "Sentence"])
        self.sentences_list.verticalHeader().setVisible(False)
        self.sentences_list.horizontalHeader().setSectionResizeMode(
            self.MARK_COLUMN, QHeaderView.ResizeMode.Fixed
        )
        self.sentences_list.horizontalHeader().setSectionResizeMode(
            self.NUMBER_COLUMN, QHeaderView.ResizeMode.Fixed
        )
        self.sentences_list.horizontalHeader().setSectionResizeMode(
            self.TEXT_COLUMN, QHeaderView.ResizeMode.Stretch
        )
        self.sentences_list.setColumnWidth(self.MARK_COLUMN, 72)
        self.sentences_list.setColumnWidth(self.NUMBER_COLUMN, 52)
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
        self.content_stack.addWidget(self.sentences_list)
        main_layout.addWidget(self.content_stack, 1)

        main_layout.addWidget(self._create_actions_frame())
        
        self.logic.state_changed.connect(self.update_ui_from_state)

        self.sentences_list.itemSelectionChanged.connect(self._on_selection_changed)
        
        self.update_ui_from_state()

    def _create_playback_frame(self):
        frame = QFrame()
        frame.setObjectName("reviewToolbar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self.play_button = QPushButton("Play Selected")
        self.play_as_playlist_button = QPushButton("Play from Selection")
        self.stop_button = QPushButton("Stop")
        
        self.play_button.clicked.connect(self._on_play)
        self.play_as_playlist_button.clicked.connect(self._on_play_playlist)
        self.stop_button.clicked.connect(self.logic.stop_playback)

        layout.addWidget(self.play_button)
        layout.addWidget(self.play_as_playlist_button)
        layout.addWidget(self.stop_button)
        layout.addStretch(1)
        return frame

    def review_count(self) -> int:
        return len(self._current_sentences)

    def _create_filter_button(self, text: str, filter_mode: str) -> QPushButton:
        button = QPushButton(text)
        button.setObjectName("reviewFilterButton")
        button.setCheckable(True)
        button.setProperty("filterMode", filter_mode)
        button.clicked.connect(lambda: self._set_filter_mode(filter_mode))
        if filter_mode == self._filter_mode:
            button.setChecked(True)
        return button

    def _create_empty_state(self) -> QWidget:
        widget = QFrame()
        widget.setObjectName("emptyReviewFrame")
        layout = QVBoxLayout(widget)
        layout.setAlignment(Qt.AlignmentFlag.AlignCenter)

        self.empty_state_title = QLabel("Nothing has been generated yet")
        self.empty_state_title.setObjectName("emptyReviewTitle")
        self.empty_state_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_state_title)

        self.empty_state_detail = QLabel(
            "Create or load a session, configure the voice and generation options, "
            "then generate audio to review it here."
        )
        self.empty_state_detail.setObjectName("emptyReviewDetail")
        self.empty_state_detail.setWordWrap(True)
        self.empty_state_detail.setMaximumWidth(520)
        self.empty_state_detail.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_state_detail)

        self.empty_state_create_button = QPushButton("Go to Create")
        self.empty_state_create_button.setObjectName("primaryButton")
        self.empty_state_create_button.clicked.connect(self.create_requested.emit)
        layout.addWidget(
            self.empty_state_create_button,
            alignment=Qt.AlignmentFlag.AlignCenter,
        )
        return widget

    def _create_actions_frame(self):
        frame = QFrame()
        frame.setObjectName("reviewSelectionToolbar")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(0, 0, 0, 0)
        self.selection_label = QLabel("No selection")
        self.selection_label.setObjectName("secondaryInfoLabel")
        self.toggle_mark_button = QPushButton("Toggle Mark")
        self.regenerate_button = QPushButton("Regenerate")
        self.regenerate_menu = QMenu(self.regenerate_button)
        self.regenerate_all_action = self.regenerate_menu.addAction("All")
        self.regenerate_selected_action = self.regenerate_menu.addAction("Selected")
        self.regenerate_marked_action = self.regenerate_menu.addAction("Marked")
        self.regenerate_button.setMenu(self.regenerate_menu)
        self.rvc_button = QPushButton("RVC")
        self.rvc_menu = QMenu(self.rvc_button)
        self.rvc_all_action = self.rvc_menu.addAction("All")
        self.rvc_selected_action = self.rvc_menu.addAction("Selected")
        self.rvc_marked_action = self.rvc_menu.addAction("Marked")
        self.rvc_button.setMenu(self.rvc_menu)
        self.remove_button = QPushButton("Remove Selected")
        self.edit_button = QPushButton("Edit Selected")
        
        self.toggle_mark_button.clicked.connect(self.toggle_mark_for_selected)
        self.regenerate_all_action.triggered.connect(self._on_regenerate_all)
        self.regenerate_selected_action.triggered.connect(self._on_regenerate_selected)
        self.regenerate_marked_action.triggered.connect(self._on_regenerate_marked)
        self.rvc_all_action.triggered.connect(self._on_rvc_all)
        self.rvc_selected_action.triggered.connect(self._on_rvc_selected)
        self.rvc_marked_action.triggered.connect(self._on_rvc_marked)
        self.remove_button.clicked.connect(self._on_remove)
        self.edit_button.clicked.connect(self._on_edit)

        layout.addWidget(self.selection_label)
        layout.addStretch(1)
        layout.addWidget(self.toggle_mark_button)
        layout.addWidget(self.regenerate_button)
        layout.addWidget(self.rvc_button)
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
        num_item = table.item(row, self.NUMBER_COLUMN)
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

        text_item = table.item(row, self.TEXT_COLUMN)
        if not text_item:
            return

        table.setCurrentCell(row, self.TEXT_COLUMN)
        table.editItem(text_item)

    def _on_table_item_changed(self, table: QTableWidget, item: QTableWidgetItem):
        if self._table_update_in_progress:
            return

        row = item.row()
        num_item = table.item(row, self.NUMBER_COLUMN)
        if not num_item:
            return

        sentence_number = num_item.data(Qt.ItemDataRole.UserRole)
        if sentence_number is None:
            return

        if item.column() == self.MARK_COLUMN:
            self.logic.mark_sentence(
                sentence_number,
                item.checkState() == Qt.CheckState.Checked,
            )
        elif item.column() == self.TEXT_COLUMN:
            self.logic.update_sentence_text(sentence_number, item.text())

    def _set_filter_mode(self, filter_mode: str):
        normalized_mode = "marked" if filter_mode == "marked" else "all"
        if self._filter_mode == normalized_mode:
            return
        self._filter_mode = normalized_mode
        self.update_ui_from_state()

    def _on_selection_changed(self):
        self._apply_action_states()

    def toggle_mark_for_selected(self):
        selected_rows, table = self._get_selected_rows_and_table()
        if not selected_rows or table is None:
            return

        sentence_numbers = self._sentence_numbers_from_rows(table, selected_rows)
        sentence_by_number = {
            str(sentence.get("sentence_number")): sentence
            for sentence in self._current_sentences
            if sentence.get("sentence_number") is not None
        }
        mark_all = any(
            not bool(sentence_by_number.get(number, {}).get("marked"))
            for number in sentence_numbers
        )
        for sentence_number in sentence_numbers:
            self.logic.mark_sentence(sentence_number, mark_all)

    def _update_audio_variant_combo(self):
        variants = self.logic.list_audio_variants()
        active_variant_id = self.logic.get_active_audio_variant_id()

        self._audio_variant_update_in_progress = True
        self.audio_variant_combo.blockSignals(True)
        try:
            self.audio_variant_combo.clear()
            active_index = 0
            for index, variant in enumerate(variants):
                variant_id = str(variant.get("id") or "")
                self.audio_variant_combo.addItem(str(variant.get("label") or variant_id), variant_id)
                if variant_id == active_variant_id:
                    active_index = index

            if self.audio_variant_combo.count():
                self.audio_variant_combo.setCurrentIndex(active_index)
        finally:
            self.audio_variant_combo.blockSignals(False)
            self._audio_variant_update_in_progress = False

    def _on_audio_variant_selected(self, _index: int):
        if self._audio_variant_update_in_progress:
            return

        variant_id = self.audio_variant_combo.currentData()
        if not variant_id:
            return

        self.logic.set_active_audio_variant(str(variant_id))

    def _get_selected_rows_and_table(self) -> tuple[list[int], QTableWidget | None]:
        if not self.sentences_list.selectedItems():
            return [], None

        selected_rows = sorted(
            {item.row() for item in self.sentences_list.selectedItems()}
        )
        return selected_rows, self.sentences_list

    def _sentence_numbers_from_rows(self, table: QTableWidget, rows: list[int]) -> list[str]:
        sentence_numbers: list[str] = []
        for row in rows:
            num_item = table.item(row, self.NUMBER_COLUMN)
            if not num_item:
                continue

            sentence_number = num_item.data(Qt.ItemDataRole.UserRole)
            if sentence_number is None:
                continue

            sentence_numbers.append(str(sentence_number))

        return sentence_numbers

    def _filter_generated_sentence_numbers(self, sentence_numbers: list[str]) -> list[str]:
        if hasattr(self.logic, "get_source_audio_sentence_numbers"):
            available_numbers = {
                str(sentence_number)
                for sentence_number in self.logic.get_source_audio_sentence_numbers()
            }
        else:
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

    def _capture_selection_snapshot(self) -> set[str]:
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not source_table:
            return set()

        selected_numbers: set[str] = set()
        for row in selected_rows:
            item = source_table.item(row, self.NUMBER_COLUMN)
            if item:
                sentence_number = item.data(Qt.ItemDataRole.UserRole)
                if sentence_number is not None:
                    selected_numbers.add(str(sentence_number))

        return selected_numbers

    def _table_sentence_numbers(self, table: QTableWidget) -> list[str]:
        numbers: list[str] = []
        for row in range(table.rowCount()):
            item = table.item(row, self.NUMBER_COLUMN)
            numbers.append(str(item.data(Qt.ItemDataRole.UserRole)) if item else "")
        return numbers

    def _table_row_map(self, table: QTableWidget) -> dict[str, int]:
        row_map: dict[str, int] = {}
        for row in range(table.rowCount()):
            item = table.item(row, self.NUMBER_COLUMN)
            if not item:
                continue
            sentence_number = item.data(Qt.ItemDataRole.UserRole)
            if sentence_number is not None:
                row_map[str(sentence_number)] = row
        return row_map

    def _build_sentence_row_data(
        self,
        sentence: dict,
        playing_sentence_number: str | None,
    ) -> tuple[str, str, bool, bool, bool]:
        sentence_number = str(sentence.get("sentence_number", "?"))
        sentence_text = sentence.get("processed_sentence", sentence.get("original_sentence", ""))
        tts_generated = sentence.get("tts_generated", "no") == "yes"
        marked = bool(sentence.get("marked"))
        is_playing = playing_sentence_number is not None and sentence_number == str(playing_sentence_number)
        return sentence_number, sentence_text, tts_generated, marked, is_playing

    def _upsert_table_row(
        self,
        table: QTableWidget,
        row: int,
        sentence_number: str,
        sentence_text: str,
        tts_generated: bool,
        marked: bool,
        is_playing: bool,
    ) -> bool:
        mark_item = table.item(row, self.MARK_COLUMN)
        if mark_item is None:
            mark_item = QTableWidgetItem()
            table.setItem(row, self.MARK_COLUMN, mark_item)
        mark_item.setData(Qt.ItemDataRole.UserRole, sentence_number)
        mark_item.setTextAlignment(
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter
        )
        mark_item.setFlags(
            (
                mark_item.flags()
                | Qt.ItemFlag.ItemIsSelectable
                | Qt.ItemFlag.ItemIsEnabled
                | Qt.ItemFlag.ItemIsUserCheckable
            )
            & ~Qt.ItemFlag.ItemIsEditable
        )
        desired_check_state = (
            Qt.CheckState.Checked if marked else Qt.CheckState.Unchecked
        )
        if mark_item.checkState() != desired_check_state:
            mark_item.setCheckState(desired_check_state)

        num_item = table.item(row, self.NUMBER_COLUMN)
        if num_item is None:
            num_item = QTableWidgetItem()
            table.setItem(row, self.NUMBER_COLUMN, num_item)

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

        text_item = table.item(row, self.TEXT_COLUMN)
        if text_item is None:
            text_item = QTableWidgetItem()
            table.setItem(row, self.TEXT_COLUMN, text_item)

        text_changed = text_item.text() != sentence_text
        if text_changed:
            text_item.setText(sentence_text)
        text_item.setFlags(text_item.flags() | Qt.ItemFlag.ItemIsEditable)

        if tts_generated:
            mark_item.setData(Qt.ItemDataRole.ForegroundRole, None)
            num_item.setData(Qt.ItemDataRole.ForegroundRole, None)
            text_item.setData(Qt.ItemDataRole.ForegroundRole, None)
        else:
            gray_color = QColor("gray")
            mark_item.setData(Qt.ItemDataRole.ForegroundRole, gray_color)
            num_item.setData(Qt.ItemDataRole.ForegroundRole, gray_color)
            text_item.setData(Qt.ItemDataRole.ForegroundRole, gray_color)

        if is_playing:
            mark_item.setData(Qt.ItemDataRole.BackgroundRole, self._highlight_color)
            num_item.setData(Qt.ItemDataRole.BackgroundRole, self._highlight_color)
            text_item.setData(Qt.ItemDataRole.BackgroundRole, self._highlight_color)
        else:
            mark_item.setData(Qt.ItemDataRole.BackgroundRole, None)
            num_item.setData(Qt.ItemDataRole.BackgroundRole, None)
            text_item.setData(Qt.ItemDataRole.BackgroundRole, None)

        return text_changed

    def _sync_table_rows(
        self,
        table: QTableWidget,
        row_data: list[tuple[str, str, bool, bool, bool]],
    ):
        desired_numbers = [sentence_number for sentence_number, _, _, _, _ in row_data]
        current_numbers = self._table_sentence_numbers(table)

        if current_numbers != desired_numbers:
            table.setRowCount(len(row_data))
            for row, (sentence_number, sentence_text, tts_generated, marked, is_playing) in enumerate(row_data):
                self._upsert_table_row(
                    table,
                    row,
                    sentence_number,
                    sentence_text,
                    tts_generated,
                    marked,
                    is_playing,
                )
            table.resizeRowsToContents()
            return

        rows_to_resize: list[int] = []
        for row, (sentence_number, sentence_text, tts_generated, marked, is_playing) in enumerate(row_data):
            text_changed = self._upsert_table_row(
                table,
                row,
                sentence_number,
                sentence_text,
                tts_generated,
                marked,
                is_playing,
            )
            if text_changed:
                rows_to_resize.append(row)

        for row in rows_to_resize:
            table.resizeRowToContents(row)

    def _restore_selection_snapshot(self, selected_numbers: set[str]):
        self.sentences_list.clearSelection()

        if not selected_numbers:
            return

        target_table = self.sentences_list
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

            selection_model.select(
                target_table.model().index(row, self.NUMBER_COLUMN),
                selection_flags,
            )

        target_table.setCurrentCell(target_rows[0], self.NUMBER_COLUMN)
        target_table.setFocus()

    def _ensure_playing_sentence_visible(self, playing_sentence_number: str | None):
        if playing_sentence_number is None:
            return

        target_number = str(playing_sentence_number)
        row = self._table_row_map(self.sentences_list).get(target_number)
        if row is None:
            return

        target_item = (
            self.sentences_list.item(row, self.TEXT_COLUMN)
            or self.sentences_list.item(row, self.NUMBER_COLUMN)
        )
        if target_item:
            self.sentences_list.scrollToItem(
                target_item,
                QAbstractItemView.ScrollHint.PositionAtCenter,
            )

    def _apply_action_states(self):
        generation_running = self.logic.is_generation_running()
        regeneration_running = self.logic.is_regeneration_running()
        rvc_processing_running = self.logic.is_rvc_processing_running()
        generation_busy = generation_running or regeneration_running or rvc_processing_running
        rvc_available = self.logic.is_rvc_available()
        selected_rows, table = self._get_selected_rows_and_table()
        selected_numbers = (
            self._sentence_numbers_from_rows(table, selected_rows)
            if table is not None
            else []
        )
        generated_numbers = set(
            self._filter_generated_sentence_numbers(
                [
                    str(sentence.get("sentence_number"))
                    for sentence in self._current_sentences
                    if sentence.get("sentence_number") is not None
                ]
            )
        )
        marked_count = sum(
            1 for sentence in self._current_sentences if sentence.get("marked")
        )
        has_rows = bool(self._current_sentences)
        has_audio = bool(generated_numbers)
        selected_count = len(selected_numbers)
        selected_has_audio = any(
            sentence_number in generated_numbers for sentence_number in selected_numbers
        )
        is_playing = self._playing_sentence_number is not None

        self.selection_label.setText(
            f"{selected_count} selected" if selected_count else "No selection"
        )
        self.play_button.setEnabled(
            (not generation_busy) and selected_count > 0 and selected_has_audio
        )
        self.play_as_playlist_button.setEnabled(
            (not generation_busy) and has_audio
        )
        self.stop_button.setEnabled(is_playing)
        self.output_options_button.setEnabled(not generation_busy)
        self.save_output_button.setEnabled((not generation_busy) and has_audio)
        self.audio_variant_combo.setEnabled((not generation_busy) and has_audio)
        self.all_filter_button.setEnabled(has_rows)
        self.marked_filter_button.setEnabled(has_rows)

        self.toggle_mark_button.setEnabled(
            (not generation_busy) and selected_count > 0
        )
        self.regenerate_button.setEnabled((not generation_busy) and has_rows)
        self.regenerate_all_action.setEnabled((not generation_busy) and has_rows)
        self.regenerate_selected_action.setEnabled(
            (not generation_busy) and selected_count > 0
        )
        self.regenerate_marked_action.setEnabled(
            (not generation_busy) and marked_count > 0
        )
        self.rvc_button.setEnabled(
            (not generation_busy) and rvc_available and has_audio
        )
        self.rvc_all_action.setEnabled(
            (not generation_busy) and rvc_available and has_audio
        )
        self.rvc_selected_action.setEnabled(
            (not generation_busy) and rvc_available and selected_has_audio
        )
        self.rvc_marked_action.setEnabled(
            (not generation_busy) and rvc_available and marked_count > 0 and has_audio
        )
        self.remove_button.setEnabled(
            (not generation_busy) and selected_count > 0
        )
        self.edit_button.setEnabled(
            (not generation_busy) and selected_count == 1
        )

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
        self.sentences_list.setEditTriggers(edit_policy)

    def _on_play(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            QMessageBox.information(self, "No Selection", "Please select a sentence to play.")
            return
        
        sentence_number = source_table.item(
            selected_rows[0], self.NUMBER_COLUMN
        ).data(Qt.ItemDataRole.UserRole)
        self.logic.play_audio_for_sentence(sentence_number)

    def _on_play_playlist(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        start_sentence_number = None
        if selected_rows:
            start_sentence_number = source_table.item(
                selected_rows[0], self.NUMBER_COLUMN
            ).data(Qt.ItemDataRole.UserRole)
        self.logic.play_playlist(start_sentence_number)

    def _on_remove(self):
        selected_rows, source_table = self._get_selected_rows_and_table()
        if not selected_rows:
            return
        
        reply = QMessageBox.question(self, "Remove Sentences", 
                                     f"Are you sure you want to remove {len(selected_rows)} sentence(s)?",
                                     QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if reply == QMessageBox.StandardButton.Yes:
            numbers_to_remove = [
                source_table.item(row, self.NUMBER_COLUMN).data(
                    Qt.ItemDataRole.UserRole
                )
                for row in selected_rows
            ]
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

    def _on_rvc_marked(self):
        marked_sentences = [
            s for s in self.logic.get_processed_sentences_snapshot() if s.get("marked")
        ]
        if not marked_sentences:
            QMessageBox.information(self, "No Marked Sentences", "There are no sentences marked for RVC processing.")
            return

        sentence_numbers = [
            str(sentence.get("sentence_number"))
            for sentence in marked_sentences
            if sentence.get("sentence_number") is not None
        ]
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

    def _on_output_options(self):
        dialog = OutputOptionsDialog(self.logic, self)
        dialog.exec()

    def update_ui_from_state(self):
        self._update_audio_variant_combo()

        selected_numbers = self._capture_selection_snapshot()
        if hasattr(self.logic, "get_audio_variant_sentences_snapshot"):
            sentences = self.logic.get_audio_variant_sentences_snapshot()
        else:
            sentences = self.logic.get_processed_sentences_snapshot()
        playing_sentence_number = self.logic.get_current_playing_sentence_number()

        self._current_sentences = list(sentences)
        self._playing_sentence_number = playing_sentence_number
        marked_count = sum(1 for sentence in sentences if sentence.get("marked"))
        self.all_filter_button.setText(f"All {len(sentences)}")
        self.marked_filter_button.setText(f"Marked {marked_count}")
        self.review_count_changed.emit(len(sentences))

        filtered_sentences = (
            [sentence for sentence in sentences if sentence.get("marked")]
            if self._filter_mode == "marked"
            else sentences
        )
        row_data = [
            self._build_sentence_row_data(sentence, playing_sentence_number)
            for sentence in filtered_sentences
        ]

        self._table_update_in_progress = True
        self.sentences_list.blockSignals(True)
        self.sentences_list.setUpdatesEnabled(False)
        try:
            self._sync_table_rows(self.sentences_list, row_data)
            self._restore_selection_snapshot(selected_numbers)
            self._ensure_playing_sentence_visible(playing_sentence_number)
        finally:
            self.sentences_list.setUpdatesEnabled(True)
            self.sentences_list.blockSignals(False)
            self._table_update_in_progress = False

        if row_data:
            self.content_stack.setCurrentWidget(self.sentences_list)
        else:
            if sentences:
                self.empty_state_title.setText("No sentences are marked")
                self.empty_state_detail.setText(
                    "Mark sentences in the All view to collect them here for "
                    "regeneration or RVC processing."
                )
                self.empty_state_create_button.setVisible(False)
            else:
                self.empty_state_title.setText("Nothing has been generated yet")
                self.empty_state_detail.setText(
                    "Create or load a session, configure the voice and generation "
                    "options, then generate audio to review it here."
                )
                self.empty_state_create_button.setVisible(True)
            self.content_stack.setCurrentWidget(self.empty_state)

        self._apply_action_states()
