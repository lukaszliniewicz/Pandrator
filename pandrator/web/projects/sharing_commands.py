"""Transactional named project viewing commands."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Callable, cast

from sqlalchemy import delete, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..access.contracts import AccessContext
from ..accounts.auth_models import UserCredential
from ..accounts.credential_resources import remove_user_project_resources
from ..accounts.models import User, WorkspaceMembership
from ..model_base import utcnow
from ..models import SessionRecord
from .models import Project, ProjectMembership
from .sharing_contracts import ProjectSharingAuditSink, project_sharing_error
from .sharing_queries import ProjectSharingQueries


class ProjectSharingCommands:
    def __init__(
        self,
        audit: ProjectSharingAuditSink,
        *,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self._audit = audit
        self._clock = clock

    @staticmethod
    def _revision(session: Session, project_id: str, expected_revision: int) -> int:
        if type(expected_revision) is not int or expected_revision < 1:
            raise project_sharing_error("invalid_revision")
        revision = session.scalar(select(Project.revision).where(Project.id == project_id))
        if revision is None or revision != expected_revision:
            raise project_sharing_error("project_revision_conflict")
        return revision

    @staticmethod
    def _source_available(session: Session, project_id: str) -> None:
        source_id = session.scalar(
            select(Project.source_session_id).where(Project.id == project_id)
        )
        if source_id is None or session.scalar(
            select(SessionRecord.id).where(
                SessionRecord.id == source_id,
                SessionRecord.project_id == project_id,
                SessionRecord.trashed_at.is_(None),
            )
        ) is None:
            raise project_sharing_error("project_unavailable")

    @staticmethod
    def _target(
        session: Session, project_id: str, user_id: str
    ) -> tuple[bool, bool]:
        row = session.execute(
            select(User.id, User.is_active)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(User.id == user_id)
        ).one_or_none()
        if row is None or not row.is_active:
            raise project_sharing_error("user_not_available")
        workspace_id = session.scalar(
            select(Project.workspace_id).where(Project.id == project_id)
        )
        if workspace_id is None:
            raise project_sharing_error("project_revision_conflict")
        if session.scalar(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        ) is not None:
            raise project_sharing_error("already_workspace_member")
        role = session.scalar(
            select(ProjectMembership.role).where(
                ProjectMembership.project_id == project_id,
                ProjectMembership.user_id == user_id,
            )
        )
        return role is not None, role == "viewer"

    def _record(
        self,
        session: Session,
        context: AccessContext,
        *,
        action: str,
        project_id: str,
        user_id: str,
        revision: int,
    ) -> None:
        self._audit(
            session,
            context,
            action=action,
            project_id=project_id,
            metadata={"user_id": user_id, "role": "viewer", "revision": revision},
        )

    @staticmethod
    def _bump_revision(
        session: Session, project_id: str, expected_revision: int, now: datetime
    ) -> int:
        result = cast(
            CursorResult[Any],
            session.execute(
                update(Project)
                .where(Project.id == project_id, Project.revision == expected_revision)
                .values(revision=Project.revision + 1, updated_at=now)
            ),
        )
        if result.rowcount != 1:
            raise project_sharing_error("project_revision_conflict")
        return expected_revision + 1

    def grant(
        self,
        session: Session,
        context: AccessContext,
        project_id: str,
        user_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        ProjectSharingQueries.require_manager(session, context, project_id, write=True)
        revision = self._revision(session, project_id, expected_revision)
        self._source_available(session, project_id)
        existing, is_viewer = self._target(session, project_id, user_id)
        if is_viewer:
            return ProjectSharingQueries.list_viewers(session, context, project_id)

        now = self._clock()
        if existing:
            session.execute(
                update(ProjectMembership)
                .where(
                    ProjectMembership.project_id == project_id,
                    ProjectMembership.user_id == user_id,
                )
                .values(
                    role="viewer",
                    granted_by_user_id=context.actor.user_id,
                    created_at=now,
                )
            )
        else:
            session.add(
                ProjectMembership(
                    project_id=project_id,
                    user_id=user_id,
                    role="viewer",
                    granted_by_user_id=context.actor.user_id,
                    created_at=now,
                )
            )
        session.flush()
        updated_revision = self._bump_revision(session, project_id, revision, now)
        self._record(
            session,
            context,
            action="project.view.grant",
            project_id=project_id,
            user_id=user_id,
            revision=updated_revision,
        )
        return ProjectSharingQueries.list_viewers(session, context, project_id)

    def revoke(
        self,
        session: Session,
        context: AccessContext,
        project_id: str,
        user_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        ProjectSharingQueries.require_manager(session, context, project_id, write=True)
        revision = self._revision(session, project_id, expected_revision)
        membership = cast(
            CursorResult[Any],
            session.execute(
                delete(ProjectMembership).where(
                    ProjectMembership.project_id == project_id,
                    ProjectMembership.user_id == user_id,
                )
            ),
        )
        resources_removed = remove_user_project_resources(session, user_id, project_id)
        if membership.rowcount or resources_removed:
            now = self._clock()
            updated_revision = self._bump_revision(session, project_id, revision, now)
            self._record(
                session,
                context,
                action="project.view.revoke",
                project_id=project_id,
                user_id=user_id,
                revision=updated_revision,
            )
        # The command's manager check is authoritative for this transaction. The
        # target may be the caller, whose credential grants were just removed.
        return ProjectSharingQueries._viewers_after_authorized_command(session, project_id)


__all__ = ["ProjectSharingCommands"]
