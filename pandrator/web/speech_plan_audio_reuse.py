"""Conservatively inherit compatible audio when a speech plan is rebuilt."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m

_BATCH_SIZE = 500
_IDENTITY_REASONS = frozenset(
    {
        "audio_unavailable",
        "not_completed",
        "audio_identity_unknown",
        "generation_settings_changed",
        "voice_reference_changed",
        "performance_changed",
    }
)
_REASON_CODES = frozenset(
    {
        *_IDENTITY_REASONS,
        "provenance_unavailable",
        "provenance_ambiguous",
        "source_revision_unavailable",
        "source_document_changed",
        "rvc_enabled",
        "synthesized_text_changed",
        "target_has_take",
        "source_take_ambiguous",
    }
)


def _chunks(values: list[str]) -> Iterable[list[str]]:
    for offset in range(0, len(values), _BATCH_SIZE):
        yield values[offset : offset + _BATCH_SIZE]


def _receipt(target_count: int, reused_count: int, reasons: Counter[str]) -> dict[str, Any]:
    return {
        "reused_audio_count": reused_count,
        "new_audio_needed_count": max(0, target_count - reused_count),
        "reason_counts": {
            code: int(reasons[code])
            for code in sorted(_REASON_CODES)
            if reasons[code]
        },
    }


def _is_ref(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool)) or (
        isinstance(value, str) and bool(value)
    )


def _typed_refs(segment: m.GenerationSegment) -> tuple[tuple[str, int | str], ...] | None:
    refs = segment.source_segment_ids_json
    if not isinstance(refs, list) or not refs or any(not _is_ref(ref) for ref in refs):
        return None
    return tuple(
        ("int", ref) if isinstance(ref, int) else ("str", ref)
        for ref in refs
    )


def _document_provenance_key(
    segment: m.GenerationSegment,
    document_id: str,
    cues_by_id: dict[str, m.Segment],
    cues_by_ordinal: dict[int, m.Segment],
) -> tuple[Any, ...] | None:
    refs = segment.source_segment_ids_json
    if not isinstance(refs, list) or not refs:
        return None
    cue_keys: list[tuple[int, int | float | None, int | float | None]] = []
    seen_cues: set[str] = set()
    for reference in refs:
        if isinstance(reference, bool) or not isinstance(reference, (str, int)):
            return None
        by_id = cues_by_id.get(reference) if isinstance(reference, str) else None
        by_ordinal = None
        if isinstance(reference, int):
            by_ordinal = cues_by_ordinal.get(reference)
        elif reference.isdecimal():
            by_ordinal = cues_by_ordinal.get(int(reference))
        matches = {cue.id: cue for cue in (by_id, by_ordinal) if cue is not None}
        if len(matches) != 1:
            return None
        cue = next(iter(matches.values()))
        if cue.id in seen_cues:
            return None
        seen_cues.add(cue.id)
        if segment.node_kind == "subtitle_cue":
            start = cue.start_ms
            end = cue.end_ms
            if (
                isinstance(start, bool)
                or not isinstance(start, (int, float))
                or isinstance(end, bool)
                or not isinstance(end, (int, float))
                or not math.isfinite(float(start))
                or not math.isfinite(float(end))
                or start < 0
                or end <= start
            ):
                return None
        else:
            start = cue.start_ms
            end = cue.end_ms
        cue_keys.append((cue.ordinal, start, end))
    return (
        document_id,
        tuple(cue_keys),
        segment.node_kind,
        segment.alignment_group,
    )


def _narration_provenance_key(
    segment: m.GenerationSegment,
) -> tuple[Any, ...] | None:
    refs = _typed_refs(segment)
    if refs is None:
        return None
    return (refs, segment.node_kind, segment.alignment_group)


def _provenance_keys(
    segments: list[m.GenerationSegment],
    *,
    document_id: str | None,
    cues: dict[
        str, tuple[dict[str, m.Segment], dict[int, m.Segment]]
    ] | None,
) -> dict[str, tuple[Any, ...] | None]:
    result: dict[str, tuple[Any, ...] | None] = {}
    for segment in segments:
        if document_id is None:
            result[segment.id] = _narration_provenance_key(segment)
            continue
        revision_cues = (cues or {}).get(segment.plan_revision_id)
        if revision_cues is None:
            result[segment.id] = None
            continue
        cues_by_id, cues_by_ordinal = revision_cues
        result[segment.id] = _document_provenance_key(
            segment, document_id, cues_by_id, cues_by_ordinal
        )
    return result


def _document_cues(
    session: Session, revision_id: str
) -> tuple[dict[str, m.Segment], dict[int, m.Segment]]:
    rows = list(
        session.scalars(select(m.Segment).where(m.Segment.revision_id == revision_id)).all()
    )
    return ({row.id: row for row in rows}, {row.ordinal + 1: row for row in rows})


def _artifact_file_is_managed_and_present(paths: Any, artifact: m.Artifact) -> bool:
    if not artifact.relative_path:
        return False
    try:
        path = paths.managed_path(artifact.relative_path)
        path.resolve(strict=True).relative_to(paths.root.resolve(strict=True))
        return path.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def inherit_rebuilt_plan_audio(
    session: Session,
    source_revision_id: str | None,
    target_revision_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Copy only completed, identity-compatible audio from the immediate parent plan.

    ``source_revision_id`` is the previously active GenerationPlanRevision ID
    supplied by the caller's revision guard.
    """
    target_revision = session.get(m.GenerationPlanRevision, target_revision_id)
    if target_revision is None:
        return _receipt(0, 0, Counter())
    targets = list(
        session.scalars(
            select(m.GenerationSegment)
            .where(
                m.GenerationSegment.plan_revision_id == target_revision_id,
                m.GenerationSegment.removed.is_(False),
            )
            .order_by(m.GenerationSegment.ordinal)
        ).all()
    )
    target_count = len(targets)
    reasons: Counter[str] = Counter()
    if not targets:
        return _receipt(0, 0, reasons)

    plan = session.get(m.GenerationPlan, target_revision.plan_id)
    source_revision = (
        session.get(m.GenerationPlanRevision, source_revision_id)
        if source_revision_id
        else None
    )
    if (
        plan is None
        or plan.active_revision_id != target_revision_id
        or source_revision is None
        or source_revision.plan_id != target_revision.plan_id
        or target_revision.parent_revision_id != source_revision_id
    ):
        reasons["source_revision_unavailable"] += target_count
        return _receipt(target_count, 0, reasons)

    source_segments = list(
        session.scalars(
            select(m.GenerationSegment)
            .where(
                m.GenerationSegment.plan_revision_id == source_revision_id,
                m.GenerationSegment.removed.is_(False),
            )
            .order_by(m.GenerationSegment.ordinal)
        ).all()
    )

    source_document_revision_id = source_revision.source_revision_id
    target_document_revision_id = target_revision.source_revision_id
    document_id: str | None = None
    cue_rows: dict[
        str, tuple[dict[str, m.Segment], dict[int, m.Segment]]
    ] | None = None
    if (
        source_document_revision_id is not None
        and target_document_revision_id is not None
    ):
        source_document_revision = session.get(
            m.DocumentRevision, source_document_revision_id
        )
        target_document_revision = session.get(
            m.DocumentRevision, target_document_revision_id
        )
        source_document = (
            session.get(m.Document, source_document_revision.document_id)
            if source_document_revision is not None
            else None
        )
        target_document = (
            session.get(m.Document, target_document_revision.document_id)
            if target_document_revision is not None
            else None
        )
        if (
            source_document is None
            or target_document is None
            or source_document.session_id != plan.session_id
            or target_document.session_id != plan.session_id
            or source_document.id != target_document.id
        ):
            reasons["source_document_changed"] += target_count
            return _receipt(target_count, 0, reasons)
        document_id = source_document.id
        cue_rows = {
            source_revision.id: _document_cues(session, source_document_revision_id),
            target_revision_id: _document_cues(session, target_document_revision_id),
        }
    elif source_document_revision_id or target_document_revision_id:
        reasons["source_document_changed"] += target_count
        return _receipt(target_count, 0, reasons)
    else:
        source_artifact_id = (source_revision.settings_json or {}).get("_source_artifact_id")
        target_artifact_id = (target_revision.settings_json or {}).get("_source_artifact_id")
        if (
            not isinstance(source_artifact_id, str)
            or not source_artifact_id.strip()
            or source_artifact_id != target_artifact_id
        ):
            reasons["source_document_changed"] += target_count
            return _receipt(target_count, 0, reasons)

    source_keys = _provenance_keys(
        source_segments, document_id=document_id, cues=cue_rows
    )
    target_keys = _provenance_keys(targets, document_id=document_id, cues=cue_rows)
    source_by_key: dict[tuple[Any, ...], list[m.GenerationSegment]] = defaultdict(list)
    target_key_counts: Counter[tuple[Any, ...]] = Counter()
    for segment in source_segments:
        key = source_keys.get(segment.id)
        if key is not None:
            source_by_key[key].append(segment)
    for segment in targets:
        key = target_keys.get(segment.id)
        if key is not None:
            target_key_counts[key] += 1

    target_ids = [segment.id for segment in targets]
    existing_target_ids: set[str] = set()
    for chunk in _chunks(target_ids):
        existing_target_ids.update(
            str(segment_id)
            for segment_id in session.scalars(
                select(m.AudioTake.generation_segment_id)
                .where(m.AudioTake.generation_segment_id.in_(chunk))
                .distinct()
            ).all()
        )

    source_ids = [segment.id for segment in source_segments]
    active_takes_by_segment: dict[str, list[m.AudioTake]] = defaultdict(list)
    for chunk in _chunks(source_ids):
        takes = session.scalars(
            select(m.AudioTake).where(
                m.AudioTake.generation_segment_id.in_(chunk),
                m.AudioTake.kind == "tts",
                m.AudioTake.is_active.is_(True),
            )
        ).all()
        for take in takes:
            active_takes_by_segment[take.generation_segment_id].append(take)

    artifact_ids = sorted(
        {
            take.artifact_id
            for takes in active_takes_by_segment.values()
            for take in takes
            if take.artifact_id
        }
    )
    artifacts_by_id: dict[str, m.Artifact] = {}
    for chunk in _chunks(artifact_ids):
        artifacts_by_id.update(
            (artifact.id, artifact)
            for artifact in session.scalars(
                select(m.Artifact).where(m.Artifact.id.in_(chunk))
            ).all()
        )

    selected = snapshot.get("selected_segment_override") or {}
    global_rvc = snapshot.get("rvc") or {}
    selected_rvc = selected.get("rvc") or {} if isinstance(selected, dict) else {}
    if bool(global_rvc.get("enabled")) or bool(selected_rvc.get("enabled")):
        reasons["rvc_enabled"] += target_count
        return _receipt(target_count, 0, reasons)

    try:
        from .generation_audio_identity import AudioIdentityContext, take_reuse_reason

        identity_context = AudioIdentityContext(session, snapshot)
    except Exception:
        reasons["audio_identity_unknown"] += target_count
        return _receipt(target_count, 0, reasons)

    reused_count = 0
    for target in targets:
        key = target_keys.get(target.id)
        if key is None:
            reasons["provenance_unavailable"] += 1
            continue
        if target_key_counts[key] > 1 or len(source_by_key.get(key, [])) > 1:
            reasons["provenance_ambiguous"] += 1
            continue
        source_matches = source_by_key.get(key, [])
        if not source_matches:
            reasons["provenance_unavailable"] += 1
            continue
        source = source_matches[0]
        if target.id in existing_target_ids:
            reasons["target_has_take"] += 1
            continue
        if source.status != "completed":
            reasons["not_completed"] += 1
            continue
        source_takes = active_takes_by_segment.get(source.id, [])
        if not source_takes:
            reasons["audio_unavailable"] += 1
            continue
        if len(source_takes) != 1:
            reasons["source_take_ambiguous"] += 1
            continue
        source_take = source_takes[0]
        artifact = artifacts_by_id.get(source_take.artifact_id or "")
        if (
            artifact is None
            or artifact.state != "current"
            or artifact.session_id != plan.session_id
            or not _artifact_file_is_managed_and_present(identity_context.paths, artifact)
        ):
            reasons["audio_unavailable"] += 1
            continue
        metadata = artifact.metadata_json or {}
        synthesized_text = metadata.get("synthesized_text") if isinstance(metadata, dict) else None
        source_spoken_text = source.optimized_text or source.text
        target_spoken_text = target.optimized_text or target.text
        if (
            not isinstance(synthesized_text, str)
            or synthesized_text != source_spoken_text
            or synthesized_text != target_spoken_text
        ):
            reasons["synthesized_text_changed"] += 1
            continue
        try:
            expected_identity = identity_context.for_segment(target)
            reason = take_reuse_reason(source, source_take, artifact, expected_identity)
        except Exception:
            reason = "audio_identity_unknown"
        if reason != "reusable":
            reasons[reason if reason in _IDENTITY_REASONS else "audio_identity_unknown"] += 1
            continue
        session.add(
            m.AudioTake(
                generation_segment_id=target.id,
                generation_run_id=None,
                artifact_id=source_take.artifact_id,
                parent_take_id=source_take.id,
                kind="tts",
                status="completed",
                settings_hash=source_take.settings_hash,
                duration_ms=source_take.duration_ms,
                is_active=True,
                revision=source_take.revision,
            )
        )
        target.status = "completed"
        reused_count += 1

    return _receipt(target_count, reused_count, reasons)
