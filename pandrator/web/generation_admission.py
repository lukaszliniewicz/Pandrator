"""Durable, revision-pinned admission of a generation run."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from typing import Any, Callable

from sqlalchemy import or_, select

from .database import Database
from .generation_start_preparation import snapshot_guard
from .job_execution import current_job_lease
from .jobs import JobQueue
from .models import (
    AppSetting,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    Job,
    SessionRecord,
    SessionSetting,
    utcnow,
)
from .schemas import GenerationStartRequest
from .settings_policy import stable_hash
from .workspace import GenerationService, _settings_source_mode
from .workspace_settings import WorkspaceSettingsService

STALE_INPUTS_MESSAGE = (
    "Generation inputs changed after admission. Refresh the preview before generating."
)


class GenerationAdmissionStaleInputs(ValueError):
    """The admission's database snapshot no longer matches its inputs."""


def _aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _settings_revision_guard(session, session_id: str) -> str:
    """Fingerprint settings provenance without reading values or credentials."""
    global_revisions = session.execute(
        select(AppSetting.key, AppSetting.revision).where(
            or_(AppSetting.key.like("defaults.%"), AppSetting.key == "services.tts")
        )
    ).all()
    session_revisions = session.execute(
        select(SessionSetting.section, SessionSetting.revision).where(
            SessionSetting.session_id == session_id
        )
    ).all()
    return stable_hash(
        {
            "global": sorted((str(key), int(revision)) for key, revision in global_revisions),
            "session": sorted((str(section), int(revision)) for section, revision in session_revisions),
        }
    )


def _same_snapshot(
    session,
    session_id: str,
    revision_id: str,
    active_revision_id: str,
    guard: str,
    settings_revision_guard: str,
) -> bool:
    plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
    return bool(
        plan
        and plan.active_revision_id == active_revision_id
        and snapshot_guard(session, session_id, revision_id) == guard
        and _settings_revision_guard(session, session_id) == settings_revision_guard
    )


def admit_generation_in_session(
    session,
    jobs: JobQueue,
    generation: GenerationService,
    session_id: str,
    request_fields: dict[str, Any],
    *,
    request_id: str,
    traceparent: str | None,
) -> dict[str, Any]:
    """Capture the active database snapshot and enqueue in the caller's transaction."""
    owner = session.get(SessionRecord, session_id)
    if owner is None:
        raise KeyError(session_id)
    plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == session_id))
    active_revision_id = plan.active_revision_id if plan else None
    revision = session.get(GenerationPlanRevision, active_revision_id) if active_revision_id else None
    if revision is None or revision.plan_id != plan.id:
        raise ValueError("Create generation segments before starting audio generation.")
    assert active_revision_id is not None
    explicit_segment_ids = list(
        dict.fromkeys(str(value) for value in request_fields.get("segment_ids", []) if str(value))
    )
    requested_revision_id = request_fields.get("speech_plan_revision_id")
    settings_source_mode = _settings_source_mode(
        request_fields.get("settings_source"),
        request_fields.get("settings_source_run_id"),
    )
    source_preparation = {
        "requested_segment_ids": explicit_segment_ids,
        "explicit_segment_ids": explicit_segment_ids,
        "generation_run_id": request_fields.get("generation_run_id"),
        "operation": request_fields.get("operation", "generate"),
        "settings_source_selector": request_fields.get("settings_source"),
        "settings_source_mode": settings_source_mode,
        "settings_source_run_id": request_fields.get("settings_source_run_id"),
        "speech_plan_revision_id": requested_revision_id or active_revision_id,
        "explicit_speech_plan_revision_id": requested_revision_id,
        "stale_only": request_fields.get("stale_only", False),
        "missing_only": request_fields.get("missing_only", False),
    }
    revision_id, source_run = generation._start_source(
        session,
        session_id,
        source_preparation,
    )
    settings_source_identity = source_preparation.get("settings_source_identity")
    pinned_fields = dict(request_fields)
    if not requested_revision_id and not explicit_segment_ids:
        pinned_fields["speech_plan_revision_id"] = revision_id
    guard = snapshot_guard(session, session_id, revision_id)
    settings_revision_guard = _settings_revision_guard(session, session_id)
    job = jobs.enqueue_in_session(
        session,
        "generation.admit",
        {
            "session_id": session_id,
            "request": pinned_fields,
            "speech_plan_revision_id": revision_id,
            "active_revision_id_at_admission": active_revision_id,
            "snapshot_guard": guard,
            "settings_revision_guard": settings_revision_guard,
            "settings_source_identity": settings_source_identity,
            "request_id": request_id,
            "traceparent": traceparent,
        },
        session_id=session_id,
        max_attempts=2,
        resource_keys=[f"session:{session_id}"],
    )
    job.payload_json = {**job.payload_json, "admission_id": job.id}
    session.flush()
    return {
        "id": job.id,
        "job_id": job.id,
        "session_id": session_id,
        "status": "queued",
        "stage": "preparing",
        "speech_plan_revision_id": revision_id,
        "settings_source_selector": request_fields.get("settings_source"),
        "settings_source_run_id": (
            settings_source_identity.get("id")
            if isinstance(settings_source_identity, dict)
            else None
        ),
        "settings_source_plan_revision_id": (
            settings_source_identity.get("plan_revision_id")
            if isinstance(settings_source_identity, dict)
            else None
        ),
        "settings_source_settings_hash": (
            settings_source_identity.get("settings_hash")
            if isinstance(settings_source_identity, dict)
            else None
        ),
        "generation_run_id": request_fields.get("generation_run_id"),
        "source_generation_run_id": source_run.id if source_run else None,
        "request_id": request_id,
        "cancellable": True,
        "poll_after_ms": 1000,
    }


def run_generation_admission(
    database: Database,
    jobs: JobQueue,
    artifacts: Any,
    payload: dict[str, Any],
    progress: Callable[[float, str | None], None],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Prepare outside the writer lock, then fence and commit the child exactly once."""
    lease = current_job_lease.get()
    admission_id = str(payload.get("admission_id") or "")
    session_id = str(payload.get("session_id") or "")
    revision_id = str(payload.get("speech_plan_revision_id") or "")
    active_revision_id = str(payload.get("active_revision_id_at_admission") or "")
    guard = str(payload.get("snapshot_guard") or "")
    settings_revision_guard = str(payload.get("settings_revision_guard") or "")
    if not lease or lease["job_id"] != admission_id:
        raise RuntimeError("Generation admission requires its current worker lease.")

    def check_job(session) -> Job:
        job = session.get(Job, admission_id, populate_existing=True)
        if (
            job is None
            or job.kind != "generation.admit"
            or job.session_id != session_id
            or job.payload_json.get("admission_id") != admission_id
            or job.lease_owner != lease["owner_id"]
            or job.lease_generation != lease["lease_generation"]
            or job.status != "running"
            or job.lease_expires_at is None
            or _aware(job.lease_expires_at) <= utcnow()
            or cancel_event.is_set()
        ):
            raise RuntimeError("Generation admission lease or cancellation changed.")
        return job

    def committed_result(session, job: Job) -> dict[str, Any] | None:
        if job.kind != "generation.admit" or job.session_id != session_id:
            raise RuntimeError("Generation admission result marker has the wrong owner.")
        result = job.result_json
        if not isinstance(result, dict) or not result.get("generation_run_id"):
            return None
        run = session.get(GenerationRun, str(result["generation_run_id"]))
        child = session.get(Job, str(result.get("job_id") or ""))
        if (
            run is None
            or run.session_id != session_id
            or run.plan_revision_id != revision_id
            or run.job_id != result.get("job_id")
            or result.get("session_id") != session_id
            or child is None
            or child.kind != "generation.run"
            or child.session_id != session_id
            or child.payload_json.get("generation_run_id") != run.id
        ):
            raise RuntimeError("Generation admission result marker does not match its child run.")
        return dict(result)

    def stale_result(job: Job) -> None:
        job.result_json = {
            "code": "generation_admission_stale_inputs",
            "details": {"speech_plan_revision_id": revision_id},
        }

    marker: dict[str, Any] | None = None
    with database.immediate_session() as session:
        admission = session.get(Job, admission_id)
        if admission is None:
            raise KeyError(admission_id)
        if result := committed_result(session, admission):
            return result
        check_job(session)
        stale = not _same_snapshot(
            session, session_id, revision_id, active_revision_id, guard, settings_revision_guard
        )
        if stale:
            stale_result(admission)
    if stale:
        raise GenerationAdmissionStaleInputs(STALE_INPUTS_MESSAGE)

    request_fields = payload.get("request")
    if not isinstance(request_fields, dict):
        raise ValueError("Invalid generation admission request.")
    request = GenerationStartRequest.model_validate(request_fields)
    if request.speech_plan_revision_id and request.speech_plan_revision_id != revision_id:
        raise ValueError("Generation admission revision does not match its request.")
    progress(0.05, "Preparing generation")
    generation = GenerationService(
        database, jobs, WorkspaceSettingsService(database), artifacts=artifacts
    )
    prepared = generation.prepare_start(
        session_id,
        **request.model_dump(),
        resolved_settings_source_identity=payload.get("settings_source_identity"),
    )
    frozen_snapshot = prepared.get("frozen_snapshot")
    prepared_revision_id = (
        str(frozen_snapshot.get("speech_plan_revision_id") or "")
        if isinstance(frozen_snapshot, dict)
        else ""
    )

    with database.immediate_session() as session:
        admission = check_job(session)
        if result := committed_result(session, admission):
            return result
        stale = (
            prepared_revision_id != revision_id
            or not _same_snapshot(
                session, session_id, revision_id, active_revision_id, guard,
                settings_revision_guard,
            )
        )
        if stale:
            stale_result(admission)
        else:
            result = generation.start_in_session(
                session, session_id, prepared=prepared, include_history=False
            )
            marker = {
                "generation_run_id": str(result["id"]),
                "job_id": str(result["job_id"]),
                "session_id": session_id,
                "status": "queued",
                "stage": "generating",
                "speech_plan_revision_id": revision_id,
                "settings_source_selector": result.get("settings_source_receipt", {}).get(
                    "selector"
                ),
                "settings_source_run_id": result.get("settings_source_receipt", {}).get(
                    "settings_source_run_id"
                ),
                "settings_source_plan_revision_id": result.get(
                    "settings_source_receipt", {}
                ).get("settings_source_plan_revision_id"),
                "settings_source_settings_hash": result.get(
                    "settings_source_receipt", {}
                ).get("settings_source_settings_hash"),
                "settings_hash": result.get("settings_hash"),
                "source_generation_run_id": result.get("source_generation_run_id"),
                "settings_source_receipt": result.get("settings_source_receipt"),
            }
            preflight = prepared.get("provider_preflight")
            if isinstance(preflight, dict):
                encoded = json.dumps(preflight, ensure_ascii=False, sort_keys=True)
                if len(encoded.encode("utf-8")) <= 2048:
                    marker["provider_preflight"] = preflight
            admission.result_json = marker
            session.flush()
    if stale:
        raise GenerationAdmissionStaleInputs(STALE_INPUTS_MESSAGE)
    assert marker is not None
    progress(1.0, "Generation queued")
    return marker
