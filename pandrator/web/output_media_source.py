"""Resolve the recording timeline shared by final export and soundtrack previews."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access.contracts import ProjectAction
from .access.current_session_policy import require_current_session_access
from .models import Artifact, MediaEditPlan, MediaEditPlanRevision
from .source_resolution import PrimarySourceResolution, classify_source, resolve_media_source


@dataclass(frozen=True, slots=True)
class OutputMediaSource:
    source: PrimarySourceResolution
    edit_revision_id: str | None
    edit_content_hash: str | None


def matching_media_edit_render_id(
    unit: Session,
    session_id: str,
    *,
    revision_id: str,
    content_hash: str,
    plan_id: str,
    revision_number: int,
    artifact_id: str | None = None,
) -> str | None:
    """Match owned current render headers without hydrating an Artifact body.

    Legacy generic revision IDs can identify a document instead of the edit.
    Retain WorkflowService's exact integer conversion for the legacy fallback;
    SQL integer casts would incorrectly accept values such as ``'1obsolete'``.
    """
    metadata = Artifact.metadata_json
    statement = (
        select(
            Artifact.id,
            metadata["media_edit_revision_id"].as_string().label("edit_revision_id"),
            metadata["revision_id"].as_string().label("revision_id"),
            metadata["plan_id"].as_string().label("plan_id"),
            metadata["revision"].label("revision"),
        )
        .where(
            Artifact.session_id == session_id,
            Artifact.role == "media_edit_media",
            Artifact.state == "current",
            metadata["content_hash"].as_string() == content_hash,
        )
        .order_by(Artifact.created_at.desc(), Artifact.id.desc())
    )
    if artifact_id is not None:
        statement = statement.where(Artifact.id == artifact_id)
    for candidate in unit.execute(statement):
        explicit = str(candidate.edit_revision_id or "")
        if explicit:
            if explicit == revision_id:
                return candidate.id
            continue
        if str(candidate.revision_id or "") == revision_id:
            return candidate.id
        value: Any = candidate.revision
        try:
            number = int(value) if value is not None else None
        except (TypeError, ValueError):
            continue
        if str(candidate.plan_id or "") == plan_id and number == revision_number:
            return candidate.id
    return None


def resolve_output_media_source(unit: Session, session_id: str) -> OutputMediaSource:
    """Require the active edit render when this session has an edit plan."""
    require_current_session_access(unit, session_id, ProjectAction.RUN)
    with unit.no_autoflush:
        plan = unit.execute(
            select(MediaEditPlan.id, MediaEditPlan.active_revision_id).where(
                MediaEditPlan.session_id == session_id
            )
        ).one_or_none()
        if plan is None:
            return OutputMediaSource(
                resolve_media_source(unit, session_id, include_metadata=False), None, None
            )
        error = "Render and review the media edit before exporting it."
        revision = unit.execute(
            select(
                MediaEditPlanRevision.id,
                MediaEditPlanRevision.content_hash,
                MediaEditPlanRevision.revision_number,
            ).where(
                MediaEditPlanRevision.id == plan.active_revision_id,
                MediaEditPlanRevision.plan_id == plan.id,
            )
        ).one_or_none()
        if revision is None:
            raise ValueError(error)
        artifact_id = matching_media_edit_render_id(
            unit,
            session_id,
            revision_id=revision.id,
            content_hash=revision.content_hash,
            plan_id=plan.id,
            revision_number=revision.revision_number,
        )
        if artifact_id is None:
            raise ValueError(error)
        artifact = unit.get_one(Artifact, artifact_id)
        name = str(
            (artifact.metadata_json or {}).get("original_filename")
            or artifact.relative_path.rsplit("/", 1)[-1]
        )
        kind, mime = str(artifact.kind or "video"), str(artifact.mime_type or "")
        return OutputMediaSource(
            PrimarySourceResolution(
                artifact=artifact,
                source_asset=None,
                attachment=None,
                profile=classify_source(name=name, kind=kind, mime_type=mime),
                name=name,
                kind=kind,
                mime_type=mime,
                resolution="derived_media_edit",
            ),
            revision.id,
            revision.content_hash,
        )
