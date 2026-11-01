"""Parent-owned copy tool descriptions and fixed public HTTP bindings."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True, slots=True)
class ProjectCopyOperation:
    action: str
    model_name: str
    title: str
    risk: Literal["read", "run"]
    scope: str
    operation_id: str
    method: Literal["GET", "POST"]
    path: str
    description: str
    idempotency: bool = False

    @property
    def name(self) -> str:
        return f"pandrator_{self.action}"


PROJECT_COPY_OPERATIONS = (
    ProjectCopyOperation(
        "preview_project_copy",
        "PreviewProjectCopyInput",
        "Preview a project copy",
        "read",
        "app.write",
        "previewProjectCopy",
        "POST",
        "/api/v1/projects/{project_id}/copy-preview",
        "Inspect what can be copied into an approved destination workspace. Use "
        "the generic project_id from a session, not a translation-project or "
        "session ID. Requires source editing and destination creation rights. "
        "The preview reports safe counts, blockers, destination model choices and "
        "a fingerprint. Supply destination_connections using its requirement IDs "
        "and approved provider/model row IDs, then review the ready preview. "
        "This read creates no job and reveals no private credentials or file paths.",
    ),
    ProjectCopyOperation(
        "copy_current_project",
        "CopyCurrentProjectInput",
        "Copy current project",
        "run",
        "app.run",
        "copyCurrentProject",
        "POST",
        "/api/v1/projects/{project_id}/copy",
        "Copy the saved state approved by a ready preview into a workspace, "
        "retaining the original. Supply its fingerprint and a fresh idempotency "
        "key and the same destination_connections reviewed in that preview. "
        "Requires app.read, app.write and app.run, source editing and "
        "destination creation authority. Unsupported editorial state or unresolved "
        "private connections block copying. Follow the returned ID with "
        "pandrator_get_project_copy; generic job output withholds private results.",
        True,
    ),
    ProjectCopyOperation(
        "get_project_copy",
        "GetProjectCopyInput",
        "Inspect a project copy",
        "read",
        "app.read",
        "getProjectCopy",
        "GET",
        "/api/v1/project-copies/{copy_id}",
        "Read your copy's current progress or completed project link. Only the "
        "initiating account, with current approved source and destination access, "
        "can retrieve the result. No internal snapshot, text, settings or "
        "filesystem paths are returned.",
    ),
)
PROJECT_COPY_OPERATION_BY_ACTION = {
    operation.action: operation for operation in PROJECT_COPY_OPERATIONS
}
