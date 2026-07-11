"""Small deterministic OpenAPI document for frontend client generation."""

from __future__ import annotations

from .schemas import SCHEMA_MODELS


def build_openapi_document() -> dict:
    schemas = {name: model.model_json_schema(ref_template="#/components/schemas/{model}") for name, model in SCHEMA_MODELS.items()}
    return {
        "openapi": "3.1.0",
        "info": {"title": "Pandrator API", "version": "1.0.0"},
        "servers": [{"url": "/"}],
        "paths": {
            "/api/v1/health": {"get": {"operationId": "getHealth", "responses": {"200": {"description": "Healthy"}}}},
            "/api/v1/capabilities": {"get": {"operationId": "getCapabilities", "responses": {"200": {"description": "Runtime capabilities"}}}},
            "/api/v1/sessions": {
                "get": {"operationId": "listSessions", "responses": {"200": {"description": "Sessions"}}},
                "post": {
                    "operationId": "createSession",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SessionCreate"}}}},
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/v1/sessions/{sessionId}": {
                "get": {"operationId": "getSession", "parameters": [{"name": "sessionId", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}], "responses": {"200": {"description": "Session"}}},
                "patch": {
                    "operationId": "updateSession",
                    "parameters": [
                        {"name": "sessionId", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}},
                        {"name": "If-Match", "in": "header", "required": True, "schema": {"type": "string"}},
                    ],
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SessionUpdate"}}}},
                    "responses": {"200": {"description": "Updated"}, "409": {"description": "Revision conflict"}},
                },
            },
            "/api/v1/jobs": {
                "get": {"operationId": "listJobs", "responses": {"200": {"description": "Jobs"}}},
                "post": {
                    "operationId": "createJob",
                    "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/JobCreate"}}}},
                    "responses": {"202": {"description": "Queued"}},
                },
            },
            "/api/v1/events": {"get": {"operationId": "streamEvents", "responses": {"200": {"description": "SSE job events"}}}},
        },
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "bearerToken": {"type": "http", "scheme": "bearer"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "session"},
            },
        },
    }

