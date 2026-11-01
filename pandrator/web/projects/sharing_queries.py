"""Fresh SQL queries for named project viewing."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Select, and_, exists, not_, or_, select
from sqlalchemy.orm import Session

from ..access.contracts import (
    AccessContext,
    AccessDenied,
    ProjectAccess,
    ProjectAction,
    ProjectNotFound,
)
from ..access.project_policy import require_project_access, visible_project_ids_statement
from ..accounts.auth_models import UserCredential
from ..accounts.models import User, WorkspaceMembership
from ..models import SessionRecord
from .models import Project, ProjectMembership
from .sharing_contracts import project_sharing_error


def _escaped_like(value: str) -> str:
    return "%" + value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC).isoformat()
    return value.astimezone(UTC).isoformat()


def _project_viewers_statement(project_id: str) -> Select[tuple[Any, ...]]:
    return (
        select(
            Project.revision,
            ProjectMembership.user_id,
            UserCredential.login_name,
            User.display_name,
            User.is_active,
            ProjectMembership.created_at,
            ProjectMembership.granted_by_user_id,
        )
        .select_from(Project)
        .outerjoin(
            ProjectMembership,
            ProjectMembership.project_id == Project.id,
        )
        .outerjoin(User, User.id == ProjectMembership.user_id)
        .outerjoin(UserCredential, UserCredential.user_id == ProjectMembership.user_id)
        .where(Project.id == project_id)
        .order_by(ProjectMembership.user_id)
        .limit(1001)
    )


def _viewers_projection(session: Session, project_id: str) -> dict[str, Any]:
    rows = session.execute(_project_viewers_statement(project_id)).all()
    if not rows:
        # Callers establish current manager authority before this projection.
        raise ProjectNotFound()
    revision = rows[0].revision
    memberships = [row for row in rows if row.user_id is not None]
    if len(memberships) > 1000:
        raise project_sharing_error(
            "validation_error", message="Too many viewing grants to display."
        )
    return {
        "project_id": project_id,
        "revision": revision,
        "items": [
            {
                "user_id": row.user_id,
                "login_name": row.login_name,
                "display_name": row.display_name,
                "is_active": bool(row.is_active),
                "access": "view",
                "granted_at": _iso(row.created_at),
                "granted_by_user_id": row.granted_by_user_id,
            }
            for row in memberships
        ],
    }


def require_manager(
    session: Session, context: AccessContext, project_id: str, *, write: bool = False,
) -> ProjectAccess:
    """Resolve workspace governance separately from a credential's write scope."""
    access = require_project_access(
        session, context, project_id,
        ProjectAction.MANAGE_MEMBERS if write else ProjectAction.READ,
    )
    if access.workspace_role not in {"owner", "admin"}:
        raise AccessDenied()
    return access


class ProjectSharingQueries:
    @staticmethod
    def require_manager(
        session: Session, context: AccessContext, project_id: str, *, write: bool = False,
    ) -> ProjectAccess:
        return require_manager(session, context, project_id, write=write)

    @staticmethod
    def list_viewers(
        session: Session, context: AccessContext, project_id: str
    ) -> dict[str, Any]:
        require_manager(session, context, project_id)
        return _viewers_projection(session, project_id)

    @staticmethod
    def _viewers_after_authorized_command(session: Session, project_id: str) -> dict[str, Any]:
        return _viewers_projection(session, project_id)

    @staticmethod
    def search_users(
        session: Session,
        context: AccessContext,
        project_id: str,
        query: str,
    ) -> list[dict[str, str]]:
        require_manager(session, context, project_id)
        if type(query) is not str:
            return []
        normalized = query.strip()
        if not 2 <= len(normalized) <= 64:
            return []

        term = _escaped_like(normalized)
        workspace_member = exists(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == Project.workspace_id,
                WorkspaceMembership.user_id == User.id,
            )
        )
        current_project_grant = exists(
            select(ProjectMembership.user_id).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == User.id,
            )
        )
        rows = session.execute(
            select(User.id, UserCredential.login_name, User.display_name)
            .join(UserCredential, UserCredential.user_id == User.id)
            .join(Project, Project.id == project_id)
            .where(
                User.is_active.is_(True),
                or_(
                    UserCredential.login_name.ilike(term, escape="\\"),
                    User.display_name.ilike(term, escape="\\"),
                ),
                not_(workspace_member),
                not_(current_project_grant),
            )
            .order_by(UserCredential.login_name, User.id)
            .limit(20)
        ).all()
        return [
            {"user_id": row.id, "login_name": row.login_name, "display_name": row.display_name}
            for row in rows
        ]

    @staticmethod
    def shared_with_me(
        session: Session,
        context: AccessContext,
        query: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> list[dict[str, str | None]]:
        visible = visible_project_ids_statement(
            session, context, ProjectAction.READ
        ).subquery()
        actor_id = context.actor.user_id
        if type(query) is not str or len(query.strip()) > 255:
            raise project_sharing_error(
                "validation_error",
                message="Search text must contain at most 255 characters.",
            )
        if (
            type(limit) is not int
            or not 1 <= limit <= 100
            or type(offset) is not int
            or offset < 0
        ):
            raise project_sharing_error(
                "validation_error", message="Invalid shared-project pagination."
            )

        term = query.strip()
        statement = (
            select(
                Project.id.label("project_id"),
                SessionRecord.id.label("source_session_id"),
                SessionRecord.name,
                SessionRecord.workflow_kind,
                SessionRecord.source_language,
                SessionRecord.target_language,
                SessionRecord.updated_at,
            )
            .join(visible, visible.c.id == Project.id)
            .join(
                ProjectMembership,
                and_(
                    ProjectMembership.project_id == Project.id,
                    ProjectMembership.user_id == actor_id,
                ),
            )
            .join(
                SessionRecord,
                and_(
                    SessionRecord.id == Project.source_session_id,
                    SessionRecord.project_id == Project.id,
                    SessionRecord.trashed_at.is_(None),
                ),
            )
            .where(
                not_(
                    exists(
                        select(WorkspaceMembership.user_id).where(
                            WorkspaceMembership.workspace_id == Project.workspace_id,
                            WorkspaceMembership.user_id == actor_id,
                        )
                    )
                ),
            )
        )
        if term:
            pattern = _escaped_like(term)
            statement = statement.where(
                or_(
                    SessionRecord.name.ilike(pattern, escape="\\"),
                    SessionRecord.workflow_kind.ilike(pattern, escape="\\"),
                    SessionRecord.source_language.ilike(pattern, escape="\\"),
                    SessionRecord.target_language.ilike(pattern, escape="\\"),
                )
            )
        rows = session.execute(
            statement.order_by(SessionRecord.updated_at.desc(), Project.id)
            .limit(limit)
            .offset(offset)
        ).all()
        return [
            {
                "project_id": row.project_id,
                "source_session_id": row.source_session_id,
                "name": row.name,
                "workflow_kind": row.workflow_kind,
                "source_language": row.source_language,
                "target_language": row.target_language,
                "updated_at": _iso(row.updated_at),
            }
            for row in rows
        ]


__all__ = ["ProjectSharingQueries", "require_manager"]
