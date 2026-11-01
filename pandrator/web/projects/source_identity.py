"""Source ownership checks shared by source creation and attachment paths."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.identity import active_workspace as _active_workspace
from ..accounts.models import Workspace
from ..models import Artifact, SourceAsset
from .artifact_identity import resolve_artifact_ownership
from .identity import initial_personal_workspace, session_project
from .models import Project


def _session_workspace_id(session: Session, session_id: str) -> str:
    project: Project = session_project(session, session_id)
    return project.workspace_id


def source_creation_workspace(
    session: Session,
    artifact: Artifact,
    *,
    workspace_id: str | None = None,
) -> Workspace:
    """Resolve a source's existing owner or the current solo creation workspace.

    This validates stored ownership only. It does not establish that a caller is
    authorized to use the returned workspace.
    """
    candidates: dict[str, list[str]] = {}
    with session.no_autoflush:
        if artifact.workspace_id is not None or artifact.project_id is not None:
            ownership = resolve_artifact_ownership(session, artifact=artifact)
            candidates.setdefault(ownership.workspace_id, []).append(
                f"stored artifact {artifact.id!r}"
            )
        if artifact.session_id is not None:
            session_workspace_id = _session_workspace_id(session, artifact.session_id)
            candidates.setdefault(session_workspace_id, []).append(
                f"artifact session {artifact.session_id!r}"
            )

        existing_assets = session.execute(
            select(SourceAsset.id, SourceAsset.workspace_id)
            .where(SourceAsset.artifact_id == artifact.id)
            .order_by(SourceAsset.id)
        ).all()
        for asset_id, asset_workspace_id in existing_assets:
            if asset_workspace_id is None:
                raise ValueError(f"Source asset {asset_id!r} has no workspace ownership.")
            candidates.setdefault(asset_workspace_id, []).append(f"source asset {asset_id!r}")

        if len(candidates) > 1:
            detail = "; ".join(
                f"{candidate_id!r} from {', '.join(sources)}"
                for candidate_id, sources in sorted(candidates.items())
            )
            raise ValueError(
                f"Artifact {artifact.id!r} has conflicting source workspace ownership: {detail}."
            )

        if workspace_id is not None:
            workspace = _active_workspace(session, workspace_id, "Requested source workspace")
            if candidates and workspace_id not in candidates:
                existing_id = next(iter(candidates))
                raise ValueError(
                    f"Artifact {artifact.id!r} belongs to workspace {existing_id!r}, "
                    f"not requested workspace {workspace_id!r}."
                )
            return workspace

        if candidates:
            candidate_id = next(iter(candidates))
            return _active_workspace(
                session, candidate_id, f"Artifact {artifact.id!r} source ownership"
            )

        return initial_personal_workspace(session)


def validate_source_attachment(
    session: Session,
    session_id: str,
    asset: SourceAsset,
) -> None:
    """Require the selected source and its artifact to share the target owner."""
    with session.no_autoflush:
        project = session_project(session, session_id)
        target_workspace = _active_workspace(
            session, project.workspace_id, f"Session {session_id!r} project"
        )

        if asset.workspace_id is None:
            raise ValueError(f"Source asset {asset.id!r} has no workspace ownership.")
        asset_workspace = _active_workspace(
            session, asset.workspace_id, f"Source asset {asset.id!r}"
        )
        if asset_workspace.id != target_workspace.id:
            raise ValueError(
                f"Source asset {asset.id!r} belongs to workspace {asset_workspace.id!r}, "
                f"not target workspace {target_workspace.id!r}."
            )

        if asset.artifact_id is not None:
            artifact = session.get(Artifact, asset.artifact_id)
            if artifact is None:
                raise KeyError(
                    f"Source asset {asset.id!r} references missing artifact {asset.artifact_id!r}."
                )
            source_creation_workspace(
                session,
                artifact,
                workspace_id=target_workspace.id,
            )


__all__ = ["source_creation_workspace", "validate_source_attachment"]
