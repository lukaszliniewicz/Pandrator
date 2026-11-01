"""Synthetic tests for revocable, credential-epoch-bound browser sessions."""

from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier
from typing import Any, Iterator, cast
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import Table, select

from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.browser_sessions import (
    BrowserSessionStore,
    IssuedBrowserSession,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import (
    AutomationClientProjectGrant,
    AutomationClientWorkspaceGrant,
    BrowserAuthProjectGrant,
    BrowserAuthWorkspaceGrant,
)
from pandrator.web.database import Database
from pandrator.web.model_base import Base
from pandrator.web.models import AutomationClient
from pandrator.web.projects.models import Project


@dataclass(frozen=True)
class BrowserSessionCase:
    database: Database
    owner_id: str
    user_id: str


@pytest.fixture
def browser_session_case(tmp_path: Path) -> Iterator[BrowserSessionCase]:
    database = Database(tmp_path / "browser-auth.sqlite3")
    Base.metadata.create_all(
        database.engine,
        tables=cast(
            list[Table],
            [
                User.__table__,
                UserCredential.__table__,
                BrowserAuthSession.__table__,
                AutomationClient.__table__,
                Workspace.__table__,
                WorkspaceMembership.__table__,
                Project.__table__,
                AutomationClientWorkspaceGrant.__table__,
                AutomationClientProjectGrant.__table__,
                BrowserAuthWorkspaceGrant.__table__,
                BrowserAuthProjectGrant.__table__,
            ],
        ),
    )
    with database.immediate_session() as session:
        owner = User(
            legacy_subject="owner",
            display_name="Owner",
            is_active=True,
            is_instance_admin=True,
        )
        user = User(
            legacy_subject=None,
            display_name="Member",
            is_active=True,
            is_instance_admin=False,
        )
        session.add_all((owner, user))
        session.flush()
        session.add_all(
            (
                UserCredential(
                    user_id=owner.id,
                    login_name="owner",
                    password_hash=None,
                    auth_revision=1,
                ),
                UserCredential(
                    user_id=user.id,
                    login_name="member",
                    password_hash=None,
                    auth_revision=1,
                ),
            )
        )
        owner_id = owner.id
        user_id = user.id
    try:
        yield BrowserSessionCase(database, owner_id, user_id)
    finally:
        database.dispose()


def issue_session(
    case: BrowserSessionCase,
    *,
    user_id: str | None = None,
    auth_revision: int = 1,
    **overrides: Any,
) -> IssuedBrowserSession:
    values = {
        "subject": "test-user",
        "principal_kind": "owner_session",
        "scopes": ["app.read", "app.run"],
    }
    values.update(overrides)
    return BrowserSessionStore(case.database).issue(
        case.user_id if user_id is None else user_id,
        auth_revision,
        **values,
    )


def as_utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def test_issue_persists_only_token_digest_and_slides_idle_expiry_to_absolute_cap(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    current_time = [datetime(2035, 1, 1, tzinfo=UTC)]
    with patch(
        "pandrator.web.accounts.browser_sessions.utcnow",
        side_effect=lambda: current_time[0],
    ):
        issued = issue_session(
            case,
            user_id=case.owner_id,
            subject="owner-browser",
            scopes=ALL_SCOPES,
            idle_seconds=20,
            absolute_seconds=50,
        )
        assert issued.raw_token not in repr(issued)
        with case.database.snapshot_session() as session:
            record = session.get(BrowserAuthSession, issued.record_id)
            assert record is not None
            assert record.token_hash == hashlib.sha256(issued.raw_token.encode("utf-8")).hexdigest()
            assert issued.raw_token != record.token_hash
            assert as_utc(record.last_seen_at) == current_time[0]
            assert as_utc(record.idle_expires_at) == current_time[0] + timedelta(seconds=20)
            assert as_utc(record.expires_at) == current_time[0] + timedelta(seconds=50)

        current_time[0] += timedelta(seconds=10)
        principal = store.resolve(issued.raw_token)
        assert principal is not None
        assert principal.user_id == case.owner_id
        assert principal.subject == "owner-browser"
        assert principal.kind == "owner_session"
        assert principal.scopes == ALL_SCOPES
        assert principal.session_id == issued.record_id
        with case.database.snapshot_session() as session:
            record = session.get(BrowserAuthSession, issued.record_id)
            assert record is not None
            assert as_utc(record.last_seen_at) == current_time[0]
            assert as_utc(record.idle_expires_at) == current_time[0] + timedelta(seconds=20)

        current_time[0] += timedelta(seconds=15)
        assert store.resolve(issued.raw_token) is not None
        current_time[0] += timedelta(seconds=15)
        assert store.resolve(issued.raw_token) is not None
        with case.database.snapshot_session() as session:
            record = session.get(BrowserAuthSession, issued.record_id)
            assert record is not None
            assert as_utc(record.idle_expires_at) == as_utc(record.expires_at)
        current_time[0] += timedelta(seconds=9)
        assert store.resolve(issued.raw_token) is not None
        current_time[0] += timedelta(seconds=1)
        assert store.resolve(issued.raw_token) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"principal_kind": "unknown"},
        {"scopes": ["app.read", "not-a-scope"]},
        {"subject": " "},
        {"subject": "x" * 201},
        {"idle_seconds": 0},
        {"idle_seconds": 10, "absolute_seconds": 9},
        {"absolute_seconds": 30 * 24 * 60 * 60 + 1},
        {"idle_seconds": True},
    ],
)
def test_issue_rejects_invalid_kind_scope_subject_or_lifetime(
    browser_session_case: BrowserSessionCase,
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ValueError):
        issue_session(browser_session_case, **overrides)
    with browser_session_case.database.snapshot_session() as session:
        assert session.scalar(select(BrowserAuthSession.id)) is None


def test_issue_requires_a_fresh_active_user_credential_epoch(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    with pytest.raises(ValueError, match="authentication revision is stale"):
        issue_session(case, auth_revision=2)
    with case.database.immediate_session() as session:
        user = session.get(User, case.user_id)
        assert user is not None
        user.is_active = False
    with pytest.raises(ValueError, match="inactive"):
        issue_session(case)


def test_resolve_rejects_epoch_changes_and_disabled_users(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    stale = issue_session(case)
    with case.database.immediate_session() as session:
        credential = session.get(UserCredential, case.user_id)
        assert credential is not None
        credential.auth_revision = 2
    assert store.resolve(stale.raw_token) is None

    current = issue_session(case, auth_revision=2)
    with case.database.immediate_session() as session:
        user = session.get(User, case.user_id)
        assert user is not None
        user.is_active = False
    assert store.resolve(current.raw_token) is None


def test_admin_scope_requires_current_admin_and_is_forbidden_for_manager_bootstrap(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    with pytest.raises(ValueError, match="instance administrator"):
        issue_session(case, scopes=ALL_SCOPES)
    with pytest.raises(ValueError, match="Manager bootstrap"):
        issue_session(
            case,
            user_id=case.owner_id,
            principal_kind="manager_bootstrap",
            scopes=ALL_SCOPES,
        )

    admin_session = issue_session(case, user_id=case.owner_id, scopes=ALL_SCOPES)
    principal = store.resolve(admin_session.raw_token)
    assert principal is not None
    assert "app.admin" in principal.scopes
    with case.database.immediate_session() as session:
        owner = session.get(User, case.owner_id)
        assert owner is not None
        owner.is_instance_admin = False
    assert store.resolve(admin_session.raw_token) is None

    malformed_user_session = issue_session(case)
    malformed_scopes_shape_session = issue_session(case)
    malformed_manager_session = issue_session(
        case,
        user_id=case.owner_id,
        principal_kind="manager_bootstrap",
        scopes=["app.read"],
    )
    with case.database.immediate_session() as session:
        ordinary = session.get(BrowserAuthSession, malformed_user_session.record_id)
        malformed_scopes = session.get(BrowserAuthSession, malformed_scopes_shape_session.record_id)
        owner = session.get(User, case.owner_id)
        manager = session.get(BrowserAuthSession, malformed_manager_session.record_id)
        assert (
            ordinary is not None
            and malformed_scopes is not None
            and manager is not None
            and owner is not None
        )
        ordinary.scopes_json = ["app.admin"]
        malformed_scopes.scopes_json = cast(Any, "app.read")
        manager.scopes_json = ["app.admin"]
        owner.is_instance_admin = True
    assert store.resolve(malformed_user_session.raw_token) is None
    assert store.resolve(malformed_scopes_shape_session.raw_token) is None
    assert store.resolve(malformed_manager_session.raw_token) is None


@pytest.mark.parametrize(
    "manager_client_id",
    ("pandrator-manager-browser", "pandrator-mcp-local"),
)
def test_reserved_manager_provenance_is_not_a_client_row_and_other_missing_clients_fail(
    browser_session_case: BrowserSessionCase,
    manager_client_id: str,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    manager = issue_session(case, user_id=case.owner_id, client_id=manager_client_id)
    principal = store.resolve(manager.raw_token)
    assert principal is not None
    assert principal.client_id == manager_client_id
    with pytest.raises(ValueError, match="client is missing"):
        issue_session(case, client_id="missing-client")


def test_actual_client_must_be_currently_owned_by_the_same_user_epoch(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    client_id = str(uuid4())
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="test client",
                subject=f"automation:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=["app.read"],
                target_instance_id=str(uuid4()),
                canonical_origin="http://localhost",
                created_by="test",
                user_id=case.user_id,
                user_auth_revision=1,
            )
        )

    with pytest.raises(ValueError, match="exceed the client's approved scopes"):
        issue_session(case, client_id=client_id)
    with case.database.immediate_session() as session:
        client = session.get(AutomationClient, client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read", "app.run"]

    issued = issue_session(case, client_id=client_id)
    principal = store.resolve(issued.raw_token)
    assert principal is not None and principal.client_id == client_id
    with case.database.immediate_session() as session:
        client = session.get(AutomationClient, client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read"]
    assert store.resolve(issued.raw_token) is None

    with case.database.immediate_session() as session:
        client = session.get(AutomationClient, client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read", "app.run"]
        client.user_auth_revision = 2
    assert store.resolve(issued.raw_token) is None
    with pytest.raises(ValueError, match="client is missing"):
        issue_session(case, client_id=client_id)


@pytest.mark.parametrize(
    "approved_scopes",
    ([], "app.read", ["unknown-scope"]),
)
def test_actual_client_rejects_malformed_or_empty_approved_scope_lists(
    browser_session_case: BrowserSessionCase,
    approved_scopes: Any,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    client_id = str(uuid4())
    with case.database.immediate_session() as session:
        session.add(
            AutomationClient(
                id=client_id,
                name="test client",
                subject=f"automation:{client_id}",
                redirect_uris_json=[],
                allowed_scopes_json=cast(Any, approved_scopes),
                target_instance_id=str(uuid4()),
                canonical_origin="http://localhost",
                created_by="test",
                user_id=case.user_id,
                user_auth_revision=1,
            )
        )

    with pytest.raises(ValueError, match="approved scopes are invalid"):
        issue_session(case, client_id=client_id, scopes=["app.read"])
    with case.database.immediate_session() as session:
        client = session.get(AutomationClient, client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read"]
    issued = issue_session(case, client_id=client_id, scopes=["app.read"])
    with case.database.immediate_session() as session:
        client = session.get(AutomationClient, client_id)
        assert client is not None
        client.allowed_scopes_json = cast(Any, approved_scopes)
    assert store.resolve(issued.raw_token) is None


def test_revoke_copy_and_user_revoke_rollback_are_transactional(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    issued = issue_session(case)
    copied_token = str(issued.raw_token)
    with pytest.raises(RuntimeError, match="force rollback"):
        with case.database.session() as session:
            store.revoke_user_in_session(session, case.user_id)
            raise RuntimeError("force rollback")
    assert store.resolve(copied_token) is not None

    store.revoke(copied_token)
    store.revoke(copied_token)
    assert store.resolve(copied_token) is None

    user_session = issue_session(case, subject="second-session")
    store.revoke_user(case.user_id)
    assert store.resolve(user_session.raw_token) is None
    store.revoke_user(case.user_id)


def test_concurrent_revoke_and_resolve_cannot_resurrect_session(
    browser_session_case: BrowserSessionCase,
) -> None:
    case = browser_session_case
    store = BrowserSessionStore(case.database)
    issued = issue_session(case)
    barrier = Barrier(2)

    def resolve_once():
        barrier.wait(timeout=10)
        return store.resolve(issued.raw_token)

    def revoke_once():
        barrier.wait(timeout=10)
        store.revoke(issued.raw_token)

    with ThreadPoolExecutor(max_workers=2) as executor:
        resolved_future = executor.submit(resolve_once)
        revoked_future = executor.submit(revoke_once)
        resolved = resolved_future.result(timeout=15)
        revoked_future.result(timeout=15)

    assert resolved is None or resolved.session_id == issued.record_id
    assert store.resolve(issued.raw_token) is None
    with case.database.snapshot_session() as session:
        record = session.get(BrowserAuthSession, issued.record_id)
        assert record is not None and record.revoked_at is not None
