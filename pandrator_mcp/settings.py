"""Sidecar settings that never contain downstream secret values."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field


def default_configuration_path() -> Path:
    configured = str(os.environ.get("PANDRATOR_MCP_CONFIG") or "").strip()
    if configured:
        return Path(configured).expanduser()
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return root / "Pandrator" / "mcp-targets.json"
    root = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return root / "pandrator" / "mcp-targets.json"


class McpSettings(BaseModel):
    """Process configuration selected outside model-visible tool arguments."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_name: str = Field(min_length=1, max_length=80)
    configuration_path: Path = Field(default_factory=default_configuration_path)
    request_timeout_seconds: float = Field(default=15.0, ge=1.0, le=120.0)
    maximum_response_bytes: int = Field(
        default=8 * 1024 * 1024,
        ge=64 * 1024,
        le=16 * 1024 * 1024,
    )
