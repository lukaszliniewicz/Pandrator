"""Pinned inventory data shared by catalogue and parameter projections."""

from __future__ import annotations

import copy
import json
from functools import lru_cache
from pathlib import Path
from typing import Any


@lru_cache(maxsize=1)
def inventory() -> dict[str, Any]:
    return json.loads(
        Path(__file__).with_name("audio_cpp_inventory.json").read_text(encoding="utf-8")
    )


def family_metadata(family: str) -> dict[str, Any]:
    family = {"fish_audio_s2": "fish_audio"}.get(family, family)
    found = next((row for row in inventory()["families"] if row["id"] == family), {})
    return copy.deepcopy(found)
