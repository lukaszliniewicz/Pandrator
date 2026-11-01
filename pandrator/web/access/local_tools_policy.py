"""Current original-personal-owner authority for legacy host-local tools."""

from __future__ import annotations

from collections.abc import Iterable
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.administration import AdministratorRequired
from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.identity import active_workspace, initial_personal_workspace
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import User, Workspace, WorkspaceMembership
from ..job_execution import current_job_lease
from ..models import Artifact, JobActorRecord, Voice
from ..voice_execution_identity import voice_execution_workspace
from .artifact_policy import ArtifactNotFound
from .artifact_visibility import resource_visibility_clause
from .contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from .current_session_policy import require_current_session_access


class LocalToolOperation(str, Enum):
    RVC_READ = "rvc_read"
    RVC_CONVERT = "rvc_convert"
    RVC_MODEL_WRITE = "rvc_model_write"
    TRAINING_READ = "training_read"
    TRAINING_RUN = "training_run"
    TRAINING_CANCEL = "training_cancel"


_SCOPES = {
    LocalToolOperation.RVC_READ: ("app.read",),
    LocalToolOperation.RVC_CONVERT: ("app.read", "app.run"),
    LocalToolOperation.RVC_MODEL_WRITE: ("app.admin", "app.read", "app.write"),
    LocalToolOperation.TRAINING_READ: ("app.admin", "app.read"),
    LocalToolOperation.TRAINING_RUN: ("app.admin", "app.read", "app.run"),
    LocalToolOperation.TRAINING_CANCEL: ("app.admin", "app.read", "app.cancel"),
}
_ADMIN = frozenset(
    {
        LocalToolOperation.RVC_MODEL_WRITE,
        LocalToolOperation.TRAINING_READ,
        LocalToolOperation.TRAINING_RUN,
        LocalToolOperation.TRAINING_CANCEL,
    }
)


def require_local_tool_workspace(
    session: Session, operation: LocalToolOperation, *, workspace_id: str | None = None
) -> str:
    if type(operation) is not LocalToolOperation:
        raise TypeError("A local tool operation is required.")
    actor = current_execution_actor.get()
    if actor is None and operation is LocalToolOperation.RVC_MODEL_WRITE:
        raise AdministratorRequired()
    with session.no_autoflush:
        if actor is None:
            lease = current_job_lease.get()
            if lease is not None:
                if (
                    type(lease) is not dict
                    or type(lease.get("job_id")) is not str
                    or not lease["job_id"]
                ):
                    raise AccessDenied()
                if (
                    session.scalar(
                        select(JobActorRecord.job_id).where(
                            JobActorRecord.job_id == lease["job_id"]
                        )
                    )
                    is not None
                ):
                    raise AccessDenied()
        elif type(actor) in {UserActor, HostActor}:
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=_SCOPES[operation],
                    require_admin=operation in _ADMIN,
                    allow_host=type(actor) is HostActor,
                )
            except PermissionError:
                raise AccessDenied() from None
        else:
            raise AccessDenied()
        if type(actor) is UserActor:
            statement = (
                select(Workspace.id)
                .join(User, User.id == Workspace.personal_owner_user_id)
                .join(WorkspaceMembership, WorkspaceMembership.workspace_id == Workspace.id)
                .where(
                    Workspace.kind == "personal",
                    Workspace.state == "active",
                    Workspace.personal_owner_user_id == actor.user_id,
                    User.legacy_subject == "owner",
                    WorkspaceMembership.user_id == actor.user_id,
                    WorkspaceMembership.role == "owner",
                    credential_workspace_clause(actor),
                )
            )
            if workspace_id is not None:
                statement = statement.where(Workspace.id == workspace_id)
            identifier = session.scalar(statement)
            if identifier is None:
                raise WorkspaceNotFound()
            return identifier
        return (
            active_workspace(session, workspace_id, "Local tools")
            if workspace_id is not None
            else initial_personal_workspace(session)
        ).id


def _require_local_tool_input_headers(
    session: Session,
    workspace_id: str,
    *,
    artifact_ids: Iterable[str] = (),
    voice_id: str | None = None,
    session_id: str | None = None,
) -> None:
    """Prove named-user headers and associated processing sessions without bodies."""
    actor = current_execution_actor.get()
    if type(actor) is not UserActor:
        return
    identifiers = list(artifact_ids)
    if any(type(identifier) is not str or not identifier for identifier in identifiers):
        raise ArtifactNotFound()
    identifiers = sorted(set(identifiers))
    associated = {session_id} if session_id is not None else set()
    for offset in range(0, len(identifiers), 500):
        chunk = identifiers[offset : offset + 500]
        headers = session.execute(
            select(Artifact.id, Artifact.workspace_id, Artifact.session_id).where(
                Artifact.id.in_(chunk),
                Artifact.workspace_id == workspace_id,
                Artifact.state != "deleted",
                resource_visibility_clause(Artifact, actor),
            )
        ).all()
        if {row.id for row in headers} != set(chunk):
            raise ArtifactNotFound()
        associated.update(row.session_id for row in headers if row.session_id is not None)
    if voice_id is not None:
        if (
            session.scalar(
                select(Voice.id).where(Voice.id == voice_id, Voice.workspace_id == workspace_id)
            )
            is None
        ):
            raise WorkspaceNotFound()
    for identifier in sorted(associated):
        require_current_session_access(session, identifier, ProjectAction.RUN)


def require_local_tool_inputs(
    session: Session,
    operation: LocalToolOperation,
    *,
    artifact_ids: Iterable[str] = (),
    voice_id: str | None = None,
    session_id: str | None = None,
    workspace_id: str | None = None,
) -> str:
    with session.no_autoflush:
        identifier = require_local_tool_workspace(session, operation, workspace_id=workspace_id)
        artifacts = tuple(artifact_ids)
        _require_local_tool_input_headers(
            session, identifier, artifact_ids=artifacts, voice_id=voice_id, session_id=session_id
        )
        voice_execution_workspace(
            session,
            {"workspace_id": identifier},
            voice_id=voice_id,
            artifact_ids=artifacts,
            session_id=session_id,
            expected_workspace_id=identifier,
        )
        return identifier
