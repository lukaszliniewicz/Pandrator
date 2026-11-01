"""Fixed workspace routes shared by the MCP catalog, inputs and HTTP adapter."""
from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .schemas.workspaces import (
    CreateWorkspaceInput,
    EnableCollaborationInput,
    GetWorkspaceInput,
    InviteWorkspaceUserInput,
    ListWorkspacesInput,
    ListWorkspacesWithoutManagerInput,
    RenameWorkspaceInput,
    RespondWorkspaceInvitationInput,
    RevokeWorkspaceInvitationInput,
    SearchWorkspaceUsersInput,
    WorkspaceMemberInput,
    WorkspaceMemberRoleInput,
    WorkspaceRevisionInput,
    WorkspaceToolInput,
)


@dataclass(frozen=True, slots=True)
class WorkspaceOperation:
    action: str
    model: type[BaseModel]
    title: str
    scope: str
    operation_id: str
    method: str
    path: str
    description: str
    idempotency: bool = False

    @property
    def required_scopes(self) -> tuple[str, ...]:
        """Full backend prerequisites; ``scope`` remains the primary scope."""
        if self.scope == "app.workspaces.enroll":
            return ("app.read", "app.write", "app.workspaces.enroll")
        if self.scope == "app.write":
            return ("app.read", "app.write")
        if self.action == "enable_collaboration":
            return ("app.admin", "app.write")
        if self.action == "list_workspaces_without_manager":
            return ("app.admin", "app.read")
        if self.action == "recover_workspace_manager":
            return ("app.admin", "app.read", "app.write")
        return (self.scope,)

    @property
    def name(self) -> str:
        return f"pandrator_{self.action}"


_ROOT = "/api/v1/workspaces/{workspace_id}"
_INVITE = "/api/v1/workspace-invitations/{invitation_id}"
WORKSPACE_OPERATIONS = (
    WorkspaceOperation("list_workspaces", ListWorkspacesInput, "List workspaces", "app.read", "listWorkspaces", "GET", "/api/v1/workspaces", "List your personal and shared workspaces within this credential's approved resources."),
    WorkspaceOperation("get_workspace", GetWorkspaceInput, "Get workspace", "app.read", "getWorkspace", "GET", _ROOT, "Read one workspace and current allowed actions. Project-only sharing does not grant workspace access."),
    WorkspaceOperation("create_workspace", CreateWorkspaceInput, "Create shared workspace", "app.workspaces.enroll", "createWorkspace", "POST", "/api/v1/workspaces", "Create an empty shared workspace as Manager. Requires explicit create-or-join approval; it does not copy private projects.", True),
    WorkspaceOperation("rename_workspace", RenameWorkspaceInput, "Rename workspace", "app.write", "renameWorkspace", "PATCH", _ROOT, "A Manager can rename a shared workspace at its current revision."),
    WorkspaceOperation("list_workspace_members", GetWorkspaceInput, "List workspace members", "app.read", "listWorkspaceMembers", "GET", _ROOT + "/members", "List members of a workspace you belong to."),
    WorkspaceOperation("search_workspace_users", SearchWorkspaceUsersInput, "Find users to invite", "app.read", "searchWorkspaceUsers", "GET", _ROOT + "/user-search", "A workspace Manager can search existing active accounts by name or login; at least two characters are required."),
    WorkspaceOperation("set_workspace_member_role", WorkspaceMemberRoleInput, "Change workspace member role", "app.write", "setWorkspaceMemberRole", "PATCH", _ROOT + "/members/{user_id}", "A Manager can change a member's role at the workspace revision. Both roles can edit all content; the last active Manager is protected."),
    WorkspaceOperation("remove_workspace_member", WorkspaceMemberInput, "Remove workspace member", "app.write", "removeWorkspaceMember", "DELETE", _ROOT + "/members/{user_id}", "A Manager can revoke a member's workspace and project access. Their work remains owned by the workspace."),
    WorkspaceOperation("leave_workspace", WorkspaceRevisionInput, "Leave workspace", "app.write", "leaveWorkspace", "POST", _ROOT + "/leave", "Leave a shared workspace and revoke your access. Another active Manager must remain."),
    WorkspaceOperation("archive_workspace", WorkspaceRevisionInput, "Archive workspace", "app.write", "archiveWorkspace", "POST", _ROOT + "/archive", "A Manager can archive a workspace. Projects remain stored and unavailable until restored."),
    WorkspaceOperation("restore_workspace", WorkspaceRevisionInput, "Restore workspace", "app.write", "restoreWorkspace", "POST", _ROOT + "/restore", "A Manager can restore an archived workspace at its current revision."),
    WorkspaceOperation("list_workspace_invitations", GetWorkspaceInput, "List workspace invitations", "app.read", "listWorkspaceInvitations", "GET", _ROOT + "/invitations", "A Manager can read invitations issued for this workspace."),
    WorkspaceOperation("invite_workspace_user", InviteWorkspaceUserInput, "Invite workspace user", "app.write", "inviteWorkspaceUser", "POST", _ROOT + "/invitations", "A Manager can invite an existing account as Member or Manager. The recipient signs in and accepts; invitations expire after seven days.", True),
    WorkspaceOperation("revoke_workspace_invitation", RevokeWorkspaceInvitationInput, "Revoke workspace invitation", "app.write", "revokeWorkspaceInvitation", "DELETE", _ROOT + "/invitations/{invitation_id}", "A Manager can revoke a pending workspace invitation at the workspace revision."),
    WorkspaceOperation("list_my_workspace_invitations", WorkspaceToolInput, "My workspace invitations", "app.read", "listMyWorkspaceInvitations", "GET", "/api/v1/workspace-invitations", "Read your invitation inbox. This credential must have access to your personal workspace."),
    WorkspaceOperation("accept_workspace_invitation", RespondWorkspaceInvitationInput, "Join invited workspace", "app.workspaces.enroll", "acceptWorkspaceInvitation", "POST", _INVITE + "/accept", "Accept a pending invitation for your account and approve this credential for the workspace. An old accepted invitation cannot restore removed access."),
    WorkspaceOperation("decline_workspace_invitation", RespondWorkspaceInvitationInput, "Decline workspace invitation", "app.write", "declineWorkspaceInvitation", "POST", _INVITE + "/decline", "Decline a pending invitation for your account without joining the workspace."),
    WorkspaceOperation("get_installation_mode", WorkspaceToolInput, "Installation access mode", "app.admin", "getInstallationMode", "GET", "/api/v1/admin/installation", "An Administrator can inspect Solo or collaboration mode and release availability."),
    WorkspaceOperation("enable_collaboration", EnableCollaborationInput, "Enable collaboration", "app.admin", "enableCollaboration", "POST", "/api/v1/admin/installation/collaboration", "An Administrator can opt this installation into named login and shared workspaces after setting an Administrator password. Requires a qualified build; network hosting is configured separately."),
    WorkspaceOperation("list_workspaces_without_manager", ListWorkspacesWithoutManagerInput, "Workspaces without a Manager", "app.admin", "listWorkspacesWithoutManager", "GET", "/api/v1/admin/workspace-recovery", "An Administrator can inspect bounded recovery metadata for shared workspaces with no active Manager, including existing active Members eligible for promotion. This does not grant project or library access."),
    WorkspaceOperation("recover_workspace_manager", WorkspaceMemberInput, "Recover workspace management", "app.admin", "recoverWorkspaceManager", "POST", "/api/v1/admin/workspace-recovery/{workspace_id}/manager", "An Administrator can promote one existing active Member to Manager only when a shared workspace has no active Manager. Requires the current workspace revision, app.read, app.write and app.admin; records an audit event and adds no membership or content access for the Administrator."),
)
WORKSPACE_OPERATION_BY_ACTION = {operation.action: operation for operation in WORKSPACE_OPERATIONS}
