"""Explicit model and Manager mutations require current Administrator delegation."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from flask import Flask
from flask.testing import FlaskClient
from sqlalchemy import func, select

from pandrator.web.accounts.auth_contracts import PrincipalKind
from pandrator.web.accounts.commands import AccountActor, AccountCommands
from pandrator.web.api import create_app
from pandrator.web.auth import ALL_SCOPES, MCP_BOOTSTRAP_SCOPES, AuthService, BootstrapTokenStore
from pandrator.web.database import Database
from pandrator.web.models import ApiToken, Job, utcnow
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class RouteCase:
    app: Flask
    client: FlaskClient
    database: Database
    auth: AuthService
    downstream: Mock
    catalogue: Mock
    user_id: str
    auth_revision: int


@pytest.fixture
def case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[RouteCase]:
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    launch = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    services = app.extensions["pandrator"]
    # Exercise ordinary queue admission with the same authenticated view wrapper.
    job_view = next(
        view for name, view in app.view_functions.items() if name.endswith(".job_create")
    )
    app.add_url_rule(
        "/api/v1/synthetic/ordinary-jobs",
        endpoint="synthetic_job_create",
        view_func=job_view,
        methods=["POST"],
    )
    downstream = Mock(return_value=({"id": "synthetic-plan", "digest": "a" * 64}, 201))
    monkeypatch.setattr(services["manager_bridge"], "request_json", downstream)
    catalogue = Mock(return_value=({"services": []}, 0))
    monkeypatch.setattr(services["tts_catalogue"], "snapshot", catalogue)
    monkeypatch.delenv("PANDRATOR_ALLOW_REMOTE_MANAGER_MUTATIONS", raising=False)
    client = app.test_client()
    try:
        response = client.post("/api/v1/auth/bootstrap", json={"token": launch})
        assert response.status_code == 200
        account = services["auth"].initial_account()
        downstream.reset_mock()
        catalogue.reset_mock()
        yield RouteCase(
            app,
            client,
            services["database"],
            services["auth"],
            downstream,
            catalogue,
            account.user_id,
            account.auth_revision,
        )
    finally:
        services["quick_transcriptions"].stop_maintenance()
        services["database"].dispose()


def token_for(
    case: RouteCase,
    *,
    scopes: frozenset[str] = ALL_SCOPES,
    user_id: str | None = None,
    auth_revision: int | None = None,
    kind: PrincipalKind = "api_token",
) -> tuple[ApiToken, str]:
    return case.auth.create_api_token(
        "synthetic model authority",
        scopes=scopes,
        user_id=user_id or case.user_id,
        auth_revision=auth_revision or case.auth_revision,
        principal_kind=kind,
    )


MUTATIONS = [
    ("POST", "/api/v1/manager/plans"),
    ("POST", "/api/v1/manager/operations"),
    ("POST", "/api/v1/manager/operations/synthetic/cancel"),
    ("POST", "/api/v1/manager/runtime/start"),
    ("POST", "/api/v1/rvc/models"),
    ("POST", "/api/v1/services/tts/xtts/models"),
    ("DELETE", "/api/v1/services/tts/xtts/models/synthetic"),
    ("POST", "/api/v1/training"),
    ("POST", "/api/v1/training/synthetic/retry"),
]


@pytest.mark.parametrize("method,path", MUTATIONS)
@pytest.mark.parametrize("authority", ["user", "admin_without_scope", "manager_bootstrap"])
def test_model_mutations_deny_before_body_validation_or_downstream(
    case: RouteCase,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    path: str,
    authority: str,
) -> None:
    scopes = ALL_SCOPES - {"app.admin"}
    identifier, revision = case.user_id, case.auth_revision
    kind: PrincipalKind = "api_token"
    if authority == "user":
        member = AccountCommands(case.database).create_user(
            AccountActor(case.user_id, case.auth_revision, ALL_SCOPES),
            login_name="synthetic-member",
            display_name="Synthetic member",
        )
        identifier, revision = member.user_id, member.auth_revision
        # Isolate the role boundary while named-user resource access remains gated.
        monkeypatch.setattr(case.auth, "solo_access_allowed", lambda _uid, _rev: True)
    elif authority == "manager_bootstrap":
        scopes, kind = MCP_BOOTSTRAP_SCOPES, "manager_bootstrap"
    _record, raw = token_for(
        case, scopes=scopes, user_id=identifier, auth_revision=revision, kind=kind
    )
    response = case.app.test_client().open(
        path,
        method=method,
        json={"invalid-body-must-not-be-parsed": True},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"]["code"] == "administrator_required"
    case.downstream.assert_not_called()
    case.catalogue.assert_not_called()
    with case.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


def test_generic_job_route_cannot_bypass_model_administration(case: RouteCase) -> None:
    _record, raw = token_for(case, scopes=frozenset({"app.admin"}))
    response = case.app.test_client().post(
        "/api/v1/jobs",
        json={"kind": "rvc.model.upload", "payload": {"actor_source": "host"}},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 403, response.get_json()
    assert response.get_json()["error"]["code"] == "administrator_required"
    case.downstream.assert_not_called()
    with case.database.session() as session:
        assert session.scalar(select(func.count()).select_from(Job)) == 0


@pytest.mark.parametrize("scope", ["app.admin", "manager.mutate"])
def test_manager_capabilities_and_admin_scope_are_independent(case: RouteCase, scope: str) -> None:
    _record, raw = token_for(case, scopes=frozenset({scope}))
    response = case.app.test_client().post(
        "/api/v1/manager/operations",
        json={"plan_id": "synthetic", "plan_digest": "a" * 64},
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 403
    case.downstream.assert_not_called()


@pytest.mark.parametrize(
    "path,payload,scope,manager_path",
    [
        (
            "/api/v1/manager/plans",
            {"kind": "install", "desired": {}},
            "manager.read",
            "/v1/plans",
        ),
        (
            "/api/v1/manager/operations",
            {"plan_id": "synthetic", "plan_digest": "a" * 64},
            "manager.mutate",
            "/v1/operations",
        ),
        (
            "/api/v1/manager/runtime/start",
            {"service_ids": ["xtts"]},
            "manager.runtime",
            "/v1/runtime/start",
        ),
    ],
)
def test_administrator_with_exact_capabilities_reaches_manager(
    case: RouteCase, path: str, payload: dict[str, Any], scope: str, manager_path: str
) -> None:
    _record, raw = token_for(case, scopes=frozenset({"app.admin", scope}))
    response = case.app.test_client().post(
        path, json=payload, headers={"Authorization": f"Bearer {raw}"}
    )
    assert response.status_code == 201, response.get_json()
    assert case.downstream.call_count == 1
    assert case.downstream.call_args.args == ("POST", manager_path)


def test_admin_role_does_not_bypass_remote_manager_restriction(case: RouteCase) -> None:
    _record, raw = token_for(case)
    response = case.app.test_client().post(
        "/api/v1/manager/plans",
        json={"kind": "install", "desired": {}},
        headers={"Authorization": f"Bearer {raw}"},
        environ_overrides={"REMOTE_ADDR": "10.2.3.4"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "local_manager_access_required"
    case.downstream.assert_not_called()


@pytest.mark.parametrize(
    "path,payload,error_code",
    [
        (
            "/api/v1/manager/plans",
            {"kind": "install", "desired": {}},
            "administrator_required",
        ),
        ("/api/v1/synthetic/ordinary-jobs", {"kind": "noop"}, "scope_denied"),
    ],
)
def test_revoked_between_principal_resolution_and_admission_has_no_side_effects(
    case: RouteCase,
    monkeypatch: pytest.MonkeyPatch,
    path: str,
    payload: dict[str, Any],
    error_code: str,
) -> None:
    record, raw = token_for(case)
    original = case.auth.solo_access_allowed

    def revoke_after_resolution(user_id: str | None, revision: int | None) -> bool:
        allowed = original(user_id, revision)
        with case.database.immediate_session() as session:
            token = session.get(ApiToken, record.id)
            assert token is not None
            token.revoked_at = utcnow()
        return allowed

    monkeypatch.setattr(case.auth, "solo_access_allowed", revoke_after_resolution)
    response = case.app.test_client().post(
        path,
        json=payload,
        headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == error_code
    case.downstream.assert_not_called()


def test_read_only_model_catalogue_remains_available_without_admin_scope(case: RouteCase) -> None:
    _record, raw = token_for(case, scopes=frozenset({"app.read"}))
    response = case.app.test_client().get(
        "/api/v1/services/models/catalogue?limit=1", headers={"Authorization": f"Bearer {raw}"}
    )
    assert response.status_code == 200, response.get_json()
    case.downstream.assert_not_called()
