"""Pinned lifecycle metadata for exact provider and TTS model identities."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

_MODEL_LIFECYCLE: dict[tuple[str, str], dict[str, Any]] = {
    ("openai", "tts-1"): {
        "provider_id": "openai",
        "model_id": "tts-1",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://developers.openai.com/api/docs/deprecations",
        "shutdown_date": "2027-01-06",
        "replacement_model_ids": ["gpt-realtime-2.1-mini"],
        "migration_kind": "different_api",
    },
    ("openai", "tts-1-hd"): {
        "provider_id": "openai",
        "model_id": "tts-1-hd",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://developers.openai.com/api/docs/deprecations",
        "shutdown_date": "2027-01-06",
        "replacement_model_ids": ["gpt-realtime-2.1-mini"],
        "migration_kind": "different_api",
    },
    ("openai", "gpt-4o-mini-tts-2025-03-20"): {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini-tts-2025-03-20",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://developers.openai.com/api/docs/deprecations",
        "shutdown_date": "2027-01-06",
        "replacement_model_ids": ["gpt-realtime-2.1-mini"],
        "migration_kind": "different_api",
    },
    ("openai", "gpt-4o-mini-tts-2025-12-15"): {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini-tts-2025-12-15",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://developers.openai.com/api/docs/deprecations",
        "shutdown_date": "2027-01-06",
        "replacement_model_ids": ["gpt-realtime-2.1-mini"],
        "migration_kind": "different_api",
    },
    ("openai", "gpt-4o-mini-tts"): {
        "provider_id": "openai",
        "model_id": "gpt-4o-mini-tts",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://developers.openai.com/api/docs/models/gpt-4o-mini-tts",
        "shutdown_date": None,
        "replacement_model_ids": ["gpt-realtime-2.1-mini"],
        "migration_kind": "different_api",
    },
    ("gemini", "gemini-2.5-flash-preview-tts"): {
        "provider_id": "gemini",
        "model_id": "gemini-2.5-flash-preview-tts",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://ai.google.dev/gemini-api/docs/deprecations",
        "shutdown_date": "2026-11-17",
        "replacement_model_ids": [
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
        ],
        "migration_kind": "tts_model",
    },
    ("gemini", "gemini-2.5-pro-preview-tts"): {
        "provider_id": "gemini",
        "model_id": "gemini-2.5-pro-preview-tts",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://ai.google.dev/gemini-api/docs/deprecations",
        "shutdown_date": "2026-11-17",
        "replacement_model_ids": [
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
        ],
        "migration_kind": "tts_model",
    },
    ("gemini", "gemini-3.1-flash-tts-preview"): {
        "provider_id": "gemini",
        "model_id": "gemini-3.1-flash-tts-preview",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://ai.google.dev/gemini-api/docs/deprecations",
        "shutdown_date": "2026-11-17",
        "replacement_model_ids": [
            "gemini-3.8-flash-tts",
            "gemini-3.8-flash-lite-tts",
        ],
        "migration_kind": "tts_model",
    },
    ("elevenlabs", "eleven_turbo_v2"): {
        "provider_id": "elevenlabs",
        "model_id": "eleven_turbo_v2",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://elevenlabs.io/docs/overview/models",
        "replacement_model_ids": ["eleven_flash_v2"],
        "migration_kind": "tts_model",
    },
    ("elevenlabs", "eleven_turbo_v2_5"): {
        "provider_id": "elevenlabs",
        "model_id": "eleven_turbo_v2_5",
        "status": "deprecated",
        "reviewed_on": "2026-10-13",
        "source_url": "https://elevenlabs.io/docs/overview/models",
        "replacement_model_ids": ["eleven_flash_v2_5"],
        "migration_kind": "tts_model",
    },
}


def model_lifecycle(provider_id: str, model_id: str) -> dict[str, Any] | None:
    """Return pinned lifecycle evidence for one exact provider/model pair."""
    metadata = _MODEL_LIFECYCLE.get((provider_id, model_id))
    return deepcopy(metadata) if metadata is not None else None
