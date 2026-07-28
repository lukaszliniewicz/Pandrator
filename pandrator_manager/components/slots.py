"""Versioned component-slot pointer helpers."""

from __future__ import annotations

import json
from pathlib import Path

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
    pointer = component_pointer(layout, component_id)
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
        candidate = Path(str(payload["path"]))
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
    return resolved if resolved.is_dir() else None
