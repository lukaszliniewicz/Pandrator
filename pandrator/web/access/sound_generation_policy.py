"""Prove current named-user sound job roots before loading private records."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import Job, SoundVariant
from .contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from .sound_library_policy import _deny_saved_actorless_lease, require_sound_library_workspace


def require_sound_generation_access(session: Session, payload: dict[str, Any]) -> Workspace | None:
    """Prove actor, active lease and immutable workspace headers before bodies."""
    actor = current_execution_actor.get()
    with session.no_autoflush:
        if actor is None:
            _deny_saved_actorless_lease(session)
            return None
        if type(actor) is HostActor:
            try:
                require_execution_actor_in_session(
                    session, actor, required_scopes=("app.read", "app.run"), allow_host=True
                )
            except PermissionError:
                raise AccessDenied() from None
            return None
        if type(actor) is not UserActor:
            raise AccessDenied()
        lease = current_job_lease.get()
        if (
            type(lease) is not dict
            or type(payload) is not dict
            or payload.get("_job_id") != lease.get("job_id")
            or payload.get("_lease_generation") != lease.get("lease_generation")
        ):
            raise AccessDenied()
        # Only scalar headers may be examined until content authority is proved.
        header = session.execute(
            select(
                Job.kind, Job.workspace_id, Job.project_id, Job.session_id, Job.workflow_run_id
            ).where(Job.id == payload.get("_job_id"))
        ).one_or_none()
        if (
            header is None
            or header.kind != "sound.generate"
            or header.project_id is not None
            or header.session_id is not None
            or header.workflow_run_id is not None
        ):
            raise WorkspaceNotFound()
        workspace = require_sound_library_workspace(
            session, ProjectAction.RUN, workspace_id=header.workspace_id
        )
        if "workspace_id" in payload:
            supplied = payload["workspace_id"]
            if (
                type(supplied) is not str
                or not supplied
                or supplied != supplied.strip()
                or supplied != workspace.id
            ):
                raise WorkspaceNotFound()
        candidate_id = payload.get("candidate_id")
        if (
            type(candidate_id) is not str
            or not candidate_id
            or session.scalar(
                select(SoundVariant.id).where(
                    SoundVariant.id == candidate_id,
                    SoundVariant.workspace_id == workspace.id,
                    SoundVariant.job_id == payload.get("_job_id"),
                )
            )
            is None
        ):
            raise WorkspaceNotFound()
        return workspace


__all__ = ["require_sound_generation_access"]
