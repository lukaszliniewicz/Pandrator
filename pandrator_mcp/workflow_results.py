"""Canonical execution action for immutable workflow-plan receipts."""

from __future__ import annotations

from typing import Any

from .errors import NextAction
from .results import ToolOutcome
from .schemas.common import WarningMessage


def _plan_summary(plan: dict[str, Any]) -> dict[str, Any]:
    """Drop the bulky resolved settings snapshot; keep the plan digest intact.

    Execution is decided by the stored plan and the digest, so the receipt may be
    smaller without changing what ``pandrator_execute_workflow_plan`` accepts.
    """

    result = dict(plan)
    settings = plan.get("settings")
    omitted: list[str] = []
    if isinstance(settings, dict):
        kept = {key: value for key, value in settings.items() if key != "resolved_snapshot"}
        if "resolved_snapshot" in settings:
            omitted.append("settings.resolved_snapshot")
        result["settings"] = kept
    result["view"] = "summary"
    result["omitted"] = omitted
    return result


def workflow_plan_outcome(plan: dict[str, Any], *, view: str = "summary") -> ToolOutcome:
    result = plan if view == "full" else _plan_summary(plan)
    plan_id = str(plan.get("plan_id") or "")
    return ToolOutcome(
        result=result,
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
