"""Conservatively inherit compatible audio when a speech plan is rebuilt."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m

_BATCH_SIZE = 500
# How far back one inheritance pass looks along the plan's own parent chain.
# A take older than this is still in history; it is only out of reach here.
_ANCESTOR_LIMIT = 20
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


def _receipt(
    target_count: int,
    reused_count: int,
    reasons: Counter[str],
    matching: str = "exact_bounds",
    *,
    revisions_checked: int = 0,
    reused_from_older_revisions: int = 0,
) -> dict[str, Any]:
    return {
        "reused_audio_count": reused_count,
        "new_audio_needed_count": max(0, target_count - reused_count),
        "matching": matching,
        "revisions_checked": revisions_checked,
        "reused_from_older_revisions": reused_from_older_revisions,
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
    *,
    timing_tolerant: bool = False,
) -> tuple[Any, ...] | None:
    refs = segment.source_segment_ids_json
    if not isinstance(refs, list) or not refs:
        return None
    cue_keys: list[tuple[Any, ...]] = []
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
        # A timing-only source keeps the same cues in the same order; only the
        # display document and the exact windows move.
        cue_keys.append((cue.ordinal,) if timing_tolerant else (cue.ordinal, start, end))
    if timing_tolerant:
        return (tuple(cue_keys), segment.node_kind, segment.alignment_group)
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
    timing_tolerant: bool = False,
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
            segment,
            document_id,
            cues_by_id,
            cues_by_ordinal,
            timing_tolerant=timing_tolerant,
        )
    return result


def _document_cues(
    session: Session, revision_id: str
) -> tuple[dict[str, m.Segment], dict[int, m.Segment]]:
    rows = list(
        session.scalars(select(m.Segment).where(m.Segment.revision_id == revision_id)).all()
    )
    return ({row.id: row for row in rows}, {row.ordinal + 1: row for row in rows})


def _revision_cues(
    session: Session,
    cache: dict[str, tuple[dict[str, m.Segment], dict[int, m.Segment]]],
    document_revision_id: str,
) -> tuple[dict[str, m.Segment], dict[int, m.Segment]]:
    """Read one document revision's cues once, for every ancestor that shares it."""
    cues = cache.get(document_revision_id)
    if cues is None:
        cues = _document_cues(session, document_revision_id)
        cache[document_revision_id] = cues
    return cues


def _artifact_file_is_managed_and_present(paths: Any, artifact: m.Artifact) -> bool:
    if not artifact.relative_path:
        return False
    try:
        path = paths.managed_path(artifact.relative_path)
        path.resolve(strict=True).relative_to(paths.root.resolve(strict=True))
        return path.is_file()
    except (OSError, RuntimeError, ValueError):
        return False


def _source_document(
    session: Session,
    ancestor: m.GenerationPlanRevision,
    target_revision: m.GenerationPlanRevision,
    plan: m.GenerationPlan,
    *,
    other_document: bool,
) -> tuple[bool, str | None]:
    """Whether an ancestor describes the same source as the target.

    ``(False, None)``: its source document is missing, outside the plan's
    session, or another document that its signatures do not tie to the
    target's, so the ancestor is skipped as a whole. ``(True, None)``: a
    narration plan, whose blocks key on their typed passage references instead
    of a document.
    """
    source_document_revision_id = ancestor.source_revision_id
    target_document_revision_id = target_revision.source_revision_id
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
            or (source_document.id != target_document.id and not other_document)
        ):
            return False, None
        return True, source_document.id
    if source_document_revision_id or target_document_revision_id:
        return False, None
    source_artifact_id = (ancestor.settings_json or {}).get("_source_artifact_id")
    target_artifact_id = (target_revision.settings_json or {}).get("_source_artifact_id")
    if (
        not isinstance(source_artifact_id, str)
        or not source_artifact_id.strip()
        or source_artifact_id != target_artifact_id
    ):
        return False, None
    return True, None


def _rvc_requested(snapshot: dict[str, Any]) -> bool:
    selected = snapshot.get("selected_segment_override") or {}
    global_rvc = snapshot.get("rvc") or {}
    selected_rvc = selected.get("rvc") or {} if isinstance(selected, dict) else {}
    return bool(global_rvc.get("enabled")) or bool(selected_rvc.get("enabled"))


def _active_takes(
    session: Session, segment_ids: list[str]
) -> dict[str, list[m.AudioTake]]:
    takes_by_segment: dict[str, list[m.AudioTake]] = defaultdict(list)
    for chunk in _chunks(segment_ids):
        takes = session.scalars(
            select(m.AudioTake).where(
                m.AudioTake.generation_segment_id.in_(chunk),
                m.AudioTake.kind == "tts",
                m.AudioTake.is_active.is_(True),
            )
        ).all()
        for take in takes:
            takes_by_segment[take.generation_segment_id].append(take)
    return takes_by_segment


def _artifacts(
    session: Session, takes_by_segment: dict[str, list[m.AudioTake]]
) -> dict[str, m.Artifact]:
    artifact_ids = sorted(
        {
            take.artifact_id
            for takes in takes_by_segment.values()
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
    return artifacts_by_id


@dataclass
class _AncestorIndex:
    """One ancestor revision's blocks, ready for per-target lookups."""

    target_keys: dict[str, tuple[Any, ...] | None]
    target_key_counts: Counter[tuple[Any, ...]]
    source_by_key: dict[tuple[Any, ...], list[m.GenerationSegment]]
    takes_by_segment: dict[str, list[m.AudioTake]]
    artifacts_by_id: dict[str, m.Artifact]


def _ancestor_index(
    session: Session,
    ancestor: m.GenerationPlanRevision,
    targets: list[m.GenerationSegment],
    *,
    document_id: str | None,
    cues: dict[str, tuple[dict[str, m.Segment], dict[int, m.Segment]]],
    timing_tolerant: bool,
) -> _AncestorIndex:
    source_segments = list(
        session.scalars(
            select(m.GenerationSegment)
            .where(
                m.GenerationSegment.plan_revision_id == ancestor.id,
                m.GenerationSegment.removed.is_(False),
            )
            .order_by(m.GenerationSegment.ordinal)
        ).all()
    )
    source_keys = _provenance_keys(
        source_segments,
        document_id=document_id,
        cues=cues,
        timing_tolerant=timing_tolerant,
    )
    target_keys = _provenance_keys(
        targets,
        document_id=document_id,
        cues=cues,
        timing_tolerant=timing_tolerant,
    )
    source_by_key: dict[tuple[Any, ...], list[m.GenerationSegment]] = defaultdict(list)
    for segment in source_segments:
        key = source_keys.get(segment.id)
        if key is not None:
            source_by_key[key].append(segment)
    target_key_counts: Counter[tuple[Any, ...]] = Counter(
        key for key in target_keys.values() if key is not None
    )
    takes_by_segment = _active_takes(session, [segment.id for segment in source_segments])
    return _AncestorIndex(
        target_keys=target_keys,
        target_key_counts=target_key_counts,
        source_by_key=source_by_key,
        takes_by_segment=takes_by_segment,
        artifacts_by_id=_artifacts(session, takes_by_segment),
    )


def _candidate_take(
    index: _AncestorIndex,
    target: m.GenerationSegment,
    *,
    plan: m.GenerationPlan,
    existing_target_ids: set[str],
    identity_context: Any,
) -> tuple[m.AudioTake | None, str | None]:
    """The take this ancestor can give ``target``, or the reason it cannot."""
    from .generation_audio_identity import take_reuse_reason

    key = index.target_keys.get(target.id)
    if key is None:
        return None, "provenance_unavailable"
    if index.target_key_counts[key] > 1:
        return None, "provenance_ambiguous"
    source_matches = index.source_by_key.get(key) or []
    if not source_matches:
        return None, "provenance_unavailable"
    if len(source_matches) > 1:
        return None, "provenance_ambiguous"
    if target.id in existing_target_ids:
        return None, "target_has_take"
    source = source_matches[0]
    if source.status != "completed":
        return None, "not_completed"
    source_takes = index.takes_by_segment.get(source.id) or []
    if not source_takes:
        return None, "audio_unavailable"
    if len(source_takes) != 1:
        return None, "source_take_ambiguous"
    source_take = source_takes[0]
    artifact = index.artifacts_by_id.get(source_take.artifact_id or "")
    if (
        artifact is None
        or artifact.state != "current"
        or artifact.session_id != plan.session_id
        or not _artifact_file_is_managed_and_present(identity_context.paths, artifact)
    ):
        return None, "audio_unavailable"
    metadata = artifact.metadata_json or {}
    synthesized_text = metadata.get("synthesized_text") if isinstance(metadata, dict) else None
    if (
        not isinstance(synthesized_text, str)
        or synthesized_text != (source.optimized_text or source.text)
        or synthesized_text != (target.optimized_text or target.text)
    ):
        return None, "synthesized_text_changed"
    try:
        expected_identity = identity_context.for_segment(target)
        reason = take_reuse_reason(source, source_take, artifact, expected_identity)
    except Exception:
        reason = "audio_identity_unknown"
    if reason != "reusable":
        return None, reason if reason in _IDENTITY_REASONS else "audio_identity_unknown"
    return source_take, None


def inherit_rebuilt_plan_audio(
    session: Session,
    source_revision_id: str | None,
    target_revision_id: str,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Copy only completed, identity-compatible audio from the plan's ancestors.

    ``source_revision_id`` is the previously active GenerationPlanRevision ID
    supplied by the caller's revision guard and must be the target revision's
    immediate parent. The search then follows that revision's own parents
    within the same plan, at most :data:`_ANCESTOR_LIMIT` revisions, stopping
    at another plan, a missing revision or a cycle. Each target block takes its
    take from the nearest ancestor where every check passes for that block, so
    a block that is ambiguous or edited in one ancestor can still be served by
    an older one. ``reason_counts`` stays comparable with earlier versions: a
    block that found no take is counted with the immediate parent's reason.

    When an ancestor's stored source signatures differ from the target's only
    in cue timing, the same cues in the same order keep their takes and only
    their windows move; ``matching`` records the immediate parent's mode,
    ``"timing_tolerant"`` or ``"exact_bounds"``, and ``revisions_checked`` and
    ``reused_from_older_revisions`` report the walk itself.
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

    from .speech_plan_source import plan_source_compatibility, stored_signatures

    target_settings = target_revision.settings_json or {}
    current_source = {
        "id": target_settings.get("_source_artifact_id"),
        "content_hash": target_settings.get("_source_content_hash"),
    }
    target_signatures = stored_signatures(target_settings)

    # Each ancestor with whether its cue windows may differ (a timing-only
    # edit) and whether it may name another document: either a timing-only
    # edit, or recorded signatures equal to the target's. The legacy "exact"
    # for a revision that recorded no source is not evidence of either.
    ancestors: list[tuple[m.GenerationPlanRevision, bool, bool]] = []
    seen: set[str] = set()
    ancestor: m.GenerationPlanRevision | None = source_revision
    while ancestor is not None and len(ancestors) < _ANCESTOR_LIMIT:
        if ancestor.id in seen or ancestor.plan_id != plan.id:
            break
        seen.add(ancestor.id)
        ancestor_settings = ancestor.settings_json or {}
        compatibility = plan_source_compatibility(
            ancestor_settings, current_source, target_signatures
        )
        ancestor_signatures = stored_signatures(ancestor_settings)
        timing_tolerant = compatibility == "timing_only"
        ancestors.append(
            (
                ancestor,
                timing_tolerant,
                timing_tolerant
                or (
                    compatibility == "exact"
                    and ancestor_signatures is not None
                    and ancestor_signatures == target_signatures
                ),
            )
        )
        ancestor = (
            session.get(m.GenerationPlanRevision, ancestor.parent_revision_id)
            if ancestor.parent_revision_id
            else None
        )
    matching = "timing_tolerant" if ancestors[0][1] else "exact_bounds"

    # The immediate parent's source document answers before RVC does, as it did
    # before the walk; RVC itself still refuses the whole call.
    parent_document = _source_document(
        session, ancestors[0][0], target_revision, plan, other_document=ancestors[0][2]
    )
    if _rvc_requested(snapshot):
        reasons[
            "rvc_enabled" if parent_document[0] else "source_document_changed"
        ] += target_count
        return _receipt(target_count, 0, reasons, matching)

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

    pending = list(targets)
    # The immediate parent's answer for each block, kept only for blocks that
    # find no take anywhere; it is what the receipt has always counted.
    provisional: dict[str, str] = {}
    reused_ids: set[str] = set()
    cue_cache: dict[str, tuple[dict[str, m.Segment], dict[int, m.Segment]]] = {}
    identity_context: Any = None
    identity_failed = False
    reused_count = 0
    reused_from_older = 0
    revisions_checked = 0
    for position, (ancestor, timing_tolerant, other_document) in enumerate(ancestors):
        if not pending:
            break
        document = (
            parent_document
            if position == 0
            else _source_document(
                session,
                ancestor,
                target_revision,
                plan,
                other_document=other_document,
            )
        )
        revisions_checked += 1
        if not document[0]:
            # An ancestor that fails the document check is skipped as a whole.
            if position == 0:
                provisional = {
                    target.id: "source_document_changed" for target in pending
                }
            continue
        if identity_context is None and not identity_failed:
            try:
                from .generation_audio_identity import AudioIdentityContext

                identity_context = AudioIdentityContext(session, snapshot)
            except Exception:
                identity_failed = True
        if identity_failed:
            if position > 0:
                # Only an older ancestor got this far; the parent's own reason
                # for each block still stands and is counted below.
                break
            reasons["audio_identity_unknown"] += target_count
            return _receipt(
                target_count, 0, reasons, matching, revisions_checked=revisions_checked
            )
        cue_rows: dict[str, tuple[dict[str, m.Segment], dict[int, m.Segment]]] = {}
        if ancestor.source_revision_id is not None:
            cue_rows[ancestor.id] = _revision_cues(
                session, cue_cache, ancestor.source_revision_id
            )
        if target_revision.source_revision_id is not None:
            cue_rows[target_revision_id] = _revision_cues(
                session, cue_cache, target_revision.source_revision_id
            )
        index = _ancestor_index(
            session,
            ancestor,
            targets,
            document_id=document[1],
            cues=cue_rows,
            timing_tolerant=timing_tolerant,
        )
        resolved: list[tuple[m.GenerationSegment, m.AudioTake]] = []
        unresolved: list[m.GenerationSegment] = []
        for target in pending:
            source_take, reason = _candidate_take(
                index,
                target,
                plan=plan,
                existing_target_ids=existing_target_ids,
                identity_context=identity_context,
            )
            if source_take is None:
                if position == 0:
                    provisional[target.id] = reason or "audio_unavailable"
                # The target's own take settles the block; no ancestor can
                # improve on it, and the reason above still counts it.
                if reason != "target_has_take":
                    unresolved.append(target)
                continue
            resolved.append((target, source_take))
        pending = unresolved
        for target, source_take in resolved:
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
            reused_ids.add(target.id)
            reused_count += 1
            if position > 0:
                reused_from_older += 1

    for segment_id, reason in provisional.items():
        if segment_id not in reused_ids:
            reasons[reason] += 1

    return _receipt(
        target_count,
        reused_count,
        reasons,
        matching,
        revisions_checked=revisions_checked,
        reused_from_older_revisions=reused_from_older,
    )
