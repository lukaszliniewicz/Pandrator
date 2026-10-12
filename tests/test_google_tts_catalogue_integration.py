"""Offline integration contracts for native Google TTS voice discovery."""

from __future__ import annotations

import socket
from copy import deepcopy
from typing import Any, Iterator
from urllib.parse import quote

import google.auth
import pytest
import requests
from google.auth.exceptions import DefaultCredentialsError

from pandrator.logic import google_tts_catalogue_http, tts_handler
from pandrator.web import tts_catalogue_service, tts_providers
from pandrator.web.database import Database
from pandrator.web.models import AppSetting
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from pandrator.web.tts_provider_contracts import TtsHealth
from pandrator.web.tts_providers import GoogleTtsAdapter, TtsProviderRegistry
from tests.web_test_support import prepare_web_test_data_root

MODELS = ("gemini-3.8-flash-tts", "gemini-3.8-flash-lite-tts")
OLD_GEMINI_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-preview-tts")
OLD_VERTEX_MODELS = ("gemini-3.1-flash-tts-preview", "gemini-2.5-flash-tts")
VOICE_ID = "Voice:Custom/Case+Sensitive"
VOICE_DISCOVERY_WARNING = (
    "Google voice discovery failed. The saved and prebuilt voices remain available; "
    "check credentials and refresh to load additional voices."
)
GEMINI_VOICES_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/voices"
VERTEX_TOKEN = "fixture-vertex-access-token"
VERTEX_PROJECT = "project/tenant one"


class FakeResponse:
    def __init__(self, payload: object, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def json(self) -> object:
        return self.payload


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("Google catalogue integration fixtures must not access the network")

    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)


@pytest.fixture
def registry() -> Iterator[TtsProviderRegistry]:
    providers = TtsProviderRegistry()
    yield providers
    providers.close()


@pytest.fixture
def catalogue_stack(
    tmp_path,
) -> Iterator[tuple[TtsCatalogueService, Database, TtsProviderRegistry]]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    providers = TtsProviderRegistry()
    catalogue = TtsCatalogueService(database, paths, providers)
    catalogue.manager_bridge.descriptor_path = None
    yield catalogue, database, providers
    catalogue.close()
    providers.close()
    database.dispose()


def _google_service(provider: str, *, api_base: str | None = None) -> dict[str, Any]:
    old_models = OLD_GEMINI_MODELS if provider == "gemini" else OLD_VERTEX_MODELS
    resolved_api_base = api_base or (
        tts_handler.GEMINI_AUDIO_BASE_URL
        if provider == "gemini"
        else "https://aiplatform.googleapis.com"
    )
    return {
        "id": provider,
        "name": provider,
        "provider": provider,
        "kind": "commercial",
        "api_base": resolved_api_base,
        "models": [*MODELS, *old_models],
        "default_model": MODELS[0],
        "default_voice": "Kore",
        "voices": ["Kore", "saved-global-voice"],
        "voice_catalogues": {
            MODELS[0]: ["Kore", "prebuilt-flash-voice"],
            MODELS[1]: ["prebuilt-lite-voice"],
            old_models[0]: ["saved-3.1-voice"],
            old_models[1]: ["prebuilt-2.5-voice", "saved-2.5-voice"],
        },
        "voice_metadata": {
            f"{old_models[0]}:saved-3.1-voice": {
                "id": "saved-3.1-voice",
                "model": old_models[0],
            }
        },
        "vertex_project": "configured-project",
        "vertex_location": "us-central1",
    }


def _provider_payload(provider: str) -> dict[str, object]:
    common = {
        "displayName": "Discovered custom voice",
        "languageCode": "en-GB",
        "pitch": 1.25,
        "apiKey": "must-not-leak",
        "sampleAudio": "must-not-leak",
        "unknownSecret": {"token": "must-not-leak"},
    }
    voice = (
        {"id": VOICE_ID, **common}
        if provider == "gemini"
        else {"voiceId": VOICE_ID, **common}
    )
    return {"voices": [voice]}


def _catalogue_service(snapshot: dict[str, Any], provider: str) -> dict[str, Any]:
    return next(item for item in snapshot["services"] if item["id"] == provider)


def _save_gemini_settings(database: Database, *, key_env: str) -> dict[str, Any]:
    service = _google_service("gemini")
    service["api_key"] = ""
    service["api_key_env"] = key_env
    settings = {"provider_configs": [service]}
    with database.session() as session:
        session.add(
            AppSetting(
                key="services.tts",
                value_json=deepcopy(settings),
                revision=1,
            )
        )
    return settings


def test_registry_installs_native_google_adapters(registry: TtsProviderRegistry) -> None:
    assert isinstance(registry.get("gemini"), GoogleTtsAdapter)
    assert isinstance(registry.get("vertex_ai"), GoogleTtsAdapter)
    assert registry.get("gemini").service_id == "gemini"
    assert registry.get("vertex_ai").service_id == "vertex_ai"


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_registry_discovers_only_structured_model_voices_with_safe_metadata(
    monkeypatch: pytest.MonkeyPatch,
    registry: TtsProviderRegistry,
    provider: str,
) -> None:
    service = _google_service(provider)
    original = deepcopy(service)
    api_key = "fixture-gemini-key" if provider == "gemini" else ""
    calls: list[tuple[str, dict[str, object]]] = []
    token_calls: list[dict[str, Any]] = []

    def get(endpoint: str, **options: object) -> FakeResponse:
        expected_endpoint = (
            GEMINI_VOICES_ENDPOINT
            if provider == "gemini"
            else "https://aiplatform.googleapis.com/v1beta1/projects/"
            f"{quote(VERTEX_PROJECT, safe='')}/locations/global/voices"
        )
        expected_headers = (
            {"x-goog-api-key": api_key}
            if provider == "gemini"
            else {"Authorization": f"Bearer {VERTEX_TOKEN}"}
        )
        expected_page = {"page_size": 1000} if provider == "gemini" else {"pageSize": 1000}
        assert endpoint == expected_endpoint
        assert options == {
            "headers": expected_headers,
            "params": expected_page,
            "timeout": 15,
            "allow_redirects": False,
        }
        calls.append((endpoint, dict(options)))
        return FakeResponse(_provider_payload(provider))

    monkeypatch.setattr(google_tts_catalogue_http.requests, "get", get)
    if provider == "vertex_ai":
        def access_token(config: dict[str, Any]) -> tuple[str, str]:
            token_calls.append(config)
            return VERTEX_TOKEN, VERTEX_PROJECT

        monkeypatch.setattr(tts_handler, "_vertex_access_token", access_token)

    changes = registry.enrich_catalog(service, api_key=api_key)

    assert len(calls) == 1
    if provider == "vertex_ai":
        assert token_calls == [{**service, "api_key": api_key}]
    assert changes["live_voices"] == [VOICE_ID]
    catalogues = changes["voice_catalogues"]
    assert catalogues[MODELS[0]] == ["Kore", "prebuilt-flash-voice", VOICE_ID]
    assert catalogues[MODELS[1]] == ["prebuilt-lite-voice", VOICE_ID]
    old_models = OLD_GEMINI_MODELS if provider == "gemini" else OLD_VERTEX_MODELS
    assert catalogues[old_models[0]] == ["saved-3.1-voice"]
    assert catalogues[old_models[1]] == ["prebuilt-2.5-voice", "saved-2.5-voice"]
    metadata = changes["voice_metadata"]
    expected_metadata = {
        "id": VOICE_ID,
        "display_name": "Discovered custom voice",
        "language_code": "en-GB",
        "pitch": "1.25",
        "model": MODELS[0],
    }
    assert metadata[f"{MODELS[0]}:{VOICE_ID}"] == expected_metadata
    assert metadata[f"{MODELS[1]}:{VOICE_ID}"] == {
        **expected_metadata,
        "model": MODELS[1],
    }
    assert metadata[f"{old_models[0]}:saved-3.1-voice"] == original["voice_metadata"][
        f"{old_models[0]}:saved-3.1-voice"
    ]
    assert "must-not-leak" not in repr(changes)
    assert "unknownSecret" not in repr(changes)
    assert service == original


def test_custom_gemini_base_and_missing_key_skip_discovery(
    monkeypatch: pytest.MonkeyPatch,
    registry: TtsProviderRegistry,
) -> None:
    calls: list[bool] = []

    def unexpected(*args: object, **kwargs: object) -> list[dict[str, object]]:
        calls.append(True)
        raise AssertionError("custom API base and missing key must skip voice discovery")

    monkeypatch.setattr(tts_providers, "get_google_voice_catalog", unexpected)
    adapter = registry.get("gemini")
    custom_service = _google_service("gemini", api_base="https://custom-google.example/openai")
    assert adapter.capabilities(custom_service).dynamic_catalog is False
    assert adapter.enrich_catalog(custom_service, api_key="fixture-key") == {}

    native_service = _google_service("gemini")
    assert adapter.capabilities(native_service).dynamic_catalog is True
    assert adapter.enrich_catalog(native_service, api_key="") == {}
    assert calls == []


@pytest.mark.parametrize("provider", ("gemini", "vertex_ai"))
def test_google_discovery_errors_are_sanitized_and_do_not_disable_availability(
    monkeypatch: pytest.MonkeyPatch,
    registry: TtsProviderRegistry,
    provider: str,
) -> None:
    service = _google_service(provider)
    service["available"] = True
    original = deepcopy(service)
    secret = "fixture-provider-secret"

    def fail_helper(*args: object, **kwargs: object) -> list[dict[str, object]]:
        raise RuntimeError(f"{secret} https://private.example/token")

    if provider == "gemini":
        monkeypatch.setattr(tts_providers, "get_google_voice_catalog", fail_helper)
        api_key = secret
    else:
        def fail_token(config: dict[str, Any]) -> tuple[str, str]:
            raise RuntimeError(f"{secret} private-project")

        monkeypatch.setattr(tts_handler, "_vertex_access_token", fail_token)
        api_key = ""

    changes = registry.get(provider).enrich_catalog(service, api_key=api_key)
    assert changes == {"voice_catalogue_warning": VOICE_DISCOVERY_WARNING}
    assert secret not in repr(changes)
    assert "private-project" not in repr(changes)
    assert "private.example" not in repr(changes)
    assert "available" not in changes
    assert service == original
    assert service["available"] is True


def test_native_discovery_cache_identity_tracks_credentials_project_and_settings() -> None:
    service = _google_service("vertex_ai")
    settings = {
        "provider_configs": [
            {"id": "vertex_ai", "vertex_project": "project-one"}
        ],
        "catalogue_setting": "first",
    }
    baseline = TtsCatalogueService._native_discovery_key(
        service, settings, "fixture-credential-one"
    )

    assert baseline is not None
    assert baseline != TtsCatalogueService._native_discovery_key(
        service, settings, "fixture-credential-two"
    )
    assert baseline != TtsCatalogueService._native_discovery_key(
        service,
        {
            "provider_configs": [
                {"id": "vertex_ai", "vertex_project": "project-two"}
            ],
            "catalogue_setting": "first",
        },
        "fixture-credential-one",
    )
    assert baseline != TtsCatalogueService._native_discovery_key(
        service,
        {
            "provider_configs": [
                {"id": "vertex_ai", "vertex_project": "project-one"}
            ],
            "catalogue_setting": "second",
        },
        "fixture-credential-one",
    )
    assert "fixture-credential-one" not in baseline


def test_snapshot_caches_successful_google_discovery_without_writing_settings(
    monkeypatch: pytest.MonkeyPatch,
    catalogue_stack: tuple[TtsCatalogueService, Database, TtsProviderRegistry],
) -> None:
    catalogue, database, providers = catalogue_stack
    key_env = "PANDRATOR_TEST_GOOGLE_TTS_KEY"
    api_key = "fixture-snapshot-gemini-key"
    monkeypatch.setenv(key_env, api_key)
    original_settings = _save_gemini_settings(database, key_env=key_env)
    monkeypatch.setattr(
        providers,
        "health",
        lambda _service: TtsHealth(True, True, ""),
    )
    calls: list[tuple[str, dict[str, object]]] = []

    def get(endpoint: str, **options: object) -> FakeResponse:
        calls.append((endpoint, dict(options)))
        return FakeResponse(_provider_payload("gemini"))

    monkeypatch.setattr(google_tts_catalogue_http.requests, "get", get)
    with database.session() as session:
        persisted = session.get(AppSetting, "services.tts")
        assert persisted is not None
        original_revision = persisted.revision
        original_value = deepcopy(persisted.value_json)

    refreshed, _ = catalogue.snapshot(
        refresh=True,
        service_ids=("gemini",),
        include_profiles=False,
    )
    refreshed_service = _catalogue_service(refreshed, "gemini")
    assert refreshed_service["available"] is True
    assert refreshed_service["live_voices"] == [VOICE_ID]
    assert VOICE_ID in refreshed_service["voice_catalogues"][MODELS[0]]
    assert calls == [(GEMINI_VOICES_ENDPOINT, {
        "headers": {"x-goog-api-key": api_key},
        "params": {"page_size": 1000},
        "timeout": 15,
        "allow_redirects": False,
    })]

    cached, _ = catalogue.snapshot(
        service_ids=("gemini",),
        include_profiles=False,
    )
    cached_service = _catalogue_service(cached, "gemini")
    assert cached_service["live_voices"] == [VOICE_ID]
    assert VOICE_ID in cached_service["voice_catalogues"][MODELS[1]]
    assert len(calls) == 1
    with database.session() as session:
        persisted = session.get(AppSetting, "services.tts")
        assert persisted is not None
        assert persisted.revision == original_revision
        assert persisted.value_json == original_value == original_settings


def test_snapshot_does_not_cache_google_discovery_errors(
    monkeypatch: pytest.MonkeyPatch,
    catalogue_stack: tuple[TtsCatalogueService, Database, TtsProviderRegistry],
) -> None:
    catalogue, database, providers = catalogue_stack
    key_env = "PANDRATOR_TEST_GOOGLE_TTS_KEY"
    monkeypatch.setenv(key_env, "fixture-snapshot-gemini-key")
    _save_gemini_settings(database, key_env=key_env)
    monkeypatch.setattr(
        providers,
        "health",
        lambda _service: TtsHealth(True, True, ""),
    )
    calls: list[int] = []

    def get(_endpoint: str, **_options: object) -> FakeResponse:
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            return FakeResponse(
                {"error": {"message": "fixture-private-token"}}, status_code=503
            )
        return FakeResponse(_provider_payload("gemini"))

    monkeypatch.setattr(google_tts_catalogue_http.requests, "get", get)
    failed, _ = catalogue.snapshot(
        refresh=True,
        service_ids=("gemini",),
        include_profiles=False,
    )
    failed_service = _catalogue_service(failed, "gemini")
    assert failed_service["available"] is True
    assert failed_service["voice_catalogue_warning"] == VOICE_DISCOVERY_WARNING
    assert "fixture-private-token" not in repr(failed)

    uncached, _ = catalogue.snapshot(
        service_ids=("gemini",),
        include_profiles=False,
    )
    uncached_service = _catalogue_service(uncached, "gemini")
    # Successful health is cached independently of failed voice discovery.
    assert uncached_service["available"] is True
    assert uncached_service.get("voice_catalogue_warning", "") == ""
    assert VOICE_ID not in uncached_service.get("live_voices", [])
    assert len(calls) == 1

    recovered, _ = catalogue.snapshot(
        refresh=True,
        service_ids=("gemini",),
        include_profiles=False,
    )
    recovered_service = _catalogue_service(recovered, "gemini")
    assert recovered_service["live_voices"] == [VOICE_ID]
    assert len(calls) == 2


@pytest.mark.parametrize(
    ("detected_project", "configured_project", "adc_available", "expected"),
    [
        ("detected-project", "", True, True),
        (None, "configured-project", True, True),
        (None, "", False, False),
    ],
)
def test_vertex_adc_credentials_are_decorated_without_refreshing(
    monkeypatch: pytest.MonkeyPatch,
    catalogue_stack: tuple[TtsCatalogueService, Database, TtsProviderRegistry],
    detected_project: str | None,
    configured_project: str,
    adc_available: bool,
    expected: bool,
) -> None:
    catalogue, _database, _providers = catalogue_stack
    default_calls: list[dict[str, object]] = []

    class CredentialsWithoutRefresh:
        def refresh(self, _request: object) -> None:
            raise AssertionError("credential decoration must not refresh ADC")

    def default_credentials(**options: object) -> tuple[object, str | None]:
        default_calls.append(dict(options))
        if not adc_available:
            raise DefaultCredentialsError("fixture-adc-secret")
        return CredentialsWithoutRefresh(), detected_project

    monkeypatch.setattr(
        tts_catalogue_service,
        "provider_credential_status",
        lambda *_args, **_kwargs: {
            "credential_configured": False,
            "credential_source": "none",
        },
    )
    monkeypatch.setattr(google.auth, "default", default_credentials)
    service = {
        "id": "vertex_ai",
        "kind": "commercial",
        "vertex_project": configured_project,
    }

    catalogue._decorate_credentials(service)

    assert service["credential_configured"] is expected
    assert service["credential_source"] == (
        "application_default" if expected else "none"
    )
    assert default_calls == [
        {"scopes": ["https://www.googleapis.com/auth/cloud-platform"]}
    ]
