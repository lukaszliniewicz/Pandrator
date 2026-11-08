"""OpenAPI fragments for shared voice setup and audiobook speech preview."""

from .audiobook_schemas import AUDIOBOOK_SCHEMAS, VOICE_SETUP_SCHEMAS


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
            "application/json": {"schema": {"type": "object", "additionalProperties": True}}
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
                    "schema": {"$ref": "#/components/schemas/AudiobookSetupConfigureRequest"}
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
    voice_setup_get = {
        "operationId": "getVoiceSetup",
        "summary": "Inspect session voice mode",
        "description": (
            "Read audiobook or voiceover voice mode and the selected TTS voice. "
            "Legacy sessions are identified so strict single-voice rendering can "
            "be adopted explicitly."
        ),
        "security": _security("app.read"),
        "parameters": [_session_parameter()],
        "responses": {
            "200": _response("Current voice mode and configuration revision."),
            "401": {"description": "Authentication required."},
            "403": {"description": "The authenticated actor lacks the required scope."},
            "404": {"description": "The voice setup session was not found."},
            "422": {
                "description": "Voice setup is available for audiobook and voiceover sessions."
            },
        },
    }
    voice_setup_patch = {
        "operationId": "configureVoiceSetup",
        "summary": "Configure session voice mode",
        "description": (
            "Atomically select single-voice or multi-voice rendering for an "
            "audiobook or voiceover session. This changes only casting mode and "
            "the strict-rendering adoption marker; it does not change text "
            "preparation, speaker labels, or speech-performance settings. "
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
                    "schema": {"$ref": "#/components/schemas/VoiceSetupConfigureRequest"}
                }
            },
        },
        "responses": {
            "200": _response("Current voice mode and configuration revision."),
            "400": {"description": "A valid idempotency key is required."},
            "401": {"description": "Authentication required."},
            "403": {"description": "The authenticated actor lacks the required scope."},
            "404": {"description": "The voice setup session was not found."},
            "409": {
                "description": "The current configuration is stale or the idempotency key conflicts."
            },
            "422": {
                "description": "The request fields are invalid or the workflow is unsupported."
            },
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
                    "schema": {"$ref": "#/components/schemas/SpeechPlanPreviewRequest"}
                }
            },
        },
        "responses": {
            "200": _response("Compiled speech-plan segment preview; no audio."),
            **common_errors,
        },
    }
    preparation = {
        "operationId": "prepareAudiobookGeneration",
        "summary": "Prepare automatic audiobook narration for current settings",
        "description": (
            "Explicit generation prerequisite. Queue deterministic text preparation "
            "when the effective chunk budget changed and the selected plan is provably "
            "untouched automatic prepared text. Preserve edited and annotated plans. "
            "Historical settings sources retain their selected segmentation. No synthesis."
        ),
        "security": _security("app.run"),
        "parameters": [_session_parameter()],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/AudiobookGenerationPreparationRequest"}
                }
            },
        },
        "responses": {
            "200": _response(
                "Current, preserved, or queued narration preparation; queued responses include job_id."
            ),
            **common_errors,
        },
    }
    segmentation_preview = {
        "operationId": "previewAudiobookSegmentation",
        "summary": "Preview effective audiobook segmentation settings",
        "description": (
            "Resolve optional text and TTS overrides through current settings inheritance "
            "and return effective and model-default chunk budgets. No settings writes, "
            "external calls, or text preparation. Inline credentials are rejected."
        ),
        "security": _security("app.read"),
        "parameters": [_session_parameter()],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": "#/components/schemas/AudiobookSegmentationPreviewRequest"}
                }
            },
        },
        "responses": {
            "200": _response("Effective segmentation and model_default_segmentation budgets."),
            **common_errors,
        },
    }
    return {
        "/api/v1/sessions/{sessionId}/audiobook-setup": {
            "get": setup_get,
            "patch": setup_patch,
        },
        "/api/v1/sessions/{sessionId}/voice-setup": {
            "get": voice_setup_get,
            "patch": voice_setup_patch,
        },
        "/api/v1/sessions/{sessionId}/speech-plan/preview": {
            "post": preview,
        },
        "/api/v1/sessions/{sessionId}/audiobook-generation-preparation": {
            "post": preparation,
        },
        "/api/v1/sessions/{sessionId}/audiobook-segmentation-preview": {
            "post": segmentation_preview,
        },
    }


__all__ = ["AUDIOBOOK_SCHEMAS", "VOICE_SETUP_SCHEMAS", "audiobook_paths"]
