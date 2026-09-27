"""MCP voice-setup contract and bounded application routes."""

from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.voice_setup import (
    ConfigureVoiceSetupInput,
    GetVoiceSetupInput,
)
from pandrator_mcp.tools.voice_setup import configure_voice_setup, get_voice_setup


def test_voice_setup_arguments_are_revision_and_idempotency_guarded():
    valid = ConfigureVoiceSetupInput(
        session_id=" session ",
        expected_revision="a" * 64,
        mode="multi_voice",
        idempotency_key="voice-mode-123",
    )
    assert valid.session_id == "session"
    assert valid.mode == "multi_voice"
    with pytest.raises(ValidationError):
        ConfigureVoiceSetupInput(
            session_id="s",
            expected_revision="short",
            mode="multi_voice",
            idempotency_key="voice-mode-123",
        )
    with pytest.raises(ValidationError):
        ConfigureVoiceSetupInput(
            session_id="s",
            expected_revision="a" * 64,
            mode="multi_voice",
            idempotency_key="short",
        )
    with pytest.raises(ValidationError):
        ConfigureVoiceSetupInput(
            session_id="s",
            expected_revision="a" * 64,
            mode="multiple",
            idempotency_key="voice-mode-123",
        )
    with pytest.raises(ValidationError):
        GetVoiceSetupInput(session_id="   ")


def test_voice_setup_client_routes_and_sends_only_api_fields():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"mode": "single_voice"})

    client.get_voice_setup("session/a")
    assert client._request_json.call_args.args == (
        "/api/v1/sessions/session%2Fa/voice-setup",
    )

    client.configure_voice_setup(
        "session/a",
        expected_revision="b" * 64,
        mode="multi_voice",
        idempotency_key="voice-mode-123",
    )
    args, kwargs = client._request_json.call_args
    assert args == ("/api/v1/sessions/session%2Fa/voice-setup",)
    assert kwargs == {
        "method": "PATCH",
        "body": {"expected_revision": "b" * 64, "mode": "multi_voice"},
        "idempotency_key": "voice-mode-123",
        "maximum_body_bytes": 4096,
    }


def test_voice_setup_tools_call_api_and_catalog_keeps_audiobook_tools():
    application = Mock()
    application.get_voice_setup.return_value = {"mode": "single_voice"}
    application.configure_voice_setup.return_value = {"mode": "multi_voice"}
    runtime = Mock()
    runtime.require_application.return_value = application

    read = get_voice_setup(runtime, GetVoiceSetupInput(session_id="s"))
    write = configure_voice_setup(
        runtime,
        ConfigureVoiceSetupInput(
            session_id="s",
            expected_revision="c" * 64,
            mode="multi_voice",
            idempotency_key="voice-mode-123",
        ),
    )
    assert read.result == {"mode": "single_voice"}
    assert write.result == {"mode": "multi_voice"}
    application.configure_voice_setup.assert_called_once_with(
        "s",
        expected_revision="c" * 64,
        mode="multi_voice",
        idempotency_key="voice-mode-123",
    )

    assert ACTION_CATALOG.get("pandrator_get_voice_setup").risk == RiskClass.READ
    configure = ACTION_CATALOG.get("pandrator_configure_voice_setup")
    assert configure.risk == RiskClass.WRITE
    assert configure.requires_idempotency
    assert configure.downstream_operation_id == "configureVoiceSetup"
    assert ACTION_CATALOG.get("pandrator_get_audiobook_setup").enabled
    assert ACTION_CATALOG.get("pandrator_configure_audiobook").enabled
