"""Structured progress and pending retries for one running job.

A job's detail text is the only progress channel every handler shares. Batch
TTS synthesis retries inside worker threads, so those threads record what they
are waiting for here instead of writing to the queue themselves; the owning
worker folds the latest retry into its next heartbeat.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pandrator.logic.tts_retry import TTS_FAILURE_LABELS


@dataclass(frozen=True, slots=True)
class PendingRetry:
    segment: str
    attempt: int
    attempt_limit: int
    provider: str
    reason: str
    retry_at: datetime

    def payload(self) -> dict[str, object]:
        return {
            "segment": self.segment,
            "attempt": self.attempt,
            "attempt_limit": self.attempt_limit,
            "provider": self.provider,
            "reason": self.reason,
            "retry_at": self.retry_at.isoformat(),
        }


_RETRY_NOUNS = {"rate_limited": "rate limits"}


class RetryTracker:
    """The latest retry per in-flight request, safe across worker threads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pending: dict[str, PendingRetry] = {}

    def record(
        self,
        segment: str,
        *,
        attempt: int,
        attempt_limit: int,
        delay_seconds: float,
        provider: str,
        reason: str,
    ) -> None:
        now = datetime.now(UTC)
        entry = PendingRetry(
            segment=str(segment),
            attempt=max(1, int(attempt)),
            attempt_limit=max(1, int(attempt_limit)),
            provider=str(provider or ""),
            reason=str(reason or ""),
            retry_at=now + timedelta(seconds=max(0.0, float(delay_seconds))),
        )
        with self._lock:
            self._pending[entry.segment] = entry

    def clear(self, segment: str) -> None:
        with self._lock:
            self._pending.pop(str(segment), None)

    def pending(self, now: datetime | None = None) -> list[PendingRetry]:
        moment = now or datetime.now(UTC)
        with self._lock:
            # A backoff that has elapsed means the request is in flight again.
            self._pending = {
                key: entry
                for key, entry in self._pending.items()
                if entry.retry_at > moment
            }
            return sorted(self._pending.values(), key=lambda entry: entry.retry_at)

    def summary(self, now: datetime | None = None) -> dict[str, object] | None:
        moment = now or datetime.now(UTC)
        pending = self.pending(moment)
        if not pending:
            return None
        soonest = pending[0]
        providers = sorted({entry.provider for entry in pending if entry.provider})
        return {
            "pending_requests": len(pending),
            "max_attempt": max(entry.attempt for entry in pending),
            "attempt_limit": max(entry.attempt_limit for entry in pending),
            "provider": providers[0] if providers else None,
            "next_retry_in_seconds": max(
                0.0, round((soonest.retry_at - moment).total_seconds(), 1)
            ),
        }

    def detail_suffix(self, now: datetime | None = None) -> str | None:
        moment = now or datetime.now(UTC)
        pending = self.pending(moment)
        if not pending:
            return None
        soonest = pending[0]
        count = len(pending)
        noun = _RETRY_NOUNS.get(soonest.reason) or TTS_FAILURE_LABELS.get(
            soonest.reason, "a retry"
        )
        provider = soonest.provider or "the provider"
        seconds = max(0.0, (soonest.retry_at - moment).total_seconds())
        requests = "request" if count == 1 else "requests"
        return (
            f"{count} {requests} waiting on {noun} "
            f"({provider}, attempt {max(item.attempt for item in pending)} of "
            f"{max(item.attempt_limit for item in pending)}, next in {seconds:.0f} s)"
        )


class JobProgressState:
    """What one handler knows about its own progress beyond the bar and detail."""

    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.retry = RetryTracker()
        self._lock = threading.Lock()
        # A heartbeat holds this from collecting the pending values through
        # writing and acknowledging them, so a concurrent heartbeat cannot
        # acknowledge values that another one collected.
        self.heartbeat_lock = threading.Lock()
        self._detail: str | None = None
        self._blocks_done: int | None = None
        self._blocks_total: int | None = None
        self._blocks_kept = 0
        self._reported_detail: str | None = None
        self._reported_state: str | None = None
        # A returned value stays pending until its heartbeat write succeeds, so
        # a busy writer resends it instead of dropping it.
        self._pending_detail: str | None = None
        self._has_pending_detail = False
        self._pending_state_signature: str | None = None
        self._has_pending_state = False

    def observe(self, detail: str | None) -> None:
        """Remember the handler's own detail text for later heartbeats."""

        with self._lock:
            self._detail = detail

    def set_blocks(self, done: int | None, total: int | None, *, kept: int = 0) -> None:
        """Record blocks passed and planned; ``kept`` of the passed ones were
        completed by an earlier attempt and took no synthesis time now."""

        with self._lock:
            self._blocks_done = None if done is None else max(0, int(done))
            self._blocks_total = None if total is None else max(0, int(total))
            self._blocks_kept = max(0, int(kept))

    def _composed_detail_locked(self, now: datetime) -> str | None:
        suffix = self.retry.detail_suffix(now)
        if suffix is None:
            return self._detail
        if self._detail:
            return f"{self._detail}; {suffix}"
        return suffix

    def desired_detail(self, now: datetime | None = None) -> tuple[bool, str | None]:
        """Return whether the detail changed, and the value to write.

        A changed value may be ``None``: that is an explicit instruction to
        clear the stored detail once a retry suffix no longer applies.
        """

        moment = now or datetime.now(UTC)
        with self._lock:
            composed = self._composed_detail_locked(moment)
            if composed == self._reported_detail:
                return False, None
            self._pending_detail = composed
            self._has_pending_detail = True
            return True, composed

    def consume_state(
        self, now: datetime | None = None
    ) -> tuple[bool, dict[str, object] | None]:
        """Return whether the structured state changed, and its new value."""

        moment = now or datetime.now(UTC)
        with self._lock:
            state: dict[str, object] = {}
            if self._blocks_total is not None:
                state["blocks_done"] = self._blocks_done or 0
                state["blocks_total"] = self._blocks_total
                if self._blocks_kept:
                    state["blocks_kept"] = self._blocks_kept
            pending = self.retry.pending(moment)
            if pending:
                state["retry"] = {
                    "attempt_limit": max(entry.attempt_limit for entry in pending),
                    "provider": next(
                        (entry.provider for entry in pending if entry.provider), None
                    ),
                    "pending": [entry.payload() for entry in pending],
                }
            signature = repr(state)
            if signature == self._reported_state:
                return False, None
            self._pending_state_signature = signature
            self._has_pending_state = True
            return True, (state or None)

    def acknowledge(self) -> None:
        """Accept the values last returned, only once their write succeeded."""

        with self._lock:
            if self._has_pending_detail:
                self._reported_detail = self._pending_detail
                self._has_pending_detail = False
            if self._has_pending_state:
                self._reported_state = self._pending_state_signature
                self._has_pending_state = False


_STATES_LOCK = threading.Lock()
_STATES: dict[str, JobProgressState] = {}


def job_progress_state(job_id: str | None) -> JobProgressState | None:
    if not job_id:
        return None
    with _STATES_LOCK:
        return _STATES.get(job_id)


@contextmanager
def open_job_progress_state(job_id: str | None) -> Iterator[JobProgressState | None]:
    """Register this job's progress state for the worker's heartbeats."""

    if not job_id:
        yield None
        return
    state = JobProgressState(job_id)
    with _STATES_LOCK:
        _STATES[job_id] = state
    try:
        yield state
    finally:
        with _STATES_LOCK:
            if _STATES.get(job_id) is state:
                del _STATES[job_id]
