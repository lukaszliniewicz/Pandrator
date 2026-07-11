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
            "/api/v1/auth/status": {"get": {"operationId": "getAuthStatus", "responses": {"200": {"description": "Authentication status"}}}},
            "/api/v1/auth/login": {"post": {"operationId": "login", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/LoginRequest"}}}}, "responses": {"200": {"description": "Authenticated"}}}},
            "/api/v1/auth/tokens": {"get": {"operationId": "listApiTokens", "responses": {"200": {"description": "Tokens"}}}, "post": {"operationId": "createApiToken", "responses": {"201": {"description": "Created"}}}},
            "/api/v1/settings/{settingKey}": {"get": {"operationId": "getSetting", "responses": {"200": {"description": "Setting"}}}, "put": {"operationId": "putSetting", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SettingUpdate"}}}}, "responses": {"200": {"description": "Saved"}, "409": {"description": "Revision conflict"}}}},
            "/api/v1/uploads": {"post": {"operationId": "uploadSource", "responses": {"201": {"description": "Uploaded"}}}},
            "/api/v1/artifacts": {"get": {"operationId": "listArtifacts", "responses": {"200": {"description": "Artifacts"}}}},
            "/api/v1/artifacts/{artifactId}/content": {"get": {"operationId": "getArtifactContent", "responses": {"200": {"description": "Range-capable artifact content"}}}},
            "/api/v1/artifacts/{artifactId}/pdf": {"get": {"operationId": "inspectPdf", "responses": {"200": {"description": "PDF geometry"}}}},
            "/api/v1/sessions/{sessionId}/workflow": {"get": {"operationId": "getWorkflow", "responses": {"200": {"description": "Workflow snapshot"}}}},
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/run": {"post": {"operationId": "runWorkflowStage", "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/sessions/{sessionId}/subtitles": {"get": {"operationId": "getSubtitleComparison", "responses": {"200": {"description": "Aligned subtitle revisions"}}}},
            "/api/v1/sessions/{sessionId}/subtitles/{stage}/review": {"post": {"operationId": "saveSubtitleReview", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SubtitleReviewRequest"}}}}, "responses": {"201": {"description": "Reviewed revision"}, "409": {"description": "Revision conflict"}}}},
            "/api/v1/sessions/{sessionId}/pdf/apply": {"post": {"operationId": "applyPdfEdits", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/PdfEditRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/sessions/{sessionId}/bundle": {"post": {"operationId": "exportSessionBundle", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BundleExportRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/session-bundles/import": {"post": {"operationId": "importSessionBundle", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/BundleImportRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/providers": {"get": {"operationId": "listProviders", "responses": {"200": {"description": "Providers"}}}, "post": {"operationId": "createProvider", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProviderCreate"}}}}, "responses": {"201": {"description": "Created"}}}},
            "/api/v1/providers/{providerId}/models": {"get": {"operationId": "listProviderModels", "responses": {"200": {"description": "Models"}}}, "post": {"operationId": "createProviderModel", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ModelCreate"}}}}, "responses": {"201": {"description": "Created"}}}},
            "/api/v1/providers/{providerId}/models/{modelId}": {"patch": {"operationId": "updateProviderModel", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ModelUpdate"}}}}, "responses": {"200": {"description": "Updated"}, "409": {"description": "Revision conflict"}}}, "delete": {"operationId": "deleteProviderModel", "responses": {"204": {"description": "Deleted"}, "409": {"description": "Replacement required"}}}},
            "/api/v1/voices": {"get": {"operationId": "listVoices", "responses": {"200": {"description": "Voices"}}}, "post": {"operationId": "createVoice", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoiceCreate"}}}}, "responses": {"201": {"description": "Created"}}}},
            "/api/v1/voices/{voiceId}/samples": {"get": {"operationId": "listVoiceSamples", "responses": {"200": {"description": "Samples"}}}, "post": {"operationId": "uploadVoiceSample", "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/jobs/{jobId}": {"get": {"operationId": "getJob", "responses": {"200": {"description": "Job"}}}},
            "/api/v1/jobs/{jobId}/cancel": {"post": {"operationId": "cancelJob", "responses": {"200": {"description": "Cancellation requested"}}}},
        },
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "bearerToken": {"type": "http", "scheme": "bearer"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "session"},
            },
        },
    }

