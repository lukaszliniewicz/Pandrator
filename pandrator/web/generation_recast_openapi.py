"""HTTP contract for scoped casting and pinned audio versions."""

from .generation_recast_schemas import (
    AudioVersionUndoRequest,
    GenerationRecastPreviewRequest,
    GenerationRecastStartRequest,
)

GENERATION_RECAST_SCHEMAS = {
    "GenerationRecastPreviewRequest": GenerationRecastPreviewRequest,
    "GenerationRecastStartRequest": GenerationRecastStartRequest,
    "AudioVersionUndoRequest": AudioVersionUndoRequest,
}


def generation_recast_paths() -> dict:
    result = {}
    for path, operation, schema, write in [
        ("/api/v1/sessions/{sessionId}/generation-recast/preview", "previewGenerationRecast", "GenerationRecastPreviewRequest", False),
        ("/api/v1/sessions/{sessionId}/generation-recast", "startGenerationRecast", "GenerationRecastStartRequest", True),
        ("/api/v1/generation-runs/{runId}/audio-version/undo-preview", "previewGenerationRecastUndo", None, False),
        ("/api/v1/generation-runs/{runId}/audio-version/undo", "undoGenerationRecast", "AudioVersionUndoRequest", True),
    ]:
        item = {
            "operationId": operation,
            "summary": operation,
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": ["app.write" if write else "app.read"]}],
            "parameters": [{"name": "Idempotency-Key", "in": "header", "required": True, "schema": {"type": "string", "minLength": 8, "maxLength": 200}}] if write else [],
            "responses": {
                "201" if operation == "startGenerationRecast" else "200": {
                    "description": "Compact cast impact or audio version receipt",
                    "content": {"application/json": {"schema": {"type": "object", "additionalProperties": True}}},
                },
                "404": {"description": "Project or audio version not found"},
                "409": {"description": "Cast, plan, audio selection or request changed"},
                "422": {"description": "Invalid request or unavailable recast"},
            },
        }
        if schema:
            item["requestBody"] = {"required": True, "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}}}
        result[path] = {"post": item}
    return result
