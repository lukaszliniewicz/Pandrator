"""Workspace roots for the existing MCP voice catalogue and collection tools."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.schemas.voice_lifecycle import (
    CreateVoiceCollectionInput,
    ListVoiceCollectionsInput,
    PromoteVoiceDesignInput,
    UpdateCatalogVoiceMetadataInput,
    UpdateVoiceCollectionInput,
    VoiceCatalogCapabilitiesInput,
    VoiceCatalogInput,
    _RevisionWriteInput,
)
from pandrator_mcp.server import build_server
from pandrator_mcp.tools.voice_lifecycle import (
    create_voice_collection,
    list_voice_collections,
    update_catalog_voice_metadata,
    update_voice_collection,
    voice_catalog,
    voice_catalog_capabilities,
)


class RecordingApplication(ApplicationVoiceMethods):
    """Exercise real request builders while recording only their HTTP boundary."""

    def __init__(self, *responses: dict[str, Any]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((path, kwargs))
        return self.responses.pop(0) if self.responses else {}

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Unexpected binary request: {path}")


def _runtime(application: Any) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


def _scope_input(model: type, **scope: Any) -> Any:
    values: dict[str, Any] = {
        VoiceCatalogInput: {},
        VoiceCatalogCapabilitiesInput: {},
        ListVoiceCollectionsInput: {},
        CreateVoiceCollectionInput: {
            "name": "Narrators",
            "idempotency_key": "collection:create:1",
        },
        UpdateVoiceCollectionInput: {
            "collection_id": "collection-1",
            "expected_revision": 3,
            "idempotency_key": "collection:update:1",
        },
        UpdateCatalogVoiceMetadataInput: {
            "reference": {
                "kind": "provider",
                "service_id": "service-1",
                "model": "model-1",
                "voice": "voice-1",
            },
            "expected_revision": 0,
            "changes": {"name": "Narrator"},
            "idempotency_key": "catalog:metadata:1",
        },
        PromoteVoiceDesignInput: {
            "voice_id": "voice-1",
            "artifact_id": "artifact-1",
            "transcript": "Hello",
            "expected_voice_revision": 1,
            "idempotency_key": "voice:promote:1",
        },
    }[model]
    return model.model_validate(values | scope)


_GET_INPUTS = (
    VoiceCatalogInput,
    VoiceCatalogCapabilitiesInput,
    ListVoiceCollectionsInput,
)
_WRITE_INPUTS = (
    CreateVoiceCollectionInput,
    UpdateVoiceCollectionInput,
    UpdateCatalogVoiceMetadataInput,
    PromoteVoiceDesignInput,
)


@pytest.mark.parametrize("model", (*_GET_INPUTS, *_WRITE_INPUTS))
def test_scope_roots_are_optional_trimmed_and_strict(model: type):
    default = _scope_input(model)
    assert default.workspace_id is None
    if model in _GET_INPUTS:
        assert default.session_id is None

    scoped = _scope_input(model, workspace_id="  workspace-1  ")
    assert scoped.workspace_id == "workspace-1"
    if model in _GET_INPUTS:
        scoped = _scope_input(
            model,
            workspace_id="  workspace-1  ",
            session_id="  session-1  ",
        )
        assert scoped.session_id == "session-1"
    else:
        assert "session_id" not in model.model_fields
        with pytest.raises(ValidationError):
            _scope_input(model, session_id="session-1")

    for field in ("workspace_id", "session_id") if model in _GET_INPUTS else ("workspace_id",):
        for invalid in ("", "   ", True, 7, "x" * 81):
            with pytest.raises(ValidationError):
                _scope_input(model, **{field: invalid})


def test_scope_roots_do_not_spread_to_the_shared_revision_write_base():
    # Concrete revision writes opt into a workspace root individually. The
    # shared base must stay scope-free so new write tools cannot inherit it.
    assert set(_RevisionWriteInput.model_fields) == {"idempotency_key"}


def test_real_request_builders_forward_roots_only_as_fixed_query_parameters():
    application = RecordingApplication()

    application.voice_catalog(workspace_id="workspace-1", session_id="session-1")
    application.voice_catalog_capabilities(
        service_id="service-1",
        model="model-1",
        workspace_id="workspace-1",
        session_id="session-1",
    )
    application.list_voice_collections(
        workspace_id="workspace-1",
        session_id="session-1",
    )
    application.create_voice_collection(
        name="Narrators",
        description="Story voices",
        idempotency_key="collection:create:1",
        workspace_id="workspace-1",
    )
    application.update_voice_collection(
        "collection/1",
        expected_revision=7,
        name="Narrator",
        add_members=[{"kind": "managed", "voice_id": "voice-1"}],
        idempotency_key="collection:update:1",
        workspace_id="workspace-1",
    )
    application.update_catalog_voice_metadata(
        reference={
            "kind": "provider",
            "service_id": "service-1",
            "model": "model-1",
            "voice": "voice-1",
        },
        expected_revision=9,
        changes={"name": "Narrator"},
        idempotency_key="catalog:metadata:1",
        workspace_id="workspace-1",
    )

    assert application.calls == [
        (
            "/api/v1/voice-catalog",
            {
                "parameters": {
                    "workspace_id": "workspace-1",
                    "session_id": "session-1",
                    "limit": 30,
                }
            },
        ),
        (
            "/api/v1/voice-catalog/capabilities",
            {
                "parameters": {
                    "service_id": "service-1",
                    "model": "model-1",
                    "workspace_id": "workspace-1",
                    "session_id": "session-1",
                }
            },
        ),
        (
            "/api/v1/voice-collections",
            {"parameters": {"workspace_id": "workspace-1", "session_id": "session-1"}},
        ),
        (
            "/api/v1/voice-collections",
            {
                "method": "POST",
                "body": {"name": "Narrators", "description": "Story voices"},
                "idempotency_key": "collection:create:1",
                "maximum_body_bytes": 32 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/voice-collections/collection%2F1",
            {
                "method": "PATCH",
                "body": {
                    "expected_revision": 7,
                    "name": "Narrator",
                    "add_members": [{"kind": "managed", "voice_id": "voice-1"}],
                },
                "idempotency_key": "collection:update:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
        (
            "/api/v1/voice-catalog/metadata",
            {
                "method": "PATCH",
                "body": {
                    "reference": {
                        "kind": "provider",
                        "service_id": "service-1",
                        "model": "model-1",
                        "voice": "voice-1",
                    },
                    "expected_revision": 9,
                    "changes": {"name": "Narrator"},
                },
                "idempotency_key": "catalog:metadata:1",
                "maximum_body_bytes": 64 * 1024,
                "parameters": {"workspace_id": "workspace-1"},
            },
        ),
    ]
    for _path, kwargs in application.calls[3:]:
        assert "workspace_id" not in kwargs["body"]
        assert "session_id" not in kwargs["body"]


def test_legacy_request_builder_calls_omit_unsupplied_scope_roots():
    application = RecordingApplication()
    application.voice_catalog()
    application.voice_catalog_capabilities()
    application.list_voice_collections()
    application.create_voice_collection(
        name="Narrators",
        description=None,
        idempotency_key="collection:create:1",
    )
    application.update_voice_collection(
        "collection-1",
        expected_revision=3,
        idempotency_key="collection:update:1",
    )
    application.update_catalog_voice_metadata(
        reference={
            "kind": "provider",
            "service_id": "service-1",
            "model": "model-1",
            "voice": "voice-1",
        },
        expected_revision=3,
        changes={"name": "Narrator"},
        idempotency_key="catalog:metadata:1",
    )

    catalog_parameters = application.calls[0][1]["parameters"]
    assert "workspace_id" not in catalog_parameters
    assert "session_id" not in catalog_parameters
    assert application.calls[1][1]["parameters"] is None
    assert application.calls[2][1] == {}
    for _path, kwargs in application.calls[3:]:
        assert "parameters" not in kwargs
        assert "workspace_id" not in kwargs["body"]
        assert "session_id" not in kwargs["body"]

    with pytest.raises(ValueError):
        application.voice_catalog(unapproved_filter="value")


def _scoped_payload(**values: Any) -> dict[str, Any]:
    return {
        "workspace_id": "workspace-1",
        "can_edit": True,
        "secret_sentinel": "do-not-return-this-secret",
        **values,
    }


def _assert_safe_scope(result: dict[str, Any]) -> None:
    assert result["workspace_id"] == "workspace-1"
    assert result["can_edit"] is True
    assert "secret_sentinel" not in str(result)
    assert "provider_token" not in str(result)


def test_handlers_forward_roots_and_keep_safe_scope_at_the_top_level():
    application = Mock()
    application.voice_catalog.side_effect = [
        _scoped_payload(
            schema_version="1",
            total=1,
            items=[{"id": "voice-1", "name": "Narrator", "provider_token": "secret"}],
        ),
        _scoped_payload(
            schema_version="1",
            total=1,
            items=[{"id": "voice-1", "name": "Narrator", "provider_token": "secret"}],
        ),
    ]
    application.voice_catalog_capabilities.return_value = _scoped_payload(
        models=[{"service_id": "service-1", "model": "model-1", "available": True}]
    )
    application.list_voice_collections.return_value = _scoped_payload(
        items=[{"id": "collection-1", "name": "Narrators", "api_key": "secret"}]
    )
    application.create_voice_collection.return_value = _scoped_payload(
        id="collection-2", name="Created"
    )
    application.update_voice_collection.return_value = _scoped_payload(
        id="collection-1", name="Updated"
    )
    application.update_catalog_voice_metadata.return_value = _scoped_payload(
        id="provider:voice-1", name="Updated voice"
    )
    runtime = _runtime(application)

    compact = voice_catalog(
        runtime,
        VoiceCatalogInput(
            view="compact", workspace_id="workspace-1", session_id="session-1"
        ),
    )
    full = voice_catalog(
        runtime,
        VoiceCatalogInput(
            view="full", workspace_id="workspace-1", session_id="session-1"
        ),
    )
    capabilities = voice_catalog_capabilities(
        runtime,
        VoiceCatalogCapabilitiesInput(
            service_id="service-1",
            model="model-1",
            workspace_id="workspace-1",
            session_id="session-1",
        ),
    )
    collections = list_voice_collections(
        runtime,
        ListVoiceCollectionsInput(workspace_id="workspace-1", session_id="session-1"),
    )
    created = create_voice_collection(
        runtime,
        CreateVoiceCollectionInput(
            name="Created",
            idempotency_key="collection:create:1",
            workspace_id="workspace-1",
        ),
    )
    updated = update_voice_collection(
        runtime,
        UpdateVoiceCollectionInput(
            collection_id="collection-1",
            expected_revision=3,
            idempotency_key="collection:update:1",
            workspace_id="workspace-1",
        ),
    )
    metadata = update_catalog_voice_metadata(
        runtime,
        UpdateCatalogVoiceMetadataInput.model_validate(
            {
                "reference": {
                    "kind": "provider",
                    "service_id": "service-1",
                    "model": "model-1",
                    "voice": "voice-1",
                },
                "expected_revision": 3,
                "changes": {"name": "Updated voice"},
                "idempotency_key": "catalog:metadata:1",
                "workspace_id": "workspace-1",
            }
        ),
    )

    for result in (compact, full, capabilities, collections, created, updated, metadata):
        _assert_safe_scope(result)
    assert application.voice_catalog.call_args_list[0].kwargs["workspace_id"] == "workspace-1"
    assert application.voice_catalog.call_args_list[0].kwargs["session_id"] == "session-1"
    assert application.voice_catalog_capabilities.call_args.kwargs == {
        "service_id": "service-1",
        "model": "model-1",
        "workspace_id": "workspace-1",
        "session_id": "session-1",
    }
    assert application.list_voice_collections.call_args.kwargs == {
        "workspace_id": "workspace-1",
        "session_id": "session-1",
    }
    assert application.create_voice_collection.call_args.kwargs["workspace_id"] == "workspace-1"
    assert application.update_voice_collection.call_args.kwargs["workspace_id"] == "workspace-1"
    assert application.update_catalog_voice_metadata.call_args.kwargs["workspace_id"] == "workspace-1"


def test_handlers_omit_malformed_scope_metadata_and_support_legacy_responses():
    malformed = {
        "workspace_id": 7,
        "can_edit": "true",
        "secret_sentinel": "do-not-return-this-secret",
        "items": [],
        "models": [],
    }
    application = Mock()
    for name in (
        "voice_catalog",
        "voice_catalog_capabilities",
        "list_voice_collections",
        "create_voice_collection",
        "update_voice_collection",
        "update_catalog_voice_metadata",
    ):
        getattr(application, name).return_value = malformed
    runtime = _runtime(application)

    results = [
        voice_catalog(runtime, VoiceCatalogInput()),
        voice_catalog_capabilities(runtime, VoiceCatalogCapabilitiesInput()),
        list_voice_collections(runtime, ListVoiceCollectionsInput()),
        create_voice_collection(
            runtime,
            CreateVoiceCollectionInput(name="New", idempotency_key="collection:create:2"),
        ),
        update_voice_collection(
            runtime,
            UpdateVoiceCollectionInput(
                collection_id="collection-1",
                expected_revision=3,
                idempotency_key="collection:update:2",
            ),
        ),
        update_catalog_voice_metadata(
            runtime,
            UpdateCatalogVoiceMetadataInput.model_validate(
                {
                    "reference": {
                        "kind": "provider",
                        "service_id": "service-1",
                        "model": "model-1",
                        "voice": "voice-1",
                    },
                    "expected_revision": 3,
                    "changes": {"name": "Updated"},
                    "idempotency_key": "catalog:metadata:2",
                }
            ),
        ),
    ]
    for result in results:
        assert "workspace_id" not in result
        assert "can_edit" not in result
        assert "secret_sentinel" not in str(result)

    application.voice_catalog.return_value = {"items": []}
    legacy = voice_catalog(runtime, VoiceCatalogInput())
    assert "workspace_id" not in legacy
    assert "can_edit" not in legacy


def _registered_tools():
    server = build_server(cast(McpRuntime, SimpleNamespace()))

    async def list_tools():
        async with Client(server, mode="auto", raise_exceptions=False) as client:
            return await client.list_tools()

    return asyncio.run(list_tools()).tools


def _string_schema_branch(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "string":
        return schema
    return next(branch for branch in schema["anyOf"] if branch.get("type") == "string")


def test_registered_voice_tool_schemas_expose_only_the_allowed_scope_roots():
    tools = {tool.name: tool for tool in _registered_tools()}
    expected = {
        "pandrator_get_voice_catalog": {"workspace_id", "session_id"},
        "pandrator_get_voice_capabilities": {"workspace_id", "session_id"},
        "pandrator_list_voice_collections": {"workspace_id", "session_id"},
        "pandrator_create_voice_collection": {"workspace_id"},
        "pandrator_update_voice_collection": {"workspace_id"},
        "pandrator_update_catalog_voice_metadata": {"workspace_id"},
    }
    for name, allowed_roots in expected.items():
        tool = tools[name]
        properties = tool.input_schema["properties"]
        assert {root for root in ("workspace_id", "session_id") if root in properties} == (
            allowed_roots
        )
        for root in allowed_roots:
            root_schema = _string_schema_branch(properties[root])
            assert root_schema["minLength"] == 1
            assert root_schema["maxLength"] == 80
        assert tool.annotations is not None
        assert tool.annotations.read_only_hint is (name in {
            "pandrator_get_voice_catalog",
            "pandrator_get_voice_capabilities",
            "pandrator_list_voice_collections",
        })
