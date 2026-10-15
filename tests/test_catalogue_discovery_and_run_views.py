"""Native discovery retention and opt-in generation response projections."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, func, select

from pandrator.logic import tts_handler
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AppSetting, Artifact, GenerationRun
from pandrator.web.tts_provider_contracts import TtsHealth, TtsProviderError
from pandrator.web.voice_catalog import (
    VoiceCatalogQuery,
    catalog_entries,
    model_modes,
    query_catalog,
)
from pandrator_mcp.schemas import ListGenerationRunsInput
from pandrator_mcp.tools.e2e import list_generation_runs
from tests.web_test_support import prepare_web_test_data_root

BREEZE = "breeze_tts_2_q8_0"


@pytest.fixture
def app_case(tmp_path, monkeypatch):
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}).get_json()["csrf_token"]
    services = app.extensions["pandrator"]
    services["tts_catalogue"].manager_bridge.descriptor_path = None
    monkeypatch.setattr(services["tts_catalogue"].manager_bridge, "inventory", lambda: {"components": [], "services": []})
    yield client, {"X-CSRF-Token": csrf}, services
    services["tts_catalogue"].close()
    services["tts_catalogue"].providers.close()
    services["database"].dispose()


def configure_old_native(services):
    with services["database"].session() as session:
        row = session.get(AppSetting, "services.tts")
        value = {"provider_configs": [{
            "id": "audio_cpp", "adapter": "audio_cpp", "connection_mode": "external",
            "api_base": "http://native.invalid:8060", "models": ["old_model"],
            "model_catalog": [{"id": "old_model"}], "default_model": "old_model",
        }]}
        if row is None:
            session.add(AppSetting(key="services.tts", value_json=value, revision=1))
        else:
            row.value_json = value
    return value


def test_native_discovery_survives_read_failure_expiry_and_config_changes(app_case, monkeypatch):
    client, _headers, services = app_case
    config = configure_old_native(services)
    catalogue = services["tts_catalogue"]
    clock = [100.0]
    monkeypatch.setattr("pandrator.web.tts_catalogue_service.monotonic", lambda: clock[0])
    monkeypatch.setattr(catalogue.providers, "health", lambda _service: TtsHealth(True, True))
    models = Mock(return_value=[{"id": BREEZE, "task": "vdes", "loaded": False}])
    monkeypatch.setattr(tts_handler, "get_audio_cpp_model_catalog", models)
    monkeypatch.setattr(tts_handler, "get_audio_cpp_voice_catalog", lambda *_args, **_kwargs: [])

    initial, _ = catalogue.snapshot(service_ids=["audio_cpp"], include_profiles=False)
    assert BREEZE not in initial["services"][0]["models"]
    assert initial["services"][0]["catalogue"] == {
        "source": "configured", "scope": "service", "service_id": "audio_cpp",
        "checked_at": None, "complete": False, "error": None,
    }
    refreshed, _ = catalogue.snapshot(refresh=True, service_ids=["audio_cpp"], include_profiles=False)
    native = refreshed["services"][0]
    assert native["models"] == [BREEZE]
    provenance = native["catalogue"]
    assert provenance["source"] == "runtime"
    assert provenance["complete"] is True
    assert provenance["error"] is None
    assert datetime.fromisoformat(provenance["checked_at"]).utcoffset() == timedelta(0)
    assert native["model_catalog"][0]["loaded"] is False
    native_mode = model_modes(native, native["model_catalog"][0])

    for _ in range(2):
        capabilities = client.get(f"/api/v1/voice-catalog/capabilities?service_id=AUDIO_CPP&model={BREEZE.upper()}").get_json()
        assert len(capabilities["models"]) == 1
        assert capabilities["models"][0]["model"] == BREEZE
        assert capabilities["models"][0]["listed"] is True
        assert capabilities["models"][0]["modes"] == native_mode
    assert models.call_count == 1  # Reads never rediscover the provider.
    assert client.get("/api/v1/voice-catalog/capabilities?model=unknown").get_json()["models"] == []
    compact, _ = catalogue.snapshot(view="compact", service_ids=["audio_cpp"])
    assert compact["services"][0]["models"] == [BREEZE]
    assert compact["services"][0]["catalogue"] == {**provenance, "source": "cache"}
    assert "loaded" not in compact["services"][0]["model_catalog"][0]

    def fail(_service, **_kwargs):
        raise TtsProviderError("audio_cpp", "catalogue", "Native catalogue failed", retryable=True)

    monkeypatch.setattr(catalogue.providers, "enrich_catalog", fail)
    clock[0] = 150.0
    failed, _ = catalogue.snapshot(refresh=True, service_ids=["audio_cpp"], include_profiles=False)
    assert failed["services"][0]["models"] == [BREEZE]
    assert failed["services"][0]["available"] is True
    assert "Native catalogue failed" in failed["services"][0]["catalogue"]["error"]
    assert failed["services"][0]["catalogue"]["complete"] is False
    assert len(catalogue._native_discovery) == 1
    read, _ = catalogue.snapshot(service_ids=["audio_cpp"], include_profiles=False)
    assert read["services"][0]["model_catalog"][0]["loaded"] is False
    assert read["services"][0]["catalogue"]["complete"] is False
    assert "Native catalogue failed" in read["services"][0]["catalogue"]["error"]
    assert read["services"][0]["catalogue"]["checked_at"] == provenance["checked_at"]
    monkeypatch.setattr(catalogue.providers, "health", lambda _service: TtsHealth(False, False, "offline"))
    offline, _ = catalogue.snapshot(refresh=True, service_ids=["audio_cpp"], include_profiles=False)
    assert offline["services"][0]["models"] == [BREEZE]
    assert offline["services"][0]["available"] is False
    assert offline["services"][0]["online"] is False

    clock[0] = 400.0
    expired, _ = catalogue.snapshot(service_ids=["audio_cpp"], include_profiles=False)
    assert expired["services"][0]["models"] == ["old_model"]
    assert expired["services"][0]["catalogue"]["source"] == "configured"
    assert expired["services"][0]["catalogue"]["checked_at"] is None
    assert expired["services"][0]["catalogue"]["complete"] is False
    assert not catalogue._native_discovery

    monkeypatch.setattr(catalogue.providers, "enrich_catalog", lambda _service, **_kwargs: {
        "models": [BREEZE], "model_catalog": [{"id": BREEZE}],
    })
    monkeypatch.setattr(catalogue.providers, "health", lambda _service: TtsHealth(True, True))
    catalogue.snapshot(refresh=True, service_ids=["audio_cpp"], include_profiles=False)
    original_config = deepcopy(config)
    for field, value in (("api_base", "http://other.invalid:8060"), ("models", ["different_model"])):
        config = deepcopy(original_config)
        config["provider_configs"][0][field] = value
        with services["database"].session() as session:
            session.get(AppSetting, "services.tts").value_json = config
        changed, _ = catalogue.snapshot(service_ids=["audio_cpp"], include_profiles=False)
        assert BREEZE not in changed["services"][0]["models"]

    for index in range(70):
        catalogue._retain_native_discovery(str(index), {"models": [BREEZE], "model_catalog": [{"id": BREEZE}]})
    assert len(catalogue._native_discovery) == 64


@pytest.mark.parametrize("first_view,second_view", [("full", "compact"), ("compact", "full")])
def test_admission_replay_projects_full_receipt_without_duplicate_run(app_case, first_view, second_view):
    client, headers, services = app_case
    session_id = client.post("/api/v1/sessions", json={"name": "Run view", "workflow_kind": "audiobook"}, headers=headers).get_json()["id"]
    plan = client.post(f"/api/v1/sessions/{session_id}/generation-plan", json={"segments": [{"text": "Hello there."}]}, headers=headers)
    assert plan.status_code == 201
    url = f"/api/v1/sessions/{session_id}/generation-runs"
    mutation_headers = {**headers, "Idempotency-Key": "run-view:replay:1"}
    invalid = client.post(f"{url}?view=invalid", json={}, headers=mutation_headers)
    assert invalid.status_code == 422
    first = client.post(f"{url}?view={first_view}", json={}, headers=mutation_headers)
    assert first.status_code == 202, first.get_json()
    second = client.post(f"{url}?view={second_view}", json={}, headers=mutation_headers)
    assert second.status_code == 202, second.get_json()
    assert second.headers["Idempotency-Replayed"] == "true"
    full = first.get_json() if first_view == "full" else second.get_json()
    compact = first.get_json() if first_view == "compact" else second.get_json()
    assert "settings_snapshot" in full
    assert compact == {key: value for key, value in full.items() if key != "settings_snapshot"}
    with services["database"].session() as session:
        assert session.scalar(select(func.count()).select_from(GenerationRun).where(GenerationRun.session_id == session_id)) == 1
    full_list = client.get(url).get_json()["items"]
    compact_list = client.get(f"{url}?view=compact").get_json()["items"]
    assert compact_list == [{key: value for key, value in item.items() if key != "settings_snapshot"} for item in full_list]
    assert client.get(f"{url}?view=invalid").status_code == 422


def test_mcp_run_compact_preserves_error_progress_selection_and_repair_fields():
    item = {
        "id": "run-1", "job_id": "job-1", "status": "failed", "error_message": "provider failure",
        "phase": "repairing_timing", "progress": 0.4, "queued_segment_ids": ["segment-1"],
        "source_generation_run_id": "source-1", "plan_revision_id": "plan-1",
        "result_generation_run_id": "repair-1", "timing_repair": {"status": "failed"},
        "settings_snapshot": {"tts": {"provider_configs": [{"id": "heavy"}]}},
    }
    application = Mock()
    application.list_generation_runs.return_value = {"items": [item]}
    runtime = SimpleNamespace(require_application=lambda: application)
    compact = list_generation_runs(runtime, ListGenerationRunsInput(session_id="session-1"))
    application.list_generation_runs.assert_called_once_with("session-1", limit=20, include_repairs=False, view="compact")
    assert compact["items"] == [{key: value for key, value in item.items() if key != "settings_snapshot"}]
    full = list_generation_runs(runtime, ListGenerationRunsInput(session_id="session-1", view="full"))
    assert full["items"] == [item]


def test_preview_projection_retains_latest_file_and_catalog_cursor_semantics(app_case):
    _client, _headers, services = app_case
    catalogue = services["tts_catalogue"]
    now = datetime.now(timezone.utc)
    with services["database"].session() as session:
        for index, voice in enumerate(("alloy", "alloy", "echo", "", "missing")):
            path = services["paths"].uploads / f"preview-{index}.wav"
            if voice != "missing":
                path.write_bytes(b"fixture")
            session.add(Artifact(id=f"preview-{index}", kind="audio", role="tts_voice_preview", relative_path=str(path.relative_to(services["paths"].root)), metadata_json={"service_id": "openai", "model": "tts-1", "voice": voice, "preview_text": f"Preview {index}", "huge_unused": "x" * 20000}, updated_at=now + timedelta(seconds=index)))
    loaded = []

    def record_load(artifact, _context):
        loaded.append(artifact.id)

    event.listen(Artifact, "load", record_load)
    try:
        previews = catalogue._previews()
    finally:
        event.remove(Artifact, "load", record_load)
    assert not loaded  # No whole ORM artifacts or metadata dictionaries loaded.
    assert [preview["artifact_id"] for preview in previews] == ["preview-2", "preview-1", "preview-0"]
    assert all("huge_unused" not in preview for preview in previews)
    catalog = {"services": [{"id": "openai", "models": ["tts-1"], "voices": ["alloy", "echo"]}], "previews": previews}
    entries = catalog_entries([], catalog, [])
    assert next(item for item in entries if item["id"] == "alloy")["preview_artifact_id"] == "preview-1"
    old_shape = deepcopy(catalog)
    old_shape["previews"] = [*previews, {"service_id": "openai", "model": "tts-1", "voice": "alloy", "artifact_id": "older"}]
    assert entries == catalog_entries([], old_shape, [])
    page = query_catalog(entries, VoiceCatalogQuery(limit=1))
    other = query_catalog(catalog_entries([], old_shape, []), VoiceCatalogQuery(limit=1, cursor=page["next_cursor"]))
    assert other["catalog_revision"] == page["catalog_revision"]
    assert other["facets"] == page["facets"]


@pytest.mark.parametrize("warning", [False, True])
def test_discovery_failure_preserves_health_and_redacts_catalogue_error(app_case, monkeypatch, warning):
    _client, _headers, services = app_case
    config = configure_old_native(services)
    config["provider_configs"][0]["api_key_env"] = "PANDRATOR_TEST_CATALOGUE_KEY"
    monkeypatch.setenv("PANDRATOR_TEST_CATALOGUE_KEY", "catalogue-secret-value")
    with services["database"].session() as session:
        session.get(AppSetting, "services.tts").value_json = config
    catalogue = services["tts_catalogue"]
    health = Mock(return_value=TtsHealth(True, True))
    message = "catalogue-secret-value api_key=another-secret " + "x" * 600

    def enrich(service, *, api_key):
        assert service["id"] == "audio_cpp"
        assert api_key == "catalogue-secret-value"
        if warning:
            return {"voice_catalogue_warning": message}
        raise TtsProviderError("audio_cpp", "catalogue", message, retryable=True)

    discovery = Mock(side_effect=enrich)
    monkeypatch.setattr(catalogue.providers, "health", health)
    monkeypatch.setattr(catalogue.providers, "enrich_catalog", discovery)
    # Passive reads must not contact health or discovery.
    passive, _ = catalogue.snapshot(service_ids=["audio_cpp"], include_profiles=False)
    assert passive["services"][0]["catalogue"]["complete"] is False
    health.assert_not_called()
    discovery.assert_not_called()
    fresh, _ = catalogue.snapshot(refresh=True, service_ids=["audio_cpp"], include_profiles=False)
    row = fresh["services"][0]
    assert row["available"] is row["online"] is True
    assert not row["availability_reason"]
    assert row["health_checked_at"]
    assert row["catalogue"]["complete"] is False
    assert len(row["catalogue"]["error"]) <= 500
    assert "catalogue-secret-value" not in row["catalogue"]["error"]
    assert "another-secret" not in row["catalogue"]["error"]
    assert "catalogue-secret-value" not in str(row)
    assert "another-secret" not in str(row)
    assert health.call_count == discovery.call_count == 1


@pytest.mark.parametrize(
    ("provenance", "available", "expected"),
    [
        (None, True, "unknown"),
        ({"complete": False, "error": None}, True, "unknown"),
        ({"complete": True, "error": "Discovery failed"}, True, "unknown"),
        ({"complete": True, "error": None}, True, "unknown"),
        ({"source": "runtime", "scope": "service", "service_id": "audio_cpp", "checked_at": "2026-10-15T00:00:00+00:00", "complete": True, "error": None}, True, "model_unavailable"),
        ({"source": "cache", "scope": "service", "service_id": "audio_cpp", "checked_at": "2026-10-15T00:00:00+00:00", "complete": True, "error": None}, True, "model_unavailable"),
        ({"source": "runtime", "scope": "model", "service_id": "audio_cpp", "checked_at": "2026-10-15T00:00:00+00:00", "complete": True, "error": None}, True, "unknown"),
        ({"source": "runtime", "scope": "service", "service_id": "gemini", "checked_at": "2026-10-15T00:00:00+00:00", "complete": True, "error": None}, True, "unknown"),
        ({"complete": False, "error": "Discovery failed"}, False, "unavailable"),
    ],
)
def test_managed_absent_model_requires_complete_discovery(provenance, available, expected):
    service = {
        "id": "audio_cpp", "models": ["listed"],
        "model_catalog": [{"id": "missing", "voice_mode": "cloning"}],
        "available": available, "catalogue": provenance,
    }
    voice = {
        "id": "voice", "name": "Voice", "revision": 1, "available_sample_count": 1,
        "metadata_json": {"providers": {"audio_cpp": {"status": "ready", "voice_id": "linked"}}},
    }
    entry = catalog_entries([voice], {"services": [service]}, [])[0]
    compatibility = next(item for item in entry["compatibility"] if item["model"] == "missing")
    assert compatibility["status"] == expected
    assert compatibility["catalogue"] == provenance


def test_scoped_mcp_configuration_reaches_backend_without_unrelated_discovery(app_case, monkeypatch):
    from pandrator_mcp.clients.application_voices import ApplicationVoiceMethods
    from pandrator_mcp.schemas.e2e import ConfigureTtsInput
    from pandrator_mcp.tools.e2e import configure_tts

    client, _headers, services = app_case
    configure_old_native(services)
    catalogue = services["tts_catalogue"]
    requests = []

    class Application(ApplicationVoiceMethods):
        def _request_json(self, path, *, parameters=None, **kwargs):
            requests.append(parameters)
            response = client.get(path, query_string=parameters)
            assert response.status_code == 200, response.get_json()
            return response.get_json()

        def _request_binary_json(self, *args, **kwargs):
            raise AssertionError("Configuration must not request binary data")

        list_voices = Mock(return_value={"items": []})
        get_session_settings = Mock(return_value={"revision": 4, "override": {"speed": 0.9}})
        update_session_settings = Mock(return_value={"revision": 5})

    def health(service):
        assert service["id"] == "audio_cpp", "An unrelated provider was probed"
        return TtsHealth(True, True)

    def enrich(service, **kwargs):
        assert service["id"] == "audio_cpp", "An unrelated provider was discovered"
        return {
            "models": [BREEZE], "default_model": BREEZE,
            "model_catalog": [{"id": BREEZE, "voice_mode": "prebuilt"}],
            "model_voice_modes": {BREEZE: "prebuilt"},
            "voices": ["native"], "voice_catalogues": {BREEZE: ["native"]},
        }

    health_probe = Mock(side_effect=health)
    discover = Mock(side_effect=enrich)
    monkeypatch.setattr(catalogue.providers, "health", health_probe)
    monkeypatch.setattr(catalogue.providers, "enrich_catalog", discover)
    application = Application()
    runtime = SimpleNamespace(require_application=lambda: application)
    result = configure_tts(runtime, ConfigureTtsInput(
        session_id="session-1", service_id="AUDIO-CPP", model=BREEZE, voice="native",
        expected_revision=4, idempotency_key="scoped-configuration",
    ))
    assert requests == [{"service_id": "audio_cpp"}, {"refresh": "true", "service_id": "audio_cpp"}]
    assert health_probe.call_count == discover.call_count == 1
    assert result.result["selection"]["model"] == BREEZE
    saved = application.update_session_settings.call_args.kwargs
    assert saved["expected_revision"] == 4
    assert saved["idempotency_key"] == "scoped-configuration"
    assert saved["value"]["speed"] == 0.9
    assert saved["value"]["provider_switch_reviewed"] is True
