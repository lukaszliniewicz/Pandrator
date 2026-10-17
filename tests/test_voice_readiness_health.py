from unittest.mock import Mock

import pytest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import ResolvedCredential
from pandrator.web.models import AppSetting
from pandrator.web.tts_catalogue_projection import _project_compact_service
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from pandrator.web.tts_provider_contracts import TtsHealth, TtsProviderError
from pandrator.web.voice_catalog import catalog_entries
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def catalogue_case(tmp_path, monkeypatch):
    prepare_web_test_data_root(tmp_path)
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=BootstrapTokenStore(),
    )
    services = app.extensions["pandrator"]
    catalogue = services["tts_catalogue"]
    catalogue.manager_bridge.descriptor_path = None
    monkeypatch.setattr(
        catalogue.manager_bridge,
        "inventory",
        lambda: {"components": [], "services": []},
    )
    yield catalogue, services
    catalogue.close()
    catalogue.providers.close()
    services["database"].dispose()


def _configure_audio_cpp(services):
    with services["database"].session() as session:
        session.add(
            AppSetting(
                key="services.tts",
                value_json={
                    "provider_configs": [
                        {
                            "id": "audio_cpp",
                            "adapter": "audio_cpp",
                            "connection_mode": "external",
                            "api_base": "http://native.invalid:8060",
                            "models": ["fixture-model"],
                            "model_catalog": [{"id": "fixture-model"}],
                        }
                    ]
                },
                revision=1,
            )
        )


def test_health_refresh_is_reused_without_probe_until_expiry(
    catalogue_case, monkeypatch
):
    catalogue, services = catalogue_case
    _configure_audio_cpp(services)
    clock = [100.0]
    monkeypatch.setattr(
        "pandrator.web.tts_catalogue_service.monotonic", lambda: clock[0]
    )
    health = Mock(return_value=TtsHealth(True, True))
    monkeypatch.setattr(catalogue.providers, "health", health)
    monkeypatch.setattr(
        catalogue.providers,
        "enrich_catalog",
        lambda _service, **_kwargs: {
            "models": ["fixture-model"],
            "model_catalog": [{"id": "fixture-model"}],
        },
    )

    refreshed, _ = catalogue.snapshot(
        refresh=True, service_ids=["audio_cpp"], include_profiles=False
    )
    first = refreshed["services"][0]
    clock[0] = 159.0
    cached, _ = catalogue.snapshot(service_ids=["audio_cpp"], include_profiles=False)
    second = cached["services"][0]

    assert second["available"] is True
    assert second["online"] is True
    assert second["health_checked_at"] == first["health_checked_at"]
    assert health.call_count == 1

    clock[0] = 160.0
    expired, _ = catalogue.snapshot(
        service_ids=["audio_cpp"], include_profiles=False
    )
    stale = expired["services"][0]
    assert stale["available"] is None
    assert stale["online"] is None
    assert stale["availability_reason"] is None
    assert stale["health_checked_at"] is None
    assert health.call_count == 1

    def fail_enrichment(_service, **_kwargs):
        raise TtsProviderError(
            "audio_cpp", "catalogue", "Fixture catalogue error", retryable=True
        )

    monkeypatch.setattr(catalogue.providers, "enrich_catalog", fail_enrichment)
    failed, _ = catalogue.snapshot(
        refresh=True, service_ids=["audio_cpp"], include_profiles=False
    )
    failed_service = failed["services"][0]
    reused_failure, _ = catalogue.snapshot(
        service_ids=["audio_cpp"], include_profiles=False
    )
    reused_service = reused_failure["services"][0]
    assert failed_service["online"] is True
    assert failed_service["available"] is True
    assert failed_service["availability_reason"] == ""
    assert failed_service["catalogue"]["complete"] is False
    assert failed_service["catalogue"]["error"] == "Fixture catalogue error"
    assert reused_service["online"] is True
    assert reused_service["available"] is True
    assert reused_service["availability_reason"] == ""
    assert reused_service["catalogue"]["complete"] is False
    assert reused_service["catalogue"]["error"] == "Fixture catalogue error"
    assert failed_service["health_checked_at"] is not None
    assert reused_service["health_checked_at"] == failed_service["health_checked_at"]
    assert health.call_count == 2


def test_health_observation_identity_changes_with_credential_and_is_bounded(
    catalogue_case,
):
    catalogue, _services = catalogue_case
    service = {
        "id": "fixture-provider",
        "api_base": "https://provider.invalid/v1",
        "connection_mode": "external",
    }
    settings = {"provider_configs": [{"id": "fixture-provider"}]}
    first_key = TtsCatalogueService._health_observation_key(
        service, settings, "fixture-credential-one"
    )
    second_key = TtsCatalogueService._health_observation_key(
        service, settings, "fixture-credential-two"
    )
    assert first_key is not None and second_key is not None
    assert first_key != second_key

    catalogue._remember_health_observations(
        [
            {
                "available": False,
                "online": True,
                "availability_reason": (
                    "credential=fixture-secret-value at "
                    "/fixture/private/credential.txt"
                ),
            }
        ],
        [first_key],
        [("fixture-secret-value", "/fixture/private/credential.txt")],
    )
    same_identity = dict(service)
    catalogue._apply_health_observations([same_identity], [first_key])
    assert "fixture-secret-value" not in same_identity["availability_reason"]
    assert "/fixture/private/credential.txt" not in same_identity["availability_reason"]

    changed_identity = dict(service)
    catalogue._apply_health_observations([changed_identity], [second_key])
    assert changed_identity["available"] is None
    assert changed_identity["online"] is None
    assert changed_identity["health_checked_at"] is None

    keys = [f"health-{index}" for index in range(130)]
    services = [
        {"available": True, "online": True, "availability_reason": None}
        for _ in keys
    ]
    catalogue._remember_health_observations(services, keys, [() for _ in keys])
    assert len(catalogue._health_observations) == 128
    assert "health-0" not in catalogue._health_observations


def _managed_voice(identifier, *, sample_count, registration=None):
    return {
        "id": identifier,
        "name": identifier,
        "revision": 1,
        "available_sample_count": sample_count,
        "metadata_json": {
            "providers": {"fixture-provider": registration} if registration else {}
        },
    }


def _clone_service(
    *, available=None, health_checked_at=None, reason=None, listed=True, catalogue=None
):
    service = {
        "id": "fixture-provider",
        "models": ["clone-model"] if listed else [],
        "available": available,
        "model_catalog": [{"id": "clone-model", "voice_mode": "cloning"}],
    }
    if health_checked_at is not None:
        service["health_checked_at"] = health_checked_at
        service["availability_reason"] = reason
    if catalogue is not None:
        service["catalogue"] = catalogue
    return service


@pytest.mark.parametrize(
    ("available", "checked_at", "status", "service_available"),
    [
        (None, None, "unknown", None),
        (False, "2026-10-08T10:00:00+00:00", "unavailable", False),
        (True, "2026-10-08T10:00:00+00:00", "ready", True),
    ],
)
def test_prepared_reference_is_independent_of_health(
    available, checked_at, status, service_available
):
    voice = _managed_voice(
        "managed-voice",
        sample_count=1,
        registration={"status": "ready", "voice_id": "provider-voice"},
    )
    entry = catalog_entries(
        [voice],
        {
            "services": [
                _clone_service(
                    available=available,
                    health_checked_at=checked_at,
                    reason="provider offline" if checked_at and not available else None,
                )
            ]
        },
        [],
    )[0]
    compatibility = entry["compatibility"][0]

    assert compatibility["status"] == status
    assert compatibility["ready"] is (status == "ready")
    assert compatibility["prepared"] is True
    assert compatibility["service_available"] is service_available
    assert compatibility["health_checked_at"] == checked_at
    assert compatibility["availability_reason"] == (
        "provider offline" if checked_at and not available else None
    )


@pytest.mark.parametrize(
    ("sample_count", "registration", "status"),
    [
        (0, {"status": "ready", "voice_id": "provider-voice"}, "needs_reference"),
        (1, None, "needs_link"),
    ],
)
def test_unprepared_reference_states_remain_unprepared(
    sample_count, registration, status
):
    entry = catalog_entries(
        [_managed_voice("managed-voice", sample_count=sample_count, registration=registration)],
        {"services": [_clone_service(available=True)]},
        [],
    )[0]
    compatibility = entry["compatibility"][0]
    assert compatibility["status"] == status
    assert compatibility["prepared"] is False


def test_missing_local_model_does_not_undo_reference_preparation():
    voice = _managed_voice(
        "managed-voice",
        sample_count=1,
        registration={"status": "ready", "voice_id": "provider-voice"},
    )
    entry = catalog_entries(
        [voice],
        {
            "services": [
                _clone_service(
                    available=True,
                    listed=False,
                    catalogue={
                        "source": "runtime",
                        "scope": "service",
                        "service_id": "fixture-provider",
                        "checked_at": "2026-10-17T00:00:00+00:00",
                        "complete": True,
                        "error": None,
                    },
                )
            ]
        },
        [],
    )[0]
    compatibility = entry["compatibility"][0]
    assert compatibility["status"] == "model_unavailable"
    assert compatibility["ready"] is False
    assert compatibility["prepared"] is True


def test_scope_mismatch_stays_blocked_and_stock_provider_voice_is_prepared():
    managed = _managed_voice(
        "managed-voice",
        sample_count=1,
        registration={
            "status": "ready",
            "resource_kind": "designed_voice",
            "voice_id": "voice_fixture",
        },
    )
    managed["metadata_json"]["providers"] = {
        "gemini": managed["metadata_json"]["providers"].pop("fixture-provider")
    }
    gemini = {
        "id": "gemini",
        "models": ["gemini-3.8-flash-tts"],
        "available": None,
        "model_catalog": [
            {"id": "gemini-3.8-flash-tts", "voice_mode": "prebuilt"}
        ],
        "managed_google_voice_ids": [],
    }
    mismatch = catalog_entries([managed], {"services": [gemini]}, [])[0][
        "compatibility"
    ][0]
    assert mismatch["status"] == "scope_mismatch"
    assert mismatch["prepared"] is False

    stock = catalog_entries(
        [],
        {
            "services": [
                {
                    "id": "fixture-provider",
                    "models": ["stock-model"],
                    "model_catalog": [
                        {"id": "stock-model", "voice_mode": "prebuilt"}
                    ],
                    "voice_catalogues": {"stock-model": ["stock-voice"]},
                    "available": None,
                }
            ]
        },
        [],
    )[0]["compatibility"][0]
    assert stock["status"] == "unknown"
    assert stock["prepared"] is True


@pytest.mark.parametrize(
    ("source", "configured", "origin"),
    [
        ("database", True, "database"),
        ("environment", True, "environment"),
        ("keyring", True, "keyring"),
        ("file", True, "file"),
        ("none", False, "unconfigured"),
        ("unavailable", False, "unknown"),
        ("application_default", True, "application_default"),
    ],
)
def test_credential_origin_uses_resolver_metadata_only(
    catalogue_case, source, configured, origin
):
    catalogue, _services = catalogue_case
    service = {
        "id": "elevenlabs",
        "secret_ref": "file-path:/fixture/private/credential.txt",
    }
    credential = ResolvedCredential(
        value="fixture-secret-value" if configured else "",
        source=source,
    )
    catalogue._decorate_credentials(service, resolved_credential=credential)
    projected = _project_compact_service(service)

    assert service["credential_origin"] == origin
    assert projected["credential_origin"] == origin
    assert "fixture-secret-value" not in origin
    assert "/fixture/private" not in origin
