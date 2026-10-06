"""Immutable performance and semantic-context bindings for generation runs."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from . import models as m
from .speech_plan_context import semantic_context_units, semantic_context_window


def performance_runtime_settings(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Use run-local alternate settings with the same precedence as synthesis."""
    settings = {**dict(snapshot.get("audio") or {}), **dict(snapshot.get("tts") or {})}
    override = snapshot.get("selected_segment_override") or {}
    from .generation_cast_runtime import filter_segment_tts_override

    settings.update(
        filter_segment_tts_override(
            snapshot.get("tts"), override.get("tts") if isinstance(override, dict) else None
        )
    )
    return settings


def frozen_semantic_contexts(snapshot: dict[str, Any]) -> dict[str, dict[str, str]]:
    raw = snapshot.get("semantic_context_snapshot") or {}
    if not raw:
        return {}
    if raw.get("schema_version") != 1:
        raise ValueError("Unsupported semantic context snapshot. Start a new generation run.")
    return semantic_context_window(raw.get("units") or [], dict(raw.get("settings") or {}))


def freeze_generation_performance_snapshot(
    session, revision_id: str, snapshot: dict[str, Any], _service_config_cache=None
) -> bool:
    """Bind a new run to current adoption and immutable semantic source text.

    Resume/retry consumes the existing run snapshot rather than reselecting it.
    The text is stored once, not repeated for every context window in a book.
    """
    if _service_config_cache is None:
        _service_config_cache = {}
    settings = performance_runtime_settings(snapshot)
    mode = str(settings.get("tts_context_mode") or "off")
    if mode not in {"off", "before", "both"}:
        raise ValueError("tts_context_mode must be off, before, or both.")
    snapshot.pop("performance_snapshot", None)
    snapshot.pop("semantic_context_snapshot", None)
    snapshot.pop("generation_control_snapshot", None)
    if settings.get("performance_enabled"):
        from .performance_plans import freeze_performance_snapshot

        freeze_performance_snapshot(session, revision_id, snapshot)
    elif settings.get("casting_enabled"):
        from .models import PerformancePlan
        from .performance_plans import freeze_performance_snapshot
        if session.scalar(select(PerformancePlan.id).where(
            PerformancePlan.plan_revision_id == revision_id,
            PerformancePlan.status == "adopted",
        )):
            freeze_performance_snapshot(session, revision_id, snapshot)
    has_block_voice = session.scalar(select(m.GenerationSegment.id).where(
        m.GenerationSegment.plan_revision_id == revision_id,
        m.GenerationSegment.removed.is_(False),
        (m.GenerationSegment.voice_id.is_not(None) | m.GenerationSegment.voice.is_not(None)),
    ).limit(1)) is not None
    if settings.get("performance_enabled") or settings.get("casting_enabled") or has_block_voice:
        from .generation_cast_runtime import freeze_cast_snapshot
        freeze_cast_snapshot(
            session, revision_id, snapshot, settings, _service_config_cache
        )
    if mode != "off":
        context_settings = {
            key: settings[key]
            for key in ("tts_context_mode", "performance_context_before", "performance_context_after", "performance_context_max_chars")
            if key in settings
        }
        semantic_context_window([], context_settings)
        snapshot["semantic_context_snapshot"] = {
            "schema_version": 1, "plan_revision_id": revision_id,
            "settings": context_settings,
            "units": semantic_context_units(session, revision_id),
        }
    return bool(settings.get("performance_enabled") or settings.get("casting_enabled")) or mode != "off"


def segment_performance_settings(
    settings: dict[str, Any], snapshot: dict[str, Any], segment_id: str, text: str,
    *, contexts: dict[str, dict[str, str]] | None = None,
) -> dict[str, Any]:
    """Supply request-only metadata without changing spoken or alignment text."""
    from copy import deepcopy

    from .generation_cast_runtime import apply_segment_voice

    result = apply_segment_voice(settings, snapshot, segment_id)
    result.pop("_performance", None)
    result.pop("_semantic_context", None)
    if bool(settings.get("performance_enabled")):
        from .performance_plans import performance_for_segment

        annotation = performance_for_segment(snapshot, segment_id, text)
        if annotation is not None:
            result["_performance"] = deepcopy(annotation)
    if str(settings.get("tts_context_mode") or "off") != "off":
        lookup = contexts if contexts is not None else frozen_semantic_contexts(snapshot)
        if segment_id in lookup:
            result["_semantic_context"] = dict(lookup[segment_id])
    return result
