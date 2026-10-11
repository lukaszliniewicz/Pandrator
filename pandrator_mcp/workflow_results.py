"""Canonical execution action for immutable workflow-plan receipts."""

from __future__ import annotations

from typing import Any

from .errors import NextAction
from .results import ToolOutcome
from .schemas.common import WarningMessage


def workflow_plan_outcome(plan: dict[str, Any]) -> ToolOutcome:
    plan_id = str(plan.get("plan_id") or "")
    return ToolOutcome(
        result=plan,
        warnings=[
            WarningMessage(code="prerequisite_rerun", message=str(value))
            for value in (plan.get("warnings") or [])
            if str(value).strip()
        ],
        next_actions=[
            NextAction(
                tool="pandrator_execute_workflow_plan",
                arguments={
                    "plan_id": plan_id,
                    "plan_digest": str(plan.get("plan_digest") or ""),
                    "accepted_confirmations": [
                        str(value) for value in (plan.get("required_confirmations") or [])
                    ],
                    "idempotency_key": f"workflow-plan:{plan_id}",
                },
                reason="Execute the exact unexpired plan after reviewing its stages, disclosures and confirmations.",
            )
        ]
        if plan_id and plan.get("plan_digest")
        else [],
    )
