"""Per-tool correlation metadata propagated to downstream services."""

from __future__ import annotations

import uuid
from contextvars import ContextVar, Token
from dataclasses import dataclass

_REQUEST_ID: ContextVar[str | None] = ContextVar(
    "pandrator_mcp_request_id",
    default=None,
)
_TRACE_ID: ContextVar[str | None] = ContextVar(
    "pandrator_mcp_trace_id",
    default=None,
)


@dataclass(frozen=True, slots=True)
class RequestContextTokens:
    request_id: Token[str | None]
    trace_id: Token[str | None]


def begin_request(request_id: str) -> RequestContextTokens:
    """Install a request and trace identity for one MCP tool invocation."""

    return RequestContextTokens(
        request_id=_REQUEST_ID.set(request_id),
        trace_id=_TRACE_ID.set(uuid.uuid4().hex),
    )


def end_request(tokens: RequestContextTokens) -> None:
    """Restore the prior request context."""

    _TRACE_ID.reset(tokens.trace_id)
    _REQUEST_ID.reset(tokens.request_id)


def correlation_headers() -> dict[str, str]:
    """Return W3C-compatible correlation headers for one downstream call."""

    request_id = _REQUEST_ID.get() or str(uuid.uuid4())
    trace_id = _TRACE_ID.get() or uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return {
        "X-Request-ID": request_id,
        "traceparent": f"00-{trace_id}-{span_id}-01",
    }
