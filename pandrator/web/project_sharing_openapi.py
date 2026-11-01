"""Fixed HTTP surface for project viewing grants and shared discovery."""

from __future__ import annotations

from .project_sharing_schemas import PROJECT_SHARING_SCHEMAS as PROJECT_SHARING_SCHEMAS


def project_sharing_paths() -> dict:
    root = "/api/v1/projects/{projectId}/viewers"
    specifications = (
        ("/api/v1/projects/shared-with-me", "get", "listSharedProjects", "SharedProjectsResponse"),
        (root, "get", "getProjectViewers", "ProjectViewersResponse"),
        (root + "/users", "get", "searchProjectViewers", "ProjectViewingCandidatesResponse"),
        (root + "/{userId}", "put", "grantProjectView", "ProjectViewersResponse"),
        (root + "/{userId}/revoke", "post", "revokeProjectView", "ProjectViewersResponse"),
    )
    paths = {}
    for path, method, operation_id, response in specifications:
        parameters = [
            {"name": name, "in": "path", "required": True,
             "schema": {"type": "string", "minLength": 1, "maxLength": 80}}
            for name in ("projectId", "userId") if "{" + name + "}" in path
        ]
        if operation_id in {"listSharedProjects", "searchProjectViewers"}:
            parameters.append({
                "name": "query", "in": "query", "required": False,
                "schema": {"type": "string", "maxLength": 255 if operation_id == "listSharedProjects" else 64},
            })
        if operation_id == "listSharedProjects":
            parameters.extend([
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
                {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "default": 0}},
            ])
        scopes = ["app.read"] if method == "get" else ["app.read", "app.write"]
        operation = {
            "operationId": operation_id,
            "description": (
                "Read projects shared with this account and approved for this credential. Private workspace metadata is withheld."
                if operation_id == "listSharedProjects" else
                "Only the personal owner or shared-workspace Manager can manage named Can view access. Editing requires workspace membership."
            ),
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": scopes}],
            "x-required-scopes": scopes,
            "parameters": parameters,
            "responses": {
                "200": {"description": "Current viewing access", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}}},
                "401": {"description": "Authentication required"},
                "403": {"description": "Current project management authority required"},
                "404": {"description": "Project or account not found or unavailable"},
                "409": {"description": "Collaboration disabled, changed revision or unavailable project"},
                "422": {"description": "Invalid bounded request"},
            },
        }
        if method != "get":
            operation["requestBody"] = {"required": True, "content": {"application/json": {
                "schema": {"$ref": "#/components/schemas/ProjectViewingRevisionRequest"}
            }}}
        paths.setdefault(path, {})[method] = operation
    return paths
