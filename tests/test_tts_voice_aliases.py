"""Regression: an old local speaker must not override the selected UI voice.

All provider checks build payloads locally; no paid synthesis is performed.
"""
from copy import deepcopy

import pytest

from pandrator.logic.tts_provider_switch import normalize_tts_voice_aliases
from pandrator.web.workspace import adapt_runtime_settings
from pandrator.web.workflow_handlers import (
    _apply_segment_tts_overrides,
    _apply_selected_segment_tts_override,
)
from pandrator.web.models import AppSetting, SessionSetting
from tests import test_web_settings_api as settings_tests


@pytest.mark.parametrize("values,expected", [
    ({"voice": "onyx", "speaker": "old-local-voice"}, "onyx"),
    ({"voice": "onyx"}, "onyx"),
    ({"speaker": "legacy-voice"}, "legacy-voice"),
    ({"voice": "", "speaker": "old-local-voice"}, ""),
    ({"voice": None, "speaker": "old-local-voice"}, None),
])
def test_one_storage_layer_has_one_voice(values, expected):
    before = deepcopy(values)
    result = normalize_tts_voice_aliases(values)
    assert result["voice"] == result["speaker"] == expected
    assert values == before


def test_empty_override_stays_empty():
    assert normalize_tts_voice_aliases({}) == {}


@pytest.mark.parametrize("service,voice", [
    ("azure-openai-v1", "onyx"),
    ("azure-speech-mai-voice-2", "de-DE-Klaus:MAI-Voice-2"),
    ("kokoro", "af_heart"),
    ("custom-service", "custom-voice"),
])
def test_runtime_repairs_conflicting_frozen_snapshots_without_mutation(service, voice):
    snapshot = {"service": service, "voice": voice, "speaker": "old-local-voice"}
    before = deepcopy(snapshot)
    runtime = adapt_runtime_settings("tts", snapshot)
    assert runtime["speaker"] == runtime["voice"] == voice
    assert snapshot == before
    assert adapt_runtime_settings("tts", runtime) == runtime


@pytest.mark.parametrize("values", [
    {"speaker": "legacy-voice"},
    {"voice": "", "speaker": "legacy-voice"},
])
def test_runtime_keeps_legacy_speaker_with_no_nonempty_ui_choice(values):
    assert adapt_runtime_settings("tts", values)["speaker"] == "legacy-voice"


def test_expert_model_alias_and_other_sections_are_unchanged():
    assert adapt_runtime_settings("tts", {
        "model": "web-model", "xtts_model": "expert-model",
    })["xtts_model"] == "expert-model"
    assert adapt_runtime_settings("audio", {
        "voice": "one", "speaker": "two",
    })["speaker"] == "two"


def test_openai_payload_uses_onyx_not_the_old_local_speaker():
    from pandrator.logic.tts_handler import _build_openai_compatible_audio_payload

    runtime = adapt_runtime_settings("tts", {
        "service": "azure-openai-v1", "model": "gpt-4o-mini-tts",
        "voice": "onyx", "speaker": "pandrator-german-voice-design-old",
    })
    payload = _build_openai_compatible_audio_payload("Test only.", runtime, {
        "name": "OpenAI-compatible test", "provider": "openai",
        "base_url": "https://speech.invalid/v1",
        "default_model": "gpt-4o-mini-tts", "default_voice": "alloy",
    })
    assert payload["voice"] == "onyx"
    assert payload["model"] == "gpt-4o-mini-tts"


def test_explicit_per_block_and_selected_run_voices_still_win():
    runtime = adapt_runtime_settings("tts", {"voice": "onyx", "speaker": "old-local"})
    block = _apply_segment_tts_overrides(runtime, voice="nova")
    assert block["voice"] == block["speaker"] == "nova"
    selected = _apply_selected_segment_tts_override(block, {"speaker": "echo"})
    assert selected["voice"] == selected["speaker"] == "echo"
    assert runtime["voice"] == runtime["speaker"] == "onyx"


@pytest.fixture
def app_case():
    case = settings_tests.SettingsApiTests(methodName="runTest")
    case.setUp()
    try:
        yield case
    finally:
        case.tearDown()


def _new_session(case):
    response = case.client.post("/api/v1/sessions", headers=case.headers,
                                json={"name": "Voice alias regression", "workflow_kind": "voiceover"})
    assert response.status_code == 201, response.get_json()
    return response.get_json()["id"]


def _put(case, url, value, revision=0):
    response = case.client.put(url, json={"value": value},
                              headers={**case.headers, "If-Match": str(revision)})
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def test_session_saves_synchronize_aliases_and_preserve_reset(app_case):
    sid = _new_session(app_case)
    url = f"/api/v1/sessions/{sid}/settings/tts"
    saved = _put(app_case, url, {
        "service": "azure-openai-v1", "model": "gpt-4o-mini-tts",
        "voice": "onyx", "speaker": "old-local-voice",
    })
    assert saved["override"]["speaker"] == saved["override"]["voice"] == "onyx"
    legacy = _put(app_case, url, {"speaker": "echo"}, saved["revision"])
    assert legacy["effective"]["voice"] == legacy["effective"]["speaker"] == "echo"
    cleared = _put(app_case, url, {"voice": "", "speaker": "old"}, legacy["revision"])
    assert cleared["effective"]["voice"] == cleared["effective"]["speaker"] == ""
    reset = _put(app_case, url, {}, cleared["revision"])
    assert reset["override"] == {}


def test_global_defaults_save_both_aliases(app_case):
    saved = _put(app_case, "/api/v1/settings/defaults.tts", {
        "voice": "onyx", "speaker": "old-local-voice",
    })
    assert saved["value"]["voice"] == saved["value"]["speaker"] == "onyx"


@pytest.mark.parametrize("override,expected", [
    ({"voice": "onyx", "speaker": "old-local-voice"}, "onyx"),
    ({"speaker": "legacy-session-voice"}, "legacy-session-voice"),
    ({"voice": ""}, ""),
])
def test_old_saved_layers_are_repaired_on_read_without_rewriting_history(app_case, override, expected):
    sid = _new_session(app_case)
    database = app_case.app.extensions["pandrator"]["database"]
    with database.session() as db:
        global_row = db.get(AppSetting, "defaults.tts")
        if global_row is None:
            global_row = AppSetting(key="defaults.tts", value_json={})
            db.add(global_row)
        global_row.value_json = {"voice": "global-voice", "speaker": "stale-global"}
        db.add(SessionSetting(session_id=sid, section="tts", value_json=deepcopy(override), revision=1))
    response = app_case.client.get(f"/api/v1/sessions/{sid}/settings/tts")
    assert response.status_code == 200
    effective = response.get_json()["effective"]
    assert effective["voice"] == effective["speaker"] == expected
    with database.session() as db:
        assert db.get(SessionSetting, (sid, "tts")).value_json == override
