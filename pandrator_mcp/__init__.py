"""Pandrator's application-independent MCP sidecar."""

from __future__ import annotations

from .catalog import ACTION_CATALOG, ActionCatalog, ActionSpec
from .targets import TargetProfile, TargetRegistry

__version__ = "0.6.3"
MCP_CONTRACT_REVISION = "2026-10-17.1"

__all__ = [
    "ACTION_CATALOG",
    "MCP_CONTRACT_REVISION",
    "ActionCatalog",
    "ActionSpec",
    "TargetProfile",
    "TargetRegistry",
    "__version__",
]
