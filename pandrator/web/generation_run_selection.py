"""Resolve the audio take selected by a generation run for each segment."""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from sqlalchemy import func, select, tuple_
from sqlalchemy.orm import Session

from .models import AudioTake, GenerationRun, GenerationSegment

_AVAILABLE_TAKE_STATUSES = ("completed", "stale")
_GUARDED_SELECTION_BATCH_SIZE = 250


def resolve_generation_run_take_ids(
    session: Session,
    run: GenerationRun,
    *,
    segment_ids: Sequence[str] | None = None,
) -> dict[str, str]:
    """Return the selected take per current segment in ``run``'s plan.

    Normal selection is cumulative within the same session and plan, ending at
    the selected run's sequence. Standalone partial regenerations can also
    retain untouched edit-copy audio through the artifact identities captured
    in that run's immutable settings snapshot.
    """
    scoped_ids = list(dict.fromkeys(segment_ids)) if segment_ids is not None else None
    if scoped_ids == []:
        return {}

    eligible_segments = select(GenerationSegment.id).where(
        GenerationSegment.plan_revision_id == run.plan_revision_id,
        GenerationSegment.removed.is_(False),
    )
    if scoped_ids is not None:
        eligible_segments = eligible_segments.where(
            GenerationSegment.id.in_(scoped_ids)
        )

    ranked_takes = (
        select(
            AudioTake.id.label("take_id"),
            AudioTake.generation_segment_id.label("segment_id"),
            func.row_number()
            .over(
                partition_by=AudioTake.generation_segment_id,
                order_by=(
                    GenerationRun.sequence_number.desc(),
                    AudioTake.created_at.desc(),
                    AudioTake.id.desc(),
                ),
            )
            .label("take_rank"),
        )
        .join(GenerationRun, GenerationRun.id == AudioTake.generation_run_id)
        .join(
            GenerationSegment,
            GenerationSegment.id == AudioTake.generation_segment_id,
        )
        .where(
            GenerationRun.session_id == run.session_id,
            GenerationRun.plan_revision_id == run.plan_revision_id,
            GenerationRun.sequence_number <= run.sequence_number,
            AudioTake.status.in_(_AVAILABLE_TAKE_STATUSES),
            GenerationSegment.plan_revision_id == run.plan_revision_id,
            GenerationSegment.removed.is_(False),
            GenerationSegment.id.in_(eligible_segments),
        )
        .subquery()
    )
    selected = {
        segment_id: take_id
        for segment_id, take_id in session.execute(
            select(ranked_takes.c.segment_id, ranked_takes.c.take_id).where(
                ranked_takes.c.take_rank == 1
            )
        )
    }

    snapshot = run.settings_snapshot_json
    if not (
        run.operation == "regenerate"
        and run.output_generation_run_id is None
        and isinstance(snapshot, Mapping)
    ):
        return selected

    requested_ids = snapshot.get("generation_request_segment_ids")
    selection_guards = snapshot.get("generation_selection_guards")
    if (
        not isinstance(requested_ids, list)
        or not requested_ids
        or any(not isinstance(item, str) or not item for item in requested_ids)
        or not isinstance(selection_guards, Mapping)
    ):
        return selected

    requested = set(requested_ids)
    in_scope = set(scoped_ids) if scoped_ids is not None else None
    guarded_pairs: list[tuple[str, str]] = []
    for segment_id, guard in selection_guards.items():
        if not isinstance(segment_id, str):
            continue
        if in_scope is not None and segment_id not in in_scope:
            continue
        if segment_id in requested or segment_id in selected:
            continue
        if not isinstance(guard, Mapping):
            continue
        artifact_id = guard.get("artifact_id")
        if isinstance(artifact_id, str) and artifact_id:
            guarded_pairs.append((segment_id, artifact_id))

    for start in range(0, len(guarded_pairs), _GUARDED_SELECTION_BATCH_SIZE):
        pairs = guarded_pairs[start : start + _GUARDED_SELECTION_BATCH_SIZE]
        guarded_rank = (
            select(
                AudioTake.id.label("take_id"),
                AudioTake.generation_segment_id.label("segment_id"),
                func.row_number()
                .over(
                    partition_by=AudioTake.generation_segment_id,
                    order_by=(AudioTake.created_at.desc(), AudioTake.id.desc()),
                )
                .label("take_rank"),
            )
            .join(
                GenerationSegment,
                GenerationSegment.id == AudioTake.generation_segment_id,
            )
            .where(
                tuple_(AudioTake.generation_segment_id, AudioTake.artifact_id).in_(
                    pairs
                ),
                GenerationSegment.plan_revision_id == run.plan_revision_id,
                GenerationSegment.removed.is_(False),
                AudioTake.status.in_(_AVAILABLE_TAKE_STATUSES),
                AudioTake.created_at <= run.created_at,
            )
            .subquery()
        )
        for segment_id, take_id in session.execute(
            select(guarded_rank.c.segment_id, guarded_rank.c.take_id).where(
                guarded_rank.c.take_rank == 1
            )
        ):
            selected.setdefault(segment_id, take_id)

    return selected
