"""Fresh caller and workspace authority for sound-library metadata and commands."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.actor_models import JobActorRecord
from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import Workspace
from ..job_execution import current_job_lease
from ..models import SoundItem, SoundVariant
from ..sound_identity import sound_creation_workspace
from .contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from .project_visibility import workspace_authority

_SOUND_LIBRARY_ACTIONS = frozenset({ProjectAction.READ, ProjectAction.EDIT, ProjectAction.RUN})
_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})


def _deny_saved_actorless_lease(session: Session) -> None:
    lease = current_job_lease.get()
    if lease is None:
        return
    if (
        type(lease) is not dict
        or lease.keys() != _LEASE_KEYS
        or type(lease.get("job_id")) is not str
        or not lease["job_id"]
        or type(lease.get("owner_id")) is not str
        or not lease["owner_id"]
        or type(lease.get("lease_generation")) is not int
        or lease["lease_generation"] <= 0
    ):
        raise AccessDenied()
    if session.scalar(
        select(JobActorRecord.job_id).where(JobActorRecord.job_id == lease["job_id"])
    ) is not None:
        raise AccessDenied()


def require_sound_library_workspace(
    session: Session,
    action: ProjectAction,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Prove current authority and retain the workspace within this transaction."""
    if type(action) is not ProjectAction or action not in _SOUND_LIBRARY_ACTIONS:
        raise TypeError("Sound-library access requires READ, EDIT or RUN authority.")

    scopes = {
        ProjectAction.READ: ("app.read",),
        ProjectAction.EDIT: ("app.read", "app.write"),
        ProjectAction.RUN: ("app.read", "app.run"),
    }[action]
    actor = current_execution_actor.get()
    with session.no_autoflush:
        if type(actor) is UserActor:
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=scopes,
                )
            except PermissionError:
                raise AccessDenied() from None

            statement = select(Workspace).where(
                Workspace.state == "active",
                workspace_authority(actor.user_id, action=action),
                credential_workspace_clause(actor),
            )
            if workspace_id is None:
                statement = statement.where(
                    Workspace.kind == "personal",
                    Workspace.personal_owner_user_id == actor.user_id,
                )
            else:
                statement = statement.where(Workspace.id == workspace_id)
            workspace = session.scalar(statement)
            if workspace is None:
                raise WorkspaceNotFound()
            return workspace

        if type(actor) is HostActor:
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=scopes,
                    allow_host=True,
                )
            except PermissionError:
                raise AccessDenied() from None
            return sound_creation_workspace(session, workspace_id=workspace_id)

        if actor is not None:
            raise AccessDenied()

        _deny_saved_actorless_lease(session)
        return sound_creation_workspace(session, workspace_id=workspace_id)


def require_sound_library_headers(
    session: Session,
    workspace_id: str,
    *,
    item_id: str | None = None,
    candidate_id: str | None = None,
) -> None:
    """Check caller-supplied IDs using workspace-filtered scalar header reads."""
    with session.no_autoflush:
        if item_id is not None and session.scalar(
            select(SoundItem.id).where(
                SoundItem.id == item_id,
                SoundItem.workspace_id == workspace_id,
            )
        ) is None:
            raise WorkspaceNotFound()
        if candidate_id is not None and session.scalar(
            select(SoundVariant.id).where(
                SoundVariant.id == candidate_id,
                SoundVariant.workspace_id == workspace_id,
            )
        ) is None:
            raise WorkspaceNotFound()


__all__ = [
    "require_sound_library_headers",
    "require_sound_library_workspace",
]
