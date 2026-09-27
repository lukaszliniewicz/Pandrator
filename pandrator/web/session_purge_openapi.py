"""OpenAPI fragments for session purge and trash policy routes."""

SESSION_PURGE_SCHEMAS: dict[str, dict] = {
    "SessionPurgePreview": {
        "type": "object",
        "required": ["revision", "impact_token", "can_purge", "blockers", "owned_file_count", "owned_bytes", "retained_shared_count", "scheduled_delete_at"],
        "properties": {
            "revision": {"type": "integer"},
            "impact_token": {"type": "string", "minLength": 64, "maxLength": 64},
            "can_purge": {"type": "boolean"},
            "blockers": {"type": "array", "items": {"type": "string"}},
            "owned_file_count": {"type": "integer", "minimum": 0},
            "owned_bytes": {"type": "integer", "minimum": 0},
            "retained_shared_count": {"type": "integer", "minimum": 0},
            "scheduled_delete_at": {"type": ["string", "null"], "format": "date-time"},
        },
    },
    "SessionPurgeRequest": {
        "type": "object", "required": ["expected_revision", "impact_token"],
        "properties": {"expected_revision": {"type": "integer", "minimum": 1}, "impact_token": {"type": "string", "minLength": 64, "maxLength": 64}},
        "additionalProperties": False,
    },
    "SessionPurgeResult": {
        "type": "object", "required": ["state"],
        "properties": {"state": {"type": "string", "enum": ["complete", "purging", "failed"]}, "error": {"type": "string"}},
    },
    "SessionTrashPolicy": {
        "type": "object", "required": ["revision", "days"],
        "properties": {"revision": {"type": "integer", "minimum": 0}, "days": {"type": ["integer", "null"], "minimum": 1, "maximum": 3650}},
    },
    "SessionTrashPolicyUpdate": {
        "type": "object", "required": ["expected_revision", "days"],
        "properties": {"expected_revision": {"type": "integer", "minimum": 0}, "days": {"type": ["integer", "null"], "minimum": 1, "maximum": 3650}},
        "additionalProperties": False,
    },
}


def _security(scope: str) -> list[dict[str, list[str]]]:
    return [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": [scope]}]


def _response(description: str, schema: str) -> dict:
    return {"description": description, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}}


def session_purge_paths() -> dict:
    session_parameter = {"name": "sessionId", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}
    errors = {"400": {"description": "Invalid request."}, "401": {"description": "Authentication required."}, "403": {"description": "Insufficient scope."}, "404": {"description": "Session not found."}, "409": {"description": "Revision, impact, active work or storage conflict."}}
    return {
        "/api/v1/sessions/{sessionId}/purge-preview": {"get": {
            "operationId": "getSessionPurgePreview", "summary": "Preview removal of a trashed session",
            "security": _security("app.read"), "parameters": [session_parameter],
            "responses": {"200": _response("Impact and blockers.", "SessionPurgePreview"), **errors},
        }},
        "/api/v1/sessions/{sessionId}/purge": {"post": {
            "operationId": "purgeSession", "summary": "Permanently remove a trashed session",
            "security": _security("app.write"), "parameters": [session_parameter],
            "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SessionPurgeRequest"}}}},
            "responses": {"200": _response("Durable cleanup state.", "SessionPurgeResult"), **errors},
        }},
        "/api/v1/session-trash-policy": {
            "get": {"operationId": "getSessionTrashPolicy", "summary": "Read automatic trash retention policy",
                    "security": _security("app.read"), "responses": {"200": _response("Policy and revision.", "SessionTrashPolicy"), **errors}},
            "patch": {"operationId": "updateSessionTrashPolicy", "summary": "Set retention for future trash events",
                      "security": _security("app.write"),
                      "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SessionTrashPolicyUpdate"}}}},
                      "responses": {"200": _response("Updated policy.", "SessionTrashPolicy"), **errors}},
        },
    }
