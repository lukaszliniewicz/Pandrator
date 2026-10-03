"""Selected detail preserves the raw metadata lookup identity used by consumers."""

from copy import deepcopy

import pytest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AppSetting
from pandrator.web.tts_catalogue_projection import (
    TtsCatalogueModelNotFoundError,
    _filter_service_models,
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
