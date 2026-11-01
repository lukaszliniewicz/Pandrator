"""Real account demotion closes legacy and structured host-reference admission."""

from __future__ import annotations

import os
from unittest.mock import Mock

import pytest
from sqlalchemy import select

from pandrator.web import credentials
from pandrator.web.accounts.auth_models import UserCredential
from pandrator.web.accounts.models import Workspace
from pandrator.web.models import Provider
from tests.test_account_authentication_integration import (
    AuthCase,
)
from tests.test_account_authentication_integration import (
    case as case,  # noqa: F401
)
from tests.test_collaboration_authentication import PASSWORD, enable, new_account


@pytest.fixture(autouse=True)
def synthetic_host_only(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setattr(os, "environ", {"PATH": os.defpath})
    forbidden = Mock(side_effect=AssertionError("Unexpected host credential effect"))
    monkeypatch.setattr(credentials, "_load_keyring", forbidden)
    monkeypatch.setattr(credentials, "_read_secret_file", forbidden)
    return forbidden


def test_demotion_closes_host_reference_admission_and_metadata(
    case: AuthCase, monkeypatch: pytest.MonkeyPatch, synthetic_host_only: Mock
) -> None:
    replacement = new_account(case, "replacement-administrator", "administrator")
    enable(case, monkeypatch)

    def login(name: str):
        client = case.app.test_client()
        response = client.post(
            "/api/v1/auth/login", json={"login_name": name, "password": PASSWORD}
        )
        assert response.status_code == 200
        return client, {"X-CSRF-Token": response.get_json()["csrf_token"]}

    administrator, admin_headers = login(replacement.login_name)
    original = case.auth.initial_account()
    with case.database.snapshot_session() as db:
        revision = db.get_one(UserCredential, original.user_id).auth_revision
        workspace_id = db.scalar(
            select(Workspace.id).where(Workspace.personal_owner_user_id == original.user_id)
        )
    assert workspace_id is not None
    demotion = administrator.patch(
        f"/api/v1/admin/users/{original.user_id}",
        json={"expected_auth_revision": revision, "role": "user"},
        headers={**admin_headers, "Idempotency-Key": "demote-host-reference-owner"},
    )
    assert demotion.status_code == 200
    client, headers = login(original.login_name)
    status = client.get("/api/v1/auth/status").get_json()["principal"]
    assert status["role"] == "user" and "app.admin" not in status["scopes"]
    assert client.get("/api/v1/admin/users").status_code == 403
    scope = {"workspace_id": workspace_id}
    for route in ("providers", "credentials"):
        metadata = client.get(f"/api/v1/{route}", query_string=scope)
        assert metadata.status_code == 200
        assert metadata.get_json()["credential_backends"] == ["database"]

    created = client.post(
        "/api/v1/providers",
        query_string=scope,
        json={
            "provider_key": "openai",
            "label": "Retained personal database connection",
            "base_url": "https://example.invalid/v1",
            "options": {"is_custom": True},
            "credential_backend": "database",
            "api_key": "synthetic-personal-database-value",
        },
        headers=headers,
    )
    assert created.status_code == 201
    saved = created.get_json()
    assert saved["credential_configured"] is True
    assert saved["credential_source"] == "database"
    references = (
        "env:PANDRATOR_SYNTHETIC_HOST",
        "keyring:Pandrator/synthetic-host",
        "file-path:/synthetic/never-read-secret",
    )
    for reference in references:
        rejected = client.post(
            "/api/v1/providers",
            query_string=scope,
            json={
                "provider_key": "openai",
                "label": "Rejected host reference",
                "secret_ref": reference,
            },
            headers=headers,
        )
        assert rejected.status_code == 422
        assert rejected.get_json()["error"]["code"] == "credential_unavailable"
        rejected_update = client.patch(
            f"/api/v1/providers/{saved['id']}",
            query_string=scope,
            json={"secret_ref": reference},
            headers={**headers, "If-Match": '"1"'},
        )
        assert rejected_update.status_code == 422
        assert rejected_update.get_json()["error"]["code"] == "credential_unavailable"
    with case.database.snapshot_session() as db:
        retained = db.get_one(Provider, saved["id"])
        assert retained.revision == 1 and retained.secret_ref == saved["secret_ref"]
        assert (
            list(
                db.scalars(
                    select(Provider.id).where(
                        Provider.workspace_id == workspace_id,
                        Provider.label == "Rejected host reference",
                    )
                )
            )
            == []
        )
    synthetic_host_only.assert_not_called()
