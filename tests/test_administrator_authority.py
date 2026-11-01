"""Synthetic account tests for the explicit Administrator mutation boundary."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator, cast
from uuid import uuid4

import pytest
from sqlalchemy import Table

from pandrator.web.accounts.actor_authorization import _has_required_scopes
from pandrator.web.accounts.administration import (
    PRIVILEGED_JOB_SCOPES,
    require_administrator_actor,
)
from pandrator.web.accounts.auth_contracts import ALL_SCOPES
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.execution_actor import (
    HostActor,
    UserActor,
    execution_actor_scope,
)
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.auth import Principal
from pandrator.web.database import Database
from pandrator.web.model_base import Base, utcnow
from pandrator.web.models import ApiToken, AutomationClient

_SAFE_DENIAL = "An active Administrator credential with the required scopes is required."


@dataclass(frozen=True)
class AdministratorCase:
    database: Database
    user_id: str
    token_id: str
    workspace_id: str


@pytest.fixture
def administrator_case(tmp_path: Path) -> Iterator[AdministratorCase]:
    database = Database(tmp_path / "administrator-authority.sqlite3")
    Base.metadata.create_all(
        database.engine,
        tables=cast(
            list[Table],
            [
                User.__table__,
                UserCredential.__table__,
                AutomationClient.__table__,
                ApiToken.__table__,
                Workspace.__table__,
                WorkspaceMembership.__table__,
            ],
        ),
    )
    user_id = str(uuid4())
    token_id = str(uuid4())
    workspace_id = str(uuid4())
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
        session.add(
            ApiToken(
                id=token_id,
                label="Synthetic API token",
                token_hash="f" * 64,
                token_prefix="f" * 12,
                subject="synthetic-admin-subject",
                scopes_json=["app.write"],
                principal_kind="api_token",
                user_id=user_id,
                user_auth_revision=1,
                target_instance_id=str(uuid4()),
                canonical_origin="https://synthetic.invalid",
            )
        )
        session.add(
            Workspace(
                id=workspace_id,
                kind="personal",
                name="Synthetic personal workspace",
                personal_owner_user_id=user_id,
                state="active",
                revision=1,
            )
        )
        session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace_id,
                user_id=user_id,
                role="owner",
            )
        )
    try:
        yield AdministratorCase(database, user_id, token_id, workspace_id)
    finally:
        database.dispose()


def user_actor(
    case: AdministratorCase,
    scopes: frozenset[str] = frozenset({"app.write"}),
    *,
    principal_kind: str = "api_token",
) -> UserActor:
    return UserActor(
        user_id=case.user_id,
        auth_revision=1,
        subject="synthetic-admin-subject",
        principal_kind=cast(Any, principal_kind),
        scopes=scopes,
        token_id=case.token_id,
    )


def require_as(
    case: AdministratorCase,
    actor: UserActor | HostActor,
    **kwargs: Any,
) -> Any:
    with execution_actor_scope(actor):
        return require_administrator_actor(case.database, **kwargs)


def assert_denied(
    case: AdministratorCase,
    actor: UserActor | HostActor,
    **kwargs: Any,
) -> None:
    with pytest.raises(PermissionError) as error:
        require_as(case, actor, **kwargs)
    assert str(error.value) == _SAFE_DENIAL


def test_admin_scope_is_explicit_in_principal_and_fresh_grant_checks() -> None:
    principal = Principal(
        subject="synthetic",
        kind="api_token",
        scopes=frozenset({"app.admin"}),
        token_id=None,
        network_zone="loopback",
        target_instance_id="synthetic",
    )
    assert principal.has_scope("app.admin")
    for scope in (
        "app.read",
        "app.write",
        "app.run",
        "manager.read",
        "manager.mutate",
        "manager.runtime",
    ):
        assert not principal.has_scope(scope)
        assert not _has_required_scopes(frozenset({"app.admin"}), frozenset({scope}))
    assert _has_required_scopes(frozenset({"app.admin"}), frozenset({"app.admin"}))
    assert ALL_SCOPES >= {
        "app.admin",
        "app.read",
        "app.write",
        "app.run",
        "manager.read",
        "manager.mutate",
        "manager.runtime",
    }


def test_privileged_job_scope_map_is_immutable_and_exact() -> None:
    assert dict(PRIVILEGED_JOB_SCOPES) == {
        "rvc.model.upload": frozenset({"app.admin", "app.write"}),
        "training.xtts": frozenset({"app.admin", "app.run"}),
    }
    with pytest.raises(TypeError):
        PRIVILEGED_JOB_SCOPES["other"] = frozenset({"app.admin"})  # type: ignore[index]
    with pytest.raises(AttributeError):
        PRIVILEGED_JOB_SCOPES["rvc.model.upload"].add("app.read")  # type: ignore[attr-defined]


def test_current_administrator_with_explicit_scopes_is_authorized(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    actor = user_actor(case, frozenset({"app.admin", "app.write"}))
    with case.database.immediate_session() as session:
        user = session.get(User, case.user_id)
        token = session.get(ApiToken, case.token_id)
        assert user is not None and token is not None
        user.is_instance_admin = True
        token.scopes_json = ["app.admin", "app.write"]

    account = require_as(case, actor, required_scopes={"app.write"})
    assert account is not None
    assert account.user_id == case.user_id
    assert account.is_instance_admin
    assert_denied(case, actor, required_scopes={"manager.mutate"})


def test_admin_role_without_explicit_admin_scope_is_denied(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    with case.database.immediate_session() as session:
        user = session.get(User, case.user_id)
        assert user is not None
        user.is_instance_admin = True
    assert_denied(case, user_actor(case))


def test_app_admin_claim_without_current_admin_role_is_denied(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    actor = user_actor(case, frozenset({"app.admin", "app.write"}))
    with case.database.immediate_session() as session:
        token = session.get(ApiToken, case.token_id)
        assert token is not None
        token.scopes_json = ["app.admin", "app.write"]
    assert_denied(case, actor)


def test_manager_bootstrap_never_promotes_even_a_current_admin(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    with case.database.immediate_session() as session:
        user = session.get(User, case.user_id)
        token = session.get(ApiToken, case.token_id)
        assert user is not None and token is not None
        user.is_instance_admin = True
        token.principal_kind = "manager_bootstrap"
        token.scopes_json = ["app.write"]
    actor = user_actor(
        case,
        frozenset({"app.write"}),
        principal_kind="manager_bootstrap",
    )
    assert_denied(case, actor)
    with pytest.raises(ValueError, match="execution actor is invalid"):
        user_actor(case, frozenset({"app.admin"}), principal_kind="manager_bootstrap")


def test_workspace_owner_is_not_an_instance_administrator(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    actor = user_actor(case, frozenset({"app.admin", "app.write"}))
    with case.database.snapshot_session() as session:
        membership = session.get(WorkspaceMembership, (case.workspace_id, case.user_id))
        user = session.get(User, case.user_id)
        assert membership is not None and membership.role == "owner"
        assert user is not None and not user.is_instance_admin
    with case.database.immediate_session() as session:
        token = session.get(ApiToken, case.token_id)
        assert token is not None
        token.scopes_json = ["app.admin", "app.write"]
    assert_denied(case, actor)


def test_revoked_expired_or_changed_transport_credentials_are_denied(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    actor = user_actor(case, frozenset({"app.admin", "app.write"}))
    with case.database.immediate_session() as session:
        user = session.get(User, case.user_id)
        token = session.get(ApiToken, case.token_id)
        assert user is not None and token is not None
        user.is_instance_admin = True
        token.scopes_json = ["app.admin", "app.write"]

    assert require_as(case, actor) is not None
    with case.database.immediate_session() as session:
        token = session.get(ApiToken, case.token_id)
        assert token is not None
        token.revoked_at = utcnow()
    assert_denied(case, actor)

    with case.database.immediate_session() as session:
        token = session.get(ApiToken, case.token_id)
        assert token is not None
        token.revoked_at = None
        token.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    assert_denied(case, actor)

    with case.database.immediate_session() as session:
        token = session.get(ApiToken, case.token_id)
        credential = session.get(UserCredential, case.user_id)
        assert token is not None and credential is not None
        token.expires_at = None
        credential.auth_revision = 2
    assert_denied(case, actor)


def test_missing_actor_and_host_default_and_explicit_allowance(
    administrator_case: AdministratorCase,
) -> None:
    case = administrator_case
    with pytest.raises(PermissionError, match="^" + _SAFE_DENIAL + "$"):
        require_administrator_actor(case.database)
    host = HostActor("pandrator-cli")
    assert_denied(case, host)
    assert require_as(case, host, allow_host=True) is None


@pytest.mark.parametrize(
    "required_scopes",
    [
        ["app.write", ""],
        ["app.write", 3],
        {"not.a.scope"},
        123,
        "   ",
    ],
)
def test_malformed_scope_requests_are_rejected_with_safe_error(
    administrator_case: AdministratorCase,
    required_scopes: object,
) -> None:
    case = administrator_case
    actor = user_actor(case, frozenset({"app.admin", "app.write"}))
    with pytest.raises(PermissionError) as error:
        require_as(case, actor, required_scopes=required_scopes)
    assert str(error.value) == _SAFE_DENIAL
