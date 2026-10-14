"""HTTP record projections shared across route domains."""

from collections.abc import Iterable
from typing import Any

from sqlalchemy import select

from .credentials import redact_inline_secrets
from .database import Database
from .models import Job, SessionRecord


def job_projects(database: Database, records: Iterable[Job]) -> dict[str, dict[str, Any]]:
    """Resolve project labels in one read, including projects now in Trash."""
    items = list(records)
    project_ids = {
        item.id: item.session_id or (item.payload_json or {}).get("session_id")
        for item in items
    }
    ids = {value for value in project_ids.values() if isinstance(value, str) and value}
    if not ids:
        return {}
    with database.session() as session:
        projects = {
            row.id: row
            for row in session.execute(select(
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
        for job_id, project_id in project_ids.items()
        if isinstance(project_id, str) and project_id
    }


def model_payload(record, fields: tuple[str, ...]) -> dict[str, Any]:
    payload = {field: getattr(record, field) for field in fields}
    for key, value in list(payload.items()):
        if hasattr(value, "isoformat"):
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
            *(("payload_json", "result_json") if include_details else ()),
            "progress",
            "progress_detail",
            "error_code",
            "error_message",
            "attempts",
            "max_attempts",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
        ),
    )
    payload["waiting_for_job"] = waiting_for_job
    payload["project"] = project
    run_id = (record.payload_json or {}).get("generation_run_id")
    payload["generation_run_id"] = (
        run_id if record.kind == "generation.run" and isinstance(run_id, str) else None
    )
    return redact_inline_secrets(payload)
