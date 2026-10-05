"""Native resource errors retain typed failures and safe unexpected-error wrapping."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp.shared.exceptions import MCPError
from pydantic import BaseModel, create_model, model_validator

import pandrator_mcp.registrations.resources as resource_owner
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import NextAction, PandratorMcpError, ToolFailure
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from tests.test_mcp_media_edit_registration import fixture_runtime
from tests.test_mcp_resource_validation import invoke_error
from tests.test_mcp_session_registration import capture_value

PROTOCOLS = ["2026-07-28", "legacy"]
MODES = ["business", "validation", "unexpected"]
SENTINEL = "resource errors fixture stdout sentinel"
UNEXPECTED_MARKER = "private-resource-unexpected-fixture"
VALIDATION_MESSAGE = "Controlled resource validation failure."
BUSINESS_MESSAGE = "Controlled resource unavailable."
FIXTURE_ACTION = {
    "tool": "pandrator_get_target_status",
    "arguments": {},
    "reason": "fixture recovery reason",
}


@dataclass(frozen=True)
class ResourceCase:
    uri: str
    model: str
    callback: str
    unexpected_message: str


CASES = [
    ResourceCase(
        "pandrator://live/status",
        "SystemStatusInput",
        "system_status",
        "Error reading resource pandrator://live/status",
    ),
    ResourceCase(
        "pandrator://sessions/session-1/workflow",
        "GetWorkflowInput",
        "get_workflow",
        "Error creating resource from template pandrator://sessions/session-1/workflow",
    ),
]


def observe_context() -> dict[str, Any]:
    return {
        "request_id": _REQUEST_ID.get(),
        "trace_id": _TRACE_ID.get(),
        "headers": correlation_headers(),
    }


def install_failure(
    monkeypatch: pytest.MonkeyPatch,
    runtime: McpRuntime,
    case: ResourceCase,
    mode: str,
    observed: list[dict[str, Any]],
    reached: list[bool],
) -> None:
    if mode == "validation":

        @model_validator(mode="before")
        @classmethod
        def probe(cls: Any, value: Any) -> Any:
            observed.append(observe_context())
            print(SENTINEL)
            raise ValueError(VALIDATION_MESSAGE)

        validators: dict[str, Any] = {"probe": probe}
        controlled = create_model(
            "ControlledResourceError" + case.model,
            __base__=getattr(resource_owner, case.model),
            __validators__=validators,
        )
        monkeypatch.setattr(resource_owner, case.model, controlled)

        def reject(*_args: Any) -> None:
            reached.append(True)
            raise AssertionError("The resource handler must not run for invalid DTO input.")

        monkeypatch.setattr(resource_owner, case.callback, reject)
        return

    def handler(current: McpRuntime, arguments: BaseModel) -> None:
        assert current is runtime
        reached.append(True)
        observed.append(
            {
                **observe_context(),
                "model": type(arguments).__name__,
                "model_json": arguments.model_dump(mode="json"),
                "model_fields_set": sorted(arguments.model_fields_set),
            }
        )
        print(SENTINEL)
        if mode == "business":
            raise PandratorMcpError(
                "application_unavailable",
                BUSINESS_MESSAGE,
                details={"stage": "transcribe"},
                retryable=True,
                next_actions=[
                    NextAction(
                        tool="pandrator_get_target_status",
                        arguments={},
                        reason="fixture recovery reason",
                    )
                ],
            )
        assert mode == "unexpected"
        raise RuntimeError(UNEXPECTED_MARKER)

    monkeypatch.setattr(resource_owner, case.callback, handler)


def record_native(
    record_property: Callable[[str, object], None],
    error: MCPError,
    observed: list[dict[str, Any]],
    captured_stdout: str,
    captured_stderr: str,
) -> None:
    # Apply the shared targeted request_id regex to opaque resource-message text.
    message = capture_value({"text": error.message})["text"]
    record_property(
        "native_capture",
        json.dumps(
            capture_value(
                {
                    "native_error": {"code": error.code, "message": message, "data": error.data},
                    "observed_context": observed,
                    "captured_stdout": captured_stdout,
                    "captured_stderr": captured_stderr,
                }
            ),
            ensure_ascii=False,
        ),
    )


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=["modern", "legacy"])
@pytest.mark.parametrize("case", CASES, ids=[case.callback for case in CASES])
@pytest.mark.parametrize("mode", MODES)
def test_native_resource_failure_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case: ResourceCase,
    mode: str,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[dict[str, Any]] = []
    reached: list[bool] = []
    install_failure(monkeypatch, runtime, case, mode, observed, reached)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    error = asyncio.run(invoke_error(runtime, case.uri, protocol))
    captured = capsys.readouterr()
    record_native(record_property, error, observed, captured.out, captured.err)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == []
    assert reached == ([] if mode == "validation" else [True])
    assert error.code == -32603 and error.data == {"uri": case.uri}
    assert len(observed) == 1
    context = observed[0]
    assert context["request_id"] and context["trace_id"]
    assert context["request_id"] == context["headers"]["X-Request-ID"]
    traceparent = context["headers"]["traceparent"]
    assert re.fullmatch(r"00-[a-f0-9]{32}-[a-f0-9]{16}-01", traceparent)
    assert traceparent.split("-")[1] == context["trace_id"]
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
    if mode == "unexpected":
        assert error.message == case.unexpected_message
        assert UNEXPECTED_MARKER not in json.dumps(
            {"code": error.code, "message": error.message, "data": error.data}
        )
        return
    failure = ToolFailure.model_validate_json(error.message)
    assert failure.request_id == context["request_id"]
    if mode == "business":
        assert failure.model_dump(mode="json") == {
            "code": "application_unavailable",
            "message": BUSINESS_MESSAGE,
            "request_id": context["request_id"],
            "details": {"stage": "transcribe"},
            "retryable": True,
            "next_actions": [FIXTURE_ACTION],
        }
        return
    errors = failure.details["errors"]
    assert errors == [
        {
            "type": "value_error",
            "loc": [],
            "msg": "Value error, Controlled resource validation failure.",
        }
    ]
    assert all(key not in item for item in errors for key in ("input", "ctx", "url"))
    assert failure.model_dump(mode="json") == {
        "code": "validation_error",
        "message": "The tool input is invalid.",
        "request_id": context["request_id"],
        "details": {"errors": errors},
        "retryable": False,
        "next_actions": [],
    }


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=["modern", "legacy"])
@pytest.mark.parametrize("case", CASES, ids=[case.callback for case in CASES])
def test_handler_origin_validation_error_retains_generic_resource_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case: ResourceCase,
) -> None:
    from pandrator_mcp.schemas import GetWorkflowInput

    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[dict[str, Any]] = []
    reached: list[bool] = []

    def handler(current: McpRuntime, arguments: BaseModel) -> None:
        assert current is runtime
        reached.append(True)
        observed.append(
            {
                **observe_context(),
                "model": type(arguments).__name__,
                "model_json": arguments.model_dump(mode="json"),
                "model_fields_set": sorted(arguments.model_fields_set),
            }
        )
        print(SENTINEL)
        GetWorkflowInput(session_id="")

    monkeypatch.setattr(resource_owner, case.callback, handler)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    error = asyncio.run(invoke_error(runtime, case.uri, protocol))
    captured = capsys.readouterr()
    record_native(record_property, error, observed, captured.out, captured.err)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == [] and reached == [True]
    assert error.code == -32603 and error.data == {"uri": case.uri}
    assert error.message == case.unexpected_message
    assert len(observed) == 1
    context = observed[0]
    assert context["request_id"] and context["trace_id"]
    assert context["request_id"] == context["headers"]["X-Request-ID"]
    traceparent = context["headers"]["traceparent"]
    assert re.fullmatch(r"00-[a-f0-9]{32}-[a-f0-9]{16}-01", traceparent)
    assert traceparent.split("-")[1] == context["trace_id"]
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
