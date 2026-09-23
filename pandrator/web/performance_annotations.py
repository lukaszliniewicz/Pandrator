"""Read stored performance annotations without importing planning services."""

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from . import models as m


def performance_batches(session: Session, plan_id: str) -> list[m.PerformanceBatch]:
    return list(
        session.scalars(
            select(m.PerformanceBatch)
            .where(m.PerformanceBatch.performance_plan_id == plan_id)
            .order_by(m.PerformanceBatch.ordinal)
        )
    )


def plan_annotations(session: Session, plan: m.PerformancePlan) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for batch in performance_batches(session, plan.id):
        if batch.status == "completed":
            result.update(deepcopy(batch.annotations_json or {}))
    # Manual edits always take precedence, including edits made while a batch
    # was leased. No automatic submission can overwrite a locked user choice.
    result.update(deepcopy(plan.manual_annotations_json or {}))
    return result
