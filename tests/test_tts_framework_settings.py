"""Effective tuning resets and audition boundary contracts, without synthesis."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from pandrator.logic import tts_handler
from pandrator.logic.audiobook_chunking import audiobook_chunk_budget
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import _material_settings
from pandrator.web.schemas import TtsVoicePreviewRequest
from pandrator_mcp.schemas.voice_lifecycle import AuditionVoiceInput
from tests.web_test_support import prepare_web_test_data_root

MODEL = "qwen3_tts_1_7b_base_q8_0"


@pytest.fixture
def workspace(tmp_path):
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    client = app.test_client()
    auth = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()
    headers = {"X-CSRF-Token": auth["csrf_token"]}
    try:
        yield app, client, headers
    finally:
        app.extensions["pandrator"]["database"].dispose()


def test_model_reset_survives_global_changes_and_inherit_restores_tuning(workspace):
    _app, client, headers = workspace
    global_value = {
        "audio_cpp_model_settings": {MODEL: {"temperature": 0.8, "seed": 17}},
        "audio_cpp_max_tokens": 512,
        "audio_cpp_temperature": 0.1,
        "audio_cpp_options": {"top_p": 0.1},
        "speed": 1.4,
    }
    saved = client.put(
        "/api/v1/settings/defaults.tts", json={"value": global_value},
        headers={**headers, "If-Match": '"0"'},
    )
    assert saved.status_code == 200, saved.get_json()
    created = client.post("/api/v1/sessions", json={"name": "Reset fixture"}, headers=headers)
    session_id = created.get_json()["id"]
    path = f"/api/v1/sessions/{session_id}/settings/tts"
    override = {"service": "audio_cpp", "model": MODEL, "audio_cpp_model_settings": {MODEL: None}}
    result = client.put(path, json={"value": override}, headers={**headers, "If-Match": '"0"'})
    assert result.status_code == 200, result.get_json()
    effective = result.get_json()["effective"]
    assert effective["audio_cpp_model_settings"][MODEL] is None
    endpoint = {"id": "audio_cpp", "adapter": "audio_cpp"}
    payload = tts_handler._build_audio_cpp_audio_payload("Hello.", effective, endpoint)
    assert payload.get("options", {}) == {}
    assert "speed" not in payload
    budget = audiobook_chunk_budget({"audiobook_chunking": "model"}, effective)
    assert budget["target_chars"] == 1800  # Legacy 512-token tuning is suppressed.
    identity = _material_settings({"tts": effective})
    global_value["audio_cpp_model_settings"][MODEL].update(seed=99, top_p=0.7)
    changed = client.put(
        "/api/v1/settings/defaults.tts", json={"value": global_value},
        headers={**headers, "If-Match": '"1"'},
    )
    assert changed.status_code == 200, changed.get_json()
    readback = client.get(path).get_json()
    assert readback["override"]["audio_cpp_model_settings"][MODEL] is None
    assert _material_settings({"tts": readback["effective"]}) == identity
    assert readback["global"]["audio_cpp_model_settings"][MODEL]["seed"] == 99
    inherited_override = {**override, "audio_cpp_model_settings": {}}
    inherited = client.put(path, json={"value": inherited_override}, headers={**headers, "If-Match": '"1"'})
    assert inherited.status_code == 200, inherited.get_json()
    assert inherited.get_json()["effective"]["audio_cpp_model_settings"][MODEL] == global_value["audio_cpp_model_settings"][MODEL]
    assert _material_settings({"tts": inherited.get_json()["effective"]}) != identity
    assert client.get("/api/v1/settings/defaults.tts").get_json()["value"] == global_value


def test_audition_schemas_share_the_1000_character_limit():
    base = {"service_id": "audio_cpp", "model": MODEL, "idempotency_key": "audition-test:1"}
    assert len(AuditionVoiceInput(**base, text="x" * 1000).text) == 1000
    assert len(TtsVoicePreviewRequest(text="x" * 1000).text) == 1000
    for schema, kwargs in ((AuditionVoiceInput, base), (TtsVoicePreviewRequest, {})):
        with pytest.raises(ValidationError):
            schema(**kwargs, text="x" * 1001)


def test_preview_empty_prompt_clears_both_directions_without_persisting(workspace):
    app, client, headers = workspace
    defaults = {"generation_prompt": "Warm and quiet.", "openai_audio_instructions": "Slowly."}
    saved = client.put("/api/v1/settings/defaults.tts", json={"value": defaults}, headers={**headers, "If-Match": '"0"'})
    assert saved.status_code == 200, saved.get_json()
    catalogue = app.extensions["pandrator"]["tts_catalogue"]
    original = deepcopy(client.get("/api/v1/settings/defaults.tts").get_json())
    inherited = catalogue.preview_settings("audio_cpp", model=MODEL, voice="", language="en")
    assert inherited["generation_prompt"] == defaults["generation_prompt"]
    assert inherited["openai_audio_instructions"] == defaults["openai_audio_instructions"]
    cleared = catalogue.preview_settings("audio_cpp", model=MODEL, voice="", language="en", generation_prompt="")
    assert cleared["generation_prompt"] == ""
    assert cleared["openai_audio_instructions"] == ""
    custom = catalogue.preview_settings("audio_cpp", model=MODEL, voice="", language="en", generation_prompt="Brightly.")
    assert custom["generation_prompt"] == "Brightly."
    assert custom["openai_audio_instructions"] == ""
    assert client.get("/api/v1/settings/defaults.tts").get_json() == original


def test_preview_acknowledges_effective_direction_state_and_replays_it(workspace):
    _app, client, headers = workspace
    request_headers = {**headers, "Idempotency-Key": "preview-directions:1"}
    body = {"text": "Hello.", "model": MODEL, "generation_prompt": ""}
    first = client.post("/api/v1/services/tts/audio_cpp/preview", json=body, headers=request_headers)
    assert first.status_code == 202, first.get_json()
    assert first.get_json()["preview"]["directions"] == {"source": "cleared", "present": False}
    second = client.post("/api/v1/services/tts/audio_cpp/preview", json=body, headers=request_headers)
    assert second.status_code == 202
    assert second.get_json() == first.get_json()


def test_voice_design_inherits_effective_directions_and_rejects_explicit_clear(workspace):
    app, client, headers = workspace
    saved = client.put(
        "/api/v1/settings/defaults.tts",
        json={"value": {"generation_prompt": "A calm baritone."}},
        headers={**headers, "If-Match": '"0"'},
    )
    assert saved.status_code == 200
    catalogue = app.extensions["pandrator"]["tts_catalogue"]
    model = "qwen3_tts_1_7b_voicedesign_q8_0"
    settings = catalogue.preview_settings("audio_cpp", model=model, voice="", language="en")
    assert settings["generation_prompt"] == "A calm baritone."
    with pytest.raises(ValueError, match="instructions|description"):
        catalogue.preview_settings("audio_cpp", model=model, voice="", language="en", generation_prompt="")


def test_preview_seed_only_overrides_reset_when_explicitly_requested(workspace):
    app, client, headers = workspace
    saved = client.put(
        "/api/v1/settings/defaults.tts",
        json={"value": {"audio_cpp_seed": 123, "audio_cpp_model_settings": {MODEL: None}}},
        headers={**headers, "If-Match": '"0"'},
    )
    assert saved.status_code == 200
    catalogue = app.extensions["pandrator"]["tts_catalogue"]
    inherited = catalogue.preview_settings("audio_cpp", model=MODEL, voice="", language="en")
    payload = tts_handler._build_audio_cpp_audio_payload("Hello.", inherited, {})
    assert "seed" not in payload.get("options", {})
    explicit = catalogue.preview_settings("audio_cpp", model=MODEL, voice="", language="en", seed=7)
    payload = tts_handler._build_audio_cpp_audio_payload("Hello.", explicit, {})
    assert payload["options"]["seed"] == 7
