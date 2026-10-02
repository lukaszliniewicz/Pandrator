"""Revision-safe forks and independent multilingual translation branches."""

from pydantic import Field, StrictBool

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


SESSION_BRANCH_INPUT_MODELS = (
    ForkSessionInput,
    GetTranslationProjectInput,
    CreateTranslationProjectInput,
    CreateTranslationBranchesInput,
)
