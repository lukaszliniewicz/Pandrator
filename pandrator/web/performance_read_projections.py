"""Pure response projections for contextual performance-plan reads."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any

PERFORMANCE_PLAN_VIEWS = ("summary", "annotations", "full")
SUMMARY_SETTING_FIELDS = (
    "mode",
    "purpose",
    "annotation_format",
    "model_name",
    "effective_model_name",
    "workflow_kind",
    "context_before",
    "context_after",
    "context_max_chars",
    "batch_size",
    "allow_vocalizations",
)
SPEECH_STRUCTURE_FIELDS = ("segment_id", "spans", "events", "boundary_after")
_UNIT_METADATA_FIELDS = (
    "speaker",
    "language",
    "node_kind",
    "timing",
)


def summary_settings(settings: dict[str, Any]) -> dict[str, Any]:
    """Return only settings that help interpret a compact plan read."""

    return {
        key: deepcopy(settings[key])
        for key in SUMMARY_SETTING_FIELDS
        if key in settings
    }


def capability_profile_id(capabilities: Any) -> str:
    """Hash a JSON capability profile using a canonical representation."""

    canonical = json.dumps(
        capabilities,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def frozen_capabilities(settings: dict[str, Any]) -> Any:
    """Read capabilities frozen on this plan, never current runtime settings."""

    return deepcopy(settings.get("capabilities") or {})


def annotation_unit(
    unit: dict[str, Any],
    annotation: dict[str, Any] | None,
    speech_structure: dict[str, Any] | None,
) -> dict[str, Any]:
    """Project one speech unit without repeating text inside spans or markup."""

    result: dict[str, Any] = {}
    for field in ("id", "ordinal"):
        if field in unit:
            result[field] = deepcopy(unit[field])
    result["text"] = str(unit.get("spoken_text") or "")
    result["text_layer"] = "speech"
    for field in _UNIT_METADATA_FIELDS:
        if field in unit:
            result[field] = deepcopy(unit[field])
    result["annotation"] = deepcopy(annotation)
    if speech_structure is None:
        result["speech_structure"] = None
    else:
        structure = {
            field: deepcopy(speech_structure[field])
            for field in SPEECH_STRUCTURE_FIELDS
            if field in speech_structure
        }
        structure["spans"] = [
            {key: deepcopy(value) for key, value in span.items() if key != "text"}
            for span in speech_structure.get("spans", [])
        ]
        result["speech_structure"] = structure
    return result


def intern_capability_profiles(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Move frozen capabilities into one stable, content-addressed list map."""

    projected: list[dict[str, Any]] = []
    profiles: dict[str, Any] = {}
    for item in items:
        compact = dict(item)
        capabilities = compact.pop("capabilities", {})
        profile_id = compact.get("capability_profile_id") or capability_profile_id(
            capabilities
        )
        compact["capability_profile_id"] = profile_id
        profiles.setdefault(profile_id, deepcopy(capabilities))
        projected.append(compact)
    return projected, profiles
