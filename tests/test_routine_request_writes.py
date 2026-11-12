"""Routine authenticated requests must not queue behind SQLite's single writer."""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import closing, contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select

from pandrator.web.accounts.actor_models import AuditActorRecord
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.audit import AuditService, _PendingAudit
from pandrator.web.auth import API_TOKEN_ACTIVITY_INTERVAL, Principal
from pandrator.web.models import ApiToken, AuditEvent, AutomationClient
from tests.test_actor_attribution_integration import ActorCase, api_principal, case

__all__ = ["case"]

_BASE = datetime.now(UTC).replace(microsecond=0)
_AUTH_CLOCK = "pandrator.web.auth.utcnow"


@contextmanager
def _capture_sql(actor_case: ActorCase) -> Iterator[list[str]]:
    statements: list[str] = []

    def capture(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        statements.append(statement.lower())

    event.listen(actor_case.database.engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(actor_case.database.engine, "before_cursor_execute", capture)


def _begin_immediates(statements: list[str]) -> int:
    return sum(statement.startswith("begin immediate") for statement in statements)


def _token_updates(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if statement.startswith("update api_tokens") or statement.startswith("update automation_")
    ]


def _resolve(actor_case: ActorCase, raw: str) -> Principal | None:
    return actor_case.auth.resolve_api_token(raw, network_zone="loopback", target_instance_id=None)


def _last_used(actor_case: ActorCase, token_id: str) -> tuple[datetime | None, datetime | None]:
    with actor_case.database.snapshot_session() as session:
        token = session.get(ApiToken, token_id)
        assert token is not None
        client = (
            session.get(AutomationClient, token.client_id) if token.client_id is not None else None
        )
        return (
            _aware(token.last_used_at),
            _aware(client.last_used_at) if client is not None else None,
        )


def _aware(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _client_token(actor_case: ActorCase) -> tuple[str, str, str]:
    client_id = str(uuid4())
    with actor_case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="routine-write-test",
                subject=f"automation:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=sorted(ALL_SCOPES),
                target_instance_id=str(uuid4()),
                canonical_origin="http://localhost",
                created_by="test",
                user_id=actor_case.user_id,
                user_auth_revision=actor_case.auth_revision,
            )
        )
    token, raw = actor_case.auth.create_api_token("client token", client_id=client_id)
    return token.id, raw, client_id


# --- API token resolution ---------------------------------------------------


def test_repeated_resolutions_inside_the_window_take_no_writer(case: ActorCase) -> None:
    token, raw = case.auth.create_api_token("routine reads")
    with patch(_AUTH_CLOCK, return_value=_BASE):
        assert _resolve(case, raw) is not None
        assert _last_used(case, token.id)[0] == _BASE
        with (
            _capture_sql(case) as statements,
            patch.object(
                case.database, "immediate_session", wraps=case.database.immediate_session
            ) as writer,
        ):
            with patch(_AUTH_CLOCK, return_value=_BASE + API_TOKEN_ACTIVITY_INTERVAL / 2):
                principals = [_resolve(case, raw) for _ in range(50)]
    assert all(principal is not None for principal in principals)
    assert writer.call_count == 0
    assert _begin_immediates(statements) == 0
    assert not _token_updates(statements)
    assert _last_used(case, token.id)[0] == _BASE


def test_due_touch_writes_the_token_and_its_client_once(case: ActorCase) -> None:
    token_id, raw, _client_id = _client_token(case)
    due = _BASE + API_TOKEN_ACTIVITY_INTERVAL
    with patch(_AUTH_CLOCK, return_value=_BASE):
        with _capture_sql(case) as statements:
            assert _resolve(case, raw) is not None
        assert _begin_immediates(statements) == 1
        assert len(_token_updates(statements)) == 2
        assert _last_used(case, token_id) == (_BASE, _BASE)
    with patch(_AUTH_CLOCK, return_value=due - timedelta(microseconds=1)):
        with _capture_sql(case) as statements:
            assert _resolve(case, raw) is not None
        assert _begin_immediates(statements) == 0
    with patch(_AUTH_CLOCK, return_value=due):
        with _capture_sql(case) as statements:
            assert _resolve(case, raw) is not None
            assert _begin_immediates(statements) == 1
            assert len(_token_updates(statements)) == 2
        assert _last_used(case, token_id) == (due, due)


def test_concurrent_due_resolutions_update_the_touch_only_once(case: ActorCase) -> None:
    token, raw = case.auth.create_api_token("racing reads")
    snapshots_closed = threading.Barrier(2)
    original = case.database.snapshot_session

    @contextmanager
    def synchronized_snapshot() -> Iterator[Any]:
        with original() as session:
            yield session
        snapshots_closed.wait(timeout=10)

    results: list[Principal | None] = []
    rowcounts: list[int] = []

    def capture_rowcount(
        _connection: Any,
        cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if statement.lower().startswith("update api_tokens"):
            rowcounts.append(cursor.rowcount)

    event.listen(case.database.engine, "after_cursor_execute", capture_rowcount)
    try:
        with (
            patch(_AUTH_CLOCK, return_value=_BASE),
            patch.object(case.database, "snapshot_session", synchronized_snapshot),
            _capture_sql(case) as statements,
        ):
            threads = [
                threading.Thread(target=lambda: results.append(_resolve(case, raw)))
                for _ in range(2)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
    finally:
        event.remove(case.database.engine, "after_cursor_execute", capture_rowcount)
    assert len(results) == 2 and all(principal is not None for principal in results)
    assert _begin_immediates(statements) == 2
    with case.database.snapshot_session() as session:
        stored = session.get(ApiToken, token.id)
        assert stored is not None and _aware(stored.last_used_at) == _BASE
    # The loser's conditional UPDATE matched no row.
    assert sorted(rowcounts) == [0, 1]
    assert _last_used(case, token.id)[0] == _BASE


def test_busy_writer_during_a_due_touch_still_authenticates(
    case: ActorCase, caplog: pytest.LogCaptureFixture
) -> None:
    token, raw = case.auth.create_api_token("busy touch")
    caplog.set_level(logging.DEBUG, logger="pandrator.web.auth")
    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            principal = _resolve(case, raw)
        finally:
            writer.rollback()
    assert principal is not None and principal.token_id == token.id
    assert _last_used(case, token.id)[0] is None
    assert any("activity update skipped" in record.getMessage() for record in caplog.records)
    assert _resolve(case, raw) is not None
    assert _last_used(case, token.id)[0] is not None


def test_invalid_tokens_resolve_to_none_without_a_write(case: ActorCase) -> None:
    revoked, revoked_raw = case.auth.create_api_token("revoked")
    case.auth.revoke_token(revoked.id)
    _expiring, expiring_raw = case.auth.create_api_token(
        "expiring", expires_at=_BASE + timedelta(seconds=5)
    )
    _wrong, wrong_instance_raw = case.auth.create_api_token(
        "wrong instance", target_instance_id="instance-a"
    )
    _origin, wrong_origin_raw = case.auth.create_api_token(
        "wrong origin", canonical_origin="https://a.example"
    )
    narrow, narrow_scope_raw = case.auth.create_api_token("scope mismatch")
    _client_scoped_id, client_scope_raw, client_scope_id = _client_token(case)
    stale, stale_account_raw = case.auth.create_api_token("account mismatch")
    with case.database.immediate_session() as session:
        stale_row = session.get(ApiToken, stale.id)
        assert stale_row is not None
        stale_row.user_auth_revision = case.auth_revision + 1
        narrow_row = session.get(ApiToken, narrow.id)
        assert narrow_row is not None
        narrow_row.scopes_json = ["not.a.scope"]
        client_row = session.get(AutomationClient, client_scope_id)
        assert client_row is not None
        client_row.allowed_scopes_json = ["app.read"]
    with _capture_sql(case) as statements:
        assert (
            case.auth.resolve_api_token(
                wrong_origin_raw,
                network_zone="loopback",
                target_instance_id=None,
                canonical_origin="https://b.example",
            )
            is None
        )
        assert _resolve(case, narrow_scope_raw) is None
        assert _resolve(case, client_scope_raw) is None
        assert _resolve(case, stale_account_raw) is None
        assert _resolve(case, revoked_raw) is None
        assert _resolve(case, "pan_unknown") is None
        with patch(_AUTH_CLOCK, return_value=_BASE + timedelta(seconds=5)):
            assert _resolve(case, expiring_raw) is None
        assert (
            case.auth.resolve_api_token(
                wrong_instance_raw, network_zone="loopback", target_instance_id="instance-b"
            )
            is None
        )
    assert _begin_immediates(statements) == 0
    assert not _token_updates(statements)


def test_current_authorization_is_read_on_every_request(case: ActorCase) -> None:
    token, raw = case.auth.create_api_token("revoked later")
    with patch(_AUTH_CLOCK, return_value=_BASE):
        assert _resolve(case, raw) is not None
    case.auth.revoke_token(token.id)
    with patch(_AUTH_CLOCK, return_value=_BASE + timedelta(seconds=1)):
        assert _resolve(case, raw) is None


def test_bearer_reads_through_the_app_open_no_token_write(case: ActorCase) -> None:
    token, raw = case.auth.create_api_token("http reads")
    client = case.app.test_client()
    headers = {"Authorization": f"Bearer {raw}"}
    assert client.get("/api/v1/auth/status", headers=headers).status_code == 200
    assert _last_used(case, token.id)[0] is not None
    with _capture_sql(case) as statements:
        for _ in range(10):
            response = client.get("/api/v1/auth/status", headers=headers)
            assert response.status_code == 200
    assert not _token_updates(statements)
    # Sequential requests are one audit batch each; no token transaction remains.
    audit_inserts = [s for s in statements if s.startswith("insert into audit_events")]
    assert len(audit_inserts) == 10
    assert _begin_immediates(statements) == len(audit_inserts)


# --- Audit group commit -----------------------------------------------------


def _record(case: ActorCase, principal: Principal, request_id: str) -> None:
    case.audit.record(
        principal=principal,
        request_id=request_id,
        traceparent=None,
        action="synthetic.action",
        method="GET",
        path="/synthetic",
        status_code=200,
        duration_ms=1,
    )


def _gate_first_batch(audit: AuditService, total: int) -> None:
    """Hold the first write until all `total` recording threads have queued an entry."""

    original = audit._insert
    first = threading.Event()

    def gated(batch: list[_PendingAudit]) -> None:
        if not first.is_set():
            first.set()
            deadline = time.monotonic() + 10
            while (
                len(batch) + len(audit._commit_queue) < total and time.monotonic() < deadline
            ):
                time.sleep(0.005)
        original(batch)

    audit._insert = gated  # type: ignore[method-assign]


def _run_threads(count: int, target: Callable[[int], None]) -> None:
    barrier = threading.Barrier(count)

    def run(index: int) -> None:
        barrier.wait(timeout=10)
        target(index)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()


def _spooled(case: ActorCase) -> list[Any]:
    pending = case.database.path.parent / "audit-pending"
    return list(pending.glob("*.json")) if pending.exists() else []


def test_concurrent_records_share_write_transactions_and_keep_their_actors(
    case: ActorCase,
) -> None:
    principal, _raw = api_principal(case)
    count = 8
    _gate_first_batch(case.audit, count)
    with _capture_sql(case) as statements:
        _run_threads(count, lambda index: _record(case, principal, f"group-{index}"))
    assert 1 <= _begin_immediates(statements) < count
    assert not _spooled(case)
    with case.database.snapshot_session() as session:
        events = list(
            session.scalars(
                select(AuditEvent).where(AuditEvent.request_id.like("group-%"))
            ).all()
        )
        assert sorted(item.request_id for item in events) == sorted(
            f"group-{index}" for index in range(count)
        )
        actors = session.scalars(
            select(AuditActorRecord).where(
                AuditActorRecord.audit_event_id.in_([item.id for item in events])
            )
        ).all()
        assert len(actors) == count
        assert {record.user_id for record in actors} == {case.user_id}


def test_held_writer_spools_every_event_of_the_batch(
    case: ActorCase, caplog: pytest.LogCaptureFixture
) -> None:
    principal, _raw = api_principal(case)
    count = 5
    _gate_first_batch(case.audit, count)
    caplog.set_level(logging.WARNING, logger="pandrator.web.audit")
    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            _run_threads(count, lambda index: _record(case, principal, f"held-{index}"))
        finally:
            writer.rollback()
    assert len(_spooled(case)) == count
    warnings = [r for r in caplog.records if "Audit writer busy" in r.getMessage()]
    assert len(warnings) == count
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.request_id.like("held-%"))
            )
            == 0
        )


def test_a_failed_spool_raises_only_in_its_own_request(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    count = 4
    _gate_first_batch(case.audit, count)
    original = case.audit._spool

    def failing_for_one(event: AuditEvent, actor: Any = None) -> None:
        if event.request_id == "spool-2":
            raise OSError("synthetic spool failure")
        original(event, actor)

    errors: dict[int, BaseException] = {}

    def record(index: int) -> None:
        try:
            _record(case, principal, f"spool-{index}")
        except BaseException as error:  # noqa: BLE001
            errors[index] = error

    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(case.audit, "_spool", side_effect=failing_for_one):
                _run_threads(count, record)
        finally:
            writer.rollback()
    assert set(errors) == {2}
    assert isinstance(errors[2], OSError)
    assert len(_spooled(case)) == count - 1


def test_one_failing_event_leaves_the_rest_of_its_batch_committed(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    count = 6
    _gate_first_batch(case.audit, count)
    original = case.audit._add_actor

    def failing_for_bad(session: Any, event_id: int, actor: Any) -> None:
        stored = session.get(AuditEvent, event_id)
        if stored is not None and stored.request_id == "bad-3":
            raise RuntimeError("synthetic bad event")
        original(session, event_id, actor)

    errors: dict[int, BaseException] = {}

    def record(index: int) -> None:
        try:
            _record(case, principal, f"bad-{index}")
        except BaseException as error:  # noqa: BLE001
            errors[index] = error

    with patch.object(case.audit, "_add_actor", side_effect=failing_for_bad):
        _run_threads(count, record)
    assert set(errors) == {3}
    assert isinstance(errors[3], RuntimeError)
    assert not _spooled(case)
    with case.database.snapshot_session() as session:
        stored_ids = sorted(
            session.scalars(
                select(AuditEvent.request_id).where(AuditEvent.request_id.like("bad-%"))
            ).all()
        )
        assert stored_ids == sorted(f"bad-{index}" for index in range(count) if index != 3)
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditActorRecord)
                .join(AuditEvent, AuditEvent.id == AuditActorRecord.audit_event_id)
                .where(AuditEvent.request_id.like("bad-%"))
            )
            == count - 1
        )


def test_batch_size_is_bounded_and_leadership_is_released(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    sizes: list[int] = []
    count = 7
    _gate_first_batch(case.audit, count)
    gated = case.audit._insert

    def observe(batch: list[_PendingAudit]) -> None:
        sizes.append(len(batch))
        gated(batch)

    with (
        patch("pandrator.web.audit.AUDIT_BATCH_LIMIT", 2),
        patch.object(case.audit, "_insert", side_effect=observe),
    ):
        _run_threads(count, lambda index: _record(case, principal, f"limit-{index}"))
        _record(case, principal, "limit-after")
    assert sum(sizes) == count + 1
    assert max(sizes) == 2
    assert not _spooled(case)
    assert not case.audit._commit_leader and not case.audit._commit_queue
    with case.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.request_id.like("limit-%"))
            )
            == count + 1
        )
