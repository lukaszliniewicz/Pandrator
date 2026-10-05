"""HTTP contracts for the shared training lifecycle."""

from flask import jsonify, request

from .domain_blueprints import DomainBlueprints
from .http_serialization import job_payload
from .route_context import RouteContext
from .schemas import TrainingCreateRequest
from .training_lifecycle import TrainingActive, TrainingNotFound


def register_training_routes(app: DomainBlueprints, context: RouteContext) -> None:
    training = context.services.training
    require_auth = context.guards.require_auth
    error_response = context.guards.error_response

    @app.get("/api/v1/training")
    @require_auth
    def training_list():
        return jsonify({"items": training.list()})

    @app.post("/api/v1/training")
    @require_auth
    def training_create():
        payload = TrainingCreateRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := context.guards.inline_credential_error(payload.settings):
            return rejected
        try:
            record, job = training.start(payload)
        except TrainingNotFound as error:
            return error_response("not_found", error.message, 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = job_payload(job)
        response["training_id"] = record.id
        return jsonify(response), 202

    @app.post("/api/v1/training/<training_id>/retry")
    @require_auth
    def training_retry(training_id: str):
        try:
            record, job = training.retry(training_id)
        except TrainingNotFound as error:
            return error_response("not_found", error.message, 404)
        except TrainingActive as error:
            return error_response("training_active", error.message, 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = job_payload(job)
        response["training_id"] = record.id
        response["retried_from"] = training_id
        return jsonify(response), 202

    @app.post("/api/v1/training/<training_id>/cancel")
    @require_auth
    def training_cancel(training_id: str):
        try:
            result = training.cancel(training_id)
        except TrainingNotFound as error:
            return error_response("not_found", error.message, 404)
        return jsonify(result), 202
