"""Pure settings helpers for audiobook sentence packing."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

DEFAULT_SENTENCE_PACKING = "auto"
DEFAULT_SENTENCE_PACKING_THRESHOLD = 300
MAX_SENTENCE_PACKING_THRESHOLD = 8192
SENTENCE_PACKING_MODES = frozenset({"auto", "sentences", "clauses"})

_PACKING_ERROR = "audiobook_sentence_packing must be 'auto', 'sentences' or 'clauses'."
_THRESHOLD_ERROR = (
    "audiobook_sentence_packing_threshold must be an integer between 1 and 8192."
)


def validate_sentence_packing_settings(settings: Mapping[str, Any]) -> None:
    """Validate optional sentence-packing values without importing app code."""

    if "audiobook_sentence_packing" in settings:
        mode = settings["audiobook_sentence_packing"]
        if not isinstance(mode, str) or mode not in SENTENCE_PACKING_MODES:
            raise ValueError(_PACKING_ERROR)

    if "audiobook_sentence_packing_threshold" not in settings:
        return

    threshold: object = settings["audiobook_sentence_packing_threshold"]
    if isinstance(threshold, str):
        try:
            threshold = int(threshold.strip())
        except ValueError as error:
            raise ValueError(_THRESHOLD_ERROR) from error
    elif type(threshold) is not int:
        raise ValueError(_THRESHOLD_ERROR)

    if not isinstance(threshold, int) or not 1 <= threshold <= MAX_SENTENCE_PACKING_THRESHOLD:
        raise ValueError(_THRESHOLD_ERROR)


def preserve_sentence_boundaries(
    settings: Mapping[str, Any], target_chars: int
) -> bool:
    """Resolve sentence packing from mode and the effective character target."""

    validate_sentence_packing_settings(settings)
    mode = settings.get("audiobook_sentence_packing", DEFAULT_SENTENCE_PACKING)
    if mode == "sentences":
        return True
    if mode == "clauses":
        return False
    threshold = settings.get(
        "audiobook_sentence_packing_threshold",
        DEFAULT_SENTENCE_PACKING_THRESHOLD,
    )
    if isinstance(threshold, str):
        threshold = int(threshold.strip())
    return target_chars >= threshold


__all__ = [
    "DEFAULT_SENTENCE_PACKING",
    "DEFAULT_SENTENCE_PACKING_THRESHOLD",
    "MAX_SENTENCE_PACKING_THRESHOLD",
    "SENTENCE_PACKING_MODES",
    "preserve_sentence_boundaries",
    "validate_sentence_packing_settings",
]
