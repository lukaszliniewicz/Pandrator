"""Shared validation for fixed-route reusable and project sound clients."""

from __future__ import annotations

import re

_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$")
_AUDIO_EXTENSIONS = frozenset({".wav", ".mp3", ".flac", ".ogg", ".m4a"})
MAX_AUDIO_IMPORT_BYTES = 16 * 1024 * 1024


def identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value


def revision(value: int, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"expected_revision must be an integer of at least {minimum}.")
    return value


def idempotency_key(value: str) -> str:
    if not isinstance(value, str) or not _IDEMPOTENCY_KEY.fullmatch(value):
        raise ValueError("Idempotency keys must contain 8-200 safe ASCII characters.")
    return value


def bounded_page(limit: int, *, maximum: int = 100) -> int:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be an integer from 1 to {maximum}.")
    return limit


__all__ = [
    "MAX_AUDIO_IMPORT_BYTES",
    "bounded_page",
    "identifier",
    "idempotency_key",
    "revision",
]
