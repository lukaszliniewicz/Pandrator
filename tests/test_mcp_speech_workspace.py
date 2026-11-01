"""Scoped passive TTS catalogue reads and explicit refresh MCP contract."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from mcp import Client
from pydantic import ValidationError

from pandrator_mcp import schemas
from pandrator_mcp.catalog import ACTION_CATALOG, RiskClass
from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
from pandrator_mcp.context import McpRuntime, build_runtime
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.results import ToolOutcome
from pandrator_mcp.schemas.e2e import (
    ConfigureTtsInput,
    TtsCatalogInput,
    TtsCatalogRefreshInput,
)
from pandrator_mcp.schemas.work import GetWorkInput
from pandrator_mcp.server import build_server
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.e2e import configure_tts, refresh_tts_catalog, tts_catalog
from pandrator_mcp.tools.work import get_work


class RecordingApplication(ApplicationVoiceMethods):
    """Use the real HTTP request builders while recording their fixed boundary."""

    def __init__(self, *responses: dict[str, Any]):
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _request_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        self.calls.append((path, kwargs))
        return self.responses.pop(0) if self.responses else {}

    def _request_binary_json(self, path: str, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError(f"Unexpected binary request: {path}")


def _runtime(application: Any) -> McpRuntime:
    return cast(
        McpRuntime,
        SimpleNamespace(require_application=lambda: application),
    )


def _registered_runtime(tmp_path: Path, application: Any) -> McpRuntime:
    runtime = build_runtime(
        McpSettings(
            target_name="unconfigured",
            configuration_path=tmp_path / "missing-targets.json",
        )
    )
    runtime.application = application
    runtime.startup_error = None
    return runtime


def _string_branch(schema: dict[str, Any]) -> dict[str, Any]:
    if schema.get("type") == "string":
        return schema
    return next(branch for branch in schema["anyOf"] if branch.get("type") == "string")


def test_catalogue_scope_inputs_trim_and_reject_invalid_values():
    scoped = TtsCatalogInput.model_validate(
        {"workspace_id": "  team-space  ", "session_id": "  session-1  "}
    )
    assert scoped.workspace_id == "team-space"
    assert scoped.session_id == "session-1"
    assert TtsCatalogInput().workspace_id is None
    assert TtsCatalogInput().session_id is None

    refresh = TtsCatalogRefreshInput.model_validate(
        {"workspace_id": "  team-space  ", "service_id": "  cloud-tts  "}
    )
    assert refresh.workspace_id == "team-space"
    assert refresh.service_id == "cloud-tts"

    invalid = (
        (TtsCatalogInput, "workspace_id", " "),
        (TtsCatalogInput, "workspace_id", "w" * 81),
        (TtsCatalogInput, "session_id", " "),
        (TtsCatalogInput, "session_id", "s" * 81),
        (TtsCatalogInput, "workspace_id", True),
        (TtsCatalogInput, "session_id", 7),
        (TtsCatalogRefreshInput, "workspace_id", " "),
        (TtsCatalogRefreshInput, "workspace_id", "w" * 81),
        (TtsCatalogRefreshInput, "service_id", " "),
        (TtsCatalogRefreshInput, "service_id", "s" * 161),
        (TtsCatalogRefreshInput, "service_id", False),
    )
    for model, field, value in invalid:
        with pytest.raises(ValidationError):
            model.model_validate({field: value})


def test_application_voice_builders_keep_reads_passive_and_refresh_fixed():
    application = RecordingApplication()
    application.tts_catalog(
        service_id="cloud-tts",
        workspace_id="workspace-1",
        session_id="session-1",
    )
    application.list_voices(workspace_id="workspace-1", session_id="session-1")
    application.list_voices()
    application.refresh_tts_catalog(
        workspace_id="workspace-1",
        service_id="cloud-tts",
    )

    assert application.calls == [
        (
            "/api/v1/services/tts",
            {
                "parameters": {
                    "service_id": "cloud-tts",
                    "workspace_id": "workspace-1",
                    "session_id": "session-1",
                    "view": "summary",
                }
            },
        ),
        (
            "/api/v1/voices",
            {"parameters": {"workspace_id": "workspace-1", "session_id": "session-1"}},
        ),
        ("/api/v1/voices", {}),
        (
            "/api/v1/services/tts/refresh",
            {
                "method": "POST",
                "parameters": {
                    "workspace_id": "workspace-1",
                    "service_id": "cloud-tts",
                    "view": "summary",
                },
            },
        ),
    ]
    assert "body" not in application.calls[-1][1]
    assert "idempotency_key" not in application.calls[-1][1]
    assert "refresh" not in application.calls[0][1]["parameters"]

    with pytest.raises(PandratorMcpError) as error:
        application.tts_catalog(refresh=True)
    assert error.value.code == "validation_error"
    assert "pandrator_refresh_tts_catalog" in str(error.value)
    assert len(application.calls) == 4


def test_catalogue_read_uses_authoritative_workspace_for_managed_voices():
    application = RecordingApplication(
        {
            "workspace_id": "resolved-workspace",
            "can_manage": False,
            "can_refresh": True,
            "revision": 17,
            "services": [
                {
                    "id": "cloud-tts",
                    "available": True,
                    "models": ["model-1"],
                    "voices": ["voice-1"],
                    "base_url": "https://private.example/api",
                    "credential": "private-credential",
                    "private_profile": {"secret": "private-profile"},
                    "preview": "private-preview",
                }
            ],
        },
        {
            "items": [
                {
                    "id": "managed-1",
                    "name": "Managed voice",
                    "language": "en",
                    "revision": 3,
                    "metadata_json": {
                        "providers": {
                            "cloud-tts": {
                                "status": "ready",
                                "voice_id": "provider-voice-1",
                                "stale_reason": None,
                                "credential": "private-credential",
                            }
                        }
                    },
                    "private_profile": "hidden",
                }
            ]
        },
    )

    result = tts_catalog(
        _runtime(application),
        TtsCatalogInput(
            workspace_id="initial-workspace",
            session_id="session-1",
        ),
    )

    assert application.calls == [
        (
            "/api/v1/services/tts",
            {
                "parameters": {
                    "workspace_id": "initial-workspace",
                    "session_id": "session-1",
                    "view": "summary",
                }
            },
        ),
        (
            "/api/v1/voices",
            {"parameters": {"workspace_id": "resolved-workspace"}},
        ),
    ]
    assert result["workspace_id"] == "resolved-workspace"
    assert result["can_manage"] is False
    assert result["can_refresh"] is True
    assert result["revision"] == 17
    assert result["managed_voices"][0]["registrations"]["cloud-tts"] == {
        "status": "ready",
        "voice_id": "provider-voice-1",
        "stale_reason": None,
    }
    assert "private-credential" not in repr(result)
    assert "private-profile" not in repr(result)
    assert "private.example" not in repr(result)


def test_catalogue_voice_scope_falls_back_to_explicit_roots_or_legacy_no_args():
    explicit = RecordingApplication({"services": []}, {"items": []})
    tts_catalog(
        _runtime(explicit),
        TtsCatalogInput(workspace_id="explicit-workspace", session_id="session-2"),
    )
    assert explicit.calls[1] == (
        "/api/v1/voices",
        {
            "parameters": {
                "workspace_id": "explicit-workspace",
                "session_id": "session-2",
            }
        },
    )

    class LegacyApplication:
        def __init__(self):
            self.calls: list[tuple[str, Any]] = []

        def tts_catalog(self, *, refresh: bool = False, service_id: str | None = None):
            self.calls.append(("tts_catalog", {"refresh": refresh, "service_id": service_id}))
            return {"services": []}

        def list_voices(self):
            self.calls.append(("list_voices", None))
            return {"items": []}

    legacy = LegacyApplication()
    tts_catalog(_runtime(legacy), TtsCatalogInput())
    assert legacy.calls == [
        ("tts_catalog", {"refresh": False, "service_id": None}),
        ("list_voices", None),
    ]


def test_explicit_refresh_projects_only_safe_post_response_fields():
    application = RecordingApplication(
        {
            "workspace_id": "actual-workspace",
            "can_manage": True,
            "can_refresh": True,
            "revision": 21,
            "default_service": "cloud-tts",
            "services": [
                {
                    "id": "cloud-tts",
                    "name": "Cloud TTS",
                    "available": True,
                    "models": ["model-1"],
                    "voices": ["voice-1"],
                    "endpoint": "https://private.example/private",
                    "credential": "private-credential",
                    "private_profile": {"token": "private-profile"},
                    "previews": ["private-preview"],
                }
            ],
            "private_profiles": [{"token": "private-profile"}],
        }
    )

    result = refresh_tts_catalog(
        _runtime(application),
        TtsCatalogRefreshInput(
            workspace_id="actual-workspace",
            service_id="cloud-tts",
        ),
    )

    assert application.calls == [
        (
            "/api/v1/services/tts/refresh",
            {
                "method": "POST",
                "parameters": {
                    "workspace_id": "actual-workspace",
                    "service_id": "cloud-tts",
                    "view": "summary",
                },
            },
        )
    ]
    assert result["schema_version"] == "1"
    assert result["workspace_id"] == "actual-workspace"
    assert result["can_manage"] is True
    assert result["can_refresh"] is True
    assert result["revision"] == 21
    assert [service["id"] for service in result["services"]] == ["cloud-tts"]
    assert result["managed_voices"] == []
    assert "private-credential" not in repr(result)
    assert "private-profile" not in repr(result)
    assert "private-preview" not in repr(result)
    assert "https://" not in repr(result)
    assert "voices_preview" not in result["services"][0]


def test_refresh_true_is_rejected_before_resolving_or_calling_application():
    class UnavailableRuntime:
        def require_application(self):
            raise AssertionError("GET refresh rejection must happen before client resolution.")

    with pytest.raises(PandratorMcpError) as error:
        tts_catalog(cast(McpRuntime, UnavailableRuntime()), TtsCatalogInput(refresh=True))
    assert error.value.code == "validation_error"
    assert str(error.value) == (
        "The TTS catalogue read is passive; use "
        "pandrator_refresh_tts_catalog to refresh TTS connections."
    )


def test_configure_tts_uses_session_catalogue_scope_without_refresh_retry():
    application = RecordingApplication(
        {
            "service": {
                "id": "audio_cpp",
                "adapter": "audio_cpp",
                "available": True,
                "models": ["advertised-model"],
                "default_model": "advertised-model",
            }
        },
        {"items": []},
    )

    with pytest.raises(PandratorMcpError) as error:
        configure_tts(
            _runtime(application),
            ConfigureTtsInput(
                session_id="session-9",
                service_id="audio_cpp",
                model="missing-model",
                expected_revision=4,
                idempotency_key="speech-test-1",
            ),
        )

    assert error.value.code == "validation_error"
    assert "not advertised" in str(error.value)
    assert application.calls == [
        (
            "/api/v1/services/tts/audio_cpp",
            {
                "parameters": {
                    "model": "missing-model",
                    "session_id": "session-9",
                },
                "maximum_body_bytes": 8 * 1024 * 1024,
            },
        ),
        ("/api/v1/voices", {"parameters": {"session_id": "session-9"}}),
    ]


def test_post_install_catalogue_read_is_passive_and_keeps_legacy_result_field():
    application = RecordingApplication(
        {"services": [{"id": "audio_cpp", "models": [], "available": True}]}
    )
    manager = SimpleNamespace(
        operation=lambda operation_id: {
            "id": operation_id,
            "kind": "audio_cpp.install",
            "state": "succeeded",
            "progress": 1.0,
        }
    )
    runtime = cast(
        McpRuntime,
        SimpleNamespace(
            application=application,
            manager=manager,
            require_application=lambda: application,
        ),
    )

    outcome = get_work(
        runtime,
        GetWorkInput(work_type="manager_operation", work_id="operation-1"),
    )

    assert isinstance(outcome, ToolOutcome)
    assert outcome.result["tts_catalogue_refresh"]["status"] == "inspected"
    assert application.calls == [("/api/v1/services/tts", {"parameters": {"view": "summary"}})]
    assert "refresh" not in (application.calls[0][1].get("parameters") or {})


def test_registered_catalogue_tools_publish_bounds_and_reject_before_requests(tmp_path: Path):
    application = RecordingApplication()
    runtime = _registered_runtime(tmp_path, application)

    async def exercise():
        async with Client(build_server(runtime), mode="auto", raise_exceptions=False) as client:
            listed = await client.list_tools()
            invalid_refresh = await client.call_tool(
                "pandrator_refresh_tts_catalog",
                {"workspace_id": "w" * 81},
            )
            passive_refresh = await client.call_tool(
                "pandrator_get_tts_catalog",
                {"refresh": True},
            )
        return listed, invalid_refresh, passive_refresh

    listed, invalid_refresh, passive_refresh = asyncio.run(exercise())
    tools = {tool.name: tool for tool in listed.tools}
    refresh_tool = tools["pandrator_refresh_tts_catalog"]
    read_tool = tools["pandrator_get_tts_catalog"]

    spec = ACTION_CATALOG.get("pandrator_refresh_tts_catalog")
    assert spec.risk is RiskClass.RUN
    assert spec.required_scope == "app.run"
    assert spec.downstream_operation_id == "refreshTtsServices"
    assert spec.method == "POST"
    assert spec.path == "/api/v1/services/tts/refresh"
    assert spec.enabled is True
    assert spec.requires_idempotency is False
    assert spec.requires_confirmation is False
    assert ACTION_CATALOG.get("pandrator_get_tts_catalog").risk is RiskClass.READ
    assert read_tool.annotations is not None
    assert read_tool.annotations.read_only_hint is True
    assert refresh_tool.annotations is not None
    assert refresh_tool.annotations.read_only_hint is False
    assert refresh_tool.annotations.destructive_hint is True
    assert refresh_tool.annotations.idempotent_hint is True

    for tool, scope_lengths in (
        (read_tool, {"workspace_id": (1, 80), "session_id": (1, 80)}),
        (refresh_tool, {"workspace_id": (1, 80), "service_id": (1, 160)}),
    ):
        properties = tool.input_schema["properties"]
        for field, (minimum, maximum) in scope_lengths.items():
            field_schema = _string_branch(properties[field])
            assert field_schema["minLength"] == minimum
            assert field_schema["maxLength"] == maximum
    response_mode = refresh_tool.input_schema["properties"]["response_mode"]
    assert response_mode["enum"] == ["standard", "structured"]
    assert response_mode["default"] == "standard"

    assert invalid_refresh.is_error
    invalid_text = " ".join(str(getattr(item, "text", "")) for item in invalid_refresh.content)
    assert "workspace_id" in invalid_text
    assert passive_refresh.is_error
    passive_text = " ".join(str(getattr(item, "text", "")) for item in passive_refresh.content)
    assert "pandrator_refresh_tts_catalog" in passive_text
    assert application.calls == []


def test_refresh_input_schema_exposes_only_bounded_scope_fields():
    schema = TtsCatalogRefreshInput.model_json_schema()
    assert set(schema["properties"]) == {"workspace_id", "service_id"}
    workspace = schema["properties"]["workspace_id"]["anyOf"][0]
    service = schema["properties"]["service_id"]["anyOf"][0]
    assert workspace["minLength"] == 1 and workspace["maxLength"] == 80
    assert service["minLength"] == 1 and service["maxLength"] == 160
    assert TtsCatalogRefreshInput in schemas.TOOL_INPUT_MODELS
    assert "TtsCatalogRefreshInput" in schemas.__all__
