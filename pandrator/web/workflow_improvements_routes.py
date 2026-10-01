"""Bounded inspection endpoints shared by subtitle review and MCP."""

from __future__ import annotations

from flask import jsonify, request

from .dispatch_preview import get_dispatch_preview
from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .schemas import ReviewSplitInspection, WorkflowInputSelection
from .settings_policy import RevisionConflict
from .subtitle_evidence import evidence_route_catalog
from .workflow_inputs import get_workflow_inputs, select_workflow_input


def register_workflow_improvements_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    require_scope = context.guards.require_scope
    error_response = context.guards.error_response

    @app.get("/api/v1/dispatch-runs/<run_id>/preview")
    @require_scope("app.read")
    def dispatch_preview(run_id: str):
        try:
            ordinal = request.args.get("batch_ordinal")
            result = get_dispatch_preview(services, run_id,
                batch_ordinal=int(ordinal) if ordinal is not None else None,
                offset=int(request.args.get("offset", "0")),
                limit=int(request.args.get("limit", "20")))
        except KeyError:
            return error_response("not_found", "Dispatch run not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.get("/api/v1/sessions/<session_id>/workflow-inputs")
    @require_scope("app.read")
    def workflow_inputs_get(session_id: str):
        try:
            return jsonify(get_workflow_inputs(services, session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.put("/api/v1/sessions/<session_id>/workflow-inputs")
    @require_scope("app.write")
    def workflow_input_select(session_id: str):
        payload = WorkflowInputSelection.model_validate(request.get_json(silent=True) or {})
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            return error_response("idempotency_key_required", "Input selection requires Idempotency-Key.", 400)
        principal = context.guards.principal()
        if principal is None:
            return error_response("authentication_required", "Sign in to select a workflow input.", 401)
        values = {"session_id": session_id, **payload.model_dump(mode="json")}
        try:
            with services.database.immediate_session() as db_session:
                reservation = services.idempotency.begin(db_session,
                    principal=principal, operation_id="selectWorkflowInput",
                    idempotency_key=key, payload=values)
                if reservation.response is not None:
                    result, status = reservation.response
                    response = jsonify(result)
                    response.status_code = status
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = select_workflow_input(services, **values, db_session=db_session)
                services.idempotency.complete(db_session, reservation, response=result,
                    status_code=200, resource_kind="workflow_input", resource_id=session_id)
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return error_response(error.code, str(error), 409, {"retryable": error.retryable})
        except KeyError:
            return error_response("not_found", "Session or input artifact not found.", 404)
        except (RevisionConflict, RuntimeError) as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.get("/api/v1/subtitle-evidence/routes")
    @require_scope("app.read")
    def subtitle_evidence_routes():
        language = str(request.args.get("language") or "").strip() or None
        if language and len(language) > 40:
            return error_response("validation_error", "Language must be at most 40 characters.", 422)
        routes = evidence_route_catalog(None, language, database=services.database, paths=services.paths)
        if request.args.get("include_languages") != "true":
            routes = [{key: value for key, value in route.items() if key != "supported_languages"}
                      for route in routes]
        return jsonify({"schema_version": "1", "language": language, "routes": routes})

    @app.post("/api/v1/sessions/<session_id>/subtitles/split-boundaries")
    @require_scope("app.read")
    def subtitle_review_split_boundaries(session_id: str):
        payload = ReviewSplitInspection.model_validate(request.get_json(silent=True) or {})
        try:
            result = services.subtitle_review.inspect_review_split_boundaries(
                session_id, payload.source_artifact_id, payload.segment_id,
                payload.expected_revision, payload.offset, payload.limit,
            )
        except KeyError:
            return error_response("not_found", "Subtitle source or segment not found.", 404)
        except RuntimeError as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)
