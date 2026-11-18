"""One server-side wait ceiling shared by every bounded polling tool.

A host's own MCP request timeout is usually shorter than the largest waits the
tool schemas allow, so a long server-side wait fails at the host without a
result. Every waiting tool therefore reports the wait it actually used and
returns ``timed_out`` so the caller can poll again instead of reading a silent
truncation as completion.
"""

from __future__ import annotations

from typing import Any

from .settings import DEFAULT_MAXIMUM_WAIT_SECONDS

WAIT_DESCRIPTION = (
    f"The server waits at most {DEFAULT_MAXIMUM_WAIT_SECONDS} seconds per call "
    "(configurable); poll again while `timed_out` is true."
)


def wait_ceiling(runtime: Any) -> int:
    """Return the configured ceiling, tolerating lightweight test runtimes."""

    settings = getattr(runtime, "settings", None)
    configured = getattr(settings, "maximum_wait_seconds", DEFAULT_MAXIMUM_WAIT_SECONDS)
    try:
        return max(1, min(int(configured), 3_600))
    except (TypeError, ValueError):
        return DEFAULT_MAXIMUM_WAIT_SECONDS


def requested_wait_seconds(requested: int | None) -> int:
    return max(0, int(requested or 0))


def effective_wait_seconds(runtime: Any, requested: int | None) -> int:
    """Clamp one requested wait to the configured ceiling; zero stays zero."""

    requested_value = requested_wait_seconds(requested)
    if requested_value == 0:
        return 0
    return min(requested_value, wait_ceiling(runtime))


def wait_receipt(*, requested: int | None, effective: int, terminal: bool) -> dict[str, Any]:
    """Report the requested and effective waits with an honest timeout flag."""

    return {
        "wait_requested_seconds": requested_wait_seconds(requested),
        "wait_effective_seconds": max(0, int(effective)),
        "timed_out": bool(effective and not terminal),
    }


def bounded_suggestion(runtime: Any, requested: int | None) -> int:
    """Return a suggested follow-up wait that never exceeds the ceiling."""

    ceiling = wait_ceiling(runtime)
    return max(1, min(requested_wait_seconds(requested) or ceiling, ceiling))


__all__ = [
    "bounded_suggestion",
    "effective_wait_seconds",
    "requested_wait_seconds",
    "wait_ceiling",
    "wait_receipt",
    "WAIT_DESCRIPTION",
]
