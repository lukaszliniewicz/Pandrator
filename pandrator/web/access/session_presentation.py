"""Bulk access metadata for already-authorized session views, without per-row reads."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from ..accounts.models import Workspace, WorkspaceMembership
from ..models import SessionRecord
from ..projects.models import Project, ProjectMembership
from .contracts import AccessContext
from .project_permissions import (
    ACTION_SCOPES,
    PROJECT_ROLE_ACTIONS,
    WORKSPACE_ROLE_ACTIONS,
)


def session_access_payloads(
    session: Session, context: AccessContext, session_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Call only after the same snapshot's policy-scoped list/detail read.

    This projection grants no access and loads no additional project content.
    It is used only in collaboration mode; Solo retains its existing reads.
    """
    if not session_ids:
        return {}
    actor = context.actor
    statement = (
        select(
            SessionRecord.id, Workspace.id.label("workspace_id"),
            Workspace.name, Workspace.kind, Workspace.personal_owner_user_id,
            WorkspaceMembership.role.label("workspace_role"),
            ProjectMembership.role.label("project_role"),
        )
        .join(Project, Project.id == SessionRecord.project_id)
        .join(Workspace, Workspace.id == Project.workspace_id)
        .outerjoin(WorkspaceMembership, and_(
            WorkspaceMembership.workspace_id == Workspace.id,
            WorkspaceMembership.user_id == actor.user_id,
        ))
        .outerjoin(ProjectMembership, and_(
            ProjectMembership.project_id == Project.id,
            ProjectMembership.user_id == actor.user_id,
        ))
        .where(SessionRecord.id.in_(session_ids), Workspace.state == "active")
    )
    result: dict[str, dict[str, Any]] = {}
    for row in session.execute(statement):
        role = row.workspace_role
        if row.kind == "personal" and (
            row.personal_owner_user_id != actor.user_id or role != "owner"
        ):
            role = None
        actions = (
            WORKSPACE_ROLE_ACTIONS.get(role, frozenset())
            if role is not None else PROJECT_ROLE_ACTIONS.get(row.project_role, frozenset())
        )
        result[row.id] = {
            "workspace_id": row.workspace_id,
            # A project-only viewer cannot enumerate the private workspace name.
            "workspace_name": row.name if role is not None else None,
            "workspace_role": ("owner" if row.kind == "personal" and role == "owner" else "manager" if role in {"admin", "owner"} else role),
            "allowed_actions": sorted(action.value for action in actions if ACTION_SCOPES[action] <= actor.scopes),
        }
    return result
