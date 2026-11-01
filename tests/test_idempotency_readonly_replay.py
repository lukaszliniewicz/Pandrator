"""File preparation can inspect exact retries without mutating reservations."""

from __future__ import annotations

from datetime import timedelta
from typing import cast

import pytest
from sqlalchemy import Table, event, select

from pandrator.web.auth import Principal
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.idempotency import IdempotencyConflict, IdempotencyInProgress, IdempotencyService
from pandrator.web.models import ApiIdempotency, utcnow

PRINCIPAL = Principal(
    "synthetic", "api_token", frozenset({"app.read"}), None, "loopback", "synthetic"
)
KEY = "retained-input-retry"
PAYLOAD = {"request": "synthetic"}


@pytest.fixture
def store(tmp_path):
    database = Database(tmp_path / "idempotency-only.sqlite3")
    # This unit exercises only the standalone reservation table, not ownership
    # migrations or an incomplete substitute for the application's schema.
    cast(Table, ApiIdempotency.__table__).create(database.engine)
    service = IdempotencyService(database, SecretRedactor(database))
    try:
        yield database, service
    finally:
        database.dispose()


def saved(
    database, service, *, state="completed", expired=False, stale=False, resource=True, payload=None
):
    with database.immediate_session() as session:
        record = ApiIdempotency(
            principal_subject=PRINCIPAL.subject,
            operation_id="preview",
            idempotency_key=KEY,
            request_digest=service.request_digest(
                "preview", PAYLOAD if payload is None else payload
            ),
            state=state,
            status_code=202,
            response_json={"id": "synthetic-job"},
            resource_kind="job" if resource else None,
            resource_id="synthetic-job" if resource else None,
            created_at=utcnow() - timedelta(minutes=20 if stale else 0),
            expires_at=utcnow() + timedelta(hours=-1 if expired else 1),
        )
        session.add(record)


def rows(database):
    with database.snapshot_session() as session:
        return [dict(row) for row in session.execute(select(ApiIdempotency.__table__)).mappings()]


def peek(session, service, **kwargs):
    return service.replay_if_present(
        session,
        principal=PRINCIPAL,
        operation_id="preview",
        idempotency_key=KEY,
        payload=PAYLOAD,
        **kwargs,
    )


@pytest.mark.parametrize(
    "state,expired,stale,resource,outcome",
    [
        ("completed", False, False, True, "replay"),
        ("failed", False, False, True, "replay"),
        ("completed", True, False, True, "fresh"),
        ("in_progress", False, False, True, "blocked"),
        ("in_progress", False, True, True, "blocked"),
        ("in_progress", False, True, False, "fresh"),
    ],
)
def test_readonly_probe_preserves_rows_and_matches_existing_lifecycle(
    store, state, expired, stale, resource, outcome
):
    database, service = store
    saved(database, service, state=state, expired=expired, stale=stale, resource=resource)
    before = rows(database)
    writes = []

    def track(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    event.listen(database.engine, "before_cursor_execute", track)
    try:
        with database.snapshot_session() as session:
            if outcome == "blocked":
                with pytest.raises(IdempotencyInProgress):
                    peek(session, service)
            else:
                result = peek(session, service)
                if outcome == "fresh":
                    assert result is None
                else:
                    assert result is not None and result.replayed
                    assert result.response == ({"id": "synthetic-job"}, 202)
    finally:
        event.remove(database.engine, "before_cursor_execute", track)
    assert not writes and rows(database) == before
    # begin remains responsible for expiration/recovery mutation, if needed.
    with database.immediate_session() as session:
        if outcome == "blocked":
            with pytest.raises(IdempotencyInProgress):
                service.begin(
                    session,
                    principal=PRINCIPAL,
                    operation_id="preview",
                    idempotency_key=KEY,
                    payload=PAYLOAD,
                )
        else:
            result = service.begin(
                session,
                principal=PRINCIPAL,
                operation_id="preview",
                idempotency_key=KEY,
                payload=PAYLOAD,
            )
            assert result.replayed is (outcome == "replay")


def test_missing_probe_never_creates_a_reservation(store):
    database, service = store
    with database.snapshot_session() as session:
        assert peek(session, service) is None
    assert rows(database) == []


def test_digest_conflict_and_legacy_fallback_leave_completed_receipt_unchanged(store):
    database, service = store
    legacy = {"request": "legacy"}
    saved(database, service, payload=legacy)
    before = rows(database)
    with database.snapshot_session() as session:
        with pytest.raises(IdempotencyConflict):
            peek(session, service)
        result = peek(session, service, legacy_payload=lambda _record: legacy)
        assert result is not None and result.replayed
    assert rows(database) == before
