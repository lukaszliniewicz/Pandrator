"""Pydantic request schemas for the manager HTTP boundary."""

from __future__ import annotations

from typing import Any

from pydantic import Field, SecretStr

from ..models import (
    DesiredComponentState,
    OperationKind,
    StrictModel,
)
from ..network import EndpointExposure


class PlanRequest(StrictModel):
    kind: OperationKind
    desired: dict[str, DesiredComponentState]
    expected_revision: int | None = Field(default=None, ge=0)


class OperationRequest(StrictModel):
    plan_id: str = Field(min_length=1)
    plan_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    accepted_confirmations: tuple[str, ...] = ()


class RuntimeRequest(StrictModel):
    service_ids: tuple[str, ...] = ()


class RecoveryExchangeRequest(StrictModel):
    token: str = Field(min_length=32, max_length=256)
    remember: bool = True


class AutomationEnrollmentGrantRequest(StrictModel):
    client_id: str = Field(min_length=36, max_length=36)
    client_name: str = Field(min_length=1, max_length=120)
    subject: str = Field(min_length=1, max_length=200)
    application_instance_id: str = Field(min_length=1, max_length=120)
    canonical_application_origin: str = Field(
        min_length=8,
        max_length=2048,
    )
    canonical_recovery_origin: str = Field(
        min_length=8,
        max_length=2048,
    )
    requested_scopes: tuple[str, ...] = Field(
        min_length=1,
        max_length=3,
    )
    expires_in_seconds: int = Field(
        ge=300,
        le=30 * 24 * 60 * 60,
    )
    code_challenge: str = Field(min_length=43, max_length=128)
    code_challenge_method: str = Field(pattern=r"^S256$")


class AutomationTokenRequest(StrictModel):
    client_id: str = Field(min_length=36, max_length=36)
    grant_code: str = Field(min_length=32, max_length=256)
    code_verifier: str = Field(min_length=43, max_length=128)
    manager_instance_id: str = Field(min_length=1, max_length=120)


class ApplicationNetworkRequest(StrictModel):
    exposure: EndpointExposure
    owner_password: SecretStr | None = None
    replace_owner_password: bool = False
    restart_if_running: bool = True


class ReleasePlanRequest(StrictModel):
    manifest: dict[str, Any]
    expected_revision: int | None = Field(default=None, ge=0)
    offline: bool = False
    start_after_activation: bool = True


class UninstallPlanRequest(StrictModel):
    expected_revision: int | None = Field(default=None, ge=0)
    purge_data: bool = False
    export_data: str | None = Field(default=None, min_length=1, max_length=4096)


class LegacyImportRequest(StrictModel):
    source_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmed: bool = False
