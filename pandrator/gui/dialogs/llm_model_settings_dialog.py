from __future__ import annotations

from typing import Any

from PyQt6.QtGui import QDoubleValidator
from PyQt6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QVBoxLayout,
)


class LLMModelSettingsDialog(QDialog):
    """Edits defaults and fallback pricing for one provider model."""

    def __init__(self, model_record: dict[str, Any], parent=None):
        super().__init__(parent)
        self._model_record = dict(model_record or {})
        self.setWindowTitle(f"Model Settings — {self._model_record.get('id', '')}")
        self.setMinimumWidth(520)

        layout = QVBoxLayout(self)
        note = QLabel(
            "Blank defaults are omitted from requests. Custom pricing is used only when "
            "the provider does not report a response cost. Rates are USD per 1M tokens."
        )
        note.setWordWrap(True)
        note.setObjectName("secondaryInfoLabel")
        layout.addWidget(note)

        form = QFormLayout()
        layout.addLayout(form)

        self.temperature_edit = QLineEdit()
        self.temperature_edit.setPlaceholderText("Omit (model default)")
        self.temperature_edit.setValidator(QDoubleValidator(0.0, 2.0, 6, self))
        temperature = self._model_record.get("default_temperature")
        if temperature is not None:
            self.temperature_edit.setText(f"{float(temperature):g}")
        form.addRow("Default temperature:", self.temperature_edit)

        self.reasoning_combo = QComboBox()
        self.reasoning_combo.setEditable(True)
        self.reasoning_combo.addItems(["Omit", "minimal", "low", "medium", "high"])
        self.reasoning_combo.setCurrentText(
            str(self._model_record.get("default_reasoning_effort") or "Omit")
        )
        form.addRow("Default reasoning:", self.reasoning_combo)

        self.input_cost_edit = self._cost_edit("Required for custom calculation")
        self.cached_cost_edit = self._cost_edit("Blank uses uncached input rate")
        self.output_cost_edit = self._cost_edit("Required for custom calculation")
        form.addRow("Uncached input:", self.input_cost_edit)
        form.addRow("Cached input:", self.cached_cost_edit)
        form.addRow("Output:", self.output_cost_edit)

        for widget, key in (
            (self.input_cost_edit, "input_cost_per_million"),
            (self.cached_cost_edit, "cached_input_cost_per_million"),
            (self.output_cost_edit, "output_cost_per_million"),
        ):
            value = self._model_record.get(key)
            if value is not None:
                widget.setText(f"{float(value):g}")

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _cost_edit(self, placeholder: str) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setValidator(QDoubleValidator(0.0, 1_000_000.0, 9, self))
        return edit

    @staticmethod
    def _optional_float(text: str) -> float | None:
        normalized = str(text or "").strip()
        return float(normalized) if normalized else None

    def model_record(self) -> dict[str, Any]:
        record = dict(self._model_record)
        record["default_temperature"] = self._optional_float(self.temperature_edit.text())
        reasoning = self.reasoning_combo.currentText().strip()
        record["default_reasoning_effort"] = "" if reasoning.lower() == "omit" else reasoning
        record["input_cost_per_million"] = self._optional_float(self.input_cost_edit.text())
        record["cached_input_cost_per_million"] = self._optional_float(self.cached_cost_edit.text())
        record["output_cost_per_million"] = self._optional_float(self.output_cost_edit.text())
        return record
