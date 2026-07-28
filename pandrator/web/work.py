"""Agent-safe projections over Pandrator's durable job queue."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.orm import Session

from .credentials import SecretRedactor
from .jobs import JobQueue
from .models import Job, JobEvent

WORK_EVENT_FIELDS = frozenset(
    {
        "agent_run_id",
        "artifact_id",
        "changed_entities",
        "code",
        "detail",
        "document_id",
        "generation_run_id",
        "job_kind",
        "level",
        "logger",
        "message",
        "model_id",
        "output_assembly_id",
        "progress",
        "reason",
        "retry_after_ms",
        "sample_id",
        "session_id",
        "source_artifact_id",
        "source_asset_id",
        "source_id",
        "status",
        "trace",
        "training_id",
        "training_run_id",
        "upload_id",
        "voice_id",
        "workflow_run_id",
    }
)
TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


def _bounded_json(value: Any, *, depth: int = 0) -> Any:
    """Bound model-visible diagnostics independently of downstream payload size."""

    if depth >= 6:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        items = list(value.items())
        bounded = {
            str(key)[:160]: _bounded_json(child, depth=depth + 1)
            for key, child in items[:50]
        }
        if len(items) > 50:
            bounded["_truncated"] = len(items) - 50
        return bounded
    if isinstance(value, (list, tuple)):
        items = list(value)
        bounded = [_bounded_json(child, depth=depth + 1) for child in items[:50]]
        if len(items) > 50:
            bounded.append(f"[TRUNCATED {len(items) - 50} ITEMS]")
        return bounded
    if isinstance(value, str):
        return value if len(value) <= 8_000 else f"{value[:8_000]}…[TRUNCATED]"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:8_000]


def _work_state(status: str) -> str:
    normalized = str(status or "queued").strip().lower()
    if normalized in {"canceled", "cancelled"}:
        return "cancelled"
    if normalized == "cancel_requested":
        return "running"
    if normalized in {"queued", "running", "succeeded", "failed"}:
        return normalized
    return "waiting"


class WorkError(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str | None = None
    message: str


class WorkView(BaseModel):
    """Stable public work projection; raw job inputs are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    type: Literal["job"] = "job"
    id: str
    kind: str
    session_id: str | None = None
    workflow_run_id: str | None = None
    state: Literal[
        "queued",
        "running",
        "waiting",
        "succeeded",
        "failed",
        "cancelled",
    ]
    progress: float | None = Field(default=None, ge=0.0, le=1.0)
    detail: str | None = None
    cancellable: bool
    poll_after_ms: int = Field(ge=0, le=60_000)
    result_summary: dict[str, Any] | None = None
    error: WorkError | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    updated_at: datetime | None = None


class WorkEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    id: int
    work_id: str | None = None
    event_type: str
    data: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class WorkEventPage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    items: list[WorkEvent]
    next_cursor: int
    retained_after: int


class EventBounds(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1"] = "1"
    oldest: int
    latest: int
    retained_after: int


class WorkService:
    """Facade that keeps raw queue payloads out of integration-facing reads."""

    def __init__(self, queue: JobQueue, redactor: SecretRedactor):
        self.queue = queue
        self.redactor = redactor

    def _project(self, job: Job) -> WorkView:
        state = _work_state(job.status)
        safe_detail = (
            self.redactor.redact(job.progress_detail)[:2_000]
            if job.progress_detail
            else None
        )
        result = None
        if isinstance(job.result_json, dict):
            result = _bounded_json(self.redactor.redact_value(job.result_json))
        error = None
        if job.error_code or job.error_message:
            error = WorkError(
                code=(
                    self.redactor.redact(job.error_code)[:160]
                    if job.error_code
                    else None
                ),
                message=self.redactor.redact(job.error_message or "The work failed.")[
                    :2_000
                ],
            )
        return WorkView(
            id=job.id,
            kind=str(job.kind),
            session_id=job.session_id,
            workflow_run_id=job.workflow_run_id,
            state=state,
            progress=max(0.0, min(float(job.progress or 0.0), 1.0)),
            detail=safe_detail,
            cancellable=state not in TERMINAL_STATES,
            poll_after_ms=(
                0 if state in TERMINAL_STATES else 750 if state == "running" else 1_500
            ),
            result_summary=result,
            error=error,
            created_at=job.created_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            updated_at=job.updated_at,
        )

    def _project_event(self, event: JobEvent) -> WorkEvent:
        source = event.payload_json if isinstance(event.payload_json, dict) else {}
        selected = {key: source[key] for key in WORK_EVENT_FIELDS if key in source}
        return WorkEvent(
            id=event.id,
            work_id=event.job_id,
            event_type=str(event.event_type),
            data=_bounded_json(self.redactor.redact_value(selected)),
            created_at=event.created_at,
        )

    def list(
        self,
        *,
        session_id: str | None = None,
        kinds: tuple[str, ...] = (),
        states: tuple[str, ...] = (),
        limit: int = 50,
    ) -> list[WorkView]:
        bounded_limit = max(1, min(int(limit), 100))
        kind_filter = {str(value).strip() for value in kinds if str(value).strip()}
        state_filter = {
            _work_state(str(value)) for value in states if str(value).strip()
        }
        results: list[WorkView] = []
        for job in self.queue.list(500):
            view = self._project(job)
            if session_id and view.session_id != session_id:
                continue
            if kind_filter and view.kind not in kind_filter:
                continue
            if state_filter and view.state not in state_filter:
                continue
            results.append(view)
            if len(results) >= bounded_limit:
                break
        return results

    def get(self, job_id: str) -> WorkView:
        return self._project(self.queue.get(job_id))

    def project_job(self, job: Job) -> WorkView:
        """Project a job already loaded in the caller's transaction."""

        return self._project(job)

    def events(
        self,
        job_id: str,
        *,
        after: int = 0,
        limit: int = 200,
    ) -> WorkEventPage:
        bounded_limit = max(1, min(int(limit), 500))
        records = [
            event
            for event in self.queue.events_for(job_id, 5_000)
            if event.id > max(0, int(after))
        ][:bounded_limit]
        bounds = self.event_bounds()
        return WorkEventPage(
            items=[self._project_event(event) for event in records],
            next_cursor=records[-1].id if records else max(0, int(after)),
            retained_after=bounds.retained_after,
        )

    def cancel(self, job_id: str, *, principal: object | None = None) -> WorkView:
        # Principal scope enforcement is introduced with scoped API principals.
        _ = principal
        return self._project(self.queue.request_cancel(job_id))

    def cancel_in_session(
        self,
        session: Session,
        job_id: str,
    ) -> WorkView:
        """Cancel and project work without opening a second transaction."""

        return self._project(
            self.queue.request_cancel_in_session(session, job_id)
        )

    def event_bounds(self) -> EventBounds:
        bounds = self.queue.event_bounds()
        return EventBounds(
            oldest=bounds["oldest"],
            latest=bounds["latest"],
            retained_after=max(0, bounds["oldest"] - 1),
        )

    def events_after(self, cursor: int, *, limit: int = 250) -> WorkEventPage:
        bounded_limit = max(1, min(int(limit), 500))
        records = self.queue.events_after(max(0, int(cursor)), bounded_limit)
        bounds = self.event_bounds()
        return WorkEventPage(
            items=[self._project_event(event) for event in records],
            next_cursor=records[-1].id if records else max(0, int(cursor)),
            retained_after=bounds.retained_after,
        )

    # Existing owner/admin routes retain their historical response shape. They
    # still pass through this facade so all queue-facing HTTP reads have one
    # application boundary while the new /work API stays payload-free.
    def diagnostic_list(self, limit: int = 100) -> list[Job]:
        return self.queue.list(limit)

    def diagnostic_get(self, job_id: str) -> Job:
        return self.queue.get(job_id)

    def diagnostic_events(
        self,
        job_id: str,
        limit: int = 1_000,
    ) -> list[JobEvent]:
        return self.queue.events_for(job_id, limit)

    def diagnostic_cancel(self, job_id: str) -> Job:
        return self.queue.request_cancel(job_id)
