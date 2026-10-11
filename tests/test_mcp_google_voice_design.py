from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, Callable, cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas import TOOL_INPUT_MODELS
from pandrator_mcp.schemas.voice_lifecycle import DesignGoogleVoiceInput
from pandrator_mcp.tools import design_google_voice as exported_design_google_voice
from pandrator_mcp.tools.voice_lifecycle import (
    design_google_voice,
    register_voice_lifecycle_tools,
)


def _arguments(**overrides: Any) -> dict[str, Any]:
    return {
        "service_id": "gemini",
        "model": "gemini-3.8-flash-tts",
        "name": "  Rowan  ",
        "prompt": "  Warm, clear narrator.  ",
        "idempotency_key": "google:design:1",
        **overrides,
    }


def test_google_voice_design_schema_is_strict_bounded_and_normalizes_text():
    parsed = DesignGoogleVoiceInput(**_arguments())

    assert parsed.name == "Rowan"
    assert parsed.prompt == "Warm, clear narrator."
    assert parsed.language == "en"
    assert parsed.voice_category == "unspecified"
    assert set(DesignGoogleVoiceInput.model_fields) == {
        "service_id",
        "model",
        "name",
        "prompt",
        "language",
        "voice_category",
        "idempotency_key",
    }

    invalid = (
        {"service_id": "google"},
        {"model": "gemini-2.5-flash-tts"},
        {"name": "   "},
        {"prompt": "   "},
        {"language": "english"},
        {"language": "en--US"},
        {"voice_category": "robot"},
        {"idempotency_key": "bad key!"},
        {"sample_text": "unrequested field"},
    )
    for change in invalid:
        with pytest.raises(ValidationError):
            DesignGoogleVoiceInput(**_arguments(**change))

    with pytest.raises(ValidationError):
        DesignGoogleVoiceInput(**_arguments(name="x" * 256))
    with pytest.raises(ValidationError):
        DesignGoogleVoiceInput(**_arguments(prompt="x" * 4_001))
    with pytest.raises(ValidationError):
        DesignGoogleVoiceInput(**_arguments(language="a" * 41))


def test_application_client_posts_exact_body_and_idempotency_header():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"id": "job-1", "state": "queued"})

    result = client.design_google_voice(
        service_id="vertex_ai",
        model="gemini-3.8-flash-lite-tts",
        name="Rowan",
        prompt="Warm narrator",
        language="en-GB",
        voice_category="androgynous",
        idempotency_key="google:design:2",
    )

    assert result == {"id": "job-1", "state": "queued"}
    client._request_json.assert_called_once_with(
        "/api/v1/voices/design/google",
        method="POST",
        body={
            "service_id": "vertex_ai",
            "model": "gemini-3.8-flash-lite-tts",
            "name": "Rowan",
            "prompt": "Warm narrator",
            "language": "en-GB",
            "voice_category": "androgynous",
        },
        idempotency_key="google:design:2",
        maximum_body_bytes=16 * 1024,
    )


def test_design_handler_returns_durable_work_and_managed_catalog_action():
    application = Mock()
    application.design_google_voice.return_value = {
        "id": "job-7",
        "state": "queued",
        "result": {"voice_id": "voice-google-7"},
        "provider_secret": "must-not-escape",
        "reference_audio": "must-not-escape-either",
        "sample_text": "must-not-be-promoted",
    }
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))

    outcome = design_google_voice(runtime, DesignGoogleVoiceInput(**_arguments()))

    application.design_google_voice.assert_called_once_with(
        service_id="gemini",
        model="gemini-3.8-flash-tts",
        name="Rowan",
        prompt="Warm, clear narrator.",
        language="en",
        voice_category="unspecified",
        idempotency_key="google:design:1",
    )
    assert outcome.work is not None
    assert (outcome.work.type, outcome.work.id, outcome.work.state) == (
        "job",
        "job-7",
        "queued",
    )
    assert outcome.result["voice_id"] == "voice-google-7"
    assert [action.tool for action in outcome.next_actions] == [
        "pandrator_get_work",
        "pandrator_get_voice_catalog",
    ]
    catalog_action = outcome.next_actions[1]
    assert catalog_action.arguments == {
        "query": "voice-google-7",
        "kind": "managed",
    }
    assert catalog_action.reason == "Inspect the saved Google voice after creation completes."
    assert "must-not-escape" not in str(outcome.result)
    assert "reference_audio" not in outcome.result
    assert "sample_text" not in outcome.result


def test_google_voice_design_is_exported_and_registered_with_exact_input_fields():
    assert exported_design_google_voice is design_google_voice
    assert DesignGoogleVoiceInput in TOOL_INPUT_MODELS

    registrations: dict[str, tuple[Callable[..., Any], dict[str, Any]]] = {}

    class Server:
        def tool(self, **options: Any) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
            def register(function: Callable[..., Any]) -> Callable[..., Any]:
                registrations[str(options["name"])] = (function, options)
                return function

            return register

    register_voice_lifecycle_tools(
        Server(),
        cast(McpRuntime, SimpleNamespace()),
        lambda *_args: {},
        read_only="read-only",
        write_action="write-action",
    )

    function, options = registrations["pandrator_design_google_voice"]
    assert options["title"] == "Create and save a Gemini or Vertex designed voice"
    assert options["annotations"] == "write-action"
    assert set(inspect.signature(function).parameters) == set(DesignGoogleVoiceInput.model_fields)
