"""Public generation choices, excluding connection/credential and inline audio data."""

from typing import Any

_TEXT_FIELDS = {
    "service": 255,
    "preview_service_id": 160,
    "model": 300,
    "xtts_model": 300,
    "voice": 300,
    "speaker": 300,
    "language": 40,
    "generation_prompt": 4000,
    "openai_audio_instructions": 4000,
}
_SEED_FIELDS = {"seed", "audio_cpp_seed"}


def public_preview_settings(settings: object) -> dict[str, Any]:
    if type(settings) is not dict:
        return {}
    return {
        key: value
        for key, value in settings.items()
        if (
            key in _TEXT_FIELDS and type(value) is str and len(value) <= _TEXT_FIELDS[key]
        ) or (key in _SEED_FIELDS and type(value) is int and 0 <= value <= 2**32 - 1)
    }


def public_preview_payload(payload: object) -> dict[str, Any]:
    if type(payload) is not dict:
        return {}
    result: dict[str, Any] = {"settings": public_preview_settings(payload.get("settings"))}
    if type(payload.get("text")) is str and len(payload["text"]) <= 1000:
        result["text"] = payload["text"]
    if type(payload.get("workspace_id")) is str and len(payload["workspace_id"]) <= 80:
        result["workspace_id"] = payload["workspace_id"]
    return result
