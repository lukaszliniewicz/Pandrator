"""Bounded metadata projections for navigating pinned language projects."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, case, exists, func, or_, select
from sqlalchemy.orm import Session

from pandrator.runtime import DataPaths

from . import models as m
from .multilingual_setup import read_setup
from .translation_projects import TranslationProjectConflict, get_session_project


def _iso(value: Any) -> str | None:
    return value.isoformat() if value is not None else None


def _latest_by_session(session: Session, statement: Any, model: Any) -> dict[str, str]:
    """Return one scalar ID per session, without hydrating historical entities."""
    ranked = statement.add_columns(
        func.row_number()
        .over(
            partition_by=model.session_id,
            order_by=(model.created_at.desc(), model.id.desc()),
        )
        .label("rank")
    ).subquery()
    rows = session.execute(select(ranked.c.session_id, ranked.c.id).where(ranked.c.rank == 1))
    return {row[0]: row[1] for row in rows}


def _artifact_metadata(session: Session, artifact_id: str | None) -> Mapping[str, Any] | None:
    if artifact_id is None:
        return None
    revision_id = m.Artifact.metadata_json["revision_id"].as_string()
    row = (
        session.execute(
            select(
                m.Artifact.id.label("artifact_id"),
                m.Artifact.session_id,
                m.Artifact.role,
                m.Artifact.state,
                m.Artifact.content_hash,
                m.Artifact.created_at,
                revision_id.label("revision_id"),
                m.Artifact.metadata_json["language"].as_string().label("language"),
                m.DocumentRevision.created_at.label("revision_created_at"),
                m.Document.session_id.label("document_session_id"),
                m.Document.stage.label("document_stage"),
                m.Document.active_revision_id,
            )
            .outerjoin(
                m.DocumentRevision,
                m.DocumentRevision.id == revision_id,
            )
            .outerjoin(
                m.Document,
                m.Document.id == m.DocumentRevision.document_id,
            )
            .where(m.Artifact.id == artifact_id)
        )
        .mappings()
        .first()
    )
    return dict(row) if row is not None else None


def _identity(artifact: Mapping[str, Any] | None) -> dict[str, Any]:
    return {
        "artifact_id": artifact["artifact_id"] if artifact else None,
        "revision_id": artifact["revision_id"] if artifact else None,
        "content_hash": artifact["content_hash"] if artifact else None,
        "created_at": _iso(artifact["created_at"]) if artifact else None,
        "revision_created_at": _iso(artifact["revision_created_at"]) if artifact else None,
        "state": artifact["state"] if artifact else None,
    }


def _source_status(session: Session, project: m.TranslationProject) -> tuple[dict, dict]:
    pinned = _artifact_metadata(session, project.checkpoint_artifact_id)
    role = pinned["role"] if pinned else None
    # Mirror source checkpoint selection using scalar SQL: an explicit selection
    # wins within its stage; otherwise require an active document revision when
    # that stage has documents. Correction takes precedence over transcription.
    stage = case((m.Artifact.role == "correction", "correct"), else_="transcribe")
    stage_document = select(m.Document.id).where(
        m.Document.session_id == project.source_session_id,
        m.Document.stage == m.Artifact.role,
    )
    current_id = session.scalar(
        select(m.Artifact.id)
        .outerjoin(
            m.SessionStageSelection,
            and_(
                m.SessionStageSelection.session_id == m.Artifact.session_id,
                m.SessionStageSelection.stage_key == stage,
            ),
        )
        .where(
            m.Artifact.session_id == project.source_session_id,
            m.Artifact.role.in_(("correction", "transcription")),
            m.Artifact.state == "current",
            or_(
                and_(
                    m.SessionStageSelection.stage_key.is_not(None),
                    m.Artifact.id == m.SessionStageSelection.artifact_id,
                ),
                and_(
                    m.SessionStageSelection.stage_key.is_(None),
                    or_(
                        ~exists(stage_document),
                        exists(
                            stage_document.where(
                                m.Document.active_revision_id
                                == m.Artifact.metadata_json["revision_id"].as_string(),
                            )
                        ),
                    ),
                ),
            ),
        )
        .order_by(
            case((m.Artifact.role == "correction", 0), else_=1),
            m.Artifact.created_at.desc(),
            m.Artifact.id.desc(),
        )
        .limit(1)
    )
    current = (
        pinned
        if current_id == project.checkpoint_artifact_id
        else _artifact_metadata(
            session,
            current_id,
        )
    )
    source = (
        session.execute(
            select(
                m.SessionRecord.name,
                m.SessionRecord.source_language,
                m.SessionRecord.trashed_at,
            ).where(m.SessionRecord.id == project.source_session_id)
        )
        .mappings()
        .first()
    )
    edit = (
        session.execute(
            select(
                m.MediaEditPlan.id.label("plan_id"),
                m.MediaEditPlan.active_revision_id,
                m.MediaEditPlanRevision.id.label("revision_id"),
                m.MediaEditPlanRevision.plan_id.label("revision_plan_id"),
                m.MediaEditPlanRevision.content_hash,
                m.MediaEditPlanRevision.created_at,
            )
            .outerjoin(
                m.MediaEditPlanRevision,
                m.MediaEditPlanRevision.id == m.MediaEditPlan.active_revision_id,
            )
            .where(m.MediaEditPlan.session_id == project.source_session_id)
        )
        .mappings()
        .first()
    )
    reasons: list[str] = []
    if source is None or source["trashed_at"] is not None:
        reasons.append("The source session is unavailable.")
    if pinned is None or pinned["session_id"] != project.source_session_id:
        reasons.append("The pinned source checkpoint is unavailable.")
    else:
        if pinned["state"] != "current" or current_id != project.checkpoint_artifact_id:
            reasons.append("The pinned source checkpoint is no longer current.")
        if pinned["content_hash"] != project.source_content_hash:
            reasons.append("The pinned source checkpoint hash changed.")
        if pinned["revision_id"] and (
            pinned["document_session_id"] != project.source_session_id
            or pinned["document_stage"] != role
            or pinned["active_revision_id"] != pinned["revision_id"]
        ):
            reasons.append("The source document revision changed.")
        language = pinned["language"]
        if not language or language.strip().lower() == "auto":
            language = source["source_language"] if source else None
        if language and language.strip().replace("_", "-").lower() != project.source_language:
            reasons.append("The source checkpoint language changed.")
    live_edit = (edit["revision_id"], edit["content_hash"]) if edit else (None, None)
    if edit and (not edit["revision_id"] or edit["revision_plan_id"] != edit["plan_id"]):
        reasons.append("The source media edit revision is unavailable.")
    if live_edit != (project.source_media_edit_revision_id, project.source_media_edit_content_hash):
        reasons.append("The source media edit plan changed since project creation.")
    status = {
        "checkpoint_role": role,
        "pinned_checkpoint": {
            **_identity(pinned),
            "role": role,
            "content_hash": project.source_content_hash,
        },
        "current_source": _identity(current),
        "current_checkpoint": {**_identity(current), "role": current["role"] if current else None},
        "current_checkpoint_revision_id": current["active_revision_id"] if current else None,
        "current_correction": _identity(current)
        if current and current["role"] == "correction"
        else _identity(None),
        "current_correction_revision_id": (
            current["active_revision_id"] if current and current["role"] == "correction" else None
        ),
        "pinned_media_edit": {
            "revision_id": project.source_media_edit_revision_id,
            "content_hash": project.source_media_edit_content_hash,
        },
        "current_media_edit": {
            "revision_id": edit["revision_id"] if edit else None,
            "content_hash": edit["content_hash"] if edit else None,
            "created_at": _iso(edit["created_at"]) if edit else None,
        },
        "source_changed": bool(reasons),
        "reasons": reasons,
        "validation_scope": "metadata",
    }
    details = {
        "source_session_name": source["name"] if source else None,
        "checkpoint_role": role,
        "checkpoint_revision_id": pinned["revision_id"] if pinned else None,
        "checkpoint_created_at": _iso(pinned["created_at"]) if pinned else None,
        "checkpoint_revision_created_at": _iso(pinned["revision_created_at"]) if pinned else None,
    }
    return status, details


def compact_project_payload(session: Session, project: m.TranslationProject) -> dict[str, Any]:
    branch_rows = (
        session.execute(
            select(
                m.TranslationProjectBranch.id,
                m.TranslationProjectBranch.session_id,
                m.TranslationProjectBranch.target_language,
                m.TranslationProjectBranch.source_checkpoint_artifact_id,
                m.Artifact.role.label("source_checkpoint_role"),
                m.Artifact.metadata_json["revision_id"]
                .as_string()
                .label("source_checkpoint_revision_id"),
                m.TranslationProjectBranch.source_content_hash,
                m.TranslationProjectBranch.created_at,
                m.SessionRecord.id.label("record_id"),
                m.SessionRecord.name,
                m.SessionRecord.workflow_kind,
                m.SessionRecord.status,
                m.SessionRecord.trashed_at,
            )
            .outerjoin(
                m.SessionRecord,
                m.SessionRecord.id == m.TranslationProjectBranch.session_id,
            )
            .outerjoin(
                m.Artifact,
                m.Artifact.id == m.TranslationProjectBranch.source_checkpoint_artifact_id,
            )
            .where(m.TranslationProjectBranch.project_id == project.id)
            .order_by(
                m.TranslationProjectBranch.created_at,
                m.TranslationProjectBranch.id,
            )
            .limit(101)
        )
        .mappings()
        .all()
    )
    if len(branch_rows) > 100:
        raise TranslationProjectConflict("This project exceeds the supported branch limit.")
    if any(row["record_id"] is None for row in branch_rows):
        raise TranslationProjectConflict("A language session is missing.")
    ids = [row["session_id"] for row in branch_rows]
    translations = select(m.Artifact.session_id, m.Artifact.id).where(
        m.Artifact.session_id.in_(ids),
        m.Artifact.role == "translation",
    )
    current = (
        _latest_by_session(session, translations.where(m.Artifact.state == "current"), m.Artifact)
        if ids
        else {}
    )
    historical = _latest_by_session(session, translations, m.Artifact) if ids else {}
    dispatches = (
        _latest_by_session(
            session,
            select(m.DispatchRun.session_id, m.DispatchRun.id).where(
                m.DispatchRun.session_id.in_(ids),
                m.DispatchRun.kind == "translation",
                m.DispatchRun.status.in_(("ready", "running", "finalizing")),
            ),
            m.DispatchRun,
        )
        if ids
        else {}
    )
    active_jobs = (
        set(
            session.scalars(
                select(m.Job.session_id)
                .where(
                    m.Job.session_id.in_(ids),
                    m.Job.kind == "dubbing.translate",
                    m.Job.status.in_(("queued", "running", "retrying")),
                )
                .distinct()
            )
        )
        if ids
        else set()
    )
    branches = []
    for row in branch_rows:
        sid = row["session_id"]
        branches.append(
            {
                "id": row["id"],
                "session_id": sid,
                "name": row["name"],
                "workflow_kind": row["workflow_kind"],
                "target_language": row["target_language"],
                "source_checkpoint_artifact_id": row["source_checkpoint_artifact_id"],
                "source_checkpoint_role": row["source_checkpoint_role"],
                "source_checkpoint_revision_id": row["source_checkpoint_revision_id"],
                "source_content_hash": row["source_content_hash"],
                "status": row["status"],
                "trashed_at": _iso(row["trashed_at"]),
                "translation_artifact_id": current.get(sid),
                "translation_status": (
                    "running"
                    if sid in dispatches or sid in active_jobs
                    else "completed"
                    if sid in current
                    else "stale"
                    if sid in historical
                    else "ready"
                ),
                "active_translation_run_id": dispatches.get(sid),
                "created_at": _iso(row["created_at"]),
            }
        )
    source_status, details = _source_status(session, project)
    return {
        "project": {
            "id": project.id,
            "name": project.name,
            "source_session_id": project.source_session_id,
            "source_language": project.source_language,
            "checkpoint_artifact_id": project.checkpoint_artifact_id,
            "source_checkpoint_artifact_id": project.checkpoint_artifact_id,
            "source_content_hash": project.source_content_hash,
            "source_media_edit_revision_id": project.source_media_edit_revision_id,
            "source_media_edit_content_hash": project.source_media_edit_content_hash,
            "revision": project.revision,
            "created_at": _iso(project.created_at),
            "branches": branches,
            "source_status": source_status,
            **details,
        }
    }


def get_compact_project(session: Session, project_id: str) -> dict[str, Any]:
    project = session.get(m.TranslationProject, project_id)
    if project is None:
        raise KeyError(project_id)
    return compact_project_payload(session, project)


def get_compact_session_project(
    session: Session,
    session_id: str,
    *,
    paths: DataPaths,
) -> dict[str, Any]:
    if session.scalar(select(m.SessionRecord.id).where(m.SessionRecord.id == session_id)) is None:
        raise KeyError(session_id)
    project = session.scalar(
        select(m.TranslationProject).where(
            m.TranslationProject.source_session_id == session_id,
        )
    )
    branch_member = False
    if project is None:
        project = session.scalar(
            select(m.TranslationProject)
            .join(
                m.TranslationProjectBranch,
                m.TranslationProjectBranch.project_id == m.TranslationProject.id,
            )
            .where(m.TranslationProjectBranch.session_id == session_id)
        )
        branch_member = project is not None
    if project is None:
        # Preserve deferred setup's authoritative readiness validation. No branch
        # histories exist here; only this pre-project path can inspect source files.
        return get_session_project(session, session_id, paths=paths)
    setup = None if branch_member else read_setup(session, session_id)
    return {
        **compact_project_payload(session, project),
        "setup": setup.model_dump(mode="json") if setup else None,
        "setup_state": "active" if setup else "none",
        "setup_blocked_reason": None,
        "correction_checkpoint_artifact_id": project.checkpoint_artifact_id
        if not branch_member
        else None,
        "source_checkpoint_artifact_id": project.checkpoint_artifact_id
        if not branch_member
        else None,
    }
