from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.schemas.audiobook import (
    ConfigureAudiobookInput,
    GetAudiobookSetupInput,
    PreviewSpeechSegmentInput,
)
from pandrator_mcp.tools.audiobook import (
    configure_audiobook,
    get_audiobook_setup,
    preview_speech_segment,
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
