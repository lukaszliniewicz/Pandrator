"""Generation interruption and resume ownership inside caller-owned transactions.

Scheduling follows durable run flags, separately from immutable output lineage.
This module depends on records and a narrow queue interface, never workspace
services or editorial audio propagation.
"""

from __future__ import annotations

from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import GenerationRun, Job, utcnow


class GenerationJobQueue(Protocol):
    def enqueue_in_session(
        self,
        session: Session,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        resource_keys: list[str] | None = None,
    ) -> Job: ...


def canonical_regeneration_root(
    session: Session,
    source_run: GenerationRun,
    session_id: str,
    plan_revision_id: str,
) -> GenerationRun:
    """Resolve a legacy regeneration chain to its full-generation root.

    New targeted runs are always rooted directly at the original full
    generation run.  Older databases can contain chains of regeneration
    children, so follow only regeneration links and fail closed when a
    malformed or cross-session/plan link is encountered.
    """
    current = source_run
    visited: set[str] = set()
    while current.operation == "regenerate":
        if current.id in visited:
            raise ValueError("The generation regeneration lineage contains a cycle.")
        visited.add(current.id)
        parent_id = str(current.source_generation_run_id or "")
        if not parent_id:
            # Regeneration on an edited plan can legitimately be an
            # independent output with no same-plan full-generation root.
            break
        parent = session.get(GenerationRun, parent_id)
        if (
            parent is None
            or parent.session_id != session_id
            or parent.plan_revision_id != plan_revision_id
            or parent.operation == "rvc"
        ):
            raise ValueError(
                "The selected source generation run does not match this session and plan."
            )
        current = parent
    if (
        current.session_id != session_id
        or current.plan_revision_id != plan_revision_id
        or current.operation == "rvc"
    ):
        raise ValueError("The selected source generation run does not match this session and plan.")
    return current


def clear_regeneration_baton(
    session: Session,
    child_run: GenerationRun,
    source_run_id: str,
) -> None:
    """Revoke one child's durable resume baton, including its job marker."""
    child_run.resume_source_on_completion = False
    child_run.updated_at = utcnow()
    child_job = session.get(Job, child_run.job_id) if child_run.job_id else None
    if child_job is None or not isinstance(child_job.payload_json, dict):
        return
    payload = dict(child_job.payload_json)
    if payload.get("auto_resume_source_generation_run_id") == source_run_id:
        payload.pop("auto_resume_source_generation_run_id", None)
        child_job.payload_json = payload
        child_job.updated_at = utcnow()


def regeneration_baton_descendants(
    session: Session,
    source_run_id: str,
) -> list[GenerationRun]:
    """Find baton-bearing regeneration descendants, including legacy chains.

    The durable flag, rather than the child's status, owns the baton.  A
    completed child commits its terminal status immediately before it
    consumes this flag in the resume transaction; filtering on status
    would leave a race in which a replacement request misses the baton.
    """
    pending = {source_run_id}
    visited: set[str] = set()
    batons: list[GenerationRun] = []
    while pending:
        parent_ids = pending - visited
        if not parent_ids:
            break
        visited.update(parent_ids)
        descendants = list(
            session.scalars(
                select(GenerationRun).where(
                    (
                        GenerationRun.source_generation_run_id.in_(parent_ids)
                        | GenerationRun.settings_snapshot_json["interrupted_generation_run_id"]
                        .as_string()
                        .in_(parent_ids)
                    ),
                    GenerationRun.operation == "regenerate",
                )
            ).all()
        )
        pending.update(child.id for child in descendants)
        batons.extend(child for child in descendants if child.resume_source_on_completion)
    return batons


def revoke_regeneration_batons(
    session: Session,
    source_run_id: str,
) -> list[GenerationRun]:
    """Revoke all existing batons below a root before assigning a replacement."""
    batons = regeneration_baton_descendants(session, source_run_id)
    for child in batons:
        clear_regeneration_baton(session, child, source_run_id)
    return batons


def generation_resource_keys(session_id: str, snapshot: dict[str, Any]) -> list[str]:
    tts = snapshot.get("tts", {})
    service = str(tts.get("service") or "tts").lower().replace(" ", "_")
    resource_keys = [f"session:{session_id}", f"service:tts:{service}"]
    compute = str(tts.get("compute_backend") or tts.get("device") or "auto").lower()
    if compute in {"cuda", "vulkan", "metal", "gpu"}:
        resource_keys.append(f"gpu:{compute}")
    return resource_keys


def interrupted_run_id(run: GenerationRun) -> str | None:
    """Scheduling ownership is distinct from the output's immutable revision."""
    return (run.settings_snapshot_json or {}).get(
        "interrupted_generation_run_id"
    ) or run.source_generation_run_id


def resume_segment_ids(session: Session, run: GenerationRun) -> list[str]:
    """Resume the original request, not every row of a targeted output plan."""

    snapshot = run.settings_snapshot_json or {}
    if "generation_request_segment_ids" in snapshot:
        return list(snapshot["generation_request_segment_ids"] or [])
    job = session.get(Job, run.job_id) if run.job_id else None
    return list((job.payload_json or {}).get("segment_ids") or []) if job else []


def release_interrupted_run(
    session: Session, jobs: GenerationJobQueue, child: GenerationRun
) -> str | None:
    """Release a temporary pause, even if a queued replacement is canceled.

    This is called inside a short write transaction. User pauses revoke the
    permission flag, so neither completion nor cancellation can undo them.
    """
    if not child.resume_source_on_completion:
        return None
    source_id = interrupted_run_id(child)
    source = session.get(GenerationRun, source_id) if source_id else None
    if source is None or source.session_id != child.session_id:
        return None

    clear_regeneration_baton(session, child, source.id)
    if source.cancel_requested or not source.pause_requested:
        return None
    # If the last queued replacement is canceled, an earlier replacement may
    # still be waiting. Transfer the resume responsibility instead of reviving
    # the full run ahead of it.
    sibling = session.scalar(
        select(GenerationRun)
        .where(
            GenerationRun.id != child.id,
            GenerationRun.session_id == source.session_id,
            GenerationRun.operation == "regenerate",
            GenerationRun.cancel_requested.is_(False),
            GenerationRun.status.in_(("queued", "running")),
            (GenerationRun.source_generation_run_id == source.id)
            | (
                GenerationRun.settings_snapshot_json["interrupted_generation_run_id"].as_string()
                == source.id
            ),
        )
        .order_by(GenerationRun.sequence_number.desc())
    )
    if sibling is not None and child.status in {"canceled", "cancelled"}:
        sibling.resume_source_on_completion = True
        sibling_job = session.get(Job, sibling.job_id) if sibling.job_id else None
        if sibling_job is not None:
            sibling_job.payload_json = {
                **(sibling_job.payload_json or {}),
                "auto_resume_source_generation_run_id": source.id,
            }
        return None
    if source.status == "pausing":
        source_job = session.get(Job, source.job_id) if source.job_id else None
        if source_job is not None and source_job.status in {"running", "queued"}:
            source.pause_requested = False
            source.status = source_job.status
            source.updated_at = utcnow()
        return None
    if source.status != "paused":
        return None
    return enqueue_generation_resume(session, jobs, source).id


def enqueue_generation_resume(
    session: Session,
    jobs: GenerationJobQueue,
    run: GenerationRun,
) -> Job:
    """Requeue the original selection and any still-owned parent resume permission."""
    payload: dict[str, Any] = {
        "generation_run_id": run.id,
        "segment_ids": resume_segment_ids(session, run),
        "operation": "resume",
    }
    source_id = interrupted_run_id(run)
    if run.resume_source_on_completion and source_id:
        payload["auto_resume_source_generation_run_id"] = source_id
    run.pause_requested = False
    run.cancel_requested = False
    run.status = "queued"
    run.updated_at = utcnow()
    job = jobs.enqueue_in_session(
        session,
        "generation.run",
        payload,
        session_id=run.session_id,
        resource_keys=generation_resource_keys(
            run.session_id, dict(run.settings_snapshot_json or {})
        ),
    )
    run.job_id = job.id
    return job


def interrupt_generation(
    session: Session,
    source: GenerationRun,
    operation: str,
) -> str | None:
    """Temporarily yield to a replacement without overriding an explicit pause."""
    if operation == "regenerate":
        if source.status in {"queued", "running"}:
            revoke_regeneration_batons(session, source.id)
            source.pause_requested = True
            source.status = "pausing"
            source.updated_at = utcnow()
            return source.id
        if source.status in {"pausing", "paused"} and regeneration_baton_descendants(
            session, source.id
        ):
            revoke_regeneration_batons(session, source.id)
            source.pause_requested = True
            source.updated_at = utcnow()
            return source.id
    elif source.status in {"queued", "running"}:
        source.pause_requested = True
        source.status = "pausing"
        source.updated_at = utcnow()
    return None
