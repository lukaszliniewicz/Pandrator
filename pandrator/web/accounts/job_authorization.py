"""Revalidate captured actors only inside a verified worker lease."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..job_execution import current_job_lease
from ..model_base import utcnow
from ..models import Job, JobActorRecord
from .actor_authorization import (
    _client_is_current,
    _deny,
    _resolve_api_token,
    require_actor_in_session,
)
from .auth_state import AccountState, account_state_in_session
from .execution_actor import (
    _MANAGER_BROWSER_CLIENT_IDS,
    ExecutionActor,
    HostActor,
    UserActor,
    _scope_set,
    _validate_user_actor,
    actor_from_storage,
)

_LEASE_KEYS = frozenset({"job_id", "owner_id", "lease_generation"})


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _valid_lease_value(value: object) -> bool:
    return type(value) is str and bool(value) and value == value.strip()


def _prove_active_job_actor(
    session: Session,
    lease: object,
    actor: ExecutionActor | None,
    now: datetime,
) -> ExecutionActor:
    if (
        type(lease) is not dict
        or lease.keys() != _LEASE_KEYS
        or not _valid_lease_value(lease.get("job_id"))
        or not _valid_lease_value(lease.get("owner_id"))
        or type(lease.get("lease_generation")) is not int
        or lease["lease_generation"] <= 0
    ):
        raise _deny()

    job_id = lease["job_id"]
    owner_id = lease["owner_id"]
    generation = lease["lease_generation"]
    with session.no_autoflush:
        job = session.execute(
            select(
                Job.status,
                Job.lease_owner,
                Job.lease_generation,
                Job.lease_expires_at,
            ).where(Job.id == job_id)
        ).one_or_none()
        if (
            job is None
            or job.status != "running"
            or job.lease_owner != owner_id
            or type(job.lease_generation) is not int
            or job.lease_generation != generation
            or type(job.lease_expires_at) is not datetime
            or _aware(job.lease_expires_at) <= now
        ):
            raise _deny()

        record = session.execute(
            select(
                JobActorRecord.user_id,
                JobActorRecord.auth_revision,
                JobActorRecord.context_json,
            ).where(JobActorRecord.job_id == job_id)
        ).one_or_none()
        if record is None:
            raise _deny()
        try:
            saved_actor = actor_from_storage(
                record.user_id,
                record.auth_revision,
                record.context_json,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise _deny() from error
        if saved_actor is None or actor is None or saved_actor != actor:
            raise _deny()
        return saved_actor


def require_current_job_actor_lease(
    session: Session,
    actor: ExecutionActor | None,
) -> ExecutionActor:
    """Prove only a worker's live lease and saved actor for terminal bookkeeping.

    This grants no account, resource, file or processing authority. Callers must
    bind their own domain record to this job before recording terminal failure.
    """
    return _prove_active_job_actor(session, current_job_lease.get(), actor, _aware(utcnow()))


def require_execution_actor_in_session(
    session: Session,
    actor: ExecutionActor | None,
    *,
    required_scopes: object = (),
    require_admin: bool = False,
    allow_host: bool = False,
) -> AccountState | None:
    """Use captured authority only when the current worker lease proves it."""
    lease = current_job_lease.get()
    if lease is None:
        return require_actor_in_session(
            session,
            actor,
            required_scopes=required_scopes,
            require_admin=require_admin,
            allow_host=allow_host,
        )

    if type(require_admin) is not bool or type(allow_host) is not bool:
        raise _deny()
    now = _aware(utcnow())
    _prove_active_job_actor(session, lease, actor, now)

    if actor is None:
        raise _deny()
    if type(actor) is HostActor:
        if actor.label != "pandrator-cli" or not allow_host:
            raise _deny()
        return None
    if type(actor) is not UserActor:
        raise _deny()

    try:
        _validate_user_actor(actor)
        required = _scope_set(required_scopes, allow_empty=True)
    except ValueError as error:
        raise _deny() from error

    with session.no_autoflush:
        try:
            account = account_state_in_session(
                session,
                actor.user_id,
                auth_revision=actor.auth_revision,
                require_admin=require_admin or "app.admin" in actor.scopes,
            )
        except PermissionError as error:
            raise _deny() from error

        if actor.token_id is not None:
            current_scopes = _resolve_api_token(
                session,
                actor,
                now,
                require_unexpired=False,
            )
        else:
            current_scopes = actor.scopes
            if (
                actor.client_id is not None
                and actor.client_id not in _MANAGER_BROWSER_CLIENT_IDS
                and not _client_is_current(session, actor, actor.client_id)
            ):
                current_scopes = None

    if current_scopes is None or not required <= actor.scopes or not required <= current_scopes:
        raise _deny()
    return account


__all__ = ["require_current_job_actor_lease", "require_execution_actor_in_session"]
