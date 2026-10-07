"""Shared defaults and strict settings for derived audiobook presentations."""

from __future__ import annotations

import math
import re
from typing import Any

BOOK_DEFAULTS: dict[str, Any] = {
    "book_style": "reading",
    "book_cue_mode": "passages",
    "book_text_mode": "auto",
    "book_target_seconds": 12,
    "book_max_seconds": 20,
    "book_max_lines": 4,
    "book_font_size": 80,
    "book_heading_font_size": 44,
    "book_font_path": "",
    "book_resolution": "1080p",
    "book_background": "#202427",
    "book_foreground": "#f0eade",
    "book_alignment": "center",
    "book_show_heading": True,
    "book_alignment_engine": "auto",
    "book_use_native_timings": True,
}

BOOK_CHOICES = {
    "book_style": ("reading", "captions"),
    "book_cue_mode": ("passages", "segments"),
    "book_text_mode": ("auto", "original", "spoken"),
    "book_resolution": ("720p", "1080p"),
    "book_alignment": ("center", "left"),
    "book_alignment_engine": ("auto", "crispasr", "qwen"),
}
BOOK_LIMITS = {
    "book_target_seconds": (2, 30),
    "book_max_seconds": (3, 60),
    "book_max_lines": (1, 8),
    "book_font_size": (28, 96),
    "book_heading_font_size": (18, 72),
}


def validate_book_settings(settings: dict[str, Any]) -> None:
    for key, choices in BOOK_CHOICES.items():
        if key in settings and settings[key] not in choices:
            raise ValueError(f"{key} must be one of {', '.join(choices)}.")
    for key, (minimum, maximum) in BOOK_LIMITS.items():
        if key in settings and (
            type(settings[key]) is not int or not minimum <= settings[key] <= maximum
        ):
            raise ValueError(f"{key} must be an integer from {minimum} to {maximum}.")
    target = settings.get("book_target_seconds", BOOK_DEFAULTS["book_target_seconds"])
    ceiling = settings.get("book_max_seconds", BOOK_DEFAULTS["book_max_seconds"])
    if target > ceiling:
        raise ValueError("The passage target must not exceed its maximum duration.")
    for key in ("book_background", "book_foreground"):
        if key in settings and (
            not isinstance(settings[key], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", settings[key])
        ):
            raise ValueError(f"{key} must be a six-digit hexadecimal colour.")
    for key in ("book_show_heading", "book_use_native_timings", "book_preview"):
        if key in settings and type(settings[key]) is not bool:
            raise ValueError(f"{key} must be a boolean.")
    if "book_font_path" in settings and (
        not isinstance(settings["book_font_path"], str) or len(settings["book_font_path"]) > 4096
    ):
        raise ValueError("book_font_path must be a font file path.")
    for key, minimum, maximum in (
        ("book_preview_start_seconds", 0, 864000),
        ("book_preview_duration_seconds", 1, 30),
    ):
        if key in settings:
            value = settings[key]
            if (
                type(value) not in (int, float)
                or not math.isfinite(value)
                or not minimum <= value <= maximum
            ):
                raise ValueError(f"{key} must be from {minimum} to {maximum} seconds.")
