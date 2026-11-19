"""Subtitle language, title and default-track metadata shared by export renderers."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Any, Iterable, Mapping

from pandrator.logic.dubbing.languages import normalize_language_code, subtitle_language_title
from pandrator.logic.dubbing.subtitle_finalization import (
    SubtitleFinalizationConfig,
    diagnose_srt_content,
)
from pandrator.logic.dubbing.subtitle_integrity import subtitle_text_integrity

from .models import Artifact, SessionRecord
from .settings_policy import RUNTIME_SETTING_ALIASES

SUBTITLE_ALIASES = RUNTIME_SETTING_ALIASES["subtitles"]
_SUBTITLE_OVERLAP_LIMIT = 100
_SUBTITLE_ISSUE_LIMIT = 50


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
    clamp_report: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    effective = {**settings, "subtitle_language": language}
    report = dict(clamp_report or {})
    return {
        "profile": subtitle_profile(effective, provenance, language=language, language_origin=language_origin, text=source),
        "input": diagnose_srt_content(source, effective),
        "final": diagnose_srt_content(finalized, effective),
        "text_integrity": subtitle_text_integrity(source, finalized),
        "clamp_overlaps": bool(settings.get("clamp_overlaps")),
        "overlaps_clamped": int(report.get("overlaps_clamped") or 0),
        "overlaps_unresolved": int(report.get("overlaps_unresolved") or 0),
    }


def subtitle_file_diagnostics(
    source: Path, finalized: Path, settings: dict[str, Any],
    provenance: dict[str, Any] | None, *, language: str,
    language_origin: str,
    clamp_report: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    return subtitle_export_diagnostics(
        source.read_text(encoding="utf-8-sig"),
        finalized.read_text(encoding="utf-8-sig"),
        settings, provenance, language=language,
        language_origin=language_origin,
        clamp_report=clamp_report,
    )


def subtitle_issue_summary(
    results: Iterable[Mapping[str, Any]],
) -> dict[str, Any] | None:
    """Flatten export diagnostics into shallow rows the work projection keeps.

    ``_bounded_json`` truncates anything at depth six, which is exactly where the
    nested per-issue dictionaries sit. Every row here is a flat list, so cue
    ordinals survive the job result summary.
    """

    tracks: list[Mapping[str, Any]] = []
    for item in results:
        if not isinstance(item, Mapping):
            continue
        diagnostics = item.get("subtitle_diagnostics")
        if isinstance(diagnostics, Mapping):
            tracks.append(diagnostics)
        for track in item.get("subtitle_tracks") or []:
            if not isinstance(track, Mapping):
                continue
            nested = track.get("subtitle_diagnostics")
            if isinstance(nested, Mapping):
                tracks.append(nested)
    if not tracks:
        return None
    overlapping_cues = 0
    issues_total = 0
    overlaps: list[list[int]] = []
    issues: list[list[Any]] = []
    for diagnostics in tracks:
        final = diagnostics.get("final")
        if not isinstance(final, Mapping):
            continue
        overlapping_cues += int(final.get("overlapping_cues") or 0)
        issues_total += int(final.get("issues_total") or 0)
        for row in final.get("overlaps") or []:
            if len(overlaps) >= _SUBTITLE_OVERLAP_LIMIT:
                break
            if isinstance(row, (list, tuple)) and len(row) == 3:
                overlaps.append([int(value) for value in row])
        for issue in final.get("issues") or []:
            if len(issues) >= _SUBTITLE_ISSUE_LIMIT:
                break
            if not isinstance(issue, Mapping):
                continue
            issues.append(
                [
                    int(issue.get("ordinal") or 0),
                    int(issue.get("start_ms") or 0),
                    int(issue.get("end_ms") or 0),
                    ",".join(str(reason) for reason in issue.get("reasons") or []),
                ]
            )
    return {
        "overlapping_cues": overlapping_cues,
        "overlaps": overlaps,
        "issues_total": issues_total,
        "issues": issues,
    }


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


def speech_track_language(
    record: SessionRecord, settings: dict[str, Any]
) -> tuple[str, str]:
    """Return the language of the generated speech track, and where it came from.

    A translation branch keeps its language on the session record rather than in
    the export settings, so the dubbed track is named from the session when the
    run itself does not say. ``output_language`` and its older ``language`` key
    are separate candidates: an ``auto`` placeholder in one must not mask a
    concrete language in the other. No usable value means no tag, as before.
    """

    candidates = (
        ("output_language", settings.get("output_language")),
        ("output_language", settings.get("language")),
        ("session_target_language", record.target_language),
        ("session_source_language", record.source_language),
        ("run_language", settings.get("target_language")),
    )
    for origin, candidate in candidates:
        language = _effective_subtitle_language(candidate)
        if language != "und":
            return language, origin
    return "", "none"
