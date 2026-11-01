"""Thin HTTP adapters for private current-project copy commands."""

from __future__ import annotations

from functools import wraps
from typing import Any, Callable

from flask import g, jsonify, request

from .accounts.installation_mode import InstallationModeError
from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .project_copy_schemas import (
    ProjectCopyPreview,
    ProjectCopyPreviewQuery,
    ProjectCopyPreviewRequest,
    ProjectCopyRequest,
    ProjectCopyStatus,
)
from .projects.copy_contracts import ProjectCopyError
from .projects.copy_jobs import ProjectCopyService
from .route_context import RouteContext


def register_project_copy_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards
    copying = ProjectCopyService(
        services.database,
        services.paths,
        services.jobs,
        services.idempotency,
        services.workspace_settings,
        services.audit,
    )

    def errors(function: Callable[..., Any]):
        @wraps(function)
        def wrapped(*args: Any, **kwargs: Any):
            try:
                return function(*args, **kwargs)
            except (ProjectCopyError, InstallationModeError) as error:
                return guards.error_response(error.code, str(error), error.status_code)
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return guards.error_response(error.code, str(error), 409)
            except KeyError:
                return guards.error_response("not_found", "Project or copy not found.", 404)

        return wrapped

    @app.get("/api/v1/projects/<project_id>/copy-preview")
    @guards.require_scope("app.write")
    @errors
    def project_copy_preview_get(project_id: str):
        query = ProjectCopyPreviewQuery.model_validate(request.args.to_dict())
        result = copying.preview(guards.access_context(), project_id, query.workspace_id)
        return jsonify(ProjectCopyPreview.model_validate(result).model_dump(mode="json"))

    @app.post("/api/v1/projects/<project_id>/copy-preview")
    @guards.require_scope("app.write")
    @errors
    def project_copy_preview_post(project_id: str):
        request.max_content_length = 128 * 1024
        payload = ProjectCopyPreviewRequest.model_validate(request.get_json(silent=True) or {})
        result = copying.preview(
            guards.access_context(), project_id, payload.destination_workspace_id,
            destination_connections=[item.model_dump(mode="json") for item in payload.destination_connections],
        )
        return jsonify(ProjectCopyPreview.model_validate(result).model_dump(mode="json"))

    @app.post("/api/v1/projects/<project_id>/copy")
    @guards.require_scope("app.run")
    @errors
    def project_copy_request(project_id: str):
        request.max_content_length = 128 * 1024
        payload = ProjectCopyRequest.model_validate(request.get_json(silent=True) or {})
        principal = guards.principal()
        if principal is None:
            return guards.error_response(
                "authentication_required", "Sign in to copy a project.", 401
            )
        result, status = copying.request(
            principal,
            project_id,
            payload.destination_workspace_id,
            expected_fingerprint=payload.expected_fingerprint,
            name=payload.name,
            destination_connections=[item.model_dump(mode="json") for item in payload.destination_connections],
            idempotency_key=request.headers.get("Idempotency-Key"),
        )
        g.audit_resource_kind, g.audit_resource_id = "job", result["id"]
        return jsonify(ProjectCopyStatus.model_validate(result).model_dump(mode="json")), status

    @app.get("/api/v1/project-copies/<copy_id>")
    @guards.require_scope("app.read")
    @errors
    def project_copy_get(copy_id: str):
        result = copying.get(guards.access_context(), copy_id)
        return jsonify(ProjectCopyStatus.model_validate(result).model_dump(mode="json"))
