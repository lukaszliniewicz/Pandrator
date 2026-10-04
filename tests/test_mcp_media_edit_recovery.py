"""Native recovery actions distinguish uncertain enqueue from known accepted work."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from mcp import Client
from mcp.types import TextContent

from pandrator_mcp.errors import NextAction, PandratorMcpError
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from pandrator_mcp.schemas import GetWorkInput, ListWorkInput
from pandrator_mcp.schemas.media_edit import ProposeMediaEditArguments, RenderMediaEditArguments
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.media_edit import propose_media_edit, render_media_edit
from tests.test_mcp_media_edit_registration import CASES, SENTINEL, Case, envelope, fixture_runtime

OPERATIONS = [CASES[7], CASES[8]]


def validated(case: Case, *, wait: bool):
    model = (
        ProposeMediaEditArguments
        if case.method == "propose_media_edit"
        else RenderMediaEditArguments
    )
    return model.model_validate({**case.arguments, "wait": wait, "timeout_seconds": 7})


@pytest.mark.parametrize("case", OPERATIONS, ids=lambda case: case.name)
@pytest.mark.parametrize("phase", ["enqueue", "wait"])
@pytest.mark.parametrize("followup", [False, True])
def test_native_timeout_recovery(
    tmp_path: Path, capsys, case: Case, phase: str, followup: bool
) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    arguments = validated(case, wait=phase == "wait")
    method = case.method if phase == "enqueue" else "wait_for_job"
    original_effect = getattr(application, method).side_effect
    details = {
        "operation_outcome": "unknown" if phase == "enqueue" else "not_applicable",
        "retry_policy": "same_request_and_idempotency_key"
        if phase == "enqueue"
        else "safe_to_retry_read",
    }
    failure = PandratorMcpError(
        "application_response_timeout", "Fixture response timeout.", details=details, retryable=True
    )
    attempted = 0

    def timeout_once(*args, **kwargs):
        nonlocal attempted
        value = original_effect(*args, **kwargs)
        attempted += 1
        if attempted == 1:
            raise failure
        return value

    getattr(application, method).side_effect = timeout_once
    kind = "media_edit.propose" if case.method == "propose_media_edit" else "media_edit.render"

    def get_work(work_id):
        calls.append(
            {
                "method": "get_work",
                "args": [work_id],
                "kwargs": {},
                "headers": correlation_headers(),
            }
        )
        print(SENTINEL)
        return {
            "id": work_id,
            "kind": kind,
            "state": "succeeded",
            "session_id": arguments.session_id,
        }

    application.get_work.side_effect = get_work
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def exercise():
        async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
            result = await client.call_tool(case.name, arguments.model_dump(mode="json"))
            assert result.is_error
            block = next(item for item in result.content if isinstance(item, TextContent))
            prefix = f"Error executing tool {case.name}: "
            assert block.text.startswith(prefix)
            value = json.loads(block.text[len(prefix) :])
            assert value["code"] == failure.code
            assert value["message"] == str(failure)
            assert value["details"] == details
            assert value["retryable"] is True
            assert len(calls) == (1 if phase == "enqueue" else 2)
            assert all(call["headers"]["X-Request-ID"] == value["request_id"] for call in calls)
            actions = value["next_actions"]
            if phase == "enqueue":
                assert len(actions) == 2
                assert actions[0]["tool"] == "pandrator_list_work"
                assert actions[0]["arguments"] == {
                    "session_id": arguments.session_id,
                    "kinds": [kind],
                    "limit": 5,
                }
                ListWorkInput.model_validate(actions[0]["arguments"])
                assert actions[1]["tool"] == case.name
                assert actions[1]["arguments"] == arguments.model_dump(mode="json")
                assert "retained" in actions[1]["reason"]
                assert "expired" in actions[1]["reason"]
                action = actions[1]
            else:
                assert len(actions) == 1
                assert actions[0]["tool"] == "pandrator_get_work"
                assert actions[0]["arguments"] == {"work_id": "job-1", "wait_seconds": 0}
                GetWorkInput.model_validate(actions[0]["arguments"])
                action = actions[0]
            if followup:
                recovered = await client.call_tool(action["tool"], action["arguments"])
                recovered_value = envelope(recovered)
                assert recovered_value["work"]["id"] == "job-1"
                assert recovered_value["request_id"] != value["request_id"]
                assert calls[-1]["headers"]["X-Request-ID"] == recovered_value["request_id"]

    asyncio.run(exercise())
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    enqueue_calls = [call for call in calls if call["method"] == case.method]
    assert len(enqueue_calls) == (2 if followup and phase == "enqueue" else 1)
    for call in enqueue_calls:
        assert call["args"] == list(case.args)
        assert call["kwargs"] == case.kwargs
    assert len([call for call in calls if call["method"] == "wait_for_job"]) == (
        1 if phase == "wait" else 0
    )
    assert len([call for call in calls if call["method"] == "get_work"]) == (
        1 if followup and phase == "wait" else 0
    )
    assert len(application.mock_calls) == len(calls)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


@pytest.mark.parametrize("case", OPERATIONS, ids=lambda case: case.name)
@pytest.mark.parametrize("phase", ["enqueue", "wait"])
def test_other_errors_retain_identity_and_actions(tmp_path: Path, case: Case, phase: str) -> None:
    runtime, calls, application = fixture_runtime(tmp_path)
    action = NextAction(
        tool="pandrator_get_work", arguments={"work_id": "fixture"}, reason="Fixture action."
    )
    failure = PandratorMcpError(
        "application_unavailable" if phase == "enqueue" else "not_found",
        "Fixture error.",
        next_actions=[action],
        details={"fixture": True},
    )
    method = case.method if phase == "enqueue" else "wait_for_job"
    original_effect = getattr(application, method).side_effect

    def fail(*args, **kwargs):
        original_effect(*args, **kwargs)
        raise failure

    getattr(application, method).side_effect = fail
    arguments = validated(case, wait=phase == "wait")
    with pytest.raises(PandratorMcpError) as caught:
        if isinstance(arguments, ProposeMediaEditArguments):
            propose_media_edit(runtime, arguments)
        else:
            render_media_edit(runtime, arguments)
    assert caught.value is failure
    assert failure.next_actions == [action]
    assert failure.details == {"fixture": True}
    assert len(calls) == (1 if phase == "enqueue" else 2)
    assert len(application.mock_calls) == len(calls)
