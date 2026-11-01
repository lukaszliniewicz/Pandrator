"""Registration tests for the fixed public-link MCP tools."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast

import pytest

from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.application_public_links import ApplicationPublicLinkMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.public_link_actions import PUBLIC_LINK_OPERATIONS
from pandrator_mcp.schemas import PUBLIC_LINK_INPUT_MODELS, TOOL_INPUT_MODELS
from pandrator_mcp.server import build_server

_REQUEST_UUID = "019a1000-0000-7000-8000-000000000001"
_SAFE_RESULT_URL = "https://example.invalid/public-link/redacted"


class FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def public_link_request(
        self,
        action: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        self.calls.append((action, dict(arguments)))
        if action == "create_project_link":
            return {"id": "link-one", "url": _SAFE_RESULT_URL}
        return {"ok": True}


def test_application_client_composes_public_link_methods() -> None:
    assert ApplicationPublicLinkMethods in ApplicationClient.__mro__


def test_build_server_registers_and_calls_public_link_tools_without_live_target() -> None:
    try:
        from mcp import Client
    except ImportError:
        pytest.skip("MCP SDK is not installed")

    application = FakeApplication()
    runtime = cast(
        McpRuntime,
        SimpleNamespace(require_application=lambda: application),
    )
    server = build_server(runtime)

    async def exercise() -> None:
        async with Client(server, mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            public_names = {operation.name for operation in PUBLIC_LINK_OPERATIONS}
            assert public_names == {
                "pandrator_get_public_sharing_settings",
                "pandrator_configure_public_sharing",
                "pandrator_list_project_links",
                "pandrator_create_project_link",
                "pandrator_revoke_project_link",
                "pandrator_list_link_submissions",
                "pandrator_get_link_submission",
                "pandrator_review_link_submission",
            }
            assert public_names <= set(tools)
            assert len(public_names) == 8

            for operation in PUBLIC_LINK_OPERATIONS:
                tool = tools[operation.name]
                expected_properties = set(operation.model.model_fields) | {"response_mode"}
                assert set(tool.input_schema["properties"]) == expected_properties
                assert set(tool.input_schema.get("required", ())) == {
                    name
                    for name, field in operation.model.model_fields.items()
                    if field.is_required()
                }
                assert operation.model.model_json_schema()["additionalProperties"] is False
                assert operation.model in TOOL_INPUT_MODELS
                assert operation.model in PUBLIC_LINK_INPUT_MODELS
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is (operation.method == "GET")
                assert tool.annotations.open_world_hint is False
                if operation.method != "GET":
                    assert tool.annotations.destructive_hint is False
                    assert tool.annotations.idempotent_hint is True

                action = ACTION_CATALOG.get(operation.name)
                assert action.input_model == operation.model.__name__
                assert action.required_scope == operation.scope
                assert action.risk == (
                    RiskClass.READ if operation.method == "GET" else RiskClass.WRITE
                )
                assert action.downstream_operation_id is None
                assert action.method is None
                assert action.path is None
                assert action.requires_idempotency is (
                    operation.action in {"create_project_link", "review_link_submission"}
                )

            settings_tool = await client.call_tool(
                "pandrator_get_public_sharing_settings",
                {"response_mode": "standard"},
            )
            assert not settings_tool.is_error
            assert settings_tool.structured_content["result"] == {"ok": True}

            configured = await client.call_tool(
                "pandrator_configure_public_sharing",
                {
                    "expected_revision": 0,
                    "enabled": False,
                    "public_origin": None,
                },
            )
            assert not configured.is_error
            assert configured.structured_content["result"] == {"ok": True}

            create_arguments = {
                "project_id": "project-one",
                "label": "Owner link",
                "mode": "manage_project",
                "request_key": _REQUEST_UUID,
                "response_mode": "structured",
            }
            structured = await client.call_tool(
                "pandrator_create_project_link",
                create_arguments,
            )
            assert not structured.is_error
            assert structured.structured_content["result"]["url"] == _SAFE_RESULT_URL

            create_arguments["response_mode"] = "standard"
            standard = await client.call_tool(
                "pandrator_create_project_link",
                create_arguments,
            )
            assert not standard.is_error
            assert standard.structured_content["result"]["url"] == _SAFE_RESULT_URL

        creates = [call for call in application.calls if call[0] == "create_project_link"]
        assert len(creates) == 2
        assert [call[1]["request_key"] for call in creates] == [_REQUEST_UUID, _REQUEST_UUID]
        assert all("Authorization" not in call[1] for call in application.calls)

    asyncio.run(exercise())
