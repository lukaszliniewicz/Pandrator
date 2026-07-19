"""Shared, cancellation-aware retry decisions for remote provider calls."""

from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Mapping


RETRYABLE_HTTP_STATUSES = {408, 409, 425, 429}
NON_RETRYABLE_EXCEPTION_NAMES = {
    "authenticationerror",
    "badrequesterror",
    "contentpolicyviolationerror",
    "contextwindowexceedederror",
    "invalidrequesterror",
    "notfounderror",
    "permissiondeniederror",
    "unprocessableentityerror",
}
RETRYABLE_EXCEPTION_NAMES = {
    "apiconnectionerror",
    "apitimeouterror",
    "internalservererror",
    "ratelimiterror",
    "serviceunavailableerror",
    "timeouterror",
}


def _response_from_error(error: BaseException) -> Any | None:
    current: BaseException | None = error
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        response = getattr(current, "response", None)
        if response is not None:
            return response
        next_error = getattr(current, "__cause__", None) or getattr(current, "__context__", None)
        current = next_error if isinstance(next_error, BaseException) else None
    return None


def status_code_from_error(error: BaseException) -> int:
    """Extract an HTTP status from Requests, LiteLLM, or wrapped exceptions."""
    for value in (
        getattr(error, "status_code", None),
        getattr(_response_from_error(error), "status_code", None),
    ):
        try:
            if value is not None:
                return int(value)
        except (TypeError, ValueError):
            continue
    return 0


def retryable_error(error: BaseException) -> bool:
    """Return whether repeating a provider request has a reasonable chance to work."""
    status = status_code_from_error(error)
    if status:
        return status in RETRYABLE_HTTP_STATUSES or status >= 500
    name = type(error).__name__.lower()
    if name in NON_RETRYABLE_EXCEPTION_NAMES or isinstance(error, (TypeError, ValueError)):
        return False
    if name in RETRYABLE_EXCEPTION_NAMES:
        return True
    # Connection resets and provider SDK transport wrappers are inconsistent
    # about their exception types. An unknown status-less runtime failure gets
    # a bounded retry, while explicit validation/configuration failures do not.
    return True


def _headers_from_error(error: BaseException) -> Mapping[str, Any]:
    response = _response_from_error(error)
    headers = getattr(response, "headers", None)
    if isinstance(headers, Mapping):
        return {str(key).lower(): value for key, value in headers.items()}
    return {}


def _retry_after_value(value: Any, *, now: datetime) -> float:
    raw = str(value or "").strip()
    if not raw:
        return 0.0
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds())


def retry_after_seconds(error: BaseException, *, now: datetime | None = None) -> float:
    """Read numeric, HTTP-date, millisecond, and reset-time rate-limit headers."""
    headers = _headers_from_error(error)
    current = now or datetime.now(timezone.utc)
    delay = _retry_after_value(headers.get("retry-after"), now=current)
    try:
        delay = max(delay, float(headers.get("retry-after-ms") or 0) / 1000.0)
    except (TypeError, ValueError):
        pass
    for key in ("x-ratelimit-reset", "x-rate-limit-reset"):
        try:
            reset = float(headers.get(key) or 0)
        except (TypeError, ValueError):
            continue
        if reset > 1_000_000_000:
            delay = max(delay, reset - current.timestamp())
        elif reset > 0:
            delay = max(delay, reset)
    return max(0.0, delay)


def retry_delay_seconds(
    failed_attempt: int,
    *,
    retry_after: float = 0.0,
    base_delay: float = 1.0,
    maximum_delay: float = 90.0,
    jitter_ratio: float = 0.2,
) -> float:
    """Calculate capped exponential backoff while honoring provider guidance."""
    attempt = max(1, int(failed_attempt))
    ceiling = max(0.0, float(maximum_delay))
    exponential = min(ceiling, max(0.0, float(base_delay)) * (2 ** (attempt - 1)))
    jitter = exponential * max(0.0, float(jitter_ratio))
    randomized = exponential + (random.uniform(-jitter, jitter) if jitter else 0.0)
    return min(ceiling, max(0.0, float(retry_after), randomized))


def wait_for_retry(delay: float, cancel_event: Any | None = None) -> bool:
    """Wait for a retry; return False when cancellation interrupts the wait."""
    seconds = max(0.0, float(delay))
    if cancel_event is not None and hasattr(cancel_event, "wait"):
        return not bool(cancel_event.wait(seconds))
    time.sleep(seconds)
    return True
