"""Created Google identities survive discovery outages without crossing scopes."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import google.auth
import pytest
from google.auth.exceptions import DefaultCredentialsError
from sqlalchemy import select

from pandrator.logic import tts_handler
from pandrator.web.credentials import ResolvedCredential
from pandrator.web.database import Database
from pandrator.web.google_voice_design import google_voice_scope
from pandrator.web.models import Voice
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from pandrator.web.tts_providers import TtsProviderRegistry
from pandrator.web.voice_catalog import catalog_entries
from pandrator.web.voice_library import mark_provider_registrations_stale, voice_payload
from tests.web_test_support import prepare_web_test_data_root

MODELS = ["gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts"]
VOICE_ID = "voice_Saved_Case"


def service(provider="gemini"):
    return {
        "id": provider,
        "api_base": tts_handler.GEMINI_AUDIO_BASE_URL if provider == "gemini" else "https://aiplatform.googleapis.com",
        "models": [*MODELS, "gemini-3.1-flash-tts-preview"],
        "voices": ["Kore"],
        "voice_catalogues": {model: ["Kore"] for model in [*MODELS, "gemini-3.1-flash-tts-preview"]},
        "vertex_project": "original-project",
        "available": True,
        "supports_prebuilt_voices": True,
    }


@pytest.fixture
def stack(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    registry = TtsProviderRegistry()
    catalogue = TtsCatalogueService(database, paths, registry)
    yield catalogue, database, paths
    catalogue.close()
    registry.close()
    database.dispose()


def save_voice(database, config, credential, *, expiry=None):
    scope = google_voice_scope(config["id"], config, credential)
    registration = {
        "status": "ready", "resource_kind": "designed_voice",
        "voice_id": VOICE_ID, "scope_fingerprint": scope["fingerprint"],
        "project": scope["project"], "models": MODELS,
        **({"expire_time": expiry} if expiry else {}),
    }
    with database.session() as session:
        row = Voice(name="Warm narrator", language="pl-PL", description="Warm, clear and calm.", metadata_json={
            "google_voice_design": {"preview_artifact_id": "saved-preview", "state": "created"},
            "providers": {config["id"]: registration},
        })
        session.add(row)
        session.flush()
        return row.id


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
def test_saved_voice_is_selectable_after_database_reopen_without_discovery(stack, provider):
    catalogue, database, paths = stack
    config = service(provider)
    credential = "fixture-key" if provider == "gemini" else '{"project_id":"original-project"}'
    voice_id = save_voice(database, config, credential)
    database.dispose()
    reopened = Database(paths.database)
    catalogue.database = reopened
    try:
        catalogue._merge_remembered_google_voices([config], [ResolvedCredential(value=credential, source="database")])
        for model in MODELS:
            assert config["voice_catalogues"][model] == ["Kore", VOICE_ID]
            assert config["voice_metadata"][f"{model}:{VOICE_ID}"]["display_name"] == "Warm narrator"
        assert config["voice_catalogues"]["gemini-3.1-flash-tts-preview"] == ["Kore"]
        with reopened.session() as session:
            row = session.get(Voice, voice_id)
            assert row is not None
            payload = voice_payload(session, paths, row)
        assert payload["origin"] == "designed"
        assert payload["preview_artifact_id"] == "saved-preview"
        assert payload["sample_count"] == 0
        assert payload["metadata_json"]["providers"][provider]["status"] == "ready"
        entries = catalog_entries([payload], {"services": [config]}, [])
        managed = next(entry for entry in entries if entry["kind"] == "managed")
        assert {binding["model"] for binding in managed["compatibility"]} == set(MODELS)
        assert all(binding["ready"] and binding["voice"] == VOICE_ID for binding in managed["compatibility"])
    finally:
        reopened.dispose()


@pytest.mark.parametrize("provider", ["gemini", "vertex_ai"])
def test_scope_change_excludes_saved_id_without_erasing_it(stack, provider):
    catalogue, database, paths = stack
    original = service(provider)
    credential = "fixture-key" if provider == "gemini" else '{"project_id":"original-project"}'
    voice_id = save_voice(database, original, credential)
    changed = deepcopy(original)
    if provider == "gemini":
        credential = "another-key"
    else:
        changed["vertex_project"] = "another-project"
    catalogue._merge_remembered_google_voices([changed], [ResolvedCredential(value=credential, source="database")])
    assert changed["managed_google_voice_ids"] == []
    assert all(VOICE_ID not in ids for ids in changed["voice_catalogues"].values())
    with database.session() as session:
        row = session.get(Voice, voice_id)
        assert row is not None
        payload = voice_payload(session, paths, row)
    managed = next(entry for entry in catalog_entries([payload], {"services": [changed]}, []) if entry["kind"] == "managed")
    assert all(not binding["ready"] for binding in managed["compatibility"])
    assert payload["metadata_json"]["providers"][provider]["voice_id"] == VOICE_ID


def test_expired_voice_is_not_offered_but_retained_in_library(stack):
    catalogue, database, _ = stack
    config = service()
    save_voice(database, config, "fixture-key", expiry="2025-01-01T00:00:00Z")
    catalogue._merge_remembered_google_voices([config], [ResolvedCredential(value="fixture-key", source="database")])
    assert config["managed_google_voice_ids"] == []
    with database.session() as session:
        assert session.scalar(select(Voice)).metadata_json["providers"]["gemini"]["voice_id"] == VOICE_ID


def test_live_expiry_extension_keeps_a_used_voice_selectable(stack):
    catalogue, database, _ = stack
    config = service()
    save_voice(database, config, "fixture-key", expiry="2025-01-01T00:00:00Z")
    config["voice_metadata"] = {
        f"{model}:{VOICE_ID}": {"expire_time": "2099-01-01T00:00:00Z"}
        for model in MODELS
    }
    catalogue._merge_remembered_google_voices([config], [ResolvedCredential(value="fixture-key", source="database")])
    assert config["managed_google_voice_ids"] == [VOICE_ID]
    for model in MODELS:
        assert VOICE_ID in config["voice_catalogues"][model]
        assert config["voice_metadata"][f"{model}:{VOICE_ID}"]["expire_time"] == "2099-01-01T00:00:00Z"


def test_reference_changes_do_not_invalidate_a_google_designed_identity(stack):
    _, database, _ = stack
    voice_id = save_voice(database, service(), "fixture-key")
    with database.session() as session:
        voice = session.get(Voice, voice_id)
        assert voice is not None
        mark_provider_registrations_stale(voice, "A new sample was imported.")
        assert voice.metadata_json["providers"]["gemini"]["status"] == "ready"


def test_adc_project_scope_is_stable_across_token_refresh(monkeypatch):
    config = service("vertex_ai")
    first = SimpleNamespace(token="first-access-token", valid=True)
    second = SimpleNamespace(token="refreshed-access-token", valid=True)
    monkeypatch.setattr(google.auth, "default", lambda **_: (first, "original-project"))
    original = google_voice_scope("vertex_ai", config, "")
    monkeypatch.setattr(google.auth, "default", lambda **_: (second, "original-project"))
    assert google_voice_scope("vertex_ai", config, "") == original


def test_unavailable_credentials_dont_disable_the_catalogue(stack, monkeypatch):
    catalogue, _, _ = stack
    def missing(**_):
        raise DefaultCredentialsError("fixture ADC unavailable")
    monkeypatch.setattr(google.auth, "default", missing)
    config = service("vertex_ai")
    config["vertex_project"] = ""
    catalogue._merge_remembered_google_voices([config], [ResolvedCredential(value="", source="environment")])
    assert config["managed_google_voice_ids"] == []


def test_no_saved_google_voices_does_not_repeat_adc_discovery(stack, monkeypatch):
    catalogue, _, _ = stack
    calls = []
    def unexpected(**_):
        calls.append(True)
        raise AssertionError("No saved identity needs a Google credential lookup.")
    monkeypatch.setattr(google.auth, "default", unexpected)
    config = service("vertex_ai")
    catalogue._merge_remembered_google_voices([config], [ResolvedCredential(value="", source="environment")])
    assert config["managed_google_voice_ids"] == []
    assert calls == []
    assert all(ids == ["Kore"] for ids in config["voice_catalogues"].values())
