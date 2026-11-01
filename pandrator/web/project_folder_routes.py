"""Authenticated project-folder endpoints and OpenAPI fragments."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated, Any

from flask import jsonify, request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy.exc import IntegrityError

from .access.folder_policy import (
    require_folder_inputs,
    require_folder_replay_scope,
    require_folder_workspace,
)
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
_WORKSPACE_QUERY = TypeAdapter(FolderId | None)


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
        operation["parameters"] = [
            {
                "name": "workspace_id",
                "in": "query",
                "required": False,
                "description": "Owning workspace; defaults to the caller's personal workspace.",
                "schema": {"type": "string", "minLength": 1, "maxLength": 80},
            }
        ]
        if write:
            assert model is not None
            operation["parameters"] += [
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
        folder_ids: Sequence[str] = (),
        session_ids: Sequence[str] = (),
        existing_folder_ids: Sequence[str] = (),
    ):
        try:
            requested_workspace = _WORKSPACE_QUERY.validate_python(request.args.get("workspace_id"))
        except ValidationError as error:
            return failure(error)
        try:
            key = services.idempotency.validate_key(
                request.headers.get("Idempotency-Key")
            )
        except ValueError as error:
            return guards.error_response("idempotency_key_required", str(error), 400)
        try:
            with services.database.immediate_session() as session:
                scope = require_folder_workspace(
                    session, guards.access_context(), requested_workspace, write=True
                )
                require_folder_inputs(
                    session, scope.id, folder_ids=folder_ids, session_ids=session_ids,
                    existing_folder_ids=existing_folder_ids,
                    allow_missing=True,
                )
                # Retain legacy personal fingerprints; team scopes cannot alias.
                scoped_payload = (
                    {**payload, "workspace_id": scope.id} if scope.kind == "team" else payload
                )
                reservation = services.idempotency.begin(
                    session,
                    principal=guards.principal(),
                    operation_id=operation_id,
                    idempotency_key=key,
                    payload=scoped_payload,
                )
                if reservation.response is not None:
                    result, response_status = reservation.response
                    require_folder_replay_scope(session, result, scope.id)
                    replayed = True
                else:
                    result = action(session, scope.id)
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
        try:
            requested_workspace = _WORKSPACE_QUERY.validate_python(request.args.get("workspace_id"))
            with services.database.snapshot_session() as session:
                scope = require_folder_workspace(session, guards.access_context(), requested_workspace)
                return jsonify(project_folder_snapshot(session, workspace_id=scope.id))
        except (KeyError, ValidationError) as error:
            return failure(error)

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
            lambda session, workspace_id: create_project_folder(
                session, workspace_id=workspace_id, **payload
            ),
            status=201,
            folder_ids=(body.parent_id,) if body.parent_id is not None else (),
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
            lambda session, _workspace_id: update_project_folder(
                session,
                folder_id,
                expected_revision=body.expected_revision,
                name=body.name,
                parent_id=body.parent_id,
                change_name="name" in fields,
                change_parent="parent_id" in fields,
            ),
            resource_id=folder_id,
            existing_folder_ids=(folder_id,),
            folder_ids=(body.parent_id,) if body.parent_id is not None else (),
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
            lambda session, _workspace_id: delete_project_folder(
                session,
                folder_id,
                expected_revision=body.expected_revision,
            ),
            resource_id=folder_id,
            existing_folder_ids=(folder_id,),
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
            lambda session, _workspace_id: replace_folder_projects(
                session,
                folder_id,
                expected_revision=body.expected_revision,
                project_ids=body.project_ids,
            ),
            resource_id=folder_id,
            existing_folder_ids=(folder_id,),
            session_ids=body.project_ids,
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
            lambda session, _workspace_id: set_session_project_folders(
                session,
                session_id,
                folder_ids=body.folder_ids,
                expected_revisions=body.expected_revisions,
            ),
            session_ids=(session_id,),
            folder_ids=tuple(set(body.folder_ids) | body.expected_revisions.keys()),
        )
