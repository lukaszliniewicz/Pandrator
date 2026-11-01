"""Workspace admission/replay and current captured execution for speech previews."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.actor_authorization import require_actor_in_session
from ..models import Job
from ..speech_input_contracts import validate_speech_input_pin
from .contracts import AccessContext, AccessDenied
from .job_policy import JobAction, JobNotFound, require_job_access
from .voice_sample_execution_policy import require_voice_sample_execution_workspace
from .workspace_policy import WorkspaceAction, require_workspace_access


def require_preview_workspace(
    session: Session, context: AccessContext, workspace_id: str | None = None
) -> str:
    with session.no_autoflush:
        try:
            require_actor_in_session(
                session, context.actor, required_scopes=("app.read", "app.write", "app.run")
            )
        except PermissionError:
            raise AccessDenied() from None
        return require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.EDIT
        ).id


def require_preview_replay(
    session: Session, context: AccessContext, job_id: str, *, workspace_id: str | None = None
) -> Job:
    workspace_id = require_preview_workspace(session, context, workspace_id)
    with session.no_autoflush:
        identifier = session.scalar(
            select(Job.id).where(
                Job.id == job_id,
                Job.kind == "tts.preview",
                Job.workspace_id == workspace_id,
                Job.project_id.is_(None),
                Job.session_id.is_(None),
                Job.workflow_run_id.is_(None),
            )
        )
    if identifier is None:
        raise JobNotFound()
    return require_job_access(session, context, identifier, JobAction.RUN)


def preview_execution_workspace(
    session: Session,
    payload: Mapping[str, Any],
    *,
    expected_workspace_id: str | None = None,
) -> str:
    try:
        pin = validate_speech_input_pin(payload.get("speech_input"))
    except ValueError:
        raise AccessDenied() from None
    return require_voice_sample_execution_workspace(
        session,
        dict(payload),
        "tts.preview",
        artifact_ids=(pin["artifact_id"],) if pin is not None else (),
        expected_workspace_id=expected_workspace_id,
    ).id


__all__ = ["preview_execution_workspace", "require_preview_replay", "require_preview_workspace"]
