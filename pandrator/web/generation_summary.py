"""Bounded generation status for the collapsed workspace drawer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select

from .database import Database
from .models import (
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
)


class GenerationActivitySummary(BaseModel):
    id: str
    status: str
    progress: float
    progress_detail: str | None


class GenerationSummary(BaseModel):
    session_id: str
    plan_revision_id: str | None
    total: int = Field(description="All blocks in the active plan, including excluded blocks.")
    included_total: int = Field(description="Nonexcluded blocks in the active plan.")
    active_run: GenerationActivitySummary | None
    assembly: GenerationActivitySummary | None


# Match preferredActiveRun's status priority, before recency. This status-only
# read deliberately keeps actual repair workers; expanded histories separately
# group those children under their original run for display and controls.
_ACTIVE_RUN_STATUSES = (
    "running", "pausing", "pause_requested", "cancel_requested", "queued", "paused"
)


def _activity(
    identifier: str,
    status: str,
    progress: float | None,
    detail: str | None,
    *,
    completed: frozenset[str] = frozenset({"completed"}),
) -> GenerationActivitySummary:
    return GenerationActivitySummary(
        id=identifier,
        status=status,
        progress=1.0 if status in completed else float(progress or 0.0),
        progress_detail=detail,
    )


def get_generation_summary(database: Database, session_id: str) -> dict[str, Any]:
    """Read one snapshot without hydrating plans, histories, takes or artifacts."""
    with database.snapshot_session() as db:
        header = db.execute(
            select(SessionRecord.id, GenerationPlanRevision.id)
            .outerjoin(GenerationPlan, GenerationPlan.session_id == SessionRecord.id)
            .outerjoin(
                GenerationPlanRevision,
                and_(
                    GenerationPlanRevision.id == GenerationPlan.active_revision_id,
                    GenerationPlanRevision.plan_id == GenerationPlan.id,
                ),
            )
            .where(SessionRecord.id == session_id, SessionRecord.trashed_at.is_(None))
        ).one_or_none()
        if header is None:
            raise KeyError(session_id)
        plan_revision_id = header[1]
        total, included_total = 0, 0
        if plan_revision_id is not None:
            counts = db.execute(
                select(
                    func.count(GenerationSegment.id),
                    func.coalesce(
                        func.sum(case((GenerationSegment.removed.is_(False), 1), else_=0)), 0
                    ),
                ).where(GenerationSegment.plan_revision_id == plan_revision_id)
            ).one()
            total, included_total = int(counts[0]), int(counts[1])
        run = db.execute(
            select(GenerationRun.id, GenerationRun.status, Job.progress, Job.progress_detail)
            .outerjoin(
                Job,
                and_(Job.id == GenerationRun.job_id, Job.session_id == session_id),
            )
            .where(
                GenerationRun.session_id == session_id,
                GenerationRun.status.in_(_ACTIVE_RUN_STATUSES),
            )
            .order_by(
                case(
                    {status: index for index, status in enumerate(_ACTIVE_RUN_STATUSES)},
                    value=GenerationRun.status,
                ),
                GenerationRun.sequence_number.desc(),
                GenerationRun.created_at.desc(),
                GenerationRun.id.desc(),
            )
            .limit(1)
        ).one_or_none()
        assembly = db.execute(
            select(OutputAssembly.id, OutputAssembly.status, Job.progress, Job.progress_detail)
            .outerjoin(
                Job,
                and_(Job.id == OutputAssembly.job_id, Job.session_id == session_id),
            )
            .where(OutputAssembly.session_id == session_id)
            .order_by(OutputAssembly.created_at.desc(), OutputAssembly.id.desc())
            .limit(1)
        ).one_or_none()
        return GenerationSummary(
            session_id=session_id,
            plan_revision_id=plan_revision_id,
            total=total,
            included_total=included_total,
            active_run=_activity(*run) if run is not None else None,
            assembly=_activity(*assembly, completed=frozenset({"completed", "stale"}))
            if assembly is not None
            else None,
        ).model_dump()
