"""Scratch-database tests for durable execution actor validation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, cast
from uuid import uuid4

import pytest
from sqlalchemy import Table
from sqlalchemy.orm import Session

from pandrator.web.accounts.actor_authorization import require_actor_in_session
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import BrowserAuthSession, UserCredential
from pandrator.web.accounts.auth_state import AccountState
from pandrator.web.accounts.execution_actor import (
    HostActor,
    PrincipalProtocol,
    UserActor,
    actor_from_principal,
    actor_from_storage,
    actor_to_storage,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User
from pandrator.web.database import Database
from pandrator.web.model_base import Base, utcnow
from pandrator.web.models import ApiToken, AutomationClient


@dataclass(frozen=True)
class ActorCase:
    database: Database
    user_id: str
    token_id: str
    client_id: str
    browser_session_id: str


@pytest.fixture
def actor_case(tmp_path: Path) -> Iterator[ActorCase]:
    database = Database(tmp_path / "execution-actor.sqlite3")
    Base.metadata.create_all(
        database.engine,
        tables=cast(
            list[Table],
            [
                User.__table__,
                UserCredential.__table__,
                ApiToken.__table__,
                AutomationClient.__table__,
                BrowserAuthSession.__table__,
            ],
        ),
    )
    user_id = str(uuid4())
    token_id = str(uuid4())
    client_id = str(uuid4())
    browser_session_id = str(uuid4())
    now = utcnow()
    with database.immediate_session() as session:
        user = User(
            id=user_id,
            legacy_subject=None,
            display_name="Synthetic member",
            is_active=True,
            is_instance_admin=False,
        )
        session.add(user)
        session.flush()
        session.add(
            UserCredential(
                user_id=user_id,
                login_name="synthetic-member",
                password_hash=None,
                auth_revision=1,
            )
        )
        session.flush()
        session.add(
            AutomationClient(
                id=client_id,
                name="Synthetic client",
                subject="synthetic-client-subject",
                redirect_uris_json=[],
                allowed_scopes_json=["app.read", "app.run", "app.write"],
                target_instance_id=str(uuid4()),
                canonical_origin="https://synthetic.invalid",
                created_by="synthetic-test",
                user_id=user_id,
                user_auth_revision=1,
            )
        )
        session.flush()
        session.add(
            ApiToken(
                id=token_id,
                label="Synthetic token",
                token_hash="a" * 64,
                token_prefix="a" * 12,
                subject="synthetic-api-subject",
                scopes_json=["app.read", "app.run", "app.write"],
                principal_kind="api_token",
                user_id=user_id,
                user_auth_revision=1,
                target_instance_id=str(uuid4()),
                canonical_origin="https://synthetic.invalid",
            )
        )
        session.add(
            BrowserAuthSession(
                id=browser_session_id,
                user_id=user_id,
                token_hash="b" * 64,
                auth_revision=1,
                subject="synthetic-browser-subject",
                principal_kind="owner_session",
                scopes_json=["app.read", "app.run", "app.write"],
                created_at=now,
                last_seen_at=now,
                idle_expires_at=now + timedelta(hours=1),
                expires_at=now + timedelta(days=1),
            )
        )
    try:
        yield ActorCase(database, user_id, token_id, client_id, browser_session_id)
    finally:
        database.dispose()


def api_actor(
    case: ActorCase,
    *,
    scopes: frozenset[str] = frozenset({"app.read", "app.run", "app.write"}),
    client_id: str | None = None,
    subject: str = "synthetic-api-subject",
    principal_kind: str = "api_token",
) -> UserActor:
    return UserActor(
        user_id=case.user_id,
        auth_revision=1,
        subject=subject,
        principal_kind=cast(Any, principal_kind),
        scopes=scopes,
        token_id=case.token_id,
        client_id=client_id,
    )


def browser_actor(
    case: ActorCase,
    *,
    scopes: frozenset[str] = frozenset({"app.read", "app.run", "app.write"}),
    client_id: str | None = None,
    subject: str = "synthetic-browser-subject",
    principal_kind: str = "owner_session",
) -> UserActor:
    return UserActor(
        user_id=case.user_id,
        auth_revision=1,
        subject=subject,
        principal_kind=cast(Any, principal_kind),
        scopes=scopes,
        browser_session_id=case.browser_session_id,
        client_id=client_id,
    )


def check_authorized(
    case: ActorCase,
    actor: UserActor | HostActor | None,
    **kwargs: Any,
) -> AccountState | None:
    with case.database.immediate_session() as session:
        return require_actor_in_session(session, actor, **kwargs)


def denied(
    case: ActorCase,
    actor: UserActor | HostActor | None,
    **kwargs: Any,
) -> None:
    with pytest.raises(PermissionError) as error:
        check_authorized(case, actor, **kwargs)
    assert str(error.value) == "The execution actor is not currently authorized."


@dataclass(frozen=True)
class PrincipalStub:
    subject: str
    kind: str
    scopes: frozenset[str]
    token_id: str | None
    browser_session_id: str | None
    client_id: str | None
    user_id: str | None
    auth_revision: int | None


def principal_stub(case: ActorCase, **overrides: Any) -> PrincipalProtocol:
    values: dict[str, Any] = {
        "subject": "synthetic-api-subject",
        "kind": "api_token",
        "scopes": frozenset({"app.read", "app.run", "app.write"}),
        "token_id": case.token_id,
        "browser_session_id": None,
        "client_id": None,
        "user_id": case.user_id,
        "auth_revision": 1,
    }
    values.update(overrides)
    return cast(PrincipalProtocol, PrincipalStub(**values))


def test_principal_snapshot_and_storage_round_trip_are_canonical(
    actor_case: ActorCase,
) -> None:
    actor = actor_from_principal(principal_stub(actor_case))
    assert actor == api_actor(actor_case)
    encoded = actor_to_storage(actor)
    assert encoded is not None
    context = encoded["context_json"]
    assert context == {
        "version": 1,
        "source": "user",
        "subject": "synthetic-api-subject",
        "principal_kind": "api_token",
        "scopes": ["app.read", "app.run", "app.write"],
        "token_id": actor_case.token_id,
        "browser_session_id": None,
        "client_id": None,
    }
    assert "a" * 64 not in repr(encoded)
    assert (
        actor_from_storage(encoded["user_id"], encoded["auth_revision"], encoded["context_json"])
        == actor
    )

    host_encoded = actor_to_storage(HostActor("pandrator-cli"))
    assert host_encoded is not None
    assert host_encoded == {
        "user_id": None,
        "auth_revision": None,
        "context_json": {"version": 1, "source": "host", "label": "pandrator-cli"},
    }
    assert actor_from_storage(
        host_encoded["user_id"],
        host_encoded["auth_revision"],
        host_encoded["context_json"],
    ) == HostActor("pandrator-cli")
    assert actor_from_storage(None, None, None) is None


def test_principal_snapshot_rejects_partial_identity_and_invalid_transports(
    actor_case: ActorCase,
) -> None:
    assert (
        actor_from_principal(
            principal_stub(actor_case, user_id=None, auth_revision=None, token_id=None)
        )
        is None
    )
    for overrides in (
        {"auth_revision": None},
        {"token_id": None},
        {"browser_session_id": actor_case.browser_session_id},
        {"scopes": frozenset()},
        {"scopes": frozenset({"not.a.scope"})},
        {"scopes": frozenset({"app.read", 4})},
        {"kind": "unknown"},
        {"kind": "manager_bootstrap", "scopes": ALL_SCOPES},
    ):
        with pytest.raises(ValueError, match="execution actor is invalid"):
            actor_from_principal(principal_stub(actor_case, **overrides))


@pytest.mark.parametrize(
    "change",
    [
        lambda context: context.update(version=True),
        lambda context: context.update(extra="rejected"),
        lambda context: context.update(scopes=["app.write", "app.read", "app.run"]),
        lambda context: context.update(scopes=["app.read", "app.read"]),
        lambda context: context.update(scopes=["app.read", 7]),
        lambda context: context.update(principal_kind="unknown"),
    ],
)
def test_storage_decoder_rejects_noncanonical_or_unknown_context(
    actor_case: ActorCase,
    change: Any,
) -> None:
    encoded = actor_to_storage(api_actor(actor_case))
    assert encoded is not None
    context = dict(encoded["context_json"])
    change(context)
    with pytest.raises(ValueError, match="execution actor is invalid"):
        actor_from_storage(encoded["user_id"], encoded["auth_revision"], context)


def test_storage_decoder_rejects_pair_source_and_host_shape_mismatches(
    actor_case: ActorCase,
) -> None:
    encoded = actor_to_storage(api_actor(actor_case))
    assert encoded is not None
    with pytest.raises(ValueError):
        actor_from_storage(encoded["user_id"], None, encoded["context_json"])
    with pytest.raises(ValueError):
        actor_from_storage(
            encoded["user_id"],
            encoded["auth_revision"],
            {"version": 1, "source": "host", "label": "pandrator-cli"},
        )
    with pytest.raises(ValueError):
        actor_from_storage(
            None,
            None,
            {"version": 1, "source": "host", "label": "arbitrary-host"},
        )


def test_context_scope_sets_and_resets_none_and_nested_actors() -> None:
    outer = HostActor("pandrator-cli")
    assert current_execution_actor.get() is None
    with execution_actor_scope(outer):
        assert current_execution_actor.get() == outer
        with execution_actor_scope(None):
            assert current_execution_actor.get() is None
        assert current_execution_actor.get() == outer
    assert current_execution_actor.get() is None
    with pytest.raises(RuntimeError):
        with execution_actor_scope(outer):
            raise RuntimeError("synthetic handler failure")
    assert current_execution_actor.get() is None


def test_missing_actor_and_host_policy_are_explicit(actor_case: ActorCase) -> None:
    denied(actor_case, None)
    host = HostActor("pandrator-cli")
    denied(actor_case, host)
    assert check_authorized(actor_case, host, allow_host=True) is None
    with pytest.raises(ValueError):
        HostActor("worker")


def test_api_actor_rechecks_required_and_current_transport_scopes(
    actor_case: ActorCase,
) -> None:
    actor = api_actor(actor_case)
    account = check_authorized(actor_case, actor, required_scopes={"app.write"})
    assert account is not None
    assert account.user_id == actor_case.user_id
    denied(actor_case, actor, required_scopes={"manager.mutate"})
    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        assert token is not None
        token.scopes_json = ["app.read"]
    denied(actor_case, actor)


def test_api_actor_rechecks_revocation_expiry_epoch_and_user_active_state(
    actor_case: ActorCase,
) -> None:
    actor = api_actor(actor_case)
    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        assert token is not None
        token.revoked_at = utcnow()
    denied(actor_case, actor)

    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        credential = session.get(UserCredential, actor_case.user_id)
        assert token is not None and credential is not None
        token.revoked_at = None
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        credential.auth_revision = 2
    denied(actor_case, actor)

    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        credential = session.get(UserCredential, actor_case.user_id)
        user = session.get(User, actor_case.user_id)
        assert token is not None and credential is not None and user is not None
        token.expires_at = None
        credential.auth_revision = 1
        user.is_active = False
    denied(actor_case, actor)


def test_api_actor_rechecks_subject_kind_and_client_identity(
    actor_case: ActorCase,
) -> None:
    actor = api_actor(actor_case)
    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        assert token is not None
        token.subject = "different-subject"
    denied(actor_case, actor)

    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        assert token is not None
        token.subject = "synthetic-api-subject"
        token.principal_kind = "service"
    denied(actor_case, actor)


def test_api_client_must_remain_active_and_keep_approved_scopes(
    actor_case: ActorCase,
) -> None:
    with actor_case.database.immediate_session() as session:
        token = session.get(ApiToken, actor_case.token_id)
        assert token is not None
        token.client_id = actor_case.client_id
    actor = api_actor(actor_case, client_id=actor_case.client_id)
    assert check_authorized(actor_case, actor) is not None
    with actor_case.database.immediate_session() as session:
        client = session.get(AutomationClient, actor_case.client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read"]
    denied(actor_case, actor)
    with actor_case.database.immediate_session() as session:
        client = session.get(AutomationClient, actor_case.client_id)
        assert client is not None
        client.allowed_scopes_json = ["app.read", "app.run", "app.write"]
        client.revoked_at = utcnow()
    denied(actor_case, actor)


def test_browser_session_rechecks_expiry_revocation_and_scopes(
    actor_case: ActorCase,
) -> None:
    actor = browser_actor(actor_case)
    assert check_authorized(actor_case, actor) is not None
    with actor_case.database.immediate_session() as session:
        browser = session.get(BrowserAuthSession, actor_case.browser_session_id)
        assert browser is not None
        browser.revoked_at = utcnow()
    denied(actor_case, actor)

    with actor_case.database.immediate_session() as session:
        browser = session.get(BrowserAuthSession, actor_case.browser_session_id)
        assert browser is not None
        browser.revoked_at = None
        browser.idle_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    denied(actor_case, actor)


def test_browser_client_uses_fresh_owner_and_approved_scope_validation(
    actor_case: ActorCase,
) -> None:
    with actor_case.database.immediate_session() as session:
        browser = session.get(BrowserAuthSession, actor_case.browser_session_id)
        assert browser is not None
        browser.client_id = actor_case.client_id
    actor = browser_actor(actor_case, client_id=actor_case.client_id)
    assert check_authorized(actor_case, actor) is not None
    with actor_case.database.immediate_session() as session:
        client = session.get(AutomationClient, actor_case.client_id)
        assert client is not None
        client.user_auth_revision = 2
    denied(actor_case, actor)


@pytest.mark.parametrize("client_id", ["pandrator-manager-browser", "pandrator-mcp-local"])
def test_manager_browser_provenance_skips_client_row_but_not_user_checks(
    actor_case: ActorCase,
    client_id: str,
) -> None:
    actor = browser_actor(
        actor_case,
        scopes=frozenset({"app.read"}),
        client_id=client_id,
        principal_kind="manager_bootstrap",
    )
    with actor_case.database.immediate_session() as session:
        browser = session.get(BrowserAuthSession, actor_case.browser_session_id)
        assert browser is not None
        browser.client_id = client_id
        browser.principal_kind = "manager_bootstrap"
        browser.scopes_json = ["app.read"]
    assert check_authorized(actor_case, actor) is not None
    with actor_case.database.immediate_session() as session:
        user = session.get(User, actor_case.user_id)
        assert user is not None
        user.is_active = False
    denied(actor_case, actor)


def test_admin_grants_are_fresh_role_bound_and_never_inferred_from_subject(
    actor_case: ActorCase,
) -> None:
    with actor_case.database.immediate_session() as session:
        user = session.get(User, actor_case.user_id)
        token = session.get(ApiToken, actor_case.token_id)
        assert user is not None and token is not None
        user.is_instance_admin = True
        token.scopes_json = sorted(ALL_SCOPES)
    actor = api_actor(actor_case, scopes=ALL_SCOPES)
    account = check_authorized(
        actor_case,
        actor,
        required_scopes={"manager.mutate"},
        require_admin=True,
    )
    assert account is not None and account.is_instance_admin
    with actor_case.database.immediate_session() as session:
        user = session.get(User, actor_case.user_id)
        assert user is not None
        user.is_instance_admin = False
    denied(actor_case, actor)
    denied(actor_case, api_actor(actor_case), require_admin=True)


def test_fresh_column_validation_ignores_stale_orm_identity_map(
    actor_case: ActorCase,
) -> None:
    actor = api_actor(actor_case)
    stale_session = Session(bind=actor_case.database.engine, expire_on_commit=False)
    try:
        stale_user = stale_session.get(User, actor_case.user_id)
        stale_token = stale_session.get(ApiToken, actor_case.token_id)
        assert stale_user is not None and stale_token is not None
        stale_session.commit()

        with actor_case.database.immediate_session() as update_session:
            token = update_session.get(ApiToken, actor_case.token_id)
            assert token is not None
            token.revoked_at = utcnow()

        assert stale_user.is_active is True
        assert stale_token.revoked_at is None
        with pytest.raises(PermissionError):
            require_actor_in_session(stale_session, actor)
    finally:
        stale_session.rollback()
        stale_session.close()
