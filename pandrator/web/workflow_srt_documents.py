"""Caller-transaction subtitle document persistence and pure speaker helpers."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from typing import Any

from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import split_speaker_label

from .logical_passages import attach_passages, passage_review_metadata
from .models import Artifact, Document, DocumentRevision, Segment, SegmentLineage


def _structured_speaker(segment: Any) -> str:
    speaker = str(getattr(segment, "speaker", None) or "").strip()
    if speaker:
        return speaker
    legacy_speaker, _text = split_speaker_label(str(getattr(segment, "text", "") or ""))
    return str(legacy_speaker or "").strip()


def _dominant_speaker(start_ms: int, end_ms: int, candidates: list[Any]) -> str:
    weighted: dict[str, tuple[str, int, int]] = {}
    for order, candidate in enumerate(candidates):
        speaker = _structured_speaker(candidate)
        candidate_start = getattr(candidate, "start_ms", None)
        candidate_end = getattr(candidate, "end_ms", None)
        if not speaker or candidate_start is None or candidate_end is None:
            continue
        overlap = min(end_ms, int(candidate_end)) - max(start_ms, int(candidate_start))
        if overlap <= 0:
            continue
        key = speaker.casefold()
        raw, total, first_order = weighted.get(key, (speaker, 0, order))
        weighted[key] = (raw, total + overlap, first_order)
    if not weighted:
        return ""
    return max(weighted.values(), key=lambda value: (value[1], -value[2]))[0]


def _hash_segments(segments) -> str:
    payload = [
        {
            "start_ms": segment.start_ms,
            "end_ms": segment.end_ms,
            "text": segment.text,
            "speaker": _structured_speaker(segment) or None,
        }
        for segment in segments
    ]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def store_srt_document_in_session(
    session: Session,
    session_id: str,
    artifact: Artifact,
    managed: Artifact,
    stage: str,
    segments,
    parents: list[Segment],
    parent_file_segments: list[Any],
    *,
    language: str | None = None,
    speaker_overrides: dict[int, str] | None = None,
    logical_passages: list[dict[str, Any]] | None = None,
    hash_segments_func: Callable[..., str] | None = None,
    speaker_resolver: Callable[..., str] | None = None,
    parent_artifact: Artifact | None = None,
) -> tuple[str, str]:
    hash_segments_func = hash_segments_func or _hash_segments
    speaker_resolver = speaker_resolver or _dominant_speaker
    speaker_candidates: list[Any] = parents or parent_file_segments
    resolved_segments = []
    speaker_sources: list[str] = []
    for item in segments:
        reviewed_speaker = str((speaker_overrides or {}).get(item.index) or "").strip()
        inherited_speaker = str(
            item.speaker
            or speaker_resolver(
                item.start_ms,
                item.end_ms,
                speaker_candidates,
            )
            or ""
        ).strip()
        resolved_segments.append(
            replace(
                item,
                speaker=reviewed_speaker or inherited_speaker,
            )
        )
        speaker_sources.append("model_reviewed" if reviewed_speaker else "timing_inherited")
    document = Document(session_id=session_id, stage=stage, language=language)
    session.add(document)
    session.flush()
    revision = DocumentRevision(
        document_id=document.id,
        revision_number=1,
        content_hash=hash_segments_func(resolved_segments),
    )
    session.add(revision)
    session.flush()
    child_records: list[Segment] = []
    speech_markup = (artifact.metadata_json or {}).get("speech_markup") or {}
    speech_metadata: dict[int, dict[str, str]] = {}
    if speech_markup:
        from .generation_cast_runtime import remap_markup
        from .generation_controls import get_generation_controls

        characters = get_generation_controls(session, session_id)["characters"]
        for ordinal, item in enumerate(resolved_segments):
            cue_markup = speech_markup.get(str(ordinal + 1))
            if cue_markup:
                speech_metadata[ordinal] = {
                    "speech_xml": remap_markup(cue_markup, str(ordinal + 1), item.text, characters)
                }
    for ordinal, item in enumerate(resolved_segments):
        child = Segment(
            revision_id=revision.id,
            ordinal=ordinal,
            start_ms=item.start_ms,
            end_ms=item.end_ms,
            text=item.text,
            speaker=item.speaker or None,
            metadata_json={
                "speaker_source": speaker_sources[ordinal],
                **speech_metadata.get(ordinal, {}),
                **(
                    passage_review_metadata(
                        [
                            row
                            for row in logical_passages
                            if min(item.end_ms, row["end_ms"]) > max(item.start_ms, row["start_ms"])
                        ]
                    )
                    if logical_passages is not None
                    else {}
                ),
            },
        )
        session.add(child)
        child_records.append(child)
    session.flush()
    document.active_revision_id = revision.id

    for child in child_records:
        overlaps = [
            parent
            for parent in parents
            if child.start_ms is not None
            and child.end_ms is not None
            and parent.start_ms is not None
            and parent.end_ms is not None
            and min(child.end_ms, parent.end_ms) > max(child.start_ms, parent.start_ms)
        ]
        for sequence, parent in enumerate(overlaps):
            session.add(
                SegmentLineage(
                    parent_segment_id=parent.id,
                    child_segment_id=child.id,
                    relation="temporal_overlap",
                    sequence=sequence,
                )
            )

    speakers = {child.speaker.casefold(): child.speaker for child in child_records if child.speaker}
    managed.metadata_json = {
        **(managed.metadata_json or {}),
        "document_id": document.id,
        "revision_id": revision.id,
        "stage": stage,
        "language": language,
        "has_speaker_metadata": bool(speakers),
        "speaker_count": len(speakers),
        "speaker_reviewed_by_model": any(source == "model_reviewed" for source in speaker_sources),
    }
    if logical_passages is not None and parent_artifact is not None:
        attach_passages(managed, logical_passages, source=parent_artifact)
        managed.metadata_json = {
            **managed.metadata_json,
            "uncertain_segment_count": sum(
                child.metadata_json.get("review_state") == "uncertain" for child in child_records
            ),
        }
    return document.id, revision.id
