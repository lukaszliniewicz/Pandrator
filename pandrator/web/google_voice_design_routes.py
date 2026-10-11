"""Authenticated request admission for native Google prompted voices."""

from __future__ import annotations

from typing import Any

from flask import jsonify, request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .domain_blueprints import DomainBlueprints
from .google_voice_design import configured_google_design_service, google_voice_scope
from .google_voice_design_schemas import GoogleVoiceDesignCreate
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Voice
from .route_context import RouteContext


def register_google_voice_design_routes(
    app: DomainBlueprints,
    context: RouteContext,
) -> None:
    services = context.services
    database = services.database
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth
    idempotency = MutationIdempotency(context)

    @app.post("/api/v1/voices/design/google")
    @require_auth
    def design_google_voice():
        try:
            payload = GoogleVoiceDesignCreate.model_validate(
                request.get_json(silent=True) or {}
            )
        except ValidationError:
            return error_response(
                "validation_error",
                "Invalid Google voice design request.",
                422,
            )
        request_body = payload.model_dump(mode="json")
        inline_secret_error = context.guards.inline_credential_error(request_body)
        if inline_secret_error is not None:
            return inline_secret_error
        idempotency_key = str(request.headers.get("Idempotency-Key") or "").strip()
        if not idempotency_key:
            return error_response(
                "idempotency_key_required",
                "Idempotency-Key is required for Google voice design.",
                400,
            )
        replay = idempotency.inspect(
            "designGoogleVoice",
            idempotency_key,
            request_body,
        )
        if replay is not None:
            return replay

        service_id = payload.service_id
        try:
            service, credential = configured_google_design_service(
                database,
                services.paths,
                service_id,
            )
            if payload.model not in service.get("models", []):
                raise ValueError("The selected Google TTS model is not configured.")
            scope = google_voice_scope(service_id, service, credential)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

        try:
            with database.immediate_session() as db_session:
                try:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="designGoogleVoice",
                        idempotency_key=idempotency_key,
                        payload=request_body,
                    )
                except (
                    IdempotencyConflict,
                    IdempotencyInProgress,
                    ValueError,
                ) as error:
                    return idempotency.failure(error)
                if reservation.replayed:
                    return idempotency.replay(reservation)

                duplicate = db_session.scalar(
                    select(Voice.id).where(Voice.name == payload.name).limit(1)
                )
                if duplicate is not None:
                    db_session.delete(reservation.record)
                    db_session.flush()
                    return error_response(
                        "already_exists",
                        "A voice with that name already exists.",
                        409,
                    )

                voice = Voice(
                    name=payload.name,
                    language=payload.language,
                    description=payload.prompt,
                    metadata_json={
                        "voice_category": payload.voice_category,
                        "google_voice_design": {
                            "state": "queued",
                            "service_id": service_id,
                            "model": payload.model,
                            "prompt": payload.prompt,
                            "scope_fingerprint": scope["fingerprint"],
                            "project": scope["project"],
                        },
                    },
                )
                db_session.add(voice)
                db_session.flush()
                job_payload: dict[str, Any] = {
                    "voice_id": voice.id,
                    "service_id": service_id,
                    "model": payload.model,
                    "name": payload.name,
                    "prompt": payload.prompt,
                    "language": payload.language,
                    "scope_fingerprint": scope["fingerprint"],
                    "project": scope["project"],
                }
                job = services.jobs.enqueue_in_session(
                    db_session,
                    "voice.google_design",
                    dict(job_payload),
                    max_attempts=1,
                    resource_keys=[
                        f"voice:{voice.id}",
                        f"service:tts:{service_id}",
                    ],
                )
                job_payload["job_id"] = job.id
                job.payload_json = {**job_payload}
                db_session.flush()
                metadata = dict(voice.metadata_json or {})
                design = dict(metadata.get("google_voice_design") or {})
                design["job_id"] = job.id
                metadata["google_voice_design"] = design
                voice.metadata_json = metadata

                receipt = _job_payload(job)
                receipt["voice_id"] = voice.id
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=receipt,
                    status_code=202,
                    resource_kind="voice",
                    resource_id=voice.id,
                )
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency.failure(error)
        except IntegrityError:
            return error_response(
                "already_exists",
                "A voice with that name already exists.",
                409,
            )
        return jsonify(receipt), 202
