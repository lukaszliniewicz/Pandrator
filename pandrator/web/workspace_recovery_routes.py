"""Explicit Administrator recovery of a team with no active Manager."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import jsonify, request
from sqlalchemy.orm import Session

from .access.contracts import WorkspaceNotFound
from .accounts.installation_mode import InstallationModeError, installation_mode_in_session
from .accounts.workspace_contracts import WorkspaceCommandError
from .accounts.workspace_recovery import WorkspaceManagerRecovery
from .domain_blueprints import DomainBlueprints
from .route_context import RouteContext
from .workspace_schemas import (
    WorkspaceManagerRecoveryRequest,
    WorkspaceManagerRecoveryResponse,
    WorkspaceRecoveryList,
    WorkspaceRecoveryQuery,
)


def register_workspace_recovery_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards
    recovery = WorkspaceManagerRecovery(audit=services.audit.record_command_in_session)

    def enabled(session: Session) -> None:
        if not installation_mode_in_session(session).enabled:
            raise InstallationModeError(
                "collaboration_disabled", "Enable collaboration to recover workspace management."
            )

    def errors(function: Callable[..., Any]):
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any):
            try:
                return function(*args, **kwargs)
            except (WorkspaceCommandError, InstallationModeError) as error:
                return guards.error_response(error.code, error.message, error.status_code)
            except WorkspaceNotFound:
                return guards.error_response("not_found", "Workspace not found.", 404)
            except ValueError:
                return guards.error_response(
                    "validation_error", "The workspace recovery request is invalid.", 422
                )
        return wrapped

    @app.get("/api/v1/admin/workspace-recovery")
    @guards.require_scope("app.admin")
    @errors
    def workspaces_without_manager_get():
        query = WorkspaceRecoveryQuery.model_validate(request.args.to_dict())
        with services.database.snapshot_session() as session:
            enabled(session)
            result = recovery.list_orphaned(session, guards.access_context(), **query.model_dump())
            return jsonify(WorkspaceRecoveryList.model_validate(result).model_dump(mode="json"))

    @app.post("/api/v1/admin/workspace-recovery/<workspace_id>/manager")
    @guards.require_scope("app.admin")
    @errors
    def workspace_manager_recover(workspace_id: str):
        request.max_content_length = 8 * 1024
        payload = WorkspaceManagerRecoveryRequest.model_validate(request.get_json(silent=True) or {})
        with services.database.immediate_session() as session:
            enabled(session)
            result = recovery.promote(
                session, guards.access_context(), workspace_id, payload.user_id,
                expected_revision=payload.expected_revision,
            )
            return jsonify(WorkspaceManagerRecoveryResponse.model_validate(result).model_dump(mode="json"))
