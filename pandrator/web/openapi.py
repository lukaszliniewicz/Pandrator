"""Small deterministic OpenAPI document for frontend client generation."""

from __future__ import annotations

import re

from .identity import ApplicationIdentityDocument
from .schemas import SCHEMA_MODELS
from .work import EventBounds, WorkError, WorkEvent, WorkEventPage, WorkView


def build_openapi_document() -> dict:
    schemas: dict[str, dict] = {}
    contract_models = {
        **SCHEMA_MODELS,
        "ApplicationIdentityDocument": ApplicationIdentityDocument,
        "EventBounds": EventBounds,
        "WorkError": WorkError,
        "WorkEvent": WorkEvent,
        "WorkEventPage": WorkEventPage,
        "WorkView": WorkView,
    }
    for name, model in contract_models.items():
        schema = model.model_json_schema(ref_template="#/components/schemas/{model}")
        definitions = schema.pop("$defs", {})
        for definition_name, definition in definitions.items():
            existing = schemas.get(definition_name)
            if existing is not None and existing != definition:
                raise ValueError(
                    f"Conflicting OpenAPI schema definition: {definition_name}"
                )
            schemas[definition_name] = definition
        schemas[name] = schema
    document = {
        "openapi": "3.1.0",
        "info": {"title": "Pandrator API", "version": "1.0.0"},
        "servers": [{"url": "/"}],
        "paths": {
            "/api/v1/health": {
                "get": {
                    "operationId": "getHealth",
                    "responses": {"200": {"description": "Healthy"}},
                }
            },
            "/api/v1/system/identity": {
                "get": {
                    "operationId": "getSystemIdentity",
                    "responses": {
                        "200": {
                            "description": "Stable authenticated application identity",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/ApplicationIdentityDocument"
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/capabilities": {
                "get": {
                    "operationId": "getCapabilities",
                    "responses": {"200": {"description": "Runtime capabilities"}},
                }
            },
            "/api/v1/sessions": {
                "get": {
                    "operationId": "listSessions",
                    "responses": {"200": {"description": "Sessions"}},
                },
                "post": {
                    "operationId": "createSession",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SessionCreate"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/v1/sessions/{sessionId}": {
                "get": {
                    "operationId": "getSession",
                    "parameters": [
                        {
                            "name": "sessionId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        }
                    ],
                    "responses": {"200": {"description": "Session"}},
                },
                "patch": {
                    "operationId": "updateSession",
                    "parameters": [
                        {
                            "name": "sessionId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SessionUpdate"}
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Updated"},
                        "409": {"description": "Revision conflict"},
                    },
                },
                "delete": {
                    "operationId": "trashSession",
                    "parameters": [
                        {
                            "name": "sessionId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                    ],
                    "responses": {
                        "200": {"description": "Session moved to trash"},
                        "409": {"description": "Revision conflict"},
                    },
                },
            },
            "/api/v1/sessions/{sessionId}/forks": {
                "post": {
                    "operationId": "forkSession",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SessionForkRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "Independent session fork created"},
                        "422": {"description": "Unsupported checkpoint"},
                    },
                }
            },
            "/api/v1/jobs": {
                "get": {
                    "operationId": "listJobs",
                    "responses": {"200": {"description": "Jobs"}},
                },
                "post": {
                    "operationId": "createJob",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/JobCreate"}
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                },
            },
            "/api/v1/work": {
                "get": {
                    "operationId": "listWork",
                    "responses": {
                        "200": {"description": "Payload-free durable work projections"}
                    },
                }
            },
            "/api/v1/work/{jobId}": {
                "get": {
                    "operationId": "getWork",
                    "responses": {
                        "200": {
                            "description": "Payload-free durable work projection",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/WorkView"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/work/{jobId}/events": {
                "get": {
                    "operationId": "listWorkEvents",
                    "responses": {
                        "200": {
                            "description": "Bounded redacted work events",
                            "content": {
                                "application/json": {
                                    "schema": {
                                        "$ref": "#/components/schemas/WorkEventPage"
                                    }
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/work/{jobId}/cancel": {
                "post": {
                    "operationId": "cancelWork",
                    "parameters": [
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 200,
                            },
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Cancellation requested",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/WorkView"}
                                }
                            },
                        }
                    },
                }
            },
            "/api/v1/events": {
                "get": {
                    "operationId": "streamEvents",
                    "responses": {"200": {"description": "SSE job events"}},
                }
            },
            "/api/v1/events/snapshot": {
                "get": {
                    "operationId": "getEventSnapshot",
                    "responses": {
                        "200": {
                            "description": "Initial event-stream resource snapshot and cursor"
                        }
                    },
                }
            },
            "/api/v1/auth/status": {
                "get": {
                    "operationId": "getAuthStatus",
                    "responses": {"200": {"description": "Authentication status"}},
                }
            },
            "/api/v1/auth/bootstrap": {
                "post": {
                    "operationId": "exchangeBootstrapToken",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/BootstrapRequest"
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Authenticated"}},
                }
            },
            "/api/v1/auth/manager-browser-bootstrap": {
                "post": {
                    "operationId": "createManagerBrowserBootstrapGrant",
                    "responses": {
                        "200": {
                            "description": "Full-authority one-use browser bootstrap grant"
                        }
                    },
                }
            },
            "/api/v1/auth/manager-bootstrap": {
                "post": {
                    "operationId": "createManagerBootstrapGrant",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ManagerBootstrapRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Scoped one-use automation bootstrap grant"
                        }
                    },
                }
            },
            "/api/v1/auth/login": {
                "post": {
                    "operationId": "login",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/LoginRequest"}
                            }
                        },
                    },
                    "responses": {"200": {"description": "Authenticated"}},
                }
            },
            "/api/v1/auth/logout": {
                "post": {
                    "operationId": "logout",
                    "responses": {"204": {"description": "Signed out"}},
                }
            },
            "/api/v1/auth/tokens": {
                "get": {
                    "operationId": "listApiTokens",
                    "responses": {"200": {"description": "Tokens"}},
                },
                "post": {
                    "operationId": "createApiToken",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/TokenCreateRequest"
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/v1/auth/automation/authorize": {
                "get": {
                    "operationId": "authorizeAutomationClient",
                    "description": "Trusted owner consent with exact redirect matching and mandatory S256 PKCE.",
                    "responses": {
                        "200": {"description": "Consent page"},
                        "302": {"description": "Authorization response"},
                    },
                }
            },
            "/api/v1/auth/automation/token": {
                "post": {
                    "operationId": "exchangeAutomationCode",
                    "responses": {
                        "200": {"description": "Bound automation credential"}
                    },
                }
            },
            "/api/v1/auth/automation-clients": {
                "get": {
                    "operationId": "listAutomationClients",
                    "responses": {"200": {"description": "Automation clients"}},
                },
                "post": {
                    "operationId": "registerAutomationClient",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/AutomationClientCreateRequest"
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Registered"}},
                },
            },
            "/api/v1/auth/automation-clients/{clientId}": {
                "delete": {
                    "operationId": "revokeAutomationClient",
                    "responses": {"204": {"description": "Revoked"}},
                }
            },
            "/api/v1/audit/events": {
                "get": {
                    "operationId": "listAuditEvents",
                    "responses": {
                        "200": {"description": "Bounded content-free audit events"}
                    },
                }
            },
            "/api/v1/settings/{settingKey}": {
                "get": {
                    "operationId": "getSetting",
                    "responses": {"200": {"description": "Setting"}},
                },
                "put": {
                    "operationId": "putSetting",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SettingUpdate"}
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Saved"},
                        "409": {"description": "Revision conflict"},
                    },
                },
            },
            "/api/v1/uploads": {
                "post": {
                    "operationId": "uploadSource",
                    "responses": {"201": {"description": "Uploaded"}},
                }
            },
            "/api/v1/sessions/{sessionId}/sources/url": {
                "post": {
                    "operationId": "downloadSourceUrl",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SourceUrlRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/sessions/{sessionId}/sources/reuse": {
                "post": {
                    "operationId": "reuseSource",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SourceReuseRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/artifacts": {
                "get": {
                    "operationId": "listArtifacts",
                    "responses": {"200": {"description": "Artifacts"}},
                }
            },
            "/api/v1/sessions/{sessionId}/outputs/{artifactId}": {
                "delete": {
                    "operationId": "deleteOutputArtifact",
                    "responses": {
                        "200": {"description": "Export removed"},
                        "409": {"description": "Artifact is not a removable export"},
                    },
                }
            },
            "/api/v1/artifacts/{artifactId}/content": {
                "get": {
                    "operationId": "getArtifactContent",
                    "responses": {
                        "200": {"description": "Range-capable artifact content"}
                    },
                }
            },
            "/api/v1/artifacts/{artifactId}/pdf": {
                "get": {
                    "operationId": "inspectPdf",
                    "responses": {"200": {"description": "PDF geometry"}},
                }
            },
            "/api/v1/sessions/{sessionId}/workflow": {
                "get": {
                    "operationId": "getWorkflow",
                    "responses": {"200": {"description": "Workflow snapshot"}},
                }
            },
            "/api/v1/sessions/{sessionId}/workflow-plans": {
                "post": {
                    "operationId": "createWorkflowPlan",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/WorkflowPlanCreateRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "Immutable workflow execution preview"}
                    },
                }
            },
            "/api/v1/workflow-plans/{planId}": {
                "get": {
                    "operationId": "getWorkflowPlan",
                    "responses": {
                        "200": {"description": "Immutable workflow execution preview"}
                    },
                }
            },
            "/api/v1/workflow-plans/{planId}/execute": {
                "post": {
                    "operationId": "executeWorkflowPlan",
                    "parameters": [
                        {
                            "name": "Idempotency-Key",
                            "in": "header",
                            "required": True,
                            "schema": {
                                "type": "string",
                                "minLength": 8,
                                "maxLength": 200,
                            },
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/WorkflowPlanExecuteRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "202": {"description": "Exact plan consumed and queued"},
                        "409": {
                            "description": "Plan stale, consumed, expired, or confirmation missing"
                        },
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/run": {
                "post": {
                    "operationId": "runWorkflowStage",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": True,
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/artifacts": {
                "get": {
                    "operationId": "listStageArtifacts",
                    "parameters": [
                        {
                            "name": "sessionId",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string", "format": "uuid"},
                        },
                        {
                            "name": "stageKey",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        },
                        {
                            "name": "limit",
                            "in": "query",
                            "required": False,
                            "schema": {
                                "type": "integer",
                                "minimum": 1,
                                "maximum": 100,
                                "default": 50,
                            },
                        },
                        {
                            "name": "before_version",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "integer", "minimum": 1},
                            "description": (
                                "Return versions older than this exclusive "
                                "version cursor."
                            ),
                        },
                    ],
                    "responses": {
                        "200": {"description": "Stage artifact history page"}
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/impact": {
                "get": {
                    "operationId": "getStageRerunImpact",
                    "responses": {"200": {"description": "Rerun lineage impact"}},
                }
            },
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/settings-mismatches": {
                "get": {
                    "operationId": "getStageSettingsMismatches",
                    "responses": {
                        "200": {
                            "description": "Prerequisite settings changed since the stored artifacts were created"
                        }
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/stages/{stageKey}/selection": {
                "put": {
                    "operationId": "selectStageArtifact",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/StageSelectionUpdate"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Stage selection updated"},
                        "409": {"description": "Revision conflict"},
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/subtitles": {
                "get": {
                    "operationId": "getSubtitleComparison",
                    "responses": {"200": {"description": "Aligned subtitle revisions"}},
                }
            },
            "/api/v1/sessions/{sessionId}/subtitles/{stage}/review": {
                "post": {
                    "operationId": "saveSubtitleReview",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/SubtitleReviewRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "201": {"description": "Reviewed revision"},
                        "409": {"description": "Revision conflict"},
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/subtitles/catalog": {
                "get": {
                    "operationId": "listSubtitleReviewArtifacts",
                    "responses": {
                        "200": {"description": "Reviewable subtitle artifact catalog"}
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/subtitles/review": {
                "get": {
                    "operationId": "getExactSubtitleReview",
                    "parameters": [
                        {
                            "name": "artifact_id",
                            "in": "query",
                            "required": True,
                            "schema": {
                                "type": "array",
                                "items": {"type": "string"},
                                "minItems": 1,
                                "maxItems": 4,
                            },
                            "style": "form",
                            "explode": True,
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Exact immutable subtitle revisions aligned for review"
                        },
                        "422": {"description": "Invalid artifact selection"},
                    },
                }
            },
            "/api/v1/sessions/{sessionId}/pdf/apply": {
                "post": {
                    "operationId": "applyPdfEdits",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/PdfEditRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/sessions/{sessionId}/bundle": {
                "post": {
                    "operationId": "exportSessionBundle",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/BundleExportRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/session-bundles/import": {
                "post": {
                    "operationId": "importSessionBundle",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/BundleImportRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/providers": {
                "get": {
                    "operationId": "listProviders",
                    "responses": {"200": {"description": "Providers"}},
                },
                "post": {
                    "operationId": "createProvider",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ProviderCreate"
                                }
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/v1/providers/profiles": {
                "get": {
                    "operationId": "listProviderProfiles",
                    "responses": {"200": {"description": "LiteLLM provider profiles"}},
                }
            },
            "/api/v1/providers/{providerId}": {
                "patch": {
                    "operationId": "updateProvider",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ProviderUpdate"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Updated"},
                        "409": {"description": "Revision conflict"},
                    },
                },
                "delete": {
                    "operationId": "deleteProvider",
                    "responses": {
                        "204": {"description": "Deleted"},
                        "409": {"description": "Replacement required"},
                    },
                },
            },
            "/api/v1/providers/{providerId}/models": {
                "get": {
                    "operationId": "listProviderModels",
                    "responses": {"200": {"description": "Models"}},
                },
                "post": {
                    "operationId": "createProviderModel",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ModelCreate"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/v1/providers/{providerId}/models/refresh": {
                "post": {
                    "operationId": "refreshProviderModels",
                    "responses": {"200": {"description": "Provider models discovered"}},
                }
            },
            "/api/v1/providers/{providerId}/test": {
                "post": {
                    "operationId": "testProvider",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ProviderTestRequest"
                                }
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Provider ready"},
                        "422": {"description": "Provider test failed"},
                    },
                }
            },
            "/api/v1/providers/{providerId}/models/{modelId}": {
                "patch": {
                    "operationId": "updateProviderModel",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/ModelUpdate"}
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Updated"},
                        "409": {"description": "Revision conflict"},
                    },
                },
                "delete": {
                    "operationId": "deleteProviderModel",
                    "responses": {
                        "204": {"description": "Deleted"},
                        "409": {"description": "Replacement required"},
                    },
                },
            },
            "/api/v1/voices": {
                "get": {
                    "operationId": "listVoices",
                    "responses": {"200": {"description": "Voices"}},
                },
                "post": {
                    "operationId": "createVoice",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/VoiceCreate"}
                            }
                        },
                    },
                    "responses": {"201": {"description": "Created"}},
                },
            },
            "/api/v1/voices/{voiceId}": {
                "patch": {
                    "operationId": "updateVoice",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/VoiceUpdate"}
                            }
                        },
                    },
                    "responses": {
                        "200": {"description": "Updated"},
                        "409": {"description": "Revision conflict"},
                    },
                },
                "delete": {
                    "operationId": "deleteVoice",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "204": {"description": "Deleted"},
                        "409": {"description": "Revision conflict or bundled voice"},
                    },
                },
            },
            "/api/v1/voices/{voiceId}/samples": {
                "get": {
                    "operationId": "listVoiceSamples",
                    "responses": {"200": {"description": "Samples"}},
                },
                "post": {
                    "operationId": "uploadVoiceSample",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "202": {"description": "Queued"},
                        "409": {"description": "Revision conflict"},
                    },
                },
            },
            "/api/v1/voices/{voiceId}/samples/{sampleId}": {
                "delete": {
                    "operationId": "deleteVoiceSample",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {"description": "Deleted"},
                        "409": {"description": "Revision conflict or bundled voice"},
                    },
                }
            },
            "/api/v1/voices/{voiceId}/samples/{sampleId}/replace": {
                "post": {
                    "operationId": "replaceVoiceSample",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "202": {"description": "Replacement queued"},
                        "409": {"description": "Revision conflict or bundled voice"},
                    },
                }
            },
            "/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe": {
                "post": {
                    "operationId": "transcribeVoiceSample",
                    "responses": {"202": {"description": "Transcription queued"}},
                }
            },
            "/api/v1/voices/{voiceId}/samples/{sampleId}/transcript": {
                "patch": {
                    "operationId": "reviewVoiceSampleTranscript",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/VoiceTranscriptReview"
                                }
                            }
                        },
                    },
                    "responses": {"200": {"description": "Transcript reviewed"}},
                }
            },
            "/api/v1/voices/{voiceId}/providers/{serviceId}": {
                "post": {
                    "operationId": "publishVoiceToProvider",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {"202": {"description": "Provider upload queued"}},
                },
                "delete": {
                    "operationId": "removeVoiceFromProvider",
                    "parameters": [
                        {
                            "name": "If-Match",
                            "in": "header",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "202": {"description": "Provider deletion queued"},
                        "409": {
                            "description": "Revision conflict or unowned legacy registration"
                        },
                        "422": {"description": "Provider deletion unsupported"},
                    },
                },
            },
            "/api/v1/rvc/models": {
                "get": {
                    "operationId": "listRvcModels",
                    "responses": {"200": {"description": "RVC readiness and models"}},
                },
                "post": {
                    "operationId": "uploadRvcModel",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/RvcModelUploadRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                },
            },
            "/api/v1/rvc/convert": {
                "post": {
                    "operationId": "convertWithRvc",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/RvcConvertRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                }
            },
            "/api/v1/training": {
                "get": {
                    "operationId": "listTrainingRuns",
                    "responses": {"200": {"description": "Training runs"}},
                },
                "post": {
                    "operationId": "createTrainingRun",
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/TrainingCreateRequest"
                                }
                            }
                        },
                    },
                    "responses": {"202": {"description": "Queued"}},
                },
            },
            "/api/v1/training/{trainingId}/cancel": {
                "post": {
                    "operationId": "cancelTrainingRun",
                    "responses": {"202": {"description": "Cancellation requested"}},
                }
            },
            "/api/v1/training/{trainingId}/retry": {
                "post": {
                    "operationId": "retryTrainingRun",
                    "responses": {"202": {"description": "Retry queued"}},
                }
            },
            "/api/v1/jobs/{jobId}": {
                "get": {
                    "operationId": "getJob",
                    "responses": {"200": {"description": "Job"}},
                }
            },
            "/api/v1/jobs/{jobId}/logs": {
                "get": {
                    "operationId": "getJobLogs",
                    "responses": {
                        "200": {"description": "Durable job event and log timeline"}
                    },
                }
            },
            "/api/v1/jobs/{jobId}/cancel": {
                "post": {
                    "operationId": "cancelJob",
                    "responses": {"200": {"description": "Cancellation requested"}},
                }
            },
        },
        "components": {
            "schemas": schemas,
            "securitySchemes": {
                "bearerToken": {"type": "http", "scheme": "bearer"},
                "cookieAuth": {"type": "apiKey", "in": "cookie", "name": "session"},
                "nativeOAuth": {
                    "type": "oauth2",
                    "flows": {
                        "authorizationCode": {
                            "authorizationUrl": "/api/v1/auth/automation/authorize",
                            "tokenUrl": "/api/v1/auth/automation/token",
                            "scopes": {
                                "app.read": "Read application state.",
                                "app.write": "Change reversible application state.",
                                "app.run": "Start durable application work.",
                                "app.cancel": "Cancel durable application work.",
                                "app.credentials.read": "Inspect credential status.",
                                "app.credentials.write": "Change credential references.",
                                "manager.read": "Inspect Manager state and plans.",
                                "manager.runtime": "Control managed runtime services.",
                                "manager.mutate": "Execute approved Manager plans.",
                            },
                        }
                    },
                },
            },
        },
    }
    paths = document["paths"]

    def operation(
        operation_id: str,
        description: str,
        schema: str | None = None,
        status: str = "200",
    ) -> dict:
        value = {
            "operationId": operation_id,
            "responses": {status: {"description": description}},
        }
        if schema:
            value["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema}"}
                    }
                },
            }
        return value

    # Parity-workspace operations are declared alongside their Pydantic DTOs;
    # this block is also the source used to generate the checked-in TS client.
    paths.update(
        {
            "/api/v1/parity": {
                "get": operation("getParityRegistry", "Qt-to-web parity registry")
            },
            "/api/v1/defaults/{section}": {
                "get": operation(
                    "getGlobalDefaults", "Built-in and configured global defaults"
                )
            },
            "/api/v1/services/tts": {
                "get": operation("listTtsServices", "TTS readiness and catalogues")
            },
            "/api/v1/services/tts/discover": {
                "post": operation(
                    "discoverTtsService",
                    "Discovered endpoint",
                    "TtsEndpointDiscoveryRequest",
                )
            },
            "/api/v1/services/tts/{serviceId}/preview": {
                "post": operation(
                    "previewTtsVoice",
                    "Voice preview queued",
                    "TtsVoicePreviewRequest",
                    "202",
                )
            },
            "/api/v1/manager/status": {
                "get": operation(
                    "getManagerStatus", "Local manager availability and status"
                )
            },
            "/api/v1/manager/components": {
                "get": operation("listManagerComponents", "Manager component inventory")
            },
            "/api/v1/manager/doctor": {
                "get": operation(
                    "getManagerDoctorReport", "Non-mutating host diagnostics"
                )
            },
            "/api/v1/manager/legacy": {
                "get": operation(
                    "getManagerLegacyImportReport",
                    "Read-only legacy workspace inspection",
                )
            },
            "/api/v1/manager/legacy/import": {
                "post": operation(
                    "importManagerLegacyWorkspace",
                    "Import the exact reviewed legacy workspace",
                    "ManagerLegacyImportRequest",
                )
            },
            "/api/v1/manager/services": {
                "get": operation("listManagerServices", "Manager service inventory")
            },
            "/api/v1/manager/releases": {
                "get": operation(
                    "listManagerReleases",
                    "Accepted product releases and activation slots",
                )
            },
            "/api/v1/manager/releases/plans": {
                "post": operation(
                    "createManagerReleasePlan",
                    "Immutable signed product release plan",
                    "ManagerReleasePlanRequest",
                    "201",
                )
            },
            "/api/v1/manager/uninstall/plans": {
                "post": operation(
                    "createManagerUninstallPlan",
                    "Immutable whole-product uninstall plan",
                    "ManagerUninstallPlanRequest",
                    "201",
                )
            },
            "/api/v1/manager/plans": {
                "post": operation(
                    "createManagerPlan",
                    "Immutable manager plan",
                    "ManagerPlanRequest",
                    "201",
                )
            },
            "/api/v1/manager/operations": {
                "get": operation("listManagerOperations", "Durable manager operations"),
                "post": operation(
                    "submitManagerOperation",
                    "Manager operation accepted",
                    "ManagerOperationRequest",
                    "202",
                ),
            },
            "/api/v1/manager/operations/{operationId}": {
                "get": operation("getManagerOperation", "Durable manager operation")
            },
            "/api/v1/manager/operations/{operationId}/tasks": {
                "get": operation(
                    "listManagerOperationTasks", "Durable manager operation tasks"
                )
            },
            "/api/v1/manager/operations/{operationId}/cancel": {
                "post": operation(
                    "cancelManagerOperation", "Safe cancellation requested"
                )
            },
            "/api/v1/manager/runtime/{action}": {
                "post": operation(
                    "controlManagerRuntime",
                    "Managed runtime action completed",
                    "ManagerRuntimeRequest",
                )
            },
            "/api/v1/manager/logs": {
                "get": operation("getManagerLogs", "Bounded managed-service log tail")
            },
            "/api/v1/credential-backends": {
                "get": operation(
                    "listCredentialBackends",
                    "Credential storage capabilities and guidance",
                )
            },
            "/api/v1/credentials": {
                "get": operation(
                    "listCredentials", "Write-only auxiliary credential status"
                )
            },
            "/api/v1/credentials/{credentialId}": {
                "put": operation(
                    "putCredential", "Auxiliary credential saved", "CredentialUpdate"
                )
            },
            "/api/v1/pronunciations": {
                "get": operation(
                    "listPronunciations", "Reviewable pronunciation library"
                ),
                "post": operation(
                    "createPronunciation",
                    "Pronunciation created",
                    "PronunciationCreate",
                    "201",
                ),
            },
            "/api/v1/pronunciations/{entryId}": {
                "patch": operation(
                    "updatePronunciation",
                    "Pronunciation updated",
                    "PronunciationUpdate",
                ),
                "delete": operation(
                    "deletePronunciation", "Pronunciation deleted", status="204"
                ),
            },
            "/api/v1/sessions/{sessionId}/settings/{section}": {
                "get": operation(
                    "getSessionSettings", "Effective settings and inheritance"
                ),
                "put": operation(
                    "putSessionSettings",
                    "Session override saved",
                    "SessionSettingsUpdate",
                ),
            },
            "/api/v1/sessions/{sessionId}/settings/resolve": {
                "post": operation(
                    "resolveSessionSettings", "Immutable effective settings snapshot"
                )
            },
            "/api/v1/sessions/{sessionId}/outcome-plan": {
                "get": operation("getOutcomePlan", "Revisioned outcome plan"),
                "put": operation(
                    "putOutcomePlan", "Outcome plan saved", "OutcomePlanUpdate"
                ),
            },
            "/api/v1/sources": {
                "get": operation("listSourceAssets", "Reusable source library")
            },
            "/api/v1/sources/{sourceAssetId}": {
                "patch": operation(
                    "updateSourceAsset", "Source asset updated", "SourceUpdateRequest"
                ),
                "delete": operation("trashSourceAsset", "Source asset moved to trash"),
            },
            "/api/v1/sources/{sourceAssetId}/restore": {
                "post": operation("restoreSourceAsset", "Source asset restored")
            },
            "/api/v1/sessions/{sessionId}/sources": {
                "get": operation("listSessionSources", "Session source attachments"),
                "post": operation(
                    "attachSessionSource",
                    "Source attached",
                    "SourceAttachRequest",
                    "201",
                ),
            },
            "/api/v1/sessions/{sessionId}/sources/{attachmentId}": {
                "delete": operation(
                    "detachSessionSource", "Source detached", status="204"
                )
            },
            "/api/v1/sessions/{sessionId}/documents": {
                "get": operation(
                    "listSessionDocuments", "Document and subtitle revisions"
                )
            },
            "/api/v1/document-revisions/{revisionId}/words": {
                "get": operation("listTimedWords", "Immutable timed words")
            },
            "/api/v1/artifacts/{artifactId}/waveform": {
                "get": operation(
                    "getArtifactWaveform",
                    "Waveform peaks or queued generation",
                    status="200",
                )
            },
            "/api/v1/artifacts/{artifactId}/context": {
                "get": operation(
                    "getArtifactContext",
                    "Artifact lineage context for comparison",
                    status="200",
                )
            },
            "/api/v1/artifacts/{artifactId}/optimization-review": {
                "post": operation(
                    "saveOptimizationReview",
                    "Reviewed speech optimization artifact",
                    "OptimizationReviewRequest",
                    "201",
                )
            },
            "/api/v1/sessions/{sessionId}/generation-plan": {
                "post": operation(
                    "createGenerationPlan",
                    "Generation plan created",
                    "GenerationPlanCreate",
                    "201",
                )
            },
            "/api/v1/sessions/{sessionId}/generation-segments": {
                "get": {
                    **operation(
                        "listGenerationSegments",
                        "Cursor-paginated generation segments",
                    ),
                    "parameters": [
                        {
                            "name": "generation_run_id",
                            "in": "query",
                            "required": False,
                            "schema": {"type": "string", "format": "uuid"},
                            "description": "Return the plan revision bound to this generation run.",
                        }
                    ],
                },
                "patch": operation(
                    "updateGenerationSegments",
                    "Generation segments updated atomically",
                    "GenerationSegmentBatchUpdate",
                ),
            },
            "/api/v1/generation-segments/{segmentId}": {
                "patch": operation(
                    "updateGenerationSegment",
                    "Generation segment updated",
                    "GenerationSegmentUpdate",
                )
            },
            "/api/v1/generation-segments/{segmentId}/takes/{takeId}/select": {
                "post": operation("selectGenerationTake", "Active audio take selected")
            },
            "/api/v1/sessions/{sessionId}/generation-runs/latest": {
                "get": operation("getLatestGenerationRun", "Latest generation run")
            },
            "/api/v1/sessions/{sessionId}/generation-runs": {
                "get": operation("listGenerationRuns", "Named generation runs"),
                "post": operation(
                    "startGenerationRun",
                    "Generation queued",
                    "GenerationStartRequest",
                    "202",
                ),
            },
            "/api/v1/generation-runs/{runId}": {
                "delete": operation(
                    "deleteGenerationRun", "Generation run deleted", status="204"
                )
            },
            "/api/v1/generation-runs/{runId}/pause": {
                "post": operation(
                    "pauseGenerationRun", "Safe pause requested", status="202"
                )
            },
            "/api/v1/generation-runs/{runId}/resume": {
                "post": operation(
                    "resumeGenerationRun", "Generation resumed", status="202"
                )
            },
            "/api/v1/generation-runs/{runId}/cancel": {
                "post": operation(
                    "cancelGenerationRun", "Cancellation requested", status="202"
                )
            },
            "/api/v1/sessions/{sessionId}/output-assemblies/latest": {
                "get": operation("getLatestOutputAssembly", "Latest output assembly")
            },
            "/api/v1/sessions/{sessionId}/output-assemblies": {
                "post": operation(
                    "createOutputAssembly",
                    "Output assembly queued",
                    "OutputAssemblyCreateRequest",
                    "202",
                )
            },
            "/api/v1/sessions/{sessionId}/output-mix-preview": {
                "post": operation(
                    "createOutputMixPreview",
                    "Soundtrack mix preview queued",
                    "OutputMixPreviewRequest",
                    "202",
                )
            },
            "/api/v1/sessions/{sessionId}/agent-runs": {
                "get": operation("listAgentRuns", "Auditable agent runs"),
                "post": operation(
                    "createAgentRun",
                    "Agentic cleaning queued",
                    "AgentRunCreateRequest",
                    "202",
                ),
            },
            "/api/v1/agent-runs/{runId}/steps": {
                "get": operation("listAgentSteps", "Auditable agent phase summaries")
            },
            "/api/v1/agent-runs/{runId}/resume": {
                "post": operation(
                    "resumeAgentRun",
                    "Resume an interrupted agentic operation from its durable checkpoint",
                    status="202",
                )
            },
            "/api/v1/agent-runs/{runId}/accept": {
                "post": operation("acceptAgentRun", "Cleaning result accepted")
            },
            "/api/v1/uploads/init": {
                "post": operation(
                    "initializeChunkUpload",
                    "Chunk upload initialized",
                    "ChunkUploadInitialize",
                    "201",
                )
            },
            "/api/v1/uploads/{uploadId}": {
                "get": operation("getChunkUpload", "Chunk upload status"),
                "delete": operation(
                    "cancelChunkUpload", "Chunk upload canceled", status="204"
                ),
            },
            "/api/v1/uploads/{uploadId}/chunks/{index}": {
                "put": operation("putUploadChunk", "Chunk accepted")
            },
            "/api/v1/uploads/{uploadId}/complete": {
                "post": operation(
                    "completeChunkUpload", "Upload promoted", status="201"
                )
            },
            "/api/v1/sessions/{sessionId}/restore": {
                "post": operation("restoreSession", "Session restored")
            },
            "/api/v1/sessions/{sessionId}/reindex": {
                "post": operation("reindexSession", "Reconciliation report")
            },
        }
    )

    idempotency_parameter = {
        "name": "Idempotency-Key",
        "in": "header",
        "required": True,
        "schema": {
            "type": "string",
            "minLength": 8,
            "maxLength": 200,
            "pattern": "^[A-Za-z0-9][A-Za-z0-9._:-]{7,199}$",
        },
    }
    revision_parameter = {
        "name": "If-Match",
        "in": "header",
        "required": True,
        "schema": {"type": "string"},
    }
    for path, method in (
        ("/api/v1/sessions", "post"),
        ("/api/v1/sessions/{sessionId}/forks", "post"),
        ("/api/v1/sessions/{sessionId}", "patch"),
        (
            "/api/v1/sessions/{sessionId}/settings/{section}",
            "put",
        ),
        ("/api/v1/sessions/{sessionId}/sources", "post"),
    ):
        parameters = paths[path][method].setdefault("parameters", [])
        if not any(item.get("name") == "Idempotency-Key" for item in parameters):
            parameters.append(dict(idempotency_parameter))
    for path, method in (
        (
            "/api/v1/sessions/{sessionId}/settings/{section}",
            "put",
        ),
        ("/api/v1/sessions/{sessionId}/sources", "post"),
    ):
        parameters = paths[path][method].setdefault("parameters", [])
        if not any(item.get("name") == "If-Match" for item in parameters):
            parameters.append(dict(revision_parameter))

    mcp_scoped_operations = (
        ("/api/v1/system/identity", "get", "app.read"),
        ("/api/v1/capabilities", "get", "app.read"),
        ("/api/v1/sessions", "get", "app.read"),
        ("/api/v1/sessions", "post", "app.write"),
        ("/api/v1/sessions/{sessionId}", "get", "app.read"),
        ("/api/v1/sessions/{sessionId}", "patch", "app.write"),
        ("/api/v1/sessions/{sessionId}/forks", "post", "app.write"),
        (
            "/api/v1/sessions/{sessionId}/workflow",
            "get",
            "app.read",
        ),
        (
            "/api/v1/sessions/{sessionId}/workflow-plans",
            "post",
            "app.read",
        ),
        (
            "/api/v1/workflow-plans/{planId}",
            "get",
            "app.read",
        ),
        (
            "/api/v1/workflow-plans/{planId}/execute",
            "post",
            "app.run",
        ),
        ("/api/v1/artifacts", "get", "app.read"),
        ("/api/v1/providers", "get", "app.read"),
        ("/api/v1/voices", "get", "app.read"),
        ("/api/v1/work", "get", "app.read"),
        ("/api/v1/work/{jobId}", "get", "app.read"),
        ("/api/v1/work/{jobId}/events", "get", "app.read"),
        ("/api/v1/work/{jobId}/cancel", "post", "app.cancel"),
        (
            "/api/v1/sessions/{sessionId}/settings/{section}",
            "get",
            "app.read",
        ),
        (
            "/api/v1/sessions/{sessionId}/settings/{section}",
            "put",
            "app.write",
        ),
        ("/api/v1/sources", "get", "app.read"),
        (
            "/api/v1/sessions/{sessionId}/sources",
            "post",
            "app.write",
        ),
        ("/api/v1/manager/status", "get", "manager.read"),
        ("/api/v1/manager/components", "get", "manager.read"),
        ("/api/v1/manager/doctor", "get", "manager.read"),
        ("/api/v1/manager/services", "get", "manager.read"),
        ("/api/v1/manager/releases", "get", "manager.read"),
        ("/api/v1/manager/plans", "post", "manager.read"),
        (
            "/api/v1/manager/operations",
            "post",
            "manager.mutate",
        ),
        (
            "/api/v1/manager/operations/{operationId}",
            "get",
            "manager.read",
        ),
        (
            "/api/v1/manager/operations/{operationId}/tasks",
            "get",
            "manager.read",
        ),
        (
            "/api/v1/manager/operations/{operationId}/cancel",
            "post",
            "manager.mutate",
        ),
        (
            "/api/v1/manager/runtime/{action}",
            "post",
            "manager.runtime",
        ),
    )
    for path, method, scope in mcp_scoped_operations:
        paths[path][method]["security"] = [
            {"cookieAuth": []},
            {"bearerToken": []},
            {"nativeOAuth": [scope]},
        ]

    # Every templated route must expose its parameters to generated clients.
    # Most operations use the same UUID-like string identifiers, while chunk
    # indices are the one numeric route component.
    for path, path_item in paths.items():
        parameter_names = re.findall(r"{([^}]+)}", path)
        if not parameter_names:
            continue
        for method, definition in path_item.items():
            if method not in {"get", "put", "post", "delete", "patch"}:
                continue
            parameters = definition.setdefault("parameters", [])
            documented = {
                parameter.get("name")
                for parameter in parameters
                if parameter.get("in") == "path"
            }
            for name in parameter_names:
                if name in documented:
                    continue
                schema = (
                    {"type": "integer", "minimum": 0}
                    if name == "index"
                    else {"type": "string"}
                )
                parameters.append(
                    {
                        "name": name,
                        "in": "path",
                        "required": True,
                        "schema": schema,
                    }
                )
    return document
