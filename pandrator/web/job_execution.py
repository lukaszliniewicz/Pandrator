"""Worker lease identity for fenced domain commits on the handler thread."""

from contextvars import ContextVar
from typing import TypedDict


class JobLease(TypedDict):
    job_id: str
    owner_id: str
    lease_generation: int


current_job_lease: ContextVar[JobLease | None] = ContextVar(
    "pandrator_current_job_lease", default=None
)
