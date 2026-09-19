"""Shared HTTP contract for character identities and casting."""

from .generation_control_schemas import GenerationControlsUpdateRequest

GENERATION_CONTROL_SCHEMAS = {
    "GenerationControlsUpdateRequest": GenerationControlsUpdateRequest
}


def generation_control_paths() -> dict:
    operations = {}
    for method, operation, scope in [
        ("get", "getGenerationControls", "app.read"),
        ("put", "updateGenerationControls", "app.write"),
    ]:
        parameters = []
        if method == "put":
            parameters.append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            )
        item = {
            "operationId": operation,
            "summary": "Read character dictionary and cast"
            if method == "get"
            else "Save character dictionary and cast",
            "description": "Session-scoped stable character identities and compatible voice bindings. Does not start analysis or synthesis. Writes require the current revision and explicit unlock IDs for protected identity changes.",
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": [scope]},
            ],
            "parameters": parameters,
            "responses": {
                code: {"description": description}
                for code, description in [
                    ("200", "Current characters, cast and revision"),
                    ("400", "Missing idempotency key"),
                    ("401", "Authentication required"),
                    ("403", "Insufficient scope"),
                    ("404", "Session not found"),
                    ("409", "Revision or protected identity conflict"),
                    ("422", "Invalid character or cast"),
                ]
            },
        }
        item["responses"]["200"]["content"] = {
            "application/json": {
                "schema": {"type": "object", "additionalProperties": True}
            }
        }
        if method == "put":
            item["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/GenerationControlsUpdateRequest"
                        }
                    }
                },
            }
        operations[method] = item
    return {"/api/v1/sessions/{sessionId}/generation-controls": operations}
