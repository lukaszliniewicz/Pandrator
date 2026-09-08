from copy import deepcopy

import pytest

from pandrator.logic.tts_provider_switch import prepare_tts_provider_switch


@pytest.mark.parametrize(
    "service", ["Qwen3 TTS", "FishS2", "VoxCPM", "Chatterbox", "Magpie"]
)
def test_existing_provider_is_not_changed(service):
    old = {
        "service": service,
        "model": "old",
        "voice": "old-voice",
        "fishs2_top_p": 0.7,
    }
    assert prepare_tts_provider_switch(old, old) == old


def test_explicit_switch_clears_old_selection_aliases_and_options():
    old = {
        "service": "Qwen3 TTS",
        "model": "Voice Cloning",
        "voice": "old",
        "speaker": "old",
    }
    supplied = {
        **old,
        "service": "audio_cpp",
        "tts_service": "Qwen3 TTS",
        "xtts_model": "Voice Cloning",
        "kobold_qwen_base_url": "http://localhost:5001",
        "options": {"legacy": True},
        "audio_cpp_voice_ref": {"id": "stale"},
        "language": "pl",
        "speed": 1.1,
    }
    original = deepcopy(supplied)
    result = prepare_tts_provider_switch(old, supplied)
    assert supplied == original
    assert result["service"] == result["tts_service"] == "audio_cpp"
    assert result["model"] == result["xtts_model"] == ""
    assert result["voice"] == result["speaker"] == ""
    assert result["options"] == result["audio_cpp_voice_ref"] == {}
    assert "kobold_qwen_base_url" not in result
    assert result["speed"] == 1.1 and result["language"] == "pl"


def test_reviewed_target_voice_can_keep_the_same_native_id():
    result = prepare_tts_provider_switch(
        {"service": "kobold_qwen", "model": "Prebuilt Voices", "voice": "Vivian"},
        {
            "service": "audio_cpp",
            "model": "qwen3_tts_1_7b_customvoice_q8_0",
            "voice": "Vivian",
            "provider_switch_reviewed": True,
        },
    )
    assert result["voice"] == result["speaker"] == "Vivian"
    assert result["model"] == result["xtts_model"] == "qwen3_tts_1_7b_customvoice_q8_0"
    assert "provider_switch_reviewed" not in result


def test_resetting_overrides_does_not_opt_into_a_provider_switch():
    assert prepare_tts_provider_switch({"service": "fishs2"}, {}) == {}


def test_unrelated_provider_and_custom_settings_are_preserved():
    value = {"service": "xtts", "model": "fine-tuned", "temperature": 0.5}
    assert prepare_tts_provider_switch({"service": "fishs2"}, value) == value
