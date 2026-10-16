"""Validation for source-cleaning narration extraction preferences."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pandrator.logic.source_cleaning.epub_narration import (
    EPUB_IMAGE_MODES,
    EPUB_TABLE_MODES,
)


def validate_source_narration_settings(settings: Mapping[str, Any]) -> None:
    """Validate supplied EPUB table and image narration modes.

    Missing values inherit configured defaults. Supplied values are strict so
    invalid settings fail before a job captures immutable inputs.
    """

    if not isinstance(settings, Mapping):
        raise ValueError("source_cleaning settings must be an object.")
    for name, choices in (
        ("epub_table_mode", EPUB_TABLE_MODES),
        ("epub_image_mode", EPUB_IMAGE_MODES),
    ):
        if name not in settings:
            continue
        value = settings[name]
        if not isinstance(value, str) or value not in choices:
            allowed = ", ".join(choices)
            raise ValueError(f"{name} must be one of: {allowed}.")
