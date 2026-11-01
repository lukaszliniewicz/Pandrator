"""Current authority to admit processing of an existing managed artifact."""

from __future__ import annotations

from sqlalchemy.orm import Session

from .access.artifact_policy import ArtifactNotFound, require_artifact_access
from .access.contracts import AccessContext, AccessDenied, ProjectAction
from .access.project_policy import require_project_access
from .accounts.actor_authorization import require_actor_in_session
from .models import Artifact


def require_artifact_run_access(
    session: Session,
    context: AccessContext,
    artifact_id: str,
) -> Artifact:
    """Prove READ plus RUN in the caller's unit before processing admission.

    Rootless content already requires actual workspace ownership in the read
    policy. Project roles add their own RUN restriction. This strict transport
    check is not the authorization contract for an already-admitted worker.
    """
    artifact = require_artifact_access(session, context, artifact_id)
    if artifact.state == "deleted":
        raise ArtifactNotFound()
    try:
        with session.no_autoflush:
            require_actor_in_session(
                session, context.actor, required_scopes=("app.read", "app.run")
            )
    except PermissionError:
        raise AccessDenied() from None
    if artifact.project_id is not None:
        require_project_access(session, context, artifact.project_id, ProjectAction.RUN)
    return artifact


def can_run_artifact(session: Session, context: AccessContext, artifact_id: str) -> bool:
    """A current UI hint, never authority for a later preparation request."""
    try:
        require_artifact_run_access(session, context, artifact_id)
    except AccessDenied:
        return False
    return True


__all__ = ["can_run_artifact", "require_artifact_run_access"]
