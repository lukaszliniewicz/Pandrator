"""Canonical TTS service identifiers shared by catalogues and queue coordination."""


def normalize_service_id(value: object) -> str:
    normalized = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    return {
        "qwen3_tts": "kobold_qwen",
        "qwen3": "kobold_qwen",
        "qwen": "kobold_qwen",
        "kobold_qwen3": "kobold_qwen",
        "audio.cpp": "audio_cpp",
        "audio-cpp": "audio_cpp",
        "audiocpp": "audio_cpp",
        "openai_compatible": "openai_compatible",
    }.get(normalized, normalized)
