"""Subtitle language, title and default-track metadata shared by export renderers."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any

from pandrator.logic.dubbing.languages import normalize_language_code, subtitle_language_title
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
    diagnose_srt_content,
)

from .models import Artifact, SessionRecord
from .settings_policy import RUNTIME_SETTING_ALIASES

SUBTITLE_ALIASES = RUNTIME_SETTING_ALIASES["subtitles"]


def subtitle_profile(
    settings: dict[str, Any], provenance: dict[str, Any] | None,
    *, language: str = "", language_origin: str = "unknown/historical_snapshot",
    text: str = "",
) -> dict[str, Any]:
    """Describe the finalizer's effective, possibly clamped limits."""
    config = SubtitleFinalizationConfig.from_settings(
        {**settings, "subtitle_language": language} if language else settings,
        text=text,
    )
    fields = (provenance or {}).get("fields") or {}
    limits = asdict(config)
    limits.pop("language")
    automatic = settings.get("subtitle_language_defaults", settings.get("language_defaults")) is True
    mapped: dict[str, Any] = {}
    for key, effective in limits.items():
        section_key = "max_cps" if key == "max_chars_per_second" else key
        runtime_key = SUBTITLE_ALIASES.get(section_key, "subtitle_" + section_key)
        supplied = settings.get(runtime_key, settings.get(section_key))
        field = fields.get(section_key, {})
        mapped[key] = {
            "effective": effective,
            "supplied": supplied,
            "origin": field.get("origin", "unknown/historical_snapshot"),
            "effective_origin": (
                "automatic_language_default"
                if automatic and key in {"max_chars_per_line", "max_chars_per_second"}
                else field.get("origin", "unknown/historical_snapshot")
            ),
        }
    if _effective_subtitle_language(language) == "und":
        language_origin = (
            "run_language"
            if not language and _effective_subtitle_language(
                settings.get("subtitle_language"), settings.get("target_language"),
                settings.get("language"), settings.get("stt_language"),
            ) != "und"
            else "text_inference" if text else "text_inference_pending"
        )
    warning = None
    if config.language.split("-")[0] in {"ja", "zh", "ko"} and not automatic:
        flag = fields.get("language_defaults", {})
        warning = {
            "code": "cjk_language_defaults_disabled",
            "language": config.language,
            "origin": flag.get("origin", "unknown/historical_snapshot"),
            "causes": flag.get("causes", []),
        }
    return {
        "language": config.language,
        "language_origin": language_origin,
        "language_defaults": {
            "effective": automatic,
            "supplied": settings.get("subtitle_language_defaults", settings.get("language_defaults")),
            "origin": fields.get("language_defaults", {}).get("origin", "unknown/historical_snapshot"),
            "causes": fields.get("language_defaults", {}).get("causes", []),
        },
        "limits": mapped,
        "warning": warning,
    }


def subtitle_export_diagnostics(
    source: str, finalized: str, settings: dict[str, Any],
    provenance: dict[str, Any] | None, *, language: str,
    language_origin: str,
) -> dict[str, Any]:
    effective = {**settings, "subtitle_language": language}
    return {
        "profile": subtitle_profile(effective, provenance, language=language, language_origin=language_origin, text=source),
        "input": diagnose_srt_content(source, effective),
        "final": diagnose_srt_content(finalized, effective),
    }


def subtitle_file_diagnostics(
    source: Path, finalized: Path, settings: dict[str, Any],
    provenance: dict[str, Any] | None, *, language: str,
    language_origin: str,
) -> dict[str, Any]:
    return subtitle_export_diagnostics(
        source.read_text(encoding="utf-8-sig"),
        finalized.read_text(encoding="utf-8-sig"),
        settings, provenance, language=language,
        language_origin=language_origin,
    )


def _effective_subtitle_language(*candidates: object) -> str:
    """Choose the first concrete language without letting ``auto`` mask it."""
    for candidate in candidates:
        normalized = normalize_language_code(str(candidate or ""), default="")
        if normalized and normalized not in {"auto", "und", "unknown"}:
            return normalized
    return "und"


def subtitle_track_details(
    item: Artifact, *, record: SessionRecord, settings: dict[str, Any], track_count: int
) -> tuple[str, str, str, bool]:
    translation_track = (
        str((item.metadata_json or {}).get("source_role") or item.role)
        == "translation"
    )
    track_name = "translation" if translation_track else "source"
    language = _effective_subtitle_language(
        (item.metadata_json or {}).get("language"),
        record.target_language
        if translation_track
        else record.source_language,
        (
            settings.get("target_language")
            if translation_track
            else settings.get("original_language")
            or settings.get("source_language")
        ),
    )
    title = subtitle_language_title(language)
    is_default = translation_track or track_count == 1
    return track_name, language, title, is_default


def subtitle_track_language_origin(
    item: Artifact, *, record: SessionRecord, settings: dict[str, Any]
) -> str:
    if _effective_subtitle_language((item.metadata_json or {}).get("language")) != "und":
        return "artifact_metadata"
    translation = str((item.metadata_json or {}).get("source_role") or item.role) == "translation"
    session_language = record.target_language if translation else record.source_language
    if _effective_subtitle_language(session_language) != "und":
        return "session_target_language" if translation else "session_source_language"
    run_language = settings.get("target_language") if translation else (
        settings.get("original_language") or settings.get("source_language")
    )
    return "run_language" if _effective_subtitle_language(run_language) != "und" else "text_inference"
