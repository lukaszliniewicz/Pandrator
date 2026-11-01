"""Typed admission commands for explicitly supported HTTP job operations."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictBool, StrictStr, field_validator
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, AccessDenied, ProjectAction
from .access.private_workspace_policy import PrivateWorkAction, require_private_workspace
from .access.session_bundle_policy import require_bundle_import_creation_access
from .access.session_policy import require_session_access
from .accounts.execution_actor import execution_actor_scope
from .jobs import JobQueue
from .models import Job


class DiagnosticJobPayload(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        strict=True,
        frozen=True,
        allow_inf_nan=False,
    )

    duration: float = Field(default=0.0, ge=0, le=30, strict=False)
    echo: JsonValue | None = None

    @field_validator("duration", mode="before")
    @classmethod
    def numeric_duration(cls, value: object) -> object:
        if type(value) not in (int, float):
            raise ValueError("duration must be a finite number from 0 to 30.")
        return value


class BundleExportJobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    session_id: StrictStr = Field(min_length=1, max_length=80)
    include_sources: StrictBool = True

    @field_validator("session_id")
    @classmethod
    def trimmed_session_id(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("session_id must be a nonempty trimmed string.")
        return value


def parse_diagnostic_payload(payload: dict[str, Any]) -> DiagnosticJobPayload:
    """Validate a diagnostic payload while discarding only legacy actor claims."""
    copied = dict(payload)
    copied.pop("actor_user_id", None)
    copied.pop("actor_source", None)
    return DiagnosticJobPayload.model_validate(copied)


def _require_context_and_command(context: object, command: object, model: type[BaseModel]) -> None:
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(command) is not model:
        raise TypeError("A valid job command is required.")


def _require_max_attempts(max_attempts: object) -> int:
    if type(max_attempts) is not int or not 1 <= max_attempts <= 10:
        raise ValueError("max_attempts must be an integer from 1 to 10.")
    return max_attempts


def enqueue_diagnostic_in_session(
    session: Session,
    context: AccessContext,
    queue: JobQueue,
    command: DiagnosticJobPayload,
    *,
    session_id: str | None = None,
    max_attempts: int = 1,
) -> Job:
    """Authorize a diagnostic root and enqueue its canonical command."""
    _require_context_and_command(context, command, DiagnosticJobPayload)
    attempts = _require_max_attempts(max_attempts)
    if session_id is not None and type(session_id) is not str:
        raise TypeError("session_id must be a string or None.")

    if session_id is None:
        workspace_id = require_private_workspace(
            session,
            context,
            PrivateWorkAction.RUN,
        )
        project_id = None
    else:
        access = require_session_access(session, context, session_id, ProjectAction.RUN)
        workspace_id = access.workspace_id
        project_id = access.project_id

    payload = command.model_dump(exclude_unset=True)
    with execution_actor_scope(context.actor):
        return queue.enqueue_in_session(
            session,
            "noop",
            payload,
            session_id=session_id,
            max_attempts=attempts,
            workspace_id=workspace_id,
            project_id=project_id,
        )


def enqueue_bundle_export_in_session(
    session: Session,
    context: AccessContext,
    queue: JobQueue,
    command: BundleExportJobPayload,
    *,
    max_attempts: int = 1,
) -> Job:
    """Authorize the session's current project before queuing a bundle export."""
    _require_context_and_command(context, command, BundleExportJobPayload)
    attempts = _require_max_attempts(max_attempts)
    access = require_session_access(
        session,
        context,
        command.session_id,
        ProjectAction.RUN,
    )

    payload = {
        "session_id": command.session_id,
        "include_sources": command.include_sources,
    }
    with execution_actor_scope(context.actor):
        return queue.enqueue_in_session(
            session,
            "session.bundle.export",
            payload,
            session_id=command.session_id,
            max_attempts=attempts,
            workspace_id=access.workspace_id,
            project_id=access.project_id,
        )


def enqueue_bundle_import_in_session(
    session: Session,
    context: AccessContext,
    queue: JobQueue,
    *,
    source_artifact_id: str,
    name: str | None = None,
) -> Job:
    """Authorize a workspace source and queue its bundle import."""
    if type(context) is not AccessContext:
        raise TypeError("An access context is required.")
    if type(source_artifact_id) is not str:
        raise TypeError("source_artifact_id must be a string.")
    if name is not None and type(name) is not str:
        raise TypeError("name must be a string or None.")

    with execution_actor_scope(context.actor):
        workspace_id = require_bundle_import_creation_access(
            session,
            source_artifact_id,
        )
        if workspace_id is None:
            raise AccessDenied()
        return queue.enqueue_in_session(
            session,
            "session.bundle.import",
            {"source_artifact_id": source_artifact_id, "name": name},
            workspace_id=workspace_id,
        )


__all__ = [
    "BundleExportJobPayload",
    "DiagnosticJobPayload",
    "enqueue_bundle_export_in_session",
    "enqueue_bundle_import_in_session",
    "enqueue_diagnostic_in_session",
    "parse_diagnostic_payload",
]
