"""HTTP admission for reusable selected speech-take comparisons."""

from __future__ import annotations

from flask import jsonify, request
from pydantic import ValidationError

from .comparison_export_schemas import ComparisonExportRequest
from .comparison_exports import (
    admit_comparison_export_in_session,
    prepare_comparison_export,
)
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext


def _failure(context: RouteContext, retry: MutationIdempotency, error: Exception):
    if isinstance(error, KeyError):
        return context.guards.error_response(
            "not_found",
            "The session, generation run, segment, or audio artifact was not found.",
            404,
        )
    if isinstance(error, (IdempotencyConflict, IdempotencyInProgress)):
        return retry.failure(error)
    if isinstance(error, (ValueError, OSError, ValidationError)):
        return context.guards.error_response("validation_error", str(error), 422)
    raise error


def register_comparison_export_routes(
    app: DomainBlueprints,
    context: RouteContext,
) -> None:
    """Register the required-key write endpoint; no settings or take state is changed."""

    services, guards = context.services, context.guards
    retry = MutationIdempotency(context)
    operation_id = "createComparisonExport"
    base = "/api/v1/sessions/<session_id>/comparison-exports"

    @app.post(base)
    @guards.require_scope("app.run")
    def create_comparison_export(session_id: str):
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            return guards.error_response(
                "idempotency_key_required", "Idempotency-Key is required.", 400
            )
        try:
            body = ComparisonExportRequest.model_validate(
                request.get_json(silent=True) or {}
            ).model_dump(mode="json")
            accepted = {"session_id": session_id, **body}
            replay = retry.inspect(operation_id, key, accepted)
            if replay is not None:
                return replay

            prepared = prepare_comparison_export(
                services.database,
                services.paths,
                session_id,
                body,
            )
            with services.database.immediate_session() as db_session:
                reservation = services.idempotency.begin(
                    db_session,
                    principal=retry.principal(),
                    operation_id=operation_id,
                    idempotency_key=key,
                    payload=accepted,
                )
                if reservation.replayed:
                    replayed = retry.replay(reservation)
                    if replayed is not None:
                        return replayed
                result = admit_comparison_export_in_session(
                    db_session,
                    services.jobs,
                    prepared,
                )
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=result,
                    status_code=202,
                    resource_kind="job",
                    resource_id=result["job_id"],
                )
            return jsonify(result), 202
        except (KeyError, ValueError, OSError, IdempotencyConflict, IdempotencyInProgress) as error:
            return _failure(context, retry, error)


__all__ = ["register_comparison_export_routes"]
