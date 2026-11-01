"""SQL-scoped workspace metadata; no content loads or account directory scans."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import and_, exists, or_, select
from sqlalchemy.orm import Session, aliased

from ..access.contracts import AccessContext
from ..access.workspace_policy import (
    WorkspaceAction,
    _workspace_access,
    _workspace_statement,
    public_workspace_role,
    require_workspace_access,
    require_workspace_actor,
    workspace_payload,
)
from ..model_base import utcnow
from .auth_models import UserCredential
from .models import User, Workspace, WorkspaceMembership
from .workspace_models import WorkspaceInvitation


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def invitation_statement():
    target, inviter = aliased(User), aliased(User)
    return (
        select(
            WorkspaceInvitation.id,
            WorkspaceInvitation.workspace_id,
            Workspace.name.label("workspace_name"),
            WorkspaceInvitation.user_id,
            target.display_name,
            WorkspaceInvitation.invited_by_user_id,
            inviter.display_name.label("invited_by_display_name"),
            WorkspaceInvitation.role,
            WorkspaceInvitation.state,
            WorkspaceInvitation.revision,
            WorkspaceInvitation.created_at,
            WorkspaceInvitation.expires_at,
            WorkspaceInvitation.responded_at,
        )
        .join(Workspace, Workspace.id == WorkspaceInvitation.workspace_id)
        .join(target, target.id == WorkspaceInvitation.user_id)
        .join(inviter, inviter.id == WorkspaceInvitation.invited_by_user_id)
    )


def invitation_payload(row: Any, *, now: datetime | None = None) -> dict[str, Any]:
    state = row.state
    if state == "pending" and aware(row.expires_at) <= aware(now or utcnow()):
        state = "expired"
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "workspace_name": row.workspace_name,
        "user_id": row.user_id,
        "display_name": row.display_name,
        "invited_by_user_id": row.invited_by_user_id,
        "invited_by_display_name": row.invited_by_display_name,
        "role": public_workspace_role(row.role),
        "state": state,
        "revision": row.revision,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "responded_at": row.responded_at.isoformat() if row.responded_at is not None else None,
    }


class WorkspaceQueries:
    @staticmethod
    def list_workspaces(
        session: Session, context: AccessContext, *, include_archived: bool = False
    ) -> list[dict[str, Any]]:
        require_workspace_actor(session, context)
        statement = _workspace_statement(context)
        if include_archived:
            archived = _workspace_statement(
                context, action=WorkspaceAction.MANAGE_READ, allow_archived=True
            ).where(Workspace.kind == "team", Workspace.state == "archived")
            combined = statement.union_all(archived).subquery()
            statement = select(combined)
        with session.no_autoflush:
            rows = session.execute(statement.order_by("created_at", "id")).all()
        return [workspace_payload(_workspace_access(row, context)) for row in rows]

    @staticmethod
    def members(
        session: Session, context: AccessContext, workspace_id: str
    ) -> list[dict[str, Any]]:
        require_workspace_access(session, context, workspace_id)
        with session.no_autoflush:
            rows = session.execute(
                select(
                    User.id,
                    User.display_name,
                    UserCredential.login_name,
                    WorkspaceMembership.role,
                    User.is_active,
                )
                .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
                .join(UserCredential, UserCredential.user_id == User.id)
                .where(WorkspaceMembership.workspace_id == workspace_id)
                .order_by(UserCredential.login_name, User.id)
            ).all()
        return [
            dict(
                user_id=row.id,
                display_name=row.display_name,
                login_name=row.login_name,
                role=public_workspace_role(row.role),
                is_active=row.is_active,
            )
            for row in rows
        ]

    @staticmethod
    def invitations(
        session: Session, context: AccessContext, workspace_id: str
    ) -> list[dict[str, Any]]:
        require_workspace_access(session, context, workspace_id, action=WorkspaceAction.MANAGE_READ)
        with session.no_autoflush:
            rows = session.execute(
                invitation_statement()
                .where(WorkspaceInvitation.workspace_id == workspace_id)
                .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
            ).all()
        return [invitation_payload(row) for row in rows]

    @staticmethod
    def inbox(session: Session, context: AccessContext) -> list[dict[str, Any]]:
        require_workspace_access(session, context)
        with session.no_autoflush:
            rows = session.execute(
                invitation_statement()
                .where(
                    WorkspaceInvitation.user_id == context.actor.user_id,
                    WorkspaceInvitation.state.in_(("pending", "accepted")),
                )
                .order_by(WorkspaceInvitation.created_at.desc(), WorkspaceInvitation.id.desc())
                .limit(100)
            ).all()
        return [invitation_payload(row) for row in rows]

    @staticmethod
    def search_users(
        session: Session, context: AccessContext, workspace_id: str, query: str
    ) -> list[dict[str, Any]]:
        require_workspace_access(session, context, workspace_id, action=WorkspaceAction.MANAGE_READ)
        if type(query) is not str or not 2 <= len(query.strip()) <= 64:
            return []
        escaped = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = f"%{escaped}%"
        with session.no_autoflush:
            rows = (
                session.execute(
                    select(User.id.label("user_id"), UserCredential.login_name, User.display_name)
                    .join(UserCredential, UserCredential.user_id == User.id)
                    .where(
                        User.is_active.is_(True),
                        or_(
                            User.display_name.ilike(pattern, escape="\\"),
                            UserCredential.login_name.ilike(pattern, escape="\\"),
                        ),
                        ~exists(
                            select(WorkspaceMembership.user_id).where(
                                and_(
                                    WorkspaceMembership.workspace_id == workspace_id,
                                    WorkspaceMembership.user_id == User.id,
                                )
                            )
                        ),
                    )
                    .order_by(UserCredential.login_name, User.id)
                    .limit(20)
                )
                .mappings()
                .all()
            )
        return [dict(row) for row in rows]
