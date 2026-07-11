import re

from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QHeaderView,
    QInputDialog,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)
from ..dialogs.llm_model_settings_dialog import LLMModelSettingsDialog
from ...logic.llm_handler import default_model_record
from .responsive_page import ScrollableSettingsPage, configure_form_grid

class ProvidersTab(QWidget):
    def __init__(self, logic, parent=None):
        super().__init__(parent)
        self.logic = logic
        self._pending_tts_adapter_config = None

        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)

        self.providers_tabs = QTabWidget()
        self.providers_tabs.addTab(self._create_tts_tab(), "TTS")
        self.providers_tabs.addTab(self._create_llm_tab(), "LLM")
        main_layout.addWidget(self.providers_tabs, 1)

        self._connect_signals()
        self.update_ui_from_state()
        self.logic.state_changed.connect(self.update_ui_from_state)

    def _create_group_label(self, text: str) -> QLabel:
        label = QLabel(text)
        font = QFont()
        font.setPointSize(14)
        font.setBold(True)
        label.setFont(font)
        return label

    def _create_llm_tab(self) -> QWidget:
        tab = ScrollableSettingsPage(page_object_name="llmProvidersPage")
        layout = tab.content_layout
        layout.addWidget(self._create_group_label("LLM Providers"))
        layout.addWidget(self._create_llm_frame())
        return tab

    def _create_tts_tab(self) -> QWidget:
        tab = ScrollableSettingsPage(page_object_name="ttsProvidersPage")
        layout = tab.content_layout
        layout.addWidget(self._create_group_label("First-Class TTS Services"))
        layout.addWidget(self._create_tts_service_frame())
        layout.addWidget(self._create_group_label("Custom TTS Providers"))
        layout.addWidget(self._create_tts_frame())
        return tab

    def _create_llm_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = configure_form_grid(
            QGridLayout(frame), label_width=180, trailing_column=4
        )

        self.llm_provider_combo = QComboBox()
        self.llm_provider_type_combo = QComboBox()
        self.llm_provider_type_combo.setEditable(True)
        self.llm_provider_type_combo.addItems(
            [
                "openai",
                "anthropic",
                "gemini",
                "openrouter",
                "ollama",
                "groq",
                "mistral",
                "vertex_ai",
                "azure",
                "bedrock",
            ]
        )

        self.llm_provider_name_edit = QLineEdit()
        self.llm_provider_name_edit.setPlaceholderText("My Provider")
        self.llm_provider_api_base_edit = QLineEdit()
        self.llm_provider_api_base_edit.setPlaceholderText("http://localhost:11434/v1")
        self.llm_provider_api_key_edit = QLineEdit()
        self.llm_provider_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.llm_provider_api_key_edit.setPlaceholderText("Optional API key")
        self.llm_provider_models_table = QTableWidget(0, 4)
        self.llm_provider_models_table.setHorizontalHeaderLabels(
            ["Model", "Temperature", "Reasoning", "Custom pricing"]
        )
        self.llm_provider_models_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.llm_provider_models_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.llm_provider_models_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.llm_provider_models_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.llm_provider_models_table.setMinimumHeight(180)
        self.llm_add_model_button = QPushButton("Add Model")
        self.llm_delete_model_button = QPushButton("Delete Model")
        self.llm_model_settings_button = QPushButton("Model Settings")

        self.llm_new_provider_button = QPushButton("New")
        self.llm_save_provider_button = QPushButton("Save")
        self.llm_remove_provider_button = QPushButton("Remove")
        self.llm_refresh_builtin_models_button = QPushButton("Refresh Built-in Models")
        for button in (
            self.llm_new_provider_button,
            self.llm_save_provider_button,
            self.llm_remove_provider_button,
            self.llm_refresh_builtin_models_button,
        ):
            button.setMaximumWidth(240)

        self.llm_feedback_label = QLabel("")
        self.llm_feedback_label.setWordWrap(True)

        layout.addWidget(QLabel("Provider:"), 0, 0)
        layout.addWidget(self.llm_provider_combo, 0, 1)
        layout.addWidget(self.llm_new_provider_button, 0, 2)
        layout.addWidget(self.llm_remove_provider_button, 0, 3)

        layout.addWidget(QLabel("Display Name:"), 1, 0)
        layout.addWidget(self.llm_provider_name_edit, 1, 1, 1, 3)

        layout.addWidget(QLabel("LiteLLM Provider:"), 2, 0)
        layout.addWidget(self.llm_provider_type_combo, 2, 1, 1, 3)

        layout.addWidget(QLabel("API Base URL:"), 3, 0)
        layout.addWidget(self.llm_provider_api_base_edit, 3, 1, 1, 3)

        layout.addWidget(QLabel("API Key:"), 4, 0)
        layout.addWidget(self.llm_provider_api_key_edit, 4, 1, 1, 3)

        layout.addWidget(QLabel("Models:"), 5, 0)
        layout.addWidget(self.llm_provider_models_table, 5, 1, 1, 3)
        model_actions = QHBoxLayout()
        model_actions.addWidget(self.llm_add_model_button)
        model_actions.addWidget(self.llm_delete_model_button)
        model_actions.addWidget(self.llm_model_settings_button)
        model_actions.addStretch(1)
        layout.addLayout(model_actions, 6, 1, 1, 3)

        layout.addWidget(self.llm_save_provider_button, 7, 2)
        layout.addWidget(self.llm_refresh_builtin_models_button, 7, 3)
        layout.addWidget(self.llm_feedback_label, 8, 0, 1, 4)

        return frame

    def _create_tts_service_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = configure_form_grid(
            QGridLayout(frame), label_width=180, trailing_column=4
        )

        self.tts_service_combo = QComboBox()
        self.tts_service_api_base_edit = QLineEdit()
        self.tts_service_api_base_edit.setPlaceholderText("Service API base URL")
        self.tts_service_api_key_label = QLabel("API Key:")
        self.tts_service_api_key_edit = QLineEdit()
        self.tts_service_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tts_service_api_key_edit.setPlaceholderText("Optional API key")
        self.tts_service_models_label = QLabel("Models:")
        self.tts_service_models_edit = QTextEdit()
        self.tts_service_models_edit.setPlaceholderText("One model per line or comma-separated")
        self.tts_service_models_edit.setFixedHeight(70)
        self.tts_service_voices_label = QLabel("Voices:")
        self.tts_service_voices_edit = QTextEdit()
        self.tts_service_voices_edit.setPlaceholderText("One voice per line or comma-separated")
        self.tts_service_voices_edit.setFixedHeight(70)
        self.tts_service_save_button = QPushButton("Save Service Settings")
        self.tts_service_save_button.setMaximumWidth(240)
        self.tts_service_feedback_label = QLabel("")
        self.tts_service_feedback_label.setWordWrap(True)

        layout.addWidget(QLabel("Service:"), 0, 0)
        layout.addWidget(self.tts_service_combo, 0, 1, 1, 3)
        layout.addWidget(QLabel("API Base URL:"), 1, 0)
        layout.addWidget(self.tts_service_api_base_edit, 1, 1, 1, 3)
        layout.addWidget(self.tts_service_api_key_label, 2, 0)
        layout.addWidget(self.tts_service_api_key_edit, 2, 1, 1, 3)
        layout.addWidget(self.tts_service_models_label, 3, 0)
        layout.addWidget(self.tts_service_models_edit, 3, 1, 1, 3)
        layout.addWidget(self.tts_service_voices_label, 4, 0)
        layout.addWidget(self.tts_service_voices_edit, 4, 1, 1, 3)
        layout.addWidget(self.tts_service_save_button, 5, 3)
        layout.addWidget(self.tts_service_feedback_label, 6, 0, 1, 4)
        return frame

    def _create_tts_frame(self) -> QFrame:
        frame = QFrame()
        frame.setObjectName("groupFrame")
        layout = configure_form_grid(
            QGridLayout(frame), label_width=180, trailing_column=4
        )

        self.tts_provider_combo = QComboBox()
        self.tts_profile_combo = QComboBox()
        self.tts_profile_combo.addItem("Auto-detect / Manual", "")
        for profile in self.logic.list_tts_provider_profiles():
            self.tts_profile_combo.addItem(str(profile.get("name") or profile.get("id")), profile.get("id"))
        self.tts_apply_profile_button = QPushButton("Apply Profile")
        self.tts_profile_hint_label = QLabel("")
        self.tts_profile_hint_label.setWordWrap(True)
        self.tts_provider_type_combo = QComboBox()
        self.tts_provider_type_combo.addItems(["openai", "gemini"])
        self.tts_provider_name_edit = QLineEdit()
        self.tts_provider_name_edit.setPlaceholderText("My Custom TTS Provider")
        self.tts_provider_api_base_edit = QLineEdit()
        self.tts_provider_api_base_edit.setPlaceholderText("https://api.example.com/v1")
        self.tts_provider_api_key_edit = QLineEdit()
        self.tts_provider_api_key_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self.tts_provider_api_key_edit.setPlaceholderText("Optional API key")

        self.tts_provider_models_edit = QTextEdit()
        self.tts_provider_models_edit.setPlaceholderText("One model per line or comma-separated")
        self.tts_provider_models_edit.setFixedHeight(70)
        self.tts_provider_voices_edit = QTextEdit()
        self.tts_provider_voices_edit.setPlaceholderText("One voice per line or comma-separated")
        self.tts_provider_voices_edit.setFixedHeight(70)
        self.tts_provider_prebuilt_checkbox = QCheckBox("Provider offers pre-built voices")
        self.tts_provider_prebuilt_checkbox.setChecked(True)

        self.tts_new_provider_button = QPushButton("New")
        self.tts_save_provider_button = QPushButton("Save")
        self.tts_remove_provider_button = QPushButton("Remove")
        self.tts_test_connection_button = QPushButton("Test Connection")
        self.tts_auto_config_button = QPushButton("Auto-configure")
        self.tts_discover_catalog_button = QPushButton("Discover Models/Voices")
        for button in (
            self.tts_apply_profile_button,
            self.tts_new_provider_button,
            self.tts_save_provider_button,
            self.tts_remove_provider_button,
            self.tts_test_connection_button,
            self.tts_auto_config_button,
            self.tts_discover_catalog_button,
        ):
            button.setMaximumWidth(240)

        self.tts_adapter_summary_label = QLabel("")
        self.tts_adapter_summary_label.setWordWrap(True)
        self.tts_feedback_label = QLabel("")
        self.tts_feedback_label.setWordWrap(True)

        layout.addWidget(QLabel("Provider:"), 0, 0)
        layout.addWidget(self.tts_provider_combo, 0, 1)
        layout.addWidget(self.tts_new_provider_button, 0, 2)
        layout.addWidget(self.tts_remove_provider_button, 0, 3)

        layout.addWidget(QLabel("Wrapper Profile:"), 1, 0)
        layout.addWidget(self.tts_profile_combo, 1, 1, 1, 2)
        layout.addWidget(self.tts_apply_profile_button, 1, 3)
        layout.addWidget(self.tts_profile_hint_label, 2, 0, 1, 4)

        layout.addWidget(QLabel("Display Name:"), 3, 0)
        layout.addWidget(self.tts_provider_name_edit, 3, 1, 1, 3)

        layout.addWidget(QLabel("Compatibility Flavor:"), 4, 0)
        layout.addWidget(self.tts_provider_type_combo, 4, 1, 1, 3)

        layout.addWidget(QLabel("API Base URL:"), 5, 0)
        layout.addWidget(self.tts_provider_api_base_edit, 5, 1, 1, 3)

        layout.addWidget(QLabel("API Key:"), 6, 0)
        layout.addWidget(self.tts_provider_api_key_edit, 6, 1, 1, 3)

        layout.addWidget(QLabel("Models:"), 7, 0)
        layout.addWidget(self.tts_provider_models_edit, 7, 1, 1, 3)

        layout.addWidget(QLabel("Voices:"), 8, 0)
        layout.addWidget(self.tts_provider_voices_edit, 8, 1, 1, 3)
        layout.addWidget(self.tts_provider_prebuilt_checkbox, 9, 1, 1, 3)

        layout.addWidget(QLabel("Detected Mapping:"), 10, 0)
        layout.addWidget(self.tts_adapter_summary_label, 10, 1, 1, 3)

        layout.addWidget(self.tts_test_connection_button, 11, 0)
        layout.addWidget(self.tts_auto_config_button, 11, 1)
        layout.addWidget(self.tts_discover_catalog_button, 11, 2)
        layout.addWidget(self.tts_save_provider_button, 11, 3)
        layout.addWidget(self.tts_feedback_label, 12, 0, 1, 4)

        return frame

    def _connect_signals(self):
        self.llm_provider_combo.currentIndexChanged.connect(self._on_llm_provider_selected)
        self.llm_new_provider_button.clicked.connect(self._on_new_llm_provider)
        self.llm_save_provider_button.clicked.connect(self._on_save_llm_provider)
        self.llm_remove_provider_button.clicked.connect(self._on_remove_llm_provider)
        self.llm_refresh_builtin_models_button.clicked.connect(self._on_refresh_builtin_llm_models)
        self.llm_add_model_button.clicked.connect(self._on_add_llm_model)
        self.llm_delete_model_button.clicked.connect(self._on_delete_llm_model)
        self.llm_model_settings_button.clicked.connect(self._on_edit_llm_model)
        self.llm_provider_models_table.itemDoubleClicked.connect(
            lambda _item: self._on_edit_llm_model()
        )

        self.tts_provider_combo.currentIndexChanged.connect(self._on_tts_provider_selected)
        self.tts_service_combo.currentIndexChanged.connect(self._on_tts_service_selected)
        self.tts_service_save_button.clicked.connect(self._on_save_tts_service)
        self.tts_new_provider_button.clicked.connect(self._on_new_tts_provider)
        self.tts_save_provider_button.clicked.connect(self._on_save_tts_provider)
        self.tts_remove_provider_button.clicked.connect(self._on_remove_tts_provider)
        self.tts_test_connection_button.clicked.connect(self._on_test_tts_provider)
        self.tts_auto_config_button.clicked.connect(self._on_auto_configure_tts_provider)
        self.tts_discover_catalog_button.clicked.connect(self._on_discover_tts_provider_catalog)
        self.tts_profile_combo.currentIndexChanged.connect(self._on_tts_profile_selected)
        self.tts_apply_profile_button.clicked.connect(self._on_apply_tts_profile)

    @staticmethod
    def _parse_items(raw_text: str) -> list[str]:
        chunks = re.split(r"[,\n;]", str(raw_text or ""))
        seen: set[str] = set()
        values: list[str] = []
        for chunk in chunks:
            item = chunk.strip()
            if item and item not in seen:
                values.append(item)
                seen.add(item)
        return values

    @staticmethod
    def _format_items(items) -> str:
        if not isinstance(items, list):
            return ""
        return "\n".join(str(item).strip() for item in items if str(item).strip())

    def _llm_model_records(self) -> list[dict]:
        records: list[dict] = []
        for row in range(self.llm_provider_models_table.rowCount()):
            item = self.llm_provider_models_table.item(row, 0)
            record = item.data(Qt.ItemDataRole.UserRole) if item is not None else None
            if isinstance(record, dict):
                records.append(dict(record))
        return records

    def _set_llm_model_records(self, records) -> None:
        normalized = [
            dict(value) if isinstance(value, dict) else default_model_record(str(value))
            for value in (records if isinstance(records, list) else [])
        ]
        self.llm_provider_models_table.setRowCount(len(normalized))
        for row, record in enumerate(normalized):
            model_item = QTableWidgetItem(str(record.get("id") or ""))
            model_item.setData(Qt.ItemDataRole.UserRole, record)
            self.llm_provider_models_table.setItem(row, 0, model_item)
            temperature = record.get("default_temperature")
            self.llm_provider_models_table.setItem(
                row, 1, QTableWidgetItem("Omit" if temperature is None else f"{float(temperature):g}")
            )
            reasoning = str(record.get("default_reasoning_effort") or "")
            self.llm_provider_models_table.setItem(row, 2, QTableWidgetItem(reasoning or "Omit"))
            custom_pricing = (
                record.get("input_cost_per_million") is not None
                and record.get("output_cost_per_million") is not None
            )
            self.llm_provider_models_table.setItem(
                row, 3, QTableWidgetItem("Configured" if custom_pricing else "Provider")
            )

    def _selected_llm_model_row(self) -> int:
        rows = self.llm_provider_models_table.selectionModel().selectedRows()
        return rows[0].row() if rows else -1

    def _on_add_llm_model(self):
        model_id, ok = QInputDialog.getText(self, "Add LLM Model", "Model ID:")
        model_id = str(model_id or "").strip()
        if not ok or not model_id:
            return
        records = self._llm_model_records()
        if any(str(record.get("id") or "") == model_id for record in records):
            self.llm_feedback_label.setText(f"Model '{model_id}' is already listed.")
            return
        records.append(default_model_record(model_id))
        self._set_llm_model_records(records)
        self.llm_provider_models_table.selectRow(len(records) - 1)

    def _on_delete_llm_model(self):
        row = self._selected_llm_model_row()
        if row < 0:
            return
        records = self._llm_model_records()
        model_id = str(records[row].get("id") or "")
        provider_id = str(self.llm_provider_combo.currentData() or "")
        provider_key = self.llm_provider_type_combo.currentText().strip() or "openai"
        provider = next(
            (
                item
                for item in self._llm_providers()
                if str(item.get("id") or "") == provider_id
            ),
            {},
        )
        is_custom = bool(provider.get("is_custom", False))
        canonical = f"custom:{provider_id}/{model_id}" if is_custom else f"{provider_key}/{model_id}"
        replacement = ""
        if self.logic.state.llm.default_model in {canonical, model_id}:
            replacement_ids = [
                str(record.get("id") or "")
                for index, record in enumerate(records)
                if index != row and str(record.get("id") or "")
            ]
            if not replacement_ids:
                QMessageBox.warning(
                    self, "Default Model", "Add a replacement model before deleting the active default."
                )
                return
            replacement, accepted = QInputDialog.getItem(
                self,
                "Replace Default Model",
                "Select the new default model:",
                replacement_ids,
                0,
                False,
            )
            if not accepted or not replacement:
                return
        del records[row]
        self._set_llm_model_records(records)
        if replacement:
            self.logic.state.llm.default_model = (
                f"custom:{provider_id}/{replacement}"
                if is_custom else f"{provider_key}/{replacement}"
            )

    def _on_edit_llm_model(self):
        row = self._selected_llm_model_row()
        if row < 0:
            return
        records = self._llm_model_records()
        dialog = LLMModelSettingsDialog(records[row], self)
        if dialog.exec():
            records[row] = dialog.model_record()
            self._set_llm_model_records(records)
            self.llm_provider_models_table.selectRow(row)

    @staticmethod
    def _adapter_config_from_record(record: dict) -> dict:
        return {
            "adapter": str(record.get("adapter") or "openai_compatible"),
            "profile_id": str(record.get("profile_id") or ""),
            "speech_path": str(record.get("speech_path") or ""),
            "models_path": str(record.get("models_path") or ""),
            "voices_path": str(record.get("voices_path") or ""),
            "request_fields": dict(record.get("request_fields") or {}),
            "request_defaults": dict(record.get("request_defaults") or {}),
        }

    @staticmethod
    def _adapter_summary(config: dict | None) -> str:
        if not isinstance(config, dict):
            return "OpenAI-compatible adapter (default route discovery)."

        adapter = str(config.get("adapter") or "openai_compatible").replace("_", " ")
        speech_path = str(config.get("speech_path") or "automatic route candidates")
        request_fields = config.get("request_fields", {})
        mapping = []
        if isinstance(request_fields, dict):
            mapping = [
                f"{logical} -> {field}"
                for logical, field in request_fields.items()
                if str(field or "").strip()
            ]
        mapping_text = f" Fields: {', '.join(mapping)}." if mapping else ""
        return f"{adapter.title()}: POST {speech_path}.{mapping_text}"

    def _set_tts_adapter_config(self, config: dict | None):
        self._pending_tts_adapter_config = config
        self.tts_adapter_summary_label.setText(self._adapter_summary(config))

    def _llm_providers(self) -> list[dict]:
        return self.logic.list_llm_provider_configs()

    def _tts_providers(self) -> list[dict]:
        return self.logic.list_tts_provider_configs()

    def _tts_profiles(self) -> list[dict]:
        return self.logic.list_tts_provider_profiles()

    def _tts_services(self) -> list[dict]:
        return self.logic.list_tts_service_configs()

    def _refresh_llm_provider_dropdown(self, target_provider_id: str = ""):
        if not target_provider_id:
            target_provider_id = str(self.llm_provider_combo.currentData() or "")

        providers = self._llm_providers()
        self.llm_provider_combo.blockSignals(True)
        self.llm_provider_combo.clear()
        self.llm_provider_combo.addItem("New Custom Provider", "")
        for provider in providers:
            provider_id = str(provider.get("id") or "")
            provider_name = str(provider.get("name") or provider_id)
            self.llm_provider_combo.addItem(provider_name, provider_id)

        index = self.llm_provider_combo.findData(target_provider_id)
        self.llm_provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.llm_provider_combo.blockSignals(False)
        self._load_llm_provider_form(str(self.llm_provider_combo.currentData() or ""))

    def _refresh_tts_provider_dropdown(self, target_provider_id: str = ""):
        if not target_provider_id:
            target_provider_id = str(self.tts_provider_combo.currentData() or "")

        providers = self._tts_providers()
        self.tts_provider_combo.blockSignals(True)
        self.tts_provider_combo.clear()
        self.tts_provider_combo.addItem("New Custom Provider", "")
        for provider in providers:
            provider_id = str(provider.get("id") or "")
            provider_name = str(provider.get("name") or provider_id)
            self.tts_provider_combo.addItem(provider_name, provider_id)

        index = self.tts_provider_combo.findData(target_provider_id)
        self.tts_provider_combo.setCurrentIndex(index if index >= 0 else 0)
        self.tts_provider_combo.blockSignals(False)
        self._load_tts_provider_form(str(self.tts_provider_combo.currentData() or ""))

    def _refresh_tts_service_dropdown(self, target_service_id: str = ""):
        if not target_service_id:
            target_service_id = str(self.tts_service_combo.currentData() or "")

        services = self._tts_services()
        self.tts_service_combo.blockSignals(True)
        self.tts_service_combo.clear()
        for service in services:
            service_id = str(service.get("id") or "")
            service_name = str(service.get("name") or service_id)
            self.tts_service_combo.addItem(service_name, service_id)

        index = self.tts_service_combo.findData(target_service_id)
        self.tts_service_combo.setCurrentIndex(index if index >= 0 else 0)
        self.tts_service_combo.blockSignals(False)
        self._load_tts_service_form(str(self.tts_service_combo.currentData() or ""))

    def _load_llm_provider_form(self, provider_id: str):
        provider = next(
            (
                item
                for item in self._llm_providers()
                if str(item.get("id") or "") == provider_id
            ),
            None,
        )

        if provider is None:
            self.llm_provider_name_edit.setText("")
            self.llm_provider_type_combo.setCurrentText("openai")
            self.llm_provider_api_base_edit.setText("")
            self.llm_provider_api_key_edit.setText("")
            self._set_llm_model_records([])
            self.llm_remove_provider_button.setEnabled(False)
            return

        self.llm_provider_name_edit.setText(str(provider.get("name") or provider.get("id") or ""))
        self.llm_provider_type_combo.setCurrentText(str(provider.get("provider") or "openai"))
        self.llm_provider_api_base_edit.setText(str(provider.get("api_base") or ""))
        self.llm_provider_api_key_edit.setText(str(provider.get("api_key") or ""))
        self._set_llm_model_records(provider.get("models", []))
        self.llm_remove_provider_button.setEnabled(bool(provider.get("is_custom", False)))

    def _load_tts_provider_form(self, provider_id: str):
        provider = next(
            (
                item
                for item in self._tts_providers()
                if str(item.get("id") or "") == provider_id
            ),
            None,
        )

        if provider is None:
            self._set_tts_profile_selection("")
            self.tts_provider_name_edit.setText("")
            self.tts_provider_type_combo.setCurrentText("openai")
            self.tts_provider_api_base_edit.setText("")
            self.tts_provider_api_key_edit.setText("")
            self.tts_provider_models_edit.setPlainText("")
            self.tts_provider_voices_edit.setPlainText("")
            self.tts_provider_prebuilt_checkbox.setChecked(True)
            self.tts_provider_prebuilt_checkbox.setEnabled(True)
            self._set_tts_adapter_config(None)
            self.tts_remove_provider_button.setEnabled(False)
            self.tts_test_connection_button.setEnabled(False)
            self.tts_discover_catalog_button.setEnabled(False)
            return

        self._set_tts_profile_selection(str(provider.get("profile_id") or ""))
        self.tts_provider_name_edit.setText(str(provider.get("name") or provider.get("id") or ""))
        self.tts_provider_type_combo.setCurrentText(str(provider.get("provider") or "openai"))
        self.tts_provider_api_base_edit.setText(str(provider.get("api_base") or ""))
        self.tts_provider_api_key_edit.setText(str(provider.get("api_key") or ""))
        self.tts_provider_models_edit.setPlainText(self._format_items(provider.get("models", [])))
        self.tts_provider_voices_edit.setPlainText(self._format_items(provider.get("voices", [])))
        self._set_tts_adapter_config(self._adapter_config_from_record(provider))
        is_custom_provider = bool(provider.get("is_custom", False))
        self.tts_provider_prebuilt_checkbox.setChecked(
            bool(provider.get("supports_prebuilt_voices", bool(provider.get("voices", []))))
        )
        self.tts_provider_prebuilt_checkbox.setEnabled(is_custom_provider)
        self.tts_remove_provider_button.setEnabled(is_custom_provider)
        self.tts_test_connection_button.setEnabled(True)
        self.tts_discover_catalog_button.setEnabled(True)

    def _load_tts_service_form(self, service_id: str):
        service = next(
            (
                item
                for item in self._tts_services()
                if str(item.get("id") or "") == service_id
            ),
            None,
        )
        if service is None:
            return

        is_commercial = str(service.get("kind") or "") == "commercial"
        self.tts_service_api_base_edit.setText(str(service.get("api_base") or ""))
        self.tts_service_api_key_edit.setText(str(service.get("api_key") or ""))
        self.tts_service_models_edit.setPlainText(self._format_items(service.get("models", [])))
        self.tts_service_voices_edit.setPlainText(self._format_items(service.get("voices", [])))

        for widget in (
            self.tts_service_api_key_label,
            self.tts_service_api_key_edit,
            self.tts_service_models_label,
            self.tts_service_models_edit,
            self.tts_service_voices_label,
            self.tts_service_voices_edit,
        ):
            widget.setVisible(is_commercial)

    def _on_new_llm_provider(self):
        self.llm_provider_combo.setCurrentIndex(0)
        self._load_llm_provider_form("")

    def _on_new_tts_provider(self):
        self.tts_provider_combo.setCurrentIndex(0)
        self._load_tts_provider_form("")

    def _on_llm_provider_selected(self):
        self._load_llm_provider_form(str(self.llm_provider_combo.currentData() or ""))

    def _on_tts_provider_selected(self):
        self._load_tts_provider_form(str(self.tts_provider_combo.currentData() or ""))

    def _set_tts_profile_selection(self, profile_id: str):
        self.tts_profile_combo.blockSignals(True)
        index = self.tts_profile_combo.findData(str(profile_id or ""))
        self.tts_profile_combo.setCurrentIndex(index if index >= 0 else 0)
        self.tts_profile_combo.blockSignals(False)
        self._update_tts_profile_hint()

    def _selected_tts_profile(self) -> dict | None:
        profile_id = str(self.tts_profile_combo.currentData() or "")
        return next(
            (profile for profile in self._tts_profiles() if str(profile.get("id") or "") == profile_id),
            None,
        )

    def _update_tts_profile_hint(self):
        profile = self._selected_tts_profile()
        if profile is None:
            self.tts_profile_hint_label.setText(
                "Choose a known wrapper profile or enter a base URL and use Auto-configure."
            )
            return
        details = " ".join(
            part
            for part in [
                str(profile.get("description") or ""),
                str(profile.get("notes") or ""),
                f"Source: {profile.get('source_url')}" if profile.get("source_url") else "",
            ]
            if part
        )
        self.tts_profile_hint_label.setText(details)

    def _on_tts_profile_selected(self):
        self._update_tts_profile_hint()

    def _on_apply_tts_profile(self):
        profile = self._selected_tts_profile()
        if profile is None:
            self.tts_feedback_label.setText("Choose a wrapper profile to apply.")
            return

        self.tts_provider_name_edit.setText(str(profile.get("name") or "Custom TTS"))
        self.tts_provider_type_combo.setCurrentText(str(profile.get("provider") or "openai"))
        self.tts_provider_api_base_edit.setText(str(profile.get("api_base") or ""))
        self.tts_provider_models_edit.setPlainText(self._format_items(profile.get("models", [])))
        self.tts_provider_voices_edit.setPlainText(self._format_items(profile.get("voices", [])))
        self.tts_provider_prebuilt_checkbox.setChecked(bool(profile.get("supports_prebuilt_voices")))
        adapter_config = self._adapter_config_from_record(profile)
        adapter_config["profile_id"] = str(profile.get("id") or "")
        self._set_tts_adapter_config(adapter_config)
        self.tts_feedback_label.setText(
            "Profile applied. Adjust the base URL, port, models, or voices as needed, then click Save."
        )

    def _on_tts_service_selected(self):
        self._load_tts_service_form(str(self.tts_service_combo.currentData() or ""))

    def _on_save_tts_service(self):
        service_id = str(self.tts_service_combo.currentData() or "")
        success, error_message = self.logic.save_tts_service_config(
            service_id=service_id,
            api_base=self.tts_service_api_base_edit.text().strip(),
            api_key=self.tts_service_api_key_edit.text().strip(),
            models=self._parse_items(self.tts_service_models_edit.toPlainText()),
            voices=self._parse_items(self.tts_service_voices_edit.toPlainText()),
        )
        if not success:
            self.tts_service_feedback_label.setText(
                error_message or "Could not save first-class TTS service."
            )
            return

        self._refresh_tts_service_dropdown(service_id)
        self.tts_service_feedback_label.setText("Saved first-class TTS service settings.")

    def _on_save_llm_provider(self):
        provider_id = str(self.llm_provider_combo.currentData() or "")
        provider_name = self.llm_provider_name_edit.text().strip()
        provider_key = self.llm_provider_type_combo.currentText().strip()
        api_base = self.llm_provider_api_base_edit.text().strip()
        api_key = self.llm_provider_api_key_edit.text().strip()
        models = self._llm_model_records()

        success, resolved_provider_id, error_message = self.logic.save_llm_provider(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_key=provider_key,
            api_base=api_base,
            api_key=api_key,
            models=models,
        )
        if not success:
            self.llm_feedback_label.setText(error_message or "Could not save LLM provider.")
            return

        self._refresh_llm_provider_dropdown(resolved_provider_id)
        self.llm_feedback_label.setText(f"Saved LLM provider '{provider_name or resolved_provider_id}'.")

    def _on_remove_llm_provider(self):
        provider_id = str(self.llm_provider_combo.currentData() or "")
        if not provider_id:
            self.llm_feedback_label.setText("Select a custom LLM provider to remove.")
            return

        success, error_message = self.logic.remove_llm_provider(provider_id)
        if not success:
            self.llm_feedback_label.setText(error_message or "Could not remove LLM provider.")
            return
        self._refresh_llm_provider_dropdown("")
        self.llm_feedback_label.setText(f"Removed LLM provider '{provider_id}'.")

    def _on_refresh_builtin_llm_models(self):
        status_lines = self.logic.refresh_llm_builtin_models()
        self._refresh_llm_provider_dropdown(str(self.llm_provider_combo.currentData() or ""))
        if status_lines:
            self.llm_feedback_label.setText(" ".join(status_lines))
        else:
            self.llm_feedback_label.setText("Finished refreshing built-in model catalogs.")

    def _on_save_tts_provider(self):
        provider_id = str(self.tts_provider_combo.currentData() or "")
        provider_name = self.tts_provider_name_edit.text().strip()
        provider_type = self.tts_provider_type_combo.currentText().strip()
        api_base = self.tts_provider_api_base_edit.text().strip()
        api_key = self.tts_provider_api_key_edit.text().strip()
        models = self._parse_items(self.tts_provider_models_edit.toPlainText())
        voices = self._parse_items(self.tts_provider_voices_edit.toPlainText())
        supports_prebuilt_voices = self.tts_provider_prebuilt_checkbox.isChecked()

        success, resolved_provider_id, error_message = self.logic.save_tts_provider(
            provider_id=provider_id,
            provider_name=provider_name,
            provider_type=provider_type,
            api_base=api_base,
            api_key=api_key,
            models=models,
            voices=voices,
            supports_prebuilt_voices=supports_prebuilt_voices,
            adapter_config=self._pending_tts_adapter_config,
        )
        if not success:
            self.tts_feedback_label.setText(error_message or "Could not save TTS provider.")
            return

        self._refresh_tts_provider_dropdown(resolved_provider_id)
        self.tts_feedback_label.setText(f"Saved TTS provider '{provider_name or resolved_provider_id}'.")

    def _on_remove_tts_provider(self):
        provider_id = str(self.tts_provider_combo.currentData() or "")
        if not provider_id:
            self.tts_feedback_label.setText("Select a custom TTS provider to remove.")
            return

        success, error_message = self.logic.remove_tts_provider(provider_id)
        if not success:
            self.tts_feedback_label.setText(error_message or "Could not remove TTS provider.")
            return

        self._refresh_tts_provider_dropdown("")
        self.tts_feedback_label.setText(f"Removed TTS provider '{provider_id}'.")

    def _on_test_tts_provider(self):
        provider_id = str(self.tts_provider_combo.currentData() or "").strip()
        if not provider_id:
            self.tts_feedback_label.setText("Save and select a provider before testing connection.")
            return

        success, message = self.logic.test_tts_provider_connection(provider_id)
        if success:
            self.tts_feedback_label.setText(message or "Connection successful.")
            return

        self.tts_feedback_label.setText(message or "Connection failed.")

    def _on_auto_configure_tts_provider(self):
        base_url = self.tts_provider_api_base_edit.text().strip()
        if not base_url:
            self.tts_feedback_label.setText("Enter an API base URL before auto-configuring.")
            return

        result = self.logic.discover_tts_endpoint_config(
            base_url,
            self.tts_provider_api_key_edit.text().strip(),
        )
        if not result.get("success"):
            details = " ".join(result.get("evidence", []) + result.get("warnings", []))
            self.tts_feedback_label.setText(
                " ".join(part for part in [str(result.get("message") or "Auto-configuration failed."), details] if part)
            )
            return

        self._set_tts_profile_selection("")
        if not self.tts_provider_name_edit.text().strip():
            self.tts_provider_name_edit.setText(str(result.get("name") or "Custom TTS"))
        self.tts_provider_api_base_edit.setText(str(result.get("api_base") or base_url))
        self.tts_provider_type_combo.setCurrentText(str(result.get("provider") or "openai"))
        self.tts_provider_models_edit.setPlainText(self._format_items(result.get("models", [])))
        self.tts_provider_voices_edit.setPlainText(self._format_items(result.get("voices", [])))
        self.tts_provider_prebuilt_checkbox.setChecked(bool(result.get("supports_prebuilt_voices")))
        self._set_tts_adapter_config(self._adapter_config_from_record(result))

        details = " ".join(result.get("evidence", []) + result.get("warnings", []))
        self.tts_feedback_label.setText(
            " ".join(
                part
                for part in [
                    str(result.get("message") or "Auto-configuration complete."),
                    details,
                    "Review the detected mapping, then click Save.",
                ]
                if part
            )
        )

    def _on_discover_tts_provider_catalog(self):
        provider_id = str(self.tts_provider_combo.currentData() or "").strip()
        if not provider_id:
            self.tts_feedback_label.setText("Save and select a provider before discovery.")
            return

        success, models, voices, message = self.logic.discover_tts_provider_catalog(provider_id)
        if not success:
            self.tts_feedback_label.setText(message or "Could not discover provider catalog.")
            return

        self.tts_provider_models_edit.setPlainText(self._format_items(models))
        self.tts_provider_voices_edit.setPlainText(self._format_items(voices))
        self.tts_feedback_label.setText(
            f"{message} Review and click Save to persist discovered values."
        )

    def update_ui_from_state(self):
        self._refresh_llm_provider_dropdown(str(self.llm_provider_combo.currentData() or ""))
        self._refresh_tts_service_dropdown(str(self.tts_service_combo.currentData() or ""))
        self._refresh_tts_provider_dropdown(str(self.tts_provider_combo.currentData() or ""))
