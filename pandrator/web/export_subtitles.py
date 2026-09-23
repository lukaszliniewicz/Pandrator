"""Subtitle language, title and default-track metadata shared by export renderers."""

from __future__ import annotations

from typing import Any

from pandrator.logic.dubbing.languages import normalize_language_code, subtitle_language_title

from .models import Artifact, SessionRecord


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
