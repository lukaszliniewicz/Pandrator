"""Revision-safe forks and independent multilingual translation branches."""

from typing import Annotated, Literal

from pydantic import Field, StrictBool, StrictInt, StrictStr, field_validator

from .common import ToolInput

_KEY = r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$"


class ForkSessionInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    checkpoint_artifact_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(pattern=_KEY)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    carry_media_assets: bool = True
    target_language: str | None = Field(default=None, min_length=2, max_length=40)


class GetTranslationProjectInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)


class CreateTranslationProjectInput(ToolInput):
    session_id: str = Field(min_length=1, max_length=80)
    checkpoint_artifact_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    idempotency_key: str = Field(pattern=_KEY)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    create_planned_branches: bool = False


class TranslationBranchTargetInput(ToolInput):
    target_language: str = Field(min_length=2, max_length=40)
    name: str | None = Field(default=None, min_length=1, max_length=255)
    carry_source_subtitle_settings: StrictBool = False


class CreateTranslationBranchesInput(ToolInput):
    project_id: str = Field(min_length=1, max_length=80)
    expected_revision: int = Field(ge=1)
    targets: list[TranslationBranchTargetInput] = Field(min_length=1, max_length=20)
    idempotency_key: str = Field(pattern=_KEY)


class PreviewTranslationProjectOperationInput(ToolInput):
    project_id: StrictStr = Field(min_length=1, max_length=80)
    selected_branch_ids: list[Annotated[StrictStr, Field(min_length=1, max_length=80)]] = Field(
        min_length=1,
        max_length=20,
    )
    expected_project_revision: StrictInt = Field(ge=1)
    action: Literal["translate", "generate", "export"]
    export_kind: Literal["configured", "subtitles"] = "configured"
    idempotency_key: StrictStr = Field(min_length=8, max_length=200, pattern=_KEY)

    @field_validator("selected_branch_ids")
    @classmethod
    def selected_branches_are_unique(cls, values: list[str]) -> list[str]:
        if len(set(values)) != len(values):
            raise ValueError("selected_branch_ids must contain unique branch IDs")
        return values


class GetTranslationProjectOperationInput(ToolInput):
    operation_id: StrictStr = Field(min_length=1, max_length=80)


class ExecuteTranslationProjectOperationInput(ToolInput):
    operation_id: StrictStr = Field(min_length=1, max_length=80)
    preview_digest: StrictStr = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    accepted_confirmations: list[StrictStr] = Field(default_factory=list, max_length=20)
    idempotency_key: StrictStr = Field(min_length=8, max_length=200, pattern=_KEY)


class CancelTranslationProjectOperationInput(ToolInput):
    operation_id: StrictStr = Field(min_length=1, max_length=80)
    idempotency_key: StrictStr = Field(min_length=8, max_length=200, pattern=_KEY)


class RetryTranslationProjectOperationPreviewInput(ToolInput):
    operation_id: StrictStr = Field(min_length=1, max_length=80)
    expected_project_revision: StrictInt = Field(ge=1)
    idempotency_key: StrictStr = Field(min_length=8, max_length=200, pattern=_KEY)


class GetTranslationProjectExportManifestInput(ToolInput):
    operation_id: StrictStr = Field(min_length=1, max_length=80)


class RequestTranslationProjectExportBundleInput(ToolInput):
    operation_id: StrictStr = Field(min_length=1, max_length=80)
    expected_manifest_digest: StrictStr = Field(
        min_length=64,
        max_length=64,
        pattern=r"^[0-9a-f]{64}$",
    )
    idempotency_key: StrictStr = Field(min_length=8, max_length=200, pattern=_KEY)


SESSION_BRANCH_INPUT_MODELS = (
    ForkSessionInput,
    GetTranslationProjectInput,
    CreateTranslationProjectInput,
    CreateTranslationBranchesInput,
    PreviewTranslationProjectOperationInput,
    GetTranslationProjectOperationInput,
    ExecuteTranslationProjectOperationInput,
    CancelTranslationProjectOperationInput,
    RetryTranslationProjectOperationPreviewInput,
    GetTranslationProjectExportManifestInput,
    RequestTranslationProjectExportBundleInput,
)
