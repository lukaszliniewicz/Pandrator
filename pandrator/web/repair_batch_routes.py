"""Authenticated grouped history and atomic, idempotent repair-batch undo."""

from flask import jsonify, request

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .repair_batch_schemas import RepairBatchUndoRequest
from .repair_batches import (
    grouped_revision_history,
    repair_batch_detail,
    undo_repair_batch_in_session,
)
from .settings_policy import RevisionConflict


def register_repair_batch_routes(app, context) -> None:
    services = context.services
    guards = context.guards
    base = "/api/v1/sessions/<session_id>/generation-plan"

    def paging():
        from .generation_review import parse_summary_flag

        limit = int(request.args.get("limit", "50"))
        before = request.args.get("before_revision_number")
        before = int(before) if before is not None else None
        if not 1 <= limit <= 100 or (before is not None and before < 1):
            raise ValueError("Limit must be 1–100 and the revision cursor must be positive.")
        summary = parse_summary_flag(request.args.get("summary"))
        return {"limit": limit, "before_revision_number": before, "summary_flags": summary}

    @app.get(f"{base}/history", endpoint="generation.grouped_plan_history")
    @guards.require_scope("app.read")
    def grouped_history(session_id):
        try:
            paging_args = paging()
            summary = paging_args.pop("summary_flags")
            return jsonify(grouped_revision_history(
                services.database, session_id, **paging_args,
                include_audio_reuse=not summary, include_undo_eligibility=not summary,
            ))
        except KeyError:
            return guards.error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return guards.error_response("validation_error", str(error), 422)

    @app.get(f"{base}/repair-batches/<batch_id>", endpoint="generation.repair_batch_detail")
    @guards.require_scope("app.read")
    def batch_detail(session_id, batch_id):
        try:
            paging_args = paging()
            paging_args.pop("summary_flags", None)  # Detail is always authoritative full.
            return jsonify(repair_batch_detail(services.database, session_id, batch_id, **paging_args))
        except KeyError:
            return guards.error_response("not_found", "Session or repair batch not found.", 404)
        except ValueError as error:
            return guards.error_response("validation_error", str(error), 422)

    @app.post(f"{base}/repair-batches/<batch_id>/undo", endpoint="generation.undo_repair_batch")
    @guards.require_scope("app.write")
    def undo_batch(session_id, batch_id):
        try:
            payload = RepairBatchUndoRequest.model_validate(request.get_json(silent=True) or {})
        except ValueError as error:
            return guards.error_response("validation_error", str(error), 422)
        key = request.headers.get("Idempotency-Key", "")
        try:
            services.idempotency.validate_key(key)
        except ValueError as error:
            return guards.error_response("idempotency_key_required", str(error), 400)
        try:
            with services.database.immediate_session() as session:
                reservation = services.idempotency.begin(
                    session, principal=guards.principal(), operation_id="undoSpeechPlanRepairBatch",
                    idempotency_key=key,
                    payload={"session_id": session_id, "batch_id": batch_id, **payload.model_dump()},
                )
                if reservation.response is not None:
                    body, status = reservation.response
                    response = jsonify(body)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = undo_repair_batch_in_session(
                    services.generation, session, session_id, batch_id, **payload.model_dump(),
                )
                services.idempotency.complete(
                    session, reservation, response=result, status_code=201,
                    resource_kind="generation_plan_revision", resource_id=result["plan_revision_id"],
                )
            return jsonify(result), 201
        except KeyError:
            return guards.error_response("not_found", "Session or repair batch not found.", 404)
        except RevisionConflict as error:
            return guards.error_response("revision_conflict", str(error), 409)
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return guards.error_response(error.code, str(error), 409, {"retryable": error.retryable})
        except ValueError as error:
            return guards.error_response("validation_error", str(error), 422)
