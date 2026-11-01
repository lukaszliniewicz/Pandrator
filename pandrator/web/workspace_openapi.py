"""Workspace, invitation and installation-mode HTTP contract."""
from __future__ import annotations

from typing import Any

from .workspace_schemas import WORKSPACE_SCHEMAS as WORKSPACE_SCHEMAS


def workspace_paths() -> dict[str, Any]:
    root = "/api/v1/workspaces/{workspaceId}"
    invite = "/api/v1/workspace-invitations/{invitationId}"
    # path, method, operation ID, body, response, primary scope, status, receipt
    operations = (
        ("/api/v1/admin/installation", "get", "getInstallationMode", None, "InstallationModeResponse", "app.admin", "200", False),
        ("/api/v1/admin/installation/collaboration", "post", "enableCollaboration", "EnableCollaborationRequest", "InstallationModeResponse", "app.admin", "200", False),
        ("/api/v1/admin/workspace-recovery", "get", "listWorkspacesWithoutManager", None, "WorkspaceRecoveryList", "app.admin", "200", False),
        ("/api/v1/admin/workspace-recovery/{workspaceId}/manager", "post", "recoverWorkspaceManager", "WorkspaceManagerRecoveryRequest", "WorkspaceManagerRecoveryResponse", "app.admin", "200", False),
        ("/api/v1/workspaces", "get", "listWorkspaces", None, "WorkspaceListResponse", "app.read", "200", False),
        ("/api/v1/workspaces", "post", "createWorkspace", "WorkspaceCreateRequest", "WorkspaceResponse", "app.workspaces.enroll", "201", True),
        (root, "get", "getWorkspace", None, "WorkspaceResponse", "app.read", "200", False),
        (root, "patch", "renameWorkspace", "WorkspaceRenameRequest", "WorkspaceResponse", "app.write", "200", False),
        (root + "/members", "get", "listWorkspaceMembers", None, "WorkspaceMembersResponse", "app.read", "200", False),
        (root + "/user-search", "get", "searchWorkspaceUsers", None, "WorkspaceUserSearchResponse", "app.read", "200", False),
        (root + "/members/{userId}", "patch", "setWorkspaceMemberRole", "WorkspaceRoleRequest", "WorkspaceResponse", "app.write", "200", False),
        (root + "/members/{userId}", "delete", "removeWorkspaceMember", "WorkspaceRevisionRequest", "WorkspaceRemovalResponse", "app.write", "200", False),
        (root + "/leave", "post", "leaveWorkspace", "WorkspaceRevisionRequest", "WorkspaceRemovalResponse", "app.write", "200", False),
        (root + "/archive", "post", "archiveWorkspace", "WorkspaceRevisionRequest", "WorkspaceResponse", "app.write", "200", False),
        (root + "/restore", "post", "restoreWorkspace", "WorkspaceRevisionRequest", "WorkspaceResponse", "app.write", "200", False),
        (root + "/invitations", "get", "listWorkspaceInvitations", None, "WorkspaceInvitationsResponse", "app.read", "200", False),
        (root + "/invitations", "post", "inviteWorkspaceUser", "WorkspaceInviteRequest", "WorkspaceInvitationResponse", "app.write", "201", True),
        (root + "/invitations/{invitationId}", "delete", "revokeWorkspaceInvitation", "WorkspaceRevisionRequest", "WorkspaceInvitationResponse", "app.write", "200", False),
        ("/api/v1/workspace-invitations", "get", "listMyWorkspaceInvitations", None, "WorkspaceInvitationsResponse", "app.read", "200", False),
        (invite + "/accept", "post", "acceptWorkspaceInvitation", None, "WorkspaceInvitationResponse", "app.workspaces.enroll", "200", False),
        (invite + "/decline", "post", "declineWorkspaceInvitation", None, "WorkspaceInvitationResponse", "app.write", "200", False),
    )
    paths: dict[str, Any] = {}
    for path, method, operation_id, body, response, scope, status, receipt in operations:
        params: list[dict[str, Any]] = []
        for name in ("workspaceId", "userId", "invitationId"):
            if "{" + name + "}" in path:
                params.append({"name": name, "in": "path", "required": True, "schema": {"type": "string"}})
        if receipt:
            params.append({"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "minLength": 8, "maxLength": 200}})
        if operation_id == "listWorkspaces":
            params.append({"name": "include_archived", "in": "query", "schema": {"type": "boolean", "default": False}})
        if operation_id == "searchWorkspaceUsers":
            params.append({"name": "q", "in": "query", "required": True, "schema": {"type": "string", "minLength": 2, "maxLength": 64}})
        if operation_id == "listWorkspacesWithoutManager":
            params.extend([
                {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
                {"name": "offset", "in": "query", "schema": {"type": "integer", "minimum": 0, "maximum": 100000, "default": 0}},
            ])
        operation = {
            "operationId": operation_id,
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": [scope]}],
            "parameters": params,
            "responses": {
                status: {"description": "Completed", "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{response}"}}}},
                "401": {"description": "Authentication required"},
                "403": {"description": "Current account, credential or action authority required"},
                "404": {"description": "Resource not found or unavailable"},
                "409": {"description": "Collaboration disabled, revision conflict or protected transition"},
                "422": {"description": "Invalid request fields"},
            },
        }
        operation["x-required-scopes"] = (
            ["app.read", "app.write", "app.workspaces.enroll"] if scope == "app.workspaces.enroll"
            else ["app.read", "app.write"] if scope == "app.write"
            else ["app.admin", "app.read", "app.write"] if operation_id == "recoverWorkspaceManager"
            else ["app.admin", "app.read"] if operation_id == "listWorkspacesWithoutManager"
            else ["app.admin", "app.write"] if operation_id == "enableCollaboration"
            else [scope]
        )
        # OAuth scope arrays express an AND requirement, matching the live
        # account/workspace checks rather than only the route's primary guard.
        operation["security"][-1]["nativeOAuth"] = operation["x-required-scopes"]
        if path.startswith("/api/v1/admin/"):
            operation["x-required-role"] = "administrator"
        if body is not None:
            operation["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{body}"}}}}
        paths.setdefault(path, {})[method] = operation
    return paths
