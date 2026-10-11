"""Safe reads for admitted generation operations and their frozen audio coverage."""

from __future__ import annotations

from typing import Any, Literal

from sqlalchemy import select, tuple_
from sqlalchemy.orm import load_only

from pandrator.runtime import DataPaths, PathBoundaryError

from .database import Database
from .generation_audio_versions import audio_version_take_ids
from .generation_run_selection import resolve_generation_run_take_ids
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
)
from .work import TERMINAL_STATES, WorkService, WorkView

_JOB_COLUMNS = (
    Job.id,
    Job.kind,
    Job.session_id,
    Job.workflow_run_id,
    Job.status,
    Job.progress,
    Job.progress_detail,
    Job.result_json,
    Job.payload_json,
    Job.error_code,
    Job.error_message,
    Job.created_at,
    Job.started_at,
    Job.finished_at,
    Job.updated_at,
    Job.attempts,
    Job.max_attempts,
)
_RUN_COLUMNS = (
    GenerationRun.id,
    GenerationRun.session_id,
    GenerationRun.plan_revision_id,
    GenerationRun.job_id,
    GenerationRun.sequence_number,
    GenerationRun.operation,
    GenerationRun.output_generation_run_id,
    GenerationRun.settings_snapshot_json,
    GenerationRun.audio_version_json,
    GenerationRun.created_at,
)
_AVAILABLE_TAKE_STATUSES = frozenset({"completed", "stale"})
_TAKE_LOOKUP_BATCH_SIZE = 500
_PARENT_TAKE_LOOKUP_BATCH_SIZE = 500
_MAX_OPERATION_ID_LENGTH = 120


def _job_statement():
    return select(Job).options(load_only(*_JOB_COLUMNS))


def _run_statement():
    return select(GenerationRun).options(load_only(*_RUN_COLUMNS))


def _validated_run(
    session,
    *,
    run_id: str,
    session_id: str,
    job_id: str,
) -> GenerationRun:
    return session.scalar(
        _run_statement().where(
            GenerationRun.id == run_id,
            GenerationRun.session_id == session_id,
            GenerationRun.job_id == job_id,
        )
    )


def _valid_id(value: object, *, maximum: int = _MAX_OPERATION_ID_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > maximum:
        return None
    return normalized


def _compact_work(view: WorkView) -> dict[str, Any]:
    """Return only the public, bounded fields needed to poll this phase."""

    return {
        "id": view.id,
        "kind": view.kind,
        "state": view.state,
        "progress": view.progress,
        "detail": view.detail,
        "error": view.error.model_dump(mode="json") if view.error else None,
        "poll_after_ms": view.poll_after_ms,
    }


def _error_projection(view: WorkView | None) -> dict[str, str | None] | None:
    if view is None or view.error is None:
        return None
    return {
        "code": view.error.code,
        "message": view.error.message[:2_000],
    }


def _validated_admission_link(
    session,
    root: Job,
) -> tuple[GenerationRun | None, Job | None, str | None]:
    marker = root.result_json if isinstance(root.result_json, dict) else {}
    run_id = _valid_id(marker.get("generation_run_id"), maximum=80)
    child_id = _valid_id(marker.get("job_id"), maximum=120)
    if run_id is None or child_id is None or root.session_id is None:
        return None, None, "Generation run linkage is not available yet."

    # First constrain the run to this session. Do not use the marker's child ID
    # as a work lookup until the run proves the same durable relationship.
    run_identity = session.execute(
        select(GenerationRun.id, GenerationRun.job_id).where(
            GenerationRun.id == run_id,
            GenerationRun.session_id == root.session_id,
            GenerationRun.job_id == child_id,
        )
    ).one_or_none()
    if run_identity is None:
        return None, None, "Generation run linkage does not match this operation."

    child_identity = session.execute(
        select(Job.id, Job.kind, Job.session_id, Job.payload_json).where(
            Job.id == child_id,
            Job.kind == "generation.run",
            Job.session_id == root.session_id,
        )
    ).one_or_none()
    child_payload = (
        child_identity.payload_json
        if child_identity and isinstance(child_identity.payload_json, dict)
        else {}
    )
    if (
        child_identity is None
        or child_identity.session_id != root.session_id
        or child_payload.get("generation_run_id") != run_id
    ):
        return None, None, "Generation run linkage does not match this operation."

    run = _validated_run(
        session,
        run_id=run_id,
        session_id=root.session_id,
        job_id=child_id,
    )
    child = session.scalar(_job_statement().where(Job.id == child_id))
    if run is None or child is None:
        return None, None, "Generation run linkage does not match this operation."
    return run, child, None


def _validated_direct_link(
    session,
    job: Job,
) -> tuple[GenerationRun | None, str | None]:
    payload = job.payload_json if isinstance(job.payload_json, dict) else {}
    run_id = _valid_id(payload.get("generation_run_id"), maximum=80)
    if run_id is None or job.session_id is None:
        return None, "Generation run linkage is not available yet."
    run_identity = session.execute(
        select(GenerationRun.id, GenerationRun.job_id).where(
            GenerationRun.id == run_id,
            GenerationRun.session_id == job.session_id,
            GenerationRun.job_id == job.id,
        )
    )
    if run_identity.one_or_none() is None:
        return None, "Generation run linkage does not match this work item."
    run = _validated_run(
        session,
        run_id=run_id,
        session_id=job.session_id,
        job_id=job.id,
    )
    if run is None:
        return None, "Generation run linkage does not match this work item."
    return run, None


def _generation_run_coverage(
    session,
    database: Database,
    run: GenerationRun,
    *,
    audio_version_side: Literal["before", "after"],
) -> dict[str, Any]:
    paths = DataPaths(database.path.parent)

    segment_rows = session.execute(
        select(GenerationSegment.id, GenerationSegment.ordinal)
        .where(
            GenerationSegment.plan_revision_id == run.plan_revision_id,
            GenerationSegment.removed.is_(False),
        )
        .order_by(GenerationSegment.ordinal, GenerationSegment.id)
    ).all()
    segment_ordinals = {str(segment_id): int(ordinal) for segment_id, ordinal in segment_rows}

    try:
        version_selection = audio_version_take_ids(run, audio_version_side)
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError("The selected audio version is invalid.") from error

    is_audio_version = version_selection is not None
    if is_audio_version:
        if any(
            not isinstance(segment_id, str)
            or not segment_id
            or len(segment_id) > 80
            or not isinstance(take_id, str)
            or not take_id
            or len(take_id) > 80
            for segment_id, take_id in version_selection.items()
        ):
            raise ValueError("The selected audio version contains invalid take IDs.")
        if run.output_generation_run_id is not None or run.session_id is None:
            raise ValueError("The selected audio version does not belong to this run.")
        revision_session_id = session.scalar(
            select(GenerationPlan.session_id)
            .join(
                GenerationPlanRevision,
                GenerationPlanRevision.plan_id == GenerationPlan.id,
            )
            .where(GenerationPlanRevision.id == run.plan_revision_id)
        )
        if revision_session_id != run.session_id:
            raise ValueError("The selected audio version does not belong to this session.")
        if set(version_selection) != set(segment_ordinals):
            raise ValueError("The selected audio version does not cover this speech plan.")
        selected_take_ids = dict(version_selection)
    else:
        if audio_version_side == "before":
            raise ValueError("Before audio is available only for an audio-version run.")
        selected_take_ids = resolve_generation_run_take_ids(session, run)

    included_count = len(segment_ordinals)
    take_ids = list(dict.fromkeys(selected_take_ids.values()))
    take_records: dict[
        str,
        tuple[
            str,
            str | None,
            str,
            str | None,
            str | None,
            str | None,
            str | None,
            str | None,
        ],
    ] = {}
    for offset in range(0, len(take_ids), _TAKE_LOOKUP_BATCH_SIZE):
        batch = take_ids[offset : offset + _TAKE_LOOKUP_BATCH_SIZE]
        rows = session.execute(
            select(
                AudioTake.id,
                AudioTake.generation_segment_id,
                AudioTake.generation_run_id,
                AudioTake.status,
                Artifact.id,
                Artifact.session_id,
                Artifact.state,
                Artifact.relative_path,
                AudioTake.parent_take_id,
            )
            .outerjoin(Artifact, Artifact.id == AudioTake.artifact_id)
            .where(AudioTake.id.in_(batch))
        )
        for (
            take_id,
            segment_id,
            take_run_id,
            status,
            artifact_id,
            artifact_session_id,
            artifact_state,
            relative_path,
            parent_take_id,
        ) in rows:
            take_records[str(take_id)] = (
                str(segment_id),
                str(take_run_id) if take_run_id is not None else None,
                str(status),
                str(artifact_id) if artifact_id is not None else None,
                str(artifact_session_id) if artifact_session_id is not None else None,
                str(artifact_state) if artifact_state is not None else None,
                str(relative_path) if relative_path is not None else None,
                str(parent_take_id) if parent_take_id is not None else None,
            )

    parent_pairs = list(
        dict.fromkeys(
            (record[7], record[0]) for record in take_records.values() if record[7] is not None
        )
    )
    parent_artifacts: dict[str, str | None] = {}
    for offset in range(0, len(parent_pairs), _PARENT_TAKE_LOOKUP_BATCH_SIZE):
        batch = parent_pairs[offset : offset + _PARENT_TAKE_LOOKUP_BATCH_SIZE]
        rows = session.execute(
            select(AudioTake.id, AudioTake.artifact_id).where(
                tuple_(AudioTake.id, AudioTake.generation_segment_id).in_(batch)
            )
        )
        parent_artifacts.update(
            {
                str(take_id): str(artifact_id) if artifact_id is not None else None
                for take_id, artifact_id in rows
            }
        )

    effective_audio_count = 0
    run_audio_count = 0
    inherited_audio_count = 0
    generated_audio_count = 0
    missing_audio_count = 0
    unavailable_artifact_count = 0
    missing_file_count = 0
    missing_ordinals: list[int] = []

    for segment_id, ordinal in segment_ordinals.items():
        take_id = selected_take_ids.get(segment_id)
        record = take_records.get(str(take_id)) if take_id else None
        if (
            record is None
            or record[0] != segment_id
            or record[2] not in _AVAILABLE_TAKE_STATUSES
            or (is_audio_version and record[1] != run.id)
        ):
            missing_audio_count += 1
            if len(missing_ordinals) < 20:
                missing_ordinals.append(ordinal)
            continue

        effective_audio_count += 1
        if record[1] == run.id:
            run_audio_count += 1
        parent_take_id = record[7]
        shares_parent_artifact = (
            parent_take_id is not None
            and record[3] is not None
            and parent_artifacts.get(parent_take_id) == record[3]
        )
        if record[1] != run.id or shares_parent_artifact:
            inherited_audio_count += 1
        else:
            generated_audio_count += 1

        artifact_id, artifact_session_id, artifact_state, relative_path = (
            record[3],
            record[4],
            record[5],
            record[6],
        )
        if (
            artifact_id is None
            or artifact_session_id != run.session_id
            or artifact_state != "current"
            or not relative_path
        ):
            unavailable_artifact_count += 1
            continue
        try:
            artifact_path = paths.managed_path(relative_path)
        except (OSError, PathBoundaryError, RuntimeError, ValueError):
            unavailable_artifact_count += 1
            continue
        if not artifact_path.is_file():
            missing_file_count += 1

    export_ready = (
        included_count > 0
        and effective_audio_count == included_count
        and unavailable_artifact_count == 0
        and missing_file_count == 0
    )
    return {
        "run_id": run.id,
        "plan_revision_id": run.plan_revision_id,
        "audio_version_side": audio_version_side,
        "included_count": included_count,
        "effective_audio_count": effective_audio_count,
        "run_audio_count": run_audio_count,
        "inherited_audio_count": inherited_audio_count,
        "generated_audio_count": generated_audio_count,
        "missing_audio_count": missing_audio_count,
        "unavailable_artifact_count": unavailable_artifact_count,
        "missing_file_count": missing_file_count,
        "missing_segment_ordinals": missing_ordinals,
        "export_ready": export_ready,
    }


def get_generation_run_coverage(
    database: Database,
    run_id: str,
    *,
    audio_version_side: Literal["before", "after"] = "after",
) -> dict[str, Any]:
    """Report bounded availability for the audio selected by one frozen run."""

    if audio_version_side not in {"before", "after"}:
        raise ValueError("Audio version side must be before or after.")
    normalized_id = _valid_id(run_id, maximum=80)
    if normalized_id is None:
        raise KeyError(run_id)
    with database.snapshot_session() as session:
        run = session.scalar(_run_statement().where(GenerationRun.id == normalized_id))
        if run is None:
            raise KeyError(run_id)
        return _generation_run_coverage(
            session,
            database,
            run,
            audio_version_side=audio_version_side,
        )


def get_generation_operation(
    work: WorkService,
    operation_id: str,
    *,
    include_coverage: bool = True,
) -> dict[str, Any]:
    """Read one stable admission operation or a direct generation job."""

    normalized_id = _valid_id(operation_id)
    if normalized_id is None:
        raise KeyError(operation_id)

    database = work.queue.database
    with database.snapshot_session() as session:
        identity = session.execute(
            select(Job.id, Job.kind).where(Job.id == normalized_id)
        ).one_or_none()
        if identity is None or identity.kind not in {
            "generation.admit",
            "generation.run",
        }:
            raise KeyError(operation_id)
        root = session.scalar(_job_statement().where(Job.id == normalized_id))
        if root is None:
            raise KeyError(operation_id)

        root_view = work.project_job(root)
        run: GenerationRun | None = None
        child: Job | None = None
        child_view: WorkView | None = None
        linkage_diagnostic: str | None = None

        if root.kind == "generation.admit":
            run, child, linkage_diagnostic = _validated_admission_link(session, root)
            if child is not None:
                child_view = work.project_job(child)
                current_view = child_view
                phase = "complete" if child_view.state in TERMINAL_STATES else "synthesis"
                state = child_view.state
                linkage_pending = False
            else:
                current_view = root_view
                failed_without_child = root_view.state in {"failed", "cancelled"}
                successful_without_link = root_view.state == "succeeded"
                marker_present = isinstance(root.result_json, dict) and any(
                    key in root.result_json for key in ("generation_run_id", "job_id")
                )
                linkage_pending = not failed_without_child and (
                    successful_without_link or marker_present
                )
                if successful_without_link:
                    state = "waiting"
                    linkage_diagnostic = linkage_diagnostic or (
                        "Generation run linkage is not available yet."
                    )
                else:
                    state = root_view.state
                phase = "complete" if failed_without_child else "preparation"
        else:
            run, linkage_diagnostic = _validated_direct_link(session, root)
            current_view = root_view
            if run is not None:
                phase = "complete" if root_view.state in TERMINAL_STATES else "synthesis"
                state = root_view.state
                linkage_pending = False
            else:
                successful_without_link = root_view.state == "succeeded"
                state = "waiting" if successful_without_link else root_view.state
                phase = "complete" if state in TERMINAL_STATES else "synthesis"
                linkage_pending = root_view.state not in {"failed", "cancelled"}

        preparation_work_id = root.id if root.kind == "generation.admit" else None
        synthesis_work_id = (
            child.id if child is not None else (root.id if root.kind == "generation.run" else None)
        )
        cancellable = root_view.cancellable or bool(child_view and child_view.cancellable)
        poll_after_ms = current_view.poll_after_ms
        if state == "waiting" and current_view.state == "succeeded":
            poll_after_ms = 1_000

        coverage = None
        if include_coverage and run is not None and child_view is not None:
            if child_view.state in TERMINAL_STATES:
                coverage = _generation_run_coverage(
                    session,
                    database,
                    run,
                    audio_version_side="after",
                )
        elif include_coverage and run is not None and root.kind == "generation.run":
            if root_view.state in TERMINAL_STATES:
                coverage = _generation_run_coverage(
                    session,
                    database,
                    run,
                    audio_version_side="after",
                )

        return {
            "schema_version": 1,
            "id": root.id,
            "session_id": root.session_id,
            "phase": phase,
            "state": state,
            "cancellable": cancellable,
            "poll_after_ms": max(0, min(int(poll_after_ms), 60_000)),
            "preparation_work_id": preparation_work_id,
            "synthesis_work_id": synthesis_work_id,
            "generation_run_id": run.id if run is not None else None,
            "work": _compact_work(current_view),
            "preparation_error": (
                _error_projection(root_view)
                if root.kind == "generation.admit"
                and root_view.state == "failed"
                and child is not None
                else None
            ),
            "linkage_pending": linkage_pending,
            "linkage_diagnostic": linkage_diagnostic if linkage_pending else None,
            "coverage": coverage,
        }


__all__ = ["get_generation_operation", "get_generation_run_coverage"]
