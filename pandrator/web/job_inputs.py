"""Resolve typed media-job context from persisted input artifacts."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy.orm import Session

from .job_identity import JobOwnership, job_creation_ownership
from .models import Artifact
from .projects.artifact_identity import resolve_artifact_ownership


def artifact_job_ownership(
    session: Session,
    artifact_ids: Sequence[str],
    *,
    session_id: str | None = None,
    project_scoped: bool = True,
) -> JobOwnership:
    """Validate inputs without accepting payload hints as ownership authority."""
    with session.no_autoflush:
        owners = []
        for identifier in artifact_ids:
            artifact = session.get(Artifact, identifier, populate_existing=True)
            if artifact is None or artifact.state == "deleted":
                raise KeyError(identifier)
            owner = resolve_artifact_ownership(session, artifact=artifact)
            # Recheck the root using fresh column reads at writer admission.
            owners.append(
                job_creation_ownership(
                    session,
                    session_id=artifact.session_id,
                    project_id=owner.project_id,
                    workspace_id=owner.workspace_id,
                )
            )
        if not owners:
            raise ValueError("A media job requires an input artifact.")
        workspace_id = owners[0].workspace_id
        if any(owner.workspace_id != workspace_id for owner in owners):
            raise ValueError("The job inputs belong to different workspaces.")
        if session_id is not None:
            return job_creation_ownership(session, session_id=session_id, workspace_id=workspace_id)
        if not project_scoped:
            return JobOwnership(workspace_id, None)
        if any(owner.project_id != owners[0].project_id for owner in owners):
            raise ValueError("The media job inputs belong to different projects.")
        return owners[0]


__all__ = ["artifact_job_ownership"]
