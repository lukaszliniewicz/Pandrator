"""Freeze the effective audiobook pause alongside the accepted speech structure."""

from __future__ import annotations

import re

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash

from . import models as m
from .generation_cast_runtime import remap_markup
from .generation_controls import get_generation_controls


def record_continues_sentence(record: dict) -> bool:
    value = record.get("sentence_continues_after")
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "clause"}
    if value is not None:
        return bool(value)
    if str(record.get("pause_kind") or "").strip().lower() == "clause":
        return True
    # Legacy split pieces lack the explicit continuation flag; only their
    # terminal punctuation distinguishes an internal clause from the last part.
    if record.get("split_part") is not None:
        text = str(record.get("text") or record.get("original_sentence") or "").rstrip()
        return re.search(r"[.!?…。！？…][\"'”’)\]}]*$", text) is None
    return False


def record_pause_metadata(record: dict, *, is_subtitle: bool = False) -> dict:
    """Keep supplied pauses distinct from defaults when a plan is materialized."""
    if record.get("silence_after_ms") is not None:
        return {"silence_override_ms": max(0, int(record["silence_after_ms"]))}
    if is_subtitle:
        return {}
    paragraph = (
        bool(record.get("paragraph_break_after"))
        or str(record.get("paragraph") or "").lower() == "yes"
    )
    kind = (
        "paragraph"
        if paragraph
        else ("clause" if record_continues_sentence(record) else "sentence")
    )
    return {"pause_kind": kind}


def default_record_pause(record: dict, settings: dict, *, is_subtitle: bool = False) -> int:
    """Resolve initial pauses with a compatibility fallback for old runtime dicts."""
    explicit = record.get("silence_after_ms")
    if explicit is not None:
        return max(0, int(explicit or 0))
    if is_subtitle:
        return 0

    sentence_silence = max(
        0,
        int(
            settings.get("sentence_silence_ms", settings.get("silence_between_sentences", 250)) or 0
        ),
    )
    is_paragraph = (
        bool(record.get("paragraph_break_after"))
        or str(record.get("paragraph") or "").lower() == "yes"
    )
    boundary = record.get("speech_boundary_after")
    if boundary == "continuation":
        return 0
    if boundary == "dialogue_turn":
        return sentence_silence
    if boundary in {"scene", "chapter", "paragraph"}:
        is_paragraph = True
    if is_paragraph:
        return max(
            0,
            int(
                settings.get("paragraph_silence_ms", settings.get("silence_for_paragraphs", 700))
                or 0
            ),
        )
    if record_continues_sentence(record):
        if "clause_silence_ms" in settings:
            return max(0, int(settings["clause_silence_ms"]))
        return max(0, round(sentence_silence / 3))
    return sentence_silence


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
    voices = {}
    voice_gap = int((snapshot.get("audio") or {}).get("voice_change_silence_ms") or 0)
    rows = list(
        session.scalars(
            select(m.GenerationSegment)
            .where(
                m.GenerationSegment.plan_revision_id == revision_id,
                m.GenerationSegment.removed.is_(False),
            )
            .order_by(m.GenerationSegment.ordinal)
        )
    )
    for row in rows:
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
        speech_plan = row.speech_plan_json or {}
        explicit = speech_plan.get("silence_override_ms")
        pause = boundary_pause(boundary, row.silence_after_ms, snapshot.get("audio") or {})
        if boundary is None and speech_plan.get("pause_kind") in {
            "clause",
            "sentence",
            "paragraph",
        }:
            key = f"{speech_plan['pause_kind']}_silence_ms"
            pause = max(0, int((snapshot.get("audio") or {}).get(key, row.silence_after_ms)))
        if explicit is not None:
            pause = max(0, int(explicit))
        if voice_gap and row.node_kind != "subtitle_cue":
            from .generation_cast_runtime import segment_render_parts
            from .generation_performance_snapshot import (
                performance_runtime_settings,
                segment_performance_settings,
            )

            settings = performance_runtime_settings(snapshot)
            if row.voice:
                settings["voice"] = settings["speaker"] = row.voice
            if row.language:
                settings["language"] = row.language
            settings = segment_performance_settings(settings, snapshot, row.id, text)
            parts = segment_render_parts(settings, snapshot, row.id, text)
            voices[row.id] = (parts[0]["voice_key"], parts[-1]["voice_key"])
        result[row.id] = {
            "text_hash": content_hash(text) if xml else None,
            "boundary_after": boundary,
            "silence_after_ms": pause,
        }
    for left, right in zip(rows, rows[1:], strict=False):
        if (left.speech_plan_json or {}).get("silence_override_ms") is not None:
            continue
        if left.id in voices and right.id in voices and voices[left.id][1] != voices[right.id][0]:
            result[left.id]["silence_after_ms"] = max(
                result[left.id]["silence_after_ms"], voice_gap
            )
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
