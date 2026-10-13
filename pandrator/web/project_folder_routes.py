"""Authenticated project-folder endpoints and OpenAPI fragments."""

from __future__ import annotations

from typing import Annotated, Any

from flask import jsonify, request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import IntegrityError

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .project_folders import (
    FolderPromotionConflict,
    create_project_folder,
    delete_project_folder,
    project_folder_snapshot,
    replace_folder_projects,
    set_session_project_folders,
    update_project_folder,
)
from .settings_policy import RevisionConflict

FolderId = Annotated[str, Field(min_length=1, max_length=80)]
Revision = Annotated[int, Field(strict=True, ge=1)]


class ProjectFolderCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=128)
    parent_id: FolderId | None = None

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value


class ProjectFolderUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Revision
    name: str | None = Field(default=None, min_length=1, max_length=128)
    parent_id: FolderId | None = None

    @field_validator("name", mode="before")
    @classmethod
    def trim_name(cls, value: object) -> object:
        return value.strip() if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_patch(self) -> ProjectFolderUpdateRequest:
        if not self.model_fields_set.intersection({"name", "parent_id"}):
            raise ValueError("Provide name or parent_id to update the folder.")
        if "name" in self.model_fields_set and self.name is None:
            raise ValueError("Folder name cannot be null.")
        return self


class ProjectFolderDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Revision


class ProjectFolderProjectsReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_revision: Revision
    project_ids: list[FolderId] = Field(max_length=1000)


class SessionProjectFoldersReplaceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    folder_ids: list[FolderId] = Field(max_length=1000)
    expected_revisions: dict[FolderId, Revision] = Field(max_length=1000)


PROJECT_FOLDER_SCHEMAS = {
    model.__name__: model
    for model in (
        ProjectFolderCreateRequest,
        ProjectFolderUpdateRequest,
        ProjectFolderDeleteRequest,
        ProjectFolderProjectsReplaceRequest,
        SessionProjectFoldersReplaceRequest,
    )
}


def project_folder_paths() -> dict[str, dict[str, Any]]:
    paths: dict[str, dict[str, Any]] = {}
    specifications = (
        ("/api/v1/project-folders", "get", "listProjectFolders", None, "200"),
        (
            "/api/v1/project-folders",
            "post",
            "createProjectFolder",
            ProjectFolderCreateRequest,
            "201",
        ),
        (
            "/api/v1/project-folders/{folderId}",
            "patch",
            "updateProjectFolder",
            ProjectFolderUpdateRequest,
            "200",
        ),
        (
            "/api/v1/project-folders/{folderId}",
            "delete",
            "deleteProjectFolder",
            ProjectFolderDeleteRequest,
            "200",
        ),
        (
            "/api/v1/project-folders/{folderId}/projects",
            "put",
            "replaceProjectFolderProjects",
            ProjectFolderProjectsReplaceRequest,
            "200",
        ),
        (
            "/api/v1/sessions/{sessionId}/project-folders",
            "put",
            "replaceSessionProjectFolders",
            SessionProjectFoldersReplaceRequest,
            "200",
        ),
    )
    for path, method, operation_id, model, success_status in specifications:
        write = method != "get"
        operation: dict[str, Any] = {
            "operationId": operation_id,
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": ["app.write" if write else "app.read"]},
            ],
            "responses": {
                success_status: {"description": "Project-folder snapshot"},
                "404": {"description": "Folder or session not found"},
                "409": {"description": "Revision or idempotency conflict"},
                "422": {"description": "Invalid project-folder request"},
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


def register_project_folder_routes(app, context) -> None:
    services, guards = context.services, context.guards
    require_scope = guards.require_scope

    def failure(error: Exception):
        if isinstance(error, KeyError):
            return guards.error_response("not_found", "Folder or session not found.", 404)
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, FolderPromotionConflict):
            return guards.error_response("folder_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(error.code, str(error), 409)
        if isinstance(error, IntegrityError):
            return guards.error_response("folder_conflict", str(error), 409)
        return guards.error_response("validation_error", str(error), 422)

    def parse(model: type[BaseModel]):
        try:
            return model.model_validate(request.get_json(silent=True) or {})
        except ValidationError as error:
            return failure(error)

    def mutate(
        operation_id: str,
        payload: dict[str, Any],
        action,
        *,
        status: int = 200,
        resource_id: str | None = None,
    ):
        try:
            key = services.idempotency.validate_key(
                request.headers.get("Idempotency-Key")
            )
        except ValueError as error:
            return guards.error_response("idempotency_key_required", str(error), 400)
        try:
            with services.database.immediate_session() as session:
                reservation = services.idempotency.begin(
                    session,
                    principal=guards.principal(),
                    operation_id=operation_id,
                    idempotency_key=key,
                    payload=payload,
                )
                if reservation.response is not None:
                    result, response_status = reservation.response
                    replayed = True
                else:
                    result = action(session)
                    response_status = status
                    replayed = False
                    services.idempotency.complete(
                        session,
                        reservation,
                        response=result,
                        status_code=status,
                        resource_kind="project_folder",
                        resource_id=resource_id,
                    )
            response = jsonify(result)
            response.status_code = response_status
            if replayed:
                response.headers["Idempotency-Replayed"] = "true"
            return response
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            FolderPromotionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
            IntegrityError,
        ) as error:
            return failure(error)

    @app.get("/api/v1/project-folders")
    @require_scope("app.read")
    def project_folders_list():
        with services.database.snapshot_session() as session:
            return jsonify(project_folder_snapshot(session))

    @app.post("/api/v1/project-folders")
    @require_scope("app.write")
    def project_folders_create():
        body = parse(ProjectFolderCreateRequest)
        if not isinstance(body, ProjectFolderCreateRequest):
            return body
        payload = body.model_dump(mode="json")
        return mutate(
            "createProjectFolder",
            payload,
            lambda session: create_project_folder(session, **payload),
            status=201,
        )

    @app.patch("/api/v1/project-folders/<folder_id>")
    @require_scope("app.write")
    def project_folders_update(folder_id: str):
        body = parse(ProjectFolderUpdateRequest)
        if not isinstance(body, ProjectFolderUpdateRequest):
            return body
        fields = body.model_fields_set
        payload = {"folder_id": folder_id, **body.model_dump(mode="json", exclude_unset=True)}
        return mutate(
            "updateProjectFolder",
            payload,
            lambda session: update_project_folder(
                session,
                folder_id,
                expected_revision=body.expected_revision,
                name=body.name,
                parent_id=body.parent_id,
                change_name="name" in fields,
                change_parent="parent_id" in fields,
            ),
            resource_id=folder_id,
        )

    @app.delete("/api/v1/project-folders/<folder_id>")
    @require_scope("app.write")
    def project_folders_delete(folder_id: str):
        body = parse(ProjectFolderDeleteRequest)
        if not isinstance(body, ProjectFolderDeleteRequest):
            return body
        payload = {"folder_id": folder_id, **body.model_dump(mode="json")}
        return mutate(
            "deleteProjectFolder",
            payload,
            lambda session: delete_project_folder(
                session,
                folder_id,
                expected_revision=body.expected_revision,
            ),
            resource_id=folder_id,
        )

    @app.put("/api/v1/project-folders/<folder_id>/projects")
    @require_scope("app.write")
    def project_folder_projects_replace(folder_id: str):
        body = parse(ProjectFolderProjectsReplaceRequest)
        if not isinstance(body, ProjectFolderProjectsReplaceRequest):
            return body
        payload = {"folder_id": folder_id, **body.model_dump(mode="json")}
        return mutate(
            "replaceProjectFolderProjects",
            payload,
            lambda session: replace_folder_projects(
                session,
                folder_id,
                expected_revision=body.expected_revision,
                project_ids=body.project_ids,
            ),
            resource_id=folder_id,
        )

    @app.put("/api/v1/sessions/<session_id>/project-folders")
    @require_scope("app.write")
    def session_project_folders_replace(session_id: str):
        body = parse(SessionProjectFoldersReplaceRequest)
        if not isinstance(body, SessionProjectFoldersReplaceRequest):
            return body
        payload = {"session_id": session_id, **body.model_dump(mode="json")}
        return mutate(
            "replaceSessionProjectFolders",
            payload,
            lambda session: set_session_project_folders(
                session,
                session_id,
                folder_ids=body.folder_ids,
                expected_revisions=body.expected_revisions,
            ),
        )
