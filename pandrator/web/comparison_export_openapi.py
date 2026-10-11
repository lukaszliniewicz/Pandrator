"""OpenAPI contract for selected speech-take comparison exports."""

from __future__ import annotations

from .comparison_export_schemas import (
    ComparisonExportClipRequest,
    ComparisonExportRequest,
)

COMPARISON_EXPORT_SCHEMAS = {
    ComparisonExportClipRequest.__name__: ComparisonExportClipRequest,
    ComparisonExportRequest.__name__: ComparisonExportRequest,
}


def comparison_export_paths() -> dict:
    return {
        "/api/v1/sessions/{sessionId}/comparison-exports": {
            "post": {
                "operationId": "createComparisonExport",
                "summary": "Export a comparison of selected speech takes",
                "description": (
                    "Queue one reusable WAV or MP3 from the ordered selected speech takes. "
                    "Recast sides use their pinned before/after takes. The separately assembled "
                    "sound mix is excluded."
                ),
                "security": [
                    {"cookieAuth": []},
                    {"bearerToken": []},
                    {"nativeOAuth": ["app.run"]},
                ],
                "parameters": [
                    {
                        "name": "sessionId",
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string", "format": "uuid"},
                    },
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
                                "$ref": "#/components/schemas/ComparisonExportRequest"
                            }
                        }
                    },
                },
                "responses": {
                    "202": {
                        "description": "Comparison export work item queued.",
                        "content": {
                            "application/json": {
                                "schema": {"type": "object", "additionalProperties": True}
                            }
                        },
                    },
                    "400": {"description": "An idempotency key is required or invalid."},
                    "401": {"description": "Authentication required."},
                    "403": {"description": "The authenticated actor lacks app.run."},
                    "404": {"description": "The session, run, segment, or artifact was not found."},
                    "409": {"description": "The idempotency key conflicts or is still in progress."},
                    "422": {"description": "The selection, source audio, or export options are invalid."},
                },
            }
        }
    }


__all__ = ["COMPARISON_EXPORT_SCHEMAS", "comparison_export_paths"]
