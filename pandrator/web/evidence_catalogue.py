"""Evidence choices from the current project's authorized workspace namespace."""

from __future__ import annotations

from typing import Any

from sqlalchemy import or_, select

from .access.contracts import AccessContext, ProjectAction, WorkspaceNotFound
from .access.workspace_policy import require_workspace_access
from .accounts.installation_mode import installation_mode_in_session
from .database import Database
from .evidence_access import require_evidence_session
from .models import Provider, ProviderModel
from .subtitle_evidence import evidence_route_catalog


def evidence_choices(
    database: Database,
    paths: Any,
    context: AccessContext,
    *,
    session_id: str | None = None,
    language: str | None = None,
    include_languages: bool = False,
) -> dict[str, Any]:
    workspace_id: str | None = None
    audio_models: list[dict[str, Any]] = []
    with database.snapshot_session() as session:
        allow_namespace = True
        if session_id is not None:
            access = require_evidence_session(session, context, session_id, ProjectAction.READ)
            requested_workspace = access.workspace_id
        else:
            # No implicit initial-owner namespace: this policy resolves the
            # actual caller's personal workspace and credential ceilings.
            requested_workspace = None
            allow_namespace = not installation_mode_in_session(session).enabled
        if allow_namespace:
            try:
                workspace_id = require_workspace_access(session, context, requested_workspace).id
            except WorkspaceNotFound:
                # Project-only grants permit builtin evidence without exposing
                # the workspace's private provider/model catalogue.
                pass
        if workspace_id is not None:
            rows = session.execute(
                select(
                    ProviderModel.id,
                    ProviderModel.model_id,
                    ProviderModel.input_modalities_json,
                    Provider.label,
                    Provider.options_json["is_custom"].as_boolean().label("custom"),
                )
                .join(Provider, Provider.id == ProviderModel.provider_id)
                .where(
                    Provider.workspace_id == workspace_id,
                    Provider.kind == "llm",
                    Provider.enabled.is_(True),
                    or_(ProviderModel.is_active.is_(True), ProviderModel.is_default.is_(True)),
                    ProviderModel.input_modalities_json.contains('"audio"'),
                )
                .order_by(Provider.label, ProviderModel.model_id, ProviderModel.id)
            )
            audio_models = [
                {
                    "id": row.id,
                    "model_id": row.model_id,
                    "provider_label": row.label,
                    "custom": bool(row.custom),
                }
                for row in rows
                if isinstance(row.input_modalities_json, list)
                and "audio" in row.input_modalities_json
            ]
    routes = evidence_route_catalog(
        None,
        language,
        **(
            {"database": database, "paths": paths, "workspace_id": workspace_id}
            if workspace_id is not None
            else {}
        ),
    )
    if not include_languages:
        routes = [
            {key: value for key, value in route.items() if key != "supported_languages"}
            for route in routes
        ]
    return {
        "schema_version": "1",
        "language": language,
        "routes": routes,
        "audio_models": audio_models,
        "can_select_audio_models": workspace_id is not None,
    }
