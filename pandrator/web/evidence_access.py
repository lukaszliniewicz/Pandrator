"""Current project rights for evidence reads, admission and editorial decisions."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from datetime import UTC
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, load_only

from .access.artifact_policy import ArtifactNotFound, require_artifact_access
from .access.artifact_visibility import resource_visibility_clause
from .access.contracts import AccessContext, AccessDenied, ProjectAccess, ProjectAction
from .access.session_policy import require_session_access
from .access.workspace_policy import require_workspace_access
from .accounts.execution_actor import UserActor
from .models import (
    Artifact,
    Document,
    DocumentRevision,
    Job,
    Segment,
    SessionRecord,
    SubtitleEvidence,
)
from .subtitle_evidence import SubtitleEvidenceService
from .subtitle_media import resolve_subtitle_media


class SubtitleEvidenceNotFound(KeyError):
    def __init__(self):
        super().__init__("Subtitle evidence not found.")


class EvidenceRevisionRequired(ValueError):
    pass


class EvidenceRevisionConflict(ValueError):
    pass


def _context(context: AccessContext) -> None:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(context.actor) is not UserActor:
        raise AccessDenied()


def require_evidence_session(
    session: Session,
    context: AccessContext,
    session_id: str,
    action: ProjectAction,
) -> ProjectAccess:
    _context(context)
    access = require_session_access(session, context, session_id, action)
    if action is not ProjectAction.READ:
        active = session.scalar(
            select(SessionRecord.id).where(
                SessionRecord.id == session_id,
                SessionRecord.trashed_at.is_(None),
                SessionRecord.status != "purging",
            )
        )
        if active is None:
            raise SubtitleEvidenceNotFound()
    return access


def require_evidence_access(
    session: Session,
    context: AccessContext,
    evidence_id: str,
    action: ProjectAction = ProjectAction.READ,
    *,
    expected_session_id: str | None = None,
) -> tuple[SubtitleEvidence, ProjectAccess]:
    _context(context)
    with session.no_autoflush:
        session_id = session.scalar(
            select(SubtitleEvidence.session_id).where(
                SubtitleEvidence.id == evidence_id,
            )
        )
        if (
            session_id is None
            or expected_session_id is not None
            and session_id != expected_session_id
        ):
            raise SubtitleEvidenceNotFound()
        access = require_evidence_session(session, context, session_id, action)
        record = session.get(SubtitleEvidence, evidence_id, populate_existing=True)
        if record is None:
            raise SubtitleEvidenceNotFound()
        if record.job_id is not None:
            job_id = session.scalar(
                select(Job.id).where(
                    Job.id == record.job_id,
                    Job.kind == "subtitle.evidence",
                    Job.workspace_id == access.workspace_id,
                    Job.project_id == access.project_id,
                    Job.session_id == session_id,
                    Job.workflow_run_id.is_(None),
                )
            )
            if job_id is None:
                raise SubtitleEvidenceNotFound()
        return record, access


def evidence_revision(record: SubtitleEvidence) -> str:
    updated = record.updated_at
    if updated.tzinfo is None:
        updated = updated.replace(tzinfo=UTC)
    value = {
        "id": record.id,
        "status": record.status,
        "resolution": record.resolution_json,
        "updated_at": updated.astimezone(UTC).isoformat(),
    }
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def require_evidence_revision(
    record: SubtitleEvidence, expected: str | None, *, required: bool
) -> None:
    if expected is None:
        if required:
            raise EvidenceRevisionRequired("Refresh the evidence before recording a decision.")
        return
    if not hmac.compare_digest(expected, evidence_revision(record)):
        raise EvidenceRevisionConflict("This evidence decision changed. Refresh it before saving.")


_REFERENCE_ERROR = "Saved audio evidence references are unavailable."


def _id_values(ids: Iterable[str]):
    """Use one bound JSON value rather than a placeholder for each stored ID."""
    values = func.json_each(json.dumps(list(ids), separators=(",", ":"))).table_valued("value")
    return select(values.c.value)


def _saved_evidence_ids(
    session: Session,
    context: AccessContext,
    access: ProjectAccess,
    session_id: str,
    artifact_id: str,
) -> set[str]:
    pinned = session.execute(
        select(Artifact.metadata_json, DocumentRevision.id)
        .join(
            DocumentRevision,
            DocumentRevision.id == Artifact.metadata_json["revision_id"].as_string(),
        )
        .join(Document, Document.id == DocumentRevision.document_id)
        .where(
            Artifact.id == artifact_id,
            Artifact.session_id == session_id,
            Artifact.project_id == access.project_id,
            Artifact.workspace_id == access.workspace_id,
            Artifact.state != "deleted",
            Document.session_id == session_id,
            resource_visibility_clause(Artifact, context.actor),
        )
    ).one_or_none()
    if pinned is None or type(pinned.metadata_json) is not dict:
        raise ArtifactNotFound()
    references: set[str] = set()

    def collect(value: Any) -> None:
        if value is None:
            return
        if type(value) is not list:
            raise ValueError(_REFERENCE_ERROR)
        for identifier in value:
            if type(identifier) is not str or not identifier or len(identifier) > 120:
                raise ValueError(_REFERENCE_ERROR)
            references.add(identifier)
            if len(references) > 1000:
                raise ValueError(_REFERENCE_ERROR)

    packet = pinned.metadata_json.get("logical_passages")
    if packet is not None:
        if type(packet) is not dict:
            raise ValueError(_REFERENCE_ERROR)
        items = packet.get("items")
        if items is not None:
            if type(items) is not list:
                raise ValueError(_REFERENCE_ERROR)
            for item in items:
                if type(item) is not dict:
                    raise ValueError(_REFERENCE_ERROR)
                collect(item.get("evidence_ids"))
    for value in session.scalars(
        select(Segment.metadata_json["evidence_ids"]).where(Segment.revision_id == pinned.id)
    ):
        collect(value)
    return references


def _jobs(
    session: Session, access: ProjectAccess, session_id: str, ids: list[str]
) -> dict[str, Job]:
    if not ids:
        return {}
    return {
        job.id: job
        for job in session.scalars(
            select(Job)
            .options(
                load_only(
                    Job.id,
                    Job.kind,
                    Job.session_id,
                    Job.status,
                    Job.progress,
                    Job.progress_detail,
                    Job.error_code,
                    Job.error_message,
                    Job.created_at,
                    Job.started_at,
                    Job.finished_at,
                    Job.updated_at,
                )
            )
            .where(
                Job.id.in_(_id_values(ids)),
                Job.kind == "subtitle.evidence",
                Job.session_id == session_id,
                Job.project_id == access.project_id,
                Job.workspace_id == access.workspace_id,
                Job.workflow_run_id.is_(None),
            )
        )
    }


def project_evidence(record: SubtitleEvidence, job: Job | None) -> dict[str, Any]:
    result = SubtitleEvidenceService._projection(record, job)
    result["record"]["revision"] = evidence_revision(record)
    return result


def get_evidence_in_session(
    session: Session,
    context: AccessContext,
    evidence_id: str,
) -> dict[str, Any]:
    with session.no_autoflush:
        record, access = require_evidence_access(session, context, evidence_id)
        jobs = _jobs(session, access, record.session_id, [record.job_id] if record.job_id else [])
        return project_evidence(record, jobs.get(record.job_id) if record.job_id else None)


def list_evidence_in_session(
    session: Session,
    context: AccessContext,
    session_id: str,
    source_artifact_id: str | None = None,
    *,
    include_referenced: bool = False,
) -> dict[str, Any]:
    with session.no_autoflush:
        access = require_evidence_session(session, context, session_id, ProjectAction.READ)
        references: set[str] = set()
        if include_referenced:
            if not source_artifact_id:
                raise ValueError(_REFERENCE_ERROR)
            references = _saved_evidence_ids(
                session, context, access, session_id, source_artifact_id
            )
        statement = select(SubtitleEvidence).where(SubtitleEvidence.session_id == session_id)
        if references:
            statement = statement.where(
                or_(
                    SubtitleEvidence.source_artifact_id == source_artifact_id,
                    SubtitleEvidence.id.in_(_id_values(sorted(references))),
                )
            )
        elif source_artifact_id:
            statement = statement.where(SubtitleEvidence.source_artifact_id == source_artifact_id)
        records = list(
            session.scalars(
                statement.order_by(SubtitleEvidence.created_at.desc(), SubtitleEvidence.id.desc())
            )
        )
        if not references.issubset({record.id for record in records}):
            raise ValueError(_REFERENCE_ERROR)
        jobs = _jobs(
            session, access, session_id, [record.job_id for record in records if record.job_id]
        )
        return {
            "session_id": session_id,
            "items": [
                project_evidence(record, jobs.get(record.job_id) if record.job_id else None)
                for record in records
            ],
        }


def authorize_evidence_request(
    session: Session,
    context: AccessContext,
    session_id: str,
    values: Mapping[str, Any],
) -> None:
    """Freshly authorize the actual request and its dependencies before a receipt."""
    with session.no_autoflush:
        access = require_evidence_session(session, context, session_id, ProjectAction.RUN)
        source_id = values.get("source_artifact_id")
        if type(source_id) is not str:
            raise ArtifactNotFound()
        source = require_artifact_access(session, context, source_id)
        media = resolve_subtitle_media(session, session_id, source)
        if (
            session.scalar(
                select(Artifact.id).where(
                    Artifact.id == media.id,
                    resource_visibility_clause(Artifact, context.actor),
                )
            )
            is None
        ):
            raise ArtifactNotFound()
        if values.get("audio_model_ids"):
            require_workspace_access(session, context, access.workspace_id)


__all__ = [
    "EvidenceRevisionConflict",
    "EvidenceRevisionRequired",
    "SubtitleEvidenceNotFound",
    "authorize_evidence_request",
    "evidence_revision",
    "get_evidence_in_session",
    "list_evidence_in_session",
    "project_evidence",
    "require_evidence_access",
    "require_evidence_revision",
    "require_evidence_session",
]
