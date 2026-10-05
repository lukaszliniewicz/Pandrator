"""Native resource DTO validation retains request context and stdout protection."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.shared.exceptions import MCPError
from mcp.types import ReadResourceResult, TextResourceContents
from pydantic import BaseModel, create_model, model_validator

import pandrator_mcp.server as adapter
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.results import ToolOutcome
from pandrator_mcp.schemas import (
    CapabilitiesInput,
    GetWorkflowInput,
    GetWorkInput,
    SystemStatusInput,
    TargetStatusInput,
)
from tests.test_mcp_media_edit_registration import fixture_runtime

SENTINEL = "resource validation fixture stdout sentinel"
MARKER = "private-resource-validation-fixture"
RESULT = {"fixture": "ok"}
PROTOCOLS = ["2026-07-28", "legacy"]


@dataclass(frozen=True)
class ResourceCase:
    uri: str
    model: str | None
    callback: str
    outcome: bool = False


CASES = [
    ResourceCase("pandrator://guide/index", None, "index"),
    ResourceCase("pandrator://guide/overview", None, "get", True),
    ResourceCase("pandrator://target/current", "TargetStatusInput", "target_status"),
    ResourceCase("pandrator://live/status", "SystemStatusInput", "system_status", True),
    ResourceCase("pandrator://live/capabilities", "CapabilitiesInput", "capabilities"),
    ResourceCase(
        "pandrator://sessions/session-1/workflow", "GetWorkflowInput", "get_workflow", True
    ),
    ResourceCase("pandrator://work/job/job-1", "GetWorkInput", "get_work"),
]
DTO_CASES = CASES[2:]
ERROR_CASES = [CASES[3], CASES[6]]
OVERSIZE_CASES = [
    ResourceCase(
        "pandrator://sessions/" + "s" * 81 + "/workflow", "GetWorkflowInput", "get_workflow"
    ),
    ResourceCase("pandrator://work/job/" + "w" * 121, "GetWorkInput", "get_work"),
]


@dataclass(frozen=True)
class Correlation:
    request_id: str | None
    trace_id: str | None
    headers: dict[str, str]


def observe_context() -> Correlation:
    return Correlation(_REQUEST_ID.get(), _TRACE_ID.get(), correlation_headers())


def assert_installed_context(observed: Correlation) -> None:
    assert observed.request_id and observed.trace_id
    assert observed.headers["X-Request-ID"] == observed.request_id
    traceparent = observed.headers["traceparent"]
    assert re.fullmatch(r"00-[a-f0-9]{32}-[a-f0-9]{16}-01", traceparent)
    assert traceparent.split("-")[1] == observed.trace_id


def record_error(
    record_property: Callable[[str, object], None], error: MCPError, observed: list[Correlation]
) -> None:
    record_property(
        "native_resource_error",
        json.dumps({"code": error.code, "message": error.message, "data": error.data}),
    )
    record_property(
        "observed_correlation",
        json.dumps(
            [
                {"request_id": item.request_id, "trace_id": item.trace_id, "headers": item.headers}
                for item in observed
            ]
        ),
    )


async def invoke(runtime: McpRuntime, uri: str, protocol: str) -> ReadResourceResult:
    async with Client(
        adapter.build_server(runtime), mode=protocol, raise_exceptions=False
    ) as client:
        return await client.read_resource(uri)


async def invoke_error(runtime: McpRuntime, uri: str, protocol: str) -> MCPError:
    async with Client(
        adapter.build_server(runtime), mode=protocol, raise_exceptions=False
    ) as client:
        with pytest.raises(MCPError) as raised:
            await client.read_resource(uri)
        return raised.value


def expected_model(case: ResourceCase) -> BaseModel | None:
    if case.model == "TargetStatusInput":
        return TargetStatusInput(include_authenticated_identity=False)
    if case.model == "SystemStatusInput":
        return SystemStatusInput()
    if case.model == "CapabilitiesInput":
        return CapabilitiesInput()
    if case.model == "GetWorkflowInput":
        return GetWorkflowInput(session_id="session-1")
    if case.model == "GetWorkInput":
        return GetWorkInput(work_type="job", work_id="job-1", include_events=False)
    assert case.model is None
    return None


def install_rejecting_handler(
    monkeypatch: pytest.MonkeyPatch, case: ResourceCase, reached: list[bool]
) -> None:
    def reject(*_args: Any) -> None:
        reached.append(True)
        raise AssertionError("Resource handler must not run for invalid DTO input.")

    monkeypatch.setattr(adapter, case.callback, reject)


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=["modern", "legacy"])
@pytest.mark.parametrize("case", DTO_CASES, ids=[case.callback for case in DTO_CASES])
def test_resource_dto_validator_runs_inside_request_and_stdout_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case: ResourceCase,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[Correlation] = []
    reached: list[bool] = []
    assert case.model is not None

    @model_validator(mode="before")
    @classmethod
    def probe(cls: Any, value: Any) -> Any:
        observed.append(observe_context())
        print(SENTINEL)
        raise ValueError(MARKER)

    validators: dict[str, Any] = {"probe": probe}
    controlled = create_model(
        "ControlledResource" + case.model,
        __base__=getattr(adapter, case.model),
        __validators__=validators,
    )
    monkeypatch.setattr(adapter, case.model, controlled)
    install_rejecting_handler(monkeypatch, case, reached)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    error = asyncio.run(invoke_error(runtime, case.uri, protocol))
    record_error(record_property, error, observed)
    captured = capsys.readouterr()
    record_property("captured_stdout", captured.out)
    record_property("captured_stderr", captured.err)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert reached == [] and calls == application.mock_calls == []
    assert len(observed) == 1
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
    assert_installed_context(observed[0])


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=["modern", "legacy"])
@pytest.mark.parametrize("case", CASES, ids=[case.callback for case in CASES])
def test_resource_callback_retains_typed_defaults_and_plain_json_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    protocol: str,
    case: ResourceCase,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    expected = expected_model(case)
    observed: list[Correlation] = []
    received: list[tuple[dict[str, Any], list[str]]] = []
    guide_arguments: list[tuple[Any, ...]] = []

    def returned() -> dict[str, str] | ToolOutcome:
        return ToolOutcome(result=dict(RESULT)) if case.outcome else dict(RESULT)

    def handler(current: McpRuntime, values: BaseModel) -> dict[str, str] | ToolOutcome:
        assert current is runtime and expected is not None
        assert isinstance(values, type(expected))
        received.append((values.model_dump(mode="json"), sorted(values.model_fields_set)))
        observed.append(observe_context())
        print(SENTINEL)
        return returned()

    def guide_callback(*args: Any) -> dict[str, str] | ToolOutcome:
        guide_arguments.append(args)
        observed.append(observe_context())
        print(SENTINEL)
        return returned()

    if expected is None:
        monkeypatch.setattr(runtime.guides, case.callback, guide_callback)
    else:
        monkeypatch.setattr(adapter, case.callback, handler)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    result = asyncio.run(invoke(runtime, case.uri, protocol))
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == []
    assert len(result.contents) == 1
    content = result.contents[0]
    assert isinstance(content, TextResourceContents)
    assert json.loads(content.text) == RESULT
    if expected is not None:
        assert received == [(expected.model_dump(mode="json"), sorted(expected.model_fields_set))]
        assert guide_arguments == []
    else:
        assert received == []
        expected_guide_arguments = [()] if case.callback == "index" else [("overview",)]
        assert guide_arguments == expected_guide_arguments
    assert len(observed) == 1
    assert_installed_context(observed[0])
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=["modern", "legacy"])
@pytest.mark.parametrize("case", OVERSIZE_CASES, ids=[case.callback for case in OVERSIZE_CASES])
def test_resource_oversize_ids_reject_before_handler(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
    protocol: str,
    case: ResourceCase,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    reached: list[bool] = []
    install_rejecting_handler(monkeypatch, case, reached)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    error = asyncio.run(invoke_error(runtime, case.uri, protocol))
    record_error(record_property, error, [])
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert reached == [] and calls == application.mock_calls == []


@pytest.mark.parametrize("protocol", PROTOCOLS, ids=["modern", "legacy"])
@pytest.mark.parametrize("case", ERROR_CASES, ids=[case.callback for case in ERROR_CASES])
def test_resource_callback_error_keeps_existing_native_error_and_guard(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    record_property: Callable[[str, object], None],
    protocol: str,
    case: ResourceCase,
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    observed: list[Correlation] = []
    expected = expected_model(case)
    assert expected is not None

    def handler(current: McpRuntime, values: BaseModel) -> None:
        assert current is runtime and isinstance(values, type(expected))
        assert values.model_dump(mode="json") == expected.model_dump(mode="json")
        assert values.model_fields_set == expected.model_fields_set
        observed.append(observe_context())
        print(SENTINEL)
        raise PandratorMcpError("validation_error", MARKER, details={"fixture": MARKER})

    monkeypatch.setattr(adapter, case.callback, handler)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())
    error = asyncio.run(invoke_error(runtime, case.uri, protocol))
    record_error(record_property, error, observed)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    assert calls == application.mock_calls == []
    assert len(observed) == 1
    assert_installed_context(observed[0])
    captured = capsys.readouterr()
    assert captured.out == "" and captured.err.count(SENTINEL) == 1
