"""Authenticated translation-project HTTP endpoints and OpenAPI fragments."""

from __future__ import annotations

import shutil
from pathlib import Path

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.exc import IntegrityError

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .settings_policy import RevisionConflict
from .translation_projects import (
    TranslationProjectConflict,
    create_branches_in_session,
    create_project_in_session,
    get_project,
    get_session_project,
)


class TranslationProjectCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    checkpoint_artifact_id: str = Field(min_length=1, max_length=80)
    name: str = Field(default="", max_length=255)
    expected_revision: int = Field(ge=1)
    create_planned_branches: bool = False


class TranslationBranchTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_language: str = Field(min_length=2, max_length=40)
    name: str = Field(default="", max_length=255)


class TranslationBranchesCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: int = Field(ge=1)
    targets: list[TranslationBranchTarget] = Field(min_length=1, max_length=20)


PROJECT_SCHEMAS = {
    model.__name__: model
    for model in (
        TranslationProjectCreateRequest,
        TranslationBranchTarget,
        TranslationBranchesCreateRequest,
    )
}


def translation_project_paths() -> dict:
    paths: dict = {}
    specifications = (
        (
            "/api/v1/sessions/{sessionId}/translation-project",
            "get",
            "getSessionTranslationProject",
            None,
        ),
        (
            "/api/v1/sessions/{sessionId}/translation-project",
            "post",
            "createSessionTranslationProject",
            TranslationProjectCreateRequest,
        ),
        ("/api/v1/translation-projects/{projectId}", "get", "getTranslationProject", None),
        (
            "/api/v1/translation-projects/{projectId}/branches",
            "post",
            "createTranslationProjectBranches",
            TranslationBranchesCreateRequest,
        ),
    )
    for path, method, operation_id, model in specifications:
        write = method == "post"
        operation = {
            "operationId": operation_id,
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": ["app.write" if write else "app.read"]},
            ],
            "responses": {
                "200": {
                    "description": "Translation project state",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        }
                    },
                },
                "404": {"description": "Session, checkpoint, or project not found"},
                "409": {"description": "Changed revision, source, or duplicate language"},
                "422": {"description": "Invalid project request"},
            },
        }
        if write:
            assert model is not None
            operation["parameters"] = [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            ]
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                    }
                },
            }
        paths.setdefault(path, {})[method] = operation
    return paths


def register_translation_project_routes(
    app, services, require_auth, error_response, principal
) -> None:
    def failure(error: Exception):
        if isinstance(error, KeyError):
            return error_response("not_found", "Session, correction, or project not found.", 404)
        if isinstance(
            error,
            (
                RevisionConflict,
                TranslationProjectConflict,
                IdempotencyConflict,
                IdempotencyInProgress,
                IntegrityError,
            ),
        ):
            return error_response("revision_conflict", str(error), 409)
        if isinstance(error, (FileNotFoundError, OSError)):
            return error_response("checkpoint_missing", str(error), 409)
        return error_response("validation_error", str(error), 422)

    def parsed(model):
        try:
            return model.model_validate(request.get_json(silent=True) or {}).model_dump()
        except ValidationError as error:
            return failure(error)

    def mutate(resource_id: str, operation_id: str, payload: dict, action):
        try:
            key = services.idempotency.validate_key(request.headers.get("Idempotency-Key"))
        except ValueError as error:
            return error_response("idempotency_key_required", str(error), 400)
        directories: list[Path] = []
        committed = False
        try:
            with services.database.immediate_session() as db:
                reservation = services.idempotency.begin(
                    db,
                    principal=principal(),
                    operation_id=operation_id,
                    idempotency_key=key,
                    payload={"resource_id": resource_id, **payload},
                )
                if reservation.response is not None:
                    result, status = reservation.response
                    response = jsonify(result)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = action(db, directories)
                services.idempotency.complete(
                    db,
                    reservation,
                    response=result,
                    status_code=200,
                    resource_kind="translation_project",
                    resource_id=result["project"]["id"],
                )
            committed = True
            return jsonify(result)
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
            IntegrityError,
            FileNotFoundError,
            OSError,
        ) as error:
            return failure(error)
        finally:
            if not committed:
                for directory in directories:
                    shutil.rmtree(directory, ignore_errors=True)

    @app.get("/api/v1/sessions/<session_id>/translation-project")
    @require_auth
    def session_translation_project(session_id):
        try:
            with services.database.session() as db:
                return jsonify(get_session_project(db, session_id, paths=services.paths))
        except (KeyError, ValueError, RevisionConflict) as error:
            return failure(error)

    @app.post("/api/v1/sessions/<session_id>/translation-project")
    @require_auth
    def session_translation_project_create(session_id):
        payload = parsed(TranslationProjectCreateRequest)
        if not isinstance(payload, dict):
            return payload
        return mutate(
            session_id,
            "createSessionTranslationProject",
            payload,
            lambda db, directories: create_project_in_session(
                db, session_id, **payload, paths=services.paths,
                session_forks=services.session_forks,
                created_directories=directories,
            ),
        )

    @app.get("/api/v1/translation-projects/<project_id>")
    @require_auth
    def translation_project_get(project_id):
        try:
            with services.database.session() as db:
                return jsonify(get_project(db, project_id))
        except (KeyError, ValueError, RevisionConflict) as error:
            return failure(error)

    @app.post("/api/v1/translation-projects/<project_id>/branches")
    @require_auth
    def translation_project_branches_create(project_id):
        payload = parsed(TranslationBranchesCreateRequest)
        if not isinstance(payload, dict):
            return payload
        return mutate(
            project_id,
            "createTranslationProjectBranches",
            payload,
            lambda db, directories: create_branches_in_session(
                db,
                project_id,
                **payload,
                session_forks=services.session_forks,
                paths=services.paths,
                created_directories=directories,
            ),
        )
