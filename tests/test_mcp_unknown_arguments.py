"""Raw tool arguments cannot silently disappear before guarded domain validation."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest
from mcp import Client
from mcp.types import TextContent

import pandrator_mcp.server as adapter
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.request_context import _REQUEST_ID, _TRACE_ID, correlation_headers
from tests.test_mcp_media_edit_registration import SENTINEL, envelope
from tests.test_mcp_transcription_options import KEY, option_runtime, source

SECRET = "fixture-private-payload-do-not-echo"


def setup_case(
    tmp_path: Path, monkeypatch, kind: str
) -> tuple[McpRuntime, list[dict[str, Any]], Any, Mock, str, dict[str, Any]]:
    runtime, calls, application = option_runtime(tmp_path)
    manager = Mock()

    def status(_runtime):
        calls.append({"method": "manager_status", "headers": correlation_headers()})
        print(SENTINEL)
        return {"status": "ready"}

    manager.side_effect = status
    monkeypatch.setattr(adapter, "manager_status", manager)
    if kind == "empty":
        return runtime, calls, application, manager, "pandrator_manager_status", {}
    if kind == "scalar":
        return (
            runtime,
            calls,
            application,
            manager,
            "pandrator_transcription_get",
            {"id": "transcription-1"},
        )
    return (
        runtime,
        calls,
        application,
        manager,
        "pandrator_transcribe",
        {"source": source("base64"), "idempotency_key": KEY},
    )


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("kind", ["empty", "scalar", "nested"])
def test_unknown_top_level_arguments_fail_before_handler(
    tmp_path: Path, monkeypatch, capsys, mode: str, kind: str
) -> None:
    runtime, calls, application, manager, name, arguments = setup_case(tmp_path, monkeypatch, kind)
    arguments.update({"z_unknown_option": SECRET, "a_unknown_option": {"payload": SECRET}})
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(
            adapter.build_server(runtime), mode=mode, raise_exceptions=False
        ) as client:
            return await client.call_tool(name, arguments)

    result = asyncio.run(call())
    assert result.is_error
    assert calls == application.mock_calls == manager.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    text = next(item.text for item in result.content if isinstance(item, TextContent))
    failure = json.loads(text)
    assert failure["code"] == "validation_error"
    assert failure["request_id"]
    assert failure["retryable"] is False
    assert failure["next_actions"] == []
    assert failure["details"] == {
        "errors": [
            {"type": "extra_forbidden", "loc": [field], "msg": "Extra inputs are not permitted"}
            for field in ["a_unknown_option", "z_unknown_option"]
        ]
    }
    assert SECRET not in text
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SECRET not in captured.err
    assert SENTINEL not in captured.err


@pytest.mark.parametrize("mode", ["auto", "legacy"])
@pytest.mark.parametrize("kind", ["empty", "scalar", "nested"])
def test_declared_arguments_keep_native_success_and_correlation(
    tmp_path: Path, monkeypatch, capsys, mode: str, kind: str
) -> None:
    runtime, calls, application, manager, name, arguments = setup_case(tmp_path, monkeypatch, kind)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(
            adapter.build_server(runtime), mode=mode, raise_exceptions=False
        ) as client:
            return await client.call_tool(name, arguments)

    value = envelope(asyncio.run(call()))
    assert value["result"] == (
        {"status": "ready"} if kind == "empty" else {"id": "transcription-1", "status": "queued"}
    )
    assert all(c["headers"]["X-Request-ID"] == value["request_id"] for c in calls)
    assert len(calls) == (2 if kind == "nested" else 1)
    assert len(application.mock_calls) == (0 if kind == "empty" else len(calls))
    assert len(manager.mock_calls) == (1 if kind == "empty" else 0)
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    captured = capsys.readouterr()
    assert captured.out == ""
    assert SENTINEL in captured.err


def test_every_registered_tool_advertises_closed_top_level_arguments(tmp_path: Path) -> None:
    runtime, calls, application = option_runtime(tmp_path)

    async def discover():
        async with Client(adapter.build_server(runtime)) as client:
            return await client.list_tools()

    tools = asyncio.run(discover()).tools
    assert len(tools) == 162
    assert all(tool.input_schema.get("additionalProperties") is False for tool in tools)
    assert calls == application.mock_calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_unknown_tool_stays_sdk_error(tmp_path: Path, capsys, mode: str) -> None:
    runtime, calls, application = option_runtime(tmp_path)
    before = (_REQUEST_ID.get(), _TRACE_ID.get())

    async def call():
        async with Client(
            adapter.build_server(runtime), mode=mode, raise_exceptions=False
        ) as client:
            return await client.call_tool("pandrator_missing_fixture_tool", {"unknown": SECRET})

    result = asyncio.run(call())
    assert result.is_error
    assert calls == application.mock_calls == []
    assert (_REQUEST_ID.get(), _TRACE_ID.get()) == before
    text = next(item.text for item in result.content if isinstance(item, TextContent))
    assert "Unknown tool" in text
    assert SECRET not in text
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_declared_dictionary_keeps_its_nested_keys(tmp_path: Path, mode: str) -> None:
    runtime, calls, application = option_runtime(tmp_path)
    value = {"a_unknown_option": {"payload": SECRET}, "z_unknown_option": 4}
    application.patch_session_settings.return_value = {"override": value, "revision": 2}

    async def call():
        async with Client(adapter.build_server(runtime), mode=mode) as client:
            return await client.call_tool(
                "pandrator_patch_session_settings",
                {
                    "session_id": "session-1",
                    "section": "text",
                    "expected_revision": 1,
                    "value": value,
                    "idempotency_key": KEY,
                },
            )

    result = envelope(asyncio.run(call()))
    assert result["result"]["override"] == value
    application.patch_session_settings.assert_called_once_with(
        "session-1", section="text", expected_revision=1, value=value, idempotency_key=KEY
    )
    assert calls == []


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_unknown_argument_over_authenticated_http(tmp_path: Path, mode: str) -> None:
    import httpx2
    from mcp.client.streamable_http import streamable_http_client

    from pandrator_mcp.http import build_http_app

    runtime, calls, application = option_runtime(tmp_path)
    token = "t" * 43
    app = build_http_app(runtime, token=token)

    async def call():
        async with (
            app.app.router.lifespan_context(app.app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app),
                base_url="http://127.0.0.1:8099",
                headers={"Authorization": f"Bearer {token}"},
            ) as http_client,
            Client(
                streamable_http_client("http://127.0.0.1:8099/mcp", http_client=http_client),
                mode=mode,
                raise_exceptions=False,
            ) as client,
        ):
            return await client.call_tool(
                "pandrator_transcription_get", {"id": "transcription-1", "unknown_option": SECRET}
            )

    result = asyncio.run(call())
    assert result.is_error
    assert calls == application.mock_calls == []
    text = next(item.text for item in result.content if isinstance(item, TextContent))
    assert json.loads(text)["code"] == "validation_error"
    assert SECRET not in text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_unknown_argument_over_native_stdio(tmp_path: Path, mode: str) -> None:
    import sys

    from mcp import StdioServerParameters

    script = (
        "from pathlib import Path\n"
        "import pandrator_mcp.server as adapter\n"
        "from pandrator_mcp.context import build_runtime\n"
        "from pandrator_mcp.settings import McpSettings\n"
        "def witness(*args):\n"
        "    raise AssertionError('unknown argument reached handler')\n"
        "adapter.manager_status = witness\n"
        "runtime = build_runtime(McpSettings(target_name='missing', "
        "configuration_path=Path('absent.json')))\n"
        "adapter.build_server(runtime).run()\n"
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-c", script],
        cwd=str(tmp_path),
        env={"PYTHONPATH": str(Path(__file__).resolve().parents[1])},
    )

    async def call():
        async with Client(parameters, mode=mode, raise_exceptions=False) as client:
            return await client.call_tool("pandrator_manager_status", {"unknown_option": SECRET})

    result = asyncio.run(call())
    assert result.is_error
    text = next(item.text for item in result.content if isinstance(item, TextContent))
    assert json.loads(text)["code"] == "validation_error"
    assert SECRET not in text


@pytest.mark.parametrize("mode", ["auto", "legacy"])
def test_removed_tool_stays_sdk_error(tmp_path: Path, capsys, mode: str) -> None:
    runtime, calls, application = option_runtime(tmp_path)
    server = adapter.build_server(runtime)
    server.remove_tool("pandrator_transcription_get")

    async def call():
        async with Client(server, mode=mode, raise_exceptions=False) as client:
            return await client.call_tool(
                "pandrator_transcription_get", {"id": "transcription-1", "unknown": SECRET}
            )

    result = asyncio.run(call())
    assert result.is_error
    assert calls == application.mock_calls == []
    text = next(item.text for item in result.content if isinstance(item, TextContent))
    assert "Unknown tool" in text
    assert SECRET not in text
    assert capsys.readouterr().out == ""
