"""Portable MCP speech-editor and comparison-export contracts."""

from __future__ import annotations

import asyncio
import inspect
import json
import subprocess
import sys
from types import SimpleNamespace
from typing import Any, Literal

import pytest
from mcp import Client
from mcp.types import TextContent
from pydantic import ValidationError

from pandrator_mcp.clients.application_editor import ApplicationEditorMethods
from pandrator_mcp.schemas.comparison_exports import ComparisonExportInput
from pandrator_mcp.schemas.speech_editor import (
    ApplySpeechSelectionBatchInput,
    PreviewSpeechSelectionBatchInput,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.comparison_exports import register_comparison_export_tools
from pandrator_mcp.tools.speech_editor import register_speech_editor_tools
from tests.test_mcp_media_edit_registration import fixture_runtime


def _change(segment_id: str = "segment-1", **extra: Any) -> dict[str, Any]:
    return {
        "revision_id": "revision-1",
        "segment_id": segment_id,
        "expected_segment_revision": 4,
        "start": 2,
        "end": 7,
        **extra,
    }


@pytest.mark.parametrize("mode", ["standard", "structured"])
@pytest.mark.parametrize("tool,method,arguments,payload", [
    ("pandrator_preview_session_deletion", "admit_session_purge_inspection",
     {"session_id": "session-1", "idempotency_key": "native-purge-inspection"},
     {"job_id": "job-1", "status": "queued"}),
    ("pandrator_preview_speech_selection_batch", "preview_speech_selection_batch",
     {"session_id": "session-1", "changes": [_change(speaker="narrator")]},
     {"preview_revision": "a" * 64, "affected_segment_ids": ["segment-1"]}),
    ("pandrator_apply_speech_selection_batch", "apply_speech_selection_batch",
     {"session_id": "session-1", "changes": [_change(speaker="narrator")],
      "expected_preview_revision": "a" * 64, "idempotency_key": "native-batch"},
     {"affected_segment_ids": ["segment-1"], "performance_plan": {"id": "plan-1"}}),
    ("pandrator_create_comparison_export", "create_comparison_export",
     {"session_id": "session-1", "generation_run_ids": ["run-1", "run-2"],
      "segment_ids": ["segment-1"], "idempotency_key": "native-comparison"},
     {"job_id": "job-1", "status": "queued"}),
])
def test_native_sdk_new_tools_use_one_structured_body(tmp_path, mode, tool, method,
                                                     arguments, payload):
    runtime, _, application = fixture_runtime(tmp_path)
    getattr(application, method).return_value = payload

    async def invoke():
        async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
            return await client.call_tool(tool, {**arguments, "response_mode": mode})

    result = asyncio.run(invoke())
    assert not result.is_error, result
    structured = result.structured_content
    assert structured["result"] == {**payload,
                                    **({"idempotency_key": arguments["idempotency_key"]}
                                       if tool == "pandrator_preview_session_deletion" else {})}
    block = next(item for item in result.content if isinstance(item, TextContent))
    text = json.loads(block.text)
    if mode == "structured":
        assert text == {"schema_version": "1", "request_id": structured["request_id"],
                        "data": "structuredContent",
                        **({"status": payload["status"]} if payload.get("status") else {})}
        assert len(block.text.encode("utf-8")) < 180
    else:
        assert text == structured
    assert len(json.dumps(result.model_dump(mode="json", by_alias=True)).encode("utf-8")) < 4096
    getattr(application, method).assert_called_once()


def test_speech_selection_batch_preserves_nested_unset_and_explicit_nulls() -> None:
    request = PreviewSpeechSelectionBatchInput.model_validate(
        {
            "session_id": "session-1",
            "changes": [
                _change(
                    voice=None,
                    delivery={"emotion": None, "pace": "brisk"},
                )
            ],
        }
    )

    change = request.changes[0].model_dump(mode="json", exclude_unset=True)
    assert change == {
        "revision_id": "revision-1",
        "segment_id": "segment-1",
        "expected_segment_revision": 4,
        "start": 2,
        "end": 7,
        "voice": None,
        "delivery": {"emotion": None, "pace": "brisk"},
    }
    assert request.detailed_preview is False


@pytest.mark.parametrize(
    "changes",
    [
        [_change(), {**_change("segment-2"), "revision_id": "revision-2"}],
        [_change(start=8, end=7)],
        [_change(start=5, end=5)],
        [_change(speaker="character")],
        [_change(speaker="narrator", character_id="character-1")],
        [_change(voice_category="female")],
        [_change(speech_xml="<speak>x</speak>", voice=None)],
        [_change(start=5, end=5, vocal_event={"kind": "laugh", "duration_ms": 100})],
    ],
)
def test_speech_selection_batch_rejects_backend_invalid_changes(changes) -> None:
    with pytest.raises(ValidationError):
        PreviewSpeechSelectionBatchInput.model_validate(
            {"session_id": "session-1", "changes": changes}
        )


def test_speech_selection_batch_rejects_mixed_revisions_and_bad_apply_fences() -> None:
    with pytest.raises(ValidationError, match="one plan revision"):
        PreviewSpeechSelectionBatchInput.model_validate(
            {
                "session_id": "session-1",
                "changes": [_change(), {**_change("segment-2"), "revision_id": "r2"}],
            }
        )
    with pytest.raises(ValidationError):
        ApplySpeechSelectionBatchInput.model_validate(
            {
                "session_id": "session-1",
                "changes": [_change()],
                "expected_preview_revision": "not-a-revision",
                "idempotency_key": "short",
            }
        )


def test_comparison_export_input_validates_canonical_and_convenience_forms() -> None:
    convenience = ComparisonExportInput.model_validate(
        {
            "session_id": "session-1",
            "generation_run_ids": ["run-a", "run-b"],
            "segment_ids": ["segment-1", "segment-2"],
            "idempotency_key": "comparison-key-1",
        }
    )
    assert convenience.gap_ms == 350
    assert convenience.output_format == "wav"

    canonical = ComparisonExportInput.model_validate(
        {
            "session_id": "session-1",
            "clips": [
                {
                    "generation_run_id": "run-a",
                    "segment_id": "segment-1",
                    "audio_version_side": "before",
                    "take_id": "take-1",
                }
            ],
            "gap_ms": 0,
            "output_format": "mp3",
            "idempotency_key": "comparison-key-2",
        }
    )
    assert canonical.clips[0].audio_version_side == "before"
    assert canonical.gap_ms == 0
    assert canonical.output_format == "mp3"


@pytest.mark.parametrize(
    "payload",
    [
        {"generation_run_ids": ["run-a"], "segment_ids": ["segment-1"]},
        {"generation_run_ids": ["run-a", "run-a"], "segment_ids": ["segment-1"]},
        {"generation_run_ids": ["run-a", "run-b"], "segment_ids": ["segment-1", "segment-1"]},
        {"clips": [], "generation_run_ids": ["run-a", "run-b"], "segment_ids": ["segment-1"]},
        {"clips": [{"generation_run_id": "run-a", "segment_id": "segment-1"}], "segment_ids": ["segment-1"]},
        {"generation_run_ids": ["run-a", "run-b"], "segment_ids": ["segment-1"], "gap_ms": 10_001},
    ],
)
def test_comparison_export_rejects_invalid_convenience_and_mixed_forms(payload) -> None:
    with pytest.raises(ValidationError):
        ComparisonExportInput.model_validate(
            {
                "session_id": "session-1",
                "idempotency_key": "comparison-key-1",
                **payload,
            }
        )


class _RequestRecorder(ApplicationEditorMethods):
    timeout_seconds = 5

    def __init__(self) -> None:
        self.requests: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.requests.append((path, kwargs))
        return {"path": path}

    def _request_binary_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Binary transport is not used by editor requests.")


def test_application_editor_client_uses_exact_encoded_paths_and_read_semantics() -> None:
    client = _RequestRecorder()
    body = {"changes": [_change()], "detailed_preview": False}

    client.preview_speech_selection_batch("session/a", body=body)
    client.apply_speech_selection_batch(
        "session/a", body={**body, "expected_preview_revision": "a" * 64},
        idempotency_key="selection-key-1",
    )
    client.create_comparison_export(
        "session/a",
        body={"clips": [], "gap_ms": 350, "output_format": "wav"},
        idempotency_key="comparison-key-1",
    )

    assert [item[0] for item in client.requests] == [
        "/api/v1/sessions/session%2Fa/speech-plan/selection-batch-preview",
        "/api/v1/sessions/session%2Fa/speech-plan/selection-batch",
        "/api/v1/sessions/session%2Fa/comparison-exports",
    ]
    preview = client.requests[0][1]
    assert preview["method"] == "POST"
    assert preview["read_only_operation"] is True
    assert preview["body"] == body
    assert "idempotency_key" not in preview
    assert client.requests[1][1]["idempotency_key"] == "selection-key-1"
    assert client.requests[2][1]["idempotency_key"] == "comparison-key-1"


class _ServerRecorder:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}
        self.annotations: dict[str, Any] = {}

    def tool(self, *, name: str, title: str, annotations: Any):
        self.annotations[name] = (title, annotations)

        def register(function):
            self.tools[name] = function
            return function

        return register


class _ApplicationRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def preview_speech_selection_batch(self, session_id: str, *, body: dict[str, Any]):
        self.calls.append(("preview", {"session_id": session_id, "body": body}))
        segment_ids = list(dict.fromkeys(item["segment_id"] for item in body["changes"]))
        return {
            "preview_revision": "a" * 64,
            "affected_segment_ids": segment_ids,
            "compilation_only": True,
        }

    def apply_speech_selection_batch(
        self, session_id: str, *, body: dict[str, Any], idempotency_key: str
    ):
        self.calls.append(
            (
                "apply",
                {"session_id": session_id, "body": body, "idempotency_key": idempotency_key},
            )
        )
        return {"affected_segment_ids": ["segment-2", "segment-1"], "status": "applied"}

    def create_comparison_export(
        self, session_id: str, *, body: dict[str, Any], idempotency_key: str
    ):
        self.calls.append(
            (
                "comparison",
                {"session_id": session_id, "body": body, "idempotency_key": idempotency_key},
            )
        )
        return {
            "job_id": "job-1",
            "kind": "export.comparison",
            "status": "queued",
            "session_id": session_id,
        }


def _tool_harness():
    application = _ApplicationRecorder()
    runtime = SimpleNamespace(require_application=lambda: application)
    server = _ServerRecorder()
    response_calls: list[str] = []

    def validated_call(handler, current_runtime, model, values):
        arguments = model.model_validate(values)
        outcome = handler(current_runtime, arguments)
        return {
            "result": outcome.result,
            "work": outcome.work.model_dump(mode="json") if outcome.work else None,
            "next_actions": [
                action.model_dump(mode="json") for action in outcome.next_actions
            ],
        }

    def response(envelope, response_mode):
        response_calls.append(response_mode)
        return {"response_mode": response_mode, "envelope": envelope}

    register_speech_editor_tools(
        server, runtime, validated_call, response,
        read_only="read-only", write_action="write",
    )
    register_comparison_export_tools(
        server, runtime, validated_call, response, execute_action="execute"
    )
    return server, application, response_calls


def test_registered_speech_tools_preserve_body_and_offer_targeted_actions() -> None:
    server, application, response_calls = _tool_harness()
    preview_tool = server.tools["pandrator_preview_speech_selection_batch"]
    apply_tool = server.tools["pandrator_apply_speech_selection_batch"]
    changes = [
        _change("segment-1", voice=None, delivery={"emotion": None, "pace": "brisk"}),
        _change("segment-2", start=8, end=10),
    ]

    preview = preview_tool(
        session_id="session-1", changes=changes, response_mode="structured"
    )
    assert len(application.calls) == 1
    assert application.calls[0] == (
        "preview",
        {
            "session_id": "session-1",
            "body": {
                "changes": [
                    {
                        "revision_id": "revision-1",
                        "segment_id": "segment-1",
                        "expected_segment_revision": 4,
                        "start": 2,
                        "end": 7,
                        "voice": None,
                        "delivery": {"emotion": None, "pace": "brisk"},
                    },
                    {
                        "revision_id": "revision-1",
                        "segment_id": "segment-2",
                        "expected_segment_revision": 4,
                        "start": 8,
                        "end": 10,
                    },
                ],
                "detailed_preview": False,
            },
        },
    )
    assert response_calls == ["structured"]
    preview_next = preview["envelope"]["next_actions"][0]
    assert preview_next["tool"] == "pandrator_apply_speech_selection_batch"
    assert [item["segment_id"] for item in preview_next["arguments"]["changes"]] == [
        "segment-1", "segment-2"
    ]

    apply_args = dict(preview_next["arguments"])
    apply = apply_tool(**apply_args, response_mode="standard")
    assert len(application.calls) == 2
    _, applied_request = application.calls[1]
    assert applied_request["session_id"] == "session-1"
    assert applied_request["body"]["expected_preview_revision"] == "a" * 64
    assert "session_id" not in applied_request["body"]
    assert "idempotency_key" not in applied_request["body"]
    assert applied_request["body"]["changes"][0]["voice"] is None
    assert applied_request["body"]["changes"][0]["delivery"] == {
        "emotion": None, "pace": "brisk"
    }
    generation = apply["envelope"]["next_actions"][0]
    assert generation["tool"] == "pandrator_regenerate_segments"
    assert generation["arguments"]["segment_ids"] == ["segment-2", "segment-1"]
    assert generation["arguments"]["settings_source"] == "current"
    assert response_calls == ["structured", "standard"]
    for tool_name in (
        "pandrator_preview_speech_selection_batch",
        "pandrator_apply_speech_selection_batch",
    ):
        signature = inspect.signature(server.tools[tool_name])
        assert signature.parameters["response_mode"].default == "standard"
        assert signature.parameters["response_mode"].annotation == Literal["standard", "structured"]


def test_registered_comparison_tool_expands_order_and_returns_work_once() -> None:
    server, application, response_calls = _tool_harness()
    tool = server.tools["pandrator_create_comparison_export"]
    result = tool(
        session_id="session-1",
        generation_run_ids=["run-a", "run-b"],
        segment_ids=["segment-1", "segment-2"],
        idempotency_key="comparison-key-1",
        response_mode="structured",
    )

    assert len(application.calls) == 1
    assert application.calls[0] == (
        "comparison",
        {
            "session_id": "session-1",
            "body": {
                "clips": [
                    {"generation_run_id": "run-a", "segment_id": "segment-1", "audio_version_side": "after"},
                    {"generation_run_id": "run-a", "segment_id": "segment-2", "audio_version_side": "after"},
                    {"generation_run_id": "run-b", "segment_id": "segment-1", "audio_version_side": "after"},
                    {"generation_run_id": "run-b", "segment_id": "segment-2", "audio_version_side": "after"},
                ],
                "gap_ms": 350,
                "output_format": "wav",
            },
            "idempotency_key": "comparison-key-1",
        },
    )
    envelope = result["envelope"]
    assert envelope["work"]["id"] == "job-1"
    assert envelope["work"]["type"] == "job"
    assert envelope["next_actions"] == [
        {
            "tool": "pandrator_get_work",
            "arguments": {"work_id": "job-1"},
            "reason": "Inspect this comparison-export job and its published audio and manifest artifacts.",
        }
    ]
    assert response_calls == ["structured"]
    signature = inspect.signature(tool)
    assert signature.parameters["response_mode"].default == "standard"
    assert signature.parameters["response_mode"].annotation == Literal["standard", "structured"]


def test_registered_comparison_tool_preserves_canonical_sides_and_options() -> None:
    server, application, _response_calls = _tool_harness()
    tool = server.tools["pandrator_create_comparison_export"]
    tool(
        session_id="session-1",
        clips=[
            {
                "generation_run_id": "run-a",
                "segment_id": "segment-1",
                "audio_version_side": "before",
                "take_id": "take-1",
            },
            {"generation_run_id": "run-b", "segment_id": "segment-2"},
        ],
        gap_ms=0,
        output_format="mp3",
        idempotency_key="comparison-key-2",
    )
    assert len(application.calls) == 1
    assert application.calls[0][1]["body"] == {
        "clips": [
            {
                "generation_run_id": "run-a",
                "segment_id": "segment-1",
                "audio_version_side": "before",
                "take_id": "take-1",
            },
            {
                "generation_run_id": "run-b",
                "segment_id": "segment-2",
                "audio_version_side": "after",
            },
        ],
        "gap_ms": 0,
        "output_format": "mp3",
    }


def test_invalid_tool_inputs_do_not_call_application() -> None:
    server, application, _response_calls = _tool_harness()
    tool = server.tools["pandrator_create_comparison_export"]
    with pytest.raises(ValidationError):
        tool(
            session_id="session-1",
            generation_run_ids=["run-a", "run-a"],
            segment_ids=["segment-1"],
            idempotency_key="comparison-key-1",
        )
    assert application.calls == []


def test_mcp_modules_import_without_backend_or_database_dependencies() -> None:
    command = (
        "import sys; "
        "from pandrator_mcp.clients.application_editor import ApplicationEditorMethods; "
        "from pandrator_mcp.schemas.speech_editor import PreviewSpeechSelectionBatchInput; "
        "from pandrator_mcp.schemas.comparison_exports import ComparisonExportInput; "
        "from pandrator_mcp.tools.speech_editor import register_speech_editor_tools; "
        "from pandrator_mcp.tools.comparison_exports import register_comparison_export_tools; "
        "assert not any(name == 'pandrator' or name.startswith('pandrator.') for name in sys.modules); "
        "assert 'sqlalchemy' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", command], check=True)
