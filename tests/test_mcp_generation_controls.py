"""Portable MCP contracts for revisioned characters and voice casting."""

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.generation_controls import (
    CastSettings,
    CharacterEntry,
    UpdateGenerationControlsInput,
    VoiceBinding,
)
from pandrator_mcp.tools.generation_controls import (
    generation_controls_action,
    register_generation_controls_tools,
)


def _response() -> dict:
    return {
        "id": "controls-1",
        "session_id": "session-1",
        "revision": 3,
        "characters": [
            {"id": "c1", "display_name": "Alice"},
        ],
        "cast": {
            "narrator": None,
            "categories": {},
            "characters": {},
            "source_speakers": {},
        },
    }


def test_generation_controls_models_are_strict_and_preserve_cast_clear_semantics():
    character = CharacterEntry(display_name=" Alice ", aliases=[" A "])
    assert character.display_name == "Alice"
    assert character.aliases == ["A"]

    with pytest.raises(ValidationError):
        CharacterEntry(display_name="Alice", unexpected=True)
    with pytest.raises(ValidationError):
        VoiceBinding()
    with pytest.raises(ValidationError):
        CastSettings(categories={"invalid": VoiceBinding(voice="narrator")})
    with pytest.raises(ValidationError):
        UpdateGenerationControlsInput(
            session_id="session-1",
            expected_revision=0,
            idempotency_key="short",
            unlock_ids=[""],
        )


def test_client_routes_only_generation_controls_and_preserves_nested_nulls():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value=_response())
    client.generation_controls_request(
        "update",
        {
            "session_id": "session/a",
            "expected_revision": 3,
            "characters": None,
            "cast": {"narrator": None, "categories": {}},
            "unlock_ids": [],
            "idempotency_key": "generation-controls-1",
        },
    )
    args, kwargs = client._request_json.call_args
    assert args[0].endswith("session%2Fa/generation-controls")
    assert kwargs["method"] == "PUT"
    assert kwargs["body"] == {
        "expected_revision": 3,
        "cast": {"narrator": None, "categories": {}},
        "unlock_ids": [],
    }
    assert kwargs["idempotency_key"] == "generation-controls-1"
    assert kwargs["maximum_body_bytes"] == 512 * 1024

    client.generation_controls_request("get", {"session_id": "session/a"})
    args, kwargs = client._request_json.call_args
    assert args[0].endswith("session%2Fa/generation-controls")
    assert kwargs == {"method": "GET"}

    with pytest.raises(ValueError):
        client.generation_controls_request("delete", {"session_id": "session"})
    with pytest.raises(ValueError):
        client.generation_controls_request("get", {"session_id": "session", "extra": 1})


def test_tool_forwards_portable_data_and_inspection_next_action():
    application = Mock()
    application.generation_controls_request.return_value = _response()
    runtime = SimpleNamespace(require_application=lambda: application)
    arguments = UpdateGenerationControlsInput(
        session_id="session-1",
        expected_revision=3,
        cast=CastSettings(narrator=None, categories={"male": VoiceBinding(voice="v")}),
        idempotency_key="generation-controls-2",
    )
    outcome = generation_controls_action(runtime, "update", arguments)
    forwarded = application.generation_controls_request.call_args.args[1]
    assert forwarded["cast"]["narrator"] is None
    assert outcome.next_actions[0].tool == "pandrator_get_generation_controls"
    assert outcome.next_actions[0].arguments == {"session_id": "session-1"}


def test_registered_signatures_are_flat_and_match_input_models():
    recorded = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def store(function):
                recorded[name] = (function, title, annotations)
                return function

            return store

    register_generation_controls_tools(
        Server(), None, lambda *args: args, read_only="read", write_action="write"
    )
    assert set(recorded) == {
        "pandrator_get_generation_controls",
        "pandrator_update_generation_controls",
    }
    assert set(inspect.signature(recorded["pandrator_get_generation_controls"][0]).parameters) == {
        "session_id"
    }
    assert "arguments" not in inspect.signature(
        recorded["pandrator_update_generation_controls"][0]
    ).parameters
    assert ACTION_CATALOG.get("pandrator_get_generation_controls").risk == RiskClass.READ
    assert ACTION_CATALOG.get("pandrator_update_generation_controls").requires_idempotency


def test_tools_are_exposed_over_mcp_2_2_in_memory_transport():
    try:
        from mcp import Client
    except ImportError:
        pytest.skip("MCP SDK client transport is not installed")
    from pandrator_mcp.server import build_server

    async def exercise():
        application = Mock()
        application.generation_controls_request.return_value = _response()
        runtime = SimpleNamespace(require_application=lambda: application)
        async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert tools["pandrator_get_generation_controls"].title == (
                "Inspect characters and voice cast"
            )
            assert "session_id" in tools["pandrator_get_generation_controls"].input_schema[
                "properties"
            ]
            result = await client.call_tool(
                "pandrator_update_generation_controls",
                {
                    "session_id": "session-1",
                    "expected_revision": 3,
                    "cast": {"narrator": None},
                    "idempotency_key": "generation-controls-3",
                },
            )
            assert not result.is_error
            assert application.generation_controls_request.call_args.args[0] == "update"
            assert (
                application.generation_controls_request.call_args.args[1]["cast"]["narrator"]
                is None
            )

    asyncio.run(exercise())
