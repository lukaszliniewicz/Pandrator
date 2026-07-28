"""System-inspection tool arguments."""

from __future__ import annotations

from .common import ToolInput


class TargetStatusInput(ToolInput):
    include_authenticated_identity: bool = True


class CapabilitiesInput(ToolInput):
    pass


class SystemStatusInput(ToolInput):
    include_capabilities: bool = True
    include_manager: bool = True
