"""Portable MCP contracts for managed voice metadata updates."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.voice_metadata import (
    UpdateVoiceMetadataInput,
    VoiceMetadataChanges,
)
from pandrator_mcp.tools.voice_metadata import (
    register_voice_metadata_tools,
    update_voice_metadata,
)


def _response() -> dict:
    return {
        "id": "voice-1",
        "revision": 4,
        "name": "Narrator",
        "language": None,
        "description": None,
        "voice_category": "unspecified",
    }


def test_voice_metadata_changes_require_an_explicit_field_and_preserve_nulls():
    with pytest.raises(ValidationError):
        VoiceMetadataChanges()
    with pytest.raises(ValidationError):
        VoiceMetadataChanges(name=None)
    with pytest.raises(ValidationError):
        VoiceMetadataChanges(extra="rejected")

    changes = VoiceMetadataChanges(
        language=None,
        description=None,
        voice_category=None,
    )
    assert changes.model_fields_set == {
        "language",
        "description",
        "voice_category",
    }
    assert changes.model_dump(mode="json", exclude_unset=True) == {
        "language": None,
        "description": None,
        "voice_category": None,
    }

    with pytest.raises(ValidationError):
        UpdateVoiceMetadataInput(
            voice_id="voice-1",
            expected_revision=0,
            changes=VoiceMetadataChanges(name="Narrator"),
        )


def test_client_routes_only_voice_metadata_and_preserves_explicit_nulls():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value=_response())
    client.voice_metadata_request(
        "update",
        {
            "voice_id": "voice/a",
            "expected_revision": 3,
            "changes": {
                "language": None,
                "description": None,
                "voice_category": None,
            },
        },
    )
    args, kwargs = client._request_json.call_args
    assert args[0].endswith("voice%2Fa")
    assert kwargs["method"] == "PATCH"
    assert kwargs["body"] == {
        "language": None,
        "description": None,
        "voice_category": None,
    }
    assert kwargs["if_match_revision"] == 3
    assert kwargs["maximum_body_bytes"] == 32 * 1024
    assert "idempotency_key" not in kwargs

    with pytest.raises(ValueError):
        client.voice_metadata_request("get", {})
    with pytest.raises(ValueError):
        client.voice_metadata_request(
            "update",
            {
                "voice_id": "voice-1",
                "expected_revision": 3,
                "changes": {"unknown": "field"},
            },
        )


def test_client_rejects_voice_metadata_body_over_32_kib():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value=_response())
    with pytest.raises(ValueError, match="32 KiB"):
        client.voice_metadata_request(
            "update",
            {
                "voice_id": "voice-1",
                "expected_revision": 3,
                "changes": {"description": "x" * (32 * 1024)},
            },
        )
    client._request_json.assert_not_called()


def test_tool_forwards_changes_and_points_to_voice_catalog():
    application = Mock()
    application.voice_metadata_request.return_value = _response()
    runtime = SimpleNamespace(require_application=lambda: application)
    arguments = UpdateVoiceMetadataInput(
        voice_id="voice-1",
        expected_revision=3,
        changes=VoiceMetadataChanges(
            language=None,
            description="A narrator",
            voice_category=None,
        ),
    )
    outcome = update_voice_metadata(runtime, arguments)
    forwarded = application.voice_metadata_request.call_args.args[1]
    assert application.voice_metadata_request.call_args.args[0] == "update"
    assert forwarded == {
        "voice_id": "voice-1",
        "expected_revision": 3,
        "changes": {
            "language": None,
            "description": "A narrator",
            "voice_category": None,
        },
    }
    assert outcome.next_actions[0].tool == "pandrator_get_voice_catalog"
    assert outcome.next_actions[0].arguments == {}


def test_registered_signature_is_flat_and_catalogued():
    recorded = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def store(function):
                recorded[name] = (function, title, annotations)
                return function

            return store

    register_voice_metadata_tools(
        Server(), None, lambda *args: args, write_action="write"
    )
    function, title, _annotations = recorded["pandrator_update_voice_metadata"]
    assert title == "Update managed voice details"
    signature = inspect.signature(function)
    assert list(signature.parameters) == [
        "voice_id",
        "expected_revision",
        "changes",
    ]
    assert "arguments" not in signature.parameters
    assert ACTION_CATALOG.get("pandrator_update_voice_metadata").risk == RiskClass.WRITE
    assert not ACTION_CATALOG.get("pandrator_update_voice_metadata").requires_idempotency


def test_tool_is_exposed_over_mcp_2_2_in_memory_transport():
    try:
        from mcp import Client
    except ImportError:
        pytest.skip("MCP SDK client transport is not installed")
    from pandrator_mcp.server import build_server

    async def exercise():
        application = Mock()
        application.voice_metadata_request.return_value = _response()
        runtime = SimpleNamespace(require_application=lambda: application)
        async with Client(
            build_server(runtime), mode="auto", raise_exceptions=True
        ) as client:
            tools = {
                tool.name: tool for tool in (await client.list_tools()).tools
            }
            voice_tool = tools["pandrator_update_voice_metadata"]
            assert voice_tool.title == "Update managed voice details"
            assert voice_tool.annotations.idempotent_hint is False
            assert set(voice_tool.input_schema["required"]) == {
                "voice_id",
                "expected_revision",
                "changes",
            }
            result = await client.call_tool(
                "pandrator_update_voice_metadata",
                {
                    "voice_id": "voice-1",
                    "expected_revision": 3,
                    "changes": {
                        "language": None,
                        "description": None,
                        "voice_category": None,
                    },
                },
            )
            assert not result.is_error
            assert application.voice_metadata_request.call_args.args[0] == "update"
            assert (
                application.voice_metadata_request.call_args.args[1]["changes"][
                    "voice_category"
                ]
                is None
            )

    asyncio.run(exercise())
