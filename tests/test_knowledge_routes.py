"""Actual HTTP ledger reads, project rights, explicit writes and audit rollback."""

from __future__ import annotations

import socket
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import func, select
from werkzeug.test import TestResponse

from pandrator.web.models import AuditEvent, KnowledgeLedger
from tests.test_account_authentication_integration import AuthCase
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_collaboration_authentication import enable, new_account
from tests.test_two_person_collaboration_flow import _headers, _login


def _body(response: TestResponse, expected: int) -> dict[str, Any]:
    value = response.get_json()
    assert response.status_code == expected, value
    assert isinstance(value, dict)
    return value


@pytest.fixture
def ledger_case(case: AuthCase, monkeypatch: pytest.MonkeyPatch):
    new_account(case, "alice")
    enable(case, monkeypatch)

    def offline(*_args, **_kwargs):
        raise AssertionError("Ledger HTTP checks must not use the network.")

    monkeypatch.setattr(socket.socket, "connect", offline)
    owner, owner_headers, owner_id = _login(case, "owner")
    alice, alice_headers, _alice_id = _login(case, "alice")
    source = _body(
        alice.post(
            "/api/v1/sessions",
            json={
                "name": "Private ledger source",
                "workflow_kind": "subtitles",
            },
            headers=_headers(alice_headers, "ledger-http-source"),
        ),
        201,
    )
    return SimpleNamespace(
        case=case,
        owner=owner,
        owner_headers=owner_headers,
        owner_id=owner_id,
        alice=alice,
        alice_headers=alice_headers,
        source=source,
        path=f"/api/v1/sessions/{source['id']}/knowledge/glossary",
        scope={"source_language": "en", "target_language": "de"},
    )


def test_ledger_http_is_passive_private_and_requires_current_editing_authority(ledger_case):
    x = ledger_case
    empty = _body(x.alice.get(x.path, query_string=x.scope), 200)
    assert empty["id"] is None and empty["revision"] == 0 and empty["updated_at"] is None
    assert empty["payload"] == {"entries": [], "conflicts": []}
    with x.case.database.snapshot_session() as db:
        assert db.scalar(select(func.count()).select_from(KnowledgeLedger)) == 0
    assert x.owner.get(x.path, query_string=x.scope).status_code == 404
    assert x.alice.post(x.path, json=x.scope).status_code == 403
    ledger = _body(x.alice.post(x.path, json=x.scope, headers=x.alice_headers), 201)
    assert _body(x.alice.post(x.path, json=x.scope, headers=x.alice_headers), 200) == ledger
    update_path = f"/api/v1/knowledge/{ledger['id']}"
    payload = {"entries": [{"source": "course", "target": "Kurs", "locked": True}], "conflicts": []}
    mutation = {**x.owner_headers, "If-Match": '"1"'}
    assert (
        x.owner.patch(update_path, json={"payload": payload}, headers=mutation).status_code == 404
    )

    viewers = f"/api/v1/projects/{x.source['project_id']}/viewers"
    revision = _body(x.alice.get(viewers), 200)["revision"]
    _body(
        x.alice.put(
            f"{viewers}/{x.owner_id}", json={"expected_revision": revision}, headers=x.alice_headers
        ),
        200,
    )
    assert _body(x.owner.get(x.path, query_string=x.scope), 200)["id"] == ledger["id"]
    assert x.owner.post(x.path, json=x.scope, headers=x.owner_headers).status_code == 403
    assert (
        x.owner.patch(update_path, json={"payload": payload}, headers=mutation).status_code == 403
    )

    headers = {**x.alice_headers, "If-Match": '"1"'}
    updated = _body(x.alice.patch(update_path, json={"payload": payload}, headers=headers), 200)
    assert updated["revision"] == 2 and updated["payload"]["entries"][0]["status"] == "active"
    assert x.alice.patch(update_path, json={"payload": payload}, headers=headers).status_code == 409
    assert _body(x.owner.get(x.path, query_string=x.scope), 200)["payload"] == updated["payload"]
    revision = _body(x.alice.get(viewers), 200)["revision"]
    _body(
        x.alice.post(
            f"{viewers}/{x.owner_id}/revoke",
            json={"expected_revision": revision},
            headers=x.alice_headers,
        ),
        200,
    )
    assert x.owner.get(x.path, query_string=x.scope).status_code == 404
    with x.case.database.snapshot_session() as db:
        commands = list(
            db.scalars(
                select(AuditEvent).where(
                    AuditEvent.action.in_(["knowledge.create", "knowledge.update"])
                )
            )
        )
        assert len(commands) == 2
        assert all(
            "course" not in str(row.metadata_json) and "Kurs" not in str(row.metadata_json)
            for row in commands
        )


def test_ledger_http_strict_inputs_and_audit_failure_leave_no_partial_editorial_state(
    ledger_case, monkeypatch
):
    x = ledger_case
    for invalid in ([], {"actor_id": x.owner_id}, {"source_language": True}):
        assert x.alice.post(x.path, json=invalid, headers=x.alice_headers).status_code == 422
    assert (
        x.alice.post(
            x.path, data="null", content_type="application/json", headers=x.alice_headers
        ).status_code
        == 422
    )
    services = x.case.app.extensions["pandrator"]["services"]
    audit = services.audit.record_command_in_session

    def fail_after_audit(*args, **kwargs):
        audit(*args, **kwargs)
        raise RuntimeError("Synthetic audit failure")

    with monkeypatch.context() as patch:
        patch.setattr(services.audit, "record_command_in_session", fail_after_audit)
        with pytest.raises(RuntimeError, match="Synthetic audit failure"):
            x.alice.post(x.path, json=x.scope, headers=x.alice_headers)
    assert _body(x.alice.get(x.path, query_string=x.scope), 200)["id"] is None
    ledger = _body(x.alice.post(x.path, json=x.scope, headers=x.alice_headers), 201)
    path = f"/api/v1/knowledge/{ledger['id']}"
    body = {"payload": {"entries": [], "conflicts": []}}
    for precondition in ('W/"1"', '"1', '1"', '"0"'):
        assert (
            x.alice.patch(
                path, json=body, headers={**x.alice_headers, "If-Match": precondition}
            ).status_code
            == 428
        )
    assert (
        x.alice.patch(
            path,
            json={"payload": {"entries": "wrong"}},
            headers={**x.alice_headers, "If-Match": '"1"'},
        ).status_code
        == 422
    )
    with monkeypatch.context() as patch:
        patch.setattr(services.audit, "record_command_in_session", fail_after_audit)
        with pytest.raises(RuntimeError, match="Synthetic audit failure"):
            x.alice.patch(path, json=body, headers={**x.alice_headers, "If-Match": '"1"'})
    assert _body(x.alice.get(x.path, query_string=x.scope), 200)["revision"] == 1
    with x.case.database.snapshot_session() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "knowledge.create")
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(AuditEvent)
                .where(AuditEvent.action == "knowledge.update")
            )
            == 0
        )


def test_solo_initial_owner_can_use_explicit_ledger_writes_without_enabling_collaboration(
    case: AuthCase,
):
    headers = {"X-CSRF-Token": case.csrf}
    source = _body(
        case.client.post(
            "/api/v1/sessions",
            json={"name": "Solo ledger", "workflow_kind": "subtitles"},
            headers=_headers(headers, "ledger-solo-source"),
        ),
        201,
    )
    path = f"/api/v1/sessions/{source['id']}/knowledge/research"
    assert _body(case.client.get(path), 200)["id"] is None
    saved = _body(case.client.post(path, json={}, headers=headers), 201)
    update = _body(
        case.client.patch(
            f"/api/v1/knowledge/{saved['id']}",
            json={
                "payload": {
                    "evidence": [],
                    "warnings": [],
                    "summary": "A local note.",
                }
            },
            headers={**headers, "If-Match": '"1"'},
        ),
        200,
    )
    assert update["payload"]["summary"] == "A local note."
    assert (
        case.client.get("/api/v1/auth/status").get_json()["installation"]["collaboration_enabled"]
        is False
    )
