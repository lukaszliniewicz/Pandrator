"""Public contracts for grouped repair history and guarded batch restoration."""

from __future__ import annotations


def repair_batch_paths() -> dict:
    base = "/api/v1/sessions/{sessionId}/generation-plan"
    paging = [
        {"name": "limit", "in": "query", "schema": {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}},
        {"name": "before_revision_number", "in": "query", "schema": {"type": "integer", "minimum": 1}},
        {"name": "summary", "in": "query", "description": "When true, skip audio-reuse inspection and undo-guard evaluation: reuse counts are null with audio_reuse_checked=false and repair eligibility is null with undo_checked=false. Default full preserves the legacy payload.", "schema": {"type": "boolean", "default": False}},
    ]

    def read(operation_id: str, summary: str) -> dict:
        return {
            "operationId": operation_id,
            "summary": summary,
            "parameters": [dict(parameter) for parameter in paging],
            "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": ["app.read"]}],
            "responses": {
                "200": {"description": "Bounded history with actual revision identifiers; preview never changes the active plan."},
                "404": {"description": "Session or repair batch not found."},
                "422": {"description": "Invalid pagination parameters."},
            },
        }

    return {
        f"{base}/history": {
            "get": read("listGroupedSpeechPlanHistory", "List manual plan changes and one entry per automatic repair batch"),
        },
        f"{base}/repair-batches/{{batchId}}": {
            "get": read("getSpeechPlanRepairBatch", "Inspect the individual checkpoints of an automatic repair batch"),
        },
        f"{base}/repair-batches/{{batchId}}/undo": {
            "post": {
                "operationId": "undoSpeechPlanRepairBatch",
                "summary": "Restore the verified pre-repair state as one new plan revision without rewriting historical audio",
                "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": ["app.write"]}],
                "parameters": [{
                    "name": "Idempotency-Key", "in": "header", "required": True,
                    "schema": {"type": "string"},
                    "description": "Replay-safe key scoped to the session, batch, and guarded undo payload.",
                }],
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RepairBatchUndoRequest"}}},
                },
                "responses": {
                    "201": {"description": "New restore revision; original and repaired checkpoints and audio are retained."},
                    "400": {"description": "A valid idempotency key is required."},
                    "404": {"description": "Session or repair batch not found."},
                    "409": {"description": "The plan, selected audio, or repair state changed; work is active; or a historical batch lacks a verified snapshot. Nothing is restored."},
                    "422": {"description": "Invalid or incomplete guard fields."},
                },
            },
        },
    }
