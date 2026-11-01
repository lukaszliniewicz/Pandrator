"""Contract tests for the fixed MCP project-viewing adapter."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.application_project_sharing import ApplicationProjectSharingMethods
from pandrator_mcp.project_sharing_actions import PROJECT_SHARING_OPERATIONS
from pandrator_mcp.schemas import PROJECT_SHARING_INPUT_MODELS, TOOL_INPUT_MODELS
from pandrator_mcp.tools.project_sharing import register_project_sharing_tools


class Recorder(ApplicationProjectSharingMethods):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"path": path, **kwargs})
        return {"ok": True}

    def _request_binary_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Project-viewing commands use fixed JSON routes.")


SAMPLES: dict[str, Any] = {
    "project_id": "project/one",
    "user_id": "user/one",
    "query": "Al",
    "limit": 25,
    "offset": 10,
    "expected_revision": 4,
}
EXPECTED_OPERATIONS = {
    "list_shared_projects": (
        "listSharedProjects",
        "GET",
        "/api/v1/projects/shared-with-me",
        "app.read",
    ),
    "get_project_viewers": (
        "getProjectViewers",
        "GET",
        "/api/v1/projects/{project_id}/viewers",
        "app.read",
    ),
    "search_project_viewers": (
        "searchProjectViewers",
        "GET",
        "/api/v1/projects/{project_id}/viewers/users",
        "app.read",
    ),
    "grant_project_view": (
        "grantProjectView",
        "PUT",
        "/api/v1/projects/{project_id}/viewers/{user_id}",
        "app.write",
    ),
    "revoke_project_view": (
        "revokeProjectView",
        "POST",
        "/api/v1/projects/{project_id}/viewers/{user_id}/revoke",
        "app.write",
    ),
}


@pytest.mark.parametrize("operation", PROJECT_SHARING_OPERATIONS, ids=lambda item: item.action)
def test_fixed_operations_match_catalog_and_http_openapi(operation: Any) -> None:
    assert {
        item.action: (item.operation_id, item.method, item.path, item.scope)
        for item in PROJECT_SHARING_OPERATIONS
    } == EXPECTED_OPERATIONS
    assert operation.name == f"pandrator_{operation.action}"
    arguments = {
        name: SAMPLES[name]
        for name, field in operation.model.model_fields.items()
        if field.is_required()
    }
    client = Recorder()
    client.project_sharing_request(operation.action, arguments)
    call = client.calls[0]

    action = ACTION_CATALOG.get(operation.name)
    assert action.input_model == operation.model.__name__
    assert action.required_scope == operation.scope
    assert action.risk == (RiskClass.READ if operation.method == "GET" else RiskClass.WRITE)
    assert action.requires_idempotency is False
    assert action.method == operation.method
    assert action.downstream_operation_id == operation.operation_id
    assert action.path == operation.path.replace("{project_id}", "{projectId}").replace(
        "{user_id}", "{userId}"
    )
    assert action.input_model in {model.__name__ for model in TOOL_INPUT_MODELS}

    document = build_openapi_document()
    public = document["paths"][action.path][operation.method.lower()]
    assert public["operationId"] == operation.operation_id
    assert public["x-required-scopes"] == (
        ["app.read"] if operation.method == "GET" else ["app.read", "app.write"]
    )
    if operation.method == "GET":
        assert "requestBody" not in public
    else:
        assert public["requestBody"]["required"] is True
        assert public["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/ProjectViewingRevisionRequest"
        )
    expected_parameters = {
        "list_shared_projects": {"query", "limit", "offset"},
        "get_project_viewers": {"projectId"},
        "search_project_viewers": {"projectId", "query"},
        "grant_project_view": {"projectId", "userId"},
        "revoke_project_view": {"projectId", "userId"},
    }[operation.action]
    assert {parameter["name"] for parameter in public["parameters"]} == expected_parameters
    assert call["method"] == operation.method
    assert call["read_only_operation"] is (operation.method == "GET")
    assert call["maximum_body_bytes"] == 8 * 1024
    if operation.method == "GET":
        assert set(call["parameters"]) == expected_parameters - {
            "projectId",
            "userId",
        }
        if "query" in expected_parameters:
            assert "query" in call["parameters"]
            assert "q" not in call["parameters"]
        if operation.action == "list_shared_projects":
            assert call["parameters"] == {"query": "", "limit": 50, "offset": 0}
        assert call["body"] is None
    else:
        assert call["parameters"] is None
        assert call["body"] == {"expected_revision": SAMPLES["expected_revision"]}
        assert "idempotency_key" not in call


def test_fixed_routes_quote_ids_and_get_queries_keep_the_openapi_names() -> None:
    client = Recorder()
    client.project_sharing_request("get_project_viewers", {"project_id": "one/two"})
    assert client.calls[-1]["path"] == "/api/v1/projects/one%2Ftwo/viewers"

    client.project_sharing_request(
        "search_project_viewers", {"project_id": "one/two", "query": "Al"}
    )
    assert client.calls[-1]["path"] == "/api/v1/projects/one%2Ftwo/viewers/users"
    assert client.calls[-1]["parameters"] == {"query": "Al"}

    client.project_sharing_request(
        "grant_project_view",
        {"project_id": "one/two", "user_id": "three/four", "expected_revision": 2},
    )
    assert client.calls[-1]["path"] == "/api/v1/projects/one%2Ftwo/viewers/three%2Ffour"
    assert client.calls[-1]["body"] == {"expected_revision": 2}
    assert "idempotency_key" not in client.calls[-1]


def test_inputs_are_strict_bounded_and_reject_role_and_idempotency_fields() -> None:
    assert all(model in TOOL_INPUT_MODELS for model in PROJECT_SHARING_INPUT_MODELS)
    client = Recorder()
    invalid = (
        ("list_shared_projects", {"url": "https://invalid.example"}),
        ("list_shared_projects", {"limit": True}),
        ("list_shared_projects", {"query": "x" * 256}),
        ("list_shared_projects", {"limit": 101}),
        ("list_shared_projects", {"offset": -1}),
        ("get_project_viewers", {"project_id": "x" * 37}),
        ("get_project_viewers", {"project_id": ""}),
        ("search_project_viewers", {"project_id": "one", "query": "x" * 65}),
        ("search_project_viewers", {"project_id": "one", "query": "x"}),
        (
            "grant_project_view",
            {
                "project_id": "one",
                "user_id": "two",
                "expected_revision": True,
            },
        ),
        (
            "grant_project_view",
            {"project_id": "one", "user_id": "x" * 37, "expected_revision": 1},
        ),
        (
            "grant_project_view",
            {"project_id": "one", "user_id": "two", "expected_revision": 0},
        ),
        (
            "revoke_project_view",
            {
                "project_id": "one",
                "user_id": "two",
                "expected_revision": 1,
                "idempotency_key": "not-accepted",
            },
        ),
        (
            "grant_project_view",
            {
                "project_id": "one",
                "user_id": "two",
                "expected_revision": 1,
                "role": "manager",
            },
        ),
    )
    for action, values in invalid:
        with pytest.raises(ValidationError):
            client.project_sharing_request(action, values)
    with pytest.raises(ValueError, match="Unknown project sharing action"):
        client.project_sharing_request("raw_http", {})
    assert client.calls == []


def test_application_client_composes_project_sharing_methods() -> None:
    assert ApplicationProjectSharingMethods in ApplicationClient.__mro__
    assert callable(getattr(ApplicationClient, "project_sharing_request", None))


def test_sdk_registration_exposes_exact_inputs_annotations_and_response_modes() -> None:
    try:
        from mcp import Client
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError:
        pytest.skip("MCP SDK is not installed")

    application = Recorder()
    runtime = SimpleNamespace(require_application=lambda: application)

    def validated_call(handler, current, model, values):
        return {"result": handler(current, model.model_validate(values)).result}

    def response(envelope, mode):
        return {"mode": mode, **envelope}

    server = MCPServer("project-sharing-contract")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    register_project_sharing_tools(
        server,
        runtime,
        validated_call,
        response,
        read_only=read_only,
        write_action=write_action,
    )

    async def exercise() -> None:
        async with Client(server, mode="auto", raise_exceptions=True) as client:
            listed = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(listed) == {operation.name for operation in PROJECT_SHARING_OPERATIONS}
            for operation in PROJECT_SHARING_OPERATIONS:
                tool = listed[operation.name]
                assert tool.annotations.read_only_hint is (operation.method == "GET")
                assert "response_mode" in tool.input_schema["properties"]
                assert "idempotency_key" not in tool.input_schema["properties"]
                assert operation.model.model_json_schema()["additionalProperties"] is False
                registered = server._tool_manager.get_tool(operation.name)
                assert registered is not None
                assert registered.annotations is not None
                assert registered.annotations.idempotent_hint is (
                    None if operation.method == "GET" else True
                )

            list_tool = listed["pandrator_list_shared_projects"]
            assert list_tool.input_schema["properties"]["query"]["default"] == ""
            assert list_tool.input_schema["properties"]["limit"]["default"] == 50
            assert list_tool.input_schema["properties"]["offset"]["default"] == 0
            result = await client.call_tool(
                "pandrator_list_shared_projects",
                {"response_mode": "structured"},
            )
            assert not result.is_error
            assert result.structured_content["mode"] == "structured"
            assert result.structured_content["result"] == {"ok": True}

    asyncio.run(exercise())


@pytest.mark.parametrize("status_code", [403, 404, 409])
def test_server_errors_pass_through_transport_boundary(status_code: int) -> None:
    class ServerFailure(RuntimeError):
        def __init__(self, status: int) -> None:
            self.status_code = status

    class FailingRecorder(Recorder):
        def __init__(self, failure: Exception) -> None:
            super().__init__()
            self.failure = failure

        def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
            raise self.failure

    failure = ServerFailure(status_code)
    client = FailingRecorder(failure)
    with pytest.raises(ServerFailure) as caught:
        client.project_sharing_request(
            "get_project_viewers",
            {"project_id": "project-one"},
        )
    assert caught.value is failure
