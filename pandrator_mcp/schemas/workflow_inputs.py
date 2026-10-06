"""Portable arguments for selecting exact workflow inputs."""

from __future__ import annotations

from typing import Literal

from pydantic import Field, model_validator

from .common import ToolInput


class GetWorkflowInputsInput(ToolInput):
    """Read the compact input manifest for one session."""

    session_id: str = Field(min_length=1, max_length=80)


class SelectWorkflowInputInput(ToolInput):
    """Select an exact artifact as one workflow consumer's input."""

    session_id: str = Field(min_length=1, max_length=80)
    consumer: Literal["translation", "generation"]
    role: Literal["source", "correction", "translation", "prepared_text", "tts_optimized"]
    artifact_id: str = Field(min_length=1, max_length=80)
    expected_outcome_revision: int = Field(ge=0)
    expected_selection_revision: int = Field(ge=0)
    expected_translation_settings_revision: int | None = Field(default=None, ge=0)
    expected_text_settings_revision: int | None = Field(default=None, ge=0)
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )

    @model_validator(mode="after")
    def validate_consumer_role(self) -> "SelectWorkflowInputInput":
        if self.role in {"prepared_text", "tts_optimized"}:
            if self.consumer != "generation" or self.expected_text_settings_revision is None:
                raise ValueError("Audiobook text selection requires generation and its text settings revision.")
        elif self.expected_text_settings_revision is not None:
            raise ValueError("Text settings revision is only valid for audiobook text selection.")
        if self.consumer == "translation" and self.role == "translation":
            raise ValueError("Translation cannot use a translation artifact as its input.")
        if self.consumer == "translation" and self.expected_translation_settings_revision is None:
            raise ValueError("Translation input selection requires its settings revision.")
        if (
            self.consumer == "generation"
            and self.expected_translation_settings_revision is not None
        ):
            raise ValueError(
                "Translation settings revision is only used for translation input selection."
            )
        return self


class ConfigureSpeechOptimizationInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    mode: Literal["off", "document", "inline"]
    expected_outcome_revision: int = Field(ge=0)
    expected_text_settings_revision: int = Field(ge=0)
    annotation_mode: Literal["off", "dialogue", "speakers"] = "off"
    annotation_only: bool = False
    idempotency_key: str = Field(
        min_length=8, max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
    )

    @model_validator(mode="after")
    def validate_annotations(self) -> "ConfigureSpeechOptimizationInput":
        if self.annotation_mode != "off" and self.mode != "document":
            raise ValueError("Annotations require document optimization.")
        if self.annotation_only and self.annotation_mode == "off":
            raise ValueError("annotation_only requires an annotation mode.")
        return self


WORKFLOW_INPUTS_INPUT_MODELS = (
    GetWorkflowInputsInput,
    SelectWorkflowInputInput,
    ConfigureSpeechOptimizationInput,
)


__all__ = [
    "GetWorkflowInputsInput",
    "SelectWorkflowInputInput",
    "ConfigureSpeechOptimizationInput",
    "WORKFLOW_INPUTS_INPUT_MODELS",
]
