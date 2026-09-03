"""Review-first workflow planning and exact execution."""

from __future__ import annotations

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ExecuteWorkflowPlanInput,
    PlanWorkflowInput,
)
from ..schemas.common import WarningMessage
from ..work_mapping import application_work_reference


def plan_workflow(
    runtime: McpRuntime,
    arguments: PlanWorkflowInput,
) -> ToolOutcome:
    plan = runtime.require_application().create_workflow_plan(
        arguments.session_id,
        target_stage=arguments.target_stage,
        overrides=arguments.overrides,
        expires_in_minutes=arguments.expires_in_minutes,
    )
    plan_id = str(plan.get("plan_id") or "")
    digest = str(plan.get("plan_digest") or "")
    confirmations = tuple(
        str(value)
        for value in plan.get("required_confirmations", []) or []
    )
    warnings = [
        WarningMessage(code="prerequisite_rerun", message=str(value))
        for value in (plan.get("warnings") or [])
        if str(value).strip()
    ]
    return ToolOutcome(
        result=plan,
        warnings=warnings,
        next_actions=[
            NextAction(
                tool="pandrator_execute_workflow_plan",
                arguments={
                    "plan_id": plan_id,
                    "plan_digest": digest,
                    "accepted_confirmations": list(confirmations),
                    "idempotency_key": f"workflow-plan:{plan_id}",
                },
                reason=(
                    "Execute only after the user reviews the exact stages, "
                    "provider disclosures, data categories, and confirmations."
                ),
            )
        ],
    )


def execute_workflow_plan(
    runtime: McpRuntime,
    arguments: ExecuteWorkflowPlanInput,
) -> ToolOutcome:
    result = runtime.require_application().execute_workflow_plan(
        arguments.plan_id,
        plan_digest=arguments.plan_digest,
        accepted_confirmations=arguments.accepted_confirmations,
        idempotency_key=arguments.idempotency_key,
    )
    work = application_work_reference(result)
    return ToolOutcome(
        result=result,
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={
                    "work_type": "job",
                    "work_id": work.id,
                    "include_events": True,
                },
                reason="Observe the durable job by its returned work handle.",
            )
        ],
    )
