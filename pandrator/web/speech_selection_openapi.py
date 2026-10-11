"""OpenAPI fragments for direct speech-selection editing."""

from .speech_selection_batch_schemas import SPEECH_SELECTION_BATCH_SCHEMAS
from .speech_selection_schemas import SPEECH_SELECTION_SCHEMAS

SPEECH_SELECTION_SCHEMAS = {**SPEECH_SELECTION_SCHEMAS, **SPEECH_SELECTION_BATCH_SCHEMAS}


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


def speech_selection_paths() -> dict:
    errors = {
        "401": {"description": "Authentication required."},
        "403": {"description": "The authenticated actor lacks the required scope."},
        "404": {"description": "The speech session or segment was not found."},
        "409": {"description": "The selected speech plan, block, lock, or preview is stale."},
        "422": {"description": "The request fields or speech markup are invalid."},
    }
    preview = {
        "operationId": "previewSpeechSelection",
        "summary": "Preview a direct speech-selection edit",
        "description": (
            "Compile a selected phrase with proposed speaker, voice, and delivery "
            "metadata, or insert a vocal_event at a cursor (start equals end). "
            "Cursor requests cannot include speaker, voice, delivery or XML edits. "
            "This read-only operation creates no performance-plan row and "
            "does not access providers or synthesize audio."
        ),
        "security": _security("app.read"),
        "parameters": [_session_parameter()],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/SpeechSelectionRequest"}
                }
            },
        },
        "responses": {"200": _response("Compilation-only selection preview."), **errors},
    }
    apply = {
        "operationId": "applySpeechSelection",
        "summary": "Apply a reviewed direct speech-selection edit",
        "description": (
            "Create and adopt one manual XML performance plan containing the reviewed "
            "selection while preserving the previous plan and generated audio."
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
                        "$ref": "#/components/schemas/SpeechSelectionApplyRequest"
                    }
                }
            },
        },
        "responses": {"200": _response("Adopted manual selection edit."), **errors},
    }
    return {
        "/api/v1/sessions/{sessionId}/speech-plan/selection-context": {
            "post": {
                "operationId": "getSpeechSelectionContext",
                "summary": "Inspect speech selection",
                "description": (
                    "Inspect a selected text range or cursor (start equals end), current speaker and delivery, "
                    "capability feedback, editable XML and the allowed XML action "
                    "reference without generating audio."
                ),
                "security": _security("app.read"),
                "parameters": [_session_parameter()],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {
                        "$ref": "#/components/schemas/SpeechSelectionContextRequest"
                    }}},
                },
                "responses": {
                    "200": _response("Current selection and XML authoring reference."),
                    **errors,
                },
            },
        },
        "/api/v1/sessions/{sessionId}/speech-plan/selection-preview": {
            "post": preview
        },
        "/api/v1/sessions/{sessionId}/speech-plan/selection": {"post": apply},
        "/api/v1/sessions/{sessionId}/speech-plan/selection-batch-preview": {
            "post": {
                "operationId": "previewSpeechSelectionBatch",
                "summary": "Preview an atomic speech selection batch",
                "description": (
                    "Compile each affected speech block without creating rows or contacting providers. "
                    "The default reply is compact; detailed_preview includes up to ten full block previews."
                ),
                "security": _security("app.read"),
                "parameters": [_session_parameter()],
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/SpeechSelectionBatchRequest"}
                }}},
                "responses": {"200": _response("Compilation-only batch preview."), **errors},
            },
        },
        "/api/v1/sessions/{sessionId}/speech-plan/selection-batch": {
            "post": {
                "operationId": "applySpeechSelectionBatch",
                "summary": "Apply an atomic speech selection batch",
                "description": (
                    "Adopt one copied manual XML performance plan for all reviewed changes "
                    "and create one sound-plan version if sound cues changed."
                ),
                "security": _security("app.write"),
                "parameters": [
                    _session_parameter(),
                    {"name": "Idempotency-Key", "in": "header", "required": True,
                     "schema": {"type": "string", "minLength": 8, "maxLength": 200}},
                ],
                "requestBody": {"required": True, "content": {"application/json": {
                    "schema": {"$ref": "#/components/schemas/SpeechSelectionBatchApplyRequest"}
                }}},
                "responses": {"200": _response("Adopted atomic selection batch."), **errors},
            },
        },
    }


__all__ = ["SPEECH_SELECTION_SCHEMAS", "speech_selection_paths"]
