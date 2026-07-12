"""Credential checks for Pandrator-native dubbing workflows."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Any

from .. import llm_handler
from . import llm_config
from .settings import TRANSLATION_BACKEND_DEEPL, migrate_dubbing_payload

DEEPL_PROVIDER_ID = "deepl"


@dataclass(frozen=True)
class CredentialValidationResult:
    ok: bool
    step_key: str = ""
    message: str = ""


def normalize_provider_id(raw_value: str | None) -> str:
    lowered = str(raw_value or "").strip().lower()
    return re.sub(r"[^a-z0-9]+", "-", lowered).strip("-")


def settings_use_deepl(settings: dict[str, Any]) -> bool:
    normalized = migrate_dubbing_payload(settings, settings.get("llm_provider_configs"))
    return normalized.get("translation_backend") == TRANSLATION_BACKEND_DEEPL


def validate_llm_credentials(
    settings: dict[str, Any],
    stage_label: str,
    *,
    stage: str = "correction",
) -> CredentialValidationResult:
    llm_settings = llm_config.resolve_dubbing_llm_settings(settings, stage=stage)
    status = llm_handler.validate_model_credentials(
        llm_settings.model_name,
        llm_settings.llm_settings,
    )
    if status.ok:
        return CredentialValidationResult(ok=True)
    return CredentialValidationResult(
        ok=False,
        message=f"{stage_label} cannot run: {status.message}",
    )


def validate_translation_credentials(settings: dict[str, Any]) -> CredentialValidationResult:
    if settings_use_deepl(settings):
        if os.environ.get("DEEPL_API_KEY", "").strip():
            return CredentialValidationResult(ok=True)
        return CredentialValidationResult(
            ok=False,
            step_key="translate",
            message="Subtitle translation cannot run: DeepL requires DEEPL_API_KEY in the API Keys tab or environment.",
        )

    result = validate_llm_credentials(settings, "Subtitle translation", stage="translation")
    if result.ok:
        return result
    return CredentialValidationResult(ok=False, step_key="translate", message=result.message)


def validate_correction_credentials(settings: dict[str, Any]) -> CredentialValidationResult:
    result = validate_llm_credentials(settings, "Subtitle correction", stage="correction")
    if result.ok:
        return result
    return CredentialValidationResult(ok=False, step_key="correct", message=result.message)


def validate_transcription_credentials(settings: dict[str, Any]) -> CredentialValidationResult:
    if bool(settings.get("correction_enabled")) and not settings_use_deepl(settings):
        result = validate_correction_credentials(settings)
        if not result.ok:
            return CredentialValidationResult(ok=False, step_key="transcribe", message=result.message)

    return CredentialValidationResult(ok=True)


def validate_task_credentials(
    task_name: str,
    settings: dict[str, Any],
    *,
    current_srt_exists: bool = False,
) -> CredentialValidationResult:
    normalized_task = str(task_name or "").strip().lower()

    if normalized_task == "transcribe":
        return validate_transcription_credentials(settings)

    if normalized_task == "correct":
        return validate_correction_credentials(settings)

    if normalized_task == "translate":
        return validate_translation_credentials(settings)

    if normalized_task == "generate_audio":
        if not current_srt_exists:
            transcription_result = validate_transcription_credentials(settings)
            if not transcription_result.ok:
                return transcription_result

        if bool(settings.get("translation_enabled")):
            translation_result = validate_translation_credentials(settings)
            if not translation_result.ok:
                return translation_result

    return CredentialValidationResult(ok=True)
