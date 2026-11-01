"""Contract tests for the fixed MCP current-project copy adapter."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import ValidationError

from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.application_project_copy import ApplicationProjectCopyMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.project_copy_actions import PROJECT_COPY_OPERATIONS
from pandrator_mcp.schemas import PROJECT_COPY_INPUT_MODELS, TOOL_INPUT_MODELS
from pandrator_mcp.schemas.project_copy import (
    CopyCurrentProjectInput,
    GetProjectCopyInput,
    PreviewProjectCopyInput,
)
from pandrator_mcp.targets import TargetProfile, TargetRegistry
from pandrator_mcp.tools.project_copy import register_project_copy_tools


class Recorder(ApplicationProjectCopyMethods):
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append({"path": path, **kwargs})
        return {"copy_id": "copy-result"}

    def _request_binary_json(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Project copy operations use fixed JSON routes.")


class FakeResponse:
    def __init__(self, status_code: int, payload: dict[str, Any]) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.payload = json.dumps(payload).encode("utf-8")
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.payload), chunk_size):
            yield self.payload[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession:
    def __init__(self, response: FakeResponse) -> None:
        self.trust_env = True
        self.response = response
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": "GET", "url": url, **kwargs})
        return self.response

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"method": method, "url": url, **kwargs})
        return self.response


SAMPLES: dict[str, Any] = {
    "project_id": "project/one",
    "destination_workspace_id": "workspace/two",
    "expected_fingerprint": "a" * 64,
    "name": "Copy target",
    "idempotency_key": "copy-request:1",
    "copy_id": "copy/one",
}
DESTINATION_CHOICE = {
    "requirement_id": "a" * 64,
    "provider_id": "6f6e49da-e384-44cc-b1f3-8db31f388dad",
    "provider_model_id": "f4429247-bb32-42f8-b847-3a1ce24f0f31",
}
EXPECTED_OPERATIONS = {
    "preview_project_copy": (
        "previewProjectCopy",
        "POST",
        "/api/v1/projects/{project_id}/copy-preview",
        "app.write",
        RiskClass.READ,
        False,
    ),
    "copy_current_project": (
        "copyCurrentProject",
        "POST",
        "/api/v1/projects/{project_id}/copy",
        "app.run",
        RiskClass.RUN,
        True,
    ),
    "get_project_copy": (
        "getProjectCopy",
        "GET",
        "/api/v1/project-copies/{copy_id}",
        "app.read",
        RiskClass.READ,
        False,
    ),
}

_MODELS_BY_NAME = {model.__name__: model for model in PROJECT_COPY_INPUT_MODELS}


def _required_arguments(operation: Any) -> dict[str, Any]:
    model = _MODELS_BY_NAME[operation.model_name]
    return {
        name: SAMPLES[name] for name, field in model.model_fields.items() if field.is_required()
    }


@pytest.mark.parametrize("operation", PROJECT_COPY_OPERATIONS, ids=lambda item: item.action)
def test_fixed_operations_match_catalog_and_http_openapi(operation: Any) -> None:
    assert {
        item.action: (
            item.operation_id,
            item.method,
            item.path,
            item.scope,
            RiskClass(item.risk),
            item.idempotency,
        )
        for item in PROJECT_COPY_OPERATIONS
    } == EXPECTED_OPERATIONS
    assert operation.name == f"pandrator_{operation.action}"

    client = Recorder()
    arguments = _required_arguments(operation)
    if operation.action == "copy_current_project":
        arguments["name"] = SAMPLES["name"]
    client.project_copy_request(operation.action, arguments)

    action = ACTION_CATALOG.get(operation.name)
    assert action.input_model == operation.model_name
    assert action.required_scope == operation.scope
    assert action.risk == RiskClass(operation.risk)
    assert action.requires_idempotency is operation.idempotency
    assert action.method == operation.method
    assert action.downstream_operation_id == operation.operation_id
    assert action.path == operation.path.replace("{project_id}", "{projectId}").replace(
        "{copy_id}", "{copyId}"
    )
    assert action.input_model in {model.__name__ for model in TOOL_INPUT_MODELS}

    document = build_openapi_document()
    public = document["paths"][action.path][operation.method.lower()]
    assert public["operationId"] == operation.operation_id
    assert (
        public["x-required-scopes"]
        == {
            "preview_project_copy": ["app.read", "app.write"],
            "copy_current_project": ["app.read", "app.write", "app.run"],
            "get_project_copy": ["app.read"],
        }[operation.action]
    )
    parameters = {parameter["name"]: parameter for parameter in public["parameters"]}
    if operation.action == "preview_project_copy":
        assert set(parameters) == {"projectId"}
        assert public["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/ProjectCopyPreviewRequest"
        )
        legacy = document["paths"][action.path]["get"]
        assert legacy["operationId"] == "getProjectCopyPreview"
        assert {item["name"] for item in legacy["parameters"]} == {"projectId", "workspace_id"}
    elif operation.action == "copy_current_project":
        assert set(parameters) == {"projectId", "Idempotency-Key"}
        assert parameters["Idempotency-Key"]["in"] == "header"
        assert parameters["Idempotency-Key"]["required"] is True
        assert public["requestBody"]["required"] is True
        assert public["requestBody"]["content"]["application/json"]["schema"]["$ref"] == (
            "#/components/schemas/ProjectCopyRequest"
        )
        request_schema = document["components"]["schemas"]["ProjectCopyRequest"]
        assert set(request_schema["properties"]) == {
            "destination_workspace_id",
            "expected_fingerprint",
            "name",
            "destination_connections",
        }
        assert set(request_schema["required"]) == {
            "destination_workspace_id",
            "expected_fingerprint",
        }
        assert request_schema["additionalProperties"] is False
    else:
        assert set(parameters) == {"copyId"}
        assert "requestBody" not in public


def test_fixed_client_routes_quote_ids_separate_queries_and_set_post_header() -> None:
    client = Recorder()
    for operation in PROJECT_COPY_OPERATIONS:
        arguments = _required_arguments(operation)
        if operation.action == "copy_current_project":
            arguments["name"] = SAMPLES["name"]
        client.project_copy_request(operation.action, arguments)

    preview, copy, status = client.calls
    assert preview["path"] == "/api/v1/projects/project%2Fone/copy-preview"
    assert preview["method"] == "POST"
    assert preview["read_only_operation"] is True
    assert preview["parameters"] is None
    assert preview["body"] == {"destination_workspace_id": "workspace/two"}
    assert preview["idempotency_key"] is None
    assert preview["maximum_body_bytes"] == 128 * 1024

    assert copy["path"] == "/api/v1/projects/project%2Fone/copy"
    assert copy["method"] == "POST"
    assert copy["read_only_operation"] is False
    assert copy["parameters"] is None
    assert copy["body"] == {
        "destination_workspace_id": "workspace/two",
        "expected_fingerprint": "a" * 64,
        "name": "Copy target",
    }
    assert copy["idempotency_key"] == SAMPLES["idempotency_key"]
    assert "idempotency_key" not in copy["body"]
    assert copy["maximum_body_bytes"] == 128 * 1024
    assert (
        len(json.dumps(copy["body"], ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        <= 128 * 1024
    )

    assert status["path"] == "/api/v1/project-copies/copy%2Fone"
    assert status["method"] == "GET"
    assert status["parameters"] is None
    assert status["body"] is None
    assert status["idempotency_key"] is None

    with pytest.raises(ValueError, match="Unknown project copy action"):
        client.project_copy_request("raw_http", {})
    with pytest.raises(ValueError, match="Unknown project copy action"):
        client.project_copy_request([], {})  # type: ignore[arg-type]
    assert len(client.calls) == 3


def test_post_sends_idempotency_in_header_and_only_bounded_domain_fields_in_json() -> None:
    response = FakeResponse(202, {"copy_id": "copy-result"})
    client, session = _application_client(response)
    result = client.project_copy_request(
        "copy_current_project",
        {
            "project_id": "project-one",
            "destination_workspace_id": "workspace-one",
            "expected_fingerprint": "a" * 64,
            "name": "Copy target",
            "idempotency_key": "copy-key-123",
        },
    )

    assert result == {"copy_id": "copy-result"}
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["params"] is None
    assert call["headers"]["Idempotency-Key"] == "copy-key-123"
    assert call["headers"]["Content-Type"] == "application/json"
    assert json.loads(call["data"]) == {
        "destination_workspace_id": "workspace-one",
        "expected_fingerprint": "a" * 64,
        "name": "Copy target",
    }
    assert response.closed is True


def test_inputs_are_strict_bounded_and_registered() -> None:
    assert all(model in TOOL_INPUT_MODELS for model in PROJECT_COPY_INPUT_MODELS)
    assert PreviewProjectCopyInput.model_validate(
        {"project_id": "project-one", "destination_workspace_id": "workspace-one"}
    )
    assert (
        CopyCurrentProjectInput.model_validate(
            {
                "project_id": "project-one",
                "destination_workspace_id": "workspace-one",
                "expected_fingerprint": "a" * 64,
                "idempotency_key": "copy-key-123",
            }
        ).name
        is None
    )
    assert GetProjectCopyInput.model_validate({"copy_id": "copy-one"})

    fields_by_model = {
        PreviewProjectCopyInput: {"project_id", "destination_workspace_id", "destination_connections"},
        CopyCurrentProjectInput: {
            "project_id",
            "destination_workspace_id",
            "destination_connections",
            "expected_fingerprint",
            "name",
            "idempotency_key",
        },
        GetProjectCopyInput: {"copy_id"},
    }
    for model, expected_fields in fields_by_model.items():
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert set(schema["properties"]) == expected_fields

    invalid = (
        ("preview_project_copy", {"project_id": "", "destination_workspace_id": "w"}),
        ("preview_project_copy", {"project_id": "p" * 37, "destination_workspace_id": "w"}),
        ("preview_project_copy", {"project_id": True, "destination_workspace_id": "w"}),
        ("preview_project_copy", {"project_id": "p", "destination_workspace_id": "w" * 37}),
        (
            "preview_project_copy",
            {"project_id": "p", "destination_workspace_id": "w", "url": "https://example.invalid"},
        ),
        (
            "preview_project_copy",
            {"project_id": "p", "destination_workspace_id": "w", "actor_id": "user"},
        ),
        (
            "preview_project_copy",
            {"project_id": "p", "destination_workspace_id": "w", "snapshot": {}},
        ),
        (
            "copy_current_project",
            {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "expected_fingerprint": "A" * 64},
        ),
        (
            "copy_current_project",
            {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "expected_fingerprint": "a" * 63},
        ),
        (
            "copy_current_project",
            {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "expected_fingerprint": True},
        ),
        (
            "copy_current_project",
            {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "idempotency_key": "short"},
        ),
        (
            "copy_current_project",
            {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "idempotency_key": "slash/key-123"},
        ),
        ("copy_current_project", {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "name": ""}),
        (
            "copy_current_project",
            {**_required_arguments(PROJECT_COPY_OPERATIONS[1]), "name": "n" * 256},
        ),
        (
            "copy_current_project",
            {
                **_required_arguments(PROJECT_COPY_OPERATIONS[1]),
                "idempotency_key": "copy-key-123",
                "job_id": "job",
            },
        ),
        ("get_project_copy", {"copy_id": "c" * 37}),
        ("get_project_copy", {"copy_id": "copy-one", "filesystem_path": "/private"}),
    )
    client = Recorder()
    for action, values in invalid:
        with pytest.raises(ValidationError):
            client.project_copy_request(action, values)
    assert client.calls == []


def test_application_client_composes_project_copy_methods() -> None:
    assert ApplicationProjectCopyMethods in ApplicationClient.__mro__
    assert callable(getattr(ApplicationClient, "project_copy_request", None))


def test_preview_and_copy_forward_the_same_strict_destination_choices() -> None:
    client = Recorder()
    for operation in PROJECT_COPY_OPERATIONS[:2]:
        arguments = _required_arguments(operation)
        arguments["destination_connections"] = [DESTINATION_CHOICE]
        client.project_copy_request(operation.action, arguments)
    preview, copy = client.calls
    assert preview["read_only_operation"] is True and preview["method"] == "POST"
    assert preview["idempotency_key"] is None
    assert copy["read_only_operation"] is False
    assert preview["body"]["destination_connections"] == [DESTINATION_CHOICE]
    assert copy["body"]["destination_connections"] == [DESTINATION_CHOICE]
    assert set(preview["body"]) == {"destination_workspace_id", "destination_connections"}
    assert set(copy["body"]) == {
        "destination_workspace_id", "expected_fingerprint", "name", "destination_connections",
    }


@pytest.mark.parametrize(("field", "value"), [
    ("provider_id", "https://private.invalid"),
    ("requirement_id", "not-a-requirement"),
    ("provider_model_id", "../model"),
    ("secret_ref", "private"),
])
def test_destination_choice_schema_matches_http_and_rejects_before_transport(field, value) -> None:
    from pandrator.web.project_copy_schemas import ProjectCopyPreviewRequest

    client = Recorder()
    arguments = {
        "project_id": "project-one",
        "destination_workspace_id": "workspace-one",
        "destination_connections": [{**DESTINATION_CHOICE, field: value}],
    }
    with pytest.raises(ValidationError):
        client.project_copy_request("preview_project_copy", arguments)
    with pytest.raises(ValidationError):
        ProjectCopyPreviewRequest.model_validate({
            key: value for key, value in arguments.items() if key != "project_id"
        })
    assert client.calls == []


def test_destination_choices_have_a_bounded_count_before_transport() -> None:
    client = Recorder()
    with pytest.raises(ValidationError):
        client.project_copy_request(
            "preview_project_copy",
            {"project_id": "project-one", "destination_workspace_id": "workspace-one",
             "destination_connections": [DESTINATION_CHOICE] * 513},
        )
    assert client.calls == []


def test_sdk_registration_uses_parent_metadata_signatures_and_both_response_modes() -> None:
    try:
        from mcp import Client
        from mcp.server import MCPServer
        from mcp.types import ToolAnnotations
    except ImportError:
        pytest.skip("MCP SDK is not installed")

    application = Recorder()
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))

    def validated_call(handler, current, model, values):
        return {"result": handler(current, model.model_validate(values)).result}

    def response(envelope, mode):
        return {"mode": mode, **envelope}

    server = MCPServer("project-copy-contract")
    read_only = ToolAnnotations(read_only_hint=True, open_world_hint=False)
    write_action = ToolAnnotations(
        read_only_hint=False,
        destructive_hint=False,
        idempotent_hint=True,
        open_world_hint=False,
    )
    register_project_copy_tools(
        server,
        runtime,
        validated_call,
        response,
        read_only=read_only,
        write_action=write_action,
    )

    async def exercise() -> None:
        async with Client(server, mode="auto", raise_exceptions=True) as client:
            tools = {tool.name: tool for tool in (await client.list_tools()).tools}
            assert set(tools) == {operation.name for operation in PROJECT_COPY_OPERATIONS}
            for operation in PROJECT_COPY_OPERATIONS:
                model = _MODELS_BY_NAME[operation.model_name]
                tool = tools[operation.name]
                assert tool.title == operation.title
                assert tool.description == operation.description
                assert tool.annotations is not None
                assert tool.annotations.read_only_hint is (operation.risk == "read")
                assert tool.annotations.open_world_hint is False
                assert set(tool.input_schema["properties"]) == {
                    *model.model_fields,
                    "response_mode",
                }
                assert model.model_json_schema()["additionalProperties"] is False
                assert ("idempotency_key" in tool.input_schema["properties"]) is (
                    operation.action == "copy_current_project"
                )
                registered = server._tool_manager.get_tool(operation.name)
                assert registered is not None
                assert registered.annotations is not None
                assert registered.annotations.idempotent_hint is (
                    None if operation.risk == "read" else True
                )
                if operation.risk != "read":
                    assert registered.annotations.destructive_hint is False

            preview_result = await client.call_tool(
                "pandrator_preview_project_copy",
                {
                    "project_id": "project-one",
                    "destination_workspace_id": "workspace-one",
                    "response_mode": "standard",
                },
            )
            assert not preview_result.is_error
            assert preview_result.structured_content["mode"] == "standard"

            copy_result = await client.call_tool(
                "pandrator_copy_current_project",
                {
                    "project_id": "project-one",
                    "destination_workspace_id": "workspace-one",
                    "expected_fingerprint": "a" * 64,
                    "idempotency_key": "copy-key-123",
                    "response_mode": "structured",
                },
            )
            assert not copy_result.is_error
            assert copy_result.structured_content["mode"] == "structured"
            assert copy_result.structured_content["result"] == {"copy_id": "copy-result"}

    asyncio.run(exercise())
    assert application.calls[0]["parameters"] is None
    assert application.calls[0]["body"] == {"destination_workspace_id": "workspace-one"}
    assert application.calls[1]["idempotency_key"] == "copy-key-123"


def _application_client(response: FakeResponse) -> tuple[ApplicationClient, FakeSession]:
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace="C:/Pandrator",
    )
    registry = TargetRegistry(
        [profile],
        network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
        local_discovery=lambda _profile: ("http://127.0.0.1:8097", "manager-id"),
    )
    session = FakeSession(response)
    client = ApplicationClient(
        registry.bind("local"),
        CredentialResolver(()),
        session=cast(Any, session),
        local_bootstrap=lambda _target, _session: "csrf-token",
    )
    return client, session


@pytest.mark.parametrize(
    ("status_code", "expected_code"),
    ((403, "scope_denied"), (404, "not_found"), (409, "revision_conflict")),
)
def test_downstream_errors_never_become_stale_success_or_expose_details(
    status_code: int,
    expected_code: str,
) -> None:
    private_value = "secret-backend-detail"
    response = FakeResponse(
        status_code,
        {
            "error": {
                "code": "unrecognized_copy_error",
                "message": private_value,
                "details": {"token": private_value, "private_path": private_value},
            }
        },
    )
    client, _session = _application_client(response)
    with pytest.raises(PandratorMcpError) as failure:
        client.project_copy_request("get_project_copy", {"copy_id": "copy-one"})

    assert failure.value.code == expected_code
    assert private_value not in str(failure.value)
    assert private_value not in repr(failure.value.details)
    assert response.closed is True
