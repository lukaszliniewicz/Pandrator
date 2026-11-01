"""Activity coalescing must keep every browser authorization check fresh."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Barrier, Lock
from typing import Any
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import event
from sqlalchemy.orm import Session

from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import BrowserSessionStore, IssuedBrowserSession
from pandrator.web.accounts.models import User
from pandrator.web.models import AutomationClient
from tests.test_browser_auth_sessions import (
    BrowserSessionCase,
    as_utc,
    issue_session,
)
from tests.test_browser_auth_sessions import browser_session_case as browser_session_case

_START = datetime(2035, 1, 1, tzinfo=UTC)
_AUTHORITY_CHANGES = (
    "revoke",
    "disable",
    "credential_epoch",
    "admin_loss",
    "client_revoke",
    "client_scope_narrowing",
    "client_owner",
    "client_epoch",
)


@contextmanager
def _capture_sql(case: BrowserSessionCase) -> Iterator[list[str]]:
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

    event.listen(case.database.engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(case.database.engine, "before_cursor_execute", capture)


def _activity_updates(statements: list[str]) -> list[str]:
    return [
        statement
        for statement in statements
        if statement.startswith("update ") and "last_seen_at" in statement
    ]


def _activity(case: BrowserSessionCase, issued: IssuedBrowserSession) -> tuple[datetime, datetime]:
    with case.database.snapshot_session() as session:
        record = session.get(BrowserAuthSession, issued.record_id)
        assert record is not None
        return as_utc(record.last_seen_at), as_utc(record.idle_expires_at)


def _issue_client_session(case: BrowserSessionCase) -> IssuedBrowserSession:
    client_id = str(uuid4())
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="activity-test",
                subject=f"automation:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=sorted(ALL_SCOPES),
                target_instance_id=str(uuid4()),
                canonical_origin="http://localhost",
                created_by="test",
                user_id=case.owner_id,
                user_auth_revision=1,
            )
        )
    return issue_session(case, user_id=case.owner_id, scopes=ALL_SCOPES, client_id=client_id)


def _invalidate(
    session: Session, case: BrowserSessionCase, issued: IssuedBrowserSession, change: str
) -> None:
    record = session.get(BrowserAuthSession, issued.record_id)
    assert record is not None
    if change == "revoke":
        record.revoked_at = _START + timedelta(seconds=1)
    elif change in {"disable", "admin_loss"}:
        user = session.get(User, case.owner_id)
        assert user is not None
        if change == "disable":
            user.is_active = False
        else:
            user.is_instance_admin = False
    elif change == "credential_epoch":
        credential = session.get(UserCredential, case.owner_id)
        assert credential is not None
        credential.auth_revision += 1
    else:
        assert record.client_id is not None
        client = session.get(AutomationClient, record.client_id)
        assert client is not None
        if change == "client_revoke":
            client.revoked_at = _START + timedelta(seconds=1)
        elif change == "client_scope_narrowing":
            client.allowed_scopes_json = ["app.read"]
        elif change == "client_owner":
            client.user_id = case.user_id
        elif change == "client_epoch":
            assert client.user_auth_revision is not None
            client.user_auth_revision += 1
        else:
            raise AssertionError(f"Unknown authority change: {change}")


def test_one_hundred_hot_resolves_read_current_join_without_writers(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case)
    with (
        patch(
            "pandrator.web.accounts.browser_sessions.utcnow",
            return_value=_START + timedelta(seconds=29),
        ),
        _capture_sql(case) as statements,
        patch.object(
            case.database, "immediate_session", wraps=case.database.immediate_session
        ) as writer,
    ):
        principals = [store.resolve(issued.raw_token) for _ in range(100)]
    assert all(principal is not None for principal in principals)
    principal = principals[0]
    assert principal is not None
    assert principal.user_id == case.user_id
    assert principal.auth_revision == 1
    assert principal.subject == "test-user"
    assert principal.kind == "owner_session"
    assert principal.scopes == frozenset({"app.read", "app.run"})
    assert principal.client_id is None
    assert principal.session_id == issued.record_id
    assert all(item == principal for item in principals)
    assert writer.call_count == 0
    assert not any(statement.startswith("begin immediate") for statement in statements)
    assert not any(statement.startswith("update ") for statement in statements)
    assert (
        sum(
            "join users" in statement and "join user_credentials" in statement
            for statement in statements
        )
        == 100
    )
    assert _activity(case, issued) == (_START, _START + timedelta(hours=12))


@pytest.mark.parametrize(
    ("idle_seconds", "absolute_seconds", "due"),
    [
        (43200, 604800, timedelta(seconds=30)),
        (10000, 10001, timedelta(seconds=30)),
        (1, 5, timedelta(milliseconds=10)),
    ],
)
def test_exact_touch_boundary_slides_once_within_absolute_cap(
    browser_session_case: BrowserSessionCase,
    idle_seconds: int,
    absolute_seconds: int,
    due: timedelta,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case, idle_seconds=idle_seconds, absolute_seconds=absolute_seconds)
    with _capture_sql(case) as statements:
        with patch(
            "pandrator.web.accounts.browser_sessions.utcnow",
            return_value=_START + due - timedelta(microseconds=1),
        ):
            assert store.resolve(issued.raw_token) is not None
        assert not _activity_updates(statements)
        with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START + due):
            assert store.resolve(issued.raw_token) is not None
            assert store.resolve(issued.raw_token) is not None
    assert len(_activity_updates(statements)) == 1
    assert sum(statement.startswith("begin immediate") for statement in statements) == 1
    assert _activity(case, issued) == (
        _START + due,
        min(_START + due + timedelta(seconds=idle_seconds), issued.expires_at),
    )


@pytest.mark.parametrize("idle_seconds", [1, 43200])
def test_coalesced_idle_expiry_is_conservative_by_at_most_the_touch_interval(
    browser_session_case: BrowserSessionCase,
    idle_seconds: int,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    idle = timedelta(seconds=idle_seconds)
    interval = min(timedelta(seconds=30), idle / 100)
    last_request = _START + interval - timedelta(microseconds=1)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case, idle_seconds=idle_seconds, absolute_seconds=idle_seconds * 2)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=last_request):
        assert store.resolve(issued.raw_token) is not None
    _last_seen, durable_expiry = _activity(case, issued)
    exact_activity_expiry = last_request + idle
    assert timedelta(0) <= exact_activity_expiry - durable_expiry <= interval
    assert interval <= idle / 100
    for now in (durable_expiry, exact_activity_expiry):
        with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=now):
            assert store.resolve(issued.raw_token) is None
    assert _activity(case, issued) == (_START, durable_expiry)


@pytest.mark.parametrize(("idle_seconds", "absolute_seconds"), [(1, 2), (1, 1)])
def test_idle_and_absolute_expiry_equality_deny_without_touch(
    browser_session_case: BrowserSessionCase,
    idle_seconds: int,
    absolute_seconds: int,
) -> None:
    case = browser_session_case
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case, idle_seconds=idle_seconds, absolute_seconds=absolute_seconds)
    with (
        patch(
            "pandrator.web.accounts.browser_sessions.utcnow",
            return_value=_START + timedelta(seconds=1),
        ),
        _capture_sql(case) as statements,
    ):
        assert BrowserSessionStore(case.database).resolve(issued.raw_token) is None
    assert not _activity_updates(statements)
    assert not any(statement.startswith("begin immediate") for statement in statements)


@pytest.mark.parametrize("change", _AUTHORITY_CHANGES)
def test_hot_resolve_denies_current_authority_changes_immediately(
    browser_session_case: BrowserSessionCase, change: str
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = _issue_client_session(case)
    with patch(
        "pandrator.web.accounts.browser_sessions.utcnow", return_value=_START + timedelta(seconds=1)
    ):
        assert store.resolve(issued.raw_token) is not None
        before = _activity(case, issued)
        with case.database.immediate_session() as session:
            _invalidate(session, case, issued, change)
        with _capture_sql(case) as statements:
            assert store.resolve(issued.raw_token) is None
    assert not _activity_updates(statements)
    assert not any(statement.startswith("begin immediate") for statement in statements)
    assert _activity(case, issued) == before


@pytest.mark.parametrize("change", _AUTHORITY_CHANGES)
def test_due_writer_revalidates_authority_after_closing_snapshot(
    browser_session_case: BrowserSessionCase, change: str
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = _issue_client_session(case)
    before = _activity(case, issued)
    snapshot = case.database.snapshot_session
    immediate = case.database.immediate_session
    snapshot_open = False
    writer_entries = 0

    @contextmanager
    def changed_after_snapshot() -> Iterator[Session]:
        nonlocal snapshot_open
        with snapshot() as session:
            snapshot_open = True
            try:
                yield session
            finally:
                snapshot_open = False
        with immediate() as session:
            _invalidate(session, case, issued, change)

    @contextmanager
    def checked_writer() -> Iterator[Session]:
        nonlocal writer_entries
        assert not snapshot_open
        writer_entries += 1
        with immediate() as session:
            yield session

    with (
        patch(
            "pandrator.web.accounts.browser_sessions.utcnow",
            return_value=_START + timedelta(seconds=30),
        ),
        patch.object(case.database, "snapshot_session", changed_after_snapshot),
        patch.object(case.database, "immediate_session", checked_writer),
        _capture_sql(case) as statements,
    ):
        assert store.resolve(issued.raw_token) is None
    assert writer_entries == 1
    assert not _activity_updates(statements)
    assert _activity(case, issued) == before


def test_concurrent_due_resolves_release_snapshots_and_touch_only_once(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case)
    snapshot = case.database.snapshot_session
    immediate = case.database.immediate_session
    snapshots_closed = Barrier(2)
    lock = Lock()
    active_snapshots = 0
    writer_entries = 0

    @contextmanager
    def synchronized_snapshot() -> Iterator[Session]:
        nonlocal active_snapshots
        with snapshot() as session:
            with lock:
                active_snapshots += 1
            yield session
        with lock:
            active_snapshots -= 1
        snapshots_closed.wait(timeout=10)

    @contextmanager
    def checked_writer() -> Iterator[Session]:
        nonlocal writer_entries
        with lock:
            assert active_snapshots == 0
            writer_entries += 1
        with immediate() as session:
            yield session

    with (
        patch(
            "pandrator.web.accounts.browser_sessions.utcnow",
            return_value=_START + timedelta(seconds=30),
        ),
        patch.object(case.database, "snapshot_session", synchronized_snapshot),
        patch.object(case.database, "immediate_session", checked_writer),
        _capture_sql(case) as statements,
        ThreadPoolExecutor(max_workers=2) as executor,
    ):
        futures = [executor.submit(store.resolve, issued.raw_token) for _ in range(2)]
        principals = [future.result(timeout=15) for future in futures]
    assert all(principal is not None for principal in principals)
    assert principals[0] == principals[1]
    assert writer_entries == 2
    assert len(_activity_updates(statements)) == 1
    assert _activity(case, issued) == (
        _START + timedelta(seconds=30),
        _START + timedelta(hours=12, seconds=30),
    )


@pytest.mark.parametrize("rollback_in_writer", [False, True])
def test_clock_rollback_does_not_regress_activity(
    browser_session_case: BrowserSessionCase, rollback_in_writer: bool
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case)
    before = _activity(case, issued)
    times = [_START - timedelta(seconds=1)]
    if rollback_in_writer:
        times.insert(0, _START + timedelta(seconds=30))
    with (
        patch("pandrator.web.accounts.browser_sessions.utcnow", side_effect=times) as now,
        _capture_sql(case) as statements,
    ):
        assert store.resolve(issued.raw_token) is not None
    assert now.call_count == len(times)
    assert not _activity_updates(statements)
    assert sum(statement.startswith("begin immediate") for statement in statements) == int(
        rollback_in_writer
    )
    assert _activity(case, issued) == before


@pytest.mark.parametrize(("idle_seconds", "absolute_seconds"), [(1, 2), (1, 1)])
def test_due_writer_rechecks_expiry_at_equality(
    browser_session_case: BrowserSessionCase,
    idle_seconds: int,
    absolute_seconds: int,
) -> None:
    case = browser_session_case
    with patch("pandrator.web.accounts.browser_sessions.utcnow", return_value=_START):
        issued = issue_session(case, idle_seconds=idle_seconds, absolute_seconds=absolute_seconds)
    before = _activity(case, issued)
    with (
        patch(
            "pandrator.web.accounts.browser_sessions.utcnow",
            side_effect=[_START + timedelta(milliseconds=10), _START + timedelta(seconds=1)],
        ) as now,
        _capture_sql(case) as statements,
    ):
        assert BrowserSessionStore(case.database).resolve(issued.raw_token) is None
    assert now.call_count == 2
    assert sum(statement.startswith("begin immediate") for statement in statements) == 1
    assert not _activity_updates(statements)
    assert _activity(case, issued) == before
