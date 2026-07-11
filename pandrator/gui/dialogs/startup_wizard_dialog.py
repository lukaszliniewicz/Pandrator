from __future__ import annotations

import os
import re
import subprocess

from PyQt6.QtCore import pyqtSignal
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


class StartupWizardDialog(QDialog):
    open_providers_requested = pyqtSignal()
    open_voices_requested = pyqtSignal()
    open_rvc_requested = pyqtSignal()

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self.setWindowTitle("Welcome to Pandrator")
        self.resize(980, 720)
        root = QVBoxLayout(self)
        self.stack = QStackedWidget()
        root.addWidget(self.stack, 1)
        self._build_home_page()
        self._build_task_page()
        self._build_setup_page()
        footer = QHBoxLayout()
        self.dont_show_check = QCheckBox("Don't open this wizard automatically again")
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self._close_wizard)
        footer.addWidget(self.dont_show_check)
        footer.addStretch(1)
        footer.addWidget(self.close_button)
        root.addLayout(footer)
        self.refresh_sessions()
        if int(getattr(self.logic.state.wizard, "setup_completed_version", 0) or 0) < 1:
            self.stack.setCurrentWidget(self.setup_page)

    def _build_home_page(self):
        self.home_page = QWidget()
        layout = QVBoxLayout(self.home_page)
        title = QLabel("What would you like to do?")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        hint = QLabel("Resume a recent session or create a guided subtitle, voiceover, or audiobook workflow.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.sessions_list = QListWidget()
        self.sessions_list.itemDoubleClicked.connect(lambda _item: self._resume_session())
        layout.addWidget(self.sessions_list, 1)
        actions = QHBoxLayout()
        resume = QPushButton("Resume Selected")
        resume.clicked.connect(self._resume_session)
        new = QPushButton("Create New")
        new.setObjectName("primaryButton")
        new.clicked.connect(lambda: self.stack.setCurrentWidget(self.task_page))
        setup = QPushButton("Setup and Readiness")
        setup.clicked.connect(lambda: self.stack.setCurrentWidget(self.setup_page))
        actions.addWidget(resume)
        actions.addWidget(new)
        actions.addWidget(setup)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.stack.addWidget(self.home_page)

    def _build_task_page(self):
        self.task_page = QWidget()
        layout = QVBoxLayout(self.task_page)
        title = QLabel("Create a new guided workflow")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        form = QFormLayout()
        layout.addLayout(form)
        self.task_kind_combo = QComboBox()
        self.task_kind_combo.addItem("Create subtitles", "subtitles")
        self.task_kind_combo.addItem("Create a voiceover", "voiceover")
        self.task_kind_combo.addItem("Create an audiobook", "audiobook")
        form.addRow("Goal:", self.task_kind_combo)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Transcribe", "transcribe")
        self.preset_combo.addItem("Clean subtitles", "clean_subtitles")
        self.preset_combo.addItem("Translate subtitles", "translate_subtitles")
        self.preset_combo.addItem("Create voiceover", "voiceover")
        self.preset_combo.addItem("Custom", "custom")
        form.addRow("Outcome:", self.preset_combo)
        self.translate_voiceover_check = QCheckBox("Translate before generating voiceover")
        form.addRow("", self.translate_voiceover_check)
        self.source_mode_combo = QComboBox()
        self.source_mode_combo.addItem("Upload a file", "file")
        self.source_mode_combo.addItem("Reuse an indexed source", "existing")
        self.source_mode_combo.addItem("Download from URL", "url")
        self.source_mode_combo.addItem("Paste text", "paste")
        form.addRow("Source:", self.source_mode_combo)
        source_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Choose a file, indexed source, or enter a URL")
        self.source_browse_button = QPushButton("Choose")
        self.source_browse_button.clicked.connect(self._choose_source)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_browse_button)
        form.addRow("Source value:", source_row)
        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setPlaceholderText("Paste or write audiobook text")
        self.paste_edit.setVisible(False)
        layout.addWidget(self.paste_edit, 1)
        self.session_name_edit = QLineEdit()
        self.session_name_edit.setPlaceholderText("Derived from the source if left blank")
        form.addRow("Session name:", self.session_name_edit)
        explanation = QLabel(
            "The selected outcome prepares the stage checklist. Every stage can still be run separately or changed later."
        )
        explanation.setWordWrap(True)
        explanation.setObjectName("secondaryInfoLabel")
        layout.addWidget(explanation)
        actions = QHBoxLayout()
        back = QPushButton("Back")
        back.clicked.connect(lambda: self.stack.setCurrentWidget(self.home_page))
        create = QPushButton("Create Session")
        create.setObjectName("primaryButton")
        create.clicked.connect(self._create_session)
        actions.addWidget(back)
        actions.addStretch(1)
        actions.addWidget(create)
        layout.addLayout(actions)
        self.task_kind_combo.currentIndexChanged.connect(self._update_task_page)
        self.preset_combo.currentIndexChanged.connect(self._update_task_page)
        self.source_mode_combo.currentIndexChanged.connect(self._update_task_page)
        self._update_task_page()
        self.stack.addWidget(self.task_page)

    def _build_setup_page(self):
        self.setup_page = QWidget()
        layout = QVBoxLayout(self.setup_page)
        title = QLabel("Setup and readiness")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        uses = QLabel(
            "Pandrator uses LLMs for subtitle correction and translation, TTS-oriented text cleanup, "
            "and agentic checking after deterministic EPUB/PDF extraction. Cloud requests may incur cost "
            "and send text to the selected provider."
        )
        uses.setWordWrap(True)
        layout.addWidget(uses)
        self.gpu_label = QLabel(self._gpu_guidance())
        self.gpu_label.setWordWrap(True)
        self.gpu_label.setObjectName("secondaryInfoLabel")
        layout.addWidget(self.gpu_label)
        self.readiness_label = QLabel()
        self.readiness_label.setWordWrap(True)
        layout.addWidget(self.readiness_label)
        configure_llm = QPushButton("Configure LLM and TTS Providers")
        configure_llm.clicked.connect(self._open_providers)
        configure_voices = QPushButton("Add Reference Voices")
        configure_voices.clicked.connect(self.open_voices_requested)
        self.configure_rvc_button = QPushButton("Manage RVC Models")
        self.configure_rvc_button.clicked.connect(self._open_rvc)
        refresh = QPushButton("Refresh Readiness")
        refresh.clicked.connect(self._refresh_readiness)
        layout.addWidget(configure_llm)
        layout.addWidget(configure_voices)
        layout.addWidget(self.configure_rvc_button)
        layout.addWidget(refresh)
        layout.addStretch(1)
        note = QLabel(
            "Local-model guidance: use an instruction-tuned quantized model sized for available VRAM. "
            "Around 8 GB usually suits a 7–9B class model; larger 20–27B class models require substantially more headroom. "
            "LM Studio can expose an OpenAI-compatible endpoint that Pandrator can add as a custom provider."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        actions = QHBoxLayout()
        continue_button = QPushButton("Continue")
        continue_button.clicked.connect(self._finish_setup)
        actions.addStretch(1)
        actions.addWidget(continue_button)
        layout.addLayout(actions)
        self.stack.addWidget(self.setup_page)
        self._refresh_readiness()

    def refresh_sessions(self):
        self.sessions_list.clear()
        for row in self.logic.list_indexed_sessions()[:20]:
            name = str(row.get("session_name") or "")
            workflow = "Dubbing" if row.get("dubbing_mode") else "Audiobook"
            modified = str(row.get("config_modified_at") or row.get("indexed_at") or "")
            item = QListWidgetItem(f"{name}\n{workflow} · {modified}")
            item.setData(256, name)
            self.sessions_list.addItem(item)
        if self.sessions_list.count():
            self.sessions_list.setCurrentRow(0)

    def _resume_session(self):
        item = self.sessions_list.currentItem()
        if item is None:
            return
        self.logic.load_session(str(item.data(256) or ""))
        self._close_wizard()

    def _update_task_page(self):
        kind = self.task_kind_combo.currentData()
        is_audiobook = kind == "audiobook"
        self.preset_combo.setEnabled(not is_audiobook)
        self.translate_voiceover_check.setVisible(
            not is_audiobook and self.preset_combo.currentData() == "voiceover"
        )
        is_paste = self.source_mode_combo.currentData() == "paste"
        self.paste_edit.setVisible(is_paste)
        self.source_edit.setVisible(not is_paste)
        self.source_browse_button.setVisible(
            self.source_mode_combo.currentData() in {"file", "existing"}
        )
        if self.source_mode_combo.currentData() == "url":
            self.source_edit.setPlaceholderText("Paste a video or audio URL")
        else:
            self.source_edit.setPlaceholderText("Choose a file or indexed source")

    def _choose_source(self):
        mode = self.source_mode_combo.currentData()
        if mode == "file":
            path, _ = QFileDialog.getOpenFileName(self, "Choose Source", "", "Supported files (*.*)")
            if path:
                self.source_edit.setText(path)
                self._derive_name(path)
        elif mode == "existing":
            rows = self.logic.list_reusable_sources(limit=500)
            choices = [str(row.get("source_path") or "") for row in rows]
            if not choices:
                QMessageBox.information(self, "Sources", "No reusable indexed sources were found.")
                return
            chooser = QComboBox()
            chooser.addItems(choices)
            box = QMessageBox(self)
            box.setWindowTitle("Reuse Source")
            box.setText("Choose an indexed source:")
            box.layout().addWidget(chooser, 1, 0, 1, box.layout().columnCount())
            box.setStandardButtons(QMessageBox.StandardButton.Ok | QMessageBox.StandardButton.Cancel)
            if box.exec() == QMessageBox.StandardButton.Ok:
                self.source_edit.setText(chooser.currentText())
                self._derive_name(chooser.currentText())

    def _derive_name(self, source: str):
        if self.session_name_edit.text().strip():
            return
        stem = os.path.splitext(os.path.basename(str(source or "")))[0]
        stem = re.sub(r"[^\w -]+", "", stem).strip()
        self.session_name_edit.setText(stem or "New Session")

    def _unique_name(self, requested: str) -> str:
        base = str(requested or "New Session").strip() or "New Session"
        existing = {str(row.get("session_name") or "").lower() for row in self.logic.list_indexed_sessions()}
        candidate = base
        number = 2
        while candidate.lower() in existing:
            candidate = f"{base} {number}"
            number += 1
        return candidate

    def _create_session(self):
        mode = str(self.source_mode_combo.currentData() or "file")
        source = self.source_edit.text().strip()
        pasted = self.paste_edit.toPlainText().strip()
        if mode == "paste" and not pasted:
            QMessageBox.warning(self, "Source Required", "Enter text before creating the session.")
            return
        if mode != "paste" and not source:
            QMessageBox.warning(self, "Source Required", "Choose or enter a source first.")
            return
        self._derive_name(source)
        name = self._unique_name(self.session_name_edit.text())
        kind = str(self.task_kind_combo.currentData() or "audiobook")
        preset = "custom" if kind == "audiobook" else str(self.preset_combo.currentData() or "custom")
        stage_labels = {
            "transcribe": "Transcribe",
            "clean_subtitles": "Transcribe → Correct",
            "translate_subtitles": "Transcribe → Correct → Translate",
            "voiceover": (
                "Transcribe → Correct → Translate → Generate Audio"
                if self.translate_voiceover_check.isChecked()
                else "Transcribe → Correct → Generate Audio"
            ),
            "custom": "Open the regular workspace",
        }
        source_summary = "Pasted text" if mode == "paste" else source
        review = QMessageBox.question(
            self,
            "Review New Workflow",
            f"Session: {name}\nGoal: {kind.title()}\nSource: {source_summary}\nStages: {stage_labels.get(preset, preset)}\n\nCreate this workflow?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if review != QMessageBox.StandardButton.Yes:
            return
        self.logic.new_session(name)
        if mode == "paste":
            self.logic.save_pasted_text(pasted, mark_paragraphs=True)
        elif mode == "url":
            self.logic.download_from_url(source)
        else:
            self.logic.select_source_file(source)
        self.logic.apply_workflow_preset(
            kind,
            preset,
            translate_voiceover=self.translate_voiceover_check.isChecked(),
            source_hint="download.mp4" if mode == "url" else source,
        )
        self._close_wizard()

    def _gpu_guidance(self) -> str:
        try:
            result = subprocess.run(
                ["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode == 0 and result.stdout.strip():
                detected = result.stdout.strip()
                memory_match = re.search(r"([\d,]+)\s*MiB", detected, re.IGNORECASE)
                memory_mib = int(memory_match.group(1).replace(",", "")) if memory_match else 0
                if memory_mib >= 20_000:
                    guidance = "This tier can usually run quantized 20–27B instruction models with useful context headroom."
                elif memory_mib >= 12_000:
                    guidance = "This tier is generally comfortable with quantized 9–14B instruction models."
                elif memory_mib >= 7_000:
                    guidance = "Start with a quantized 7–9B instruction model and a conservative context size."
                else:
                    guidance = "Prefer a compact quantized 3–7B model or a cloud provider."
                return f"Detected GPU: {detected}. {guidance}"
        except (OSError, subprocess.SubprocessError):
            pass
        return "GPU VRAM could not be detected. Local-model size guidance will remain conservative."

    def _refresh_readiness(self):
        from ...logic.dubbing.stt_backends import detect_stt_backend_statuses

        statuses = detect_stt_backend_statuses()
        stt = ", ".join(
            f"{status.label}: {'ready' if status.installed else 'not installed'}"
            for status in statuses.values()
        )
        rvc = "ready" if self.logic.is_rvc_available() else "not available"
        self.configure_rvc_button.setEnabled(self.logic.is_rvc_available())
        self.configure_rvc_button.setToolTip(
            "" if self.logic.is_rvc_available() else "Install and start the RVC service before adding models."
        )
        providers = len(self.logic.list_llm_provider_configs())
        tts_services = self.logic.list_tts_service_configs()
        configured_tts = sum(
            1 for service in tts_services if str(service.get("api_base") or "").strip()
        )
        voices = len(self.logic.list_voice_library())
        self.readiness_label.setText(
            f"LLM providers: {providers} · TTS endpoints: {configured_tts}/{len(tts_services)} configured · "
            f"STT: {stt} · RVC: {rvc} · Reference voices: {voices}.\n"
            "Outputs are stored under the Pandrator Outputs directory. Review provider privacy and pricing before cloud use."
        )

    def _finish_setup(self):
        self.logic.state.wizard.setup_completed_version = 1
        self.logic._persist_global_settings(force=True)
        self.stack.setCurrentWidget(self.home_page)

    def _open_providers(self):
        self.open_providers_requested.emit()
        self.accept()

    def _open_rvc(self):
        if not self.logic.is_rvc_available():
            return
        self.open_rvc_requested.emit()
        self.accept()

    def _close_wizard(self):
        if self.dont_show_check.isChecked():
            self.logic.state.wizard.show_on_startup = False
            self.logic._persist_global_settings(force=True)
        self.accept()

    def closeEvent(self, event):
        if self.dont_show_check.isChecked():
            self.logic.state.wizard.show_on_startup = False
            self.logic._persist_global_settings(force=True)
        super().closeEvent(event)
