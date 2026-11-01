"""Strict, bounded inputs for workspace collaboration tools."""
from __future__ import annotations

from typing import Literal

from pydantic import ConfigDict, Field

from .common import ToolInput


class WorkspaceToolInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class ListWorkspacesInput(WorkspaceToolInput):
    include_archived: bool = False


class ListWorkspacesWithoutManagerInput(WorkspaceToolInput):
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100_000)


class GetWorkspaceInput(WorkspaceToolInput):
    workspace_id: str = Field(min_length=1, max_length=36)


class CreateWorkspaceInput(WorkspaceToolInput):
    name: str = Field(min_length=1, max_length=255)
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


class WorkspaceRevisionInput(GetWorkspaceInput):
    expected_revision: int = Field(ge=1)


class RenameWorkspaceInput(WorkspaceRevisionInput):
    name: str = Field(min_length=1, max_length=255)


class WorkspaceMemberInput(WorkspaceRevisionInput):
    user_id: str = Field(min_length=1, max_length=36)


class WorkspaceMemberRoleInput(WorkspaceMemberInput):
    role: Literal["manager", "member"]


class InviteWorkspaceUserInput(WorkspaceMemberRoleInput):
    idempotency_key: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")


class RevokeWorkspaceInvitationInput(WorkspaceRevisionInput):
    invitation_id: str = Field(min_length=1, max_length=36)


class RespondWorkspaceInvitationInput(WorkspaceToolInput):
    invitation_id: str = Field(min_length=1, max_length=36)


class SearchWorkspaceUsersInput(GetWorkspaceInput):
    query: str = Field(min_length=2, max_length=64)


class EnableCollaborationInput(WorkspaceToolInput):
    expected_revision: int = Field(ge=0)


WORKSPACE_INPUT_MODELS = (
    WorkspaceToolInput, ListWorkspacesInput, GetWorkspaceInput, CreateWorkspaceInput,
    WorkspaceRevisionInput, RenameWorkspaceInput, WorkspaceMemberInput,
    WorkspaceMemberRoleInput, InviteWorkspaceUserInput, RevokeWorkspaceInvitationInput,
    RespondWorkspaceInvitationInput, SearchWorkspaceUsersInput, EnableCollaborationInput,
    ListWorkspacesWithoutManagerInput,
)
