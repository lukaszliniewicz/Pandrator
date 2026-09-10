"""Typed, guarded UI/API actions for source management and speech-plan review."""

from __future__ import annotations

import re
from typing import Literal

from flask import jsonify, request
from pydantic import BaseModel, ConfigDict, Field

from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .source_management import (
    change_source_in_session,
    cleanup_reset_files,
    preview_source_change,
    source_status,
    confirm_recording_timing,
    start_new_source_session_in_session,
)
from .speech_plan_workspace import (
    prepare_speech_plan,
    prepare_speech_plan_data,
    review_speech_plan,
    select_speech_plan,
    speech_plan_status,
)
from .workspace import RevisionConflict


class SourceChangePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    role: Literal["primary", "media"] = "primary"
    new_source_asset_id: str | None = Field(default=None, min_length=1, max_length=80)


class SourceChangeRequest(SourceChangePreviewRequest):
    expected_revision: int = Field(ge=1)
    impact_token: str = Field(pattern=r"^[a-f0-9]{64}$")


class SpeechPlanPrepareRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    expected_plan_revision_id: str | None = Field(
        default=None, min_length=1, max_length=80
    )
    source_artifact_id: str = Field(min_length=1, max_length=80)


class SpeechPlanSelectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(min_length=1, max_length=80)
    expected_plan_revision_id: str | None = Field(
        default=None, min_length=1, max_length=80
    )


class SpeechPlanReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision_id: str = Field(min_length=1, max_length=80)
    content_signature: str = Field(min_length=32, max_length=128)


class RecordingTimingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_revision: int = Field(ge=1)
    media_artifact_id: str = Field(min_length=1, max_length=80)


FLOW_SCHEMAS = {
    model.__name__: model
    for model in (
        RecordingTimingRequest,
        SourceChangePreviewRequest,
        SourceChangeRequest,
        SpeechPlanPrepareRequest,
        SpeechPlanSelectRequest,
        SpeechPlanReviewRequest,
    )
}


def session_flow_paths():
    specifications = (
        ("sources/status", "get", "getSessionSourceStatus", None),
        (
            "sources/change-preview",
            "post",
            "previewSessionSourceChange",
            SourceChangePreviewRequest,
        ),
        ("sources/change", "post", "changeSessionSource", SourceChangeRequest),
        (
            "sources/start-new-session",
            "post",
            "startNewSourceSession",
            SourceChangeRequest,
        ),
        ("sources/cleanup", "post", "cleanupResetSourceFiles", None),
        (
            "sources/confirm-timing",
            "post",
            "confirmRecordingTiming",
            RecordingTimingRequest,
        ),
        ("generation-plan/status", "get", "getSpeechPlanStatus", None),
        (
            "generation-plan/prepare",
            "post",
            "prepareSpeechPlan",
            SpeechPlanPrepareRequest,
        ),
        ("generation-plan/select", "post", "selectSpeechPlan", SpeechPlanSelectRequest),
        ("generation-plan/review", "post", "reviewSpeechPlan", SpeechPlanReviewRequest),
    )
    paths = {}
    for suffix, method, operation_id, model in specifications:
        write = method == "post" and not suffix.endswith("preview")
        operation = {
            "operationId": operation_id,
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": ["app.write" if write else "app.read"]},
            ],
            "responses": {
                "200": {
                    "description": "Current state or completed action",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        }
                    },
                },
                "404": {"description": "Session or revision not found"},
                "409": {
                    "description": "Changed revision, active work, or idempotency conflict"
                },
                "422": {"description": "Invalid action"},
            },
        }
        if write and suffix != "sources/cleanup":
            operation["parameters"] = [
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            ]
        if model is not None:
            operation["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{model.__name__}"}
                    }
                },
            }
        paths[f"/api/v1/sessions/{{sessionId}}/{suffix}"] = {method: operation}
    return paths


def register_session_flow_routes(
    app, services, require_auth, error_response, principal
):
    def failure(error):
        if isinstance(error, KeyError):
            return error_response(
                "not_found", "Session, source or speech-plan revision not found.", 404
            )
        if isinstance(
            error, (RevisionConflict, IdempotencyConflict, IdempotencyInProgress)
        ):
            return error_response("revision_conflict", str(error), 409)
        return error_response("validation_error", str(error), 422)

    def mutate(session_id, operation_id, payload, action):
        key = request.headers.get("Idempotency-Key", "")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{7,199}", key):
            return error_response(
                "idempotency_key_required",
                "Provide a valid Idempotency-Key for this action.",
                400,
            )
        try:
            with services.database.immediate_session() as session:
                reservation = services.idempotency.begin(
                    session,
                    principal=principal(),
                    operation_id=operation_id,
                    idempotency_key=key,
                    payload={"session_id": session_id, **payload},
                )
                if reservation.response is not None:
                    result, status_code = reservation.response
                    response = jsonify(result)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                result = action(session)
                services.idempotency.complete(
                    session,
                    reservation,
                    response=result,
                    status_code=200,
                    resource_kind="session",
                    resource_id=session_id,
                )
            return jsonify(result)
        except (
            KeyError,
            ValueError,
            RevisionConflict,
            IdempotencyConflict,
            IdempotencyInProgress,
        ) as error:
            return failure(error)

    @app.get("/api/v1/sessions/<session_id>/sources/status")
    @require_auth
    def session_source_status(session_id):
        try:
            with services.database.session() as session:
                return jsonify(source_status(session, session_id))
        except KeyError as error:
            return failure(error)

    @app.post("/api/v1/sessions/<session_id>/sources/change-preview")
    @require_auth
    def session_source_change_preview(session_id):
        payload = SourceChangePreviewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            with services.database.session() as session:
                return jsonify(
                    preview_source_change(session, session_id, **payload.model_dump())
                )
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.post("/api/v1/sessions/<session_id>/sources/change")
    @require_auth
    def session_source_change(session_id):
        payload = SourceChangeRequest.model_validate(
            request.get_json(silent=True) or {}
        ).model_dump()
        response = mutate(
            session_id,
            "changeSessionSource",
            payload,
            lambda session: change_source_in_session(
                services, session, session_id, **payload
            ),
        )
        if getattr(response, "status_code", 500) == 200:
            cleanup_reset_files(services, session_id)
        return response

    @app.post("/api/v1/sessions/<session_id>/sources/start-new-session")
    @require_auth
    def session_source_start_new(session_id):
        payload = SourceChangeRequest.model_validate(
            request.get_json(silent=True) or {}
        ).model_dump()
        return mutate(
            session_id,
            "startNewSourceSession",
            payload,
            lambda session: start_new_source_session_in_session(
                services, session, session_id, **payload
            ),
        )

    @app.post("/api/v1/sessions/<session_id>/sources/cleanup")
    @require_auth
    def session_source_cleanup(session_id):
        # Only pre-existing tombstones are eligible; this never removes live data.
        try:
            with services.database.session() as session:
                source_status(session, session_id)
            return jsonify(cleanup_reset_files(services, session_id))
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.post("/api/v1/sessions/<session_id>/sources/confirm-timing")
    @require_auth
    def session_recording_timing_confirm(session_id):
        payload = RecordingTimingRequest.model_validate(
            request.get_json(silent=True) or {}
        ).model_dump()
        return mutate(
            session_id,
            "confirmRecordingTiming",
            payload,
            lambda session: confirm_recording_timing(session, session_id, **payload),
        )

    @app.get("/api/v1/sessions/<session_id>/generation-plan/status")
    @require_auth
    def speech_plan_workspace_status(session_id):
        try:
            return jsonify(speech_plan_status(services, session_id))
        except (KeyError, ValueError) as error:
            return failure(error)

    @app.post("/api/v1/sessions/<session_id>/generation-plan/prepare")
    @require_auth
    def speech_plan_prepare(session_id):
        payload = SpeechPlanPrepareRequest.model_validate(
            request.get_json(silent=True) or {}
        ).model_dump()
        try:
            prepared = prepare_speech_plan_data(
                services, session_id, payload["source_artifact_id"]
            )
        except (KeyError, ValueError, RevisionConflict) as error:
            return failure(error)
        return mutate(
            session_id,
            "prepareSpeechPlan",
            payload,
            lambda session: prepare_speech_plan(
                services,
                session,
                session_id,
                expected_revision=payload["expected_revision"],
                expected_plan_revision_id=payload["expected_plan_revision_id"],
                prepared=prepared,
            ),
        )

    @app.post("/api/v1/sessions/<session_id>/generation-plan/select")
    @require_auth
    def speech_plan_select(session_id):
        payload = SpeechPlanSelectRequest.model_validate(
            request.get_json(silent=True) or {}
        ).model_dump()
        return mutate(
            session_id,
            "selectSpeechPlan",
            payload,
            lambda session: select_speech_plan(session, session_id, **payload),
        )

    @app.post("/api/v1/sessions/<session_id>/generation-plan/review")
    @require_auth
    def speech_plan_review(session_id):
        payload = SpeechPlanReviewRequest.model_validate(
            request.get_json(silent=True) or {}
        ).model_dump()
        return mutate(
            session_id,
            "reviewSpeechPlan",
            payload,
            lambda session: review_speech_plan(session, session_id, **payload),
        )
