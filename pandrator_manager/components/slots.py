"""Versioned component-slot pointer helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..context import WorkspaceLayout


def component_container(layout: WorkspaceLayout, component_id: str) -> Path:
    if component_id == "pandrator":
        return layout.root / "app"
    return layout.service_root(component_id)


def component_pointer(layout: WorkspaceLayout, component_id: str) -> Path:
    return component_container(layout, component_id) / "current.json"


def active_component_path(
    layout: WorkspaceLayout,
    component_id: str,
) -> Path | None:
    active = _active_component_pointer(layout, component_id)
    return active[0] if active is not None else None


def active_component_metadata(
    layout: WorkspaceLayout,
    component_id: str,
) -> dict[str, str] | None:
    """Return metadata recorded by a validated active component pointer.

    The metadata is intentionally read only after the pointer path has passed
    the same ``versions`` containment check used by ``active_component_path``.
    Empty or non-string values are ignored, and legacy/manual pointers without
    a recorded revision remain metadata-free.
    """

    active = _active_component_pointer(layout, component_id)
    if active is None:
        return None
    _path, payload = active
    metadata: dict[str, str] = {}
    for key in ("version", "revision"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def _active_component_pointer(
    layout: WorkspaceLayout,
    component_id: str,
) -> tuple[Path, dict[str, Any]] | None:
    pointer = component_pointer(layout, component_id)
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            return None
        raw_candidate = payload.get("path")
        if not isinstance(raw_candidate, str) or not raw_candidate:
            return None
        candidate = Path(raw_candidate)
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None
    versions = component_container(layout, component_id) / "versions"
    try:
        resolved = layout.require_within(
            candidate,
            roots=(versions,),
        )
    except Exception:
        return None
    return (resolved, payload) if resolved.is_dir() else None
