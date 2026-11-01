"""Admission and rollback behavior for synchronous audit replay."""

from __future__ import annotations

import json
import sqlite3
from contextlib import closing
from datetime import datetime
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select
from sqlalchemy.engine import Connection

from pandrator.web.accounts.actor_models import AuditActorRecord
from pandrator.web.accounts.execution_actor import UserActor, actor_from_storage
from pandrator.web.audit import AUDIT_WRITE_TIMEOUT_MS
from pandrator.web.models import AuditEvent
from tests.test_actor_attribution_integration import (
    ActorCase,
    api_principal,
    case,
    record_audit,
)


@pytest.fixture
def actor_case(request: pytest.FixtureRequest) -> ActorCase:
    return request.getfixturevalue(case.__name__)


def _events_for_request(actor_case: ActorCase, request_id: str) -> list[AuditEvent]:
    with actor_case.database.session() as session:
        return list(
            session.scalars(select(AuditEvent).where(AuditEvent.request_id == request_id)).all()
        )


def _actor_for_event(actor_case: ActorCase, event_id: int) -> UserActor | None:
    with actor_case.database.session() as session:
        record = session.get(AuditActorRecord, event_id)
        if record is None:
            return None
        actor = actor_from_storage(record.user_id, record.auth_revision, record.context_json)
        return actor if isinstance(actor, UserActor) else None


def test_record_commits_current_event_and_defers_busy_pending_replay(
    actor_case: ActorCase,
) -> None:
    principal, raw_token = api_principal(actor_case)
    old_request_id = "audit-replay-old-request"
    current_request_id = "audit-replay-current-request"
    with closing(sqlite3.connect(actor_case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        with actor_case.audit.redactor.scoped_values([raw_token]):
            actor_case.audit.record(
                principal=principal,
                request_id=old_request_id,
                traceparent=None,
                action="synthetic.replay",
                method="POST",
                path=f"/synthetic/{raw_token}",
                status_code=201,
                duration_ms=1,
                idempotency_key=raw_token,
                metadata={"retryable": raw_token},
            )
        spool_files = list((actor_case.database.path.parent / "audit-pending").glob("*.json"))
        spool_path = next(
            path
            for path in spool_files
            if json.loads(path.read_text(encoding="utf-8"))["request_id"] == old_request_id
        )
        saved_spool_bytes = spool_path.read_bytes()
        writer.rollback()

    payload = json.loads(saved_spool_bytes)
    assert raw_token not in saved_spool_bytes.decode("utf-8")
    assert payload["path"] == "/synthetic/[REDACTED]"
    assert payload["idempotency_key"] == "[REDACTED]"
    assert payload["metadata_json"]["retryable"] == "[REDACTED]"
    original_created_at = datetime.fromisoformat(payload["created_at"])

    original_drain = actor_case.audit._drain_pending
    original_immediate_session = actor_case.database.immediate_session
    timeout_arguments: list[int | None] = []

    def observe_immediate_session(*, busy_timeout_ms: int | None = None):
        timeout_arguments.append(busy_timeout_ms)
        return original_immediate_session(busy_timeout_ms=busy_timeout_ms)

    def drain_while_another_writer_holds_the_database(**kwargs: Any) -> None:
        with closing(sqlite3.connect(actor_case.database.path)) as writer:
            writer.execute("BEGIN IMMEDIATE")
            try:
                current_event = writer.execute(
                    "SELECT id FROM audit_events WHERE request_id = ?",
                    (current_request_id,),
                ).fetchone()
                assert current_event is not None
                current_actor_count = writer.execute(
                    "SELECT COUNT(*) FROM audit_actors WHERE audit_event_id = ?",
                    (current_event[0],),
                ).fetchone()[0]
                assert current_actor_count == 1
                original_drain(**kwargs)
            finally:
                writer.rollback()

    with (
        patch.object(
            actor_case.database,
            "immediate_session",
            new=observe_immediate_session,
        ),
        patch.object(
            actor_case.audit,
            "_drain_pending",
            new=drain_while_another_writer_holds_the_database,
        ),
    ):
        record_audit(actor_case, principal, current_request_id)

    assert timeout_arguments == [AUDIT_WRITE_TIMEOUT_MS, 0]
    assert spool_path.exists()
    assert spool_path.read_bytes() == saved_spool_bytes
    assert _events_for_request(actor_case, old_request_id) == []
    current_events = _events_for_request(actor_case, current_request_id)
    assert len(current_events) == 1
    current_actor = _actor_for_event(actor_case, current_events[0].id)
    assert current_actor is not None
    assert current_actor.user_id == actor_case.user_id
    assert current_actor.auth_revision == actor_case.auth_revision
    assert current_actor.token_id == principal.token_id

    record_audit(actor_case, principal, "audit-replay-follow-up-request")

    assert not spool_path.exists()
    replayed_events = _events_for_request(actor_case, old_request_id)
    assert len(replayed_events) == 1
    replayed_created_at = replayed_events[0].created_at
    if replayed_created_at.tzinfo is None:
        replayed_created_at = replayed_created_at.replace(tzinfo=original_created_at.tzinfo)
    assert replayed_created_at == original_created_at
    assert replayed_events[0].path == "/synthetic/[REDACTED]"
    assert replayed_events[0].idempotency_key == "[REDACTED]"
    replayed_actor = _actor_for_event(actor_case, replayed_events[0].id)
    assert replayed_actor is not None
    assert replayed_actor.user_id == actor_case.user_id
    assert replayed_actor.auth_revision == actor_case.auth_revision
    assert replayed_actor.token_id == principal.token_id


def test_record_timeout_covers_connection_commit_and_cleanup_keeps_default(
    actor_case: ActorCase,
) -> None:
    principal, _raw_token = api_principal(actor_case)
    commit_timeouts: list[int] = []

    def observe_commit_timeout(connection: Connection) -> None:
        raw_connection = connection.connection.driver_connection
        assert raw_connection is not None
        row = raw_connection.execute("PRAGMA busy_timeout").fetchone()
        assert row is not None
        commit_timeouts.append(int(row[0]))

    event.listen(actor_case.database.engine, "commit", observe_commit_timeout)
    try:
        record_audit(actor_case, principal, "audit-replay-commit-timeout")
    finally:
        event.remove(actor_case.database.engine, "commit", observe_commit_timeout)

    assert commit_timeouts == [AUDIT_WRITE_TIMEOUT_MS]
    events = _events_for_request(actor_case, "audit-replay-commit-timeout")
    assert len(events) == 1
    actor = _actor_for_event(actor_case, events[0].id)
    assert actor is not None
    assert actor.user_id == actor_case.user_id
    assert actor.auth_revision == actor_case.auth_revision

    with actor_case.database.session() as session:
        timeout = session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
    assert timeout == 30_000

    original_immediate_session = actor_case.database.immediate_session
    timeout_arguments: list[int | None] = []

    def observe_immediate_session(*, busy_timeout_ms: int | None = None):
        timeout_arguments.append(busy_timeout_ms)
        return original_immediate_session(busy_timeout_ms=busy_timeout_ms)

    with patch.object(
        actor_case.database,
        "immediate_session",
        new=observe_immediate_session,
    ):
        actor_case.audit.cleanup()

    assert timeout_arguments == [AUDIT_WRITE_TIMEOUT_MS]


def test_actor_projection_failure_rolls_back_current_event_without_spooling(
    actor_case: ActorCase,
) -> None:
    principal, _raw_token = api_principal(actor_case)
    request_id = "audit-replay-actor-projection-error"
    with actor_case.database.session() as session:
        actor_count_before = session.scalar(select(func.count()).select_from(AuditActorRecord))

    with (
        patch.object(
            actor_case.audit,
            "_add_actor",
            side_effect=RuntimeError("synthetic actor projection failure"),
        ),
        pytest.raises(RuntimeError, match="synthetic actor projection failure"),
    ):
        record_audit(actor_case, principal, request_id)

    assert _events_for_request(actor_case, request_id) == []
    with actor_case.database.session() as session:
        actor_count_after = session.scalar(select(func.count()).select_from(AuditActorRecord))
    assert actor_count_after == actor_count_before
    pending = actor_case.database.path.parent / "audit-pending"
    assert not pending.exists() or not list(pending.glob("*.json"))
