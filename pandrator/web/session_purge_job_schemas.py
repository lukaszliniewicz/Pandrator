"""Strict admission contracts for durable session purge maintenance."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, StrictInt

from .schemas import StrictModel


class PurgeInspectionRequest(StrictModel):
    pass


class PurgeAdmissionRequest(StrictModel):
    expected_revision: StrictInt = Field(ge=0)
    impact_token: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-fA-F]{64}$")
    confirm: Literal[True]


class PurgeJobReceipt(StrictModel):
    job_id: str
    state: Literal["queued"]
    target_session_id: str


class PurgeInspectionStatus(StrictModel):
    job_id: str
    state: str
    inspection: dict[str, Any] | None = None
    error: dict[str, Any] | None = None


SESSION_PURGE_JOB_SCHEMAS = {
    model.__name__: model
    for model in (
        PurgeInspectionRequest, PurgeAdmissionRequest,
        PurgeJobReceipt, PurgeInspectionStatus,
    )
}
