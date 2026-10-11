import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

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
        "collections": [
            {
                "id": "collection-1",
                "name": "Narrators",
                "revision": 3,
                "member_count": 1,
                "provider_token": "must-not-leak",
            }
        ],
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
                "profile": {
                    "evidence": {
                        "pitch": {"source": "user", "status": "described"}
                    }
                },
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
    assert "view" not in call
    assert "fields" not in call
    assert result == {
        "schema_version": "1",
        "catalog_revision": "rev-1",
        "total": 1,
        "next_cursor": "cursor",
        "facets": {"kind": {"provider": 1}},
        "taxonomy": {"voice_category": ["female"]},
        "items": [
            {
                "key": "provider:s:m:v",
                "name": "Voice",
                "compatibility": [],
                "reference": {
                    "kind": "provider",
                    "service_id": "s",
                    "model": "m",
                    "voice": "v",
                },
                "profile": {
                    "evidence": {
                        "pitch": {"source": "user", "status": "described"}
                    }
                },
                "evidence": {
                    "pitch": {"source": "user", "status": "described"}
                },
                "safe_artifact_id": "artifact-1",
            }
        ],
        "collections": [
            {
                "id": "collection-1",
                "name": "Narrators",
                "revision": 3,
                "member_count": 1,
            }
        ],
    }
    assert "provider_token" not in str(result)


def test_voice_catalog_compact_default_projects_safe_rows_and_keeps_page_metadata():
    application = Mock()
    application.voice_catalog.return_value = {
        "schema_version": "2",
        "catalog_revision": "rev-compact",
        "total": 8,
        "next_cursor": "next-page",
        "facets": {"kind": {"managed": 8}},
        "taxonomy": {"voice_category": ["female"]},
        "collections": [{"id": "collection-1", "name": "Narrators"}],
        "items": [
            {
                "id": "voice-1",
                "key": "managed:voice-1",
                "kind": "managed",
                "name": "Mara",
                "reference": {
                    "kind": "managed",
                    "voice_id": "voice-1",
                    "provider_token": "must-not-leak",
                },
                "language": "en",
                "voice_category": "female",
                "revision": 4,
                "sample_count": 2,
                "safe_artifact_id": "safe-artifact-1",
                "compatibility": [{"service_id": "audio_cpp", "ready": True}],
                "description": "not requested",
                "profile": {"private_provider_data": "not requested"},
                "provider_token": "must-not-leak",
            }
        ],
    }

    result = voice_catalog(_runtime(application), VoiceCatalogInput(view="compact"))

    assert result == {
        "schema_version": "2",
        "catalog_revision": "rev-compact",
        "total": 8,
        "next_cursor": "next-page",
        "items": [
            {
                "id": "voice-1",
                "key": "managed:voice-1",
                "kind": "managed",
                "name": "Mara",
                "reference": {"kind": "managed", "voice_id": "voice-1"},
                "language": "en",
                "voice_category": "female",
                "revision": 4,
                "sample_count": 2,
                "safe_artifact_id": "safe-artifact-1",
                "compatibility": [{"service_id": "audio_cpp", "ready": True}],
            }
        ],
    }
    call = application.voice_catalog.call_args.kwargs
    assert "view" not in call
    assert "fields" not in call
    assert "provider_token" not in str(result)


def test_voice_catalog_explicit_fields_return_only_selected_keys():
    application = Mock()
    application.voice_catalog.return_value = {
        "schema_version": "1",
        "catalog_revision": "rev-1",
        "total": 1,
        "next_cursor": None,
        "facets": {"kind": {"managed": 1}},
        "taxonomy": {"voice_category": ["female"]},
        "collections": [{"id": "collection-1"}],
        "items": [
            {
                "id": "voice-1",
                "key": "managed:voice-1",
                "name": "Mara",
                "voice_category": "female",
                "profile": {"evidence": {"pitch": {"status": "described"}}},
                "provider_secret": "must-not-leak",
            }
        ],
    }

    result = voice_catalog(
        _runtime(application),
        VoiceCatalogInput(view="full", fields=["id", "name", "voice_category"]),
    )

    assert result == {
        "schema_version": "1",
        "catalog_revision": "rev-1",
        "total": 1,
        "next_cursor": None,
        "items": [{"id": "voice-1", "name": "Mara", "voice_category": "female"}],
    }
    assert "provider_secret" not in str(result)


def test_voice_catalog_explicit_row_collections_do_not_restore_global_collections():
    application = Mock()
    application.voice_catalog.return_value = {
        "collections": [{"id": "global-collection"}],
        "items": [
            {
                "collections": [{"id": "row-collection", "name": "Narrators"}],
            }
        ],
    }

    result = voice_catalog(
        _runtime(application),
        VoiceCatalogInput(view="compact", fields=["collections"]),
    )

    assert result == {
        "schema_version": "1",
        "items": [{"collections": [{"id": "row-collection", "name": "Narrators"}]}],
    }


@pytest.mark.parametrize(
    ("fields", "message"),
    [
        ([], "at least 1 item"),
        (["id", "id"], "unique voice catalog field names"),
        (["id", "provider_secret"], "Unknown voice catalog field(s)"),
        (["id"] * 21, "at most 20 voice catalog field names"),
    ],
)
def test_voice_catalog_rejects_invalid_field_lists(fields, message):
    with pytest.raises(ValidationError) as error:
        VoiceCatalogInput(fields=fields)

    assert message in str(error.value)


@pytest.mark.parametrize(
    ("view", "fields"),
    [("full", None), ("compact", ["profile", "evidence", "safe_artifact_id"])],
)
def test_voice_catalog_safe_profile_evidence_and_preview_artifact_projection(view, fields):
    application = Mock()
    application.voice_catalog.return_value = {
        "items": [
            {
                "profile": {
                    "schema_version": 1,
                    "pitch": "low",
                    "perceived_age": "adult",
                    "textures": ["warm", {"provider_secret": "must-not-leak"}, 7],
                    "delivery_presets": ["storytelling"],
                    "use_cases": ["narration"],
                    "languages": [
                        {
                            "language": "en",
                            "locale": "en-US",
                            "accent": "soft",
                            "detail": "safe language detail",
                            "evidence": {
                                "source": "provider",
                                "status": "described",
                                "note": "provider supplied evidence",
                                "provider_token": "must-not-leak",
                            },
                            "provider_token": "must-not-leak",
                        },
                        {"language": {"provider_secret": "must-not-leak"}},
                        "invalid language row",
                    ],
                    "tags": ["Narrator", {"access_token": "must-not-leak"}],
                    "evidence": {
                        "pitch": {
                            "source": "user",
                            "status": "described",
                            "note": "profile evidence",
                            "provider_secret": "must-not-leak",
                        },
                        "provider_metadata": {"token": "must-not-leak"},
                        "invalid_source": {"source": "unknown", "status": "reviewed"},
                    },
                    "provider_metadata": {"api_key": "must-not-leak"},
                },
                "preview_artifact_id": "preview-1",
                "artifact_id": "raw-artifact-1",
                "provider_secret": "must-not-leak",
                "evidence": {
                    "pitch": {
                        "source": "audition_review",
                        "status": "reviewed",
                        "artifact_id": "evidence-artifact-1",
                        "note": "reviewed pitch",
                        "reviewer": "must-not-leak",
                        "confidence": 0.99,
                    },
                    "provider_metadata": {"token": "must-not-leak"},
                },
            }
        ]
    }

    result = voice_catalog(
        _runtime(application),
        VoiceCatalogInput(view=view, fields=fields),
    )

    safe_profile = {
        "schema_version": 1,
        "pitch": "low",
        "perceived_age": "adult",
        "textures": ["warm"],
        "delivery_presets": ["storytelling"],
        "use_cases": ["narration"],
        "languages": [
            {
                "language": "en",
                "locale": "en-US",
                "accent": "soft",
                "detail": "safe language detail",
                "evidence": {
                    "source": "provider",
                    "status": "described",
                    "note": "provider supplied evidence",
                },
            }
        ],
        "tags": ["Narrator"],
        "evidence": {
            "pitch": {
                "source": "user",
                "status": "described",
                "note": "profile evidence",
            }
        },
    }
    safe_evidence = {
        "pitch": {
            "source": "audition_review",
            "status": "reviewed",
            "artifact_id": "evidence-artifact-1",
            "note": "reviewed pitch",
        }
    }
    if fields is None:
        assert result["items"][0]["profile"] == safe_profile
        assert result["items"][0]["evidence"] == safe_evidence
        assert result["items"][0]["safe_artifact_id"] == "preview-1"
    else:
        assert result["items"] == [
            {
                "profile": safe_profile,
                "evidence": safe_evidence,
                "safe_artifact_id": "preview-1",
            }
        ]
    assert "raw-artifact-1" not in str(result)
    assert "provider_secret" not in str(result)
    assert "provider_token" not in str(result)
    assert "access_token" not in str(result)
    assert "api_key" not in str(result)
    assert "reviewer" not in str(result)
    assert "confidence" not in str(result)


def test_voice_catalog_compact_projection_reduces_synthetic_three_row_json_bytes():
    def row(index):
        return {
            "id": f"voice-{index}",
            "key": f"managed:voice-{index}",
            "kind": "managed",
            "name": f"Voice {index}",
            "description": "A detailed voice description. " * 8,
            "language": "en",
            "voice_category": "female",
            "origin": "user",
            "revision": index,
            "collections": [],
            "compatibility": [],
            "sample_count": 2,
            "created_at": "2026-10-11T00:00:00Z",
            "updated_at": "2026-10-11T00:00:00Z",
            "bundled": False,
            "match_reasons": ["name"],
            "reference": {"kind": "managed", "voice_id": f"voice-{index}"},
            "profile": {"tags": ["profile detail. " * 18 for _ in range(5)]},
            "evidence": {
                "pitch": {
                    "source": "user",
                    "status": "described",
                    "note": "evidence detail. " * 18,
                }
            },
            "preview_artifact_id": f"preview-{index}",
            "provider_metadata": {"private": "provider detail. " * 18},
        }

    application = Mock()
    application.voice_catalog.return_value = {
        "schema_version": "1",
        "catalog_revision": "rev-size",
        "total": 3,
        "next_cursor": None,
        "facets": {"kind": {"managed": 3}},
        "taxonomy": {"voice_category": ["female"]},
        "collections": [],
        "items": [row(index) for index in range(3)],
    }

    full = voice_catalog(_runtime(application), VoiceCatalogInput())
    compact = voice_catalog(_runtime(application), VoiceCatalogInput(view="compact"))
    full_bytes = len(json.dumps(full, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    compact_bytes = len(
        json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )

    assert compact_bytes < full_bytes * 0.65


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
