"""Stable, display-safe settings snapshots for generated output artifacts."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from .workspace import BUILTIN_DEFAULTS, RUNTIME_SETTING_ALIASES, stable_hash

OUTPUT_SETTINGS_SNAPSHOT_VERSION = 1
OUTPUT_SETTINGS_SECTIONS = ("output", "audio", "subtitles")


def build_output_settings_snapshot(
    settings: dict[str, Any] | None,
    resolved_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture only output-related effective settings in a stable contract."""

    flattened = dict(settings or {})
    resolved = resolved_snapshot if isinstance(resolved_snapshot, dict) else {}
    sections: dict[str, dict[str, Any]] = {}
    for section in OUTPUT_SETTINGS_SECTIONS:
        resolved_section = resolved.get(section)
        if isinstance(resolved_section, dict):
            sections[section] = deepcopy(resolved_section)
            continue
        aliases = RUNTIME_SETTING_ALIASES.get(section, {})
        values: dict[str, Any] = {}
        for key in BUILTIN_DEFAULTS[section]:
            runtime_key = aliases.get(key, key)
            if runtime_key in flattened:
                values[key] = deepcopy(flattened[runtime_key])
            elif key in flattened:
                values[key] = deepcopy(flattened[key])
        if values:
            sections[section] = values

    return {
        "version": OUTPUT_SETTINGS_SNAPSHOT_VERSION,
        "settings_hash": stable_hash(sections),
        "sections": sections,
    }
