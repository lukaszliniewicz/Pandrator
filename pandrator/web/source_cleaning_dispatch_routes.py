"""HTTP routes for passive PDF/EPUB source-cleaning dispatch."""

from __future__ import annotations

from typing import Any

from flask import g, jsonify, request

from .auth import Principal
from .dispatch import DispatchError
from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .schemas import (
    DispatchBatchClaimRequest,
    DispatchBatchReleaseRequest,
    DispatchBatchRenewRequest,
    SourceCleaningDispatchBatchSubmitRequest,
    SourceCleaningDispatchInspectionRequest,
    SourceCleaningDispatchRunCreateRequest,
)


def register_source_cleaning_dispatch_routes(
    app: DomainBlueprints,
    context: RouteContext,
) -> None:
    services = context.services
    database = services.database
    dispatch = services.source_cleaning_dispatch
    require_scope = context.guards.require_scope
    error_response = context.guards.error_response

    def dispatch_error(error: DispatchError):
        details: dict[str, Any] = {}
        if isinstance(error.details, dict):
            details.update(error.details)
        elif error.details is not None:
            details["details"] = error.details
        details["retryable"] = error.retryable
        return error_response(error.code, str(error), error.status, details)

    def error_body(error: DispatchError) -> dict[str, Any]:
        details: dict[str, Any] = {}
        if isinstance(error.details, dict):
            details.update(error.details)
        elif error.details is not None:
            details["details"] = error.details
        details["retryable"] = error.retryable
        return {
            "error": {
                "code": error.code,
                "message": str(error),
                "details": details,
                "request_id": getattr(g, "request_id", ""),
            }
        }

    def idempotency_error(error: Exception):
        if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
            return error_response(
                error.code,
                str(error),
                409,
                {"retryable": error.retryable},
            )
        return error_response("idempotency_key_required", str(error), 400)

    def request_key(*, required: bool) -> tuple[str | None, Any | None]:
        raw = str(request.headers.get("Idempotency-Key") or "").strip()
        if not raw and required:
            return None, error_response(
                "idempotency_key_required",
                "This dispatch operation requires Idempotency-Key.",
                400,
            )
        if not raw:
            principal = context.guards.principal()
            if principal is not None and principal.kind in {
                "automation_client",
                "manager_bootstrap",
            }:
                return None, error_response(
                    "idempotency_key_required",
                    "This automation write requires Idempotency-Key.",
                    400,
                )
            return None, None
        return raw, None

    def authenticated_principal() -> Principal:
        principal = context.guards.principal()
        if principal is None:
            raise RuntimeError("An authenticated principal is required.")
        return principal

    def replay_response(reservation):
        replay = reservation.response
        if replay is None:
            return None
        payload, status = replay
        response = jsonify(payload)
        response.status_code = status
        response.headers["Idempotency-Replayed"] = "true"
        return response

    @app.post("/api/v1/sessions/<session_id>/source-cleaning-dispatch-runs")
    @require_scope("app.run")
    def create_source_cleaning_dispatch_run(session_id: str):
        payload = SourceCleaningDispatchRunCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        key, key_error = request_key(required=False)
        if key_error is not None:
            return key_error
        body = payload.model_dump(mode="json")
        try:
            with database.immediate_session() as db_session:
                reservation = None
                if key is not None:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=authenticated_principal(),
                            operation_id="createSourceCleaningDispatchRun",
                            idempotency_key=key,
                            payload={"session_id": session_id, **body},
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_error(error)
                    replay = replay_response(reservation)
                    if replay is not None:
                        return replay
                result = dispatch.create_in_session(
                    db_session,
                    session_id=session_id,
                    **body,
                )
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="source_cleaning_dispatch_run",
                        resource_id=result["id"],
                    )
            return jsonify(result), 202
        except DispatchError as error:
            return dispatch_error(error)

    @app.get("/api/v1/sessions/<session_id>/source-cleaning-dispatch-runs")
    @require_scope("app.read")
    def list_source_cleaning_dispatch_runs(session_id: str):
        limit = request.args.get("limit", 50, type=int) or 50
        return jsonify({"items": dispatch.list_runs(session_id, limit=limit)})

    @app.get("/api/v1/source-cleaning-dispatch-runs/<run_id>")
    @require_scope("app.read")
    def get_source_cleaning_dispatch_run(run_id: str):
        try:
            return jsonify(dispatch.get(run_id))
        except DispatchError as error:
            return dispatch_error(error)

    @app.post("/api/v1/source-cleaning-dispatch-runs/<run_id>/claim")
    @require_scope("app.run")
    def claim_source_cleaning_dispatch_batch(run_id: str):
        payload = DispatchBatchClaimRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        key, key_error = request_key(required=True)
        if key_error is not None:
            return key_error
        assert key is not None
        try:
            with database.immediate_session() as db_session:
                try:
                    services.idempotency.validate_key(key)
                except ValueError as error:
                    return idempotency_error(error)
                result = dispatch.claim_in_session(
                    db_session,
                    run_id=run_id,
                    claim_key=key,
                    **payload.model_dump(),
                )
            return jsonify(result)
        except DispatchError as error:
            return dispatch_error(error)

    @app.post("/api/v1/source-cleaning-dispatch-batches/<batch_id>/renew")
    @require_scope("app.run")
    def renew_source_cleaning_dispatch_batch(batch_id: str):
        payload = DispatchBatchRenewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        key, key_error = request_key(required=False)
        if key_error is not None:
            return key_error
        body = payload.model_dump(mode="json")
        try:
            with database.immediate_session() as db_session:
                reservation = None
                if key is not None:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=authenticated_principal(),
                            operation_id="renewSourceCleaningDispatchBatch",
                            idempotency_key=key,
                            payload={"batch_id": batch_id, **body},
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_error(error)
                    replay = replay_response(reservation)
                    if replay is not None:
                        return replay
                result = dispatch.renew_in_session(
                    db_session,
                    batch_id=batch_id,
                    **body,
                )
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="source_cleaning_dispatch_batch",
                        resource_id=batch_id,
                    )
            return jsonify(result)
        except DispatchError as error:
            return dispatch_error(error)

    @app.post("/api/v1/source-cleaning-dispatch-batches/<batch_id>/release")
    @require_scope("app.run")
    def release_source_cleaning_dispatch_batch(batch_id: str):
        payload = DispatchBatchReleaseRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        key, key_error = request_key(required=False)
        if key_error is not None:
            return key_error
        body = payload.model_dump(mode="json")
        try:
            with database.immediate_session() as db_session:
                reservation = None
                if key is not None:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=authenticated_principal(),
                            operation_id="releaseSourceCleaningDispatchBatch",
                            idempotency_key=key,
                            payload={"batch_id": batch_id, **body},
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_error(error)
                    replay = replay_response(reservation)
                    if replay is not None:
                        return replay
                result = dispatch.release_in_session(
                    db_session,
                    batch_id=batch_id,
                    **body,
                )
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="source_cleaning_dispatch_batch",
                        resource_id=batch_id,
                    )
            return jsonify(result)
        except DispatchError as error:
            return dispatch_error(error)

    @app.post("/api/v1/source-cleaning-dispatch-batches/<batch_id>/submit")
    @require_scope("app.run")
    def submit_source_cleaning_dispatch_batch(batch_id: str):
        payload = SourceCleaningDispatchBatchSubmitRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        key, key_error = request_key(required=True)
        if key_error is not None:
            return key_error
        assert key is not None
        body = payload.model_dump(mode="json")
        try:
            with database.immediate_session() as db_session:
                try:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=authenticated_principal(),
                        operation_id="submitSourceCleaningDispatchBatch",
                        idempotency_key=key,
                        payload={"batch_id": batch_id, **body},
                    )
                except (
                    IdempotencyConflict,
                    IdempotencyInProgress,
                    ValueError,
                ) as error:
                    return idempotency_error(error)
                replay = reservation.response
                if replay is not None:
                    replay_payload, replay_status = replay
                    if isinstance(replay_payload, dict) and isinstance(
                        replay_payload.get("run_id"), str
                    ):
                        try:
                            result, retry_status = (
                                dispatch.retry_finalization_in_session(
                                    db_session,
                                    run_id=replay_payload["run_id"],
                                )
                            )
                            if (
                                retry_status != replay_status
                                or result != replay_payload
                            ):
                                services.idempotency.complete(
                                    db_session,
                                    reservation,
                                    response=result,
                                    status_code=retry_status,
                                    resource_kind="source_cleaning_dispatch_batch",
                                    resource_id=batch_id,
                                )
                                replay_payload, replay_status = result, retry_status
                        except DispatchError as error:
                            services.idempotency.complete(
                                db_session,
                                reservation,
                                response=error_body(error),
                                status_code=error.status,
                                resource_kind="source_cleaning_dispatch_batch",
                                resource_id=batch_id,
                            )
                            return dispatch_error(error)
                    response = jsonify(replay_payload)
                    response.status_code = replay_status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                try:
                    result, status_code = dispatch.submit_in_session(
                        db_session,
                        batch_id=batch_id,
                        submission_key=key,
                        **body,
                    )
                except DispatchError as error:
                    if isinstance(error.details, dict) and error.details.get(
                        "batch_accepted"
                    ):
                        services.idempotency.complete(
                            db_session,
                            reservation,
                            response=error_body(error),
                            status_code=error.status,
                            resource_kind="source_cleaning_dispatch_batch",
                            resource_id=batch_id,
                        )
                    else:
                        db_session.delete(reservation.record)
                        db_session.flush()
                    return dispatch_error(error)
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=result,
                    status_code=status_code,
                    resource_kind="source_cleaning_dispatch_batch",
                    resource_id=batch_id,
                )
            return jsonify(result), status_code
        except DispatchError as error:
            return dispatch_error(error)

    @app.post("/api/v1/source-cleaning-dispatch-batches/<batch_id>/inspect")
    @require_scope("app.run")
    def inspect_source_cleaning_dispatch_extraction(batch_id: str):
        payload = SourceCleaningDispatchInspectionRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        key, key_error = request_key(required=True)
        if key_error is not None:
            return key_error
        assert key is not None
        body = payload.model_dump(mode="json")
        try:
            with database.immediate_session() as db_session:
                try:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=authenticated_principal(),
                        operation_id="inspectSourceCleaningDispatchExtraction",
                        idempotency_key=key,
                        payload={"batch_id": batch_id, **body},
                    )
                except (
                    IdempotencyConflict,
                    IdempotencyInProgress,
                    ValueError,
                ) as error:
                    return idempotency_error(error)
                replay = replay_response(reservation)
                if replay is not None:
                    return replay
                result = dispatch.inspect_in_session(
                    db_session,
                    batch_id=batch_id,
                    **body,
                )
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=result,
                    status_code=200,
                    resource_kind="source_cleaning_dispatch_batch",
                    resource_id=batch_id,
                )
            return jsonify(result)
        except DispatchError as error:
            return dispatch_error(error)
