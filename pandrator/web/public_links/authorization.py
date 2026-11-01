"""Fresh leaf checks; deliberately independent of the resource-policy graph."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..accounts.execution_actor import UserActor
from ..model_base import utcnow
from .contracts import LINK_SCOPES, SETTING_KEY
from .models import ProjectLink, ProjectLinkSession


def aware(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _denied() -> PermissionError:
    return PermissionError("The project link is not currently authorized.")


def require_project_link_governance(
    session: Session,
    user_id: str,
    auth_revision: int,
    project_id: str,
) -> tuple[str, str]:
    """Prove active issuer governance and an active matching project source."""
    row = session.execute(
        text(
            "SELECT p.workspace_id, p.source_session_id FROM projects p "
            "JOIN workspaces w ON w.id=p.workspace_id "
            "JOIN workspace_memberships m ON m.workspace_id=w.id AND m.user_id=:user "
            "JOIN users u ON u.id=m.user_id "
            "JOIN user_credentials c ON c.user_id=u.id "
            "JOIN sessions s ON s.id=p.source_session_id AND s.project_id=p.id "
            "WHERE p.id=:project AND w.state='active' AND u.is_active=1 "
            "AND c.auth_revision=:revision AND m.role IN ('owner','admin') "
            "AND (w.kind='team' OR (w.kind='personal' AND w.personal_owner_user_id=u.id "
            "AND m.role='owner')) AND s.trashed_at IS NULL"
        ),
        {"user": user_id, "revision": auth_revision, "project": project_id},
    ).one_or_none()
    if row is None:
        raise _denied()
    return row.workspace_id, row.source_session_id


def require_link_in_session(session: Session, link: ProjectLink) -> None:
    """Validate persisted roots and issuer authority in the caller's transaction."""
    setting = session.execute(
        text("SELECT value_json FROM app_settings WHERE key=:key"), {"key": SETTING_KEY}
    ).scalar_one_or_none()
    # SQLite's text JSON result is a string; JSON decoding stays bounded to this row.
    import json

    if isinstance(setting, str):
        try:
            setting = json.loads(setting)
        except ValueError:
            raise _denied() from None
    if (
        not isinstance(setting, dict)
        or setting.get("enabled") is not True
        or link.revoked_at is not None
        or aware(link.expires_at) <= utcnow()
        or link.mode not in LINK_SCOPES
    ):
        raise _denied()
    workspace_id, root_session_id = require_project_link_governance(
        session,
        link.issuer_user_id,
        link.issuer_auth_revision,
        link.project_id,
    )
    if workspace_id != link.workspace_id or root_session_id != link.root_session_id:
        raise _denied()
    if link.mode == "review_translation":
        target = session.execute(
            text(
                "SELECT a.id FROM artifacts a JOIN sessions s ON s.id=a.session_id "
                "JOIN document_revisions r ON r.id=:revision "
                "JOIN documents d ON d.id=r.document_id "
                "WHERE a.id=:artifact AND a.project_id=:project AND a.workspace_id=:workspace "
                "AND a.session_id=:target AND s.project_id=:project AND s.trashed_at IS NULL "
                "AND a.state!='deleted' AND a.role='translation' "
                "AND a.content_hash=:hash "
                "AND r.revision_number=:number AND d.session_id=s.id AND d.stage='translation'"
            ),
            {
                "revision": link.target_revision_id,
                "artifact": link.target_artifact_id,
                "project": link.project_id,
                "workspace": link.workspace_id,
                "target": link.target_session_id,
                "hash": link.target_hash,
                "number": link.target_revision_number,
            },
        ).first()
        if target is None:
            raise _denied()


def require_project_link_actor_in_session(session: Session, actor: UserActor) -> None:
    if (
        actor.principal_kind != "project_link"
        or actor.browser_session_id is None
        or actor.token_id is not None
        or actor.client_id is not None
    ):
        raise _denied()
    row = session.execute(
        select(ProjectLink, ProjectLinkSession)
        .join(ProjectLinkSession, ProjectLinkSession.link_id == ProjectLink.id)
        .where(ProjectLinkSession.browser_session_id == actor.browser_session_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    if row is None:
        raise _denied()
    link, _binding = row
    if (
        link.issuer_user_id != actor.user_id
        or link.issuer_auth_revision != actor.auth_revision
        or actor.subject != f"project-link:{link.id}"
        or actor.scopes != LINK_SCOPES.get(link.mode)
    ):
        raise _denied()
    # Fail closed if persisted delegation was expanded or detached from this project.
    grants = (
        session.execute(
            text("SELECT project_id FROM browser_auth_project_grants WHERE browser_session_id=:id"),
            {"id": actor.browser_session_id},
        )
        .scalars()
        .all()
    )
    workspaces = session.execute(
        text("SELECT workspace_id FROM browser_auth_workspace_grants WHERE browser_session_id=:id"),
        {"id": actor.browser_session_id},
    ).first()
    if grants != [link.project_id] or workspaces is not None:
        raise _denied()
    require_link_in_session(session, link)
