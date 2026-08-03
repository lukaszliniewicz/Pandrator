"""Shared tray artwork for native StatusNotifier and pystray backends."""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image, ImageDraw

TRAY_ICON_PATH = Path(__file__).with_name("pandrator-tray.png")


def _fallback_icon(size: int) -> Image.Image:
    image = Image.new("RGBA", (size, size), "#211b2b")
    draw = ImageDraw.Draw(image)
    inset = max(3, size // 9)
    radius = max(4, size // 5)
    draw.rounded_rectangle(
        (inset, inset, size - inset, size - inset),
        radius=radius,
        fill="#ad8ce8",
    )
    draw.polygon(
        (
            (size * 11 // 32, size * 9 // 32),
            (size * 24 // 32, size // 2),
            (size * 11 // 32, size * 23 // 32),
        ),
        fill="#211b2b",
    )
    return image


def load_tray_icon(size: int = 64) -> Image.Image:
    """Load and resize the packaged Pandrator mark, with a safe fallback."""

    try:
        with Image.open(TRAY_ICON_PATH) as source:
            source.load()
            logo = source.convert("RGBA")
    except (OSError, ValueError):
        logging.warning(
            "Pandrator tray logo could not be loaded from %s; using fallback icon.",
            TRAY_ICON_PATH,
        )
        return _fallback_icon(size)
    if logo.size == (size, size):
        return logo
    return logo.resize((size, size), Image.Resampling.LANCZOS)
