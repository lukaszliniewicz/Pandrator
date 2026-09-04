"""Shared context-capsule and deterministic wave helpers for passive dispatch."""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

MAX_PARALLEL_BATCHES = 8
MAX_CONTEXT_CAPSULE_BYTES = 128 * 1024
MAX_CONTEXT_DELTA_BYTES = 32 * 1024

_MAP_FIELDS = ("terminology", "entities")
_LIST_FIELDS = ("style_rules", "decisions", "notes")


def _json_size(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )


def validate_context_size(
    value: Mapping[str, Any],
    *,
    maximum_bytes: int,
    label: str,
) -> None:
    """Reject a capsule or delta that would bloat every delegated packet."""

    if _json_size(value) > maximum_bytes:
        raise ValueError(f"{label} exceeds the {maximum_bytes // 1024} KiB limit.")


def empty_context_capsule() -> dict[str, Any]:
    return {
        "overview": "",
        "terminology": {},
        "entities": {},
        "style_rules": [],
        "decisions": [],
        "notes": [],
    }


def normalize_context_capsule(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return only the stable, explicitly supported capsule fields."""

    source = dict(value or {})
    result = empty_context_capsule()
    result["overview"] = str(source.get("overview") or "")
    for field in _MAP_FIELDS:
        raw = source.get(field)
        result[field] = (
            {str(key): str(item) for key, item in raw.items()}
            if isinstance(raw, Mapping)
            else {}
        )
    for field in _LIST_FIELDS:
        raw = source.get(field)
        result[field] = [str(item) for item in raw] if isinstance(raw, list) else []
    validate_context_size(
        result,
        maximum_bytes=MAX_CONTEXT_CAPSULE_BYTES,
        label="Context capsule",
    )
    return result


def normalize_context_delta(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a bounded delta using the capsule's mergeable fields."""

    source = dict(value or {})
    result: dict[str, Any] = {}
    for field in _MAP_FIELDS:
        raw = source.get(field)
        result[field] = (
            {str(key): str(item) for key, item in raw.items()}
            if isinstance(raw, Mapping)
            else {}
        )
    for field in _LIST_FIELDS:
        raw = source.get(field)
        result[field] = [str(item) for item in raw] if isinstance(raw, list) else []
    validate_context_size(
        result,
        maximum_bytes=MAX_CONTEXT_DELTA_BYTES,
        label="Context delta",
    )
    return result


def merge_context_capsule(
    initial: Mapping[str, Any] | None,
    deltas: Iterable[tuple[int, Mapping[str, Any]]],
) -> dict[str, Any]:
    """Merge accepted deltas in ordinal order, independent of completion order."""

    result = normalize_context_capsule(initial)
    for _ordinal, raw_delta in sorted(deltas, key=lambda item: item[0]):
        delta = normalize_context_delta(raw_delta)
        for field in _MAP_FIELDS:
            result[field].update(delta[field])
        for field in _LIST_FIELDS:
            known = set(result[field])
            for item in delta[field]:
                if item not in known:
                    result[field].append(item)
                    known.add(item)
    validate_context_size(
        result,
        maximum_bytes=MAX_CONTEXT_CAPSULE_BYTES,
        label="Merged context capsule",
    )
    return deepcopy(result)


def execution_policy(settings: Mapping[str, Any]) -> tuple[str, int]:
    mode = str(settings.get("execution_mode") or "serial").strip().lower()
    if mode not in {"serial", "parallel"}:
        mode = "serial"
    if mode == "serial":
        return mode, 1
    try:
        width = int(settings.get("max_parallel_batches") or 2)
    except (TypeError, ValueError):
        width = 2
    return mode, max(2, min(MAX_PARALLEL_BATCHES, width))


def wave_bounds(ordinal: int, settings: Mapping[str, Any]) -> tuple[int, int, int]:
    """Return zero-based wave index and its inclusive/exclusive ordinal bounds."""

    _mode, width = execution_policy(settings)
    index = ordinal // width
    start = index * width
    return index, start, start + width


def context_capsule_for_wave(
    settings: Mapping[str, Any],
    *,
    wave_start: int,
) -> dict[str, Any]:
    """Build the immutable capsule snapshot visible to one serial step or wave."""

    raw_deltas = settings.get("context_deltas")
    accepted: list[tuple[int, Mapping[str, Any]]] = []
    if isinstance(raw_deltas, Mapping):
        for raw_ordinal, raw_delta in raw_deltas.items():
            try:
                ordinal = int(raw_ordinal)
            except (TypeError, ValueError):
                continue
            if ordinal < wave_start and isinstance(raw_delta, Mapping):
                accepted.append((ordinal, raw_delta))
    initial = settings.get("context_capsule")
    return merge_context_capsule(
        initial if isinstance(initial, Mapping) else None,
        accepted,
    )


def store_context_delta(
    settings: Mapping[str, Any],
    *,
    ordinal: int,
    delta: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Return settings with one normalized, ordinal-keyed accepted delta."""

    result = deepcopy(dict(settings))
    deltas = result.get("context_deltas")
    normalized_deltas = dict(deltas) if isinstance(deltas, Mapping) else {}
    normalized_deltas[str(ordinal)] = normalize_context_delta(delta)
    result["context_deltas"] = normalized_deltas
    return result


__all__ = [
    "MAX_CONTEXT_CAPSULE_BYTES",
    "MAX_CONTEXT_DELTA_BYTES",
    "MAX_PARALLEL_BATCHES",
    "context_capsule_for_wave",
    "empty_context_capsule",
    "execution_policy",
    "merge_context_capsule",
    "normalize_context_capsule",
    "normalize_context_delta",
    "store_context_delta",
    "validate_context_size",
    "wave_bounds",
]
