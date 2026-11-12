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
from sqlalchemy import event, func, select, update

from pandrator.web.accounts.actor_models import AuditActorRecord
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.audit import _EVENT_FIELDS, AuditService, _PendingAudit
from pandrator.web.auth import API_TOKEN_ACTIVITY_INTERVAL, AuthService, Principal
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
        assert not _token_updates(statements)
        assert _last_used(case, token_id) == (_BASE, _BASE)
    with patch(_AUTH_CLOCK, return_value=due):
        with _capture_sql(case) as statements:
            assert _resolve(case, raw) is not None
            assert _begin_immediates(statements) == 1
            assert len(_token_updates(statements)) == 2
        assert _last_used(case, token_id) == (due, due)


@pytest.mark.parametrize("separate_processes", [False, True], ids=["one-process", "two-processes"])
def test_concurrent_due_resolutions_update_the_touch_only_once(
    case: ActorCase, separate_processes: bool
) -> None:
    token, raw = case.auth.create_api_token("racing reads")
    # A second AuthService has its own in-process claims, as another server process would.
    services = [case.auth, AuthService(case.database) if separate_processes else case.auth]
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
                threading.Thread(
                    target=lambda service=service: results.append(
                        service.resolve_api_token(
                            raw, network_zone="loopback", target_instance_id=None
                        )
                    )
                )
                for service in services
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=15)
                assert not thread.is_alive()
    finally:
        event.remove(case.database.engine, "after_cursor_execute", capture_rowcount)
    assert len(results) == 2 and all(principal is not None for principal in results)
    if separate_processes:
        # Both take the writer; the loser's conditional UPDATE matches no row.
        assert _begin_immediates(statements) == 2
        assert sorted(rowcounts) == [0, 1]
    else:
        # The in-process claim lets one of them try the touch.
        assert _begin_immediates(statements) == 1
        assert rowcounts == [1]
    assert _last_used(case, token.id)[0] == _BASE


def test_busy_writer_during_a_due_touch_still_authenticates(
    case: ActorCase, caplog: pytest.LogCaptureFixture
) -> None:
    token, raw = case.auth.create_api_token("busy touch")
    caplog.set_level(logging.DEBUG, logger="pandrator.web.auth")
    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with patch(_AUTH_CLOCK, return_value=_BASE):
                principal = _resolve(case, raw)
                # The skipped touch is not retried by the next request in the interval.
                with _capture_sql(case) as statements:
                    assert _resolve(case, raw) is not None
        finally:
            writer.rollback()
    assert principal is not None and principal.token_id == token.id
    assert _begin_immediates(statements) == 0
    assert _last_used(case, token.id)[0] is None
    assert any("activity update skipped" in record.getMessage() for record in caplog.records)
    retry = _BASE + API_TOKEN_ACTIVITY_INTERVAL
    with patch(_AUTH_CLOCK, return_value=retry):
        assert _resolve(case, raw) is not None
    assert _last_used(case, token.id)[0] == retry


def test_a_stored_time_ahead_of_the_clock_is_touched_again(case: ActorCase) -> None:
    token, raw = case.auth.create_api_token("clock stepped back")
    ahead = _BASE + timedelta(hours=1)
    with patch(_AUTH_CLOCK, return_value=ahead):
        assert _resolve(case, raw) is not None
    assert _last_used(case, token.id)[0] == ahead
    with patch(_AUTH_CLOCK, return_value=_BASE):
        assert _resolve(case, raw) is not None
    assert _last_used(case, token.id)[0] == _BASE


@pytest.mark.parametrize("separate_processes", [False, True], ids=["one-process", "two-processes"])
def test_an_earlier_clock_arriving_later_neither_touches_nor_moves_the_time_back(
    case: ActorCase, separate_processes: bool
) -> None:
    # Each request reads the clock before its snapshot, so a request that read
    # it earlier can finish after one that read it later. It must not count the
    # later claim or stored time as a clock step back.
    token, raw = case.auth.create_api_token("out of order clocks")
    later = _BASE + timedelta(seconds=1)
    with patch(_AUTH_CLOCK, return_value=later):
        assert _resolve(case, raw) is not None
    assert _last_used(case, token.id)[0] == later
    service = AuthService(case.database) if separate_processes else case.auth
    with patch(_AUTH_CLOCK, return_value=_BASE), _capture_sql(case) as statements:
        principal = service.resolve_api_token(
            raw, network_zone="loopback", target_instance_id=None
        )
    assert principal is not None
    assert _begin_immediates(statements) == 0
    assert _last_used(case, token.id)[0] == later


def test_a_touch_with_an_earlier_clock_leaves_a_later_time_alone(case: ActorCase) -> None:
    # The snapshot can find a touch due that another process makes before this
    # request's UPDATE runs; the UPDATE must then match nothing.
    token, raw = case.auth.create_api_token("stale due touch")
    later = _BASE + timedelta(seconds=1)
    with patch(_AUTH_CLOCK, return_value=later):
        principal = _resolve(case, raw)
    assert principal is not None
    with patch(_AUTH_CLOCK, return_value=_BASE):
        case.auth._touch_api_token(principal, token_due=True, client_due=False)
    assert _last_used(case, token.id)[0] == later


@pytest.mark.parametrize("separate_processes", [False, True], ids=["one-process", "two-processes"])
def test_a_newer_time_stored_before_the_touch_takes_the_writer_is_kept(
    case: ActorCase, separate_processes: bool
) -> None:
    # Request A claims its touch, then stalls before taking the writer while
    # request B stores a time more than one interval later. A reads the clock
    # only once it holds the writer, so B's time is not ahead and is kept.
    token, raw = case.auth.create_api_token("stalled before the writer")
    newer = _BASE + timedelta(seconds=120)
    other_request_wrote: list[bool] = []
    original = case.database.immediate_session

    def clock() -> datetime:
        return newer + timedelta(seconds=1) if other_request_wrote else _BASE

    @contextmanager
    def other_request_first(*args: Any, **kwargs: Any) -> Iterator[Any]:
        if not other_request_wrote:
            with original() as other:
                other.execute(
                    update(ApiToken).where(ApiToken.id == token.id).values(last_used_at=newer)
                )
            other_request_wrote.append(True)
        with original(*args, **kwargs) as session:
            yield session

    service = AuthService(case.database) if separate_processes else case.auth
    with (
        patch(_AUTH_CLOCK, side_effect=clock),
        patch.object(case.database, "immediate_session", other_request_first),
    ):
        principal = service.resolve_api_token(
            raw, network_zone="loopback", target_instance_id=None
        )
    assert principal is not None
    assert other_request_wrote == [True]
    assert _last_used(case, token.id)[0] == newer


@pytest.mark.parametrize("separate_processes", [False, True], ids=["one-process", "two-processes"])
def test_a_request_stalled_past_the_interval_keeps_a_newer_time(
    case: ActorCase, separate_processes: bool
) -> None:
    # Request A reads the clock for its snapshot, then stalls for over a minute
    # while request B stores a newer time. A must compare against the clock as it
    # is when it claims and touches, not take B's time for a clock step back.
    token, raw = case.auth.create_api_token("stalled request")
    stored = _BASE + timedelta(seconds=120)
    with patch(_AUTH_CLOCK, return_value=stored):
        assert _resolve(case, raw) is not None
    assert _last_used(case, token.id)[0] == stored
    readings = iter([_BASE])
    service = AuthService(case.database) if separate_processes else case.auth
    with patch(
        _AUTH_CLOCK, side_effect=lambda: next(readings, stored + timedelta(seconds=1))
    ):
        principal = service.resolve_api_token(
            raw, network_zone="loopback", target_instance_id=None
        )
    assert principal is not None
    assert _last_used(case, token.id)[0] == stored


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


def _batch_together(audit: AuditService, total: int) -> list[list[str]]:
    """Make the next `total` recording threads one batch; return each insert's request ids.

    Leadership is held until all of them have queued, so the first thread to lead
    takes every entry at once (up to the batch limit).
    """
    batches: list[list[str]] = []
    original = audit._insert

    def observed(batch: list[_PendingAudit]) -> None:
        batches.append([str(entry.fields["request_id"]) for entry in batch])
        original(batch)

    audit._insert = observed  # type: ignore[method-assign]
    with audit._commit_condition:
        audit._commit_leader = True

    def release() -> None:
        deadline = time.monotonic() + 10
        while len(audit._commit_queue) < total and time.monotonic() < deadline:
            time.sleep(0.005)
        with audit._commit_condition:
            audit._commit_leader = False
            audit._commit_condition.notify_all()

    threading.Thread(target=release, daemon=True).start()
    return batches


def _run_threads(count: int, target: Callable[[int], None]) -> None:
    barrier = threading.Barrier(count)
    failures: list[BaseException] = []

    def run(index: int) -> None:
        try:
            barrier.wait(timeout=10)
            target(index)
        except BaseException as error:  # noqa: BLE001 - re-raised below in the test thread
            failures.append(error)

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    if failures:
        raise failures[0]


def _spooled(case: ActorCase) -> list[Any]:
    pending = case.database.path.parent / "audit-pending"
    return list(pending.glob("*.json")) if pending.exists() else []


def test_concurrent_records_share_write_transactions_and_keep_their_actors(
    case: ActorCase,
) -> None:
    principal, _raw = api_principal(case)
    count = 8
    batches = _batch_together(case.audit, count)
    with _capture_sql(case) as statements:
        _run_threads(count, lambda index: _record(case, principal, f"group-{index}"))
    assert [len(batch) for batch in batches] == [count]
    assert _begin_immediates(statements) == 1
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
    batches = _batch_together(case.audit, count)
    caplog.set_level(logging.WARNING, logger="pandrator.web.audit")
    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            _run_threads(count, lambda index: _record(case, principal, f"held-{index}"))
        finally:
            writer.rollback()
    assert [len(batch) for batch in batches] == [count]
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


def test_a_held_writer_lets_each_request_spool_its_own_event(case: ActorCase) -> None:
    # The leader releases the busy batch at once; every request writes its own
    # spool file in its own thread instead of the leader writing them in turn.
    principal, _raw = api_principal(case)
    count = 5
    batches = _batch_together(case.audit, count)
    original = case.audit._spool
    spooling_threads: dict[str, int] = {}
    recording_threads: dict[str, int] = {}

    def spool(event: AuditEvent, actor: Any = None) -> None:
        spooling_threads[event.request_id] = threading.get_ident()
        original(event, actor)

    def record(index: int) -> None:
        recording_threads[f"own-{index}"] = threading.get_ident()
        _record(case, principal, f"own-{index}")

    with closing(sqlite3.connect(case.database.path)) as writer:
        writer.execute("BEGIN IMMEDIATE")
        try:
            with patch.object(case.audit, "_spool", side_effect=spool):
                _run_threads(count, record)
        finally:
            writer.rollback()
    assert [len(batch) for batch in batches] == [count]
    assert spooling_threads == recording_threads
    assert len(_spooled(case)) == count


def test_a_failed_spool_raises_only_in_its_own_request(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    count = 4
    batches = _batch_together(case.audit, count)
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
    # The failing entry shared its batch with the others.
    assert len(batches) == 1
    assert sorted(batches[0]) == sorted(f"spool-{index}" for index in range(count))
    assert set(errors) == {2}
    assert isinstance(errors[2], OSError)
    assert len(_spooled(case)) == count - 1


def test_one_failing_event_leaves_the_rest_of_its_batch_committed(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    count = 6
    batches = _batch_together(case.audit, count)
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
    # One shared batch failed, then each entry was retried alone.
    expected = sorted(f"bad-{index}" for index in range(count))
    assert len(batches) == count + 1
    assert sorted(batches[0]) == expected
    assert sorted(item for batch in batches[1:] for item in batch) == expected
    assert all(len(batch) == 1 for batch in batches[1:])
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
    count = 7
    batches = _batch_together(case.audit, count)
    with patch("pandrator.web.audit.AUDIT_BATCH_LIMIT", 2):
        _run_threads(count, lambda index: _record(case, principal, f"limit-{index}"))
        _record(case, principal, "limit-after")
    sizes = [len(batch) for batch in batches]
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


def test_a_batch_prunes_when_any_of_its_ids_is_a_multiple_of_100(case: ActorCase) -> None:
    principal, _raw = api_principal(case)
    _record(case, principal, "prune-seed")
    with case.database.immediate_session() as session:
        seed = session.scalar(select(AuditEvent).where(AuditEvent.request_id == "prune-seed"))
        assert seed is not None
        highest = int(session.scalar(select(func.max(AuditEvent.id))) or 0)
        # The next batch of four gets ids ...99, ...00, ...01, ...02: the multiple of
        # 100 is neither its first nor its last id.
        floor = ((highest + 2) // 100 + 1) * 100 - 2
        session.add(
            AuditEvent(
                id=floor, **{field: getattr(seed, field) for field in _EVENT_FIELDS}
            )
        )
    count = 4
    batches = _batch_together(case.audit, count)
    with patch.object(
        case.audit, "_prune_in_session", wraps=case.audit._prune_in_session
    ) as prune:
        _run_threads(count, lambda index: _record(case, principal, f"prune-{index}"))
    assert [len(batch) for batch in batches] == [count]
    assert prune.call_count == 1
    with case.database.snapshot_session() as session:
        ids = sorted(
            session.scalars(
                select(AuditEvent.id).where(AuditEvent.request_id.like("prune-_"))
            ).all()
        )
    assert ids == list(range(floor + 1, floor + count + 1))
    assert (floor + 2) % 100 == 0
