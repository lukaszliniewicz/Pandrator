"""Elapsed budgets and shared quota admission for TTS synthesis retries."""

from __future__ import annotations

import contextvars
import hashlib
import json
import math
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

DEFAULT_TTS_MAX_ATTEMPTS = 10
DEFAULT_TTS_RETRY_TIMEOUT_SECONDS = 900


def normalize_retry_timeout_seconds(value: Any) -> int:
    """Normalize the persisted retry budget to its supported integer range."""
    try:
        timeout = int(value)
    except (TypeError, ValueError, OverflowError):
        timeout = DEFAULT_TTS_RETRY_TIMEOUT_SECONDS
    return max(1, min(3600, timeout))


class TtsRetryBudgetExceeded(TimeoutError):
    """A retry operation has no elapsed budget left for another action."""

    def __init__(self, message: str, *, status_code: int = 0):
        super().__init__(message)
        self.status_code = int(status_code or 0)


class TtsRetryBudget:
    """A lazy monotonic deadline shared by one logical synthesis operation."""

    def __init__(
        self,
        maximum_elapsed_seconds: int | float = DEFAULT_TTS_RETRY_TIMEOUT_SECONDS,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.maximum_elapsed_seconds = normalize_retry_timeout_seconds(
            maximum_elapsed_seconds
        )
        self._clock = clock
        self._started_at: float | None = None

    def start(self) -> None:
        if self._started_at is None:
            self._started_at = self._clock()

    @property
    def started(self) -> bool:
        return self._started_at is not None

    def remaining_seconds(self, *, start: bool = False) -> float:
        if self._started_at is None:
            if not start:
                return float(self.maximum_elapsed_seconds)
            self.start()
        assert self._started_at is not None
        return max(
            0.0,
            float(self.maximum_elapsed_seconds) - (self._clock() - self._started_at),
        )

    def ensure_time_remaining(self, action: str = "continue synthesis") -> float:
        remaining = self.remaining_seconds(start=True)
        if remaining <= 0:
            raise TtsRetryBudgetExceeded(
                f"The TTS retry budget expired before it could {action}."
            )
        return remaining

    def request_timeout(self, default_seconds: float) -> float:
        try:
            default = float(default_seconds)
        except (TypeError, ValueError, OverflowError):
            default = float(DEFAULT_TTS_RETRY_TIMEOUT_SECONDS)
        if not math.isfinite(default) or default <= 0:
            default = float(DEFAULT_TTS_RETRY_TIMEOUT_SECONDS)
        remaining = self.ensure_time_remaining("start a provider request")
        return min(default, remaining)


_active_budget: contextvars.ContextVar[TtsRetryBudget | None] = contextvars.ContextVar(
    "pandrator_tts_retry_budget", default=None
)


def current_retry_budget() -> TtsRetryBudget | None:
    return _active_budget.get()


@contextmanager
def retry_budget_scope(
    timeout_seconds: int | float = DEFAULT_TTS_RETRY_TIMEOUT_SECONDS,
    *,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[TtsRetryBudget]:
    """Install one lazy budget, reusing it through recursive wrappers."""
    active = current_retry_budget()
    if active is not None:
        yield active
        return
    budget = TtsRetryBudget(timeout_seconds, clock=clock)
    token = _active_budget.set(budget)
    try:
        yield budget
    finally:
        _active_budget.reset(token)


def request_timeout(default_seconds: float) -> float:
    """Clamp a provider timeout to this synthesis operation's remaining time."""
    budget = current_retry_budget()
    return (
        default_seconds
        if budget is None
        else budget.request_timeout(default_seconds)
    )


def make_retry_scope_key(
    *,
    provider: str,
    endpoint: str,
    model: str,
    account: str = "",
    project: str = "",
    location: str = "",
) -> str:
    """Return an opaque key; credentials and endpoint details are never retained."""
    account_digest = (
        hashlib.sha256(account.encode("utf-8", "surrogatepass")).hexdigest()
        if account
        else ""
    )
    scope = {
        "provider": str(provider or "").strip().lower(),
        "endpoint": str(endpoint or "").strip().rstrip("/").lower(),
        "model": str(model or "").strip().lower(),
        "account": account_digest,
        "project": str(project or "").strip().lower(),
        "location": str(location or "").strip().lower(),
    }
    payload = json.dumps(scope, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True, slots=True)
class TtsRetryAdmission:
    scope_key: str
    generation: int


@dataclass(slots=True)
class _GateState:
    blocked_until: float = 0.0
    generation: int = 0
    recovering: bool = False
    next_admission: float = 0.0


class TtsRetryGate:
    """Process-local shared 429 cooldown with staggered recovery admissions."""

    def __init__(
        self,
        *,
        clock: Callable[[], float] = time.monotonic,
        waiter: Callable[[float, Any | None], bool] | None = None,
    ) -> None:
        self._clock = clock
        self._waiter = waiter or self._default_waiter
        self._lock = threading.Lock()
        self._states: dict[str, _GateState] = {}

    @staticmethod
    def _default_waiter(delay: float, cancel_event: Any | None) -> bool:
        if cancel_event is not None and hasattr(cancel_event, "wait"):
            return not bool(cancel_event.wait(delay))
        time.sleep(delay)
        return True

    @staticmethod
    def _safe_delay(delay: Any) -> float:
        try:
            value = float(delay)
        except (TypeError, ValueError, OverflowError):
            return 0.0
        return max(0.0, value) if math.isfinite(value) else 0.0

    def publish_429(self, scope_key: str, delay: float) -> int:
        """Extend a shared cooldown and start a fresh recovery generation."""
        now = self._clock()
        cooldown = self._safe_delay(delay)
        with self._lock:
            state = self._states.setdefault(scope_key, _GateState())
            state.generation += 1
            state.blocked_until = max(state.blocked_until, now + cooldown)
            state.recovering = True
            state.next_admission = state.blocked_until
            return state.generation

    def wait_for_admission(
        self,
        scope_key: str,
        *,
        cancel_event: Any | None = None,
        budget: TtsRetryBudget | None = None,
    ) -> TtsRetryAdmission | None:
        """Wait outside the state lock, rechecking peer cooldown extensions."""
        while True:
            if cancel_event is not None and cancel_event.is_set():
                return None
            now = self._clock()
            if budget is not None:
                budget.ensure_time_remaining("admit a provider request")

            with self._lock:
                state = self._states.get(scope_key)
                if state is None:
                    return TtsRetryAdmission(scope_key, 0)
                ready_at = state.blocked_until
                if state.recovering:
                    ready_at = max(ready_at, state.next_admission)
                if now >= ready_at:
                    generation = state.generation
                    if state.recovering:
                        state.next_admission = now + 1.0
                    return TtsRetryAdmission(scope_key, generation)
                delay = ready_at - now

            if budget is not None:
                remaining = budget.ensure_time_remaining("wait for provider admission")
                if delay >= remaining:
                    raise TtsRetryBudgetExceeded(
                        "The TTS retry budget cannot accommodate the provider's "
                        "shared rate-limit cooldown."
                    )
            if not self._waiter(delay, cancel_event):
                return None
            if budget is not None and budget.remaining_seconds() <= 0:
                raise TtsRetryBudgetExceeded(
                    "The TTS retry budget expired while waiting for provider admission."
                )

    def record_success(self, admission: TtsRetryAdmission) -> None:
        """Restore normal concurrency only after a current-generation success."""
        with self._lock:
            state = self._states.get(admission.scope_key)
            if state is None or state.generation != admission.generation:
                return
            if state.recovering:
                state.blocked_until = 0.0
                state.next_admission = 0.0
                state.recovering = False

    def clear(self) -> None:
        """Clear process-local state; intended for isolated tests and shutdown."""
        with self._lock:
            self._states.clear()
