"""Pure guidance handlers used by the MCP protocol adapter."""

from __future__ import annotations

from typing import Any

from ..context import McpRuntime
from ..schemas import ExplainSystemInput


def explain_system(
    runtime: McpRuntime,
    arguments: ExplainSystemInput,
) -> dict[str, Any]:
    guide = runtime.guides.get(arguments.topic)
    guide["audience"] = arguments.audience
    if arguments.include_live_context and runtime.application is not None:
        try:
            guide["live_context"] = {
                "health": runtime.application.health(),
                "identity": runtime.application.identity(),
            }
        except Exception:
            guide["live_context"] = {
                "available": False,
                "note": (
                    "Static guidance is available, but live target context could not be loaded."
                ),
            }
    return guide
