"""Durable purge admission and result routes, separate from legacy sync routes."""

from __future__ import annotations

from flask import jsonify, request
from pydantic import ValidationError

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Job, SessionRecord
from .route_context import RouteContext
from .session_purge_job_schemas import PurgeAdmissionRequest, PurgeInspectionRequest
from .session_purge_jobs import INSPECTION_KIND, PURGE_KIND


def register_session_purge_job_routes(app, context: RouteContext) -> None:
    services, guards = context.services, context.guards

    def admit(session_id: str, *, kind: str, schema, operation: str):
        raw = request.get_json(silent=True)
        try:
            body = schema.model_validate(raw if raw is not None else {}).model_dump(mode="json")
            key = services.idempotency.validate_key(request.headers.get("Idempotency-Key"))
        except (ValidationError, ValueError) as error:
            return guards.error_response("invalid_request", str(error), 400)
        principal = guards.principal()
        if principal is None:
            return guards.error_response("authentication_required", "Authentication is required.", 401)
        try:
            with services.database.immediate_session() as session:
                reservation = services.idempotency.begin(
                    session,
                    principal=principal,
                    operation_id=operation,
                    idempotency_key=key,
                    payload={"target_session_id": session_id, **body},
                )
                if reservation.response is not None:
                    result, status = reservation.response
                    response = jsonify(result)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                if kind == PURGE_KIND:
                    if record.trashed_at is None or record.status != "trashed":
                        raise ValueError("Only a trashed session can be permanently deleted.")
                    if record.revision != body["expected_revision"]:
                        raise IdempotencyConflict("Session revision changed; refresh the purge inspection.")
                payload = {"target_session_id": session_id}
                if kind == PURGE_KIND:
                    payload.update({
                        "expected_revision": body["expected_revision"],
                        "impact_fingerprint": body["impact_token"],
                    })
                job = services.jobs.enqueue_in_session(
                    session, kind, payload,
                    session_id=None, workflow_run_id=None,
                    resource_keys=[f"maintenance:session-purge:{session_id}"],
                    max_attempts=3,
                )
                result = {"job_id": job.id, "state": "queued", "target_session_id": session_id}
                services.idempotency.complete(
                    session, reservation, response=result, status_code=202,
                    resource_kind="job", resource_id=job.id,
                )
            return jsonify(result), 202
        except KeyError:
            return guards.error_response("not_found", "Session not found.", 404)
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return guards.error_response(error.code, str(error), 409, {"retryable": error.retryable})
        except ValueError as error:
            return guards.error_response("invalid_request", str(error), 409)

    @app.post("/api/v1/sessions/<session_id>/purge-inspections")
    @guards.require_scope("app.read")
    def admit_purge_inspection(session_id: str):
        return admit(session_id, kind=INSPECTION_KIND,
                     schema=PurgeInspectionRequest, operation="admitSessionPurgeInspection")

    @app.post("/api/v1/sessions/<session_id>/purge-admissions")
    @guards.require_scope("app.write")
    def admit_session_purge(session_id: str):
        return admit(session_id, kind=PURGE_KIND,
                     schema=PurgeAdmissionRequest, operation="admitSessionPurge")

    @app.get("/api/v1/purge-inspections/<job_id>")
    @guards.require_scope("app.read")
    def get_purge_inspection(job_id: str):
        with services.database.session() as session:
            job = session.get(Job, job_id)
            if job is None or job.kind != INSPECTION_KIND:
                return guards.error_response("not_found", "Purge inspection not found.", 404)
            if job.status == "succeeded" and isinstance(job.result_json, dict):
                result = dict(job.result_json)
                result["impact_token"] = result.pop("impact_fingerprint", None)
                return jsonify({"job_id": job.id, "state": "succeeded", "inspection": result})
            if job.status in {"failed", "canceled", "cancelled"}:
                return jsonify({"job_id": job.id, "state": job.status,
                                "error": {"code": job.error_code or "inspection_unavailable"}})
            return jsonify({"job_id": job.id, "state": job.status}), 202
