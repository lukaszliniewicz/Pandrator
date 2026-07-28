"""Pandrator's application-independent MCP sidecar."""

from __future__ import annotations

from .catalog import ACTION_CATALOG, ActionCatalog, ActionSpec
from .targets import TargetProfile, TargetRegistry

__version__ = "0.1.0"

__all__ = [
    "ACTION_CATALOG",
    "ActionCatalog",
    "ActionSpec",
    "TargetProfile",
    "TargetRegistry",
    "__version__",
]
