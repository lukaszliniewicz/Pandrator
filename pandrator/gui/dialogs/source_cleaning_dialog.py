import json
import logging
import threading

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtGui import QTextCursor
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QSpinBox,
    QPushButton,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)


class SourceCleaningDialog(QDialog):
    status_updated = pyqtSignal(str)
    cleaning_finished = pyqtSignal(object)

    _MIN_MAX_ITERATIONS = 1
    _MAX_MAX_ITERATIONS = 100
    _DEFAULT_MAX_ITERATIONS = 30

    def _load_max_iterations(self) -> int:
        cleaning_settings = getattr(self.logic.state, "source_cleaning", None)
        try:
            value = int(getattr(cleaning_settings, "max_iterations", self._DEFAULT_MAX_ITERATIONS))
        except (TypeError, ValueError):
            value = self._DEFAULT_MAX_ITERATIONS
        return max(self._MIN_MAX_ITERATIONS, min(value, self._MAX_MAX_ITERATIONS))

    def _save_max_iterations(self) -> None:
        cleaning_settings = getattr(self.logic.state, "source_cleaning", None)
        if cleaning_settings is None:
            return
        new_value = max(self._MIN_MAX_ITERATIONS, min(int(self.max_iterations_spinbox.value()), self._MAX_MAX_ITERATIONS))
        if getattr(cleaning_settings, "max_iterations", None) == new_value:
            return
        cleaning_settings.max_iterations = new_value
        persist = getattr(self.logic, "_persist_global_settings", None)
        if callable(persist):
            try:
                persist()
            except Exception as e:  # noqa: BLE001 - persistence is best-effort on dialog close
                logging.getLogger(__name__).warning(
                    "Failed to persist source cleaning max_iterations: %s", e
                )

    def __init__(self, logic, source_path_hint: str = "", parent=None):
        super().__init__(parent)
        self.logic = logic
        self.source_path_hint = source_path_hint
        self._choice = "cancel"
        self._result: dict | None = None
        self._running = False
        self._worker_thread: threading.Thread | None = None

        self.setWindowTitle("LLM Source Cleaning Review")
        self.resize(1100, 820)

        self._build_layout()
        self._connect_signals()
        self._populate_model_combo()
        self._show_raw_source_preview()
        self._set_running(False)

    def _build_layout(self):
        layout = QVBoxLayout(self)

        header = QLabel(
            "Use an LLM tool loop to inspect the extracted source, mark chapters, remove non-audiobook fragments, "
            "and propose metadata. Review the result before accepting."
        )
        header.setWordWrap(True)
        header.setObjectName("secondaryInfoLabel")
        layout.addWidget(header)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Model:"))
        self.model_combo = QComboBox()
        self.model_combo.setEditable(True)
        controls.addWidget(self.model_combo, 1)

        self.remove_footnotes_checkbox = QCheckBox("Remove Footnotes/Endnotes")
        controls.addWidget(self.remove_footnotes_checkbox)

        controls.addWidget(QLabel("Max Iterations:"))
        self.max_iterations_spinbox = QSpinBox()
        self.max_iterations_spinbox.setRange(1, 100)
        self.max_iterations_spinbox.setValue(self._load_max_iterations())
        self.max_iterations_spinbox.setToolTip(
            "Maximum number of LLM/tool turns the cleaning agent is allowed to take."
        )
        controls.addWidget(self.max_iterations_spinbox)

        self.run_button = QPushButton("Run LLM Cleaning")
        controls.addWidget(self.run_button)
        layout.addLayout(controls)

        status_row = QHBoxLayout()
        self.status_label = QLabel("Choose a model and click Run LLM Cleaning when ready.")
        self.status_label.setWordWrap(True)
        status_row.addWidget(self.status_label, 1)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        status_row.addWidget(self.progress_bar)
        layout.addLayout(status_row)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)

        text_tab = QWidget()
        text_layout = QVBoxLayout(text_tab)
        text_controls = QHBoxLayout()
        self.add_chapter_button = QPushButton("Add Chapter Marker")
        text_controls.addWidget(self.add_chapter_button)
        text_controls.addStretch(1)
        text_layout.addLayout(text_controls)
        self.text_edit = QTextEdit()
        text_layout.addWidget(self.text_edit, 1)
        self.tabs.addTab(text_tab, "Cleaned Text")

        self.diff_edit = QPlainTextEdit()
        self.diff_edit.setReadOnly(True)
        self.tabs.addTab(self.diff_edit, "Diff")

        metadata_tab = QWidget()
        metadata_layout = QFormLayout(metadata_tab)
        self.title_edit = QLineEdit()
        self.album_edit = QLineEdit()
        self.artist_edit = QLineEdit()
        self.genre_edit = QLineEdit()
        self.language_edit = QLineEdit()
        metadata_layout.addRow("Title:", self.title_edit)
        metadata_layout.addRow("Album:", self.album_edit)
        metadata_layout.addRow("Artist/Author:", self.artist_edit)
        metadata_layout.addRow("Genre:", self.genre_edit)
        metadata_layout.addRow("Language:", self.language_edit)
        self.tabs.addTab(metadata_tab, "Metadata")

        self.report_edit = QPlainTextEdit()
        self.report_edit.setReadOnly(True)
        self.tabs.addTab(self.report_edit, "Report")

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.accept_button = QPushButton("Accept Cleaned Text")
        self.manual_button = QPushButton("Manual Review Raw Text")
        self.cancel_button = QPushButton("Cancel Import")
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.manual_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def _connect_signals(self):
        self.run_button.clicked.connect(self._start_cleaning)
        self.accept_button.clicked.connect(self._accept_cleaned_text)
        self.manual_button.clicked.connect(self._manual_review)
        self.cancel_button.clicked.connect(self._cancel_import)
        self.add_chapter_button.clicked.connect(self._insert_chapter_marker)
        self.text_edit.textChanged.connect(self._refresh_action_state)
        self.status_updated.connect(self._on_status_updated)
        self.cleaning_finished.connect(self._on_cleaning_finished)

    def _populate_model_combo(self):
        models = []
        try:
            models = self.logic.list_llm_models()
        except Exception:
            models = ["default"]

        if "default" not in models:
            models.insert(0, "default")
        self.model_combo.addItems(models)
        target = str(getattr(self.logic.state.llm, "default_model", "") or "default").strip()
        if target and target in models:
            self.model_combo.setCurrentText(target)
        else:
            self.model_combo.setCurrentText("default")

    def _set_running(self, running: bool):
        self._running = running
        self.run_button.setEnabled(not running)
        self.model_combo.setEnabled(not running)
        self.remove_footnotes_checkbox.setEnabled(not running)
        self.max_iterations_spinbox.setEnabled(not running)
        self.text_edit.setReadOnly(running)
        self.add_chapter_button.setEnabled(not running)
        self.manual_button.setEnabled(not running)
        self.cancel_button.setEnabled(not running)
        self._refresh_action_state()
        if running:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)

    def _refresh_action_state(self):
        has_reviewable_result = self._result is not None
        has_text = bool(self.text_edit.toPlainText().strip())
        self.accept_button.setEnabled((not self._running) and has_reviewable_result and has_text)

    def _show_raw_source_preview(self):
        raw_text = str(getattr(self.logic.state, "raw_text", "") or "")
        self.text_edit.setPlainText(raw_text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.Start)
        self.tabs.setTabText(0, "Source Preview")
        if raw_text:
            self.status_label.setText(
                f"Parsed source preview: {len(raw_text):,} characters. Choose a model and run LLM cleaning."
            )
        else:
            self.status_label.setText("No extracted source preview is available.")

    def _start_cleaning(self):
        if self._running:
            return

        self._result = None
        self.diff_edit.clear()
        self.report_edit.clear()
        self.status_label.setText("Starting source-cleaning loop...")
        self._set_running(True)

        remove_footnotes = self.remove_footnotes_checkbox.isChecked()
        model_name = self.model_combo.currentText().strip() or "default"
        max_iterations = int(self.max_iterations_spinbox.value())

        def worker():
            try:
                result = self.logic.run_source_cleaning(
                    source_path_hint=self.source_path_hint,
                    remove_footnotes=remove_footnotes,
                    model_name=model_name,
                    max_iterations=max_iterations,
                    progress_callback=self.status_updated.emit,
                )
            except Exception as e:
                result = {
                    "success": False,
                    "error": str(e),
                    "warnings": [str(e)],
                }
            self.cleaning_finished.emit(result)

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _on_status_updated(self, message: str):
        self.status_label.setText(str(message or "Working..."))

    def _on_cleaning_finished(self, result: object):
        payload = result if isinstance(result, dict) else {"success": False, "error": "Invalid cleaning result."}
        self._result = None if payload.get("error") else payload
        self._set_running(False)

        if payload.get("error"):
            self.status_label.setText(f"LLM source cleaning failed: {payload.get('error')}")
            self.report_edit.setPlainText(json.dumps(payload, indent=2, ensure_ascii=False))
            self.tabs.setCurrentWidget(self.report_edit)
            return

        cleaned_text = str(payload.get("cleaned_text") or "")
        self.text_edit.setPlainText(cleaned_text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.Start)
        self.tabs.setTabText(0, "Cleaned Text")
        self._refresh_action_state()
        self.diff_edit.setPlainText(str(payload.get("diff") or ""))
        self._populate_metadata(payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {})
        self.report_edit.setPlainText(
            json.dumps(
                {
                    "report": payload.get("report", {}),
                    "validation": payload.get("validation", {}),
                    "warnings": payload.get("warnings", []),
                    "artifacts_dir": payload.get("artifacts_dir", ""),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

        warnings = payload.get("warnings") if isinstance(payload.get("warnings"), list) else []
        if payload.get("success"):
            self.status_label.setText("Source-cleaning result is ready for review.")
        else:
            self.status_label.setText("Source-cleaning result needs review before accepting.")

        if warnings:
            QMessageBox.warning(
                self,
                "Source Cleaning Warnings",
                "\n".join(str(item) for item in warnings[:6]),
            )

    def _populate_metadata(self, metadata: dict):
        self.title_edit.setText(str(metadata.get("title") or ""))
        self.album_edit.setText(str(metadata.get("album") or metadata.get("title") or ""))
        self.artist_edit.setText(str(metadata.get("artist") or metadata.get("author") or ""))
        self.genre_edit.setText(str(metadata.get("genre") or ""))
        self.language_edit.setText(str(metadata.get("language") or ""))

    def _insert_chapter_marker(self):
        cursor = self.text_edit.textCursor()
        cursor.insertText("[[Chapter]]")

    def _accept_cleaned_text(self):
        edited_text = self.text_edit.toPlainText()
        if not edited_text.strip():
            QMessageBox.warning(self, "Source Cleaning", "Cleaned text is empty.")
            return
        if self._result is None:
            QMessageBox.warning(self, "Source Cleaning", "Run source cleaning before accepting the result.")
            return
        original_text = str(self._result.get("cleaned_text") or "")
        self._result["cleaned_text"] = edited_text
        self._result["user_edited"] = edited_text != original_text
        self._choice = "accept"
        self.accept()

    def _manual_review(self):
        self._choice = "manual"
        self.accept()

    def _cancel_import(self):
        self._choice = "cancel"
        self.reject()

    def closeEvent(self, event):
        if self._running:
            event.ignore()
            QMessageBox.information(
                self,
                "Source Cleaning Running",
                "Please wait for source cleaning to finish before closing this dialog.",
            )
            return
        self._save_max_iterations()
        super().closeEvent(event)

    def choice(self) -> str:
        return self._choice

    def get_data(self) -> dict:
        return {
            "text": self.text_edit.toPlainText(),
            "metadata": {
                "title": self.title_edit.text(),
                "album": self.album_edit.text(),
                "artist": self.artist_edit.text(),
                "genre": self.genre_edit.text(),
                "language": self.language_edit.text(),
            },
            "result": self._result or {},
        }
