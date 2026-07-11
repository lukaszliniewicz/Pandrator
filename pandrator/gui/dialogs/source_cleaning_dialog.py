import json
import logging
import re
import threading
from threading import Event

from PyQt6.QtCore import Qt, pyqtSignal
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
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from ...logic.source_cleaning import (
    MAX_PHASE_MAX_ITERATIONS,
    MIN_PHASE_MAX_ITERATIONS,
    PHASE_DESCRIPTIONS,
    PHASE_HELP_TEXT,
    PHASE_ORDER,
    resolve_phase_max_iterations,
)


class SourceCleaningDialog(QDialog):
    status_updated = pyqtSignal(str)
    cleaning_finished = pyqtSignal(object)
    preview_finished = pyqtSignal(object)

    def _load_phase_max_iterations(self) -> dict[str, int]:
        cleaning_settings = getattr(self.logic.state, "source_cleaning", None)
        return resolve_phase_max_iterations(
            getattr(cleaning_settings, "phase_max_iterations", None),
            total=getattr(cleaning_settings, "max_iterations", 53),
        )

    def _current_phase_max_iterations(self) -> dict[str, int]:
        return {
            phase_name: int(spinbox.value())
            for phase_name, spinbox in self.phase_iteration_spinboxes.items()
        }

    def _save_phase_iterations_as_defaults(self) -> None:
        cleaning_settings = getattr(self.logic.state, "source_cleaning", None)
        if cleaning_settings is None:
            return
        phase_iterations = self._current_phase_max_iterations()
        cleaning_settings.phase_max_iterations = phase_iterations
        cleaning_settings.max_iterations = sum(phase_iterations.values())
        persist = getattr(self.logic, "_persist_global_settings", None)
        if callable(persist):
            try:
                persist()
            except Exception as e:  # noqa: BLE001 - persistence is best-effort
                logging.getLogger(__name__).warning(
                    "Failed to persist source cleaning phase limits: %s", e
                )
                self.status_label.setText(f"Could not save cleaning phase defaults: {e}")
                return
        self.status_label.setText("Advanced cleaning settings saved as defaults for future imports.")

    def _toggle_phase_limits(self, expanded: bool) -> None:
        self.phase_limits_content.setVisible(expanded)
        arrow = Qt.ArrowType.DownArrow if expanded else Qt.ArrowType.RightArrow
        self.phase_limits_toggle.setArrowType(arrow)

    def _update_phase_limits_summary(self) -> None:
        total = sum(self._current_phase_max_iterations().values())
        self.phase_limits_summary.setText(f"{len(PHASE_ORDER)} phases | up to {total} LLM turns total")

    def __init__(self, logic, source_path_hint: str = "", parent=None):
        super().__init__(parent)
        self.logic = logic
        self.source_path_hint = source_path_hint
        self._choice = "cancel"
        self._result: dict | None = None
        self._running = False
        self._worker_thread: threading.Thread | None = None
        self._stop_event: Event = Event()

        self.setWindowTitle("Source Cleaning Review")
        self.resize(1100, 820)

        self._build_layout()
        self._connect_signals()
        self._populate_model_combo()
        self._show_raw_source_preview()
        self._populate_metadata(getattr(self.logic.state, "metadata", {}) or {})
        self._set_running(False)

    def _build_layout(self):
        layout = QVBoxLayout(self)

        header = QLabel(
            "Review deterministic source ingestion and cleanup. Optionally run the LLM tool loop to inspect the "
            "structured source, refine chapters, remove non-audiobook fragments, and propose metadata."
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
        if hasattr(self.logic.state, "text_processing") and hasattr(self.logic.state.text_processing, "remove_footnotes"):
            self.remove_footnotes_checkbox.setChecked(self.logic.state.text_processing.remove_footnotes)
        controls.addWidget(self.remove_footnotes_checkbox)

        self.filter_citations_checkbox = QCheckBox("Filter Citations")
        if hasattr(self.logic.state, "text_processing") and hasattr(self.logic.state.text_processing, "filter_citations"):
            self.filter_citations_checkbox.setChecked(self.logic.state.text_processing.filter_citations)
        else:
            self.filter_citations_checkbox.setChecked(True)
        self.filter_citations_checkbox.setEnabled(not self.remove_footnotes_checkbox.isChecked())
        
        self.remove_footnotes_checkbox.stateChanged.connect(self._on_deterministic_settings_changed)
        self.filter_citations_checkbox.stateChanged.connect(self._on_deterministic_settings_changed)
        controls.addWidget(self.filter_citations_checkbox)

        self.run_button = QPushButton("Run LLM Cleaning")
        controls.addWidget(self.run_button)

        self.stop_button = QPushButton("Stop")
        self.stop_button.setToolTip("Request the cleaning agent to stop after the current LLM turn.")
        controls.addWidget(self.stop_button)
        layout.addLayout(controls)

        self.pdf_controls_widget = QWidget()
        pdf_controls = QHBoxLayout(self.pdf_controls_widget)
        pdf_controls.setContentsMargins(0, 0, 0, 0)
        pdf_controls.addWidget(QLabel("PDF OCR:"))
        self.pdf_ocr_mode_combo = QComboBox()
        self.pdf_ocr_mode_combo.addItem("Automatic per page", "auto")
        self.pdf_ocr_mode_combo.addItem("Native text only", "off")
        self.pdf_ocr_mode_combo.addItem("Force OCR on every page", "force")
        pdf_controls.addWidget(self.pdf_ocr_mode_combo)
        pdf_controls.addWidget(QLabel("OCR language/script:"))
        self.pdf_ocr_language_combo = QComboBox()
        self.pdf_ocr_language_combo.setEditable(True)
        self.pdf_ocr_language_combo.addItems(["auto", "en", "pl", "de", "fr", "es", "cyrillic", "arabic"])
        pdf_controls.addWidget(self.pdf_ocr_language_combo)
        self.pdf_remove_toc_checkbox = QCheckBox("Remove high-confidence TOC")
        pdf_controls.addWidget(self.pdf_remove_toc_checkbox)
        self.pdf_remove_marginals_checkbox = QCheckBox("Remove repeated margins/page numbers")
        pdf_controls.addWidget(self.pdf_remove_marginals_checkbox)
        self.pdf_refresh_button = QPushButton("Re-ingest PDF")
        pdf_controls.addWidget(self.pdf_refresh_button)
        layout.addWidget(self.pdf_controls_widget)
        self._load_pdf_settings()

        phase_limits_header = QHBoxLayout()
        self.phase_limits_toggle = QToolButton()
        self.phase_limits_toggle.setText("Advanced cleaning settings")
        self.phase_limits_toggle.setCheckable(True)
        self.phase_limits_toggle.setArrowType(Qt.ArrowType.RightArrow)
        self.phase_limits_toggle.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        self.phase_limits_toggle.setToolTip(
            "Expand to set the maximum LLM turns for each cleaning phase."
        )
        phase_limits_header.addWidget(self.phase_limits_toggle)
        self.phase_limits_summary = QLabel()
        self.phase_limits_summary.setObjectName("secondaryInfoLabel")
        phase_limits_header.addWidget(self.phase_limits_summary)
        phase_limits_header.addStretch(1)
        layout.addLayout(phase_limits_header)

        self.phase_limits_content = QFrame()
        self.phase_limits_content.setObjectName("groupFrame")
        phase_limits_layout = QFormLayout(self.phase_limits_content)
        phase_limits_layout.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        saved_phase_iterations = self._load_phase_max_iterations()
        self.phase_iteration_spinboxes: dict[str, QSpinBox] = {}
        for phase_name in PHASE_ORDER:
            spinbox = QSpinBox()
            spinbox.setRange(MIN_PHASE_MAX_ITERATIONS, MAX_PHASE_MAX_ITERATIONS)
            spinbox.setValue(saved_phase_iterations[phase_name])
            spinbox.setSuffix(" turns")
            spinbox.setMinimumWidth(120)
            spinbox.setToolTip(PHASE_HELP_TEXT[phase_name])
            spinbox.valueChanged.connect(self._update_phase_limits_summary)
            label = QLabel(f"{PHASE_DESCRIPTIONS[phase_name]}:")
            label.setToolTip(PHASE_HELP_TEXT[phase_name])
            phase_limits_layout.addRow(label, spinbox)
            self.phase_iteration_spinboxes[phase_name] = spinbox

        phase_defaults_row = QHBoxLayout()
        phase_defaults_hint = QLabel("These settings apply to this run unless saved as defaults.")
        phase_defaults_hint.setObjectName("secondaryInfoLabel")
        phase_defaults_row.addWidget(phase_defaults_hint)
        phase_defaults_row.addStretch(1)
        self.save_phase_defaults_button = QPushButton("Save as Defaults")
        phase_defaults_row.addWidget(self.save_phase_defaults_button)
        phase_limits_layout.addRow(phase_defaults_row)
        self.phase_limits_content.setVisible(False)
        layout.addWidget(self.phase_limits_content)
        self._update_phase_limits_summary()

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

        self.ingestion_report_edit = QPlainTextEdit()
        self.ingestion_report_edit.setReadOnly(True)
        self.tabs.addTab(self.ingestion_report_edit, "PDF Ingestion")
        self.tabs.setTabVisible(self.tabs.indexOf(self.ingestion_report_edit), self._is_pdf_source())

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(separator)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.accept_button = QPushButton("Accept Cleaned Text")
        self.cancel_button = QPushButton("Cancel Import")
        buttons.addWidget(self.accept_button)
        buttons.addWidget(self.cancel_button)
        layout.addLayout(buttons)

    def _connect_signals(self):
        self.run_button.clicked.connect(self._start_cleaning)
        self.stop_button.clicked.connect(self._stop_cleaning)
        self.accept_button.clicked.connect(self._accept_cleaned_text)
        self.cancel_button.clicked.connect(self._cancel_import)
        self.add_chapter_button.clicked.connect(self._insert_chapter_marker)
        self.text_edit.textChanged.connect(self._refresh_action_state)
        self.status_updated.connect(self._on_status_updated)
        self.cleaning_finished.connect(self._on_cleaning_finished)
        self.preview_finished.connect(self._on_preview_finished)
        self.pdf_refresh_button.clicked.connect(self._refresh_pdf_ingestion)
        self.phase_limits_toggle.toggled.connect(self._toggle_phase_limits)
        self.save_phase_defaults_button.clicked.connect(self._save_phase_iterations_as_defaults)

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

    def _set_running(self, running: bool, allow_stop: bool = True):
        self._running = running
        self.run_button.setEnabled(not running)
        self.stop_button.setEnabled(running and allow_stop)
        self.model_combo.setEnabled(not running)
        self.remove_footnotes_checkbox.setEnabled(not running)
        self.filter_citations_checkbox.setEnabled(not running and not self.remove_footnotes_checkbox.isChecked())
        self.phase_limits_content.setEnabled(not running)
        self.pdf_controls_widget.setEnabled(not running)
        self.text_edit.setReadOnly(running)
        self.add_chapter_button.setEnabled(not running)
        self.cancel_button.setEnabled(not running)
        self._refresh_action_state()
        if running:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(0)

    def _refresh_action_state(self):
        has_text = bool(self.text_edit.toPlainText().strip())
        self.accept_button.setEnabled((not self._running) and has_text)

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
        self._show_pdf_ingestion_review_data()

    def _show_pdf_ingestion_review_data(self):
        if not self._is_pdf_source():
            return
        getter = getattr(self.logic, "get_pdf_ingestion_review_data", None)
        review_data = getter() if callable(getter) else {}
        self.diff_edit.setPlainText(str(review_data.get("diff") or ""))
        self.ingestion_report_edit.setPlainText(
            json.dumps(
                {
                    "ingestion": review_data.get("ingestion", {}),
                    "deterministic_cleanup": review_data.get("cleanup_report", {}),
                },
                indent=2,
                ensure_ascii=False,
            )
        )

    def _is_pdf_source(self) -> bool:
        return str(self.source_path_hint or "").lower().endswith(".pdf")

    def _load_pdf_settings(self):
        is_pdf = self._is_pdf_source()
        self.pdf_controls_widget.setVisible(is_pdf)
        if not is_pdf:
            return
        settings = getattr(self.logic.state, "source_cleaning", None)
        mode = str(getattr(settings, "pdf_ocr_mode", "auto") or "auto")
        mode_index = self.pdf_ocr_mode_combo.findData(mode)
        self.pdf_ocr_mode_combo.setCurrentIndex(max(0, mode_index))
        self.pdf_ocr_language_combo.setCurrentText(
            str(getattr(settings, "pdf_ocr_language", "auto") or "auto")
        )
        self.pdf_remove_toc_checkbox.setChecked(bool(getattr(settings, "pdf_remove_toc", True)))
        self.pdf_remove_marginals_checkbox.setChecked(
            bool(getattr(settings, "pdf_remove_repeated_marginals", True))
        )

    def _save_pdf_settings(self):
        if not self._is_pdf_source():
            return
        settings = getattr(self.logic.state, "source_cleaning", None)
        if settings is None:
            return
        settings.pdf_ocr_mode = str(self.pdf_ocr_mode_combo.currentData() or "auto")
        settings.pdf_ocr_language = self.pdf_ocr_language_combo.currentText().strip().lower() or "auto"
        settings.pdf_remove_toc = self.pdf_remove_toc_checkbox.isChecked()
        settings.pdf_remove_repeated_marginals = self.pdf_remove_marginals_checkbox.isChecked()
        persist = getattr(self.logic, "_persist_global_settings", None)
        if callable(persist):
            persist()

    def _refresh_pdf_ingestion(self):
        if self._running or not self._is_pdf_source():
            return
        self._save_pdf_settings()
        invalidate = getattr(self.logic, "invalidate_pdf_ingestion_cache", None)
        if callable(invalidate):
            invalidate()
        self._on_deterministic_settings_changed()

    def _start_cleaning(self):
        if self._running:
            return

        self._result = None
        self._save_pdf_settings()
        self._stop_event.clear()
        self.diff_edit.clear()
        self.report_edit.clear()
        self.status_label.setText("Starting source-cleaning loop...")
        self._set_running(True)

        remove_footnotes = self.remove_footnotes_checkbox.isChecked()
        filter_citations = self.filter_citations_checkbox.isChecked()
        model_name = self.model_combo.currentText().strip() or "default"
        phase_max_iterations = self._current_phase_max_iterations()
        stop_event = self._stop_event
        extracted_text = self.text_edit.toPlainText()

        def worker():
            try:
                result = self.logic.run_source_cleaning(
                    source_path_hint=self.source_path_hint,
                    remove_footnotes=remove_footnotes,
                    filter_citations=filter_citations,
                    model_name=model_name,
                    phase_max_iterations=phase_max_iterations,
                    progress_callback=self.status_updated.emit,
                    stop_event=stop_event,
                    extracted_text=extracted_text,
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

    def _stop_cleaning(self):
        if self._running:
            self._stop_event.set()
            self.stop_button.setEnabled(False)
            self.status_label.setText("Stop requested — waiting for the current LLM turn to finish...")

    def _on_status_updated(self, message: str):
        status = str(message or "Working...")
        self.status_label.setText(status)
        page_progress = re.search(r"\bpage\s+(\d+)/(\d+)\b", status, flags=re.IGNORECASE)
        if page_progress:
            current, total = (int(value) for value in page_progress.groups())
            self.progress_bar.setRange(0, max(total, 1))
            self.progress_bar.setValue(max(0, min(current, total)))
        elif self._running:
            self.progress_bar.setRange(0, 0)

    def _on_cleaning_finished(self, result: object):
        self._worker_thread = None
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

    def _on_deterministic_settings_changed(self):
        is_remove_checked = self.remove_footnotes_checkbox.isChecked()
        self.filter_citations_checkbox.setEnabled(not is_remove_checked)
        
        if self._running:
            return
            
        remove_footnotes = is_remove_checked
        filter_citations = self.filter_citations_checkbox.isChecked()

        self.status_label.setText("Refreshing deterministic text preview...")
        self._set_running(True, allow_stop=False)

        def worker():
            try:
                text = self.logic.extract_deterministic_clean_text(
                    self.source_path_hint,
                    remove_footnotes=remove_footnotes,
                    filter_citations=filter_citations,
                    progress_callback=self.status_updated.emit,
                )
                result = {"success": True, "text": text}
            except Exception as e:
                result = {"success": False, "error": str(e)}
            self.preview_finished.emit(result)

        self._worker_thread = threading.Thread(target=worker, daemon=True)
        self._worker_thread.start()

    def _on_preview_finished(self, result: object):
        self._worker_thread = None
        payload = result if isinstance(result, dict) else {"success": False, "error": "Invalid preview result."}
        self._set_running(False)
        if not payload.get("success"):
            self.status_label.setText(f"Failed to refresh preview: {payload.get('error')}")
            return

        text = str(payload.get("text") or "")
        self.text_edit.setPlainText(text)
        self.text_edit.moveCursor(QTextCursor.MoveOperation.Start)
        self.tabs.setTabText(0, "Source Preview")
        self.status_label.setText(f"Preview refreshed: {len(text):,} characters.")
        self._show_pdf_ingestion_review_data()
        self._refresh_action_state()

    def _accept_cleaned_text(self):
        edited_text = self.text_edit.toPlainText()
        if not edited_text.strip():
            QMessageBox.warning(self, "Source Cleaning", "Cleaned text is empty.")
            return
        if self._result is None:
            self._result = {
                "success": True,
                "cleaned_text": edited_text,
                "user_edited": True,
                "metadata": {
                    "title": self.title_edit.text(),
                    "album": self.album_edit.text(),
                    "artist": self.artist_edit.text(),
                    "genre": self.genre_edit.text(),
                    "language": self.language_edit.text(),
                }
            }
        else:
            original_text = str(self._result.get("cleaned_text") or "")
            self._result["cleaned_text"] = edited_text
            self._result["user_edited"] = edited_text != original_text
            self._result["metadata"] = {
                "title": self.title_edit.text(),
                "album": self.album_edit.text(),
                "artist": self.artist_edit.text(),
                "genre": self.genre_edit.text(),
                "language": self.language_edit.text(),
            }
            
        self._choice = "accept"
        self.accept()

    def _cancel_import(self):
        self._choice = "cancel"
        self.reject()

    def closeEvent(self, event):
        if self._running:
            event.ignore()
            QMessageBox.information(
                self,
                "Source Processing Running",
                "Please wait for the current source-processing operation to finish before closing this dialog.",
            )
            return
        self._save_pdf_settings()
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
