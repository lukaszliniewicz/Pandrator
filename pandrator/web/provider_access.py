"""Current workspace authority and minimal approved provider catalogues."""

from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, ProjectAction, WorkspaceNotFound
from .access.session_policy import require_session_access
from .access.workspace_policy import WorkspaceAction, require_workspace_access
from .accounts.actor_authorization import require_actor_in_session
from .models import Provider, ProviderModel


class ProviderScopeInvalid(ValueError):
    def __init__(self):
        super().__init__("Choose a valid provider workspace or session scope.")


def _identifier(value: str | None) -> str | None:
    if value is not None and (not value or value != value.strip() or len(value) > 80):
        raise ProviderScopeInvalid()
    return value


def require_provider_workspace(
    session: Session,
    context: AccessContext,
    workspace_id: str | None = None,
    *,
    action: WorkspaceAction = WorkspaceAction.READ,
    session_id: str | None = None,
    run: bool = False,
):
    workspace_id = _identifier(workspace_id)
    session_id = _identifier(session_id)
    if session_id is not None:
        root = require_session_access(session, context, session_id, ProjectAction.READ)
        if workspace_id is not None and workspace_id != root.workspace_id:
            raise WorkspaceNotFound()
        workspace_id = root.workspace_id
    access = require_workspace_access(session, context, workspace_id, action=action)
    if action is WorkspaceAction.MANAGE or run:
        # Preserve the route's existing credential effect scope in the same
        # writer/fence; a cached request principal cannot prove it survived.
        required = ["app.read", "app.write", "app.credentials.write"]
        if run:
            required.append("app.run")
        require_actor_in_session(session, context.actor, required_scopes=required)
    return access


def member_provider_catalogue(session: Session, workspace_id: str):
    rows = session.execute(
        select(
            Provider.id,
            Provider.kind,
            Provider.provider_key,
            Provider.label,
            Provider.enabled,
            Provider.revision,
            Provider.options_json["is_custom"].as_boolean().label("is_custom"),
        )
        .where(Provider.workspace_id == workspace_id, Provider.enabled.is_(True))
        .order_by(Provider.label)
    )
    return [
        {
            "id": row.id,
            "kind": row.kind,
            "provider_key": row.provider_key,
            "label": row.label,
            "enabled": row.enabled,
            "revision": row.revision,
            "options_json": {"is_custom": bool(row.is_custom)},
        }
        for row in rows
    ]


def member_model_catalogue(session: Session, provider_id: str, workspace_id: str):
    rows = session.execute(
        select(
            ProviderModel.id,
            ProviderModel.provider_id,
            ProviderModel.model_id,
            ProviderModel.is_active,
            ProviderModel.is_default,
            ProviderModel.default_temperature,
            ProviderModel.default_reasoning_effort,
            ProviderModel.input_cost_per_million,
            ProviderModel.cached_input_cost_per_million,
            ProviderModel.output_cost_per_million,
            ProviderModel.context_window_tokens,
            ProviderModel.max_output_tokens,
            ProviderModel.input_modalities_json,
            ProviderModel.output_modalities_json,
            ProviderModel.revision,
        )
        .join(Provider, Provider.id == ProviderModel.provider_id)
        .where(
            Provider.id == provider_id,
            Provider.workspace_id == workspace_id,
            Provider.enabled.is_(True),
            or_(ProviderModel.is_active.is_(True), ProviderModel.is_default.is_(True)),
        )
        .order_by(ProviderModel.model_id)
    )
    result = []
    for row in rows:
        item = dict(row._mapping)
        item["input_modalities"] = list(item.pop("input_modalities_json") or ["text"])
        item["output_modalities"] = list(item.pop("output_modalities_json") or ["text"])
        item["supports_audio_input"] = "audio" in item["input_modalities"]
        item["options_json"] = {}
        result.append(item)
    return result
