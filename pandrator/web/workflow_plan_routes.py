"""HTTP routes for immutable workflow preview and execution."""

from __future__ import annotations

from flask import g, jsonify, request

from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .schemas import (
    WorkflowPlanCreateRequest,
    WorkflowPlanExecuteRequest,
)
from .workflow_plans import WorkflowPlanError

MAXIMUM_PLAN_REQUEST_BYTES = 512 * 1024


def register_workflow_plan_routes(
    app: DomainBlueprints,
    context: RouteContext,
) -> None:
    services = context.services
    plans = services.workflow_plans
    error_response = context.guards.error_response

    def plan_error(error: WorkflowPlanError):
        return error_response(
            error.code,
            str(error),
            error.status_code,
            {
                **(error.details or {}),
                "retryable": error.retryable,
            },
        )

    @app.post("/api/v1/sessions/<session_id>/workflow-plans")
    @context.guards.require_scope("app.read")
    def workflow_plan_create(session_id: str):
        if (
            request.content_length is not None
            and request.content_length > MAXIMUM_PLAN_REQUEST_BYTES
        ):
            return error_response(
                "request_too_large",
                "The workflow-plan request exceeds the size limit.",
                413,
            )
        payload = WorkflowPlanCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        inline_error = context.guards.inline_credential_error(
            payload.overrides
        )
        if inline_error is not None:
            return inline_error
        principal = context.guards.principal()
        assert principal is not None
        identity = services.identity.snapshot(
            observed_origin=request.url_root
        )
        try:
            plan = plans.create(
                principal=principal,
                target_identity=identity.model_dump(mode="json"),
                session_id=session_id,
                target_stage=payload.target_stage,
                overrides=payload.overrides,
                expires_in_minutes=payload.expires_in_minutes,
            )
        except KeyError:
            return error_response(
                "not_found",
                "Session not found.",
                404,
            )
        except WorkflowPlanError as error:
            return plan_error(error)
        except ValueError as error:
            return error_response(
                "validation_error",
                str(error),
                422,
            )
        g.audit_plan_id = plan["plan_id"]
        g.audit_plan_digest = plan["plan_digest"]
        g.audit_resource_kind = "workflow_plan"
        g.audit_resource_id = plan["plan_id"]
        return jsonify(plan), 201

    @app.get("/api/v1/workflow-plans/<plan_id>")
    @context.guards.require_scope("app.read")
    def workflow_plan_get(plan_id: str):
        principal = context.guards.principal()
        assert principal is not None
        try:
            plan = plans.get(plan_id, principal=principal)
        except WorkflowPlanError as error:
            return plan_error(error)
        g.audit_plan_id = plan_id
        g.audit_plan_digest = plan["plan_digest"]
        g.audit_resource_kind = "workflow_plan"
        g.audit_resource_id = plan_id
        return jsonify(plan)

    @app.post("/api/v1/workflow-plans/<plan_id>/execute")
    @context.guards.require_scope("app.run")
    def workflow_plan_execute(plan_id: str):
        if (
            request.content_length is not None
            and request.content_length > MAXIMUM_PLAN_REQUEST_BYTES
        ):
            return error_response(
                "request_too_large",
                "The workflow-plan execution request exceeds the size limit.",
                413,
            )
        payload = WorkflowPlanExecuteRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        principal = context.guards.principal()
        assert principal is not None
        identity = services.identity.snapshot(
            observed_origin=request.url_root
        )
        try:
            result, status_code, replayed = plans.execute(
                principal=principal,
                target_identity=identity.model_dump(mode="json"),
                plan_id=plan_id,
                supplied_digest=payload.plan_digest,
                accepted_confirmations=payload.accepted_confirmations,
                idempotency_key=request.headers.get("Idempotency-Key"),
            )
        except ValueError as error:
            return error_response(
                "idempotency_key_required",
                str(error),
                400,
            )
        except IdempotencyConflict as error:
            return error_response(error.code, str(error), 409)
        except IdempotencyInProgress as error:
            return error_response(
                error.code,
                str(error),
                409,
                {"retryable": True},
            )
        except WorkflowPlanError as error:
            return plan_error(error)
        response = jsonify(result)
        response.status_code = status_code
        if replayed:
            response.headers["Idempotency-Replayed"] = "true"
        g.audit_plan_id = plan_id
        g.audit_plan_digest = payload.plan_digest
        g.audit_resource_kind = "work"
        g.audit_resource_id = result.get("id")
        return response
