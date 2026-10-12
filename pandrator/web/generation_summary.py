"""Bounded generation status for the collapsed workspace drawer."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field
from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from .database import Database
from .models import (
    Artifact,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
    UsageEvent,
)


class GenerationActivitySummary(BaseModel):
    id: str
    status: str
    progress: float
    progress_detail: str | None
    job_id: str | None = None
    usage: dict[str, Any] | None = None


class GenerationSummary(BaseModel):
    session_id: str
    plan_revision_id: str | None
    plan_source_role: str | None = None
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
    job_id: str | None = None,
    usage: dict[str, Any] | None = None,
) -> GenerationActivitySummary:
    return GenerationActivitySummary(
        id=identifier,
        status=status,
        progress=1.0 if status in completed else float(progress or 0.0),
        progress_detail=detail,
        job_id=job_id,
        usage=usage,
    )


def _generation_run_usage(db: Session, session_id: str, generation_run_id: str) -> dict[str, Any] | None:
    """Aggregate only the selected run's compact usage fields in SQL."""
    stage = func.coalesce(func.nullif(UsageEvent.stage, ""), "other")
    commercial_flag = UsageEvent.raw_usage_json["commercial"].as_boolean()
    estimated_flag = UsageEvent.raw_usage_json["estimated"].as_boolean()
    rows = db.execute(
        select(
            stage.label("stage"),
            UsageEvent.model_id.label("model_id"),
            func.count(UsageEvent.id).label("event_count"),
            func.sum(
                case((UsageEvent.cost_usd.is_not(None), 1), else_=0)
            ).label("priced_event_count"),
            func.sum(
                case((UsageEvent.cost_usd.is_not(None), UsageEvent.cost_usd), else_=0.0)
            ).label("cost_usd"),
            func.sum(func.coalesce(UsageEvent.input_tokens, 0)).label("input_tokens"),
            func.sum(
                func.coalesce(UsageEvent.cached_input_tokens, 0)
            ).label("cached_input_tokens"),
            func.sum(func.coalesce(UsageEvent.output_tokens, 0)).label("output_tokens"),
            func.max(case((commercial_flag.is_(True), 1), else_=0)).label("commercial"),
            func.max(
                case(
                    (
                        and_(commercial_flag.is_(True), UsageEvent.cost_usd.is_(None)),
                        1,
                    ),
                    else_=0,
                )
            ).label("has_unpriced_usage"),
            func.max(case((estimated_flag.is_(True), 1), else_=0)).label("estimated"),
        )
        .where(
            UsageEvent.session_id == session_id,
            UsageEvent.generation_run_id == generation_run_id,
        )
        .group_by(stage, UsageEvent.model_id)
    ).all()
    if not rows:
        return None

    event_count = 0
    priced_event_count = 0
    total_cost_usd = 0.0
    commercial = False
    has_unpriced_usage = False
    estimated = False
    input_tokens = 0
    cached_input_tokens = 0
    output_tokens = 0
    model_ids: set[str] = set()
    stages: dict[str, dict[str, int | float]] = {}
    for row in rows:
        stage_name = str(row.stage or "other")
        stage_summary = stages.setdefault(
            stage_name,
            {
                "cost_usd": 0.0,
                "event_count": 0,
                "priced_event_count": 0,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
            },
        )
        row_event_count = int(row.event_count or 0)
        row_priced_count = int(row.priced_event_count or 0)
        row_cost = float(row.cost_usd or 0.0)
        row_input_tokens = int(row.input_tokens or 0)
        row_cached_input_tokens = int(row.cached_input_tokens or 0)
        row_output_tokens = int(row.output_tokens or 0)
        event_count += row_event_count
        priced_event_count += row_priced_count
        total_cost_usd += row_cost
        input_tokens += row_input_tokens
        cached_input_tokens += row_cached_input_tokens
        output_tokens += row_output_tokens
        commercial = commercial or bool(row.commercial)
        has_unpriced_usage = has_unpriced_usage or bool(row.has_unpriced_usage)
        estimated = estimated or bool(row.estimated)
        model_id = str(row.model_id or "")
        if model_id.strip():
            model_ids.add(model_id)
        stage_summary["cost_usd"] += row_cost
        stage_summary["event_count"] += row_event_count
        stage_summary["priced_event_count"] += row_priced_count
        stage_summary["input_tokens"] += row_input_tokens
        stage_summary["cached_input_tokens"] += row_cached_input_tokens
        stage_summary["output_tokens"] += row_output_tokens

    return {
        "total_cost_usd": total_cost_usd if priced_event_count else None,
        "event_count": event_count,
        "priced_event_count": priced_event_count,
        "has_unpriced_usage": has_unpriced_usage,
        "commercial": commercial,
        "estimated": estimated,
        "input_tokens": input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": output_tokens,
        "total_tokens": input_tokens + output_tokens,
        "model_ids": sorted(model_ids),
        "stages": [
            {"stage": stage_name, **values}
            for stage_name, values in sorted(stages.items())
        ],
    }


def get_generation_summary(database: Database, session_id: str) -> dict[str, Any]:
    """Read one snapshot without hydrating plans, histories, takes or artifacts."""
    with database.snapshot_session() as db:
        source_artifact_id = GenerationPlanRevision.settings_json[
            "_source_artifact_id"
        ].as_string()
        header = db.execute(
            select(SessionRecord.id, GenerationPlanRevision.id, Artifact.role)
            .outerjoin(GenerationPlan, GenerationPlan.session_id == SessionRecord.id)
            .outerjoin(
                GenerationPlanRevision,
                and_(
                    GenerationPlanRevision.id == GenerationPlan.active_revision_id,
                    GenerationPlanRevision.plan_id == GenerationPlan.id,
                ),
            )
            .outerjoin(
                Artifact,
                and_(
                    Artifact.id == source_artifact_id,
                    Artifact.session_id == SessionRecord.id,
                    func.json_type(
                        GenerationPlanRevision.settings_json,
                        "$._source_artifact_id",
                    ) == "text",
                ),
            )
            .where(SessionRecord.id == session_id, SessionRecord.trashed_at.is_(None))
        ).one_or_none()
        if header is None:
            raise KeyError(session_id)
        plan_revision_id = header[1]
        plan_source_role = header[2]
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
            select(
                GenerationRun.id,
                GenerationRun.status,
                Job.progress,
                Job.progress_detail,
                Job.id,
            )
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
        active_run = None
        if run is not None:
            active_run = _activity(
                *run[:4],
                job_id=run[4],
                usage=_generation_run_usage(db, session_id, run[0]),
            )
        return GenerationSummary(
            session_id=session_id,
            plan_revision_id=plan_revision_id,
            plan_source_role=plan_source_role,
            total=total,
            included_total=included_total,
            active_run=active_run,
            assembly=_activity(*assembly, completed=frozenset({"completed", "stale"}))
            if assembly is not None
            else None,
        ).model_dump()
