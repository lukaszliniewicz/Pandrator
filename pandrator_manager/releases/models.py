"""Strict schemas for the signed release envelope."""

from __future__ import annotations

from datetime import datetime
from pathlib import PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from packaging.version import InvalidVersion, Version
from pydantic import Field, field_validator, model_validator

from ..models import StrictModel


class TrustedReleaseKey(StrictModel):
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    algorithm: Literal["ed25519"] = "ed25519"
    public_key: str = Field(min_length=40, max_length=100)


class KeyRotation(StrictModel):
    activates_at_sequence: int = Field(ge=1)
    threshold: int = Field(default=1, ge=1, le=8)
    keys: tuple[TrustedReleaseKey, ...]

    @model_validator(mode="after")
    def validate_rotation(self) -> "KeyRotation":
        if not self.keys:
            raise ValueError("A key rotation must authorize at least one key.")
        ids = [key.key_id for key in self.keys]
        if len(ids) != len(set(ids)):
            raise ValueError("A key rotation contains duplicate key IDs.")
        if self.threshold > len(self.keys):
            raise ValueError("The key threshold exceeds the number of keys.")
        return self


class ReleaseArtifact(StrictModel):
    filename: str = Field(pattern=r"^[^/\\\x00]{1,240}$")
    url: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(gt=0)
    kind: Literal["wheel", "zip", "tar", "executable", "appimage"]
    systems: tuple[str, ...]
    architectures: tuple[str, ...]
    python_tags: tuple[str, ...] = ()

    @field_validator("url")
    @classmethod
    def https_only(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.fragment
        ):
            raise ValueError("Release artifact URLs must be HTTPS URLs.")
        return value

    @field_validator("systems", "architectures")
    @classmethod
    def non_empty_targets(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError("Release artifacts need explicit target selectors.")
        return value


class ReleasePayload(StrictModel):
    schema_version: Literal[1] = 1
    product: str = Field(pattern=r"^[a-z][a-z0-9_.-]{1,100}$")
    channel: Literal["stable", "beta", "nightly"] = "stable"
    version: str
    sequence: int = Field(ge=1)
    published_at: datetime
    minimum_manager_version: str | None = None
    artifacts: tuple[ReleaseArtifact, ...]
    key_rotation: KeyRotation | None = None

    @field_validator("version", "minimum_manager_version")
    @classmethod
    def valid_version(cls, value: str | None) -> str | None:
        if value is None:
            return None
        try:
            Version(value)
        except InvalidVersion as error:
            raise ValueError("Release versions must follow PEP 440.") from error
        return value

    @model_validator(mode="after")
    def validate_payload(self) -> "ReleasePayload":
        if not self.artifacts:
            raise ValueError("A release must contain at least one artifact.")
        names = [artifact.filename for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError("A release contains duplicate artifact filenames.")
        if (
            self.key_rotation is not None
            and self.key_rotation.activates_at_sequence <= self.sequence
        ):
            raise ValueError(
                "Rotated keys must activate after the authorizing release."
            )
        return self


class ReleaseSignature(StrictModel):
    key_id: str = Field(pattern=r"^[a-zA-Z0-9_.-]{1,80}$")
    signature: str = Field(min_length=80, max_length=120)


class ReleaseEnvelope(StrictModel):
    signed: ReleasePayload
    signatures: tuple[ReleaseSignature, ...]

    @model_validator(mode="after")
    def validate_signatures(self) -> "ReleaseEnvelope":
        if not self.signatures:
            raise ValueError("A release manifest must contain a signature.")
        ids = [signature.key_id for signature in self.signatures]
        if len(ids) != len(set(ids)):
            raise ValueError("A release manifest contains duplicate signatures.")
        return self


def _safe_bundle_path(value: str) -> str:
    """Normalize a signed bundle-relative path without touching the host."""

    if "\\" in value or "\x00" in value:
        raise ValueError("Release bundle paths must use portable '/' separators.")
    selected = PurePosixPath(value)
    if (
        not value
        or selected.is_absolute()
        or ".." in selected.parts
        or any(part in {"", "."} for part in selected.parts)
    ):
        raise ValueError("Release bundle paths must be safe relative paths.")
    return selected.as_posix()


class ReleaseBundleMetadata(StrictModel):
    """Authenticated archive contract with no caller-controlled commands."""

    schema_version: Literal[1] = 1
    product: Literal["pandrator", "pandrator-manager"]
    version: str
    application_root: str
    python: str
    runtime_kind: Literal["python", "native_launcher"] = "python"

    @field_validator("version")
    @classmethod
    def valid_bundle_version(cls, value: str) -> str:
        try:
            Version(value)
        except InvalidVersion as error:
            raise ValueError("Release bundle versions must follow PEP 440.") from error
        return value

    @field_validator("application_root", "python")
    @classmethod
    def safe_relative_path(cls, value: str) -> str:
        return _safe_bundle_path(value)

    @model_validator(mode="after")
    def validate_runtime_kind(self) -> "ReleaseBundleMetadata":
        if self.product == "pandrator" and self.runtime_kind != "python":
            raise ValueError(
                "Pandrator application bundles require a Python runtime."
            )
        return self
