"""Strict, bounded inputs for owner-managed public project links."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import ConfigDict, Field, model_validator

from .common import ToolInput

_Identifier = Annotated[str, Field(min_length=1, max_length=36)]
_UUID = r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"


class PublicLinkToolInput(ToolInput):
    model_config = ConfigDict(extra="forbid", strict=True)


class GetPublicSharingSettingsInput(PublicLinkToolInput):
    """Input for reading public-sharing configuration."""


class PublicSharingSettingsInput(PublicLinkToolInput):
    expected_revision: int = Field(ge=0)
    enabled: bool
    public_origin: str | None = Field(max_length=2048)


class ProjectLinksInput(PublicLinkToolInput):
    project_id: _Identifier


class CreateProjectLinkInput(ProjectLinksInput):
    label: str = Field(min_length=1, max_length=80)
    mode: Literal["manage_project", "review_translation"]
    expires_in_days: int = Field(default=7, ge=1, le=30)
    target_artifact_id: _Identifier | None = None
    request_key: str = Field(pattern=_UUID)

    @model_validator(mode="after")
    def validate_mode_target(self) -> CreateProjectLinkInput:
        if self.mode == "manage_project" and self.target_artifact_id is not None:
            raise ValueError("Manager project links cannot target an artifact.")
        if self.mode == "review_translation" and self.target_artifact_id is None:
            raise ValueError("Translation review links require a target artifact.")
        return self


class RevokeProjectLinkInput(PublicLinkToolInput):
    link_id: _Identifier
    expected_revision: int = Field(ge=1)


class ListLinkSubmissionsInput(ProjectLinksInput):
    status: Literal["pending", "accepted", "rejected"] = "pending"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=25, ge=1, le=50)


class GetLinkSubmissionInput(PublicLinkToolInput):
    submission_id: _Identifier
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=50, ge=1, le=100)


class ReviewLinkSubmissionInput(PublicLinkToolInput):
    submission_id: _Identifier
    action: Literal["accept", "reject"]
    expected_revision: int = Field(ge=1)
    expected_source_hash: str | None = Field(default=None, max_length=128)
    expected_document_revision: int | None = Field(default=None, ge=1)
    expected_selected_artifact_id: _Identifier | None = None
    idempotency_key: str = Field(pattern=_UUID)

    @model_validator(mode="after")
    def validate_review_guards(self) -> ReviewLinkSubmissionInput:
        guarded_fields = (
            "expected_source_hash",
            "expected_document_revision",
            "expected_selected_artifact_id",
        )
        if self.action == "accept":
            missing = [name for name in guarded_fields if getattr(self, name) is None]
            if missing:
                raise ValueError("Accepting a submission requires all expected source guards.")
        elif any(name in self.model_fields_set for name in guarded_fields):
            raise ValueError("Rejecting a submission does not accept source guards.")
        return self


PUBLIC_LINK_ACTION_INPUT_MODELS = {
    "get_public_sharing_settings": GetPublicSharingSettingsInput,
    "configure_public_sharing": PublicSharingSettingsInput,
    "list_project_links": ProjectLinksInput,
    "create_project_link": CreateProjectLinkInput,
    "revoke_project_link": RevokeProjectLinkInput,
    "list_link_submissions": ListLinkSubmissionsInput,
    "get_link_submission": GetLinkSubmissionInput,
    "review_link_submission": ReviewLinkSubmissionInput,
}

PUBLIC_LINK_INPUT_MODELS = tuple(PUBLIC_LINK_ACTION_INPUT_MODELS.values())

__all__ = [
    "CreateProjectLinkInput",
    "GetLinkSubmissionInput",
    "GetPublicSharingSettingsInput",
    "ListLinkSubmissionsInput",
    "PUBLIC_LINK_ACTION_INPUT_MODELS",
    "PUBLIC_LINK_INPUT_MODELS",
    "ProjectLinksInput",
    "PublicLinkToolInput",
    "PublicSharingSettingsInput",
    "ReviewLinkSubmissionInput",
    "RevokeProjectLinkInput",
]
