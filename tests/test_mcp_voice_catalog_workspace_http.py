"""Modern MCP library adapters through real authenticated workspace handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from pandrator.web import models as m
from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.voice_lifecycle import (
    CreateVoiceCollectionInput,
    ListVoiceCollectionsInput,
    UpdateCatalogVoiceMetadataInput,
    UpdateVoiceCollectionInput,
    VoiceCatalogCapabilitiesInput,
    VoiceCatalogInput,
)
from pandrator_mcp.tools.voice_lifecycle import (
    create_voice_collection,
    list_voice_collections,
    update_catalog_voice_metadata,
    update_voice_collection,
    voice_catalog,
    voice_catalog_capabilities,
)
from tests.test_speech_catalogue_workspace_access import (
    case as case,  # noqa: F401
)
from tests.test_speech_catalogue_workspace_access import (
    catalogue as catalogue,  # noqa: F401
)
from tests.test_speech_catalogue_workspace_access import (
    connection,
    save,
    team,
)
from tests.test_speech_catalogue_workspace_access import (
    local_only as local_only,  # noqa: F401
)
from tests.test_voice_list_workspace_access import create_team_session, seed_voice


class CatalogueHttpClient(ApplicationVoiceMethods):
    """Keep the real URL/body builders; replace only their HTTP transport."""

    def __init__(self, client: Any, csrf: dict[str, str]):
        self.client, self.csrf = client, csrf
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.last_status = 0

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        method = options.get("method", "GET")
        headers = dict(self.csrf)
        if options.get("idempotency_key"):
            headers["Idempotency-Key"] = options["idempotency_key"]
        self.calls.append((path, method, options))
        response = self.client.open(
            path,
            method=method,
            query_string=options.get("parameters"),
            json=options.get("body"),
            headers=headers,
        )
        self.last_status = response.status_code
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", "Disposable HTTP request denied.")
        assert len(response.data) <= options.get("maximum_body_bytes", 512 * 1024)
        result = response.get_json()
        assert isinstance(result, dict)
        return result

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        raise AssertionError("Catalogue operations must not retrieve binary content.")


def _runtime(application: CatalogueHttpClient) -> McpRuntime:
    return cast(McpRuntime, SimpleNamespace(require_application=lambda: application))


def test_mcp_real_http_library_roots_and_capabilities(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    own = save(members.alice, members.ah, records=[connection("ALICE")])
    save(members.owner, members.oh, workspace=members.workspace_id, records=[connection("TEAM")])
    seed_voice(case, own["workspace_id"], "Alice private reference")
    seed_voice(case, members.workspace_id, "Team reference")
    project = create_team_session(members, key="modern-library-project")
    application = CatalogueHttpClient(members.alice, members.ah)
    runtime = _runtime(application)
    personal = voice_catalog(runtime, VoiceCatalogInput(kind="managed", view="compact"))
    assert personal["workspace_id"] == own["workspace_id"] and personal["can_edit"]
    assert [row["name"] for row in personal["items"]] == ["Alice private reference"]
    shared = voice_catalog(
        runtime, VoiceCatalogInput(session_id=project["id"], kind="managed", view="compact")
    )
    assert shared["workspace_id"] == members.workspace_id and shared["can_edit"]
    assert [row["name"] for row in shared["items"]] == ["Team reference"]
    assert application.calls[-1][2]["parameters"]["session_id"] == project["id"]
    assert "workspace_id" not in application.calls[-1][2]["parameters"]
    capability = voice_catalog_capabilities(
        runtime, VoiceCatalogCapabilitiesInput(session_id=project["id"])
    )
    assert capability["workspace_id"] == members.workspace_id and capability["can_edit"]
    assert [row["service_id"] for row in capability["models"]] == ["openai"]
    collections = list_voice_collections(
        runtime, ListVoiceCollectionsInput(workspace_id=members.workspace_id)
    )
    assert collections["workspace_id"] == members.workspace_id and collections["items"] == []
    with pytest.raises(PandratorMcpError):
        voice_catalog(
            runtime,
            VoiceCatalogInput(workspace_id=own["workspace_id"], session_id=project["id"]),
        )
    assert application.last_status == 404 and not catalogue.calls


def test_mcp_real_http_member_collection_annotation_and_revoked_retry(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    save(members.owner, members.oh, workspace=members.workspace_id, records=[connection("TEAM")])
    application = CatalogueHttpClient(members.alice, members.ah)
    runtime = _runtime(application)
    query = VoiceCatalogInput(workspace_id=members.workspace_id, kind="provider")
    choice = voice_catalog(runtime, query)["items"][0]
    create = CreateVoiceCollectionInput(
        workspace_id=members.workspace_id, name="Team narrators", idempotency_key="team-narrators-mcp"
    )
    collection = create_voice_collection(runtime, create)
    assert collection["workspace_id"] == members.workspace_id and collection["can_edit"]
    assert create_voice_collection(runtime, create) == collection
    updated = update_voice_collection(
        runtime,
        UpdateVoiceCollectionInput(
            workspace_id=members.workspace_id,
            collection_id=collection["id"],
            expected_revision=collection["revision"],
            add_members=[choice["reference"]],
            idempotency_key="team-narrators-add-mcp",
        ),
    )
    assert updated["member_count"] == 1 and updated["workspace_id"] == members.workspace_id
    metadata = update_catalog_voice_metadata(
        runtime,
        UpdateCatalogVoiceMetadataInput.model_validate({
            "workspace_id": members.workspace_id,
            "reference": choice["reference"],
            "expected_revision": choice["revision"],
            "changes": {"name": "Team lead narrator"},
            "idempotency_key": "team-narrator-profile-mcp",
        }),
    )
    assert metadata["name"] == "Team lead narrator" and metadata["can_edit"]
    assert voice_catalog(runtime, query)["items"][0]["name"] == "Team lead narrator"
    assert len(list_voice_collections(runtime, ListVoiceCollectionsInput())["items"]) == 0
    with case.database.immediate_session() as session:
        row = session.get_one(m.WorkspaceMembership, (members.workspace_id, members.alice_id))
        session.delete(row)
    with pytest.raises(PandratorMcpError):
        create_voice_collection(runtime, create)
    assert application.last_status == 404
    assert not catalogue.calls


def test_modern_library_openapi_scope_and_write_contracts():
    paths = build_openapi_document()["paths"]
    for path, method in (
        ("/api/v1/voice-catalog", "get"),
        ("/api/v1/voice-catalog/capabilities", "get"),
        ("/api/v1/voice-collections", "get"),
        ("/api/v1/voice-collections", "post"),
        ("/api/v1/voice-collections/{collectionId}", "patch"),
        ("/api/v1/voice-catalog/metadata", "patch"),
    ):
        operation = paths[path][method]
        parameters = {row["name"]: row for row in operation["parameters"]}
        for name in (("workspace_id", "session_id") if method == "get" else ("workspace_id",)):
            schema = parameters[name]["schema"]
            if "anyOf" in schema:
                schema = next(branch for branch in schema["anyOf"] if branch.get("type") == "string")
            assert schema["minLength"] == 1 and schema["maxLength"] == 80
        assert {"nativeOAuth": ["app.read"] if method == "get" else ["app.read", "app.write"]} in operation["security"]
        if method != "get":
            assert "session_id" not in parameters and "Idempotency-Key" in parameters
        else:
            assert "requestBody" not in operation


def test_mcp_project_filters_accept_the_saved_provider_alias(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    save(
        members.owner,
        members.oh,
        workspace=members.workspace_id,
        records=[connection("TEAM", identifier="team_approved_tts")],
    )
    project = create_team_session(members, key="alias-library-project")
    application = CatalogueHttpClient(members.alice, members.ah)
    runtime = _runtime(application)
    current = voice_catalog(runtime, VoiceCatalogInput(session_id=project["id"], kind="provider"))
    filtered = voice_catalog(
        runtime,
        VoiceCatalogInput(
            session_id=project["id"], kind="provider", service_id="team_approved_tts"
        ),
    )
    assert current["total"] and filtered["total"] == current["total"]
    assert filtered["items"][0]["key"] == current["items"][0]["key"]
    capabilities = voice_catalog_capabilities(
        runtime,
        VoiceCatalogCapabilitiesInput(session_id=project["id"], service_id="team_approved_tts"),
    )
    assert capabilities["models"]
    assert not catalogue.calls
