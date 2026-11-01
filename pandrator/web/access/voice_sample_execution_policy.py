"""Current captured actor, lease and workspace proof for voice sample workers."""

from __future__ import annotations

import json
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..accounts.credential_resources import credential_workspace_clause
from ..accounts.execution_actor import HostActor, UserActor, current_execution_actor
from ..accounts.job_authorization import require_execution_actor_in_session
from ..accounts.models import User, Workspace
from ..job_execution import current_job_lease
from ..models import Job, JobActorRecord, utcnow
from ..voice_execution_identity import voice_execution_workspace
from .contracts import AccessDenied, ProjectAction
from .project_visibility import workspace_authority

_KINDS = frozenset({"voice.normalize_recording", "voice.transcribe", "voice.publish", "voice.unpublish", "voice.google_design", "tts.preview"})
_INJECTED = frozenset({"_job_id", "_lease_generation"})


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def _json_value(value: Any) -> bool:
    if value is None or type(value) in {str, int, float, bool}:
        return True
    if type(value) is list:
        return all(_json_value(item) for item in value)
    if type(value) is dict:
        return all(type(key) is str and _json_value(item) for key, item in value.items())
    return False


def require_voice_sample_execution_workspace(
    session: Session,
    payload: dict[str, Any],
    kind: Literal["voice.normalize_recording", "voice.transcribe", "voice.publish", "voice.unpublish", "voice.google_design", "tts.preview"],
    *,
    voice_id: str | None = None,
    sample_ids: Iterable[str] = (),
    artifact_ids: Iterable[str] = (),
    expected_workspace_id: str | None = None,
) -> Workspace:
    if type(kind) is not str or kind not in _KINDS or type(payload) is not dict:
        raise AccessDenied()
    actor = current_execution_actor.get()
    lease = current_job_lease.get()
    with session.no_autoflush:
        if lease is None:
            if type(actor) is not HostActor:
                raise AccessDenied()
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=("app.read", "app.write", "app.run"),
                    allow_host=True,
                )
            except PermissionError:
                raise AccessDenied() from None
            return voice_execution_workspace(
                session,
                payload,
                voice_id=voice_id,
                sample_ids=sample_ids,
                artifact_ids=artifact_ids,
                expected_workspace_id=expected_workspace_id,
            )
        if (
            type(lease) is not dict
            or lease.keys() != {"job_id", "owner_id", "lease_generation"}
            or type(lease.get("job_id")) is not str
            or not lease["job_id"]
            or type(lease.get("owner_id")) is not str
            or not lease["owner_id"]
            or type(lease.get("lease_generation")) is not int
            or lease["lease_generation"] < 1
            or type(payload.get("_job_id")) is not str
            or payload["_job_id"] != lease["job_id"]
            or type(payload.get("_lease_generation")) is not int
            or payload["_lease_generation"] != lease["lease_generation"]
        ):
            raise AccessDenied()
        header = session.execute(
            select(
                Job.id,
                Job.kind,
                Job.workspace_id,
                Job.project_id,
                Job.session_id,
                Job.workflow_run_id,
                Job.status,
                Job.lease_owner,
                Job.lease_generation,
                Job.lease_expires_at,
            ).where(Job.id == lease["job_id"])
        ).one_or_none()
        if (
            header is None
            or header.kind != kind
            or header.project_id is not None
            or header.session_id is not None
            or header.workflow_run_id is not None
            or header.status != "running"
            or header.lease_owner != lease["owner_id"]
            or type(header.lease_generation) is not int
            or header.lease_generation != lease["lease_generation"]
            or not isinstance(header.lease_expires_at, datetime)
            or _utc(header.lease_expires_at) <= _utc(utcnow())
        ):
            raise AccessDenied()
        clauses = [Workspace.id == header.workspace_id, Workspace.state == "active"]
        if type(actor) is UserActor or type(actor) is HostActor:
            try:
                require_execution_actor_in_session(
                    session,
                    actor,
                    required_scopes=("app.read", "app.write", "app.run"),
                    allow_host=type(actor) is HostActor,
                )
            except PermissionError:
                raise AccessDenied() from None
            if type(actor) is UserActor:
                clauses += [
                    workspace_authority(actor.user_id, action=ProjectAction.EDIT),
                    credential_workspace_clause(actor),
                ]
                if (
                    type(payload.get("workspace_id")) is not str
                    or payload["workspace_id"] != header.workspace_id
                ):
                    raise AccessDenied()
        elif actor is None:
            if (
                session.scalar(
                    select(JobActorRecord.job_id).where(JobActorRecord.job_id == header.id)
                )
                is not None
            ):
                raise AccessDenied()
            clauses.append(
                Workspace.personal_owner_user_id.in_(
                    select(User.id).where(User.legacy_subject == "owner")
                )
            )
            clauses.append(Workspace.kind == "personal")
        else:
            raise AccessDenied()
        workspace = session.scalar(
            select(Workspace).where(*clauses).execution_options(populate_existing=True)
        )
        if workspace is None:
            raise AccessDenied()
        stored = session.scalar(
            select(Job.payload_json).where(
                Job.id == header.id,
                Job.kind == kind,
                Job.workspace_id == workspace.id,
                Job.project_id.is_(None),
                Job.session_id.is_(None),
                Job.workflow_run_id.is_(None),
                Job.status == "running",
                Job.lease_owner == lease["owner_id"],
                Job.lease_generation == lease["lease_generation"],
            )
        )
        try:
            supplied = {key: value for key, value in payload.items() if key not in _INJECTED}
            same = (
                type(stored) is dict
                and _json_value(stored)
                and _json_value(supplied)
                and json.dumps(stored, sort_keys=True, separators=(",", ":"), allow_nan=False)
                == json.dumps(supplied, sort_keys=True, separators=(",", ":"), allow_nan=False)
            )
        except (TypeError, ValueError):
            same = False
        if not same:
            raise AccessDenied()
        if expected_workspace_id is not None and expected_workspace_id != workspace.id:
            raise ValueError("Voice processing inputs have inconsistent workspace ownership.")
        return voice_execution_workspace(
            session,
            payload,
            voice_id=voice_id,
            sample_ids=sample_ids,
            artifact_ids=artifact_ids,
            expected_workspace_id=workspace.id,
        )
