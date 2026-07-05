from __future__ import annotations

import re


VISUAL_TAGS = {"figure", "figcaption", "img"}

VISUAL_SEMANTIC_VALUES = {
    "caption",
    "doc-cover",
    "cover",
    "figure",
    "image",
    "img",
    "illustration",
    "illustrations",
}

VISUAL_SELECTOR_VALUES = {
    "artwork",
    "caption",
    "captioned",
    "cover",
    "coverimage",
    "diagram",
    "fig",
    "figcaption",
    "figcenter",
    "figleft",
    "figure",
    "figright",
    "frontis",
    "frontispiece",
    "graphic",
    "graphics",
    "ill",
    "illo",
    "illus",
    "illustration",
    "illustrations",
    "image",
    "images",
    "img",
    "map",
    "photo",
    "photograph",
    "pic",
    "picture",
    "plate",
}

VISUAL_SELECTOR_RE = re.compile(
    r"^(?:"
    r"caption|caption\d+|"
    r"cover(?:[-_\s]?(?:fig|image|img|illustration))?|"
    r"fig|fig\d+|fig[-_\s]?\d+|fig(?:caption|center|left|right)|"
    r"figure|figure\d+|"
    r"frontis(?:piece)?|"
    r"ill|illo\d*|illus(?:t|tration|trations?)?|illustrations?|"
    r"image\d*|img\d*|"
    r"plate\d*|"
    r"photo(?:graph)?|picture|pic|artwork|graphics?|map|diagram"
    r")$",
    re.IGNORECASE,
)


def visual_block_indexes(blocks: list[dict], protected_indexes: set[int] | None = None) -> set[int]:
    protected = protected_indexes or set()
    return {
        idx
        for idx, block in enumerate(blocks)
        if idx not in protected and is_visual_material_block(block)
    }


def is_visual_material_block(block: dict) -> bool:
    """Return True for EPUB blocks that are clearly illustration/caption material."""
    tag = str(block.get("tag", "") or "").strip().lower()
    if tag in VISUAL_TAGS:
        return True

    semantic_values = _values_from_block(block, ("role", "roles", "epub_type", "epub_types"))
    if any(_is_visual_semantic_value(value) for value in semantic_values):
        return True

    selector_values = _values_from_block(block, ("classes", "id", "nested_ids", "aria_label"))
    return any(_is_visual_selector_value(value) for value in selector_values)


def _is_visual_semantic_value(value: str) -> bool:
    if value in VISUAL_SEMANTIC_VALUES:
        return True
    parts = set(_split_value(value))
    return bool(parts.intersection(VISUAL_SEMANTIC_VALUES))


def _is_visual_selector_value(value: str) -> bool:
    if value in VISUAL_SELECTOR_VALUES:
        return True
    if VISUAL_SELECTOR_RE.match(value):
        return True
    parts = set(_split_value(value))
    return bool(parts.intersection(VISUAL_SELECTOR_VALUES))


def _values_from_block(block: dict, keys: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    for key in keys:
        raw_value = block.get(key)
        if not raw_value:
            continue
        if isinstance(raw_value, str):
            raw_items = raw_value.split()
        else:
            raw_items = raw_value
        for item in raw_items:
            normalized = str(item or "").strip().lower()
            if normalized:
                values.append(normalized)
    return values


def _split_value(value: str) -> list[str]:
    return [
        part
        for part in re.split(r"[^0-9a-z]+", value.lower())
        if part
    ]
