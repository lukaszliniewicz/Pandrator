"""Typed transaction-local workspace and installation settings authority."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from sqlalchemy.orm import Session

from .access.contracts import AccessContext, ProjectAction, WorkspaceNotFound
from .access.session_policy import require_session_access
from .access.workspace_policy import WorkspaceAction, require_workspace_access
from .accounts.actor_authorization import require_actor_in_session
from .accounts.administration import AdministratorRequired
from .accounts.identity import initial_personal_workspace
from .settings_identity import HOST_SETTING_KEYS

SERVICE_SETTING_KEYS = frozenset({"services.tts", "services.stt"})


class SettingsScopeInvalid(ValueError):
    pass


def _identifier(value: str | None) -> str | None:
    if value is not None and (
        type(value) is not str or not value or value != value.strip() or len(value) > 80
    ):
        raise SettingsScopeInvalid("Choose a valid settings workspace or session scope.")
    return value


@dataclass(frozen=True, slots=True)
class SettingsAccess:
    workspace_id: str
    setting_scope: Literal["workspace", "installation"]
    can_manage: bool

    def metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "setting_scope": self.setting_scope,
            "can_manage": self.can_manage,
        }
        if self.setting_scope == "workspace":
            result["workspace_id"] = self.workspace_id
        return result


def require_settings_access(
    session: Session,
    context: AccessContext,
    setting_key: str,
    *,
    workspace_id: str | None = None,
    session_id: str | None = None,
    write: bool = False,
) -> SettingsAccess:
    workspace_id, session_id = _identifier(workspace_id), _identifier(session_id)
    if setting_key in HOST_SETTING_KEYS:
        if workspace_id is not None or session_id is not None:
            raise SettingsScopeInvalid(
                "Installation settings do not accept workspace or session scope."
            )
        required = ["app.admin", "app.read"]
        if write:
            required.append("app.write")
        try:
            require_actor_in_session(
                session, context.actor, required_scopes=required, require_admin=True
            )
        except PermissionError:
            raise AdministratorRequired() from None
        return SettingsAccess(
            initial_personal_workspace(session).id,
            "installation",
            {"app.admin", "app.read", "app.write"} <= context.actor.scopes,
        )
    if session_id is not None:
        root = require_session_access(session, context, session_id, ProjectAction.READ)
        if workspace_id is not None and workspace_id != root.workspace_id:
            raise WorkspaceNotFound()
        workspace_id = root.workspace_id
    action = (
        WorkspaceAction.MANAGE
        if write
        else WorkspaceAction.READ
        if setting_key.startswith("defaults.")
        else WorkspaceAction.MANAGE_READ
    )
    access = require_workspace_access(session, context, workspace_id, action=action)
    if write and setting_key in SERVICE_SETTING_KEYS:
        require_actor_in_session(
            session,
            context.actor,
            required_scopes=("app.read", "app.write", "app.credentials.write"),
        )
    can_manage = WorkspaceAction.MANAGE in access.allowed_actions
    if setting_key in SERVICE_SETTING_KEYS:
        can_manage = can_manage and "app.credentials.write" in context.actor.scopes
    return SettingsAccess(access.id, "workspace", can_manage)
