from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from pandrator.logic.tts_provider_switch import prepare_tts_provider_switch
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas import ConfigureTtsInput
from pandrator_mcp.tools.e2e import configure_tts


def configure(mode, voice, *, ready=False, default_voice=""):
    old = {
        "service": "kobold_qwen",
        "tts_service": "kobold_qwen",
        "model": "Prebuilt Voices",
        "xtts_model": "Prebuilt Voices",
        "voice": "Vivian",
        "speaker": "Vivian",
        "options": {"old": True},
    }
    application = Mock()
    application.get_session_settings.return_value = {"override": old}
    application.update_session_settings.return_value = {"revision": 2}
    catalog = {
        "services": [
            {
                "id": "audio_cpp",
                "models": ["target"],
                "default_model": "target",
                "model_voice_modes": {"target": mode},
                "voices": ["Vivian"],
                "default_voice": default_voice,
            }
        ],
        "managed_voices": [
            {
                "id": "managed",
                "name": "My narrator",
                "registrations": {
                    "audio_cpp": {
                        "status": "ready" if ready else "pending",
                        "voice_id": "linked-id",
                    }
                },
            }
        ],
    }
    with patch("pandrator_mcp.tools.e2e.tts_catalog", return_value=catalog):
        configure_tts(
            SimpleNamespace(require_application=lambda: application),
            ConfigureTtsInput(
                session_id="session",
                service_id="audio_cpp",
                model="target",
                voice=voice,
                expected_revision=1,
                idempotency_key="switch-001",
            ),
        )
    value = application.update_session_settings.call_args.kwargs["value"]
    return prepare_tts_provider_switch(old, value)


def test_native_voice_survives_explicit_mcp_switch_and_aliases_are_reset():
    result = configure("prebuilt", "Vivian")
    assert result["model"] == result["xtts_model"] == "target"
    assert result["voice"] == result["speaker"] == "Vivian"
    assert result["service"] == result["tts_service"] == "audio_cpp"
    assert result["options"] == {}
    assert "provider_switch_reviewed" not in result


@pytest.mark.parametrize(
    "voice,default_voice", [(None, ""), (None, "Vivian"), ("Vivian", "")]
)
def test_cloning_cannot_reuse_a_prebuilt_or_missing_voice(voice, default_voice):
    with pytest.raises(PandratorMcpError, match="ready audio.cpp reference link"):
        configure("cloning", voice, default_voice=default_voice)


@pytest.mark.parametrize("voice", ["My narrator", "linked-id"])
def test_cloning_accepts_ready_managed_link(voice):
    result = configure("cloning", voice, ready=True)
    assert result["voice"] == result["speaker"] == "linked-id"


def test_optional_cloning_can_explicitly_clear_old_voice():
    result = configure("optional_cloning", None)
    assert result["voice"] == result["speaker"] == ""


def test_cloning_rejects_pending_managed_link():
    with pytest.raises(PandratorMcpError, match="ready managed registration"):
        configure("cloning", "linked-id", ready=False)
