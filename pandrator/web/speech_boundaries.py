"""Freeze the effective audiobook pause alongside the accepted speech structure."""

from __future__ import annotations

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash

from . import models as m
from .generation_cast_runtime import remap_markup
from .generation_controls import get_generation_controls


def boundary_pause(boundary: str | None, stored_ms: int, audio: dict) -> int:
    if boundary == "continuation":
        return 0
    if boundary == "dialogue_turn":
        return max(0, int(audio.get("sentence_silence_ms", 250)))
    if boundary in {"paragraph", "scene", "chapter"}:
        return max(0, int(audio.get("paragraph_silence_ms", 700)))
    return max(0, int(stored_ms or 0))


def freeze_boundaries(session, revision_id: str, snapshot: dict) -> None:
    revision = session.get(m.GenerationPlanRevision, revision_id)
    plan = session.get(m.GenerationPlan, revision.plan_id) if revision else None
    record = session.get(m.SessionRecord, plan.session_id) if plan else None
    if record is None or record.workflow_kind != "audiobook":
        return
    frozen = snapshot.get("performance_snapshot") or {}
    characters = frozen.get(
        "character_dictionary",
        get_generation_controls(session, record.id)["characters"],
    )
    annotations = frozen.get("annotations") or {}
    result = {}
    for row in session.scalars(
        select(m.GenerationSegment).where(
            m.GenerationSegment.plan_revision_id == revision_id,
            m.GenerationSegment.removed.is_(False),
        )
    ):
        text = row.optimized_text or row.text
        entry = annotations.get(row.id) or {}
        xml = (entry.get("annotation") or {}).get("_speech_xml") or (
            row.speech_plan_json or {}
        ).get("speech_xml")
        boundary = None
        if xml:
            xml = remap_markup(xml, row.id, text, characters)
            boundary = parse_speech_markup(
                xml,
                expected_segment_id=row.id,
                expected_text=text,
                characters=characters,
            ).boundary_after
        result[row.id] = {
            "text_hash": content_hash(text) if xml else None,
            "boundary_after": boundary,
            "silence_after_ms": boundary_pause(
                boundary, row.silence_after_ms, snapshot.get("audio") or {}
            ),
        }
    snapshot["speech_boundaries"] = result


def assembly_pause(segment, snapshot: dict) -> int:
    entry = (snapshot.get("speech_boundaries") or {}).get(segment.id)
    if entry is None:
        return max(0, int(segment.silence_after_ms or 0))
    if entry.get("text_hash") is not None and entry["text_hash"] != content_hash(
        segment.optimized_text or segment.text
    ):
        raise ValueError("Speech text changed after assembly boundaries were frozen.")
    return max(0, int(entry["silence_after_ms"]))
