"""Workspace writes inside the caller's short immediate transaction."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from ..access.contracts import AccessContext
from ..access.workspace_policy import (
    WorkspaceAccess,
    WorkspaceAction,
    require_workspace_access,
    require_workspace_actor,
    workspace_payload,
)
from ..model_base import utcnow
from ..projects.models import Project, ProjectMembership
from .auth_models import UserCredential
from .credential_resources import enroll_workspace_resources, remove_user_workspace_resources
from .models import User, Workspace, WorkspaceMembership
from .workspace_contracts import WorkspaceAuditSink, workspace_error
from .workspace_models import WorkspaceInvitation
from .workspace_queries import aware, invitation_payload, invitation_statement


def _name(value: str) -> str:
    if type(value) is not str or not 1 <= len(value.strip()) <= 255:
        raise workspace_error("invalid_workspace_name")
    return value.strip()


def _role(value: str) -> str:
    if type(value) is not str or value not in {"manager", "member"}:
        raise workspace_error("invalid_workspace_role")
    return "admin" if value == "manager" else "member"


def _team_revision(access: WorkspaceAccess, expected_revision: int) -> None:
    if access.kind != "team":
        raise workspace_error("personal_workspace_protected")
    if type(expected_revision) is not int or expected_revision < 1:
        raise workspace_error("invalid_revision")
    if access.revision != expected_revision:
        raise workspace_error("workspace_revision_conflict")


def _revoke_pending(session: Session, workspace_id: str, now: datetime, *clauses: Any) -> None:
    session.execute(
        update(WorkspaceInvitation)
        .where(
            WorkspaceInvitation.workspace_id == workspace_id,
            WorkspaceInvitation.state == "pending",
            *clauses,
        )
        .values(state="revoked", responded_at=now, revision=WorkspaceInvitation.revision + 1)
    )


class WorkspaceCommands:
    def __init__(
        self, *, audit: WorkspaceAuditSink, clock: Callable[[], datetime] = utcnow
    ) -> None:
        self._audit = audit
        self._clock = clock

    def _bump(
        self, session: Session, access: WorkspaceAccess, now: datetime, **changes: Any
    ) -> WorkspaceAccess:
        session.execute(
            update(Workspace)
            .where(Workspace.id == access.id)
            .values(revision=access.revision + 1, updated_at=now, **changes)
        )
        return replace(access, revision=access.revision + 1, updated_at=now, **changes)

    def _record(
        self,
        session: Session,
        context: AccessContext,
        action: str,
        workspace_id: str,
        revision: int,
        **metadata: str | int | bool | None,
    ) -> None:
        self._audit(
            session,
            context,
            action=action,
            workspace_id=workspace_id,
            metadata={"revision": revision, **metadata},
        )

    @staticmethod
    def _member(session: Session, workspace_id: str, user_id: str) -> tuple[str, bool]:
        row = session.execute(
            select(WorkspaceMembership.role, User.is_active)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        ).one_or_none()
        if row is None:
            raise workspace_error("user_not_available")
        return row.role, row.is_active

    @staticmethod
    def _guard_manager(
        session: Session, workspace_id: str, user_id: str, role: str, active: bool
    ) -> None:
        if role not in {"owner", "admin"} or not active:
            return
        other = session.scalar(
            select(WorkspaceMembership.user_id)
            .join(User, User.id == WorkspaceMembership.user_id)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id != user_id,
                WorkspaceMembership.role.in_(("owner", "admin")),
                User.is_active.is_(True),
            )
            .limit(1)
        )
        if other is None:
            raise workspace_error("last_workspace_manager")

    def create(self, session: Session, context: AccessContext, *, name: str) -> dict[str, Any]:
        require_workspace_actor(session, context, enroll=True)
        normalized = _name(name)
        now = aware(self._clock())
        workspace = Workspace(
            kind="team",
            name=normalized,
            personal_owner_user_id=None,
            state="active",
            revision=1,
            created_at=now,
            updated_at=now,
        )
        session.add(workspace)
        session.flush([workspace])
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=context.actor.user_id,
                role="admin",
                created_at=now,
            )
        )
        session.flush()
        enroll_workspace_resources(session, context.actor, workspace.id)
        self._record(session, context, "workspace.create", workspace.id, 1)
        return workspace_payload(require_workspace_access(session, context, workspace.id))

    def rename(
        self,
        session: Session,
        context: AccessContext,
        workspace_id: str,
        *,
        name: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE
        )
        _team_revision(access, expected_revision)
        normalized = _name(name)
        if normalized == access.name:
            return workspace_payload(access)
        updated = self._bump(session, access, aware(self._clock()), name=normalized)
        self._record(session, context, "workspace.rename", workspace_id, updated.revision)
        return workspace_payload(updated)

    def set_member_role(
        self,
        session: Session,
        context: AccessContext,
        workspace_id: str,
        *,
        user_id: str,
        role: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE
        )
        _team_revision(access, expected_revision)
        desired = _role(role)
        current, active = self._member(session, workspace_id, user_id)
        if current == desired:
            return workspace_payload(access)
        now = aware(self._clock())
        if current in {"owner", "admin"} and desired == "member":
            self._guard_manager(session, workspace_id, user_id, current, active)
            _revoke_pending(
                session, workspace_id, now, WorkspaceInvitation.invited_by_user_id == user_id
            )
        session.execute(
            update(WorkspaceMembership)
            .where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
            .values(role=desired)
        )
        updated = self._bump(session, access, now)
        self._record(
            session,
            context,
            "workspace.set_member_role",
            workspace_id,
            updated.revision,
            user_id=user_id,
            role=role,
        )
        # A manager may demote themself; reflect the current role in the result.
        return workspace_payload(require_workspace_access(session, context, workspace_id))

    def _remove(
        self,
        session: Session,
        context: AccessContext,
        access: WorkspaceAccess,
        user_id: str,
        expected_revision: int,
        *,
        action: str,
    ) -> dict[str, Any]:
        _team_revision(access, expected_revision)
        current, active = self._member(session, access.id, user_id)
        self._guard_manager(session, access.id, user_id, current, active)
        now = aware(self._clock())
        remove_user_workspace_resources(session, user_id, access.id)
        session.execute(
            delete(ProjectMembership).where(
                ProjectMembership.user_id == user_id,
                ProjectMembership.project_id.in_(
                    select(Project.id).where(Project.workspace_id == access.id)
                ),
            )
        )
        session.execute(
            delete(WorkspaceMembership).where(
                WorkspaceMembership.workspace_id == access.id,
                WorkspaceMembership.user_id == user_id,
            )
        )
        _revoke_pending(
            session,
            access.id,
            now,
            or_(
                WorkspaceInvitation.user_id == user_id,
                WorkspaceInvitation.invited_by_user_id == user_id,
            ),
        )
        updated = self._bump(session, access, now)
        self._record(session, context, action, access.id, updated.revision, user_id=user_id)
        return {
            "workspace_id": access.id,
            "revision": updated.revision,
            "user_id": user_id,
            "removed": True,
        }

    def remove_member(
        self,
        session: Session,
        context: AccessContext,
        workspace_id: str,
        *,
        user_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE
        )
        return self._remove(
            session, context, access, user_id, expected_revision, action="workspace.remove_member"
        )

    def leave(
        self, session: Session, context: AccessContext, workspace_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.EDIT
        )
        return self._remove(
            session,
            context,
            access,
            context.actor.user_id,
            expected_revision,
            action="workspace.leave",
        )

    def archive(
        self, session: Session, context: AccessContext, workspace_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE
        )
        _team_revision(access, expected_revision)
        now = aware(self._clock())
        _revoke_pending(session, workspace_id, now)
        updated = self._bump(session, access, now, state="archived")
        self._record(
            session, context, "workspace.archive", workspace_id, updated.revision, state="archived"
        )
        return workspace_payload(
            require_workspace_access(
                session,
                context,
                workspace_id,
                action=WorkspaceAction.MANAGE_READ,
                allow_archived=True,
            )
        )

    def restore(
        self, session: Session, context: AccessContext, workspace_id: str, *, expected_revision: int
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE, allow_archived=True
        )
        _team_revision(access, expected_revision)
        if access.state == "active":
            return workspace_payload(access)
        updated = self._bump(session, access, aware(self._clock()), state="active")
        self._record(
            session, context, "workspace.restore", workspace_id, updated.revision, state="active"
        )
        return workspace_payload(require_workspace_access(session, context, workspace_id))

    def _invitation_result(
        self, session: Session, invitation_id: str, now: datetime
    ) -> dict[str, Any]:
        session.flush()
        row = session.execute(
            invitation_statement().where(WorkspaceInvitation.id == invitation_id)
        ).one()
        return invitation_payload(row, now=now)

    def invite(
        self,
        session: Session,
        context: AccessContext,
        workspace_id: str,
        *,
        user_id: str,
        role: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE
        )
        _team_revision(access, expected_revision)
        desired = _role(role)
        target = session.scalar(
            select(User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(User.id == user_id, User.is_active.is_(True))
        )
        existing = session.scalar(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == workspace_id,
                WorkspaceMembership.user_id == user_id,
            )
        )
        if target is None or existing is not None:
            raise workspace_error("user_not_available")
        now = aware(self._clock())
        _revoke_pending(session, workspace_id, now, WorkspaceInvitation.user_id == user_id)
        invitation = WorkspaceInvitation(
            workspace_id=workspace_id,
            user_id=user_id,
            invited_by_user_id=context.actor.user_id,
            inviter_auth_revision=context.actor.auth_revision,
            role=desired,
            state="pending",
            revision=1,
            created_at=now,
            expires_at=now + timedelta(days=7),
        )
        session.add(invitation)
        session.flush([invitation])
        updated = self._bump(session, access, now)
        self._record(
            session,
            context,
            "workspace.invite",
            workspace_id,
            updated.revision,
            user_id=user_id,
            invitation_id=invitation.id,
            role=role,
        )
        return self._invitation_result(session, invitation.id, now)

    def revoke_invitation(
        self,
        session: Session,
        context: AccessContext,
        workspace_id: str,
        *,
        invitation_id: str,
        expected_revision: int,
    ) -> dict[str, Any]:
        access = require_workspace_access(
            session, context, workspace_id, action=WorkspaceAction.MANAGE
        )
        _team_revision(access, expected_revision)
        invitation = session.scalars(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.id == invitation_id,
                WorkspaceInvitation.workspace_id == workspace_id,
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if invitation is None:
            raise workspace_error("invitation_not_found")
        now = aware(self._clock())
        if invitation.state != "pending" or aware(invitation.expires_at) <= now:
            raise workspace_error("invitation_unavailable")
        invitation.state, invitation.responded_at = "revoked", now
        invitation.revision += 1
        updated = self._bump(session, access, now)
        self._record(
            session,
            context,
            "workspace.revoke_invitation",
            workspace_id,
            updated.revision,
            invitation_id=invitation_id,
            state="revoked",
        )
        return self._invitation_result(session, invitation_id, now)

    @staticmethod
    def _target_invitation(
        session: Session, context: AccessContext, invitation_id: str
    ) -> WorkspaceInvitation:
        invitation = session.scalars(
            select(WorkspaceInvitation)
            .where(
                WorkspaceInvitation.id == invitation_id,
                WorkspaceInvitation.user_id == context.actor.user_id,
            )
            .execution_options(populate_existing=True)
        ).one_or_none()
        if invitation is None:
            raise workspace_error("invitation_not_found")
        return invitation

    def accept_invitation(
        self, session: Session, context: AccessContext, invitation_id: str
    ) -> dict[str, Any]:
        require_workspace_actor(session, context, enroll=True)
        invitation = self._target_invitation(session, context, invitation_id)
        now = aware(self._clock())
        if invitation.state == "accepted":
            require_workspace_access(session, context, invitation.workspace_id)
            return self._invitation_result(session, invitation_id, now)
        if invitation.state != "pending" or aware(invitation.expires_at) <= now:
            raise workspace_error("invitation_unavailable")
        workspace = session.execute(
            select(Workspace.id, Workspace.revision).where(
                Workspace.id == invitation.workspace_id,
                Workspace.kind == "team",
                Workspace.state == "active",
            )
        ).one_or_none()
        inviter = session.scalar(
            select(User.id)
            .join(UserCredential, UserCredential.user_id == User.id)
            .join(WorkspaceMembership, WorkspaceMembership.user_id == User.id)
            .where(
                User.id == invitation.invited_by_user_id,
                User.is_active.is_(True),
                UserCredential.auth_revision == invitation.inviter_auth_revision,
                WorkspaceMembership.workspace_id == invitation.workspace_id,
                WorkspaceMembership.role.in_(("owner", "admin")),
            )
        )
        if workspace is None or inviter is None:
            raise workspace_error("invitation_unavailable")
        membership = session.scalar(
            select(WorkspaceMembership.user_id).where(
                WorkspaceMembership.workspace_id == invitation.workspace_id,
                WorkspaceMembership.user_id == context.actor.user_id,
            )
        )
        if membership is None:
            session.add(
                WorkspaceMembership(
                    workspace_id=invitation.workspace_id,
                    user_id=context.actor.user_id,
                    role=invitation.role,
                    created_at=now,
                )
            )
            session.flush()
        enroll_workspace_resources(session, context.actor, invitation.workspace_id)
        invitation.state, invitation.responded_at = "accepted", now
        invitation.revision += 1
        revision = workspace.revision + 1
        session.execute(
            update(Workspace)
            .where(Workspace.id == workspace.id)
            .values(revision=revision, updated_at=now)
        )
        self._record(
            session,
            context,
            "workspace.accept_invitation",
            workspace.id,
            revision,
            invitation_id=invitation_id,
            user_id=context.actor.user_id,
            state="accepted",
        )
        return self._invitation_result(session, invitation_id, now)

    def decline_invitation(
        self, session: Session, context: AccessContext, invitation_id: str
    ) -> dict[str, Any]:
        require_workspace_actor(session, context, write=True)
        invitation = self._target_invitation(session, context, invitation_id)
        now = aware(self._clock())
        if invitation.state != "pending" or aware(invitation.expires_at) <= now:
            raise workspace_error("invitation_unavailable")
        invitation.state, invitation.responded_at = "declined", now
        invitation.revision += 1
        revision = session.scalar(
            select(Workspace.revision).where(Workspace.id == invitation.workspace_id)
        )
        self._record(
            session,
            context,
            "workspace.decline_invitation",
            invitation.workspace_id,
            revision or 1,
            invitation_id=invitation_id,
            user_id=context.actor.user_id,
            state="declined",
        )
        return self._invitation_result(session, invitation_id, now)
