import re
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QGridLayout,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
)

from ...constants import KOKORO_LANGUAGES, LANGUAGE_DISPLAY_NAMES, VOXTRAL_LANGUAGES


DEFAULT_PREVIEW_TEXT = "The quick brown fox jumps over the lazy dog."

KOKORO_PREFIX_LANGUAGE_CODES = {
    "a": "en",
    "b": "en-gb",
    "d": "de",
    "e": "es",
    "f": "fr",
    "h": "hi",
    "i": "it",
    "j": "ja",
    "p": "pt",
    "z": "zh-cn",
}

VOICE_GENDER_LABELS = {
    "f": "Female",
    "m": "Male",
    "female": "Female",
    "male": "Male",
}

VOXTRAL_STYLE_LABELS = {
    "casual": "Casual",
    "cheerful": "Cheerful",
    "neutral": "Neutral",
}

KOKORO_OPENAI_ALIAS_VOICES = {
    "alloy",
    "ash",
    "ballad",
    "cedar",
    "coral",
    "echo",
    "fable",
    "marin",
    "nova",
    "onyx",
    "sage",
    "shimmer",
    "verse",
}


KOKORO_NAMED_VOICE_META: dict[str, tuple[str, str]] = {
    "martin": ("d", "m"),
}

class VoiceCatalogDialog(QDialog):
    preview_generated = pyqtSignal(str, bool, str, str)
    generation_finished = pyqtSignal()

    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._voice_rows: dict[str, int] = {}
        self._voice_entries: list[dict] = []
        self._generation_thread: threading.Thread | None = None

        self.setWindowTitle("Browse Voices")
        self.resize(1120, 700)

        self._build_ui()
        self._connect_signals()
        self.reload_voices(use_remote=False)

    def _build_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setSpacing(8)

        self.description_label = QLabel(
            "Browse built-in voices for the active TTS service, organize by language and gender, and generate preview samples for quick comparison."
        )
        self.description_label.setWordWrap(True)
        self.description_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.description_label)

        self.service_label = QLabel("")
        self.service_label.setWordWrap(True)
        self.service_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.service_label)

        filter_layout = QHBoxLayout()
        filter_layout.addWidget(QLabel("Language:"))
        self.language_filter_combo = QComboBox(self)
        filter_layout.addWidget(self.language_filter_combo, 1)
        self.language_filter_hint = QLabel("")
        self.language_filter_hint.setObjectName("secondaryInfoLabel")
        self.language_filter_hint.setWordWrap(True)
        filter_layout.addWidget(self.language_filter_hint, 2)
        main_layout.addLayout(filter_layout)

        main_layout.addWidget(QLabel("Preview Text"))
        self.preview_text_edit = QTextEdit(self)
        self.preview_text_edit.setPlaceholderText("Type sample text to render with selected voices")
        self.preview_text_edit.setPlainText(DEFAULT_PREVIEW_TEXT)
        self.preview_text_edit.setFixedHeight(90)
        main_layout.addWidget(self.preview_text_edit)

        self.voice_table = QTableWidget(0, 5, self)
        self.voice_table.setHorizontalHeaderLabels([
            "Voice",
            "Language",
            "Gender",
            "Status",
            "Preview WAV",
        ])
        self.voice_table.verticalHeader().setVisible(False)
        self.voice_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.voice_table.setSelectionMode(QTableWidget.SelectionMode.ExtendedSelection)
        self.voice_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.voice_table.setAlternatingRowColors(True)
        self.voice_table.setStyleSheet("QTableWidget { alternate-background-color: #3a3446; }")

        header = self.voice_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        main_layout.addWidget(self.voice_table, 1)

        actions_layout = QGridLayout()

        self.refresh_button = QPushButton("Refresh Voices")
        self.generate_selected_button = QPushButton("Generate Selected")
        self.generate_all_button = QPushButton("Generate All")
        self.play_selected_button = QPushButton("Play Selected")
        self.use_selected_button = QPushButton("Use Selected Voice")

        actions_layout.addWidget(self.refresh_button, 0, 0)
        actions_layout.addWidget(self.generate_selected_button, 0, 1)
        actions_layout.addWidget(self.generate_all_button, 0, 2)
        actions_layout.addWidget(self.play_selected_button, 1, 1)
        actions_layout.addWidget(self.use_selected_button, 1, 2)

        main_layout.addLayout(actions_layout)

        self.status_label = QLabel("")
        self.status_label.setWordWrap(True)
        self.status_label.setObjectName("secondaryInfoLabel")
        main_layout.addWidget(self.status_label)

        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        self.buttons.rejected.connect(self.reject)
        self.buttons.accepted.connect(self.accept)
        self.close_button = self.buttons.button(QDialogButtonBox.StandardButton.Close)
        main_layout.addWidget(self.buttons)

    def _connect_signals(self):
        self.voice_table.itemSelectionChanged.connect(self._refresh_action_state)
        self.language_filter_combo.currentIndexChanged.connect(self._on_language_filter_changed)

        self.refresh_button.clicked.connect(lambda: self.reload_voices(use_remote=True))
        self.generate_selected_button.clicked.connect(self._on_generate_selected)
        self.generate_all_button.clicked.connect(self._on_generate_all)
        self.play_selected_button.clicked.connect(self._on_play_selected)
        self.use_selected_button.clicked.connect(self._on_use_selected_voice)

        self.preview_generated.connect(self._on_preview_generated)
        self.generation_finished.connect(self._on_generation_finished)

        self.logic.state_changed.connect(self._refresh_header)

    @staticmethod
    def _titleize_identifier(value: str) -> str:
        tokens = [token for token in re.split(r"[_\-]+", str(value or "").strip()) if token]
        return " ".join(token.capitalize() for token in tokens)

    @staticmethod
    def _split_weight_suffix(voice_token: str) -> tuple[str, str]:
        trimmed = str(voice_token or "").strip()
        weighted_match = re.fullmatch(r"(.+?)(\(\s*\d+(?:\.\d+)?\s*\))", trimmed)
        if not weighted_match:
            return trimmed, ""
        return weighted_match.group(1).strip(), f" {weighted_match.group(2)}"

    @staticmethod
    def _language_label_for_service(service: str, language_code: str) -> str:
        normalized = str(language_code or "").strip().lower()
        if service == "Kokoro" and normalized == "en":
            return "American English"
        if service == "Kokoro" and normalized == "en-gb":
            return "British English"
        return LANGUAGE_DISPLAY_NAMES.get(normalized, str(language_code or "").strip())

    @staticmethod
    def _normalize_language_code_for_service(service: str, language_value: str) -> str:
        normalized = str(language_value or "").strip().lower()
        if not normalized:
            return ""

        if service == "Kokoro":
            kokoro_aliases = {
                "en-us": "en",
                "pt-br": "pt",
                "fr-fr": "fr",
                "zh": "zh-cn",
            }
            normalized = kokoro_aliases.get(normalized, normalized)

        return normalized

    @staticmethod
    def _parse_kokoro_voice_name(raw_name: str) -> tuple[str, str]:
        normalized_name = str(raw_name or "").strip("_").strip()
        if not normalized_name:
            return "Voice", ""

        version_suffix = ""
        version_match = re.match(r"^v(\d+)_?(.*)$", normalized_name, flags=re.IGNORECASE)
        if version_match and version_match.group(2):
            normalized_name = version_match.group(2).strip("_").strip()
            version_suffix = f" (v{version_match.group(1)})"

        tokens = [token for token in re.split(r"[_\-]+", normalized_name) if token]
        display_name = " ".join(token.capitalize() for token in tokens) if tokens else "Voice"
        return display_name, version_suffix

    def _kokoro_component_meta(self, voice_token: str) -> tuple[str, str, str]:
        token_without_weight, weight_suffix = self._split_weight_suffix(voice_token)
        prefix, separator, voice_name = token_without_weight.partition("_")
        normalized_prefix = prefix.lower().strip()

        if separator and len(normalized_prefix) == 2:
            language_code = KOKORO_PREFIX_LANGUAGE_CODES.get(normalized_prefix[0], "")
            gender_label = VOICE_GENDER_LABELS.get(normalized_prefix[1], "")
            if language_code and gender_label:
                display_name, version_suffix = self._parse_kokoro_voice_name(voice_name)
                return f"{display_name}{version_suffix}{weight_suffix}", language_code, gender_label

        if normalized_prefix in KOKORO_OPENAI_ALIAS_VOICES and not separator:
            alias_name = self._titleize_identifier(normalized_prefix)
            return f"{alias_name}{weight_suffix}", "en", ""

        if not separator and normalized_prefix in KOKORO_NAMED_VOICE_META:
            lang_key, gender_key = KOKORO_NAMED_VOICE_META[normalized_prefix]
            language_code = KOKORO_PREFIX_LANGUAGE_CODES.get(lang_key, "")
            gender_label = VOICE_GENDER_LABELS.get(gender_key, "")
            if language_code:
                display_name = self._titleize_identifier(token_without_weight) or token_without_weight
                return f"{display_name}{weight_suffix}", language_code, gender_label

        fallback_name = self._titleize_identifier(token_without_weight)
        return (fallback_name or token_without_weight), "", ""

    def _build_kokoro_voice_entry(self, voice_id: str) -> dict:
        normalized_voice_id = str(voice_id or "").strip()
        parts = [part.strip() for part in normalized_voice_id.split("+") if part.strip()]

        if len(parts) > 1:
            part_meta = [self._kokoro_component_meta(part) for part in parts]
            display_label = "Blend: " + " + ".join(meta[0] for meta in part_meta)
            language_codes = [meta[1] for meta in part_meta if meta[1]]
            gender_labels = [meta[2] for meta in part_meta if meta[2]]
            language_code = language_codes[0] if language_codes and all(code == language_codes[0] for code in language_codes) else ""
            gender_label = gender_labels[0] if gender_labels and all(g == gender_labels[0] for g in gender_labels) else ""
        else:
            display_label, language_code, gender_label = self._kokoro_component_meta(normalized_voice_id)

        language_label = self._language_label_for_service("Kokoro", language_code) if language_code else "Other"
        return {
            "voice_id": normalized_voice_id,
            "display_label": display_label,
            "language_code": language_code,
            "language_label": language_label,
            "gender_label": gender_label,
        }

    def _build_voxtral_voice_entry(self, voice_id: str) -> dict:
        normalized_voice_id = str(voice_id or "").strip()
        lowered = normalized_voice_id.lower()
        left, separator, right = lowered.partition("_")

        language_code = ""
        language_label = "Other"
        gender_label = ""
        display_label = self._titleize_identifier(normalized_voice_id) or normalized_voice_id

        if separator:
            gender_label = VOICE_GENDER_LABELS.get(right, "")

            if left in VOXTRAL_STYLE_LABELS:
                language_code = "en"
                language_label = self._language_label_for_service("Voxtral", language_code)
                display_label = VOXTRAL_STYLE_LABELS[left]
                if gender_label:
                    display_label = f"{display_label} {gender_label}"
            elif left in VOXTRAL_LANGUAGES:
                language_code = left
                language_label = self._language_label_for_service("Voxtral", language_code)
                if gender_label:
                    display_label = f"Standard {gender_label}"
                else:
                    display_label = "Standard"

        if not language_code and left in VOXTRAL_LANGUAGES:
            language_code = left
            language_label = self._language_label_for_service("Voxtral", language_code)

        return {
            "voice_id": normalized_voice_id,
            "display_label": display_label,
            "language_code": language_code,
            "language_label": language_label,
            "gender_label": gender_label,
        }

    def _build_silero_voice_entry(self, voice_id: str) -> dict:
        normalized_voice_id = str(voice_id or "").strip()
        current_language_name = str(self.logic.state.tts.language or "").strip() or "Silero"
        return {
            "voice_id": normalized_voice_id,
            "display_label": self._titleize_identifier(normalized_voice_id) or normalized_voice_id,
            "language_code": current_language_name.lower(),
            "language_label": current_language_name,
            "gender_label": "",
        }

    def _build_generic_voice_entry(self, service: str, voice_id: str) -> dict:
        normalized_voice_id = str(voice_id or "").strip()
        current_language = self._normalize_language_code_for_service(service, str(self.logic.state.tts.language or ""))
        language_label = self._language_label_for_service(service, current_language) if current_language else ""
        return {
            "voice_id": normalized_voice_id,
            "display_label": normalized_voice_id,
            "language_code": current_language,
            "language_label": language_label,
            "gender_label": "",
        }

    def _build_voice_entry(self, service: str, voice_id: str) -> dict:
        if service == "Kokoro":
            return self._build_kokoro_voice_entry(voice_id)
        if service == "Voxtral":
            return self._build_voxtral_voice_entry(voice_id)
        if service == "Silero":
            return self._build_silero_voice_entry(voice_id)
        return self._build_generic_voice_entry(service, voice_id)

    def _refresh_header(self):
        service = str(self.logic.state.tts.service or "").strip() or "Unknown"
        model = str(self.logic.state.tts.xtts_model or "").strip() or "(auto)"
        provider = str(self.logic.state.tts.openai_audio_endpoint or "").strip()

        if service == "OpenAI-Compatible" and provider:
            service_text = f"Active service: {service} ({provider})"
        else:
            service_text = f"Active service: {service}"

        self.service_label.setText(f"{service_text} | Active model: {model}")

    def _is_generating(self) -> bool:
        return self._generation_thread is not None and self._generation_thread.is_alive()

    def _selected_voice_ids(self) -> list[str]:
        selected_rows = sorted({item.row() for item in self.voice_table.selectedItems()})
        voices: list[str] = []
        seen: set[str] = set()
        for row in selected_rows:
            voice_item = self.voice_table.item(row, 0)
            if voice_item is None:
                continue
            voice_id = str(voice_item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if not voice_id:
                continue
            lowered = voice_id.lower()
            if lowered in seen:
                continue
            seen.add(lowered)
            voices.append(voice_id)
        return voices

    def _selected_voice_id(self) -> str:
        voices = self._selected_voice_ids()
        return voices[0] if voices else ""

    def _first_row_for_voice(self, voice_id: str) -> int:
        return int(self._voice_rows.get(str(voice_id or "").strip(), -1))

    def _active_service(self) -> str:
        return str(self.logic.state.tts.service or "").strip()

    def _language_filter_options(self, service: str, voice_entries: list[dict]) -> list[tuple[str, str]]:
        if service == "Kokoro":
            return [
                (self._language_label_for_service("Kokoro", code), code)
                for code in KOKORO_LANGUAGES
            ]

        if service == "Voxtral":
            present_codes = {
                str(entry.get("language_code") or "").strip().lower()
                for entry in voice_entries
                if str(entry.get("language_code") or "").strip()
            }
            ordered_codes = [code for code in VOXTRAL_LANGUAGES if code in present_codes]
            if "en" in present_codes and "en" not in ordered_codes:
                ordered_codes.insert(0, "en")
            return [
                (self._language_label_for_service("Voxtral", code), code)
                for code in ordered_codes
            ]

        if service == "Silero":
            current_language_name = str(self.logic.state.tts.language or "").strip()
            if not current_language_name:
                return []
            return [(current_language_name, current_language_name.lower())]

        return []

    def _preferred_language_filter_code(self, service: str, options: list[tuple[str, str]]) -> str:
        if not options:
            return ""

        if service == "Silero":
            return str(options[0][1] or "")

        preferred_code = self._normalize_language_code_for_service(
            service,
            str(self.logic.state.tts.language or ""),
        )
        option_codes = {str(code or "") for _, code in options}
        if preferred_code in option_codes:
            return preferred_code
        return str(options[0][1] or "")

    def _configure_language_filter(self, service: str, voice_entries: list[dict]):
        options = self._language_filter_options(service, voice_entries)
        selected_code_before = str(self.language_filter_combo.currentData() or "")

        self.language_filter_combo.blockSignals(True)
        self.language_filter_combo.clear()

        for label, code in options:
            self.language_filter_combo.addItem(label, code)

        if options:
            selected_code = selected_code_before if selected_code_before in {code for _, code in options} else self._preferred_language_filter_code(service, options)
            target_index = self.language_filter_combo.findData(selected_code)
            if target_index < 0:
                target_index = 0
            self.language_filter_combo.setCurrentIndex(target_index)
            self.language_filter_combo.setVisible(True)
            self.language_filter_hint.setVisible(True)
            if service == "Kokoro":
                self.language_filter_hint.setText("Kokoro preview generation is limited to the selected supported language.")
            elif service == "Voxtral":
                self.language_filter_hint.setText("Voxtral voices are grouped by language and gender.")
            else:
                self.language_filter_hint.setText("Silero voices are shown for the currently selected Silero language.")
        else:
            self.language_filter_combo.setVisible(False)
            self.language_filter_hint.setVisible(False)

        self.language_filter_combo.blockSignals(False)

    def _filtered_voice_entries(self) -> list[dict]:
        service = self._active_service()
        selected_code = str(self.language_filter_combo.currentData() or "").strip().lower()

        entries = list(self._voice_entries)
        if selected_code:
            entries = [
                entry
                for entry in entries
                if str(entry.get("language_code") or "").strip().lower() == selected_code
            ]

        gender_order = {"Female": 0, "Male": 1, "": 2}
        entries.sort(
            key=lambda entry: (
                str(entry.get("language_label") or "").lower(),
                gender_order.get(str(entry.get("gender_label") or ""), 99),
                str(entry.get("display_label") or "").lower(),
            )
        )

        if service == "Kokoro" and selected_code:
            entries = [
                entry
                for entry in entries
                if str(entry.get("language_code") or "").strip().lower() == selected_code
            ]

        return entries

    def reload_voices(self, *, use_remote: bool):
        if self._is_generating():
            return

        self._refresh_header()

        if not self.logic.tts_service_supports_prebuilt_voices():
            self.voice_table.setRowCount(0)
            self._voice_rows = {}
            self._voice_entries = []
            self.status_label.setText(
                "Current service does not expose built-in voices. Use Manage Voice Samples for voice-cloning services."
            )
            self._configure_language_filter(self._active_service(), [])
            self._refresh_action_state()
            return

        service = self._active_service()
        voices = self.logic.list_active_prebuilt_voices(
            use_remote=use_remote,
            update_state=False,
        )
        self._voice_entries = [self._build_voice_entry(service, voice_id) for voice_id in voices]
        self._configure_language_filter(service, self._voice_entries)
        self._render_voice_table()

    def _render_voice_table(self):
        filtered_entries = self._filtered_voice_entries()
        self.voice_table.setRowCount(len(filtered_entries))
        self._voice_rows = {}

        for row, entry in enumerate(filtered_entries):
            voice_id = str(entry.get("voice_id") or "").strip()
            display_label = str(entry.get("display_label") or voice_id)
            language_code = str(entry.get("language_code") or "").strip().lower()
            language_label = str(entry.get("language_label") or "")
            gender_label = str(entry.get("gender_label") or "")

            self._voice_rows[voice_id] = row

            voice_item = QTableWidgetItem(display_label)
            voice_item.setData(Qt.ItemDataRole.UserRole, voice_id)
            voice_item.setToolTip(f"{display_label}\nID: {voice_id}")
            self.voice_table.setItem(row, 0, voice_item)

            language_item = QTableWidgetItem(language_label)
            self.voice_table.setItem(row, 1, language_item)

            gender_item = QTableWidgetItem(gender_label)
            self.voice_table.setItem(row, 2, gender_item)

            preview_path = self.logic.get_prebuilt_voice_preview_path(
                voice_id,
                language_code=language_code,
                existing_only=True,
            )
            status_item = QTableWidgetItem("Ready" if preview_path else "Not generated")
            preview_item = QTableWidgetItem(preview_path)
            if preview_path:
                preview_item.setToolTip(preview_path)

            self.voice_table.setItem(row, 3, status_item)
            self.voice_table.setItem(row, 4, preview_item)

        if filtered_entries:
            current_speaker = str(self.logic.state.tts.speaker or "").strip()
            target_row = self._first_row_for_voice(current_speaker)
            if target_row < 0:
                target_row = 0
            self.voice_table.selectRow(target_row)
            self.status_label.setText(
                "Tip: Select voices, generate previews, then click 'Use Selected Voice' to apply your choice."
            )
        else:
            self.status_label.setText(
                "No voices available for the selected language."
            )

        self._refresh_action_state()

    def _refresh_action_state(self):
        has_rows = self.voice_table.rowCount() > 0
        has_selection = bool(self._selected_voice_ids())
        generating = self._is_generating()
        selected_voice = self._selected_voice_id()

        selected_row = self._first_row_for_voice(selected_voice)
        selected_preview_path = ""
        if selected_row >= 0:
            preview_item = self.voice_table.item(selected_row, 4)
            if preview_item is not None:
                selected_preview_path = str(preview_item.text() or "").strip()

        has_preview = bool(selected_preview_path)

        self.refresh_button.setEnabled((not generating) and self.logic.tts_service_supports_prebuilt_voices())
        self.generate_all_button.setEnabled((not generating) and has_rows)
        self.generate_selected_button.setEnabled((not generating) and has_selection)
        self.play_selected_button.setEnabled((not generating) and has_preview)
        self.use_selected_button.setEnabled((not generating) and has_selection)
        self.language_filter_combo.setEnabled((not generating) and self.language_filter_combo.count() > 0)
        if self.close_button is not None:
            self.close_button.setEnabled(not generating)

    def _on_language_filter_changed(self, _index: int = -1):
        if self._is_generating():
            return
        self._render_voice_table()

    def _on_generate_selected(self):
        voices = self._selected_voice_ids()
        if not voices:
            QMessageBox.information(self, "Voice Preview", "Select at least one voice first.")
            return
        self._start_generation(voices)

    def _on_generate_all(self):
        voices: list[str] = []
        for row in range(self.voice_table.rowCount()):
            item = self.voice_table.item(row, 0)
            if item is None:
                continue
            voice_id = str(item.data(Qt.ItemDataRole.UserRole) or "").strip()
            if voice_id:
                voices.append(voice_id)

        if not voices:
            QMessageBox.information(self, "Voice Preview", "No voices available for generation.")
            return

        self._start_generation(voices)

    def _row_language_code(self, row: int) -> str:
        language_item = self.voice_table.item(row, 1)
        if language_item is None:
            return ""

        language_label = str(language_item.text() or "").strip().lower()
        if not language_label:
            return ""

        service = self._active_service()
        options = self._language_filter_options(service, self._voice_entries)
        for label, code in options:
            if str(label or "").strip().lower() == language_label:
                return str(code or "").strip().lower()

        if service == "Silero":
            return str(self.language_filter_combo.currentData() or "").strip().lower()

        return ""

    def _start_generation(self, voices: list[str]):
        if self._is_generating():
            return

        preview_text = self.preview_text_edit.toPlainText().strip()
        if not preview_text:
            QMessageBox.warning(self, "Voice Preview", "Preview text cannot be empty.")
            return

        service = self._active_service()
        selected_language_code = str(self.language_filter_combo.currentData() or "").strip().lower()

        generation_targets: list[tuple[str, str]] = []
        for voice_id in voices:
            row = self._first_row_for_voice(voice_id)
            if row < 0:
                continue

            language_code = self._row_language_code(row) or selected_language_code
            if service == "Kokoro":
                if not selected_language_code:
                    continue
                if language_code != selected_language_code:
                    continue

            generation_targets.append((voice_id, language_code))

            status_item = self.voice_table.item(row, 3)
            if status_item is None:
                status_item = QTableWidgetItem()
                self.voice_table.setItem(row, 3, status_item)
            status_item.setText("Generating...")
            status_item.setToolTip("")

        if not generation_targets:
            if service == "Kokoro":
                QMessageBox.information(
                    self,
                    "Voice Preview",
                    "No voices matched the selected Kokoro language filter.",
                )
            else:
                QMessageBox.information(self, "Voice Preview", "No valid voices selected.")
            self._refresh_action_state()
            return

        self.status_label.setText(f"Generating {len(generation_targets)} preview sample(s)...")
        self._refresh_action_state()

        def worker():
            for voice_id, language_code in generation_targets:
                success, output_path, error_message = self.logic.generate_prebuilt_voice_preview_sample(
                    voice_id,
                    preview_text,
                    language_code=language_code,
                )
                self.preview_generated.emit(voice_id, success, output_path, error_message)
            self.generation_finished.emit()

        self._generation_thread = threading.Thread(target=worker, daemon=True)
        self._generation_thread.start()

    def _on_preview_generated(self, voice_id: str, success: bool, output_path: str, error_message: str):
        normalized_voice_id = str(voice_id or "").strip()
        row = self._first_row_for_voice(normalized_voice_id)
        if row < 0:
            return

        status_item = self.voice_table.item(row, 3)
        if status_item is None:
            status_item = QTableWidgetItem()
            self.voice_table.setItem(row, 3, status_item)

        preview_item = self.voice_table.item(row, 4)
        if preview_item is None:
            preview_item = QTableWidgetItem()
            self.voice_table.setItem(row, 4, preview_item)

        if success and output_path:
            status_item.setText("Ready")
            status_item.setToolTip("")
            preview_item.setText(output_path)
            preview_item.setToolTip(output_path)
        else:
            status_item.setText("Failed")
            status_item.setToolTip(error_message or "Unknown error")
            preview_item.setText("")
            preview_item.setToolTip(error_message or "Unknown error")

        self._refresh_action_state()

    def _on_generation_finished(self):
        self._generation_thread = None
        self.status_label.setText("Preview generation finished.")
        self._refresh_action_state()

    def _on_play_selected(self):
        voice_id = self._selected_voice_id()
        if not voice_id:
            QMessageBox.information(self, "Voice Preview", "Select a voice first.")
            return

        row = self._first_row_for_voice(voice_id)
        if row < 0:
            QMessageBox.information(self, "Voice Preview", "Select a voice first.")
            return

        preview_item = self.voice_table.item(row, 4)
        preview_path = str(preview_item.text() if preview_item else "").strip()
        if not preview_path:
            QMessageBox.information(
                self,
                "Voice Preview",
                "Generate a preview for the selected voice first.",
            )
            return

        if not self.logic.play_audio_file(preview_path):
            QMessageBox.warning(self, "Voice Preview", "Could not play the selected preview WAV.")

    def _on_use_selected_voice(self):
        voice_id = self._selected_voice_id()
        if not voice_id:
            QMessageBox.information(self, "Voice Preview", "Select a voice first.")
            return

        self.logic.set_tts_speaker_voice(voice_id)
        self.status_label.setText(f"Selected voice: {voice_id}")

    def closeEvent(self, event):
        if self._is_generating():
            QMessageBox.information(
                self,
                "Voice Preview",
                "Please wait for preview generation to finish before closing this window.",
            )
            event.ignore()
            return
        super().closeEvent(event)
