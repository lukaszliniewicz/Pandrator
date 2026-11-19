"""Shared immutable workflow stage definition and the stage keys it accepts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class StageDefinition:
    key: str
    title: str
    explanation: str
    executable: bool = True
    prerequisite_roles: tuple[str, ...] = ()
    output_role: str | None = None
    job_kind: str | None = None


# Earlier releases also called document optimisation ``optimize_document``.
# Requests and stored job input may still use that name.
_RETIRED_STAGE_KEYS = {"optimize_document": "optimize_tts"}


def stage_key_from_input(stage_key: str) -> str:
    """Return the current key for a stage named in a request or stored input."""

    return _RETIRED_STAGE_KEYS.get(stage_key, stage_key)


def stage_keys_from_input(stage_keys: Iterable[str]) -> list[str]:
    """Return current keys for a list of stages, once each and in order."""

    return list(dict.fromkeys(stage_key_from_input(str(key)) for key in stage_keys))


def stage_settings_from_input(values: Mapping[str, Any]) -> dict[str, Any]:
    """Key per-stage settings by current stage keys; a current key wins."""

    retired = {
        _RETIRED_STAGE_KEYS[key]: value
        for key, value in values.items()
        if key in _RETIRED_STAGE_KEYS
    }
    current = {
        key: value for key, value in values.items() if key not in _RETIRED_STAGE_KEYS
    }
    return {**retired, **current}
