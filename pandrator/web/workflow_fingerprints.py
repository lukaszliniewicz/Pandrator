"""Semantic workflow settings fingerprints shared by readers and execution."""

from __future__ import annotations

from typing import Any

from pandrator.logic.dubbing.settings import normalize_correction_style
from pandrator.logic.dubbing.srt_utils import timing_context_mode_from_settings


def _stage_settings_fingerprint(stage_key: str, settings: dict[str, Any]) -> dict[str, Any]:
    """Semantic identity of a stage's settings, independent of submission shape.

    Only values that can change the produced artifact are included.  Raw hashes
    of whole settings dictionaries were unstable: the same configuration could
    arrive flat from a stage dialog or section-shaped from resolved settings,
    and hydrated dictionaries carry volatile provider data (keys, costs).  Both
    caused prerequisite stages such as translation to rerun spuriously.
    """

    def _text(*keys: str) -> str:
        for key in keys:
            value = settings.get(key)
            if value is not None and str(value).strip():
                return str(value).strip()
        return ""

    def _model(*keys: str) -> str:
        value = _text(*keys)
        return "" if value.lower() == "default" else value

    def _positive_int(key: str, default: int = 1) -> int:
        try:
            return max(1, int(settings.get(key) or default))
        except (TypeError, ValueError):
            return default

    def _nonnegative_int(*keys: str, default: int) -> int:
        value: Any = None
        for key in keys:
            if settings.get(key) not in {None, ""}:
                value = settings[key]
                break
        try:
            return max(0, int(default if value is None or value == "" else value))
        except (TypeError, ValueError):
            return default

    def _processing_shape() -> dict[str, Any]:
        shape: dict[str, Any] = {}
        char_limit = _nonnegative_int("char_limit", "llm_char", default=6000)
        segment_limit = _nonnegative_int(
            "max_segments_per_batch",
            "max_subtitles_per_call",
            default=40,
        )
        if char_limit != 6000:
            shape["char_limit"] = char_limit
        if segment_limit != 40:
            shape["max_segments_per_batch"] = segment_limit
        if bool(settings.get("no_remove_subtitles", False)):
            shape["no_remove_subtitles"] = True
        if settings.get("context") is False:
            shape["context"] = False
        context_before = _nonnegative_int("context_before", default=8)
        context_after = _nonnegative_int("context_after", default=2)
        if context_before != 8:
            shape["context_before"] = context_before
        if context_after != 2:
            shape["context_after"] = context_after
        mode = timing_context_mode_from_settings(settings)
        if mode != "full":
            shape["timing_context_mode"] = mode
        elif (
            gap := _nonnegative_int(
                "substantial_gap_ms",
                "timing_context_gap_ms",
                default=2000,
            )
        ) != 2000:
            shape["substantial_gap_ms"] = gap
        return shape

    if stage_key == "translate":
        backend = _text("translation_backend", "backend").lower() or "llm"
        model = _model("translation_model", "translate_model", "model_name")
        if not model and backend == "llm":
            model = _text("llm_default_model")
        result: dict[str, Any] = {
            "backend": backend,
            "target_language": _text("target_language").lower(),
            "model": model,
            "instructions": _text("translate_prompt", "instructions"),
        }
        reasoning_effort = _text("reasoning_effort")
        if backend == "llm" and reasoning_effort:
            result["reasoning_effort"] = reasoning_effort
        concurrent_calls = _positive_int("llm_concurrent_calls")
        if backend == "llm" and concurrent_calls > 1:
            result["llm_concurrent_calls"] = concurrent_calls
        result.update(_processing_shape())
        if bool(settings.get("glossary_enabled", False)) and settings.get("glossary"):
            result["glossary"] = settings["glossary"]
        research = _research_fingerprint(settings)
        return {**result, **({"web_research": research} if research else {})}
    if stage_key == "correct":
        model = _model("correction_model", "correct_model", "model_name") or _text(
            "llm_default_model"
        )
        result = {
            "model": model,
            "instructions": _text("custom_correction_prompt", "instructions"),
            "correction_style": normalize_correction_style(settings.get("correction_style")),
        }
        reasoning_effort = _text("reasoning_effort")
        if reasoning_effort:
            result["reasoning_effort"] = reasoning_effort
        concurrent_calls = _positive_int("llm_concurrent_calls")
        if concurrent_calls > 1:
            result["llm_concurrent_calls"] = concurrent_calls
        result.update(_processing_shape())
        research = _research_fingerprint(settings)
        return {**result, **({"web_research": research} if research else {})}
    return {}


def _research_fingerprint(settings: dict[str, Any]) -> dict[str, Any]:
    if not bool(settings.get("web_research_enabled", False)):
        # Keep pre-feature artifact fingerprints reusable when research is off.
        return {}
    try:
        context_fraction = min(
            0.8,
            max(0.1, float(settings.get("web_research_context_fraction") or 0.8)),
        )
    except (TypeError, ValueError):
        context_fraction = 0.8
    return {
        "enabled": True,
        "provider": str(settings.get("web_research_provider") or "jina").strip().lower(),
        "model": str(settings.get("web_research_model_name") or "").strip(),
        "mode": str(settings.get("web_research_mode") or "global").strip().lower(),
        "context_fraction": context_fraction,
        "language": str(settings.get("web_research_language") or "").strip().lower(),
        "max_searches": max(0, int(settings.get("web_research_max_searches") or 3)),
        "max_extractions": max(0, int(settings.get("web_research_max_extractions") or 2)),
        "preferred_domains": str(settings.get("web_research_preferred_domains") or "").strip(),
        "blocked_domains": str(settings.get("web_research_blocked_domains") or "").strip(),
    }
