from __future__ import annotations

import hashlib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.web.accounts.auth_models import (
    BrowserAuthSession,
    PasswordResetGrant,
    UserCredential,
)
from pandrator.web.accounts.commands import (
    AccountActor,
    AccountCommands,
    IssuedResetGrant,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.database import Database
from pandrator.web.model_base import Base

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=10)
ADMIN_ID = "10000000-0000-4000-8000-000000000001"
TARGET_ID = "10000000-0000-4000-8000-000000000002"
ADMIN_WORKSPACE_ID = "10000000-0000-4000-8000-000000000101"


@pytest.fixture
def account_database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "account-command-transactions.sqlite3")
    Base.metadata.create_all(
        database.engine,
        tables=[
            Base.metadata.tables[table_name]
            for table_name in (
                "users",
                "workspaces",
                "workspace_memberships",
                "user_credentials",
                "browser_auth_sessions",
                "password_reset_grants",
                "automation_clients",
                "automation_enrollment_grants",
                "api_tokens",
                "owner_account",
            )
        ],
    )
    with database.immediate_session() as session:
        user = User(
            id=ADMIN_ID,
            legacy_subject="initial-owner",
            display_name="Initial owner",
            is_active=True,
            is_instance_admin=True,
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(user)
        session.flush([user])
        credential = UserCredential(
            user_id=ADMIN_ID,
            login_name="owner",
            password_hash=None,
            auth_revision=1,
            created_at=OLD,
            updated_at=OLD,
        )
        workspace = Workspace(
            id=ADMIN_WORKSPACE_ID,
            kind="personal",
            name="Initial owner",
            personal_owner_user_id=ADMIN_ID,
            state="active",
            revision=1,
            created_at=OLD,
            updated_at=OLD,
        )
        session.add_all([credential, workspace])
        session.flush([credential, workspace])
        session.add(
            WorkspaceMembership(
                workspace_id=ADMIN_WORKSPACE_ID,
                user_id=ADMIN_ID,
                role="owner",
                created_at=OLD,
            )
        )
    try:
        yield database
    finally:
        database.dispose()


def _actor() -> AccountActor:
    return AccountActor(ADMIN_ID, 1, frozenset({"app.admin"}))


def _commands(database: Database) -> AccountCommands:
    return AccountCommands(database, clock=lambda: NOW)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _insert_target(session: Session) -> None:
    user = User(
        id=TARGET_ID,
        legacy_subject=None,
        display_name="Target user",
        is_active=True,
        is_instance_admin=False,
        created_at=OLD,
        updated_at=OLD,
    )
    session.add(user)
    session.flush([user])
    session.add(
        UserCredential(
            user_id=TARGET_ID,
            login_name="target",
            password_hash=None,
            auth_revision=1,
            created_at=OLD,
            updated_at=OLD,
        )
    )
    session.flush()


def test_create_user_in_session_joins_caller_transaction_and_rolls_back_graph(
    account_database: Database,
) -> None:
    commands = _commands(account_database)
    created_user_id: str | None = None
    with pytest.raises(RuntimeError, match="abort outer command"):
        with account_database.immediate_session() as session:
            created = commands.create_user_in_session(
                session,
                _actor(),
                login_name="  New.User  ",
                display_name=" New user ",
            )
            created_user_id = created.user_id
            user = session.get(User, created.user_id)
            credential = session.get(UserCredential, created.user_id)
            workspace = session.scalar(
                select(Workspace).where(Workspace.personal_owner_user_id == created.user_id)
            )
            assert user is not None and user.display_name == "New user"
            assert credential is not None and credential.login_name == "new.user"
            assert workspace is not None and workspace.kind == "personal"
            assert session.get(WorkspaceMembership, (workspace.id, created.user_id)) is not None
            raise RuntimeError("abort outer command")

    assert created_user_id is not None
    with account_database.session() as session:
        assert session.get(User, created_user_id) is None
        assert session.get(UserCredential, created_user_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(Workspace)
                .where(Workspace.personal_owner_user_id == created_user_id)
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count())
                .select_from(WorkspaceMembership)
                .where(WorkspaceMembership.user_id == created_user_id)
            )
            == 0
        )


def test_security_update_and_credential_revocations_roll_back_together(
    account_database: Database,
) -> None:
    commands = _commands(account_database)
    with account_database.immediate_session() as session:
        _insert_target(session)
        session.add(
            BrowserAuthSession(
                id="target-browser-session",
                user_id=TARGET_ID,
                token_hash=hashlib.sha256(b"target-browser-token").hexdigest(),
                auth_revision=1,
                subject=f"user:{TARGET_ID}",
                principal_kind="browser_session",
                scopes_json=["app.admin"],
                created_at=NOW,
                last_seen_at=NOW,
                idle_expires_at=NOW + timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
            )
        )

    with pytest.raises(RuntimeError, match="abort outer command"):
        with account_database.immediate_session() as session:
            result = commands.update_user_in_session(
                session,
                _actor(),
                TARGET_ID,
                expected_auth_revision=1,
                is_active=False,
            )
            assert result.is_active is False
            credential = session.get(UserCredential, TARGET_ID)
            browser_session = session.get(BrowserAuthSession, "target-browser-session")
            assert credential is not None and credential.auth_revision == 2
            assert browser_session is not None and _as_utc(browser_session.revoked_at) == NOW
            raise RuntimeError("abort outer command")

    with account_database.session() as session:
        user = session.get(User, TARGET_ID)
        credential = session.get(UserCredential, TARGET_ID)
        browser_session = session.get(BrowserAuthSession, "target-browser-session")
        assert user is not None and user.is_active is True
        assert credential is not None and credential.auth_revision == 1
        assert browser_session is not None and browser_session.revoked_at is None


def test_reset_issue_revocation_and_caller_write_roll_back_without_storing_raw_token(
    account_database: Database,
) -> None:
    commands = _commands(account_database)
    old_digest = hashlib.sha256(b"older-reset-secret-value").hexdigest()
    with account_database.immediate_session() as session:
        _insert_target(session)
        previous_grant = PasswordResetGrant(
            user_id=TARGET_ID,
            created_by_user_id=ADMIN_ID,
            token_hash=old_digest,
            auth_revision=1,
            created_at=NOW - timedelta(minutes=5),
            expires_at=NOW + timedelta(minutes=25),
        )
        session.add(previous_grant)
        session.flush([previous_grant])
        previous_grant_id = previous_grant.id

    issued: IssuedResetGrant | None = None
    with pytest.raises(RuntimeError, match="abort outer command"):
        with account_database.immediate_session() as session:
            owner = session.get(User, ADMIN_ID)
            assert owner is not None
            owner.display_name = "Caller pending write"
            issued = commands.issue_password_reset_in_session(
                session,
                _actor(),
                TARGET_ID,
                expected_auth_revision=1,
            )
            previous_grant = session.get(PasswordResetGrant, previous_grant_id)
            assert previous_grant is not None and _as_utc(previous_grant.revoked_at) == NOW
            new_grant = session.get(PasswordResetGrant, issued.grant_id)
            assert new_grant is not None
            assert (
                new_grant.token_hash == hashlib.sha256(issued.raw_token.encode("utf-8")).hexdigest()
            )
            assert new_grant.token_hash != issued.raw_token
            assert (
                session.scalar(
                    select(PasswordResetGrant.id).where(
                        PasswordResetGrant.token_hash == issued.raw_token
                    )
                )
                is None
            )
            raise RuntimeError("abort outer command")

    assert issued is not None
    with account_database.session() as session:
        owner = session.get(User, ADMIN_ID)
        previous_grant = session.get(PasswordResetGrant, previous_grant_id)
        assert owner is not None and owner.display_name == "Initial owner"
        assert previous_grant is not None and previous_grant.revoked_at is None
        assert session.get(PasswordResetGrant, issued.grant_id) is None
        assert (
            session.scalar(
                select(func.count())
                .select_from(PasswordResetGrant)
                .where(PasswordResetGrant.token_hash == issued.raw_token)
            )
            == 0
        )
