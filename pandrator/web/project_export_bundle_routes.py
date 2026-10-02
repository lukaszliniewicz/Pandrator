"""Authenticated routes and OpenAPI fragments for project export bundles."""

from __future__ import annotations

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field, StrictStr, ValidationError

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .project_export_bundles import (
    ProjectExportBundleError,
    ProjectExportBundleService,
)
from .project_operations import ProjectOperationError
from .settings_policy import RevisionConflict


class ProjectExportBundleRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_manifest_digest: StrictStr = Field(
        min_length=64,
        max_length=64,
        pattern="^[0-9a-f]{64}$",
    )


PROJECT_EXPORT_BUNDLE_SCHEMAS = {
    ProjectExportBundleRequest.__name__: ProjectExportBundleRequest,
}


def project_export_bundle_paths() -> dict:
    operation_path = "/api/v1/translation-project-operations/{operationId}/exports"
    return {
        f"{operation_path}/manifest": {
            "get": {
                "operationId": "getTranslationProjectExportManifest",
                "description": (
                    "Return a redacted, versioned manifest for every selected language, "
                    "including incomplete states and verified artifact hashes."
                ),
                "security": [{"cookieAuth": []}, {"bearerToken": []}],
                "x-required-scopes": ["app.read"],
                "parameters": [
                    {
                        "name": "operationId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                ],
                "responses": {
                    "200": {"description": "Versioned project export manifest"},
                    "404": {"description": "Operation not found or belongs to another principal"},
                    "409": {"description": "Operation target or export action is incompatible"},
                },
            }
        },
        f"{operation_path}/bundle": {
            "post": {
                "operationId": "requestTranslationProjectExportBundle",
                "description": (
                    "Queue or recover a durable ZIP bundle only when every selected export "
                    "is complete and still matches its recorded managed-file hash."
                ),
                "security": [{"cookieAuth": []}, {"bearerToken": []}],
                "x-required-scopes": ["app.write"],
                "parameters": [
                    {
                        "name": "operationId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    },
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                    },
                ],
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/ProjectExportBundleRequest"}
                        }
                    },
                },
                "responses": {
                    "200": {"description": "Matching cached bundle and manifest artifacts"},
                    "202": {"description": "Bundle job queued, running or recovered"},
                    "400": {"description": "Missing or invalid idempotency key"},
                    "404": {"description": "Operation not found or belongs to another principal"},
                    "409": {"description": "Incomplete, stale or unavailable bundle inputs"},
                    "422": {"description": "Invalid bundle request"},
                },
            }
        },
    }


def register_project_export_bundle_routes(app, context) -> None:
    services = context.services
    bundles = ProjectExportBundleService(services)

    def invoke(model, callback):
        if request.content_length is not None and request.content_length > 16 * 1024:
            return context.guards.error_response(
                "request_too_large", "The project export request exceeds the size limit.", 413
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
                "validation_error", "Invalid project export bundle request.", 422
            )
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return context.guards.error_response(error.code, services.redactor.redact(error), 409)
        except ProjectExportBundleError as error:
            return context.guards.error_response(
                error.code,
                services.redactor.redact(error),
                error.status_code,
                services.redactor.redact_value(error.details),
            )
        except ProjectOperationError as error:
            return context.guards.error_response(
                error.code,
                services.redactor.redact(error),
                error.status_code,
                services.redactor.redact_value(error.details),
            )
        except RevisionConflict as error:
            return context.guards.error_response(
                "revision_conflict", services.redactor.redact(error), 409
            )
        except KeyError:
            return context.guards.error_response("not_found", "Project operation not found.", 404)
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
        if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], int):
            body, status_code = result
            return jsonify(body), status_code
        return jsonify(result), 200

    @app.get("/api/v1/translation-project-operations/<operation_id>/exports/manifest")
    @context.guards.require_scope("app.read")
    def project_export_manifest(operation_id):
        return invoke(
            None,
            lambda _body, principal, target: bundles.manifest(operation_id, principal, target),
        )

    @app.post("/api/v1/translation-project-operations/<operation_id>/exports/bundle")
    @context.guards.require_scope("app.write")
    def project_export_bundle(operation_id):
        return invoke(
            ProjectExportBundleRequest,
            lambda body, principal, target: bundles.request_bundle(
                operation_id,
                body.expected_manifest_digest,
                principal=principal,
                target_identity=target,
                idempotency_key=request.headers.get("Idempotency-Key"),
            ),
        )
