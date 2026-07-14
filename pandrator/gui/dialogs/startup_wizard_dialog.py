from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from PyQt6.QtCore import QSize, Qt, pyqtSignal
from PyQt6.QtGui import QIcon
from PyQt6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QCommandLinkButton,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)


WIZARD_ICON_DIR = Path(__file__).resolve().parents[2] / "assets" / "icons" / "wizard"


def wizard_icon(name: str) -> QIcon:
    return QIcon(str(WIZARD_ICON_DIR / f"{name}.svg"))


class WizardTile(QCommandLinkButton):
    def __init__(self, title: str, description: str, icon_name: str, parent=None):
        super().__init__(title, description, parent)
        self.setObjectName("wizardTile")
        self.setIcon(wizard_icon(icon_name))
        self.setIconSize(QSize(42, 42))
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)
        self.setMinimumHeight(92)


class StartupWizardDialog(QDialog):
    open_providers_requested = pyqtSignal()
    open_voices_requested = pyqtSignal()
    open_rvc_requested = pyqtSignal()

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self.setObjectName("startupWizard")
        self.setModal(False)
        self.setWindowTitle("Welcome to Pandrator")
        self.resize(1040, 780)
        root = QVBoxLayout(self)
        root.setContentsMargins(22, 20, 22, 16)
        root.setSpacing(14)
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

    @staticmethod
    def _section_header(layout: QVBoxLayout, eyebrow: str, title: str, body: str = "") -> None:
        eyebrow_label = QLabel(eyebrow.upper())
        eyebrow_label.setObjectName("wizardEyebrow")
        layout.addWidget(eyebrow_label)
        title_label = QLabel(title)
        title_label.setObjectName("wizardSectionTitle")
        layout.addWidget(title_label)
        if body:
            body_label = QLabel(body)
            body_label.setObjectName("wizardSectionBody")
            body_label.setWordWrap(True)
            layout.addWidget(body_label)

    @staticmethod
    def _info_card(title: str, body: str, icon_name: str = "") -> QFrame:
        card = QFrame()
        card.setObjectName("wizardInfoCard")
        card_layout = QHBoxLayout(card)
        card_layout.setContentsMargins(16, 14, 16, 14)
        if icon_name:
            icon_label = QLabel()
            icon_label.setPixmap(wizard_icon(icon_name).pixmap(38, 38))
            icon_label.setAlignment(Qt.AlignmentFlag.AlignTop)
            card_layout.addWidget(icon_label)
        text_layout = QVBoxLayout()
        title_label = QLabel(title)
        title_label.setObjectName("wizardCardTitle")
        body_label = QLabel(body)
        body_label.setObjectName("wizardSectionBody")
        body_label.setWordWrap(True)
        text_layout.addWidget(title_label)
        text_layout.addWidget(body_label)
        card_layout.addLayout(text_layout, 1)
        return card

    def _build_home_page(self):
        self.home_page = QWidget()
        layout = QVBoxLayout(self.home_page)
        layout.setSpacing(12)
        title = QLabel("What would you like to do?")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        hint = QLabel(
            "Pick up where you left off, start a guided media task, or review your local and cloud setup."
        )
        hint.setObjectName("wizardLead")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        action_grid = QGridLayout()
        action_grid.setSpacing(12)
        new_tile = WizardTile(
            "Create a new task",
            "Choose subtitles, voiceover, or audiobook and let Pandrator prepare the workspace.",
            "new-task",
        )
        new_tile.clicked.connect(lambda: self.stack.setCurrentWidget(self.task_page))
        setup_tile = WizardTile(
            "Setup and readiness",
            "Check providers, local components, voices, privacy, costs, and output location.",
            "setup",
        )
        setup_tile.clicked.connect(lambda: self.stack.setCurrentWidget(self.setup_page))
        action_grid.addWidget(new_tile, 0, 0)
        action_grid.addWidget(setup_tile, 0, 1)
        action_grid.setColumnStretch(0, 1)
        action_grid.setColumnStretch(1, 1)
        layout.addLayout(action_grid)

        self._section_header(
            layout,
            "Recent work",
            "Resume a session",
            "Your most recently updated sessions appear first.",
        )
        self.sessions_list = QListWidget()
        self.sessions_list.setObjectName("wizardSessionList")
        self.sessions_list.setSpacing(4)
        self.sessions_list.itemDoubleClicked.connect(lambda _item: self._resume_session())
        layout.addWidget(self.sessions_list, 1)
        actions = QHBoxLayout()
        resume = QPushButton("Resume selected session")
        resume.setIcon(wizard_icon("session"))
        resume.clicked.connect(self._resume_session)
        actions.addWidget(resume)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.stack.addWidget(self.home_page)

    def _build_task_page(self):
        self.task_page = QWidget()
        layout = QVBoxLayout(self.task_page)
        layout.setSpacing(10)
        title = QLabel("Create a new guided workflow")
        title.setObjectName("workspaceTitle")
        layout.addWidget(title)
        lead = QLabel("Choose the outcome first. Pandrator will reveal only the source and workflow options that matter.")
        lead.setObjectName("wizardLead")
        lead.setWordWrap(True)
        layout.addWidget(lead)

        self._section_header(layout, "Step 1", "What are you making?")
        self.task_kind_combo = QComboBox()
        self.task_kind_combo.addItem("Create an audiobook", "audiobook")
        self.task_kind_combo.addItem("Create subtitles", "subtitles")
        self.task_kind_combo.addItem("Create a voiceover", "voiceover")
        self.task_kind_combo.setVisible(False)
        self.task_kind_tiles: dict[str, WizardTile] = {}
        self.task_kind_group = QButtonGroup(self)
        self.task_kind_group.setExclusive(True)
        goal_grid = QGridLayout()
        goal_grid.setSpacing(10)
        goal_options = (
            ("audiobook", "Audiobook", "Turn a document or pasted text into long-form narration.", "audiobook"),
            ("subtitles", "Subtitles", "Transcribe, correct, translate, review, and export timed text.", "subtitles"),
            ("voiceover", "Voiceover", "Prepare subtitles and generate synchronized dubbing audio.", "voiceover"),
        )
        for index, (key, tile_title, description, icon_name) in enumerate(goal_options):
            tile = WizardTile(tile_title, description, icon_name)
            tile.setCheckable(True)
            self.task_kind_group.addButton(tile, index)
            self.task_kind_tiles[key] = tile
            goal_grid.addWidget(tile, 0, index)
            goal_grid.setColumnStretch(index, 1)
        self.task_kind_group.idClicked.connect(self.task_kind_combo.setCurrentIndex)
        layout.addLayout(goal_grid)

        workflow_row = QHBoxLayout()
        workflow_text = QVBoxLayout()
        workflow_label = QLabel("Step 2 · Choose how far Pandrator should prepare the workflow")
        workflow_label.setObjectName("wizardCardTitle")
        workflow_hint = QLabel("You can add, remove, or rerun stages later without losing completed artifacts.")
        workflow_hint.setObjectName("wizardSectionBody")
        workflow_hint.setWordWrap(True)
        workflow_text.addWidget(workflow_label)
        workflow_text.addWidget(workflow_hint)
        workflow_row.addLayout(workflow_text, 1)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("Transcribe", "transcribe")
        self.preset_combo.addItem("Clean subtitles", "clean_subtitles")
        self.preset_combo.addItem("Translate subtitles", "translate_subtitles")
        self.preset_combo.addItem("Create voiceover", "voiceover")
        self.preset_combo.addItem("Custom", "custom")
        self.preset_combo.setMinimumWidth(250)
        workflow_row.addWidget(self.preset_combo)
        layout.addLayout(workflow_row)
        self.translate_voiceover_check = QCheckBox("Translate before generating voiceover")
        layout.addWidget(self.translate_voiceover_check)

        self._section_header(layout, "Step 3", "Where is the source?")
        self.source_mode_combo = QComboBox()
        self.source_mode_combo.addItem("Upload a file", "file")
        self.source_mode_combo.addItem("Reuse an indexed source", "existing")
        self.source_mode_combo.addItem("Download from URL", "url")
        self.source_mode_combo.addItem("Paste text", "paste")
        self.source_mode_combo.setVisible(False)
        self.source_mode_tiles: dict[str, WizardTile] = {}
        self.source_mode_group = QButtonGroup(self)
        self.source_mode_group.setExclusive(True)
        source_grid = QGridLayout()
        source_grid.setSpacing(10)
        source_options = (
            ("file", "Upload file", "Choose media, subtitles, or a document from this computer.", "upload"),
            ("existing", "Reuse source", "Start from a source already indexed by another session.", "library"),
            ("url", "Download URL", "Fetch supported online video or audio into the new session.", "link"),
            ("paste", "Paste text", "Write or paste text directly into an audiobook session.", "paste"),
        )
        for index, (key, tile_title, description, icon_name) in enumerate(source_options):
            tile = WizardTile(tile_title, description, icon_name)
            tile.setCheckable(True)
            self.source_mode_group.addButton(tile, index)
            self.source_mode_tiles[key] = tile
            source_grid.addWidget(tile, index // 2, index % 2)
        source_grid.setColumnStretch(0, 1)
        source_grid.setColumnStretch(1, 1)
        self.source_mode_group.idClicked.connect(self.source_mode_combo.setCurrentIndex)
        layout.addLayout(source_grid)

        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        source_row = QHBoxLayout()
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Choose a file, indexed source, or enter a URL")
        self.source_browse_button = QPushButton("Choose")
        self.source_browse_button.clicked.connect(self._choose_source)
        source_row.addWidget(self.source_edit, 1)
        source_row.addWidget(self.source_browse_button)
        form.addRow("Source value:", source_row)
        self.session_name_edit = QLineEdit()
        self.session_name_edit.setPlaceholderText("Derived from the source if left blank")
        form.addRow("Session name:", self.session_name_edit)
        layout.addLayout(form)
        self.paste_edit = QPlainTextEdit()
        self.paste_edit.setPlaceholderText("Paste or write audiobook text")
        self.paste_edit.setMaximumHeight(120)
        self.paste_edit.setVisible(False)
        layout.addWidget(self.paste_edit)
        layout.addStretch(1)
        actions = QHBoxLayout()
        back = QPushButton("Back")
        back.setIcon(wizard_icon("session"))
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
        page_layout = QVBoxLayout(self.setup_page)
        title = QLabel("Setup and readiness")
        title.setObjectName("workspaceTitle")
        page_layout.addWidget(title)
        lead = QLabel(
            "Configure only what you plan to use. You can leave this page temporarily and return without losing your place."
        )
        lead.setObjectName("wizardLead")
        lead.setWordWrap(True)
        page_layout.addWidget(lead)

        scroll = QScrollArea()
        scroll.setObjectName("wizardSetupScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(4, 4, 10, 4)
        layout.setSpacing(12)

        self._section_header(
            layout,
            "1 · Understand",
            "Where Pandrator uses language models",
            "LLMs are optional helpers. Deterministic extraction and local generation continue to work without a cloud provider.",
        )
        layout.addWidget(
            self._info_card(
                "Four focused uses",
                "• Correct and restructure subtitles\n"
                "• Translate subtitle artifacts\n"
                "• Normalize generation segments for speech\n"
                "• Check and clean EPUB/PDF extraction in an agentic review loop",
                "providers",
            )
        )

        self._section_header(
            layout,
            "2 · Local hardware",
            "Choose a model size that fits",
            "VRAM guidance is capability-based so it remains useful as model families change.",
        )
        self.gpu_label = QLabel(self._gpu_guidance())
        self.gpu_label.setWordWrap(True)
        self.gpu_label.setObjectName("wizardSectionBody")
        gpu_card = QFrame()
        gpu_card.setObjectName("wizardInfoCard")
        gpu_layout = QHBoxLayout(gpu_card)
        gpu_icon = QLabel()
        gpu_icon.setPixmap(wizard_icon("gpu").pixmap(42, 42))
        gpu_layout.addWidget(gpu_icon, 0, Qt.AlignmentFlag.AlignTop)
        gpu_layout.addWidget(self.gpu_label, 1)
        layout.addWidget(gpu_card)

        self._section_header(
            layout,
            "3 · Configure",
            "Services and reusable voices",
            "Each tile opens the relevant workspace. A Return to Wizard control will remain available in the main window.",
        )
        component_grid = QGridLayout()
        component_grid.setSpacing(10)
        self.configure_provider_tile = WizardTile(
            "LLM and TTS providers",
            "Configure local OpenAI-compatible endpoints or commercial services.",
            "providers",
        )
        self.configure_provider_tile.clicked.connect(self._open_providers)
        self.configure_voice_tile = WizardTile(
            "Reference voices",
            "Add, record, preview, and transcribe reusable voice samples.",
            "voices",
        )
        self.configure_voice_tile.clicked.connect(self._open_voices)
        self.configure_rvc_button = WizardTile(
            "RVC models",
            "Manage conversion models when the optional RVC service is available.",
            "rvc",
        )
        self.configure_rvc_button.clicked.connect(self._open_rvc)
        component_grid.addWidget(self.configure_provider_tile, 0, 0)
        component_grid.addWidget(self.configure_voice_tile, 0, 1)
        component_grid.addWidget(self.configure_rvc_button, 1, 0, 1, 2)
        component_grid.setColumnStretch(0, 1)
        component_grid.setColumnStretch(1, 1)
        layout.addLayout(component_grid)

        self._section_header(
            layout,
            "4 · Review",
            "Privacy, cost, and output location",
        )
        review_card = QFrame()
        review_card.setObjectName("wizardInfoCard")
        review_layout = QHBoxLayout(review_card)
        review_icon = QLabel()
        review_icon.setPixmap(wizard_icon("privacy").pixmap(42, 42))
        review_layout.addWidget(review_icon, 0, Qt.AlignmentFlag.AlignTop)
        self.readiness_label = QLabel()
        self.readiness_label.setTextFormat(Qt.TextFormat.RichText)
        self.readiness_label.setWordWrap(True)
        review_layout.addWidget(self.readiness_label, 1)
        layout.addWidget(review_card)
        layout.addStretch(1)
        scroll.setWidget(content)
        page_layout.addWidget(scroll, 1)

        actions = QHBoxLayout()
        back_button = QPushButton("Back")
        back_button.clicked.connect(lambda: self.stack.setCurrentWidget(self.home_page))
        refresh = QPushButton("Refresh readiness")
        refresh.clicked.connect(self._refresh_readiness)
        continue_button = QPushButton("Continue")
        continue_button.setObjectName("primaryButton")
        continue_button.clicked.connect(self._finish_setup)
        actions.addWidget(back_button)
        actions.addWidget(refresh)
        actions.addStretch(1)
        actions.addWidget(continue_button)
        page_layout.addLayout(actions)
        self.stack.addWidget(self.setup_page)
        self._refresh_readiness()

    def refresh_sessions(self):
        self.sessions_list.clear()
        for row in self.logic.list_indexed_sessions()[:20]:
            name = str(row.get("session_name") or "")
            workflow = "Dubbing" if row.get("dubbing_mode") else "Audiobook"
            modified = str(row.get("config_modified_at") or row.get("indexed_at") or "")
            item = QListWidgetItem(wizard_icon("session"), f"{name}\n{workflow} · {modified}")
            item.setData(256, name)
            item.setSizeHint(QSize(0, 58))
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
        kind = str(self.task_kind_combo.currentData() or "subtitles")
        for key, tile in self.task_kind_tiles.items():
            tile.setChecked(key == kind)
        is_audiobook = kind == "audiobook"
        self.preset_combo.setEnabled(not is_audiobook)
        self.preset_combo.setToolTip(
            "Audiobook sources open the regular text workspace after import."
            if is_audiobook else "Choose which stages should be included initially."
        )
        self.translate_voiceover_check.setVisible(
            not is_audiobook and self.preset_combo.currentData() == "voiceover"
        )
        self.source_mode_tiles["paste"].setEnabled(is_audiobook)
        self.source_mode_tiles["url"].setEnabled(not is_audiobook)
        mode = str(self.source_mode_combo.currentData() or "file")
        if (mode == "paste" and not is_audiobook) or (mode == "url" and is_audiobook):
            self.source_mode_combo.setCurrentIndex(self.source_mode_combo.findData("file"))
            mode = "file"
        for key, tile in self.source_mode_tiles.items():
            tile.setChecked(key == mode)
        is_paste = mode == "paste"
        self.paste_edit.setVisible(is_paste)
        self.source_edit.setVisible(not is_paste)
        self.source_browse_button.setVisible(
            mode in {"file", "existing"}
        )
        if mode == "url":
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
                return (
                    f"Detected GPU: {detected}. {guidance} "
                    "LM Studio can expose the selected local model through an OpenAI-compatible endpoint."
                )
        except (OSError, subprocess.SubprocessError):
            pass
        return (
            "GPU VRAM could not be detected. Start with a compact quantized instruction model, "
            "or use a cloud provider. LM Studio can expose local models through an OpenAI-compatible endpoint."
        )

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
        self.configure_provider_tile.setDescription(
            f"{providers} LLM provider definitions · {configured_tts}/{len(tts_services)} TTS endpoints configured."
        )
        self.configure_voice_tile.setDescription(
            f"{voices} reference voice{'s' if voices != 1 else ''} available in the library."
        )
        self.configure_rvc_button.setDescription(
            "RVC service ready; models can be added now."
            if self.logic.is_rvc_available()
            else "Optional RVC service is not available; model upload remains disabled."
        )
        self.readiness_label.setText(
            f"<b>Current readiness</b><br>"
            f"LLM providers: {providers}<br>"
            f"TTS endpoints: {configured_tts}/{len(tts_services)} configured<br>"
            f"Local STT: {stt}<br>"
            f"RVC: {rvc}<br>"
            f"Reference voices: {voices}<br><br>"
            "<b>Before using cloud services</b><br>"
            "Text or audio may be sent to the provider you select, and API usage may incur cost. "
            "Credentials remain in provider settings; the wizard stores no secrets.<br><br>"
            "<b>Outputs</b><br>Generated files are stored under the Pandrator Outputs directory."
        )

    def _finish_setup(self):
        self.logic.state.wizard.setup_completed_version = 1
        self.logic._persist_global_settings(force=True)
        self.stack.setCurrentWidget(self.home_page)

    def _open_providers(self):
        self.hide()
        self.open_providers_requested.emit()

    def _open_voices(self):
        self.hide()
        self.open_voices_requested.emit()

    def _open_rvc(self):
        if not self.logic.is_rvc_available():
            return
        self.hide()
        self.open_rvc_requested.emit()

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
