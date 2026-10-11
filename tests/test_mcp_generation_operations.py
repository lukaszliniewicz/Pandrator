"""Typed application-client and MCP contract tests for generation operations."""

from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from pandrator_mcp.clients.application_generation_operations import (
    ApplicationGenerationOperationMethods,
)
from pandrator_mcp.results import ToolOutcome
from pandrator_mcp.schemas.generation_operations import (
    GetGenerationOperationInput,
    GetGenerationRunCoverageInput,
)
from pandrator_mcp.tools.generation_operations import (
    get_generation_operation,
    get_generation_run_coverage,
    register_generation_operation_tools,
)


class RequestProbe(ApplicationGenerationOperationMethods):
    timeout_seconds = 1.0

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def _request_json(self, path, **kwargs):
        self.calls.append((path, kwargs))
        return {"path": path}

    def _request_binary_json(self, *args, **kwargs):
        raise AssertionError("No binary request should be made.")


class FakeServer:
    def __init__(self):
        self.tools = {}

    def tool(self, *, name, title, annotations):
        def register(function):
            self.tools[name] = {
                "function": function,
                "title": title,
                "annotations": annotations,
            }
            return function

        return register


class FakeApplication:
    def __init__(self, operations=None, coverage=None):
        self.operations = list(operations or [])
        self.coverage = coverage or {}
        self.operation_calls = []
        self.coverage_calls = []

    def get_generation_operation(self, operation_id, include_coverage=True):
        self.operation_calls.append((operation_id, include_coverage))
        if not self.operations:
            raise AssertionError("No operation response remains.")
        return self.operations.pop(0)

    def get_generation_run_coverage(self, generation_run_id, audio_version_side="after"):
        self.coverage_calls.append((generation_run_id, audio_version_side))
        return self.coverage


def operation_payload(*, state="queued", coverage=None):
    return {
        "schema_version": 1,
        "id": "stable-root-id",
        "session_id": "session-id",
        "phase": "synthesis" if state not in {"succeeded", "failed", "cancelled"} else "complete",
        "state": state,
        "cancellable": state not in {"succeeded", "failed", "cancelled"},
        "poll_after_ms": 100,
        "preparation_work_id": "stable-root-id",
        "synthesis_work_id": "child-work-id",
        "generation_run_id": "run-id",
        "work": {
            "id": "child-work-id",
            "kind": "generation.run",
            "state": state,
            "progress": 0.5,
            "detail": "Current phase only",
            "error": None,
            "poll_after_ms": 100,
            "payload_json": {"must_not_escape": True},
        },
        "preparation_error": None,
        "linkage_pending": False,
        "linkage_diagnostic": None,
        "coverage": coverage,
        "settings_snapshot_json": {"must_not_escape": True},
    }


def coverage_payload(*, export_ready=False, audio_version_side="after"):
    return {
        "run_id": "run-id",
        "plan_revision_id": "revision-id",
        "audio_version_side": audio_version_side,
        "included_count": 2,
        "effective_audio_count": 1,
        "run_audio_count": 1,
        "inherited_audio_count": 0,
        "generated_audio_count": 1,
        "missing_audio_count": 1,
        "unavailable_artifact_count": 0,
        "missing_file_count": 0,
        "missing_segment_ordinals": [1],
        "export_ready": export_ready,
        "text": "do not expose",
        "path": "/private/file.wav",
    }


def make_runtime(application) -> Any:
    return SimpleNamespace(require_application=lambda: application)


def call_validated(handler, runtime, model, values):
    arguments = model.model_validate(values)
    outcome = handler(runtime, arguments)
    assert isinstance(outcome, ToolOutcome)
    return {
        "schema_version": "1",
        "request_id": "request-id",
        "result": outcome.result,
        "work": outcome.work.model_dump(mode="json") if outcome.work else None,
        "warnings": [],
        "next_actions": [item.model_dump(mode="json") for item in outcome.next_actions],
    }


def make_response(envelope, mode):
    if mode == "standard":
        return envelope
    return {
        "content": [{"type": "text", "text": "structured data is in structuredContent"}],
        "structuredContent": envelope,
    }


def test_generation_operation_client_uses_only_quoted_fixed_routes():
    client = RequestProbe()
    client.get_generation_operation("root/a ?#", include_coverage=False)
    client.get_generation_run_coverage("run/a ?#", "before")
    assert client.calls == [
        (
            "/api/v1/generation-operations/root%2Fa%20%3F%23",
            {"parameters": {"include_coverage": "false"}, "read_only_operation": True},
        ),
        (
            "/api/v1/generation-runs/run%2Fa%20%3F%23/coverage",
            {"parameters": {"audio_version_side": "before"}, "read_only_operation": True},
        ),
    ]
    with pytest.raises(ValueError, match="Audio version side"):
        cast(Any, client).get_generation_run_coverage("run-id", "latest")


def test_generation_operation_schemas_are_bounded_and_strict():
    assert GetGenerationOperationInput(operation_id="root").wait_seconds == 0
    assert GetGenerationOperationInput(operation_id="root").include_coverage is True
    with pytest.raises(ValidationError):
        GetGenerationOperationInput(operation_id="x" * 121)
    with pytest.raises(ValidationError):
        GetGenerationOperationInput(operation_id="root", wait_seconds=61)
    with pytest.raises(ValidationError):
        GetGenerationOperationInput.model_validate({"operation_id": "root", "extra": True})
    assert GetGenerationRunCoverageInput(generation_run_id="run").audio_version_side == "after"
    with pytest.raises(ValidationError):
        GetGenerationRunCoverageInput.model_validate(
            {"generation_run_id": "run", "audio_version_side": "latest"}
        )


def test_wait_zero_reads_once_and_points_to_the_same_stable_operation():
    application = FakeApplication([operation_payload(state="running")])
    runtime = make_runtime(application)
    sleeps = Mock()

    outcome = get_generation_operation(
        runtime,
        GetGenerationOperationInput(operation_id="stable-root-id", wait_seconds=0),
    )

    assert application.operation_calls == [("stable-root-id", True)]
    assert outcome.result["id"] == "stable-root-id"
    assert outcome.result["poll_count"] == 1
    assert outcome.result["timed_out"] is False
    assert outcome.work is not None
    assert outcome.work.id == "child-work-id"
    assert outcome.result["work"]["id"] == "child-work-id"
    assert "payload_json" not in repr(outcome.result)
    assert outcome.next_actions[0].tool == "pandrator_get_generation_operation"
    assert outcome.next_actions[0].arguments["operation_id"] == "stable-root-id"
    sleeps.assert_not_called()


def test_wait_polls_to_terminal_and_does_not_sleep_after_terminal(monkeypatch):
    application = FakeApplication(
        [
            operation_payload(state="queued"),
            operation_payload(state="running"),
            operation_payload(state="succeeded", coverage=coverage_payload(export_ready=True)),
        ]
    )
    sleeps = Mock()
    monkeypatch.setattr("pandrator_mcp.tools.generation_operations.time.sleep", sleeps)
    clock_values = iter([0.0, 0.0, 0.2, 0.3])
    monkeypatch.setattr(
        "pandrator_mcp.tools.generation_operations.time.monotonic",
        lambda: next(clock_values),
    )

    outcome = get_generation_operation(
        make_runtime(application),
        GetGenerationOperationInput(operation_id="stable-root-id", wait_seconds=1),
    )

    assert outcome.result["state"] == "succeeded"
    assert outcome.result["poll_count"] == 3
    assert outcome.result["timed_out"] is False
    assert sleeps.call_count == 2
    assert outcome.next_actions[0].tool == "pandrator_plan_export_variant"
    assert outcome.next_actions[0].arguments == {
        "session_id": "session-id",
        "generation_run_id": "run-id",
        "audio_version_side": "after",
    }


def test_terminal_export_action_preserves_selected_before_audio_version():
    application = FakeApplication(
        [
            operation_payload(
                state="succeeded",
                coverage=coverage_payload(
                    export_ready=True,
                    audio_version_side="before",
                ),
            )
        ]
    )

    outcome = get_generation_operation(
        make_runtime(application),
        GetGenerationOperationInput(operation_id="stable-root-id"),
    )

    assert outcome.next_actions[0].tool == "pandrator_plan_export_variant"
    assert outcome.next_actions[0].arguments["audio_version_side"] == "before"


def test_terminal_incomplete_coverage_points_to_selected_compact_segments():
    application = FakeApplication([operation_payload(state="failed", coverage=coverage_payload())])
    outcome = get_generation_operation(
        make_runtime(application),
        GetGenerationOperationInput(operation_id="stable-root-id"),
    )
    assert outcome.next_actions[0].tool == "pandrator_list_generation_segments"
    assert outcome.next_actions[0].arguments == {
        "session_id": "session-id",
        "generation_run_id": "run-id",
        "audio_version_side": "after",
        "view": "compact",
    }


def test_coverage_tool_returns_only_counts_ids_side_and_bounded_ordinals():
    application = FakeApplication(coverage=coverage_payload())
    outcome = get_generation_run_coverage(
        make_runtime(application),
        GetGenerationRunCoverageInput(generation_run_id="run-id"),
    )
    assert application.coverage_calls == [("run-id", "after")]
    assert outcome.result["run_id"] == "run-id"
    assert outcome.result["missing_segment_ordinals"] == [1]
    assert "text" not in repr(outcome.result)
    assert "path" not in repr(outcome.result)


def test_registration_exposes_flat_typed_read_only_tools_and_structured_shape():
    application = FakeApplication(
        [operation_payload(state="running")],
        coverage=coverage_payload(),
    )
    runtime = make_runtime(application)
    server = FakeServer()
    read_only = object()
    response_modes = []

    def response(envelope, mode):
        response_modes.append(mode)
        return make_response(envelope, mode)

    register_generation_operation_tools(
        server,
        runtime,
        call_validated,
        response,
        read_only=read_only,
    )

    operation_tool = server.tools["pandrator_get_generation_operation"]
    signature = inspect.signature(operation_tool["function"])
    assert list(signature.parameters) == [
        "operation_id",
        "wait_seconds",
        "include_coverage",
        "response_mode",
    ]
    assert signature.parameters["wait_seconds"].default == 0
    assert signature.parameters["include_coverage"].default is True
    assert operation_tool["annotations"] is read_only
    structured = operation_tool["function"](
        operation_id="stable-root-id",
        response_mode="structured",
    )
    assert "structuredContent" in structured
    assert structured["structuredContent"]["result"]["id"] == "stable-root-id"

    coverage_tool = server.tools["pandrator_get_generation_run_coverage"]
    coverage_signature = inspect.signature(coverage_tool["function"])
    assert list(coverage_signature.parameters) == [
        "generation_run_id",
        "audio_version_side",
        "response_mode",
    ]
    assert coverage_signature.parameters["audio_version_side"].default == "after"
    assert coverage_tool["annotations"] is read_only
    structured_coverage = coverage_tool["function"](
        generation_run_id="run-id",
        response_mode="structured",
    )
    assert "structuredContent" in structured_coverage
    assert structured_coverage["structuredContent"]["result"]["run_id"] == "run-id"
    assert response_modes == ["structured", "structured"]
