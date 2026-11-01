from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

import pandrator.web.accounts.commands as commands_module
from pandrator.web.accounts.auth_models import (
    BrowserAuthSession,
    PasswordResetGrant,
    UserCredential,
)
from pandrator.web.accounts.commands import (
    AccountActor,
    AccountCommandError,
    AccountCommands,
    AccountSummary,
    IssuedResetGrant,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.accounts.passwords import (
    _dummy_password_hash,
    dummy_verify,
    hash_password,
    mirror_initial_owner_password,
    password_needs_rehash,
    validate_password,
    verify_password,
)
from pandrator.web.database import Database
from pandrator.web.model_base import Base
from pandrator.web.models import (
    ApiToken,
    AutomationClient,
    AutomationEnrollmentGrant,
    OwnerAccount,
)

NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=10)
OWNER_ID = "00000000-0000-4000-8000-000000000001"
OTHER_ID = "00000000-0000-4000-8000-000000000002"
THIRD_ID = "00000000-0000-4000-8000-000000000003"
OWNER_WORKSPACE_ID = "00000000-0000-4000-8000-000000000101"
_CONFIGURED_HASH = hash_password("initial administrator fixture password")


@pytest.fixture
def account_database(tmp_path: Path) -> Iterator[Database]:
    database = Database(tmp_path / "account-commands.sqlite3")
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
            id=OWNER_ID,
            legacy_subject="owner",
            display_name="Initial owner",
            is_active=True,
            is_instance_admin=True,
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(user)
        session.flush([user])
        credential = UserCredential(
            user_id=OWNER_ID,
            login_name="owner",
            password_hash=_CONFIGURED_HASH,
            auth_revision=1,
            created_at=OLD,
            updated_at=OLD,
        )
        workspace = Workspace(
            id=OWNER_WORKSPACE_ID,
            kind="personal",
            name="Initial owner",
            personal_owner_user_id=OWNER_ID,
            state="active",
            revision=1,
            created_at=OLD,
            updated_at=OLD,
        )
        session.add_all([credential, workspace])
        session.flush([credential, workspace])
        session.add(
            WorkspaceMembership(
                workspace_id=OWNER_WORKSPACE_ID,
                user_id=OWNER_ID,
                role="owner",
                created_at=OLD,
            )
        )
        session.add(
            OwnerAccount(
                singleton_id=1,
                password_hash=_CONFIGURED_HASH,
                created_at=OLD,
                updated_at=OLD,
            )
        )
    try:
        yield database
    finally:
        database.dispose()


def _actor(
    user_id: str = OWNER_ID,
    *,
    auth_revision: int = 1,
    scopes: frozenset[str] = frozenset({"app.admin"}),
) -> AccountActor:
    return AccountActor(user_id, auth_revision, scopes)


def _commands(database: Database) -> AccountCommands:
    return AccountCommands(database, clock=lambda: NOW)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _insert_user(
    database: Database,
    *,
    user_id: str = OTHER_ID,
    login_name: str = "other",
    display_name: str = "Other user",
    admin: bool = False,
    active: bool = True,
    password_hash: str | None = None,
    auth_revision: int = 1,
    legacy_subject: str | None = None,
) -> None:
    with database.immediate_session() as session:
        user = User(
            id=user_id,
            legacy_subject=legacy_subject,
            display_name=display_name,
            is_active=active,
            is_instance_admin=admin,
            created_at=OLD,
            updated_at=OLD,
        )
        session.add(user)
        session.flush([user])
        session.add(
            UserCredential(
                user_id=user_id,
                login_name=login_name,
                password_hash=password_hash,
                auth_revision=auth_revision,
                created_at=OLD,
                updated_at=OLD,
            )
        )


def _insert_reset_grant(
    session: Session,
    *,
    user_id: str,
    raw_token: str,
    created_by_user_id: str = OWNER_ID,
    auth_revision: int = 1,
    expires_at: datetime = NOW + timedelta(minutes=30),
    consumed_at: datetime | None = None,
    revoked_at: datetime | None = None,
) -> PasswordResetGrant:
    grant = PasswordResetGrant(
        user_id=user_id,
        created_by_user_id=created_by_user_id,
        token_hash=hashlib.sha256(raw_token.encode("ascii")).hexdigest(),
        auth_revision=auth_revision,
        created_at=NOW,
        expires_at=expires_at,
        consumed_at=consumed_at,
        revoked_at=revoked_at,
    )
    session.add(grant)
    session.flush([grant])
    return grant


def _add_bound_credentials(session: Session, *, user_id: str, user_auth_revision: int) -> None:
    session.add_all(
        [
            BrowserAuthSession(
                id=f"browser-{user_id}",
                user_id=user_id,
                token_hash=hashlib.sha256(f"browser-{user_id}".encode()).hexdigest(),
                auth_revision=user_auth_revision,
                subject=f"user:{user_id}",
                principal_kind="browser_session",
                scopes_json=["app.admin"],
                created_at=NOW,
                last_seen_at=NOW,
                idle_expires_at=NOW + timedelta(hours=1),
                expires_at=NOW + timedelta(days=1),
            ),
            ApiToken(
                id=f"api-{user_id}",
                label="bound token",
                token_hash=hashlib.sha256(f"api-{user_id}".encode()).hexdigest(),
                token_prefix=f"bound-{user_id[-2:]}",
                user_id=user_id,
                user_auth_revision=user_auth_revision,
                created_at=NOW,
            ),
        ]
    )


def test_password_helpers_bound_inputs_and_preserve_password_text() -> None:
    password = "  пароль 東京 e\u0301  "
    assert len(password) >= 15
    assert validate_password(password) == password
    encoded = hash_password(password)
    assert verify_password(encoded, password)
    assert not verify_password(encoded, password + "x")
    assert password_needs_rehash(encoded) is False

    legacy_encoded = hash_password("legacy pass", minimum_length=10)
    assert verify_password(legacy_encoded, "legacy pass")
    assert not verify_password("not an encoded hash", password)
    assert not verify_password(encoded, "x" * 1025)
    assert not verify_password(encoded, "\ud800")

    with pytest.raises(ValueError) as short_password:
        hash_password("too short")
    assert "too short" not in str(short_password.value)
    with pytest.raises(ValueError):
        hash_password("\ud800" * 15)
    with pytest.raises(ValueError):
        validate_password("x" * 1025)

    before = _dummy_password_hash.cache_info()
    dummy_verify("unknown login")
    dummy_verify("a different unknown login")
    after = _dummy_password_hash.cache_info()
    assert after.currsize == 1
    assert after.hits >= before.hits + 1


def test_account_creation_normalizes_identity_and_is_safe(account_database: Database) -> None:
    commands = _commands(account_database)
    created = commands.create_user(_actor(), login_name="  New.User_1  ", display_name=" Zoë 東京 ")

    assert created == AccountSummary(
        created.user_id,
        "new.user_1",
        "Zoë 東京",
        "user",
        True,
        1,
    )
    assert [item.name for item in fields(AccountSummary)] == [
        "user_id",
        "login_name",
        "display_name",
        "role",
        "is_active",
        "auth_revision",
        "password_configured",
    ]
    assert created.password_configured is False
    assert _CONFIGURED_HASH not in repr(created)
    with account_database.session() as session:
        user = session.get(User, created.user_id)
        credential = session.get(UserCredential, created.user_id)
        workspace = session.scalar(
            select(Workspace).where(Workspace.personal_owner_user_id == created.user_id)
        )
        assert workspace is not None
        membership = session.get(WorkspaceMembership, (workspace.id, created.user_id))
        assert user is not None and user.legacy_subject is None
        assert credential is not None
        assert credential.login_name == "new.user_1"
        assert credential.password_hash is None
        assert credential.auth_revision == 1
        assert workspace.kind == "personal"
        assert workspace.state == "active"
        assert membership is not None and membership.role == "owner"
        assert session.scalar(select(func.count()).select_from(PasswordResetGrant)) == 0


def test_account_creation_rejects_malformed_identity_and_role(account_database: Database) -> None:
    commands = _commands(account_database)
    for login_name in ("", "has space", "tést", ".starts-with-dot", "a" * 65):
        with pytest.raises(AccountCommandError) as error:
            commands.create_user(_actor(), login_name=login_name, display_name="Name")
        assert error.value.code == "invalid_login_name"

    with pytest.raises(AccountCommandError) as duplicate:
        commands.create_user(_actor(), login_name=" OWNER ", display_name="Duplicate owner")
    assert duplicate.value.code == "login_name_taken"

    for display_name in (" \t", "x" * 256):
        with pytest.raises(AccountCommandError) as error:
            commands.create_user(
                _actor(), login_name=f"name-{len(display_name)}", display_name=display_name
            )
        assert error.value.code == "invalid_display_name"

    with pytest.raises(AccountCommandError) as role_error:
        commands.create_user(_actor(), login_name="new-user", display_name="New user", role="admin")
    assert role_error.value.code == "invalid_role"
    with account_database.session() as session:
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_account_creation_rolls_back_user_credential_and_workspace(
    account_database: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    commands = _commands(account_database)
    monkeypatch.setattr(
        commands_module,
        "WorkspaceMembership",
        lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("membership failure")),
    )

    with pytest.raises(RuntimeError, match="membership failure"):
        commands.create_user(_actor(), login_name="rolled-back", display_name="Rolled back")

    with account_database.session() as session:
        assert (
            session.scalar(
                select(func.count())
                .select_from(UserCredential)
                .where(UserCredential.login_name == "rolled-back")
            )
            == 0
        )
        assert (
            session.scalar(
                select(func.count()).select_from(Workspace).where(Workspace.name == "Rolled back")
            )
            == 0
        )
        assert session.scalar(select(func.count()).select_from(User)) == 1


def test_admin_authority_requires_current_user_role_revision_and_scope(
    account_database: Database,
) -> None:
    commands = _commands(account_database)
    _insert_user(account_database)

    with pytest.raises(AccountCommandError) as ordinary_user:
        commands.list_users(_actor(OTHER_ID, scopes=frozenset({"app.admin", "*​"})))
    assert ordinary_user.value.code == "stale_actor"

    with pytest.raises(AccountCommandError) as no_scope:
        commands.list_users(_actor(scopes=frozenset({"app.read"})))
    assert no_scope.value.code == "forbidden"

    with pytest.raises(AccountCommandError) as stale_epoch:
        commands.list_users(_actor(auth_revision=2))
    assert stale_epoch.value.code == "stale_actor"

    summaries = commands.list_users(_actor())
    assert [summary.login_name for summary in summaries] == ["other", "owner"]
    assert all(isinstance(summary, AccountSummary) for summary in summaries)

    with account_database.immediate_session() as session:
        owner = session.get(User, OWNER_ID)
        assert owner is not None
        owner.is_active = False
    with pytest.raises(AccountCommandError) as disabled_actor:
        commands.list_users(_actor())
    assert disabled_actor.value.code == "stale_actor"


def test_last_active_admin_needs_a_configured_successor(account_database: Database) -> None:
    commands = _commands(account_database)
    with pytest.raises(AccountCommandError) as no_successor:
        commands.update_user(_actor(), OWNER_ID, expected_auth_revision=1, role="user")
    assert no_successor.value.code == "last_administrator"
    with account_database.session() as session:
        credential = session.get(UserCredential, OWNER_ID)
        user = session.get(User, OWNER_ID)
        assert credential is not None and credential.auth_revision == 1
        assert user is not None and user.is_instance_admin is True

    _insert_user(
        account_database,
        user_id=OTHER_ID,
        login_name="administrator-two",
        admin=True,
    )
    with pytest.raises(AccountCommandError) as unconfigured_successor:
        commands.update_user(_actor(), OWNER_ID, expected_auth_revision=1, role="user")
    assert unconfigured_successor.value.code == "last_administrator"
    with account_database.immediate_session() as session:
        successor_credential = session.get(UserCredential, OTHER_ID)
        assert successor_credential is not None
        successor_credential.password_hash = _CONFIGURED_HASH
    with pytest.raises(AccountCommandError) as stale_target_revision:
        commands.update_user(
            _actor(), OWNER_ID, expected_auth_revision=2, display_name="Stale change"
        )
    assert stale_target_revision.value.code == "stale_account"
    result = commands.update_user(_actor(), OWNER_ID, expected_auth_revision=1, role="user")
    assert result.role == "user"
    assert result.auth_revision == 2


def test_display_rename_does_not_change_auth_epoch_or_revoke_sessions(
    account_database: Database,
) -> None:
    with account_database.immediate_session() as session:
        _add_bound_credentials(session, user_id=OWNER_ID, user_auth_revision=1)

    result = _commands(account_database).update_user(
        _actor(), OWNER_ID, expected_auth_revision=1, display_name="Renamed owner"
    )
    assert result.display_name == "Renamed owner"
    assert result.auth_revision == 1
    with account_database.session() as session:
        browser = session.get(BrowserAuthSession, f"browser-{OWNER_ID}")
        api_token = session.get(ApiToken, f"api-{OWNER_ID}")
        assert browser is not None and browser.revoked_at is None
        assert api_token is not None and api_token.revoked_at is None


def test_two_administrators_cannot_retire_each_other_concurrently(
    account_database: Database,
) -> None:
    _insert_user(
        account_database,
        user_id=OTHER_ID,
        login_name="administrator-two",
        admin=True,
        password_hash=_CONFIGURED_HASH,
    )
    commands = _commands(account_database)
    gate = threading.Barrier(2)

    def retire_self(actor: AccountActor, user_id: str) -> str:
        gate.wait(timeout=5)
        try:
            commands.update_user(actor, user_id, expected_auth_revision=1, is_active=False)
        except AccountCommandError as error:
            return error.code
        return "retired"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(
            executor.map(
                lambda entry: retire_self(*entry),
                [(_actor(), OWNER_ID), (_actor(OTHER_ID), OTHER_ID)],
            )
        )

    assert sorted(outcomes) == ["last_administrator", "retired"]
    with account_database.session() as session:
        viable_admins = session.scalar(
            select(func.count())
            .select_from(User)
            .join(UserCredential, UserCredential.user_id == User.id)
            .where(
                User.is_active.is_(True),
                User.is_instance_admin.is_(True),
                UserCredential.password_hash.is_not(None),
                UserCredential.password_hash != "",
            )
        )
        assert viable_admins == 1


def test_security_update_revokes_only_credentials_bound_to_target(
    account_database: Database,
) -> None:
    _insert_user(
        account_database,
        user_id=OTHER_ID,
        login_name="administrator-two",
        admin=True,
        password_hash=_CONFIGURED_HASH,
    )
    _insert_user(account_database, user_id=THIRD_ID, login_name="third-user")
    with account_database.immediate_session() as session:
        _add_bound_credentials(session, user_id=OWNER_ID, user_auth_revision=1)
        _add_bound_credentials(session, user_id=THIRD_ID, user_auth_revision=1)
        session.add(
            ApiToken(
                id="unbound-token",
                label="legacy token",
                token_hash="d" * 64,
                token_prefix="legacy",
                created_at=NOW,
            )
        )
        for suffix, user_id in (("owner", OWNER_ID), ("third", THIRD_ID)):
            client_id = f"client-{suffix}"
            session.add(
                AutomationClient(
                    id=client_id,
                    name=f"{suffix} client",
                    subject=f"subject:{suffix}",
                    redirect_uris_json=["https://example.invalid/callback"],
                    allowed_scopes_json=["app.read"],
                    target_instance_id="test-instance",
                    canonical_origin="https://example.invalid",
                    created_by="test",
                    user_id=user_id,
                    user_auth_revision=1,
                    created_at=NOW,
                )
            )
        session.flush()
        session.add(
            AutomationClient(
                id="unbound-client",
                name="unbound client",
                subject="subject:unbound",
                redirect_uris_json=[],
                allowed_scopes_json=[],
                target_instance_id="test-instance",
                canonical_origin="https://example.invalid",
                created_by="test",
                created_at=NOW,
            )
        )
        for index, client_id in enumerate(("client-owner", "client-third", "unbound-client")):
            session.add(
                AutomationEnrollmentGrant(
                    id=f"enrollment-{index}",
                    client_id=client_id,
                    code_hash=f"{index + 1:064x}",
                    code_prefix=f"code-{index}",
                    code_challenge="challenge",
                    expires_at=NOW + timedelta(minutes=5),
                    token_expires_at=NOW + timedelta(hours=1),
                    created_at=NOW,
                )
            )
        _insert_reset_grant(session, user_id=OWNER_ID, raw_token="pending-owner-reset")
        _insert_reset_grant(
            session,
            user_id=THIRD_ID,
            raw_token="pending-third-reset",
            created_by_user_id=OTHER_ID,
        )

    result = _commands(account_database).update_user(
        _actor(), OWNER_ID, expected_auth_revision=1, is_active=False
    )
    assert result.auth_revision == 2

    with account_database.session() as session:
        owner_browser = session.get(BrowserAuthSession, f"browser-{OWNER_ID}")
        third_browser = session.get(BrowserAuthSession, f"browser-{THIRD_ID}")
        owner_api = session.get(ApiToken, f"api-{OWNER_ID}")
        third_api = session.get(ApiToken, f"api-{THIRD_ID}")
        unbound_api = session.get(ApiToken, "unbound-token")
        owner_client = session.get(AutomationClient, "client-owner")
        third_client = session.get(AutomationClient, "client-third")
        unbound_client = session.get(AutomationClient, "unbound-client")
        owner_grant = session.get(AutomationEnrollmentGrant, "enrollment-0")
        third_grant = session.get(AutomationEnrollmentGrant, "enrollment-1")
        unbound_grant = session.get(AutomationEnrollmentGrant, "enrollment-2")
        assert owner_browser is not None and _as_utc(owner_browser.revoked_at) == NOW
        assert third_browser is not None and third_browser.revoked_at is None
        assert owner_api is not None and _as_utc(owner_api.revoked_at) == NOW
        assert third_api is not None and third_api.revoked_at is None
        assert unbound_api is not None and unbound_api.revoked_at is None
        assert owner_client is not None and _as_utc(owner_client.revoked_at) == NOW
        assert third_client is not None and third_client.revoked_at is None
        assert unbound_client is not None and unbound_client.revoked_at is None
        assert owner_grant is not None and _as_utc(owner_grant.consumed_at) == NOW
        assert third_grant is not None and third_grant.consumed_at is None
        assert unbound_grant is not None and unbound_grant.consumed_at is None
        owner_reset = session.scalar(
            select(PasswordResetGrant).where(PasswordResetGrant.user_id == OWNER_ID)
        )
        third_reset = session.scalar(
            select(PasswordResetGrant).where(PasswordResetGrant.user_id == THIRD_ID)
        )
        assert owner_reset is not None and _as_utc(owner_reset.revoked_at) == NOW
        assert third_reset is not None and third_reset.revoked_at is None


def test_demoting_reset_issuer_revokes_delegated_grants_only(
    account_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _insert_user(
        account_database,
        user_id=OTHER_ID,
        login_name="administrator-two",
        admin=True,
        password_hash=_CONFIGURED_HASH,
    )
    _insert_user(account_database, user_id=THIRD_ID, login_name="third-user")
    commands = _commands(account_database)
    delegated_to_admin = commands.issue_password_reset(_actor(), OTHER_ID, expected_auth_revision=1)
    unrelated_from_unchanged_admin = commands.issue_password_reset(
        _actor(OTHER_ID), THIRD_ID, expected_auth_revision=1
    )

    commands.update_user(_actor(OTHER_ID), OWNER_ID, expected_auth_revision=1, role="user")
    with account_database.session() as session:
        delegated_row = session.get(PasswordResetGrant, delegated_to_admin.grant_id)
        unrelated_row = session.get(PasswordResetGrant, unrelated_from_unchanged_admin.grant_id)
        assert delegated_row is not None and delegated_row.revoked_at is not None
        assert unrelated_row is not None and unrelated_row.revoked_at is None

    def no_hash(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("revoked delegated grant must not reach Argon2 work")

    with monkeypatch.context() as patcher:
        patcher.setattr(commands_module, "hash_password", no_hash)
        with pytest.raises(AccountCommandError) as invalid_delegated_grant:
            commands.redeem_password_reset(
                delegated_to_admin.raw_token, "a valid replacement password"
            )
    assert invalid_delegated_grant.value.code == "invalid_reset"

    assert (
        commands.redeem_password_reset(
            unrelated_from_unchanged_admin.raw_token,
            "a valid replacement password for the third user",
        )
        is None
    )
    with account_database.session() as session:
        admin_credential = session.get(UserCredential, OTHER_ID)
        third_credential = session.get(UserCredential, THIRD_ID)
        assert admin_credential is not None
        assert admin_credential.auth_revision == 1
        assert admin_credential.password_hash == _CONFIGURED_HASH
        assert third_credential is not None
        assert third_credential.auth_revision == 2
        assert third_credential.password_hash is not None
        assert verify_password(
            third_credential.password_hash,
            "a valid replacement password for the third user",
        )


def test_reset_issuance_stores_only_digest_and_revokes_prior_unused_grants(
    account_database: Database,
) -> None:
    commands = _commands(account_database)
    created = commands.create_user(_actor(), login_name="new-user", display_name="New user")
    with account_database.session() as session:
        assert session.scalar(select(func.count()).select_from(PasswordResetGrant)) == 0
        credential = session.get(UserCredential, created.user_id)
        assert credential is not None
        before_password_hash = credential.password_hash

    with pytest.raises(AccountCommandError) as short_ttl:
        commands.issue_password_reset(
            _actor(), created.user_id, expected_auth_revision=1, ttl_seconds=59
        )
    assert short_ttl.value.code == "invalid_reset_ttl"
    with pytest.raises(AccountCommandError) as long_ttl:
        commands.issue_password_reset(
            _actor(), created.user_id, expected_auth_revision=1, ttl_seconds=3601
        )
    assert long_ttl.value.code == "invalid_reset_ttl"

    first = commands.issue_password_reset(
        _actor(), created.user_id, expected_auth_revision=1, ttl_seconds=60
    )
    second = commands.issue_password_reset(
        _actor(), created.user_id, expected_auth_revision=1, ttl_seconds=3600
    )
    assert first.expires_at == NOW + timedelta(seconds=60)
    assert second.expires_at == NOW + timedelta(seconds=3600)
    assert isinstance(first, IssuedResetGrant)
    assert first.raw_token not in repr(first)
    with account_database.session() as session:
        first_row = session.get(PasswordResetGrant, first.grant_id)
        second_row = session.get(PasswordResetGrant, second.grant_id)
        credential = session.get(UserCredential, created.user_id)
        assert first_row is not None
        assert second_row is not None
        assert first_row.token_hash == hashlib.sha256(first.raw_token.encode()).hexdigest()
        assert second_row.token_hash == hashlib.sha256(second.raw_token.encode()).hexdigest()
        assert first.raw_token not in first_row.token_hash
        assert _as_utc(first_row.revoked_at) == NOW
        assert second_row.revoked_at is None
        assert credential is not None
        assert before_password_hash is None
        assert credential.password_hash is None
        assert credential.auth_revision == 1


@pytest.mark.parametrize(
    "invalid_state",
    ("absent", "expired", "used", "revoked", "stale_revision", "inactive", "non_ascii"),
)
def test_invalid_reset_proofs_are_generic_and_checked_before_hashing(
    account_database: Database,
    monkeypatch: pytest.MonkeyPatch,
    invalid_state: str,
) -> None:
    commands = _commands(account_database)
    target = commands.create_user(_actor(), login_name="reset-target", display_name="Reset target")
    raw_token = f"reset-token-{invalid_state}"
    if invalid_state not in {"absent", "non_ascii"}:
        with account_database.immediate_session() as session:
            _insert_reset_grant(
                session,
                user_id=target.user_id,
                raw_token=raw_token,
                expires_at=NOW - timedelta(seconds=1)
                if invalid_state == "expired"
                else NOW + timedelta(minutes=5),
                consumed_at=NOW if invalid_state == "used" else None,
                revoked_at=NOW if invalid_state == "revoked" else None,
            )
            if invalid_state == "stale_revision":
                credential = session.get(UserCredential, target.user_id)
                assert credential is not None
                credential.auth_revision = 2
            if invalid_state == "inactive":
                user = session.get(User, target.user_id)
                assert user is not None
                user.is_active = False

    def no_hash(*_args: object, **_kwargs: object) -> str:
        raise AssertionError("invalid proof must be rejected before Argon2 work")

    monkeypatch.setattr(commands_module, "hash_password", no_hash)
    submitted_token = "\ud800" if invalid_state == "non_ascii" else raw_token
    with pytest.raises(AccountCommandError) as error:
        commands.redeem_password_reset(submitted_token, "a valid but unneeded password")
    assert error.value.code == "invalid_reset"
    assert str(error.value) == "The password reset link is invalid or expired."
    assert raw_token not in str(error.value)


def test_reset_success_is_exactly_once_and_revokes_bound_state(
    account_database: Database,
) -> None:
    _insert_user(
        account_database,
        user_id=OTHER_ID,
        login_name="administrator-two",
        admin=True,
        password_hash=_CONFIGURED_HASH,
    )
    with account_database.immediate_session() as session:
        _add_bound_credentials(session, user_id=OWNER_ID, user_auth_revision=1)
    commands = _commands(account_database)
    issued = commands.issue_password_reset(_actor(OTHER_ID), OWNER_ID, expected_auth_revision=1)
    with account_database.immediate_session() as session:
        other_grant = _insert_reset_grant(
            session,
            user_id=OWNER_ID,
            raw_token="another-pending-reset",
        )
        other_grant.id = "other-pending-reset-id"
    new_password = "a newly configured Unicode password 東京"

    assert commands.redeem_password_reset(issued.raw_token, new_password) is None
    with account_database.session() as session:
        credential = session.get(UserCredential, OWNER_ID)
        user = session.get(User, OWNER_ID)
        legacy = session.get(OwnerAccount, 1)
        current_grant = session.get(PasswordResetGrant, issued.grant_id)
        other = session.get(PasswordResetGrant, "other-pending-reset-id")
        reset_browser = session.get(BrowserAuthSession, f"browser-{OWNER_ID}")
        reset_api = session.get(ApiToken, f"api-{OWNER_ID}")
        assert credential is not None and credential.auth_revision == 2
        assert credential.password_hash is not None
        assert verify_password(credential.password_hash, new_password)
        assert user is not None and _as_utc(user.updated_at) == NOW
        assert legacy is not None
        assert legacy.password_hash == credential.password_hash
        assert _as_utc(legacy.created_at) == OLD
        assert _as_utc(legacy.updated_at) == NOW
        assert current_grant is not None
        assert _as_utc(current_grant.consumed_at) == NOW
        assert current_grant.revoked_at is None
        assert other is not None and _as_utc(other.revoked_at) == NOW
        assert reset_browser is not None and _as_utc(reset_browser.revoked_at) == NOW
        assert reset_api is not None and _as_utc(reset_api.revoked_at) == NOW

    with pytest.raises(AccountCommandError) as replay:
        commands.redeem_password_reset(issued.raw_token, "another valid password here")
    assert replay.value.code == "invalid_reset"


def test_two_concurrent_redemptions_consume_a_grant_once(
    account_database: Database,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    commands = _commands(account_database)
    target = commands.create_user(_actor(), login_name="race-target", display_name="Race target")
    issued = commands.issue_password_reset(_actor(), target.user_id, expected_auth_revision=1)
    hash_gate = threading.Barrier(2)

    def simultaneous_hash(_password: str, *, minimum_length: int = 15) -> str:
        assert minimum_length == 15
        hash_gate.wait(timeout=5)
        return "test-encoded-password"

    monkeypatch.setattr(commands_module, "hash_password", simultaneous_hash)

    def redeem() -> str:
        try:
            commands.redeem_password_reset(issued.raw_token, "concurrent reset password value")
        except AccountCommandError as error:
            return error.code
        return "redeemed"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _index: redeem(), (1, 2)))
    assert sorted(outcomes) == ["invalid_reset", "redeemed"]
    with account_database.session() as session:
        credential = session.get(UserCredential, target.user_id)
        grant = session.get(PasswordResetGrant, issued.grant_id)
        assert credential is not None and credential.auth_revision == 2
        assert credential.password_hash == "test-encoded-password"
        assert grant is not None and _as_utc(grant.consumed_at) == NOW


def test_invalid_new_password_does_not_consume_reset_proof(
    account_database: Database,
) -> None:
    commands = _commands(account_database)
    target = commands.create_user(
        _actor(), login_name="short-reset-target", display_name="Short reset"
    )
    issued = commands.issue_password_reset(_actor(), target.user_id, expected_auth_revision=1)

    with pytest.raises(AccountCommandError) as invalid_password:
        commands.redeem_password_reset(issued.raw_token, "too short")
    assert invalid_password.value.code == "invalid_password"
    with account_database.session() as session:
        credential = session.get(UserCredential, target.user_id)
        grant = session.get(PasswordResetGrant, issued.grant_id)
        assert credential is not None and credential.auth_revision == 1
        assert credential.password_hash is None
        assert grant is not None and grant.consumed_at is None


def test_reset_issuance_refuses_disabled_target(account_database: Database) -> None:
    commands = _commands(account_database)
    target = commands.create_user(
        _actor(), login_name="disabled-target", display_name="Disabled target"
    )
    with account_database.immediate_session() as session:
        user = session.get(User, target.user_id)
        assert user is not None
        user.is_active = False

    with pytest.raises(AccountCommandError) as error:
        commands.issue_password_reset(_actor(), target.user_id, expected_auth_revision=1)
    assert error.value.code == "account_disabled"
    with account_database.session() as session:
        assert session.scalar(select(func.count()).select_from(PasswordResetGrant)) == 0


def test_initial_owner_password_mirror_uses_structural_identity_only(
    account_database: Database,
) -> None:
    _insert_user(account_database, user_id=OTHER_ID, legacy_subject="not-owner")
    replacement_hash = "replacement-encoded-value"
    with account_database.immediate_session() as session:
        owner = session.get(User, OWNER_ID)
        assert owner is not None
        owner.is_active = False
        owner.is_instance_admin = False
    with account_database.immediate_session() as session:
        mirror_initial_owner_password(
            session,
            user_id=OTHER_ID,
            encoded=replacement_hash,
            updated_at=NOW,
        )
    with account_database.session() as session:
        legacy = session.get(OwnerAccount, 1)
        assert legacy is not None
        assert legacy.password_hash == _CONFIGURED_HASH
        assert _as_utc(legacy.created_at) == OLD

    with account_database.immediate_session() as session:
        mirror_initial_owner_password(
            session,
            user_id=OWNER_ID,
            encoded=replacement_hash,
            updated_at=NOW,
        )
    with account_database.session() as session:
        legacy = session.get(OwnerAccount, 1)
        assert legacy is not None
        assert legacy.password_hash == replacement_hash
        assert _as_utc(legacy.created_at) == OLD
        assert _as_utc(legacy.updated_at) == NOW

    with account_database.immediate_session() as session:
        mirror_initial_owner_password(session, user_id=OWNER_ID, encoded=None, updated_at=NOW)
    with account_database.session() as session:
        assert session.get(OwnerAccount, 1) is None
