"""Persisted credential resource ceilings, separate from live user roles."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, cast

from sqlalchemy import and_, delete, exists, false, insert, or_, select, true
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..access.project_visibility import (
    creatable_workspace_ids_for_user,
    project_ids_for_user,
)
from ..projects.models import Project
from .actor_authorization import require_actor_in_session
from .execution_actor import _MANAGER_BROWSER_CLIENT_IDS, UserActor
from .models import Workspace, WorkspaceMembership
from .resource_grant_models import (
    ApiTokenProjectGrant,
    ApiTokenWorkspaceGrant,
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
    AutomationCodeProjectGrant,
    AutomationCodeWorkspaceGrant,
    BrowserAuthProjectGrant,
    BrowserAuthWorkspaceGrant,
)


class CredentialResourcesDenied(PermissionError):
    def __init__(self) -> None:
        super().__init__("The requested credential resources are not available.")


def _identifier(value: object) -> None:
    if type(value) is not str or not value or value != value.strip() or len(value) > 80:
        raise ValueError("Credential resource identifiers must be nonempty trimmed strings.")


@dataclass(frozen=True, slots=True)
class CredentialResources:
    workspace_ids: frozenset[str] = frozenset()
    project_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.workspace_ids) is not frozenset or type(self.project_ids) is not frozenset:
            raise ValueError("Credential resources require explicit frozen identifier sets.")
        if len(self.workspace_ids) + len(self.project_ids) > 1000:
            raise ValueError("A credential resource selection cannot exceed 1000 identifiers.")
        for identifier in (*self.workspace_ids, *self.project_ids):
            _identifier(identifier)


class CredentialOwner(str, Enum):
    TOKEN = "token"
    CLIENT = "client"
    BROWSER = "browser"
    CODE = "code"


_MODELS: dict[CredentialOwner, tuple[Any, Any, str]] = {
    CredentialOwner.TOKEN: (ApiTokenWorkspaceGrant, ApiTokenProjectGrant, "token_id"),
    CredentialOwner.CLIENT: (
        AutomationClientWorkspaceGrant,
        AutomationClientProjectGrant,
        "client_id",
    ),
    CredentialOwner.BROWSER: (
        BrowserAuthWorkspaceGrant,
        BrowserAuthProjectGrant,
        "browser_session_id",
    ),
    CredentialOwner.CODE: (
        AutomationCodeWorkspaceGrant,
        AutomationCodeProjectGrant,
        "code_grant_id",
    ),
}


def _models(owner: CredentialOwner, owner_id: str) -> tuple[Any, Any, str]:
    if type(owner) is not CredentialOwner:
        raise TypeError("A credential owner kind is required.")
    _identifier(owner_id)
    return _MODELS[owner]


def _selection(value: CredentialResources) -> CredentialResources:
    if type(value) is not CredentialResources:
        raise TypeError("A typed credential resource selection is required.")
    return value


def default_personal_resources(session: Session, user_id: str) -> CredentialResources:
    """Find only this user's proved active personal scope, without repairing it."""
    _identifier(user_id)
    with session.no_autoflush:
        workspace_id = session.scalars(
            select(Workspace.id)
            .join(
                WorkspaceMembership,
                and_(
                    WorkspaceMembership.workspace_id == Workspace.id,
                    WorkspaceMembership.user_id == user_id,
                    WorkspaceMembership.role == "owner",
                ),
            )
            .where(
                Workspace.kind == "personal",
                Workspace.state == "active",
                Workspace.personal_owner_user_id == user_id,
            )
        ).one_or_none()
    return CredentialResources(
        workspace_ids=frozenset({workspace_id}) if workspace_id is not None else frozenset()
    )


def resources_for_owner(
    session: Session,
    owner: CredentialOwner,
    owner_id: str,
) -> CredentialResources:
    """Read a snapshot from SQL; missing rows mean no grant, never a default."""
    workspace_model, project_model, owner_column = _models(owner, owner_id)
    with session.no_autoflush:
        workspaces = frozenset(
            session.scalars(
                select(workspace_model.workspace_id).where(
                    getattr(workspace_model, owner_column) == owner_id
                )
            ).all()
        )
        projects = frozenset(
            session.scalars(
                select(project_model.project_id).where(
                    getattr(project_model, owner_column) == owner_id
                )
            ).all()
        )
    return CredentialResources(workspace_ids=workspaces, project_ids=projects)


def record_resources(
    session: Session,
    owner: CredentialOwner,
    owner_id: str,
    resources: CredentialResources,
) -> None:
    """Insert grants while the authorized caller mints a new credential owner."""
    selected = _selection(resources)
    workspace_model, project_model, owner_column = _models(owner, owner_id)
    with session.no_autoflush:
        if selected.workspace_ids:
            session.execute(
                insert(workspace_model.__table__),
                [
                    {owner_column: owner_id, "workspace_id": value}
                    for value in sorted(selected.workspace_ids)
                ],
            )
        if selected.project_ids:
            session.execute(
                insert(project_model.__table__),
                [
                    {owner_column: owner_id, "project_id": value}
                    for value in sorted(selected.project_ids)
                ],
            )


def replace_client_resources(
    session: Session,
    client_id: str,
    resources: CredentialResources,
) -> None:
    """Replace an approval only after its command has fenced old credentials."""
    _selection(resources)
    workspace_model, project_model, owner_column = _models(CredentialOwner.CLIENT, client_id)
    with session.no_autoflush:
        for model in (workspace_model, project_model):
            session.execute(
                delete(model.__table__).where(getattr(model, owner_column) == client_id)
            )
    record_resources(session, CredentialOwner.CLIENT, client_id, resources)


def validate_resources_for_user(
    session: Session,
    user_id: str,
    resources: CredentialResources,
) -> CredentialResources:
    """Validate targets against live roles; caller separately validates account."""
    _identifier(user_id)
    selected = _selection(resources)
    with session.no_autoflush:
        if selected.workspace_ids:
            permitted = frozenset(
                session.scalars(
                    creatable_workspace_ids_for_user(user_id).where(
                        Workspace.id.in_(selected.workspace_ids)
                    )
                ).all()
            )
            if permitted != selected.workspace_ids:
                raise CredentialResourcesDenied()
        if selected.project_ids:
            permitted = frozenset(
                session.scalars(
                    project_ids_for_user(user_id).where(Project.id.in_(selected.project_ids))
                ).all()
            )
            if permitted != selected.project_ids:
                raise CredentialResourcesDenied()
    return selected


def require_resources_within(
    session: Session,
    resources: CredentialResources,
    ceiling: CredentialResources,
) -> None:
    """Require a resource subset; role and transport checks remain independent."""
    selected, approved = _selection(resources), _selection(ceiling)
    if not selected.workspace_ids <= approved.workspace_ids:
        raise CredentialResourcesDenied()
    remaining = selected.project_ids - approved.project_ids
    if not remaining:
        return
    with session.no_autoflush:
        covered = frozenset(
            session.scalars(
                select(Project.id).where(
                    Project.id.in_(remaining),
                    Project.workspace_id.in_(approved.workspace_ids),
                )
            ).all()
        )
    if covered != remaining:
        raise CredentialResourcesDenied()


def is_interactive_actor(actor: UserActor) -> bool:
    return (
        type(actor) is UserActor
        and actor.token_id is None
        and actor.browser_session_id is not None
        and actor.principal_kind == "owner_session"
        and actor.client_id is None
    )


def _actor_owners(actor: UserActor) -> tuple[tuple[CredentialOwner, str], ...]:
    if type(actor) is not UserActor:
        raise TypeError("A user actor is required.")
    if is_interactive_actor(actor):
        return ()
    if actor.token_id is not None:
        owners = [(CredentialOwner.TOKEN, actor.token_id)]
    elif actor.browser_session_id is not None:
        owners = [(CredentialOwner.BROWSER, actor.browser_session_id)]
    else:
        raise CredentialResourcesDenied()
    if actor.client_id is not None and not (
        actor.browser_session_id is not None and actor.client_id in _MANAGER_BROWSER_CLIENT_IDS
    ):
        owners.append((CredentialOwner.CLIENT, actor.client_id))
    return tuple(owners)


def require_delegation_resources(
    session: Session,
    actor: UserActor,
    resources: CredentialResources,
) -> CredentialResources:
    """An approving transport cannot delegate beyond its own current ceiling."""
    require_actor_in_session(session, actor, required_scopes=())
    selected = validate_resources_for_user(session, actor.user_id, resources)
    for owner, owner_id in _actor_owners(actor):
        require_resources_within(session, selected, resources_for_owner(session, owner, owner_id))
    return selected


def credential_project_clause(actor: UserActor):
    """Correlate to Project after strict actor validation in this same unit."""
    if is_interactive_actor(actor):
        return true()
    clauses = []
    for owner, owner_id in _actor_owners(actor):
        workspace_model, project_model, owner_column = _models(owner, owner_id)
        clauses.append(
            or_(
                exists(
                    select(workspace_model.workspace_id).where(
                        getattr(workspace_model, owner_column) == owner_id,
                        workspace_model.workspace_id == Project.workspace_id,
                    )
                ),
                exists(
                    select(project_model.project_id).where(
                        getattr(project_model, owner_column) == owner_id,
                        project_model.project_id == Project.id,
                    )
                ),
            )
        )
    return and_(*clauses) if clauses else false()


def credential_workspace_clause(actor: UserActor):
    """Correlate to Workspace; project grants never confer workspace authority."""
    if is_interactive_actor(actor):
        return true()
    clauses = []
    for owner, owner_id in _actor_owners(actor):
        workspace_model, _project_model, owner_column = _models(owner, owner_id)
        clauses.append(
            exists(
                select(workspace_model.workspace_id).where(
                    getattr(workspace_model, owner_column) == owner_id,
                    workspace_model.workspace_id == Workspace.id,
                )
            )
        )
    return and_(*clauses) if clauses else false()


__all__ = [
    "CredentialOwner",
    "CredentialResources",
    "CredentialResourcesDenied",
    "credential_project_clause",
    "credential_workspace_clause",
    "default_personal_resources",
    "is_interactive_actor",
    "record_resources",
    "replace_client_resources",
    "require_delegation_resources",
    "require_resources_within",
    "resources_for_owner",
    "validate_resources_for_user",
]


def enroll_workspace_resources(session: Session, actor: UserActor, workspace_id: str) -> None:
    """Append only this live transport's grants after explicit workspace enrollment."""
    _identifier(workspace_id)
    require_actor_in_session(
        session, actor, required_scopes=("app.read", "app.write", "app.workspaces.enroll")
    )
    if actor.principal_kind not in {"owner_session", "api_token", "automation_client"}:
        raise CredentialResourcesDenied()
    with session.no_autoflush:
        membership = session.scalar(
            creatable_workspace_ids_for_user(actor.user_id).where(Workspace.id == workspace_id)
        )
        if membership is None:
            raise CredentialResourcesDenied()
        for owner, owner_id in _actor_owners(actor):
            workspace_model, _project_model, owner_column = _models(owner, owner_id)
            existing = session.scalar(select(workspace_model.workspace_id).where(
                getattr(workspace_model, owner_column) == owner_id,
                workspace_model.workspace_id == workspace_id,
            ))
            if existing is None:
                session.execute(insert(workspace_model.__table__).values(
                    **{owner_column: owner_id, "workspace_id": workspace_id}
                ))


def remove_user_workspace_resources(session: Session, user_id: str, workspace_id: str) -> None:
    """Remove one user's current and stale grants for one workspace, in bulk."""
    from ..models import ApiToken, AutomationClient, AutomationEnrollmentGrant
    from .auth_models import BrowserAuthSession

    _identifier(user_id)
    _identifier(workspace_id)
    owner_ids = {
        CredentialOwner.TOKEN: select(ApiToken.id).where(ApiToken.user_id == user_id),
        CredentialOwner.CLIENT: select(AutomationClient.id).where(AutomationClient.user_id == user_id),
        CredentialOwner.BROWSER: select(BrowserAuthSession.id).where(BrowserAuthSession.user_id == user_id),
        CredentialOwner.CODE: (
            select(AutomationEnrollmentGrant.id)
            .join(AutomationClient, AutomationClient.id == AutomationEnrollmentGrant.client_id)
            .where(AutomationClient.user_id == user_id)
        ),
    }
    project_ids = select(Project.id).where(Project.workspace_id == workspace_id)
    with session.no_autoflush:
        for owner, identifiers in owner_ids.items():
            workspace_model, project_model, owner_column = _MODELS[owner]
            session.execute(delete(workspace_model.__table__).where(
                getattr(workspace_model, owner_column).in_(identifiers),
                workspace_model.workspace_id == workspace_id,
            ))
            session.execute(delete(project_model.__table__).where(
                getattr(project_model, owner_column).in_(identifiers),
                project_model.project_id.in_(project_ids),
            ))


def remove_user_project_resources(session: Session, user_id: str, project_id: str) -> bool:
    """Remove all current and stale project grants owned by one user's credentials."""
    from ..models import ApiToken, AutomationClient, AutomationEnrollmentGrant
    from .auth_models import BrowserAuthSession

    _identifier(user_id)
    _identifier(project_id)
    owner_ids = {
        CredentialOwner.TOKEN: select(ApiToken.id).where(ApiToken.user_id == user_id),
        CredentialOwner.CLIENT: select(AutomationClient.id).where(
            AutomationClient.user_id == user_id
        ),
        CredentialOwner.BROWSER: select(BrowserAuthSession.id).where(
            BrowserAuthSession.user_id == user_id
        ),
        CredentialOwner.CODE: (
            select(AutomationEnrollmentGrant.id)
            .join(AutomationClient, AutomationClient.id == AutomationEnrollmentGrant.client_id)
            .where(AutomationClient.user_id == user_id)
        ),
    }
    removed = False
    with session.no_autoflush:
        for owner, identifiers in owner_ids.items():
            _workspace_model, project_model, owner_column = _MODELS[owner]
            result = cast(
                CursorResult[Any],
                session.execute(
                    delete(project_model.__table__).where(
                        getattr(project_model, owner_column).in_(identifiers),
                        project_model.project_id == project_id,
                    )
                ),
            )
            removed = removed or (result.rowcount is not None and result.rowcount > 0)
    return removed


__all__ += [
    "enroll_workspace_resources",
    "remove_user_project_resources",
    "remove_user_workspace_resources",
]
