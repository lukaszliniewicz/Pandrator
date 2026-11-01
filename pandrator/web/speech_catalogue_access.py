"""Transaction-local speech scope and captured TTS revision fences; no HTTP state."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, ProjectAction, WorkspaceNotFound
from .access.session_policy import require_session_access
from .access.workspace_policy import WorkspaceAccess, WorkspaceAction, require_workspace_access
from .accounts.actor_authorization import require_actor_in_session
from .accounts.administration import AdministratorRequired
from .accounts.identity import initial_personal_workspace
from .accounts.installation_mode import installation_mode_in_session
from .credential_identity import credential_namespace
from .database import Database
from .models import WorkspaceSetting


class SpeechScopeInvalid(ValueError):
    pass


class CatalogueChanged(ValueError):
    def __init__(self):
        super().__init__("The speech catalogue changed. Refresh it before trying again.")


def scope_identifier(value: str | None) -> str | None:
    if value is not None and (
        type(value) is not str or not value or value != value.strip() or len(value) > 80
    ):
        raise SpeechScopeInvalid("Choose a valid speech workspace or session scope.")
    return value


@dataclass(frozen=True, slots=True)
class SpeechAccess:
    workspace: WorkspaceAccess
    configuration: bool
    legacy_manager: bool
    can_manage: bool
    can_refresh: bool

    def metadata(self):
        return {
            "workspace_id": self.workspace.id,
            "can_manage": self.can_manage,
            "can_refresh": self.can_refresh,
        }


def require_speech_access(
    session: Session,
    context: AccessContext,
    workspace_id: str | None = None,
    *,
    session_id: str | None = None,
    action: WorkspaceAction = WorkspaceAction.READ,
    refresh: bool = False,
    admin: bool = False,
) -> SpeechAccess:
    workspace_id, session_id = scope_identifier(workspace_id), scope_identifier(session_id)
    if session_id is not None:
        root = require_session_access(session, context, session_id, ProjectAction.READ)
        if workspace_id is not None and workspace_id != root.workspace_id:
            raise WorkspaceNotFound()
        workspace_id = root.workspace_id
    workspace = require_workspace_access(session, context, workspace_id, action=action)
    required = {"app.read"}
    if action is WorkspaceAction.MANAGE:
        required.add("app.write")
    if refresh:
        required.update(("app.write", "app.run", "app.credentials.write"))
    if admin:
        required.add("app.admin")
    if refresh or admin:
        try:
            require_actor_in_session(
                session, context.actor, required_scopes=required, require_admin=admin
            )
        except PermissionError:
            if admin:
                raise AdministratorRequired() from None
            raise
    legacy = False
    if (
        workspace.kind == "personal"
        and "app.admin" in context.actor.scopes
        and not installation_mode_in_session(session).enabled
    ):
        if workspace.id == initial_personal_workspace(session).id:
            try:
                require_actor_in_session(
                    session,
                    context.actor,
                    required_scopes=("app.admin", "app.read"),
                    require_admin=True,
                )
                legacy = True
            except PermissionError:
                pass
    can_manage = (
        WorkspaceAction.MANAGE in workspace.allowed_actions
        and "app.credentials.write" in context.actor.scopes
    )
    return SpeechAccess(
        workspace,
        WorkspaceAction.MANAGE_READ in workspace.allowed_actions,
        legacy,
        can_manage,
        can_manage and "app.run" in context.actor.scopes,
    )


def tts_revision_headers(session: Session, workspace_id: str) -> tuple[int, int]:
    rows = {
        key: revision
        for key, revision in session.execute(
            select(WorkspaceSetting.key, WorkspaceSetting.revision).where(
                WorkspaceSetting.workspace_id == workspace_id,
                WorkspaceSetting.key.in_(("services.tts", "defaults.tts")),
            )
        ).all()
    }
    return rows.get("services.tts", 0), rows.get("defaults.tts", 0)


@dataclass(frozen=True, slots=True)
class SpeechFence:
    database: Database
    context: AccessContext
    workspace_id: str
    action: WorkspaceAction
    refresh: bool = False
    admin: bool = False
    revisions: tuple[int, int] | None = None
    expected_host_credentials: bool | None = None

    def __call__(self) -> None:
        with self.database.snapshot_session() as session:
            require_speech_access(
                session,
                self.context,
                self.workspace_id,
                action=self.action,
                refresh=self.refresh,
                admin=self.admin,
            )
            if (
                self.expected_host_credentials is not None
                and credential_namespace(session, self.workspace_id).host_credentials_allowed
                != self.expected_host_credentials
            ):
                raise CatalogueChanged()
            if (
                self.revisions is not None
                and tts_revision_headers(session, self.workspace_id) != self.revisions
            ):
                raise CatalogueChanged()
