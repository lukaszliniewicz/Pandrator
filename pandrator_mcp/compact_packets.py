"""Stable manifest caching for model-facing compact claim packets."""

from __future__ import annotations

import hashlib
import json
from typing import Any


def compact_manifest_packet(
    projected: dict[str, Any],
    manifest: dict[str, Any],
    *,
    known_manifest_hash: str | None,
) -> dict[str, Any]:
    canonical = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest_hash = hashlib.sha256(canonical).hexdigest()
    result = dict(projected)
    result["packet_format"] = "compact-v1"
    result["manifest_hash"] = manifest_hash
    if known_manifest_hash != manifest_hash:
        result["manifest"] = manifest
    return result
