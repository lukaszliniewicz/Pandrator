"""Selected detail preserves the raw metadata lookup identity used by consumers."""

from copy import deepcopy

import pytest

from pandrator.logic.tts_model_lifecycle import model_lifecycle
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AppSetting
from pandrator.web.tts_catalogue_projection import (
    TtsCatalogueModelNotFoundError,
    _decorate_model_language_support,
    _filter_service_models,
    _slim_model_catalog,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.mark.parametrize(
    ("models", "key", "row", "selected", "retained"),
    [
        (["custom:version"], "custom:version:voice", {"id": "voice"}, "custom:version", True),
        (["custom:version"], "custom:version:voice", {}, "custom:version", True),
        (["custom", "custom:version"], "custom:version:voice", {"id": "voice"}, "custom", False),
        (
            ["custom", "custom:version"],
            "custom:version:voice",
            {"id": "version:voice"},
            "custom",
            True,
        ),
        (
            ["custom", "custom:version"],
            "custom:version:voice",
            {"id": "version:voice"},
            "custom:version",
            False,
        ),
        # An unescaped key can represent two valid lookups. A sparse row cannot
        # distinguish them; either exact consumer lookup must remain possible.
        (["custom", "custom:version"], "custom:version:voice", {}, "custom", True),
        (["custom", "custom:version"], "custom:version:voice", {}, "custom:version", True),
        (
            ["MAI-Voice-2"],
            "MAI-Voice-2:en-US-Ethan:MAI-Voice-2",
            {"id": "en-US-Ethan:MAI-Voice-2"},
            "MAI-Voice-2",
            True,
        ),
        (["a", "b"], "a:voice", {"voice_id": "voice", "model": "b", "model_id": "b"}, "a", True),
        (["a", "b"], "a:voice", {"voice_id": "voice", "model": "b"}, "b", False),
        (
            ["a", "a:prefix"],
            "a:prefix:voice",
            {"id": "prefix:voice", "voice_id": "voice"},
            "a:prefix",
            True,
        ),
        (["a", "b"], "a:voice", {"voice_id": " voice "}, "a", True),
        (["a", "b"], "voice:global", {"id": "voice:global"}, "b", True),
        (["a", "b"], "voice", {"id": "voice"}, "a", True),
        (["a", "b"], "a:voice", None, "b", False),
        (["a"], "a:voice", "legacy row", "a", True),
    ],
)
def test_metadata_identity(models, key, row, selected, retained):
    service = {"models": models, "voice_metadata": {key: row}}
    result = _filter_service_models(service, [selected], service_id="fixture")
    assert result["voice_metadata"] == ({key: row} if retained else {})
    if retained:
        assert result["voice_metadata"][key] is row


@pytest.mark.parametrize("row", [{"id": "voice"}, {}, None])
def test_declared_colon_model_does_not_admit_phantom_prefix(row):
    service = {"models": ["custom:version"], "voice_metadata": {"custom:version:voice": row}}
    original = deepcopy(service)
    with pytest.raises(TtsCatalogueModelNotFoundError):
        _filter_service_models(service, ["custom"], service_id="fixture")
    assert service == original


@pytest.mark.parametrize(
    ("key", "row", "model"),
    [
        ("model:voice", {}, "model"),
        ("model:voice:variant", None, "model"),
        ("model:version:voice", {"id": "voice"}, "model:version"),
        ("model:voice:variant", {"voice_id": "voice:variant"}, "model"),
    ],
)
def test_metadata_only_catalogue_remains_selectable(key, row, model):
    service = {"voice_metadata": {key: row}}
    assert _filter_service_models(service, [model], service_id="fixture") == service
    assert service["voice_metadata"] == {key: row}


def test_global_metadata_uses_voice_catalogue_without_inventing_models():
    service = {
        "models": ["model"],
        "voices": ["global"],
        "voice_catalogues": {"model": ["global:variant"]},
        "voice_metadata": {"global": {"language": "en"}, "global:variant": {}},
    }
    original = deepcopy(service)
    with pytest.raises(TtsCatalogueModelNotFoundError):
        _filter_service_models(service, ["global"], service_id="fixture")
    assert service == original
    assert _filter_service_models(service, ["model"], service_id="fixture") == original


def test_metadata_identity_does_not_depend_on_row_order():
    sparse = ("model:version:other", {})
    identified = ("model:version:voice", {"id": "voice"})
    for rows in ([sparse, identified], [identified, sparse]):
        service = {"voice_metadata": dict(rows)}
        original = deepcopy(service)
        with pytest.raises(TtsCatalogueModelNotFoundError):
            _filter_service_models(service, ["model"], service_id="fixture")
        assert service == original
        result = _filter_service_models(service, ["model:version"], service_id="fixture")
        assert result["voice_metadata"] == dict(rows)


@pytest.mark.parametrize("key", ["orphan", ":voice", "model:"])
def test_incomplete_metadata_key_does_not_admit_model(key):
    service = {"voice_metadata": {key: {}}}
    with pytest.raises(TtsCatalogueModelNotFoundError):
        _filter_service_models(service, [key.split(":", 1)[0]], service_id="fixture")


def test_selection_preserves_chooser_context_and_filters_other_model_details():
    model = "model:version"
    service = {
        "models": [model, "other"],
        "voices": ["voice", "global"],
        "default_model": "other",
        "default_voice": "global",
        "model_catalog": [{"id": model}, {"id": "other"}],
        "voice_catalogues": {model: ["voice"], "other": ["global"]},
        "voice_metadata": {f"{model}:voice": {"id": "voice"}, "other:global": {"id": "global"}},
        "model_voice_modes": {model: "prebuilt", "other": "cloning"},
        "default_voices": {model: "voice", "other": "global"},
        "generation_prompt_models": [model, "other"],
    }
    original = deepcopy(service)
    selected = _filter_service_models(service, [model], service_id="fixture")
    for field in ("models", "voices", "default_model", "default_voice"):
        assert selected[field] == original[field]
    for field in ("voice_catalogues", "model_voice_modes", "default_voices"):
        assert selected[field] == {model: original[field][model]}
    assert selected["model_catalog"] == [{"id": model}]
    assert selected["generation_prompt_models"] == [model]
    assert selected["voice_metadata"] == {f"{model}:voice": {"id": "voice"}}


def test_model_selection_remains_case_sensitive():
    service = {"voice_metadata": {"Model:version:voice": {"id": "voice"}}}
    with pytest.raises(TtsCatalogueModelNotFoundError):
        _filter_service_models(service, ["model:version"], service_id="fixture")


@pytest.mark.parametrize("voice", ["voice", "voice:variant"])
def test_native_model_detail_preserves_persisted_metadata(tmp_path, voice):
    prepare_web_test_data_root(str(tmp_path))
    bootstrap = BootstrapTokenStore()
    app = create_app(
        data_root=str(tmp_path),
        testing=True,
        background_maintenance=False,
        bootstrap_tokens=bootstrap,
    )
    services = app.extensions["pandrator"]
    catalogue = services["tts_catalogue"]
    # No Manager discovery, provider refresh or live application database.
    catalogue.manager_bridge.descriptor_path = None
    model = "custom:version"
    key = f"{model}:{voice}"
    metadata = {key: {"id": voice, "language": "en"}, "shared:voice": {"language": "fr"}}
    try:
        with services["database"].session() as session:
            session.merge(
                AppSetting(
                    key="services.tts",
                    revision=1,
                    value_json={
                        "provider_configs": [
                            {
                                "id": "audio_cpp",
                                "models": [model],
                                "default_model": model,
                                "model_catalog": [{"id": model}],
                                "voices": [voice, "shared:voice"],
                                "voice_catalogues": {model: [voice, "shared:voice"]},
                                "voice_metadata": metadata,
                            }
                        ]
                    },
                )
            )
        client = app.test_client()
        assert (
            client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}).status_code
            == 200
        )
        full = client.get("/api/v1/services/tts/audio_cpp")
        detail = client.get("/api/v1/services/tts/audio_cpp", query_string={"model": model})
        assert full.status_code == detail.status_code == 200
        assert full.get_json()["service"]["voice_metadata"] == metadata
        assert detail.get_json()["service"]["voice_metadata"] == metadata
        assert detail.get_json()["service"]["voice_catalogues"] == {model: [voice, "shared:voice"]}
        for phantom in ("custom", "shared"):
            response = client.get("/api/v1/services/tts/audio_cpp", query_string={"model": phantom})
            assert response.status_code == 404
            assert response.get_json()["error"]["code"] == "tts_model_not_found"
    finally:
        services["tts_providers"].close()
        catalogue.manager_bridge.session.close()
        services["database"].dispose()


@pytest.mark.parametrize(
    (
        "provider_id",
        "model_id",
        "source_url",
        "shutdown_date",
        "include_shutdown_date",
        "replacements",
        "kind",
    ),
    [
        (
            "openai",
            "tts-1",
            "https://developers.openai.com/api/docs/deprecations",
            "2027-01-06",
            True,
            ["gpt-realtime-2.1-mini"],
            "different_api",
        ),
        (
            "openai",
            "tts-1-hd",
            "https://developers.openai.com/api/docs/deprecations",
            "2027-01-06",
            True,
            ["gpt-realtime-2.1-mini"],
            "different_api",
        ),
        (
            "openai",
            "gpt-4o-mini-tts-2025-03-20",
            "https://developers.openai.com/api/docs/deprecations",
            "2027-01-06",
            True,
            ["gpt-realtime-2.1-mini"],
            "different_api",
        ),
        (
            "openai",
            "gpt-4o-mini-tts-2025-12-15",
            "https://developers.openai.com/api/docs/deprecations",
            "2027-01-06",
            True,
            ["gpt-realtime-2.1-mini"],
            "different_api",
        ),
        (
            "openai",
            "gpt-4o-mini-tts",
            "https://developers.openai.com/api/docs/models/gpt-4o-mini-tts",
            None,
            True,
            ["gpt-realtime-2.1-mini"],
            "different_api",
        ),
        (
            "gemini",
            "gemini-2.5-flash-preview-tts",
            "https://ai.google.dev/gemini-api/docs/deprecations",
            "2026-11-17",
            True,
            ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"],
            "tts_model",
        ),
        (
            "gemini",
            "gemini-2.5-pro-preview-tts",
            "https://ai.google.dev/gemini-api/docs/deprecations",
            "2026-11-17",
            True,
            ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"],
            "tts_model",
        ),
        (
            "gemini",
            "gemini-3.1-flash-tts-preview",
            "https://ai.google.dev/gemini-api/docs/deprecations",
            "2026-11-17",
            True,
            ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"],
            "tts_model",
        ),
        (
            "elevenlabs",
            "eleven_turbo_v2",
            "https://elevenlabs.io/docs/overview/models",
            None,
            False,
            ["eleven_flash_v2"],
            "tts_model",
        ),
        (
            "elevenlabs",
            "eleven_turbo_v2_5",
            "https://elevenlabs.io/docs/overview/models",
            None,
            False,
            ["eleven_flash_v2_5"],
            "tts_model",
        ),
    ],
)
def test_model_lifecycle_returns_exact_pinned_metadata(
    provider_id,
    model_id,
    source_url,
    shutdown_date,
    include_shutdown_date,
    replacements,
    kind,
):
    expected = {
        "provider_id": provider_id,
        "model_id": model_id,
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": source_url,
        "replacement_model_ids": replacements,
        "migration_kind": kind,
    }
    if include_shutdown_date:
        expected["shutdown_date"] = shutdown_date
    assert model_lifecycle(provider_id, model_id) == expected


def test_model_lifecycle_returns_fresh_nested_data_and_requires_exact_identity():
    first = model_lifecycle("gemini", "gemini-2.5-flash-preview-tts")
    assert first is not None
    first["replacement_model_ids"].append("untrusted-model")
    assert model_lifecycle("gemini", "gemini-2.5-flash-preview-tts") == {
        "provider_id": "gemini",
        "model_id": "gemini-2.5-flash-preview-tts",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://ai.google.dev/gemini-api/docs/deprecations",
        "shutdown_date": "2026-11-17",
        "replacement_model_ids": [
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
        ],
        "migration_kind": "tts_model",
    }
    assert model_lifecycle("OpenAI", "tts-1") is None
    assert model_lifecycle("openai", "TTS-1") is None
    assert model_lifecycle("vertex_ai", "gemini-2.5-flash-preview-tts") is None


@pytest.mark.parametrize(
    ("provider_id", "model_id"),
    [
        ("openai", "tts-1"),
        ("openai", "gpt-4o-mini-tts"),
        ("gemini", "gemini-3.1-flash-tts-preview"),
        ("elevenlabs", "eleven_turbo_v2_5"),
    ],
)
def test_lifecycle_is_consistent_across_full_compact_and_detail_projections(
    provider_id, model_id
):
    current_model = "current-model"
    service = {
        "id": provider_id,
        "provider": provider_id,
        "adapter": "elevenlabs_native" if provider_id == "elevenlabs" else provider_id,
        "models": [model_id, current_model],
        "default_model": model_id,
        "voices": ["alloy"],
        "available": False,
        "online": False,
        "health_checked_at": "2026-10-12T12:00:00Z",
        "settings": {"format": "mp3"},
        "model_catalog": [
            {
                "id": model_id,
                "api_base": "https://configured.example/v1",
                "api_key": "configured-secret",
                "credential_reference": "credential-ref",
                "model_lifecycle": {
                    "provider_id": "forged",
                    "model_id": model_id,
                    "source_url": "https://configured.example/v1",
                    "api_key": "configured-secret",
                },
            },
            {
                "id": current_model,
                "model_lifecycle": {"status": "deprecated", "source_url": "fake"},
            },
        ],
    }
    original_context = {
        key: deepcopy(service[key])
        for key in (
            "models",
            "default_model",
            "voices",
            "available",
            "online",
            "health_checked_at",
            "settings",
        )
    }

    _decorate_model_language_support(service)
    full_rows = {row["id"]: row for row in service["model_catalog"]}
    expected = model_lifecycle(provider_id, model_id)
    assert expected is not None
    assert full_rows[model_id]["model_lifecycle"] == expected
    assert "model_lifecycle" not in full_rows[current_model]
    for key, value in original_context.items():
        assert service[key] == value

    compact_rows = {row["id"]: row for row in _slim_model_catalog(service)}
    assert compact_rows[model_id]["model_lifecycle"] == expected
    assert "model_lifecycle" not in compact_rows[current_model]
    assert {"api_base", "api_key", "credential_reference"}.isdisjoint(
        compact_rows[model_id]
    )
    assert "configured-secret" not in repr(compact_rows[model_id])
    assert "configured.example" not in repr(compact_rows[model_id])
    compact_lifecycle = compact_rows[model_id]["model_lifecycle"]
    assert set(compact_lifecycle) == set(expected)
    assert not {
        "api_base",
        "api_key",
        "credential_reference",
        "endpoint",
        "password",
        "secret",
        "token",
    }.intersection(compact_lifecycle)
    assert compact_lifecycle["source_url"] == expected["source_url"]
    assert "configured.example" not in compact_lifecycle["source_url"]

    detail = _filter_service_models(
        deepcopy(service), [model_id], service_id=provider_id
    )
    assert [row["id"] for row in detail["model_catalog"]] == [model_id]
    assert detail["model_catalog"][0]["model_lifecycle"] == expected
    assert detail["models"] == original_context["models"]
    assert detail["default_model"] == original_context["default_model"]
    assert detail["voices"] == original_context["voices"]


@pytest.mark.parametrize(
    ("service_id", "adapter", "provider", "model_id"),
    [
        ("azure-speech-mai-voice-2", "azure_speech", "azure", "tts-1"),
        (
            "vertex_ai",
            "vertex_ai",
            "vertex_ai",
            "gemini-2.5-flash-preview-tts",
        ),
        (
            "custom-openai-compatible",
            "openai_compatible",
            "openai",
            "tts-1",
        ),
        ("openai", "openai", "openai", "custom-model"),
    ],
)
def test_non_native_identity_or_unknown_model_cannot_claim_static_lifecycle(
    service_id, adapter, provider, model_id
):
    service = {
        "id": service_id,
        "adapter": adapter,
        "provider": provider,
        "models": [model_id],
        "model_catalog": [
            {
                "id": model_id,
                "model_lifecycle": {
                    "provider_id": "openai",
                    "model_id": "tts-1",
                    "status": "deprecated",
                    "source_url": "https://attacker.example/fake",
                },
            }
        ],
    }
    _decorate_model_language_support(service)
    assert "model_lifecycle" not in service["model_catalog"][0]
    compact = _slim_model_catalog(service)
    assert all("model_lifecycle" not in row for row in compact)
