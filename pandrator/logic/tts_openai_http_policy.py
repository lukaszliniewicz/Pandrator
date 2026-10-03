"""Pure URL, bearer-header and fallback policy for OpenAI-compatible TTS APIs."""

XTTS_OPENAI_PLACEHOLDER_API_KEY = "sk-placeholder"
OPENAI_CANDIDATE_FALLBACK_STATUS_CODES = {404, 405, 501}


def _normalize_base_url(base_url: str | None, fallback: str) -> str:
    normalized = (base_url or fallback).strip().rstrip("/")
    return normalized or fallback


def _openai_auth_headers(
    api_key: str = XTTS_OPENAI_PLACEHOLDER_API_KEY,
) -> dict[str, str]:
    return {"Authorization": f"Bearer {api_key}"}


def _should_try_next_openai_candidate(status_code: int) -> bool:
    return int(status_code) in OPENAI_CANDIDATE_FALLBACK_STATUS_CODES


def _openai_url_candidates(base_url: str, suffix: str) -> list[str]:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        candidates = [f"{normalized}/{suffix}"]
    else:
        candidates = [
            f"{normalized}/v1/{suffix}",
            f"{normalized}/{suffix}",
        ]

    deduped: list[str] = []
    seen: set[str] = set()
    for url in candidates:
        if url not in seen:
            deduped.append(url)
            seen.add(url)
    return deduped


def _openai_models_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "models")


def _openai_voices_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "voices")


def _openai_audio_voices_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/voices")


def _openai_audio_speech_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/speech")


def _openai_audio_speech_batch_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "audio/speech/batch")


def _openai_capabilities_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "capabilities")


def _openai_files_urls(base_url: str) -> list[str]:
    return _openai_url_candidates(base_url, "files")
