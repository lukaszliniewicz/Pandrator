"""Fixed project-viewing tool metadata and HTTP bindings."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel

from .schemas.project_sharing import (
    GetProjectViewersInput,
    ListSharedProjectsInput,
    ProjectViewerMutationInput,
    SearchProjectViewersInput,
)


@dataclass(frozen=True, slots=True)
class ProjectSharingOperation:
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
    def name(self) -> str:
        return f"pandrator_{self.action}"


_ROOT = "/api/v1/projects/{project_id}/viewers"
PROJECT_SHARING_OPERATIONS = (
    ProjectSharingOperation(
        "list_shared_projects", ListSharedProjectsInput, "Projects shared with me",
        "app.read", "listSharedProjects", "GET", "/api/v1/projects/shared-with-me",
        "List projects shared with your account and approved for this credential. "
        "Can view allows reading saved work and downloading existing outputs; "
        "editing and processing require workspace membership.",
    ),
    ProjectSharingOperation(
        "get_project_viewers", GetProjectViewersInput, "Project viewing access",
        "app.read", "getProjectViewers", "GET", _ROOT,
        "A personal owner or shared-workspace Manager can inspect named viewing "
        "access and the project revision. Project readers cannot list recipients "
        "or browse the private workspace.",
    ),
    ProjectSharingOperation(
        "search_project_viewers", SearchProjectViewersInput, "Find a project viewer",
        "app.read", "searchProjectViewers", "GET", _ROOT + "/users",
        "A personal owner or shared-workspace Manager can find existing active "
        "accounts by name or login. Enter at least two characters. Workspace "
        "members already have content access and do not need a viewing grant.",
    ),
    ProjectSharingOperation(
        "grant_project_view", ProjectViewerMutationInput, "Grant Can view access",
        "app.write", "grantProjectView", "PUT", _ROOT + "/{user_id}",
        "A personal owner or shared-workspace Manager can grant one existing "
        "account Can view at the current project revision. This covers all "
        "languages and existing outputs without sharing the workspace or "
        "expanding the recipient's existing API credentials. On a revision "
        "conflict, refresh viewing access before retrying.",
    ),
    ProjectSharingOperation(
        "revoke_project_view", ProjectViewerMutationInput, "Revoke project viewing",
        "app.write", "revokeProjectView", "POST", _ROOT + "/{user_id}/revoke",
        "A personal owner or shared-workspace Manager can revoke named viewing "
        "access and the recipient's project credential approvals at the current "
        "project revision. Stored work is retained. Workspace membership, when "
        "present, still grants workspace content access.",
    ),
)
PROJECT_SHARING_OPERATION_BY_ACTION = {
    operation.action: operation for operation in PROJECT_SHARING_OPERATIONS
}
