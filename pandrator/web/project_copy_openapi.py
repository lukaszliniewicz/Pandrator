"""Public current-project copy contracts; frozen editorial inputs stay private."""

from __future__ import annotations

from .project_copy_schemas import PROJECT_COPY_SCHEMAS as PROJECT_COPY_SCHEMAS


def project_copy_paths() -> dict:
    root = "/api/v1/projects/{projectId}"
    specifications = (
        (
            root + "/copy-preview",
            "post",
            "previewProjectCopy",
            "ProjectCopyPreview",
            ["app.read", "app.write"],
        ),
        (
            root + "/copy-preview",
            "get",
            "getProjectCopyPreview",
            "ProjectCopyPreview",
            ["app.read", "app.write"],
        ),
        (
            root + "/copy",
            "post",
            "copyCurrentProject",
            "ProjectCopyStatus",
            ["app.read", "app.write", "app.run"],
        ),
        (
            "/api/v1/project-copies/{copyId}",
            "get",
            "getProjectCopy",
            "ProjectCopyStatus",
            ["app.read"],
        ),
    )
    paths = {}
    for path, method, operation_id, response, scopes in specifications:
        parameters = [
            {
                "name": name,
                "in": "path",
                "required": True,
                "schema": {"type": "string", "minLength": 1, "maxLength": 36},
            }
            for name in ("projectId", "copyId")
            if "{" + name + "}" in path
        ]
        if operation_id == "getProjectCopyPreview":
            parameters.append(
                {
                    "name": "workspace_id",
                    "in": "query",
                    "required": True,
                    "schema": {"type": "string", "minLength": 1, "maxLength": 36},
                }
            )
        if operation_id == "copyCurrentProject":
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            )
        operation = {
            "operationId": operation_id,
            "description": (
                "Copy the approved saved project into a workspace using a durable job. "
                "Source editing and destination creation authority are required. "
                "Unsupported saved state or unresolved private connections block copying. "
                "The original project remains intact."
                if operation_id == "copyCurrentProject"
                else "Read a safe copy preview or the initiating user's current authorized copy status. "
                "Internal snapshots, private connections and file paths are withheld."
            ),
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": scopes}],
            "x-required-scopes": scopes,
            "parameters": parameters,
            "responses": {
                "200": {
                    "description": "Current authorized state",
                    "content": {
                        "application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}
                    },
                },
                "401": {"description": "Authentication required"},
                "403": {"description": "Current action authority required"},
                "404": {"description": "Project, workspace or copy unavailable"},
                "409": {
                    "description": "Collaboration disabled, stale preview, unsupported state or receipt conflict"
                },
                "422": {"description": "Invalid bounded request"},
            },
        }
        if operation_id == "copyCurrentProject":
            operation["responses"]["202"] = {
                "description": "Copy queued",
                "content": {
                    "application/json": {
                        "schema": {"$ref": "#/components/schemas/ProjectCopyStatus"}
                    }
                },
            }
        if method == "post":
            request_model = (
                "ProjectCopyRequest" if operation_id == "copyCurrentProject"
                else "ProjectCopyPreviewRequest"
            )
            operation["responses"]["413"] = {"description": "Request exceeds the bounded input limit"}
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{request_model}"}
                    }
                },
            }
        paths.setdefault(path, {})[method] = operation
    return paths
