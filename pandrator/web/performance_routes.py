"""Authenticated, optimistic and idempotent performance-planning API."""

from __future__ import annotations

from flask import jsonify, request
from sqlalchemy import select

from . import models as m
from . import performance_plans as plans
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .performance_schemas import (
    PerformanceAdoptRequest,
    PerformanceEditRequest,
    PerformanceLeaseRequest,
    PerformancePlanCreateRequest,
    PerformancePreviewRequest,
    PerformanceRenewRequest,
    PerformanceSubmitRequest,
)
from .workspace import RevisionConflict, adapt_runtime_settings


def register_performance_routes(app, context) -> None:
    services, guards = context.services, context.guards
    base = "/api/v1/sessions/<session_id>/performance-plans"

    def failure(error):
        if isinstance(error, KeyError):
            return guards.error_response(
                "not_found", "Session, performance plan or batch not found.", 404
            )
        if isinstance(error, RevisionConflict):
            return guards.error_response("revision_conflict", str(error), 409)
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return guards.error_response(
                error.code, str(error), 409, {"retryable": error.retryable}
            )
        return guards.error_response("validation_error", str(error), 422)

    def paging():
        offset, limit = (
            int(request.args.get("offset", 0)),
            int(request.args.get("limit", 50)),
        )
        if offset < 0 or not 1 <= limit <= 100:
            raise ValueError("Offset must be nonnegative and limit must be 1–100.")
        return offset, limit

    def settings(session, session_id):
        if session.get(m.SessionRecord, session_id) is None:
            raise KeyError(session_id)
        effective = services.workspace_settings.get_in_session(
            session, session_id, "tts"
        )["effective"]
        return adapt_runtime_settings("tts", dict(effective))

    def write(session_id, operation, schema, callback, *, plan_id=None, batch_id=None):
        try:
            body = (
                schema.model_validate(request.get_json(silent=True) or {}).model_dump(
                    mode="json", by_alias=True
                )
                if schema
                else {}
            )
            key = request.headers.get("Idempotency-Key", "")
            try:
                services.idempotency.validate_key(key)
            except ValueError as error:
                return guards.error_response(
                    "idempotency_key_required", str(error), 400
                )
            with services.database.immediate_session() as session:
                if session.get(m.SessionRecord, session_id) is None:
                    raise KeyError(session_id)
                reservation = services.idempotency.begin(
                    session,
                    principal=guards.principal(),
                    operation_id=operation,
                    idempotency_key=key,
                    payload={
                        "session_id": session_id,
                        "plan_id": plan_id,
                        "batch_id": batch_id,
                        **body,
                    },
                )
                if reservation.response is not None:
                    payload, status = reservation.response
                    response = jsonify(payload)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                plan = plans.get_plan(session, session_id, plan_id) if plan_id else None
                result, status = callback(session, plan, body)
                services.idempotency.complete(
                    session,
                    reservation,
                    response=result,
                    status_code=status,
                    resource_kind="performance_plan",
                    resource_id=result.get("id") or plan_id,
                )
            return jsonify(result), status
        except (
            ValueError,
            KeyError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)

    def enqueue(session, plan):
        plans._assert_current(session, plan, editable=True)
        existing = session.get(m.Job, plan.job_id) if plan.job_id else None
        if existing and existing.status in {
            "queued",
            "running",
            "retrying",
            "cancelling",
        }:
            raise RevisionConflict(
                "Performance analysis is already queued or running for this draft."
            )
        job = services.jobs.enqueue_in_session(
            session,
            "speech.performance",
            {"session_id": plan.session_id, "performance_plan_id": plan.id},
            session_id=plan.session_id,
            resource_keys=[f"session:{plan.session_id}:performance"],
            max_attempts=1,
        )
        plan.job_id = job.id
        return job.id

    @app.get(base, endpoint="list_performance_plans")
    @guards.require_scope("app.read")
    def list_plans(session_id):
        try:
            offset, limit = paging()
            with services.database.session() as session:
                runtime = settings(session, session_id)
                statement = select(m.PerformancePlan).where(
                    m.PerformancePlan.session_id == session_id
                )
                revision = request.args.get("plan_revision_id")
                if revision:
                    if len(revision) > 80:
                        raise ValueError("Invalid plan revision identifier.")
                    statement = statement.where(
                        m.PerformancePlan.plan_revision_id == revision
                    )
                rows = list(
                    session.scalars(
                        statement.order_by(
                            m.PerformancePlan.created_at.desc(), m.PerformancePlan.id
                        )
                        .offset(offset)
                        .limit(limit)
                    )
                )
                from pandrator.logic.speech_performance import resolve_capabilities

                return jsonify(
                    {
                        "items": [
                            plans.describe_plan(session, plan, include_units=False)
                            for plan in rows
                        ],
                        "offset": offset,
                        "limit": limit,
                        "capabilities": resolve_capabilities(runtime),
                        "performance_enabled": bool(runtime.get("performance_enabled")),
                        "tts_context_mode": runtime.get("tts_context_mode", "off"),
                    }
                )
        except (ValueError, KeyError, RevisionConflict) as error:
            return failure(error)

    @app.post(base, endpoint="create_performance_plan")
    @guards.require_scope("app.run")
    def create(session_id):
        def operation(session, _plan, body):
            payload = PerformancePlanCreateRequest.model_validate(body)
            plan = plans.create_plan(
                session, session_id, payload, settings(session, session_id)
            )
            if payload.mode == "llm":
                enqueue(session, plan)
            return plans.describe_plan(session, plan), 201

        return write(
            session_id, "createPerformancePlan", PerformancePlanCreateRequest, operation
        )

    @app.get(base + "/<plan_id>", endpoint="get_performance_plan")
    @guards.require_scope("app.read")
    def get(session_id, plan_id):
        try:
            offset, limit = paging()
            filter_name = request.args.get("filter", "all")
            with services.database.session() as session:
                return jsonify(
                    plans.describe_plan(
                        session,
                        plans.get_plan(session, session_id, plan_id),
                        offset=offset,
                        limit=limit,
                        filter=filter_name,
                    )
                )
        except (ValueError, KeyError, RevisionConflict) as error:
            return failure(error)

    @app.patch(base + "/<plan_id>", endpoint="edit_performance_plan")
    @guards.require_scope("app.write")
    def edit(session_id, plan_id):
        return write(
            session_id,
            "editPerformancePlan",
            PerformanceEditRequest,
            lambda session, plan, body: (
                plans.edit_annotations(session, plan, **body),
                200,
            ),
            plan_id=plan_id,
        )

    @app.post(base + "/<plan_id>/adopt", endpoint="adopt_performance_plan")
    @guards.require_scope("app.write")
    def adopt(session_id, plan_id):
        def operation(session, plan, body):
            enable = body.pop("enable")
            result = plans.adopt_plan(session, plan, **body)
            if enable:
                current = services.workspace_settings.get_in_session(
                    session, session_id, "tts"
                )
                services.workspace_settings.update_in_session(
                    session,
                    session_id,
                    "tts",
                    current["revision"],
                    {**current["override"], "performance_enabled": True},
                )
            result["enabled"] = enable
            return result, 200

        return write(
            session_id,
            "adoptPerformancePlan",
            PerformanceAdoptRequest,
            operation,
            plan_id=plan_id,
        )

    @app.post(base + "/<plan_id>/analyse", endpoint="analyse_performance_plan")
    @guards.require_scope("app.run")
    def analyse(session_id, plan_id):
        return write(
            session_id,
            "analysePerformancePlan",
            None,
            lambda session, plan, body: (
                {"id": plan.id, "job_id": enqueue(session, plan)},
                202,
            ),
            plan_id=plan_id,
        )

    @app.post(base + "/<plan_id>/preview", endpoint="preview_performance_plan")
    @guards.require_scope("app.read")
    def preview(session_id, plan_id):
        try:
            payload = PerformancePreviewRequest.model_validate(
                request.get_json(silent=True) or {}
            )
            with services.database.session() as session:
                plan = plans.get_plan(session, session_id, plan_id)
                runtime = settings(session, session_id)
                if payload.service is not None:
                    from pandrator.logic.tts_provider_switch import (
                        prepare_tts_provider_switch,
                    )

                    switched = adapt_runtime_settings(
                        "tts",
                        prepare_tts_provider_switch(
                            runtime, {"service": payload.service}
                        ),
                    )
                    runtime = {**runtime, **switched}
                if payload.model is not None:
                    runtime.update(model=payload.model, xtts_model=payload.model)
                for name, value in (
                    ("generation_prompt", payload.generation_prompt),
                    ("tts_context_mode", payload.context_mode),
                    ("performance_context_before", payload.context_before),
                    ("performance_context_after", payload.context_after),
                    ("performance_context_max_chars", payload.context_max_chars),
                    ("casting_enabled", payload.casting_enabled),
                    ("_preview_performance_enabled", payload.performance_enabled),
                    ("performance_allow_vocalizations", payload.allow_vocalizations),
                ):
                    if value is not None:
                        runtime[name] = value
                unit = plans._unit_map(plan).get(payload.segment_id)
                if unit is None:
                    raise KeyError(payload.segment_id)
                if unit.get("language"):
                    runtime.update(language=unit["language"])
                result = plans.preview_segment(
                        session,
                        plan,
                        payload.segment_id,
                        runtime,
                        annotation=payload.annotation.model_dump(
                            mode="json", by_alias=True
                        )
                        if payload.annotation is not None
                        else None,
                        speech_xml=payload.speech_xml,
                    )
                result["generation_source"] = {
                    "plan_revision_id": plan.plan_revision_id,
                    "source_artifact_id": (session.get(m.GenerationPlanRevision, plan.plan_revision_id).settings_json or {}).get("_source_artifact_id"),
                }
                from .speech_boundaries import boundary_pause
                row = session.get(m.GenerationSegment, payload.segment_id)
                boundary = (result.get("speech_structure") or {}).get("boundary_after")
                audio = services.workspace_settings.get_in_session(session, session_id, "audio")["effective"]
                result["assembly_boundary"] = {
                    "boundary_after": boundary,
                    "stored_silence_after_ms": row.silence_after_ms,
                    "effective_silence_after_ms": boundary_pause(boundary, row.silence_after_ms, audio),
                }
                model = result["capabilities"].get("model")
                backend = result["capabilities"].get("backend")
            catalogue, _ = services.tts_catalogue.snapshot(refresh=True)
            service = next((item for item in catalogue.get("services", []) if item.get("id") == backend), None)
            ready = bool(service and service.get("available") and model in (service.get("models") or []))
            result["readiness"] = {
                "model_available": ready,
                "status": "ready" if ready else "unavailable_or_unverified",
                "compilation_only": True,
                "acoustic_compliance": "not_tested",
            }
            return jsonify(result)
        except (ValueError, KeyError, RevisionConflict) as error:
            return failure(error)

    @app.post(base + "/<plan_id>/claim", endpoint="claim_performance_batch")
    @guards.require_scope("app.run")
    def claim(session_id, plan_id):
        return write(
            session_id,
            "claimPerformanceBatch",
            PerformanceLeaseRequest,
            lambda session, plan, body: (plans.claim_batch(session, plan, **body), 200),
            plan_id=plan_id,
        )

    @app.post(
        base + "/<plan_id>/batches/<batch_id>/submit",
        endpoint="submit_performance_batch",
    )
    @guards.require_scope("app.run")
    def submit(session_id, plan_id, batch_id):
        return write(
            session_id,
            "submitPerformanceBatch",
            PerformanceSubmitRequest,
            lambda session, plan, body: (
                plans.submit_batch(
                    session, plan, batch_id, body["lease_token"], body["items"]
                ),
                200,
            ),
            plan_id=plan_id,
            batch_id=batch_id,
        )

    @app.post(
        base + "/<plan_id>/batches/<batch_id>/renew", endpoint="renew_performance_batch"
    )
    @guards.require_scope("app.run")
    def renew(session_id, plan_id, batch_id):
        return write(
            session_id,
            "renewPerformanceBatch",
            PerformanceRenewRequest,
            lambda session, plan, body: (
                plans.renew_batch(
                    session,
                    plan,
                    batch_id,
                    body["lease_token"],
                    lease_seconds=body["lease_seconds"],
                ),
                200,
            ),
            plan_id=plan_id,
            batch_id=batch_id,
        )

    @app.post(
        base + "/<plan_id>/batches/<batch_id>/release",
        endpoint="release_performance_batch",
    )
    @guards.require_scope("app.run")
    def release(session_id, plan_id, batch_id):
        return write(
            session_id,
            "releasePerformanceBatch",
            PerformanceRenewRequest,
            lambda session, plan, body: (
                plans.renew_batch(
                    session, plan, batch_id, body["lease_token"], release=True
                ),
                200,
            ),
            plan_id=plan_id,
            batch_id=batch_id,
        )
