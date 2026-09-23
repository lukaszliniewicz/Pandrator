from __future__ import annotations

import asyncio
import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas.audiobook import (
    ApplySpeechSelectionInput,
    ConfigureAudiobookInput,
    GetAudiobookSetupInput,
    PreviewSpeechSegmentInput,
    PreviewSpeechSelectionInput,
)
from pandrator_mcp.tools.audiobook import (
    apply_speech_selection,
    configure_audiobook,
    get_audiobook_setup,
    preview_speech_segment,
    preview_speech_selection,
    register_audiobook_tools,
)

REVISION = "b" * 64


def test_inputs_are_flat_strict_and_bounded():
    assert GetAudiobookSetupInput(session_id=" session-1 ").session_id == "session-1"
    configure = ConfigureAudiobookInput(
        session_id="session-1",
        expected_revision=REVISION,
        mode="multi_voice",
        idempotency_key="audiobook-mcp-1",
    )
    assert configure.mode == "multi_voice"
    preview = PreviewSpeechSegmentInput(
        session_id="session-1",
        revision_id="revision",
        segment_id="segment",
        generation_run_id="run",
        include_request=True,
    )
    assert preview.generation_run_id == "run"
    with pytest.raises(ValidationError):
        GetAudiobookSetupInput(session_id="session-1", extra=True)
    with pytest.raises(ValidationError):
        ConfigureAudiobookInput(
            session_id="session-1",
            expected_revision="bad",
            mode="single_voice",
            idempotency_key="audiobook-mcp-2",
        )
    with pytest.raises(ValidationError):
        PreviewSpeechSegmentInput(
            session_id="session-1", revision_id=" ", segment_id="segment"
        )


def test_handlers_call_dedicated_client_contracts_without_next_actions():
    application = Mock()
    application.get_audiobook_setup.return_value = {"mode": "single_voice"}
    application.configure_audiobook.return_value = {"mode": "multi_voice"}
    application.preview_speech_segment.return_value = {"compilation_only": True}
    runtime = SimpleNamespace(require_application=lambda: application)

    setup = get_audiobook_setup(runtime, GetAudiobookSetupInput(session_id="s"))
    configured = configure_audiobook(
        runtime,
        ConfigureAudiobookInput(
            session_id="s",
            expected_revision=REVISION,
            mode="multi_voice",
            idempotency_key="audiobook-mcp-3",
        ),
    )
    preview = preview_speech_segment(
        runtime,
        PreviewSpeechSegmentInput(
            session_id="s", revision_id="r", segment_id="segment"
        ),
    )
    assert setup.result == {"mode": "single_voice"}
    assert configured.next_actions == []
    assert preview.result == {"compilation_only": True}
    application.configure_audiobook.assert_called_once_with(
        "s",
        expected_revision=REVISION,
        mode="multi_voice",
        idempotency_key="audiobook-mcp-3",
    )
    application.preview_speech_segment.assert_called_once_with(
        "s",
        revision_id="r",
        segment_id="segment",
        generation_run_id=None,
        include_request=False,
    )


def test_registered_tools_have_flat_model_signatures():
    recorded = {}

    class Server:
        def tool(self, *, name, title, annotations):
            def register(function):
                recorded[name] = (function, title, annotations)
                return function

            return register

    register_audiobook_tools(
        Server(),
        None,
        lambda *args: args,
        read_only="read",
        write_action="write",
    )
    assert set(recorded) == {
        "pandrator_get_audiobook_setup",
        "pandrator_configure_audiobook",
        "pandrator_preview_speech_segment",
        "pandrator_preview_speech_selection",
        "pandrator_apply_speech_selection",
    }
    assert set(inspect.signature(recorded["pandrator_get_audiobook_setup"][0]).parameters) == {
        "session_id"
    }
    assert set(inspect.signature(recorded["pandrator_configure_audiobook"][0]).parameters) == {
        "session_id",
        "expected_revision",
        "mode",
        "idempotency_key",
    }
    assert set(inspect.signature(recorded["pandrator_preview_speech_segment"][0]).parameters) == {
        "session_id",
        "revision_id",
        "segment_id",
        "generation_run_id",
        "include_request",
    }


def _selection(**changes):
    return {
        "session_id": "session/a",
        "revision_id": "revision-1",
        "segment_id": "segment-1",
        "expected_segment_revision": 2,
        "start": 0,
        "end": 12,
        **changes,
    }


def test_selection_models_and_client_preserve_omission_and_null():
    whole = PreviewSpeechSelectionInput(**_selection())
    assert whole.model_dump(exclude_unset=True, exclude={"session_id"}) == {
        "revision_id": "revision-1",
        "segment_id": "segment-1",
        "expected_segment_revision": 2,
        "start": 0,
        "end": 12,
    }
    part = PreviewSpeechSelectionInput(
        **_selection(start=3, end=8, voice=None, delivery={"instruction": None, "pace": "brisk"})
    )
    assert part.model_dump(exclude_unset=True)["voice"] is None
    assert part.model_dump(exclude_unset=True)["delivery"] == {
        "instruction": None,
        "pace": "brisk",
    }
    with pytest.raises(ValidationError):
        PreviewSpeechSelectionInput(**_selection(start=4, end=4))
    with pytest.raises(ValidationError):
        PreviewSpeechSelectionInput(**_selection(start=-1))
    with pytest.raises(ValidationError):
        ApplySpeechSelectionInput(**_selection(expected_preview_revision="bad", idempotency_key="selection-1"))
    with pytest.raises(ValidationError):
        PreviewSpeechSelectionInput(**_selection(speaker="character"))

    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"preview_revision": REVISION})
    client.preview_speech_selection("session/a", body=part.model_dump(exclude_unset=True, exclude={"session_id"}))
    args, kwargs = client._request_json.call_args
    assert args[0].endswith("session%2Fa/speech-plan/selection-preview")
    assert kwargs["body"]["delivery"] == {"instruction": None, "pace": "brisk"}
    assert kwargs.get("idempotency_key") is None
    client.apply_speech_selection("session/a", body={"expected_preview_revision": REVISION}, idempotency_key="selection-1")
    args, kwargs = client._request_json.call_args
    assert args[0].endswith("session%2Fa/speech-plan/selection")
    assert kwargs["idempotency_key"] == "selection-1"
    assert ACTION_CATALOG.get("pandrator_preview_speech_selection").risk == RiskClass.READ
    assert ACTION_CATALOG.get("pandrator_apply_speech_selection").requires_idempotency


def test_selection_handlers_forward_backend_preview_token_and_partial_patch():
    application = Mock()
    application.preview_speech_selection.return_value = {"preview_revision": REVISION}
    application.apply_speech_selection.return_value = {"performance_plan": {"id": "plan-1"}}
    runtime = SimpleNamespace(require_application=lambda: application)
    preview = preview_speech_selection(runtime, PreviewSpeechSelectionInput(**_selection()))
    assert preview.result["preview_revision"] == REVISION
    application.preview_speech_selection.assert_called_once_with(
        "session/a", body=PreviewSpeechSelectionInput(**_selection()).model_dump(exclude_unset=True, exclude={"session_id"})
    )
    applied = apply_speech_selection(runtime, ApplySpeechSelectionInput(
        **_selection(start=3, end=8, voice=None, delivery={"emotion": "curious"}),
        expected_preview_revision=preview.result["preview_revision"],
        idempotency_key="selection-apply-1",
    ))
    assert applied.result["performance_plan"]["id"] == "plan-1"
    body = application.apply_speech_selection.call_args.kwargs["body"]
    assert body["voice"] is None
    assert body["delivery"] == {"emotion": "curious"}
    assert body["expected_preview_revision"] == REVISION
    assert "idempotency_key" not in body


def test_selection_registered_mcp_schema_and_transport_preserve_optional_fields():
    from mcp import Client

    from pandrator_mcp.server import build_server

    async def exercise():
        application = Mock()
        application.preview_speech_selection.return_value = {"preview_revision": REVISION}
        application.apply_speech_selection.return_value = {"performance_plan": {"id": "plan-1"}}
        runtime = SimpleNamespace(require_application=lambda: application)
        async with Client(build_server(runtime), mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            preview_schema = tools["pandrator_preview_speech_selection"].input_schema
            apply_schema = tools["pandrator_apply_speech_selection"].input_schema
            assert "delivery" in preview_schema["properties"]
            assert "voice" in preview_schema["properties"]
            assert "default" not in preview_schema["properties"]["voice"]
            assert "default" not in preview_schema["properties"]["delivery"]
            assert "object object" not in str(preview_schema)
            assert "expected_preview_revision" in apply_schema["required"]
            assert "idempotency_key" in apply_schema["required"]
            result = await client.call_tool("pandrator_preview_speech_selection", _selection())
            assert not result.is_error
            body = application.preview_speech_selection.call_args.kwargs["body"]
            assert body == PreviewSpeechSelectionInput(**_selection()).model_dump(
                exclude_unset=True, exclude={"session_id"}
            )
            result = await client.call_tool("pandrator_preview_speech_selection", _selection(start=3, end=8, voice=None, delivery=None))
            assert not result.is_error
            body = application.preview_speech_selection.call_args.kwargs["body"]
            assert body["voice"] is None
            assert body["delivery"] is None
            result = await client.call_tool("pandrator_preview_speech_selection", _selection(start=3, end=8, voice=None, delivery={"pace": "brisk"}))
            assert not result.is_error
            body = application.preview_speech_selection.call_args.kwargs["body"]
            assert body["voice"] is None
            assert body["delivery"] == {"pace": "brisk"}
            forwarded = application.preview_speech_selection.call_count
            invalid = await client.call_tool(
                "pandrator_preview_speech_selection", _selection(start=8, end=8)
            )
            assert invalid.is_error
            assert application.preview_speech_selection.call_count == forwarded
            result = await client.call_tool("pandrator_apply_speech_selection", {
                **_selection(),
                "expected_preview_revision": REVISION,
                "idempotency_key": "selection-apply-2",
            })
            assert not result.is_error
            assert application.apply_speech_selection.call_args.kwargs["idempotency_key"] == "selection-apply-2"

    asyncio.run(exercise())
