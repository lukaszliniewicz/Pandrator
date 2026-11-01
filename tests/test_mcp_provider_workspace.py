"""Scoped provider status keeps private configuration out of MCP metadata."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime, build_runtime
from pandrator_mcp.schemas.inventory import ProviderStatusInput
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.inventory import provider_status


class RecordingClient(ApplicationVoiceMethods):
    def __init__(self, payload: dict[str, Any]):
        self.payload = payload
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((path, kwargs))
        return self.payload

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Provider status must not request binary content.")


def _runtime(client: RecordingClient) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: client))


def test_scoped_status_uses_actual_client_builder_and_omits_private_configuration():
    client = RecordingClient(
        {
            "workspace_id": "team-workspace",
            "can_manage": False,
            "items": [
                {
                    "id": "approved",
                    "enabled": True,
                    "label": "Workshop model",
                    "base_url": "https://private.invalid/credential",
                    "secret_ref": "private-reference",
                    "options_json": {"private": "do not expose"},
                    "revision": 1,
                },
                {"id": "disabled", "enabled": False},
            ],
        }
    )
    result = provider_status(
        _runtime(client),
        ProviderStatusInput(
            workspace_id="team-workspace",
            include_disabled=False,
        ),
    )
    assert client.calls == [
        ("/api/v1/providers", {"parameters": {"workspace_id": "team-workspace"}})
    ]
    assert result["workspace_id"] == "team-workspace" and result["can_manage"] is False
    assert [item["id"] for item in result["items"]] == ["approved"]
    assert result["items"][0]["credential_configured"] is None
    assert "private.invalid" not in str(result) and "private-reference" not in str(result)
    assert "options_json" not in str(result) and "base_url" not in result["items"][0]


def test_personal_default_omits_query_and_preserves_only_actual_readiness_booleans():
    client = RecordingClient(
        {
            "items": [
                {"id": "ready", "enabled": True, "credential_configured": True},
                {"id": "missing", "enabled": True, "credential_configured": False},
                {"id": "unknown", "enabled": True, "credential_configured": "false"},
            ]
        }
    )
    result = provider_status(_runtime(client), ProviderStatusInput())
    assert client.calls == [("/api/v1/providers", {})]
    assert [item["credential_configured"] for item in result["items"]] == [True, False, None]


def test_workspace_argument_is_optional_bounded_and_strict():
    assert ProviderStatusInput().workspace_id is None
    for value in ("", "a" * 81, True, 17):
        with pytest.raises(ValidationError):
            ProviderStatusInput.model_validate({"workspace_id": value})


def test_registered_sdk_status_tool_accepts_the_same_workspace_field(tmp_path: Path):
    runtime = build_runtime(
        McpSettings(
            target_name="unconfigured",
            configuration_path=tmp_path / "missing-targets.json",
        )
    )
    tool = next(
        item
        for item in asyncio.run(build_server(runtime).list_tools())
        if item.name == "pandrator_get_provider_status"
    )
    assert "workspace_id" in tool.input_schema["properties"]
    assert "include_disabled" in tool.input_schema["properties"]


def test_openapi_declares_catalogue_roots_and_existing_management_scopes():
    paths = build_openapi_document()["paths"]
    for path, method in (
        ("/api/v1/providers", "get"),
        ("/api/v1/providers", "post"),
        ("/api/v1/providers/{providerId}/models", "get"),
        ("/api/v1/providers/{providerId}/models/refresh", "post"),
        ("/api/v1/credentials", "get"),
        ("/api/v1/credentials/{credentialId}", "put"),
    ):
        operation = paths[path][method]
        parameters = {item["name"] for item in operation["parameters"]}
        assert "workspace_id" in parameters
        if method == "get" and "/providers" in path:
            assert "session_id" in parameters
        if method != "get":
            assert {"app.read", "app.write", "app.credentials.write"} <= set(
                operation["x-required-scopes"]
            )
        if path.endswith("/refresh"):
            assert "app.run" in operation["x-required-scopes"]
