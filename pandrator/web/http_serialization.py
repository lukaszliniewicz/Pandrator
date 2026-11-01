"""HTTP record projections shared across route domains."""

from collections.abc import Iterable
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .credentials import redact_inline_secrets
from .database import Database
from .model_base import as_utc
from .models import Job, SessionRecord
from .projects.copy_contracts import PROJECT_COPY_JOB_KIND, public_copy_job_result
from .speech_preview_settings import public_preview_payload
from .voice_provider_effects import voice_provider_effect_summary


def job_projects(
    database: Database, records: Iterable[Job], *, db_session: Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Resolve authoritative job-session labels in the caller's authorized snapshot."""
    items = list(records)
    if db_session is None:
        with database.session() as session:
            return job_projects(database, items, db_session=session)
    session_ids_by_job = {item.id: item.session_id for item in items}
    ids = {value for value in session_ids_by_job.values() if value}
    if not ids:
        return {}
    with db_session.no_autoflush:
        projects = {
            row.id: row
            for row in db_session.execute(select(
                SessionRecord.id, SessionRecord.name, SessionRecord.trashed_at,
                SessionRecord.status,
            ).where(SessionRecord.id.in_(ids)))
        }
    return {
        job_id: {
            "id": project_id,
            "name": projects[project_id].name if project_id in projects else None,
            "available": project_id in projects
            and projects[project_id].trashed_at is None
            and projects[project_id].status != "purging",
            "trashed": project_id in projects and projects[project_id].trashed_at is not None,
        }
        for job_id, project_id in session_ids_by_job.items()
        if project_id
    }


def model_payload(record, fields: tuple[str, ...]) -> dict[str, Any]:
    payload = {field: getattr(record, field) for field in fields}
    for key, value in list(payload.items()):
        if isinstance(value, datetime):
            payload[key] = as_utc(value).isoformat()
        elif hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


def job_payload(
    record,
    *,
    include_details: bool = True,
    waiting_for_job: dict[str, Any] | None = None,
    project: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = model_payload(
        record,
        (
            "id",
            "kind",
            "session_id",
            "workflow_run_id",
            "status",
            *(("payload_json", "result_json") if include_details and record.kind != PROJECT_COPY_JOB_KIND else ()),
            "progress",
            "progress_detail",
            "error_code",
            "error_message",
            "attempts",
            "max_attempts",
            "available_at",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        ),
    )
    payload["waiting_for_job"] = waiting_for_job
    if record.available_at is None:
        payload.pop("available_at", None)
    payload["project"] = project
    if include_details and record.kind == PROJECT_COPY_JOB_KIND:
        payload["payload_json"] = {}
        payload["result_json"] = public_copy_job_result(record.id) if record.status == "succeeded" else None
    if include_details and record.kind == "tts.preview":
        payload["payload_json"] = public_preview_payload(record.payload_json)
    run_id = (record.payload_json or {}).get("generation_run_id") if record.kind == "generation.run" else None
    payload["generation_run_id"] = (
        run_id if record.kind == "generation.run" and isinstance(run_id, str) else None
    )
    if include_details and record.kind in {"voice.publish", "voice.unpublish", "voice.google_design"}:
        effect = voice_provider_effect_summary(getattr(record, "external_effect_json", None))
        if effect is not None:
            payload["external_effect"] = effect
    return redact_inline_secrets(payload)
