"""Narrow recovery for active or archived teams without an active Manager."""

from __future__ import annotations

from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..access.contracts import AccessContext, AccessDenied, WorkspaceNotFound
from ..model_base import utcnow
from .actor_authorization import require_actor_in_session
from .execution_actor import UserActor
from .models import User, Workspace, WorkspaceMembership
from .workspace_contracts import WorkspaceAuditSink, workspace_error

_RECOVERY_READ_SCOPES = frozenset({"app.read", "app.admin"})
_RECOVERY_WRITE_SCOPES = _RECOVERY_READ_SCOPES | {"app.write"}
_VALID_WORKSPACE_STATES = ("active", "archived")
_MANAGER_ROLES = ("owner", "admin")
_MAX_CANDIDATES_PER_WORKSPACE = 1000


def _active_manager_statement(workspace_id: Any):
    return (
        select(WorkspaceMembership.user_id)
        .join(User, User.id == WorkspaceMembership.user_id)
        .where(
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.role.in_(_MANAGER_ROLES),
            User.is_active.is_(True),
        )
    )


def _require_recovery_actor(session: Session, context: AccessContext, *, write: bool) -> None:
    if type(context) is not AccessContext or type(context.actor) is not UserActor:
        raise AccessDenied()
    try:
        with session.no_autoflush:
            require_actor_in_session(
                session,
                context.actor,
                required_scopes=(_RECOVERY_WRITE_SCOPES if write else _RECOVERY_READ_SCOPES),
                require_admin=True,
            )
    except PermissionError:
        raise AccessDenied() from None


def _workspace_row(session: Session, workspace_id: str):
    if type(workspace_id) is not str or not workspace_id:
        raise WorkspaceNotFound()
    row = session.execute(
        select(
            Workspace.id,
            Workspace.name,
            Workspace.kind,
            Workspace.state,
            Workspace.revision,
        ).where(Workspace.id == workspace_id)
    ).one_or_none()
    if row is None or row.kind not in {"personal", "team"}:
        raise WorkspaceNotFound()
    if row.state not in _VALID_WORKSPACE_STATES:
        raise WorkspaceNotFound()
    return row


class WorkspaceManagerRecovery:
    """List orphaned-team recovery metadata and promote an existing Member."""

    def __init__(self, *, audit: WorkspaceAuditSink) -> None:
        self._audit = audit

    def list_orphaned(
        self,
        session: Session,
        context: AccessContext,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be an integer from 1 to 100.")
        if type(offset) is not int or not 0 <= offset <= 100_000:
            raise ValueError("offset must be an integer from 0 to 100000.")

        _require_recovery_actor(session, context, write=False)
        orphaned = (
            select(
                Workspace.id,
                Workspace.name,
                Workspace.state,
                Workspace.revision,
            )
            .where(
                Workspace.kind == "team",
                Workspace.state.in_(_VALID_WORKSPACE_STATES),
                ~_active_manager_statement(Workspace.id).exists(),
            )
            .order_by(Workspace.name, Workspace.id)
            .limit(limit + 1)
            .offset(offset)
        )
        with session.no_autoflush:
            rows = session.execute(orphaned).all()
            page = rows[:limit]
            workspace_ids = [row.id for row in page]
            candidates_by_workspace: dict[str, list[dict[str, str]]] = {
                workspace_id: [] for workspace_id in workspace_ids
            }
            if workspace_ids:
                ranked_candidates = (
                    select(
                        WorkspaceMembership.workspace_id.label("workspace_id"),
                        User.id.label("user_id"),
                        User.display_name.label("display_name"),
                        func.row_number()
                        .over(
                            partition_by=WorkspaceMembership.workspace_id,
                            order_by=(User.display_name, User.id),
                        )
                        .label("candidate_rank"),
                    )
                    .join(User, User.id == WorkspaceMembership.user_id)
                    .where(
                        WorkspaceMembership.workspace_id.in_(workspace_ids),
                        WorkspaceMembership.role == "member",
                        User.is_active.is_(True),
                    )
                    .subquery()
                )
                candidate_rows = session.execute(
                    select(
                        ranked_candidates.c.workspace_id,
                        ranked_candidates.c.user_id,
                        ranked_candidates.c.display_name,
                        ranked_candidates.c.candidate_rank,
                    )
                    .where(ranked_candidates.c.candidate_rank <= _MAX_CANDIDATES_PER_WORKSPACE + 1)
                    .order_by(
                        ranked_candidates.c.workspace_id,
                        ranked_candidates.c.candidate_rank,
                    )
                ).all()
                for candidate in candidate_rows:
                    if candidate.candidate_rank > _MAX_CANDIDATES_PER_WORKSPACE:
                        raise ValueError(
                            "A workspace exceeds the Manager recovery candidate bound."
                        )
                    candidates_by_workspace[candidate.workspace_id].append(
                        {
                            "user_id": candidate.user_id,
                            "display_name": candidate.display_name,
                        }
                    )

        return {
            "items": [
                {
                    "id": row.id,
                    "name": row.name,
                    "state": row.state,
                    "revision": row.revision,
                    "candidates": candidates_by_workspace[row.id],
                }
                for row in page
            ],
            "has_more": len(rows) > limit,
            "offset": offset,
            "limit": limit,
        }

    def promote(
        self,
        session: Session,
        context: AccessContext,
        workspace_id: str,
        user_id: str,
        *,
        expected_revision: int,
    ) -> dict[str, Any]:
        _require_recovery_actor(session, context, write=True)
        if type(expected_revision) is not int or expected_revision < 1:
            raise workspace_error("invalid_revision")

        with session.no_autoflush:
            workspace = _workspace_row(session, workspace_id)
            if workspace.kind == "personal":
                raise workspace_error("personal_workspace_protected")

            if session.scalar(_active_manager_statement(workspace_id).limit(1)) is not None:
                raise workspace_error("workspace_manager_recovery_not_needed")

            if type(user_id) is not str or not user_id:
                raise workspace_error("workspace_manager_recovery_target")

            target = session.execute(
                select(WorkspaceMembership.role, User.is_active)
                .join(User, User.id == WorkspaceMembership.user_id)
                .where(
                    WorkspaceMembership.workspace_id == workspace_id,
                    WorkspaceMembership.user_id == user_id,
                )
            ).one_or_none()
            if target is None or target.role != "member" or not target.is_active:
                raise workspace_error("workspace_manager_recovery_target")

            if workspace.revision != expected_revision:
                raise workspace_error("workspace_revision_conflict")

            next_revision = expected_revision + 1
            updated_at = utcnow()
            result = cast(
                CursorResult[Any],
                session.execute(
                    update(Workspace)
                    .where(
                        Workspace.id == workspace_id,
                        Workspace.kind == "team",
                        Workspace.state.in_(_VALID_WORKSPACE_STATES),
                        Workspace.revision == expected_revision,
                    )
                    .values(revision=next_revision, updated_at=updated_at)
                ),
            )
            if result.rowcount != 1:
                raise workspace_error("workspace_revision_conflict")

            membership_update = cast(
                CursorResult[Any],
                session.execute(
                    update(WorkspaceMembership)
                    .where(
                        WorkspaceMembership.workspace_id == workspace_id,
                        WorkspaceMembership.user_id == user_id,
                        WorkspaceMembership.role == "member",
                    )
                    .values(role="admin")
                ),
            )
            if membership_update.rowcount != 1:
                raise workspace_error("workspace_manager_recovery_target")

            self._audit(
                session,
                context,
                action="workspace.manager.recover",
                workspace_id=workspace_id,
                metadata={
                    "user_id": user_id,
                    "role": "manager",
                    "revision": next_revision,
                },
            )

        return {
            "id": workspace.id,
            "name": workspace.name,
            "state": workspace.state,
            "revision": next_revision,
            "recovered_user_id": user_id,
        }


__all__ = ["WorkspaceManagerRecovery"]
