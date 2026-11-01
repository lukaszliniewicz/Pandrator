"""Named-user recovery keeps global administration separate from content."""

from __future__ import annotations

import socket
from typing import Any

import pytest
from sqlalchemy import func, select
from werkzeug.test import TestResponse

from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.models import AuditEvent
from tests.test_account_authentication_integration import AuthCase
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_two_person_collaboration_flow import _headers, _login


def _body(response: TestResponse, status: int) -> dict[str, Any]:
    result = response.get_json()
    assert response.status_code == status, result
    assert isinstance(result, dict)
    return result


def test_administrator_recovers_member_governance_without_joining_or_reading_content(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch
) -> None:
    alice_account = new_account(case, login="alice")
    bob_account = new_account(case, login="bob")
    enable(case, monkeypatch)

    def blocked_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Recovery must not use the network.")

    monkeypatch.setattr(socket.socket, "connect", blocked_network)
    administrator, administrator_csrf, administrator_id = _login(case, "owner")
    alice, alice_csrf, _alice_id = _login(case, "alice")
    bob, bob_csrf, _bob_id = _login(case, "bob")
    team = _body(
        alice.post(
            "/api/v1/workspaces",
            json={"name": "Private team needing a Manager"},
            headers=_headers(alice_csrf, "recovery-team-create"),
        ),
        201,
    )
    invitation = _body(
        alice.post(
            f"/api/v1/workspaces/{team['id']}/invitations",
            json={
                "user_id": bob_account.user_id,
                "role": "member",
                "expected_revision": team["revision"],
            },
            headers=_headers(alice_csrf, "recovery-invite-member"),
        ),
        201,
    )
    _body(
        bob.post(
            f"/api/v1/workspace-invitations/{invitation['id']}/accept",
            json={},
            headers=_headers(bob_csrf, "recovery-accept-member"),
        ),
        200,
    )
    source = _body(
        alice.post(
            "/api/v1/sessions",
            json={
                "name": "Team-owned project",
                "workflow_kind": "subtitles",
                "workspace_id": team["id"],
            },
            headers=_headers(alice_csrf, "recovery-team-project"),
        ),
        201,
    )
    private = _body(
        alice.post(
            "/api/v1/sessions",
            json={"name": "Alice private work", "workflow_kind": "subtitles"},
            headers=_headers(alice_csrf, "recovery-personal-project"),
        ),
        201,
    )
    endpoint = f"/api/v1/admin/workspace-recovery/{team['id']}/manager"
    assert _body(administrator.get("/api/v1/admin/workspace-recovery"), 200)["items"] == []
    assert bob.get("/api/v1/admin/workspace-recovery").status_code == 403
    assert (
        bob.patch(
            f"/api/v1/workspaces/{team['id']}",
            json={"name": "Member cannot govern yet", "expected_revision": 3},
            headers=bob_csrf,
        ).status_code
        == 404
    )
    not_needed = _body(
        administrator.post(
            endpoint,
            json={"user_id": bob_account.user_id, "expected_revision": 3},
            headers=administrator_csrf,
        ),
        409,
    )
    assert not_needed["error"]["code"] == "workspace_manager_recovery_not_needed"

    accounts = _body(administrator.get("/api/v1/admin/users"), 200)["items"]
    alice_record = next(
        account for account in accounts if account["user_id"] == alice_account.user_id
    )
    _body(
        administrator.patch(
            f"/api/v1/admin/users/{alice_account.user_id}",
            json={"expected_auth_revision": alice_record["auth_revision"], "is_active": False},
            headers=_headers(administrator_csrf, "recovery-disable-manager"),
        ),
        200,
    )
    assert alice.get(f"/api/v1/sessions/{source['id']}").status_code == 401
    assert bob.get(f"/api/v1/sessions/{source['id']}").status_code == 200
    assert administrator.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    assert administrator.get(f"/api/v1/workspaces/{team['id']}").status_code == 404

    recovery = _body(administrator.get("/api/v1/admin/workspace-recovery?limit=1"), 200)
    assert recovery["has_more"] is False
    assert len(recovery["items"]) == 1
    item = recovery["items"][0]
    assert set(item) == {"id", "name", "state", "revision", "candidates"}
    assert item["id"] == team["id"]
    assert item["candidates"] == [{"user_id": bob_account.user_id, "display_name": "Bob"}]
    body = {"user_id": bob_account.user_id, "expected_revision": item["revision"]}
    assert administrator.post(endpoint, json=body).status_code == 403
    assert (
        administrator.post(
            endpoint, json={**body, "role": "administrator"}, headers=administrator_csrf
        ).status_code
        == 422
    )
    assert (
        administrator.post(
            endpoint,
            json={**body, "expected_revision": item["revision"] + 1},
            headers=administrator_csrf,
        ).status_code
        == 409
    )
    assert bob.post(endpoint, json=body, headers=bob_csrf).status_code == 403
    restored = _body(administrator.post(endpoint, json=body, headers=administrator_csrf), 200)
    assert restored["recovered_user_id"] == bob_account.user_id
    assert restored["revision"] == item["revision"] + 1
    assert _body(administrator.get("/api/v1/admin/workspace-recovery"), 200)["items"] == []
    assert administrator.post(endpoint, json=body, headers=administrator_csrf).status_code == 409
    assert administrator.get(f"/api/v1/sessions/{source['id']}").status_code == 404
    assert administrator.get(f"/api/v1/workspaces/{team['id']}").status_code == 404
    assert administrator.get(f"/api/v1/sessions/{private['id']}").status_code == 404
    assert bob.get(f"/api/v1/sessions/{private['id']}").status_code == 404
    governed = _body(
        bob.patch(
            f"/api/v1/workspaces/{team['id']}",
            json={
                "name": "Member has recovered management",
                "expected_revision": restored["revision"],
            },
            headers=bob_csrf,
        ),
        200,
    )
    assert governed["role"] == "manager"
    assert bob.get(f"/api/v1/sessions/{source['id']}").status_code == 200
    with case.database.snapshot_session() as db:
        assert db.get(WorkspaceMembership, (team["id"], administrator_id)) is None
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "workspace.manager.recover")
            )
            == 1
        )


def test_recovery_routes_remain_closed_in_solo_and_validate_bounded_inputs(case: AuthCase) -> None:
    assert case.client.get("/api/v1/admin/workspace-recovery").status_code == 409
    assert case.client.get("/api/v1/admin/workspace-recovery?limit=101").status_code == 422
    assert case.client.get("/api/v1/admin/workspace-recovery?offset=-1").status_code == 422
    assert (
        case.client.get("/api/v1/admin/workspace-recovery?include_content=true").status_code == 422
    )
    assert (
        case.client.post(
            "/api/v1/admin/workspace-recovery/missing/manager",
            json={"user_id": "missing", "expected_revision": True},
            headers={"X-CSRF-Token": case.csrf},
        ).status_code
        == 422
    )
