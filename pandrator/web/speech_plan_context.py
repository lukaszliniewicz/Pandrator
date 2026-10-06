"""Speech-plan signatures and bounded semantic context primitives."""

from __future__ import annotations

from typing import Any

import regex
from sqlalchemy import select

from . import models as m
from .settings_policy import stable_hash

SIGNATURE_FIELDS = (
    "ordinal",
    "text",
    "optimized_text",
    "node_kind",
    "speaker",
    "language",
    "voice",
    "voice_id",
    "silence_after_ms",
    "removed",
    "source_segment_ids_json",
    "speech_block_provenance_json",
    "speech_plan_json",
    "paragraph_break_after",
)


def plan_signature(session, revision_id: str) -> str:
    columns = [getattr(m.GenerationSegment, key) for key in SIGNATURE_FIELDS]
    rows = session.execute(
        select(*columns)
        .where(m.GenerationSegment.plan_revision_id == revision_id)
        .order_by(m.GenerationSegment.ordinal)
    )
    return stable_hash([dict(zip(SIGNATURE_FIELDS, row, strict=True)) for row in rows])


def semantic_context_units(session, revision_id: str) -> list[dict[str, Any]]:
    """Read the full accepted plan, even for single-block regeneration."""
    return [
        {
            "id": item.id, "text": item.text, "speaker": item.speaker or "",
            "language": item.language or "", "node_kind": item.node_kind,
            "section_id": str((item.speech_block_provenance_json or {}).get("section_id") or ""),
        }
        for item in session.scalars(
            select(m.GenerationSegment)
            .where(m.GenerationSegment.plan_revision_id == revision_id, m.GenerationSegment.removed.is_(False))
            .order_by(m.GenerationSegment.ordinal)
        )
    ]


def semantic_context_window(units: list[dict[str, Any]], settings: dict[str, Any], *, target_ids: set[str] | None = None) -> dict[str, dict[str, str]]:
    """Bounded context without audio conditioning or whitespace tokenization."""
    sizes = {}
    for name, default, maximum in (("before", 2, 20), ("after", 1, 20), ("max_chars", 4000, 16000)):
        value = settings.get(f"performance_context_{name}", default)
        if value is None:
            value = default
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= maximum:
            raise ValueError(f"Context {name} must be an integer between 0 and {maximum}.")
        sizes[name] = value
    mode = str(settings.get("tts_context_mode") or "both")
    if mode not in {"off", "before", "both"}:
        raise ValueError("tts_context_mode must be off, before, or both.")
    if mode == "off":
        return {}
    if mode == "before":
        sizes["after"] = 0
    section_kinds = {"heading", "title", "chapter", "chapter_title", "section", "section_title"}

    def boundary(left, right):
        return (
            (left.get("language") and right.get("language") and left["language"] != right["language"])
            or (left.get("section_id") and right.get("section_id") and left["section_id"] != right["section_id"])
            or str(left.get("node_kind") or "").lower() in section_kinds
            or str(right.get("node_kind") or "").lower() in section_kinds
        )

    def context_text(other, current):
        text = str(other.get("text") or "")
        if other.get("speaker") and other.get("speaker") != current.get("speaker"):
            # This is semantic evidence, not a voice reference. A short answer
            # may need another speaker's question to make sense.
            return f"[Other speaker: {str(other['speaker'])[:160]}] {text}"
        return text

    def trim_graphemes(value: str, limit: int, *, from_end: bool) -> str:
        if limit <= 0:
            return ""
        graphemes = [match.group(0) for match in regex.finditer(r"\X", value)]
        if from_end:
            result = []
            size = 0
            for item in reversed(graphemes):
                if size + len(item) > limit:
                    break
                result.append(item)
                size += len(item)
            return "".join(reversed(result))
        result = []
        size = 0
        for item in graphemes:
            if size + len(item) > limit:
                break
            result.append(item)
            size += len(item)
        return "".join(result)

    def marker_and_text(value: str) -> tuple[str, str]:
        prefix = "[Other speaker: "
        if value.startswith(prefix):
            marker_end = value.find("] ", len(prefix))
            if marker_end >= 0:
                marker_end += 2
                return value[:marker_end], value[marker_end:]
        return "", value

    def trim_context(value: str, limit: int, *, from_end: bool) -> str:
        marker, text = marker_and_text(value)
        if not marker:
            return trim_graphemes(value, limit, from_end=from_end)
        if len(marker) > limit:
            # Omitting the unit is preferable to exposing another speaker's
            # words without the marker that makes their meaning safe.
            return ""
        return marker + trim_graphemes(
            text, limit - len(marker), from_end=from_end
        )

    def fit_side(items: list[str], budget: int, *, nearest_is_last: bool) -> str:
        selected: list[str] = []
        remaining = budget
        candidates = list(reversed(items)) if nearest_is_last else list(items)
        for item in candidates:
            separator = 1 if selected else 0
            allowance = remaining - separator
            if allowance <= 0:
                break
            if len(item) <= allowance:
                selected.append(item)
                remaining -= separator + len(item)
                continue
            shortened = trim_context(
                item, allowance, from_end=nearest_is_last
            )
            if shortened:
                selected.append(shortened)
            break
        if nearest_is_last:
            selected.reverse()
        return "\n".join(selected)

    result = {}
    for index, unit in enumerate(units):
        if target_ids is not None and str(unit["id"]) not in target_ids:
            continue
        before, after = [], []
        for offset in range(1, sizes["before"] + 1):
            previous = index - offset
            if previous < 0 or boundary(units[previous], units[previous + 1]):
                break
            before.append(context_text(units[previous], unit))
        for offset in range(1, sizes["after"] + 1):
            following = index + offset
            if following >= len(units) or boundary(units[following - 1], units[following]):
                break
            after.append(context_text(units[following], unit))
        previous_text = "\n".join(reversed(before))
        following_text = "\n".join(after)
        limit = sizes["max_chars"]
        if len(previous_text) + len(following_text) > limit:
            before_budget = min(len(previous_text), limit // 2)
            after_budget = min(len(following_text), limit - before_budget)
            before_budget = min(len(previous_text), limit - after_budget)
            previous_text = fit_side(
                list(reversed(before)), before_budget, nearest_is_last=True
            )
            following_text = fit_side(after, after_budget, nearest_is_last=False)
        result[str(unit["id"])] = {"before": previous_text, "after": following_text}
    return result
