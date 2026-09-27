"""MCP session-purge and future trash-retention contracts."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.session_purge import (
    DeleteSessionPermanentlyInput,
    GetSessionTrashPolicyInput,
    PreviewSessionDeletionInput,
    UpdateSessionTrashPolicyInput,
)
from pandrator_mcp.tools.session_purge import (
    delete_session_permanently,
    get_session_trash_policy,
    preview_session_deletion,
    update_session_trash_policy,
)


def test_permanent_delete_requires_explicit_confirmation_and_preview_token():
    valid = DeleteSessionPermanentlyInput(
        session_id=" session ",
        expected_revision=4,
        impact_token="a" * 64,
        confirm=True,
    )
    assert valid.session_id == "session"
    with pytest.raises(ValidationError):
        DeleteSessionPermanentlyInput(
            session_id="s",
            expected_revision=4,
            impact_token="a" * 64,
        )
    with pytest.raises(ValidationError):
        DeleteSessionPermanentlyInput(
            session_id="s",
            expected_revision=4,
            impact_token="a" * 64,
            confirm=False,
        )
    with pytest.raises(ValidationError):
        DeleteSessionPermanentlyInput(
            session_id="s",
            expected_revision=4,
            impact_token="not-a-digest",
            confirm=True,
        )


def test_trash_policy_is_revision_guarded_and_nullable_within_bounds():
    assert UpdateSessionTrashPolicyInput(expected_revision=0, days=None).days is None
    assert UpdateSessionTrashPolicyInput(expected_revision=8, days=3650).days == 3650
    with pytest.raises(ValidationError):
        UpdateSessionTrashPolicyInput(expected_revision=0, days=0)
    with pytest.raises(ValidationError):
        UpdateSessionTrashPolicyInput(expected_revision=0, days=3651)
    with pytest.raises(ValidationError):
        UpdateSessionTrashPolicyInput(expected_revision=0)
    assert GetSessionTrashPolicyInput().model_dump() == {}


def test_purge_and_policy_client_routes_preserve_preconditions():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"state": "complete"})

    client.preview_session_deletion("session/a")
    assert client._request_json.call_args.args == (
        "/api/v1/sessions/session%2Fa/purge-preview",
    )

    client.delete_session_permanently(
        "session/a", expected_revision=9, impact_token="b" * 64
    )
    args, kwargs = client._request_json.call_args
    assert args == ("/api/v1/sessions/session%2Fa/purge",)
    assert kwargs == {
        "method": "POST",
        "body": {"expected_revision": 9, "impact_token": "b" * 64},
    }

    client.get_session_trash_policy()
    assert client._request_json.call_args.args == ("/api/v1/session-trash-policy",)
    client.update_session_trash_policy(expected_revision=1, days=None)
    args, kwargs = client._request_json.call_args
    assert args == ("/api/v1/session-trash-policy",)
    assert kwargs == {
        "method": "PATCH",
        "body": {"expected_revision": 1, "days": None},
    }


def test_purge_tools_pass_through_backend_state_and_strip_mcp_confirmation():
    application = Mock()
    application.preview_session_deletion.return_value = {
        "can_purge": True,
        "revision": 3,
        "impact_token": "c" * 64,
    }
    application.delete_session_permanently.return_value = {"state": "complete"}
    application.get_session_trash_policy.return_value = {"revision": 0, "days": None}
    application.update_session_trash_policy.return_value = {"revision": 1, "days": 30}
    runtime = Mock()
    runtime.require_application.return_value = application

    assert preview_session_deletion(
        runtime, PreviewSessionDeletionInput(session_id="s")
    ).result["can_purge"]
    deleted = delete_session_permanently(
        runtime,
        DeleteSessionPermanentlyInput(
            session_id="s",
            expected_revision=3,
            impact_token="c" * 64,
            confirm=True,
        ),
    )
    assert deleted.result == {"state": "complete"}
    application.delete_session_permanently.assert_called_once_with(
        "s", expected_revision=3, impact_token="c" * 64
    )
    assert get_session_trash_policy(
        runtime, GetSessionTrashPolicyInput()
    ).result == {"revision": 0, "days": None}
    assert update_session_trash_policy(
        runtime, UpdateSessionTrashPolicyInput(expected_revision=0, days=30)
    ).result == {"revision": 1, "days": 30}


def test_catalog_marks_deletion_destructive_and_policy_as_revision_guarded():
    preview = ACTION_CATALOG.get("pandrator_preview_session_deletion")
    assert preview.risk == RiskClass.READ
    assert preview.downstream_operation_id == "getSessionPurgePreview"

    deletion = ACTION_CATALOG.get("pandrator_delete_session_permanently")
    assert deletion.risk == RiskClass.WRITE
    assert deletion.requires_confirmation
    assert deletion.downstream_operation_id == "purgeSession"
    assert deletion.path == "/api/v1/sessions/{sessionId}/purge"

    policy_read = ACTION_CATALOG.get("pandrator_get_session_trash_policy")
    policy_write = ACTION_CATALOG.get("pandrator_update_session_trash_policy")
    assert policy_read.risk == RiskClass.READ
    assert policy_read.downstream_operation_id == "getSessionTrashPolicy"
    assert policy_write.risk == RiskClass.WRITE
    assert policy_write.downstream_operation_id == "updateSessionTrashPolicy"
