"""Small deterministic OpenAPI document for frontend client generation."""

from __future__ import annotations

from .schemas import SCHEMA_MODELS


def build_openapi_document() -> dict:
    schemas = {name: model.model_json_schema(ref_template="#/components/schemas/{model}") for name, model in SCHEMA_MODELS.items()}
    document = {
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
            "/api/v1/sessions/{sessionId}/sources/url": {"post": {"operationId": "downloadSourceUrl", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SourceUrlRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/sessions/{sessionId}/sources/reuse": {"post": {"operationId": "reuseSource", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/SourceReuseRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
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
            "/api/v1/providers/profiles": {"get": {"operationId": "listProviderProfiles", "responses": {"200": {"description": "LiteLLM provider profiles"}}}},
            "/api/v1/providers/{providerId}": {"patch": {"operationId": "updateProvider", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProviderUpdate"}}}}, "responses": {"200": {"description": "Updated"}, "409": {"description": "Revision conflict"}}}, "delete": {"operationId": "deleteProvider", "responses": {"204": {"description": "Deleted"}, "409": {"description": "Replacement required"}}}},
            "/api/v1/providers/{providerId}/models": {"get": {"operationId": "listProviderModels", "responses": {"200": {"description": "Models"}}}, "post": {"operationId": "createProviderModel", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ModelCreate"}}}}, "responses": {"201": {"description": "Created"}}}},
            "/api/v1/providers/{providerId}/test": {"post": {"operationId": "testProvider", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ProviderTestRequest"}}}}, "responses": {"200": {"description": "Provider ready"}, "422": {"description": "Provider test failed"}}}},
            "/api/v1/providers/{providerId}/models/{modelId}": {"patch": {"operationId": "updateProviderModel", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ModelUpdate"}}}}, "responses": {"200": {"description": "Updated"}, "409": {"description": "Revision conflict"}}}, "delete": {"operationId": "deleteProviderModel", "responses": {"204": {"description": "Deleted"}, "409": {"description": "Replacement required"}}}},
            "/api/v1/voices": {"get": {"operationId": "listVoices", "responses": {"200": {"description": "Voices"}}}, "post": {"operationId": "createVoice", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoiceCreate"}}}}, "responses": {"201": {"description": "Created"}}}},
            "/api/v1/voices/{voiceId}/samples": {"get": {"operationId": "listVoiceSamples", "responses": {"200": {"description": "Samples"}}}, "post": {"operationId": "uploadVoiceSample", "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/voices/{voiceId}/providers/{serviceId}": {"post": {"operationId": "publishVoiceToProvider", "responses": {"202": {"description": "Provider upload queued"}}}},
            "/api/v1/rvc/models": {"get": {"operationId": "listRvcModels", "responses": {"200": {"description": "RVC readiness and models"}}}, "post": {"operationId": "uploadRvcModel", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RvcModelUploadRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/rvc/convert": {"post": {"operationId": "convertWithRvc", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/RvcConvertRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/training": {"get": {"operationId": "listTrainingRuns", "responses": {"200": {"description": "Training runs"}}}, "post": {"operationId": "createTrainingRun", "requestBody": {"required": True, "content": {"application/json": {"schema": {"$ref": "#/components/schemas/TrainingCreateRequest"}}}}, "responses": {"202": {"description": "Queued"}}}},
            "/api/v1/training/{trainingId}/cancel": {"post": {"operationId": "cancelTrainingRun", "responses": {"202": {"description": "Cancellation requested"}}}},
            "/api/v1/training/{trainingId}/retry": {"post": {"operationId": "retryTrainingRun", "responses": {"202": {"description": "Retry queued"}}}},
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
    paths = document["paths"]

    def operation(operation_id: str, description: str, schema: str | None = None, status: str = "200") -> dict:
        value = {"operationId": operation_id, "responses": {status: {"description": description}}}
        if schema:
            value["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
            }
        return value

    # Parity-workspace operations are declared alongside their Pydantic DTOs;
    # this block is also the source used to generate the checked-in TS client.
    paths.update({
        "/api/v1/parity": {"get": operation("getParityRegistry", "Qt-to-web parity registry")},
        "/api/v1/defaults/{section}": {"get": operation("getGlobalDefaults", "Built-in and configured global defaults")},
        "/api/v1/services/tts": {"get": operation("listTtsServices", "TTS readiness and catalogues")},
        "/api/v1/services/tts/discover": {"post": operation("discoverTtsService", "Discovered endpoint", "TtsEndpointDiscoveryRequest")},
        "/api/v1/services/tts/{serviceId}/preview": {"post": operation("previewTtsVoice", "Voice preview queued", "TtsVoicePreviewRequest", "202")},
        "/api/v1/credentials": {"get": operation("listCredentials", "Write-only auxiliary credential status")},
        "/api/v1/credentials/{credentialId}": {"put": operation("putCredential", "Auxiliary credential saved", "CredentialUpdate")},
        "/api/v1/sessions/{sessionId}/settings/{section}": {
            "get": operation("getSessionSettings", "Effective settings and inheritance"),
            "put": operation("putSessionSettings", "Session override saved", "SessionSettingsUpdate"),
        },
        "/api/v1/sessions/{sessionId}/settings/resolve": {"post": operation("resolveSessionSettings", "Immutable effective settings snapshot")},
        "/api/v1/sessions/{sessionId}/outcome-plan": {
            "get": operation("getOutcomePlan", "Revisioned outcome plan"),
            "put": operation("putOutcomePlan", "Outcome plan saved", "OutcomePlanUpdate"),
        },
        "/api/v1/sources": {"get": operation("listSourceAssets", "Reusable source library")},
        "/api/v1/sources/{sourceAssetId}": {
            "patch": operation("updateSourceAsset", "Source asset updated", "SourceUpdateRequest"),
            "delete": operation("trashSourceAsset", "Source asset moved to trash"),
        },
        "/api/v1/sources/{sourceAssetId}/restore": {"post": operation("restoreSourceAsset", "Source asset restored")},
        "/api/v1/sessions/{sessionId}/sources": {
            "get": operation("listSessionSources", "Session source attachments"),
            "post": operation("attachSessionSource", "Source attached", "SourceAttachRequest", "201"),
        },
        "/api/v1/sessions/{sessionId}/sources/{attachmentId}": {"delete": operation("detachSessionSource", "Source detached", status="204")},
        "/api/v1/sessions/{sessionId}/documents": {"get": operation("listSessionDocuments", "Document and subtitle revisions")},
        "/api/v1/document-revisions/{revisionId}/words": {"get": operation("listTimedWords", "Immutable timed words")},
        "/api/v1/artifacts/{artifactId}/waveform": {"get": operation("getArtifactWaveform", "Waveform peaks or queued generation", status="200")},
        "/api/v1/artifacts/{artifactId}/context": {"get": operation("getArtifactContext", "Artifact lineage context for comparison", status="200")},
        "/api/v1/artifacts/{artifactId}/optimization-review": {"post": operation("saveOptimizationReview", "Reviewed speech optimization artifact", "OptimizationReviewRequest", "201")},
        "/api/v1/sessions/{sessionId}/generation-plan": {"post": operation("createGenerationPlan", "Generation plan created", "GenerationPlanCreate", "201")},
        "/api/v1/sessions/{sessionId}/generation-segments": {"get": operation("listGenerationSegments", "Cursor-paginated generation segments")},
        "/api/v1/generation-segments/{segmentId}": {"patch": operation("updateGenerationSegment", "Generation segment updated", "GenerationSegmentUpdate")},
        "/api/v1/generation-segments/{segmentId}/takes/{takeId}/select": {"post": operation("selectGenerationTake", "Active audio take selected")},
        "/api/v1/sessions/{sessionId}/generation-runs/latest": {"get": operation("getLatestGenerationRun", "Latest generation run")},
        "/api/v1/sessions/{sessionId}/generation-runs": {
            "get": operation("listGenerationRuns", "Named generation runs"),
            "post": operation("startGenerationRun", "Generation queued", "GenerationStartRequest", "202"),
        },
        "/api/v1/generation-runs/{runId}": {"delete": operation("deleteGenerationRun", "Generation run deleted", status="204")},
        "/api/v1/generation-runs/{runId}/pause": {"post": operation("pauseGenerationRun", "Safe pause requested", status="202")},
        "/api/v1/generation-runs/{runId}/resume": {"post": operation("resumeGenerationRun", "Generation resumed", status="202")},
        "/api/v1/generation-runs/{runId}/cancel": {"post": operation("cancelGenerationRun", "Cancellation requested", status="202")},
        "/api/v1/sessions/{sessionId}/output-assemblies/latest": {"get": operation("getLatestOutputAssembly", "Latest output assembly")},
        "/api/v1/sessions/{sessionId}/output-assemblies": {"post": operation("createOutputAssembly", "Output assembly queued", "OutputAssemblyCreateRequest", "202")},
        "/api/v1/sessions/{sessionId}/agent-runs": {
            "get": operation("listAgentRuns", "Agentic cleaning runs"),
            "post": operation("createAgentRun", "Agentic cleaning queued", "AgentRunCreateRequest", "202"),
        },
        "/api/v1/agent-runs/{runId}/steps": {"get": operation("listAgentSteps", "Auditable agent phase summaries")},
        "/api/v1/agent-runs/{runId}/accept": {"post": operation("acceptAgentRun", "Cleaning result accepted")},
        "/api/v1/uploads/init": {"post": operation("initializeChunkUpload", "Chunk upload initialized", "ChunkUploadInitialize", "201")},
        "/api/v1/uploads/{uploadId}": {
            "get": operation("getChunkUpload", "Chunk upload status"),
            "delete": operation("cancelChunkUpload", "Chunk upload canceled", status="204"),
        },
        "/api/v1/uploads/{uploadId}/chunks/{index}": {"put": operation("putUploadChunk", "Chunk accepted")},
        "/api/v1/uploads/{uploadId}/complete": {"post": operation("completeChunkUpload", "Upload promoted", status="201")},
        "/api/v1/sessions/{sessionId}/restore": {"post": operation("restoreSession", "Session restored")},
        "/api/v1/sessions/{sessionId}/reindex": {"post": operation("reindexSession", "Reconciliation report")},
    })
    return document

