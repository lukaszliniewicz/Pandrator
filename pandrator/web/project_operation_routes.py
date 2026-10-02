"""Authenticated selected-language operation routes and portable OpenAPI fragments."""

from __future__ import annotations

from typing import Literal

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, ValidationError

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .project_operations import ProjectOperationError, TranslationProjectOperationService
from .settings_policy import RevisionConflict
from .workflow_plans import WorkflowPlanError


class OperationPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selected_branch_ids: list[StrictStr] = Field(min_length=1, max_length=20)
    expected_project_revision: StrictInt = Field(ge=1)
    action: Literal["translate", "generate", "export"]
    export_kind: Literal["configured", "subtitles"] = "configured"


class OperationExecuteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_digest: StrictStr = Field(min_length=64, max_length=64, pattern="^[0-9a-f]{64}$")
    accepted_confirmations: list[StrictStr] = Field(default_factory=list, max_length=20)


class OperationCancelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OperationRetryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_project_revision: StrictInt = Field(ge=1)


PROJECT_OPERATION_SCHEMAS = {
    model.__name__: model
    for model in (
        OperationPreviewRequest,
        OperationExecuteRequest,
        OperationCancelRequest,
        OperationRetryPreviewRequest,
    )
}


def project_operation_paths() -> dict:
    specs = (
        (
            "/api/v1/translation-projects/{projectId}/operations/preview",
            "post",
            "previewTranslationProjectOperation",
            OperationPreviewRequest,
            "app.read",
            "Create a job-free, principal-bound preview for selected language branches. Blocked branches are skipped; inputs, settings and required confirmations are captured for 30 minutes.",
        ),
        (
            "/api/v1/translation-project-operations/{operationId}/execute",
            "post",
            "executeTranslationProjectOperation",
            OperationExecuteRequest,
            "app.write",
            "Submit eligible captured children sequentially through existing workflow or reviewed-generation authorities. Exact replay recovers committed children without duplicate jobs.",
        ),
        (
            "/api/v1/translation-project-operations/{operationId}",
            "get",
            "getTranslationProjectOperation",
            None,
            "app.read",
            "Read redacted progress and results derived from existing jobs, generation runs and produced artifacts. Passive translation dispatch remains awaiting_agent with manual resume information.",
        ),
        (
            "/api/v1/translation-project-operations/{operationId}/cancel",
            "post",
            "cancelTranslationProjectOperation",
            OperationCancelRequest,
            "app.write",
            "Durably block pending children and request cancellation of supported active queue work, including committed children not yet linked after a crash. Completed work and passive dispatch authority are preserved.",
        ),
        (
            "/api/v1/translation-project-operations/{operationId}/retry-preview",
            "post",
            "retryTranslationProjectOperationPreview",
            OperationRetryPreviewRequest,
            "app.read",
            "Create a new preview with fresh guards/settings for failed, blocked or canceled eligible children. Successful children are retained; active or partial passive translation work requires manual resume.",
        ),
    )
    paths = {}
    for path, method, operation_id, model, scope, description in specs:
        path_name = "projectId" if "{projectId}" in path else "operationId"
        parameters = [
            {"name": path_name, "in": "path", "required": True, "schema": {"type": "string"}}
        ]
        entry = {
            "operationId": operation_id,
            "description": description,
            "security": [{"cookieAuth": []}, {"bearerToken": []}],
            "x-required-scopes": [scope],
            "parameters": parameters,
            "responses": {
                "200": {"description": "Redacted durable operation and live child states"},
                "201": {"description": "Created operation preview; no jobs submitted"},
                "202": {"description": "Eligible child work submitted or recovered"},
                "400": {"description": "Missing or invalid idempotency key"},
                "404": {"description": "Operation not found or belongs to another principal"},
                "409": {
                    "description": "Expired/stale preview, target mismatch, confirmation or idempotency conflict"
                },
                "422": {"description": "Invalid selection or request"},
            },
        }
        if model:
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            )
            entry["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{model.__name__}"},
                    }
                },
            }
        paths.setdefault(path, {})[method] = entry
    return paths


def register_project_operation_routes(app, context) -> None:
    services = context.services
    operations = TranslationProjectOperationService(services)

    def invoke(model, callback, status):
        if request.content_length is not None and request.content_length > 128 * 1024:
            return context.guards.error_response(
                "request_too_large", "The operation request exceeds the size limit.", 413
            )
        try:
            payload = model.model_validate(request.get_json(silent=True) or {}) if model else None
            principal = context.guards.principal()
            assert principal is not None
            target = services.identity.snapshot(observed_origin=request.url_root).model_dump(
                mode="json"
            )
            result = callback(payload, principal, target)
        except ValidationError:
            return context.guards.error_response(
                "validation_error", "Invalid project operation request.", 422
            )
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return context.guards.error_response(error.code, services.redactor.redact(error), 409)
        except (ProjectOperationError, WorkflowPlanError) as error:
            return context.guards.error_response(
                error.code, services.redactor.redact(error), error.status_code
            )
        except RevisionConflict as error:
            return context.guards.error_response(
                "revision_conflict", services.redactor.redact(error), 409
            )
        except KeyError:
            return context.guards.error_response("not_found", "Project or branch not found.", 404)
        except ValueError as error:
            code = (
                "idempotency_key_required"
                if "Idempotency-Key" in str(error)
                else "validation_error"
            )
            return context.guards.error_response(
                code,
                services.redactor.redact(error),
                400 if code == "idempotency_key_required" else 422,
            )
        return jsonify(result), status

    @app.post("/api/v1/translation-projects/<project_id>/operations/preview")
    @context.guards.require_scope("app.read")
    def project_operation_preview(project_id):
        return invoke(
            OperationPreviewRequest,
            lambda body, principal, target: operations.preview(
                project_id,
                body.selected_branch_ids,
                expected_project_revision=body.expected_project_revision,
                action=body.action,
                export_kind=body.export_kind,
                principal=principal,
                target_identity=target,
                idempotency_key=request.headers.get("Idempotency-Key"),
            ),
            201,
        )

    @app.post("/api/v1/translation-project-operations/<operation_id>/execute")
    @context.guards.require_scope("app.write")
    def project_operation_execute(operation_id):
        return invoke(
            OperationExecuteRequest,
            lambda body, principal, target: operations.execute(
                operation_id,
                body.preview_digest,
                body.accepted_confirmations,
                principal=principal,
                target_identity=target,
                idempotency_key=request.headers.get("Idempotency-Key"),
            ),
            202,
        )

    @app.get("/api/v1/translation-project-operations/<operation_id>")
    @context.guards.require_scope("app.read")
    def project_operation_get(operation_id):
        return invoke(
            None,
            lambda _body, principal, target: operations.get(
                operation_id,
                principal=principal,
                target_identity=target,
            ),
            200,
        )

    @app.post("/api/v1/translation-project-operations/<operation_id>/cancel")
    @context.guards.require_scope("app.write")
    def project_operation_cancel(operation_id):
        return invoke(
            OperationCancelRequest,
            lambda _body, principal, target: operations.cancel(
                operation_id,
                principal=principal,
                target_identity=target,
                idempotency_key=request.headers.get("Idempotency-Key"),
            ),
            200,
        )

    @app.post("/api/v1/translation-project-operations/<operation_id>/retry-preview")
    @context.guards.require_scope("app.read")
    def project_operation_retry_preview(operation_id):
        return invoke(
            OperationRetryPreviewRequest,
            lambda body, principal, target: operations.retry_preview(
                operation_id,
                expected_project_revision=body.expected_project_revision,
                principal=principal,
                target_identity=target,
                idempotency_key=request.headers.get("Idempotency-Key"),
            ),
            201,
        )
