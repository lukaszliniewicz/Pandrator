"""Audition reads retain generation choices while hiding private runtime inputs."""

from copy import deepcopy

from pandrator.web.speech_preview_settings import public_preview_payload, public_preview_settings


def test_preview_projection_preserves_choices_and_excludes_connection_credentials_and_inline_audio():
    choices = {"service": "Google Gemini", "preview_service_id": "gemini", "model": "test-model",
        "voice": "voice_test", "language": "en", "generation_prompt": "", "seed": 0}
    private = {**choices, "provider_configs": [{"api_key": "private-key", "api_base": "private-url"}],
        "secret_ref": "private-path", "api_key_env": "PRIVATE_ENV", "preview_api_base": "private-url",
        "audio_cpp_voice_ref": {"data": "private-audio"}}
    before = deepcopy(private)
    assert public_preview_settings(private) == choices
    assert private == before
    assert public_preview_payload({"text": "Audition", "workspace_id": "workshop", "settings": private,
        "tts_settings_revisions": {"services.tts": 9}}) == {
        "text": "Audition", "workspace_id": "workshop", "settings": choices}


def test_preview_projection_bounds_untrusted_saved_values_without_stringifying_private_objects():
    assert public_preview_payload(None) == {}
    assert public_preview_settings({"model": {"secret": "private"}, "seed": True,
        "generation_prompt": "x" * 4001, "voice": "v" * 300, "audio_cpp_seed": 2**32}) == {"voice": "v" * 300}
