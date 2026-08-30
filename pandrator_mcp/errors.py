"""Typed failures shared by CLI, clients, and MCP tool envelopes."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

FailureCode = Literal[
    "application_unavailable",
    "manager_unavailable",
    "recovery_enrollment_required",
    "authentication_required",
    "scope_denied",
    "target_identity_mismatch",
    "network_policy_denied",
    "tls_validation_failed",
    "not_found",
    "revision_conflict",
    "response_too_large",
    "run_not_claimable",
    "idempotency_conflict",
    "idempotency_in_progress",
    "idempotency_key_required",
    "plan_stale",
    "plan_consumed",
    "plan_digest_mismatch",
    "plan_expired",
    "plan_invalid",
    "confirmation_required",
    "batch_completed",
    "dispatch_busy",
    "dispatch_sequential",
    "duplicate_session",
    "finalization_conflict",
    "finalization_incomplete",
    "ineligible_source",
    "invalid_kind",
    "invalid_model_response",
    "invalid_output_role",
    "lease_conflict",
    "lease_expired",
    "precondition_required",
    "result_kind_mismatch",
    "session_busy",
    "source_changed",
    "source_deleted",
    "source_hash_unavailable",
    "source_hash_missing",
    "source_language_mismatch",
    "source_language_missing",
    "source_not_found",
    "source_revision_missing",
    "source_revision_mismatch",
    "source_segments_invalid",
    "source_session_mismatch",
    "source_unavailable",
    "source_unmaterialized",
    "target_language_required",
    "validation_error",
    "rate_limited",
    "downstream_unavailable",
    "incompatible_downstream",
    "internal_error",
]


class NextAction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str


class ToolFailure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: FailureCode
    message: str
    request_id: str
    details: dict[str, Any] = Field(default_factory=dict)
    retryable: bool = False
    next_actions: list[NextAction] = Field(default_factory=list)


class PandratorMcpError(RuntimeError):
    """Internal typed exception converted to a model-safe ToolFailure."""

    def __init__(
        self,
        code: FailureCode,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})
        self.retryable = retryable


class TargetResolutionError(PandratorMcpError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(
            "network_policy_denied",
            message,
            details=details,
        )


class CredentialResolutionError(PandratorMcpError):
    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(
            "authentication_required",
            message,
            details=details,
        )
