"""MCP catalogue builders through actual authenticated workspace HTTP handlers."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast

import pytest

from pandrator.web.openapi import build_openapi_document
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.e2e import TtsCatalogInput, TtsCatalogRefreshInput
from pandrator_mcp.tools.e2e import refresh_tts_catalog, tts_catalog
from tests.test_speech_catalogue_workspace_access import (
    NATIVE_MODEL,
    change_role,
    connection,
    save,
    team,
)
from tests.test_speech_catalogue_workspace_access import (
    case as case,  # noqa: F401
)
from tests.test_speech_catalogue_workspace_access import (
    catalogue as catalogue,  # noqa: F401
)
from tests.test_speech_catalogue_workspace_access import (
    local_only as local_only,  # noqa: F401
)
from tests.test_voice_list_workspace_access import (
    create_team_session,
    personal_workspace_id,
    seed_voice,
)


class WorkspaceHttpClient(ApplicationVoiceMethods):
    def __init__(self, client: Any, csrf: dict[str, str]):
        self.client, self.csrf = client, csrf
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self.last_status = 0

    def _request_json(self, path: str, **options: Any) -> dict[str, Any]:
        method = options.get("method", "GET")
        self.calls.append((path, method, options))
        response = self.client.open(
            path, method=method, query_string=options.get("parameters"), headers=self.csrf
        )
        self.last_status = response.status_code
        if response.status_code >= 400:
            raise PandratorMcpError("scope_denied", "Synthetic HTTP request denied.")
        assert len(response.data) <= options.get("maximum_body_bytes", 512 * 1024)
        result = response.get_json()
        assert isinstance(result, dict)
        return result

    def _request_binary_json(self, path: str, **options: Any) -> dict[str, Any]:
        raise AssertionError("Catalogue inspection must not retrieve binary content.")


def test_actual_mcp_http_namespace_and_refresh_authority(case, catalogue, monkeypatch):
    save(
        case.client,
        {"X-CSRF-Token": case.csrf},
        records=[{**connection("INITIAL"), "models": ["initial-only-model"]}],
    )
    members = team(case, monkeypatch)
    own = save(
        members.alice, members.ah, records=[{**connection("ALICE"), "models": ["alice-only-model"]}]
    )
    save(
        members.owner,
        members.oh,
        workspace=members.workspace_id,
        records=[{**connection("TEAM"), "models": ["team-only-model"]}],
    )
    seed_voice(case, own["workspace_id"], "Alice reference")
    seed_voice(case, members.workspace_id, "Team reference")
    seed_voice(
        case,
        personal_workspace_id(case, case.auth.initial_account().user_id),
        "Owner private reference",
    )

    application = WorkspaceHttpClient(members.alice, members.ah)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    result = tts_catalog(runtime, TtsCatalogInput(workspace_id=members.workspace_id))
    assert result["workspace_id"] == members.workspace_id and not result["can_manage"]
    assert "team-only-model" in result["services"][0]["models"]
    assert "alice-only-model" not in str(result) and "initial-only-model" not in str(result)
    assert [row["name"] for row in result["managed_voices"]] == ["Team reference"]
    assert "Alice reference" not in str(result) and "Owner private reference" not in str(result)
    assert application.calls[-1][2]["parameters"] == {"workspace_id": members.workspace_id}
    assert not catalogue.calls

    personal = tts_catalog(runtime, TtsCatalogInput(service_id="openai"))
    assert personal["workspace_id"] == own["workspace_id"] and personal["can_manage"]
    assert "alice-only-model" in personal["services"][0]["models"]
    assert "team-only-model" not in str(personal) and "initial-only-model" not in str(personal)
    assert [row["name"] for row in personal["managed_voices"]] == ["Alice reference"]
    with pytest.raises(PandratorMcpError):
        refresh_tts_catalog(runtime, TtsCatalogRefreshInput(workspace_id=members.workspace_id))
    assert application.last_status == 404 and not catalogue.calls
    change_role(case, members, "admin")
    refreshed = refresh_tts_catalog(
        runtime, TtsCatalogRefreshInput(workspace_id=members.workspace_id, service_id="openai")
    )
    assert refreshed["workspace_id"] == members.workspace_id and refreshed["can_refresh"]
    assert catalogue.calls == [("health", "openai"), ("enrich", "openai")]
    assert application.calls[-1][:2] == ("/api/v1/services/tts/refresh", "POST")
    assert "body" not in application.calls[-1][2]


def test_openapi_matches_registered_scoped_passive_reads_and_effects(case):
    paths = build_openapi_document()["paths"]
    for path in (
        "/api/v1/services/tts",
        "/api/v1/services/tts/{serviceId}",
        "/api/v1/services/stt",
        "/api/v1/voices",
    ):
        parameters = {row["name"]: row for row in paths[path]["get"]["parameters"]}
        assert parameters["workspace_id"]["schema"]["maxLength"] == 80
        assert parameters["session_id"]["schema"]["minLength"] == 1
    for path in ("/api/v1/services/tts/refresh", "/api/v1/services/tts/{serviceId}/refresh"):
        operation = paths[path]["post"]
        assert operation["x-required-scopes"] == [
            "app.read",
            "app.write",
            "app.run",
            "app.credentials.write",
        ]
        assert not {"session_id", "refresh"} & {row["name"] for row in operation["parameters"]}
        assert "requestBody" not in operation
    for path, method in (
        ("/api/v1/services/tts/discover", "post"),
        ("/api/v1/services/tts/xtts/models", "get"),
        ("/api/v1/services/tts/xtts/models", "post"),
        ("/api/v1/services/tts/xtts/models/{modelId}", "delete"),
    ):
        assert paths[path][method]["x-required-role"] == "administrator"
        assert "app.read" in paths[path][method]["x-required-scopes"]
    registered = {
        (rule.rule, method)
        for rule in case.app.url_map.iter_rules()
        for method in (rule.methods or ())
    }
    assert ("/api/v1/services/tts/refresh", "POST") in registered
    assert ("/api/v1/services/tts/<service_id>/refresh", "POST") in registered


def test_mcp_selected_model_uses_project_scoped_approved_detail(case, catalogue, monkeypatch):
    members = team(case, monkeypatch)
    save(
        members.owner,
        members.oh,
        workspace=members.workspace_id,
        records=[connection("NATIVE", identifier="audio_cpp", native=True)],
    )
    project = create_team_session(members, key="mcp-selected-speech-model")
    application = WorkspaceHttpClient(members.alice, members.ah)
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    result = tts_catalog(
        runtime,
        TtsCatalogInput(
            session_id=project["id"],
            service_id="audio_cpp",
            model=NATIVE_MODEL,
            detail="full",
            include_managed_voices=False,
        ),
    )
    assert result["workspace_id"] == members.workspace_id and not result["can_manage"]
    model = result["services"][0]
    assert set(model["expressive_capabilities"]) == {NATIVE_MODEL}
    assert model["expressive_capabilities"][NATIVE_MODEL]["instructions"] != "none"
    assert model["model_catalog"][0]["recommended_chunk_characters"] == 1800
    assert "PRIVATE configured" not in str(result) and not catalogue.calls
    assert application.calls == [
        (
            "/api/v1/services/tts/audio_cpp",
            "GET",
            {
                "parameters": {"session_id": project["id"], "model": NATIVE_MODEL},
                "maximum_body_bytes": 8 * 1024 * 1024,
            },
        )
    ]


def test_default_mcp_summary_fits_normal_transport_and_full_detail_is_explicit(case, catalogue):
    from pandrator.logic.tts_provider_profiles import AUDIO_CPP_MODEL_CATALOG

    save(
        case.client,
        {"X-CSRF-Token": case.csrf},
        records=[
            {
                **connection("NATIVE", identifier="audio_cpp", native=True),
                "models": [row["id"] for row in AUDIO_CPP_MODEL_CATALOG],
                "voices": [f"voice-{number}" for number in range(35)],
            }
        ],
    )
    application = WorkspaceHttpClient(case.client, {"X-CSRF-Token": case.csrf})
    runtime = cast(McpRuntime, SimpleNamespace(require_application=lambda: application))
    summary = tts_catalog(
        runtime, TtsCatalogInput(service_id="audio_cpp", include_managed_voices=False)
    )
    assert application.calls[-1][2]["parameters"]["view"] == "summary"
    assert "maximum_body_bytes" not in application.calls[-1][2]
    assert summary["services"][0]["voice_count"] >= 35
    assert len(summary["services"][0].get("voices_preview", [])) <= 20
    full = tts_catalog(
        runtime,
        TtsCatalogInput(
            service_id="audio_cpp",
            detail="full",
            include_managed_voices=False,
        ),
    )
    assert application.calls[-1][2]["parameters"]["view"] == "full"
    assert application.calls[-1][2]["maximum_body_bytes"] == 8 * 1024 * 1024
    assert full["services"][0]["model_catalog"] and not catalogue.calls
