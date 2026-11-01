"""Account and workspace identity checks, independent of project services."""

from __future__ import annotations

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .models import User, Workspace, WorkspaceMembership


def initial_personal_workspace(session: Session) -> Workspace:
    """Return the migration-seeded active owner workspace without repairing it."""
    with session.no_autoflush:
        row = session.execute(
            select(User, Workspace, WorkspaceMembership)
            .select_from(User)
            .outerjoin(
                Workspace,
                and_(
                    Workspace.personal_owner_user_id == User.id,
                    Workspace.kind == "personal",
                ),
            )
            .outerjoin(
                WorkspaceMembership,
                and_(
                    WorkspaceMembership.workspace_id == Workspace.id,
                    WorkspaceMembership.user_id == User.id,
                ),
            )
            .where(User.legacy_subject == "owner")
        ).one_or_none()
        if row is None:
            raise RuntimeError("The migration-seeded owner user is missing.")
        owner, workspace, membership = row
        if not owner.is_active or not owner.is_instance_admin:
            raise RuntimeError(
                "The migration-seeded owner user is inactive or is not an instance admin."
            )

        if workspace is None:
            raise RuntimeError("The migration-seeded owner personal workspace is missing.")
        if workspace.state != "active":
            raise RuntimeError("The migration-seeded owner personal workspace is not active.")

        if membership is None or membership.role != "owner":
            raise RuntimeError(
                "The migration-seeded owner workspace membership is missing or invalid."
            )
    return workspace


def active_workspace(session: Session, workspace_id: str, context: str) -> Workspace:
    """Resolve an existing active owner; this does not grant caller authority."""
    with session.no_autoflush:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise ValueError(f"{context} references missing workspace {workspace_id!r}.")
        if workspace.state != "active":
            raise ValueError(f"{context} belongs to inactive workspace {workspace_id!r}.")
    return workspace
