"""OpenAPI fragments for audiobook setup and speech-plan preview."""

from .audiobook_schemas import AUDIOBOOK_SCHEMAS


def _security(scope: str) -> list[dict[str, list[str]]]:
    return [
        {"cookieAuth": []},
        {"bearerToken": []},
        {"nativeOAuth": [scope]},
    ]


def _session_parameter() -> dict:
    return {
        "name": "sessionId",
        "in": "path",
        "required": True,
        "schema": {"type": "string", "format": "uuid"},
    }


def _response(description: str) -> dict:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {"type": "object", "additionalProperties": True}
            }
        },
    }


def audiobook_paths() -> dict:
    """Return documented audiobook paths without inventing response fields."""

    common_errors = {
        "401": {"description": "Authentication required."},
        "403": {"description": "The authenticated actor lacks the required scope."},
        "404": {"description": "The audiobook session or requested segment was not found."},
        "409": {"description": "The current configuration or speech-plan revision is stale."},
        "422": {"description": "The request fields are invalid."},
    }
    setup_patch = {
        "operationId": "configureAudiobook",
        "summary": "Configure audiobook voice mode",
        "description": (
            "Atomically configure audiobook annotation/casting and document-optimization "
            "flags while preserving engine, references and delivery directions. "
            "The operation never starts synthesis."
        ),
        "security": _security("app.write"),
        "parameters": [
            _session_parameter(),
            {
                "name": "Idempotency-Key",
                "in": "header",
                "required": True,
                "schema": {"type": "string", "minLength": 8, "maxLength": 200},
            },
        ],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/AudiobookSetupConfigureRequest"
                    }
                }
            },
        },
        "responses": {
            "200": _response("Current audiobook setup and configuration revision."),
            "400": {"description": "A valid idempotency key is required."},
            **common_errors,
        },
    }
    setup_get = {
        "operationId": "getAudiobookSetup",
        "summary": "Inspect audiobook setup",
        "description": (
            "Read the current audiobook voice mode and effective configuration "
            "without refreshing the catalog or writing session state."
        ),
        "security": _security("app.read"),
        "parameters": [_session_parameter()],
        "responses": {
            "200": _response("Current audiobook setup and configuration revision."),
            **common_errors,
        },
    }
    preview = {
        "operationId": "previewSpeechSegment",
        "summary": "Preview a speech-plan segment",
        "description": (
            "Compile the current or explicitly frozen cast and speech directions "
            "for one segment. This read-only operation creates no performance plan "
            "and synthesizes no audio; include_request opts into provider request details."
        ),
        "security": _security("app.read"),
        "parameters": [_session_parameter()],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": "#/components/schemas/SpeechPlanPreviewRequest"
                    }
                }
            },
        },
        "responses": {
            "200": _response("Compiled speech-plan segment preview; no audio."),
            **common_errors,
        },
    }
    return {
        "/api/v1/sessions/{sessionId}/audiobook-setup": {
            "get": setup_get,
            "patch": setup_patch,
        },
        "/api/v1/sessions/{sessionId}/speech-plan/preview": {
            "post": preview,
        },
    }


__all__ = ["AUDIOBOOK_SCHEMAS", "audiobook_paths"]
