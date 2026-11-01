"""Small HTTP contracts for opt-in collaboration and workspace governance."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InstallationModeResponse(BaseModel):
    mode: Literal["solo", "collaboration"]
    revision: int
    collaboration_available: bool
    collaboration_enabled: bool


class EnableCollaborationRequest(_Request):
    expected_revision: StrictInt = Field(ge=0)


class WorkspaceCreateRequest(_Request):
    name: StrictStr = Field(min_length=1, max_length=255)


class WorkspaceRevisionRequest(_Request):
    expected_revision: StrictInt = Field(ge=1)


class WorkspaceRenameRequest(WorkspaceRevisionRequest):
    name: StrictStr = Field(min_length=1, max_length=255)


class WorkspaceRoleRequest(WorkspaceRevisionRequest):
    role: Literal["manager", "member"]


class WorkspaceInviteRequest(WorkspaceRoleRequest):
    user_id: StrictStr = Field(min_length=1, max_length=36)


class WorkspaceResponse(BaseModel):
    id: str
    kind: Literal["personal", "team"]
    name: str
    state: Literal["active", "archived"]
    revision: int
    role: Literal["owner", "manager", "member"]
    allowed_actions: list[Literal["read", "edit", "manage"]]
    created_at: datetime
    updated_at: datetime


class WorkspaceListResponse(BaseModel):
    items: list[WorkspaceResponse]


class WorkspaceMemberResponse(BaseModel):
    user_id: str
    display_name: str
    login_name: str
    role: Literal["owner", "manager", "member"]
    is_active: bool


class WorkspaceMembersResponse(BaseModel):
    items: list[WorkspaceMemberResponse]


class WorkspaceInvitationResponse(BaseModel):
    id: str
    workspace_id: str
    workspace_name: str
    user_id: str
    display_name: str
    invited_by_user_id: str
    invited_by_display_name: str
    role: Literal["manager", "member"]
    state: Literal["pending", "accepted", "revoked", "declined", "expired"]
    revision: int
    created_at: datetime
    expires_at: datetime
    responded_at: datetime | None


class WorkspaceInvitationsResponse(BaseModel):
    items: list[WorkspaceInvitationResponse]


class WorkspaceRemovalResponse(BaseModel):
    workspace_id: str
    user_id: str
    revision: int
    removed: bool


class WorkspaceUserSearchResponse(BaseModel):
    items: list[dict[str, str]]


class WorkspaceRecoveryQuery(_Request):
    limit: int = Field(default=50, ge=1, le=100)
    offset: int = Field(default=0, ge=0, le=100_000)


class WorkspaceManagerRecoveryRequest(WorkspaceRevisionRequest):
    user_id: StrictStr = Field(min_length=1, max_length=36)


class WorkspaceRecoveryCandidate(BaseModel):
    user_id: str
    display_name: str


class WorkspaceRecoveryItem(BaseModel):
    id: str
    name: str
    state: Literal["active", "archived"]
    revision: int
    candidates: list[WorkspaceRecoveryCandidate]


class WorkspaceRecoveryList(BaseModel):
    items: list[WorkspaceRecoveryItem]
    has_more: bool
    limit: int
    offset: int


class WorkspaceManagerRecoveryResponse(BaseModel):
    id: str
    name: str
    state: Literal["active", "archived"]
    revision: int
    recovered_user_id: str


WORKSPACE_SCHEMAS = {
    model.__name__: model
    for model in (
        InstallationModeResponse, EnableCollaborationRequest,
        WorkspaceCreateRequest, WorkspaceRevisionRequest, WorkspaceRenameRequest,
        WorkspaceRoleRequest, WorkspaceInviteRequest, WorkspaceResponse,
        WorkspaceListResponse, WorkspaceMembersResponse, WorkspaceInvitationResponse,
        WorkspaceInvitationsResponse, WorkspaceRemovalResponse, WorkspaceUserSearchResponse,
        WorkspaceRecoveryQuery, WorkspaceManagerRecoveryRequest, WorkspaceRecoveryCandidate,
        WorkspaceRecoveryItem, WorkspaceRecoveryList, WorkspaceManagerRecoveryResponse,
    )
}
