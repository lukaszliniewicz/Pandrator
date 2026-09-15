"""Backend defaults and validation for source logical-passage settings."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

from .source_passage_policy import (
    DEFAULT_CUE_JOIN_GAP_MS,
    DEFAULT_DIAGNOSTIC_SPAN_MS,
    DEFAULT_MIN_CHARS,
    DEFAULT_PREFERRED_CHARS,
    DEFAULT_SENTENCE_LOOKAHEAD_CHARS,
    SOURCE_PASSAGE_POLICY_VERSION,
)

SOURCE_PASSAGE_DEFAULTS: dict[str, int] = {
    "min_chars": DEFAULT_MIN_CHARS,
    "preferred_chars": DEFAULT_PREFERRED_CHARS,
    "sentence_lookahead_chars": DEFAULT_SENTENCE_LOOKAHEAD_CHARS,
    "cue_join_gap_ms": DEFAULT_CUE_JOIN_GAP_MS,
    "diagnostic_span_ms": DEFAULT_DIAGNOSTIC_SPAN_MS,
}

SOURCE_PASSAGE_RANGES: dict[str, tuple[int, int]] = {
    "min_chars": (1, 500),
    "preferred_chars": (1, 1000),
    "sentence_lookahead_chars": (0, 200),
    "cue_join_gap_ms": (0, 3000),
    "diagnostic_span_ms": (1000, 60000),
}

SOURCE_PASSAGE_DESCRIPTIONS: dict[str, str] = {
    "min_chars": (
        "Minimum characters applied to fallback clause selection only. "
        "Short genuine sentences stay independent; "
        "relation min<=preferred enforced."
    ),
    "preferred_chars": (
        "Soft preferred passage length in characters. "
        "Lengths are preferences, never forced cuts."
    ),
    "sentence_lookahead_chars": (
        "Sentence fit window beyond preferred_chars."
    ),
    "cue_join_gap_ms": (
        "Ordinary guarded joining allowance in milliseconds. Verified "
        "unfinished same-speaker phrases may bridge gaps up to this; bounded "
        "unfinished-phrase bridging can still allow more, up to a 3000 ms "
        "cumulative budget enforced by the builder, which also clamps larger "
        "values to 3000. Never an automatic split threshold; 0 keeps a zero "
        "ordinary gap."
    ),
    "diagnostic_span_ms": (
        "Diagnostic span in milliseconds. Flags span_preference_exceeded "
        "only; never a hard cap."
    ),
}

# Flattened runtime keys used in job/dispatch payloads. Prefixed to avoid any
# collision with generic names from other sections.
RUNTIME_PREFIX = "source_passage_"
WEB_TO_RUNTIME: dict[str, str] = {
    "min_chars": "source_passage_min_chars",
    "preferred_chars": "source_passage_preferred_chars",
    "sentence_lookahead_chars": "source_passage_sentence_lookahead_chars",
    "cue_join_gap_ms": "source_passage_cue_join_gap_ms",
    "diagnostic_span_ms": "source_passage_diagnostic_span_ms",
}
RUNTIME_TO_WEB: dict[str, str] = {v: k for k, v in WEB_TO_RUNTIME.items()}

__all__ = [
    "SOURCE_PASSAGE_POLICY_VERSION",
    "SOURCE_PASSAGE_DEFAULTS",
    "SOURCE_PASSAGE_RANGES",
    "SOURCE_PASSAGE_DESCRIPTIONS",
    "WEB_TO_RUNTIME",
    "RUNTIME_TO_WEB",
    "normalize_source_passage_settings",
    "effective_source_passage_settings",
    "to_build_kwargs",
    "to_runtime_keys",
    "from_runtime_keys",
    "source_passage_settings_hash",
    "describe_source_passage_settings",
]


def _strict_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer")
    return value


def normalize_source_passage_settings(raw: Any) -> dict[str, int]:
    """Validate a `source_passages` section dict and return effective values."""
    if raw is None:
        return dict(SOURCE_PASSAGE_DEFAULTS)
    if not isinstance(raw, dict):
        raise TypeError("source_passages settings must be an object")
    unknown = set(raw) - set(SOURCE_PASSAGE_DEFAULTS)
    if unknown:
        raise ValueError(f"Unknown source_passages keys: {sorted(unknown)}")
    merged = dict(SOURCE_PASSAGE_DEFAULTS)
    for key in SOURCE_PASSAGE_DEFAULTS:
        if key in raw:
            # Strict contract: an explicit null is rejected; inherit a value
            # by omitting its key.
            value = _strict_int(raw[key], key)
            low, high = SOURCE_PASSAGE_RANGES[key]
            if not low <= value <= high:
                raise ValueError(f"{key} must be an integer from {low} to {high}.")
            merged[key] = value
    if merged["min_chars"] > merged["preferred_chars"]:
        raise ValueError("min_chars must not exceed preferred_chars.")
    return merged


def effective_source_passage_settings(*layers: Any) -> dict[str, int]:
    """Merge builtin <- global <- session <- run layers, then validate."""
    merged: dict[str, Any] = {}
    for layer in layers:
        if layer is None:
            continue
        if not isinstance(layer, dict):
            raise TypeError("source_passages settings layers must be objects")
        unknown = set(layer) - set(SOURCE_PASSAGE_DEFAULTS)
        if unknown:
            raise ValueError(f"Unknown source_passages keys: {sorted(unknown)}")
        for key, value in layer.items():
            if value is None:
                raise TypeError(f"{key} must be an integer")
            merged[key] = deepcopy(value)
    return normalize_source_passage_settings({**SOURCE_PASSAGE_DEFAULTS, **merged})


def to_build_kwargs(
    effective: dict[str, Any], *, language_code: str = "en"
) -> dict[str, Any]:
    """Map effective web settings onto `build_source_passages` kwargs."""
    normalized = normalize_source_passage_settings(effective)
    return {
        "min_chars": normalized["min_chars"],
        "max_chars": normalized["preferred_chars"],
        "sentence_lookahead_chars": normalized["sentence_lookahead_chars"],
        "pause_ms": normalized["cue_join_gap_ms"],
        "max_span_ms": normalized["diagnostic_span_ms"],
        "language_code": language_code or "en",
    }


def to_runtime_keys(effective: dict[str, Any]) -> dict[str, int]:
    """Return prefixed flattened runtime keys for job/dispatch payloads."""
    normalized = normalize_source_passage_settings(effective)
    return {WEB_TO_RUNTIME[key]: normalized[key] for key in SOURCE_PASSAGE_DEFAULTS}


def from_runtime_keys(payload: Any) -> dict[str, int]:
    """Read prefixed runtime keys back to web keys, falling back to defaults."""
    if payload is None:
        return dict(SOURCE_PASSAGE_DEFAULTS)
    if not isinstance(payload, dict):
        raise TypeError("source passage runtime payload must be an object")
    web_values: dict[str, Any] = {}
    for runtime_key, web_key in RUNTIME_TO_WEB.items():
        if runtime_key in payload:
            if payload[runtime_key] is None:
                raise TypeError(f"{runtime_key} must be an integer")
            web_values[web_key] = payload[runtime_key]
    nested = payload.get("source_passages")
    if isinstance(nested, dict):
        for key, value in nested.items():
            if key not in SOURCE_PASSAGE_DEFAULTS:
                raise ValueError(f"Unknown source_passages keys: {[key]}")
            if value is None:
                raise TypeError(f"{key} must be an integer")
            web_values.setdefault(key, value)
    elif nested is not None:
        raise TypeError("source_passages settings must be an object")
    if not web_values:
        return dict(SOURCE_PASSAGE_DEFAULTS)
    return normalize_source_passage_settings({**SOURCE_PASSAGE_DEFAULTS, **web_values})


def source_passage_settings_hash(effective: dict[str, Any]) -> str:
    normalized = normalize_source_passage_settings(effective)
    encoded = json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def describe_source_passage_settings() -> dict[str, dict[str, Any]]:
    return {
        key: {
            "default": SOURCE_PASSAGE_DEFAULTS[key],
            "minimum": SOURCE_PASSAGE_RANGES[key][0],
            "maximum": SOURCE_PASSAGE_RANGES[key][1],
            "type": "integer",
            "description": SOURCE_PASSAGE_DESCRIPTIONS[key],
        }
        for key in SOURCE_PASSAGE_DEFAULTS
    }
