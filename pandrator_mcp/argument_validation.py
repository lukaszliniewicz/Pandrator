"""Reject undeclared native tool arguments before the SDK can discard them."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from .errors import PandratorMcpError

if TYPE_CHECKING:
    from mcp.server.extension import Extension


def create_argument_validation_extension(
    tool_schema: Callable[[str], Mapping[str, Any] | None],
    tool_failure: Callable[[PandratorMcpError, str], Exception],
) -> Extension:
    """Borrow current registration schemas and the adapter's safe failure projection."""
    from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
    from mcp.server.extension import Extension
    from mcp.types import CallToolRequestParams, CallToolResult, TextContent

    class StrictToolArguments(Extension):
        identifier = "io.pandrator/strict-tool-arguments"

        async def intercept_tool_call(
            self,
            params: CallToolRequestParams,
            ctx: ServerRequestContext[Any, Any],
            call_next: CallNext,
        ) -> HandlerResult:
            schema = tool_schema(params.name)
            if schema is not None:
                extra_fields = sorted((params.arguments or {}).keys() - schema["properties"].keys())
                if extra_fields:
                    error = PandratorMcpError(
                        "validation_error",
                        "The tool input is invalid.",
                        details={
                            "errors": [
                                {
                                    "type": "extra_forbidden",
                                    "loc": [field],
                                    "msg": "Extra inputs are not permitted",
                                }
                                for field in extra_fields
                            ]
                        },
                    )
                    # An interceptor is outside the SDK's ToolError catch. Return
                    # the same native failure channel without invoking the handler.
                    return CallToolResult(
                        content=[
                            TextContent(
                                type="text", text=str(tool_failure(error, str(uuid.uuid4())))
                            )
                        ],
                        is_error=True,
                    )
            return await call_next(ctx)

    return StrictToolArguments()
