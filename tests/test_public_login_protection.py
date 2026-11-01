"""HTTP regressions for account login throttling and optional security events."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from pandrator.web.api import create_app
from pandrator.web.models import AppSetting
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def app_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    apps = []

    def build(
        name: str,
        *,
        collaboration: bool = False,
        security_log: str | None = None,
        proxy_hops: int = 0,
    ):
        if security_log is None:
            monkeypatch.delenv("PANDRATOR_SECURITY_LOG", raising=False)
        else:
            monkeypatch.setenv("PANDRATOR_SECURITY_LOG", security_log)

        data_root = tmp_path / name
        prepare_web_test_data_root(data_root)
        app = create_app(
            data_root=data_root,
            testing=True,
            background_maintenance=False,
            proxy_hops=proxy_hops,
        )
        services = app.extensions["pandrator"]
        if collaboration:
            with services["database"].immediate_session() as session:
                session.add(
                    AppSetting(
                        key="auth.installation",
                        value_json={"schema_version": 1, "mode": "collaboration"},
                        revision=1,
                    )
                )
        apps.append(app)
        return app, services

    yield build

    for app in apps:
        app.extensions["pandrator"]["database"].dispose()


def _stub_login(auth, monkeypatch: pytest.MonkeyPatch):
    calls: list[str | None] = []

    def reject(login_name: str | None, _password: str):
        calls.append(login_name)
        return None

    monkeypatch.setattr(auth, "authenticate_login", reject)
    return calls


def test_distributed_failures_lock_normalized_account_before_auth(app_factory, monkeypatch):
    app, services = app_factory("account-throttle", collaboration=True)
    auth = services["auth"]
    assert auth.collaboration_enabled()
    calls = _stub_login(auth, monkeypatch)
    client = app.test_client()

    responses = [
        client.post(
            "/api/v1/auth/login",
            json={
                "login_name": "Alice" if index % 2 == 0 else " aLiCe ",
                "password": "wrong-password",
            },
            environ_overrides={"REMOTE_ADDR": f"192.0.2.{10 + index % 5}"},
        )
        for index in range(20)
    ]

    assert all(response.status_code == 401 for response in responses)
    assert "Retry-After" not in responses[-2].headers
    assert "Retry-After" in responses[-1].headers
    assert len(calls) == 20

    blocked = client.post(
        "/api/v1/auth/login",
        json={"login_name": "ALICE", "password": "wrong-password"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.10"},
    )
    assert blocked.status_code == 429
    assert blocked.get_json()["error"]["code"] == "login_throttled"
    assert len(calls) == 20

    independent_account = client.post(
        "/api/v1/auth/login",
        json={"login_name": "bob", "password": "wrong-password"},
        environ_overrides={"REMOTE_ADDR": "203.0.113.11"},
    )
    assert independent_account.status_code == 401
    assert len(calls) == 21


def test_solo_owner_aliases_match_and_loopback_remains_unthrottled(app_factory, monkeypatch):
    app, services = app_factory("solo-loopback")
    auth = services["auth"]
    password = "disposable owner password"
    auth.initialize_owner(password)
    absent_alias = auth.authenticate_login(None, password)
    named_alias = auth.authenticate_login("OWNER", password)
    assert absent_alias is not None and named_alias is not None
    assert absent_alias.user_id == named_alias.user_id

    calls = _stub_login(auth, monkeypatch)
    client = app.test_client()
    for _ in range(21):
        response = client.post(
            "/api/v1/auth/login",
            json={"password": "wrong-password"},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        assert response.status_code == 401
        assert "Retry-After" not in response.headers
    assert len(calls) == 21


def test_existing_ip_threshold_still_blocks_after_five_failures(app_factory, monkeypatch):
    app, services = app_factory("ip-throttle", collaboration=True)
    auth = services["auth"]
    calls = _stub_login(auth, monkeypatch)
    services["login_throttle"].max_attempts = 5
    client = app.test_client()
    remote = {"REMOTE_ADDR": "198.51.100.19"}

    failures = [
        client.post(
            "/api/v1/auth/login",
            json={"login_name": "alice", "password": "wrong-password"},
            environ_overrides=remote,
        )
        for _ in range(5)
    ]
    assert all(response.status_code == 401 for response in failures)
    assert "Retry-After" in failures[-1].headers

    blocked = client.post(
        "/api/v1/auth/login",
        json={"login_name": "alice", "password": "wrong-password"},
        environ_overrides=remote,
    )
    assert blocked.status_code == 429
    assert len(calls) == 5


def test_jsonl_events_use_trusted_ip_static_routes_and_omit_credentials(
    app_factory, monkeypatch, capsys
):
    app, services = app_factory(
        "security-events", collaboration=True, security_log="stdout", proxy_hops=0
    )
    auth = services["auth"]
    calls = _stub_login(auth, monkeypatch)
    client = app.test_client()

    ipv4_login = "private-login-ipv4-marker"
    ipv4_password = "private-password-ipv4-marker"
    ipv4 = client.post(
        "/api/v1/auth/login",
        json={"login_name": ipv4_login, "password": ipv4_password},
        headers={"X-Forwarded-For": "198.51.100.201"},
        environ_overrides={"REMOTE_ADDR": "192.0.2.40"},
    )
    ipv6_login = "private-login-ipv6-marker"
    ipv6_password = "private-password-ipv6-marker"
    ipv6 = client.post(
        "/api/v1/auth/login",
        json={"login_name": ipv6_login, "password": ipv6_password},
        headers={"X-Forwarded-For": "198.51.100.202"},
        environ_overrides={"REMOTE_ADDR": "2001:db8::42"},
    )
    assert ipv4.status_code == ipv6.status_code == 401

    services["login_throttle"].max_attempts = 2
    throttled_ip = "198.51.100.57"
    throttled_login_markers = [f"throttle-login-marker-{index}" for index in range(3)]
    throttled_password_markers = [f"throttle-password-marker-{index}" for index in range(3)]
    throttle_responses = [
        client.post(
            "/api/v1/auth/login",
            json={
                "login_name": throttled_login_markers[index],
                "password": throttled_password_markers[index],
            },
            environ_overrides={"REMOTE_ADDR": throttled_ip},
        )
        for index in range(len(throttled_login_markers))
    ]
    assert [response.status_code for response in throttle_responses] == [401, 401, 429]
    assert len(calls) == 4

    redemption_token = "private-redemption-token-marker"
    link_id = str(uuid4())
    redemption_responses = [
        client.post(
            f"/api/v1/public-links/{link_id}/redeem",
            json={"token": redemption_token},
            environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
        )
        for _ in range(6)
    ]
    assert [response.status_code for response in redemption_responses] == [
        403,
        403,
        403,
        403,
        403,
        429,
    ]

    lines = [line for line in capsys.readouterr().out.splitlines() if line]
    records = [json.loads(line) for line in lines]
    assert records
    assert all(record["method"] == "POST" for record in records)

    auth_failures = [record for record in records if record["event"] == "authentication_failed"]
    assert any(
        record["client_ip"] == "192.0.2.40"
        and record["route"] == "/api/v1/auth/login"
        and record["status"] == 401
        for record in auth_failures
    )
    assert any(
        record["client_ip"] == "2001:db8::42"
        and record["route"] == "/api/v1/auth/login"
        and record["status"] == 401
        for record in auth_failures
    )
    assert "198.51.100.201" not in {record["client_ip"] for record in records}
    assert any(
        record["event"] == "authentication_throttled"
        and record["client_ip"] == throttled_ip
        and record["route"] == "/api/v1/auth/login"
        and record["status"] == 429
        for record in records
    )
    assert any(
        record["event"] == "project_link_redemption_failed"
        and record["client_ip"] == "127.0.0.1"
        and record["route"] == "/api/v1/public-links/<link_id>/redeem"
        and record["status"] == 403
        for record in records
    )
    assert any(
        record["event"] == "project_link_redemption_throttled"
        and record["client_ip"] == "127.0.0.1"
        and record["route"] == "/api/v1/public-links/<link_id>/redeem"
        and record["status"] == 429
        for record in records
    )
    serialized = json.dumps(records)
    for marker in (
        ipv4_login,
        ipv4_password,
        ipv6_login,
        ipv6_password,
        *throttled_login_markers,
        *throttled_password_markers,
        redemption_token,
        link_id,
        "198.51.100.202",
    ):
        assert marker not in serialized


def test_disabled_security_sink_emits_no_records_or_file(app_factory, monkeypatch, capsys):
    app, services = app_factory("disabled-security-events", security_log="none")
    auth = services["auth"]
    _stub_login(auth, monkeypatch)
    sink = app.extensions["pandrator_security_events"]
    assert not sink.enabled

    response = app.test_client().post(
        "/api/v1/auth/login",
        json={"login_name": "private-disabled-login", "password": "private-disabled-password"},
        environ_overrides={"REMOTE_ADDR": "192.0.2.91"},
    )
    assert response.status_code == 401
    assert capsys.readouterr().out == ""
    data_root = services["paths"].root
    assert not list(Path(data_root).rglob("*security*.jsonl"))
