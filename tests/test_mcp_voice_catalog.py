from types import SimpleNamespace
from unittest.mock import Mock

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.schemas import TtsCatalogInput, VoiceCatalogInput
from pandrator_mcp.schemas.voice_lifecycle import VoiceCatalogCapabilitiesInput
from pandrator_mcp.tools.e2e import tts_catalog
from pandrator_mcp.tools.voice_lifecycle import (
    voice_catalog,
    voice_catalog_capabilities,
)


def _runtime(application):
    return SimpleNamespace(require_application=lambda: application)


def test_voice_catalog_forwards_flat_filters_and_projects_safe_items():
    application = Mock()
    application.voice_catalog.return_value = {
        "schema_version": "1",
        "catalog_revision": "rev-1",
        "total": 1,
        "next_cursor": "cursor",
        "facets": {"kind": {"provider": 1}},
        "taxonomy": {"voice_category": ["female"]},
        "items": [
            {
                "key": "provider:s:m:v",
                "reference": {
                    "kind": "provider",
                    "service_id": "s",
                    "model": "m",
                    "voice": "v",
                },
                "name": "Voice",
                "profile": {"evidence": {"pitch": {"status": "described"}}},
                "compatibility": [],
                "preview_artifact_id": "artifact-1",
                "provider_token": "must-not-leak",
            }
        ],
    }
    runtime = _runtime(application)
    result = voice_catalog(
        runtime,
        VoiceCatalogInput(
            query="warm",
            language="en",
            kind="provider",
            perceived_age="adult",
            delivery_preset="storytelling",
            tag="Narrator",
            ready_only=True,
            sort="recently_updated",
            limit=10,
            cursor="cursor-1",
        ),
    )

    call = application.voice_catalog.call_args.kwargs
    assert call["query"] == "warm"
    assert call["language"] == "en"
    assert call["kind"] == "provider"
    assert call["perceived_age"] == "adult"
    assert call["delivery_preset"] == "storytelling"
    assert call["tag"] == "Narrator"
    assert call["ready_only"] is True
    assert call["sort"] == "recently_updated"
    assert call["cursor"] == "cursor-1"
    assert result["items"][0]["safe_artifact_id"] == "artifact-1"
    assert "provider_token" not in str(result)


def test_voice_client_forwards_new_flat_filters():
    client = object.__new__(ApplicationClient)
    client._request_json = Mock(return_value={"items": []})

    client.voice_catalog(
        perceived_age="adult",
        delivery_preset="storytelling",
        tag="Narrator",
    )

    args, kwargs = client._request_json.call_args
    assert args == ("/api/v1/voice-catalog",)
    assert kwargs["parameters"] == {
        "perceived_age": "adult",
        "delivery_preset": "storytelling",
        "tag": "Narrator",
        "limit": 30,
    }


def test_voice_catalog_capabilities_filters_models_without_exposing_raw_services():
    application = Mock()
    application.voice_catalog_capabilities.return_value = {
        "schema_version": "1",
        "voice_profile_schema_version": 1,
        "features": {"voice_design": True},
        "markup": {"syntax": "safe"},
        "models": [
            {"service_id": "s", "model": "design", "modes": {"design": True}},
            {"service_id": "s", "model": "clone", "modes": {"cloning": True}},
        ],
    }
    result = voice_catalog_capabilities(
        _runtime(application),
        VoiceCatalogCapabilitiesInput(service_id="s", model="DESIGN"),
    )
    assert [item["model"] for item in result["models"]] == ["design"]
    assert result["models"][0]["modes"]["design"] is True
    assert result["voice_profile_schema_version"] == 1


def test_tts_catalog_prunes_full_nested_model_maps_to_retained_model():
    application = Mock()
    application.tts_catalog.return_value = {
        "services": [
            {
                "id": "service-a",
                "name": "Service A",
                "available": True,
                "models": ["model-a", "model-b"],
                "model_catalog": [{"id": "model-a"}, {"id": "model-b"}],
                "model_voice_modes": {"model-a": {"design": True}, "model-b": {}},
                "expressive_capabilities": {
                    "model-a": {"status": "verified"},
                    "model-b": {},
                },
                "voice_catalogues": {"model-a": ["a"], "model-b": ["b"]},
                "defaults_by_model": {"model-a": "a", "model-b": "b"},
                "voices": [],
            }
        ],
        "managed_voices": [],
    }
    application.list_voices.return_value = {"items": []}
    result = tts_catalog(
        _runtime(application),
        TtsCatalogInput(model="MODEL-A", detail="full"),
    )
    service = result["services"][0]
    assert service["models"] == ["model-a"]
    assert [item["id"] for item in service["model_catalog"]] == ["model-a"]
    assert set(service["model_voice_modes"]) == {"model-a"}
    assert set(service["expressive_capabilities"]) == {"model-a"}
    assert set(service["voice_catalogues"]) == {"model-a"}
    assert set(service["defaults_by_model"]) == {"model-a"}


def test_unfiltered_tts_catalog_keeps_known_uninstalled_models():
    application = Mock()
    application.tts_catalog.return_value = {
        "services": [
            {
                "id": "audio_cpp",
                "models": ["base"],
                "model_catalog": [{"id": "base"}, {"id": "design"}],
                "model_voice_modes": {"base": "cloning", "design": "design"},
            }
        ]
    }
    application.list_voices.return_value = {"items": []}
    result = tts_catalog(_runtime(application), TtsCatalogInput(detail="full"))
    assert [model["id"] for model in result["services"][0]["model_catalog"]] == [
        "base",
        "design",
    ]
    assert result["services"][0]["model_voice_modes"]["design"] == "design"
