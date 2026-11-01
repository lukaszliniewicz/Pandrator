"""HTTP contracts shared by catalog clients and portable agents."""

from .voice_catalog import VoiceCatalogQuery
from .voice_catalog_schemas import (
    CatalogVoiceUpdate,
    VoiceCollectionCreate,
    VoiceCollectionUpdate,
    VoiceCompatibility,
)
from .voice_lifecycle_schemas import (
    VoiceDesignedSamplePreparationRequest,
    VoicePublishRequest,
    VoiceReferenceImportRequest,
)
from .voice_sample_transcription import VoiceTranscriptionChoices

VOICE_CATALOG_SCHEMAS = {
    "VoiceCompatibility": VoiceCompatibility,
    "VoiceTranscriptionChoices": VoiceTranscriptionChoices,
    "VoiceCatalogQuery": VoiceCatalogQuery,
    "VoiceReferenceImportRequest": VoiceReferenceImportRequest,
    "VoiceDesignedSamplePreparationRequest": VoiceDesignedSamplePreparationRequest,
    "VoicePublishRequest": VoicePublishRequest,
    "VoiceCollectionCreate": VoiceCollectionCreate,
    "VoiceCollectionUpdate": VoiceCollectionUpdate,
    "CatalogVoiceUpdate": CatalogVoiceUpdate,
}


def voice_catalog_paths():
    paths = {}
    for path, method, operation, schema, status in [
        ("/api/v1/voice-catalog", "get", "queryVoiceCatalog", None, "200"),
        (
            "/api/v1/voice-catalog/capabilities",
            "get",
            "getVoiceCatalogCapabilities",
            None,
            "200",
        ),
        (
            "/api/v1/voice-catalog/metadata",
            "patch",
            "updateCatalogVoiceMetadata",
            "CatalogVoiceUpdate",
            "200",
        ),
        ("/api/v1/voice-collections", "get", "listVoiceCollections", None, "200"),
        (
            "/api/v1/voice-collections",
            "post",
            "createVoiceCollection",
            "VoiceCollectionCreate",
            "201",
        ),
        (
            "/api/v1/voice-collections/{collectionId}",
            "patch",
            "updateVoiceCollection",
            "VoiceCollectionUpdate",
            "200",
        ),
    ]:
        definition = {
            "operationId": operation,
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": ["app.read"] if method == "get" else ["app.read", "app.write"]},
            ],
            "parameters": [],
            "responses": {
                status: {
                    "description": "Voice catalog result",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        }
                    },
                }
            },
        }
        if schema:
            definition["requestBody"] = {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {"$ref": f"#/components/schemas/{schema}"}
                    }
                },
            }
            definition["parameters"].append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": True,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            )
        if "{collectionId}" in path:
            definition["parameters"].append(
                {
                    "name": "collectionId",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                }
            )
        paths.setdefault(path, {})[method] = definition
    paths["/api/v1/voices/{voiceId}/samples/from-artifact"] = {
        "post": {
            "operationId": "createVoiceSampleFromArtifact",
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": ["app.run"]},
            ],
            "parameters": [
                {
                    "name": "voiceId",
                    "in": "path",
                    "required": True,
                    "schema": {"type": "string"},
                },
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                },
            ],
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "$ref": "#/components/schemas/VoiceReferenceImportRequest"
                        }
                    }
                },
            },
            "responses": {
                "202": {
                    "description": "Voice normalization job",
                    "content": {
                        "application/json": {
                            "schema": {"type": "object", "additionalProperties": True}
                        }
                    },
                }
            },
        }
    }
    paths["/api/v1/voice-catalog"]["get"]["parameters"] = [
        {"name": name, "in": "query", "schema": schema}
        for name, schema in VoiceCatalogQuery.model_json_schema()["properties"].items()
    ]
    paths["/api/v1/voice-catalog/capabilities"]["get"]["parameters"] = [
        {"name": name, "in": "query", "schema": {"type": "string"}}
        for name in ("service_id", "model")
    ]
    for path, method in (
        ("/api/v1/voice-catalog", "get"),
        ("/api/v1/voice-catalog/capabilities", "get"),
        ("/api/v1/voice-collections", "get"),
        ("/api/v1/voice-collections", "post"),
        ("/api/v1/voice-collections/{collectionId}", "patch"),
        ("/api/v1/voice-catalog/metadata", "patch"),
    ):
        definition = paths[path][method]
        definition["description"] = (
            "Uses the caller's personal workspace when no root is supplied. "
            "A project root also requires independent access to its workspace. "
            "Workspace Members can edit library content; connection management is separate."
        )
        names = ("workspace_id", "session_id") if method == "get" else ("workspace_id",)
        for name in names:
            if not any(item["name"] == name for item in definition["parameters"]):
                definition["parameters"].append({
                    "name": name,
                    "in": "query",
                    "required": False,
                    "description": "A nonblank scope identifier without surrounding whitespace.",
                    "schema": {"type": "string", "minLength": 1, "maxLength": 80},
                })
        for response in definition["responses"].values():
            response["content"]["application/json"]["schema"]["properties"] = {
                "workspace_id": {"type": "string"},
                "can_edit": {"type": "boolean"},
            }
        definition["responses"].update({
            "404": {"description": "The workspace, project or collection is absent or unavailable."},
            "422": {"description": "The request or scope identifier is invalid."},
        })
        if method != "get":
            definition["responses"]["409"] = {
                "description": "The revision, captured catalogue or idempotency request conflicts."
            }
    return paths


def extend_voice_lifecycle_paths(paths):
    paths["/api/v1/voices/{voiceId}/samples/from-preview"]["post"]["requestBody"]["content"]["application/json"]["schema"] = {
        "$ref": "#/components/schemas/VoiceDesignedSamplePreparationRequest"
    }
    publication = paths["/api/v1/voices/{voiceId}/providers/{serviceId}"]["post"]
    publication["requestBody"] = {
        "required": False,
        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoicePublishRequest"}}},
    }
    paths["/api/v1/voices/{voiceId}/samples/from-preview"]["post"]["responses"]["200"] = {
        "description": "An identical ready normalized reference was reused"
    }
    for path, method, scope, idempotent in [
        ("/api/v1/voices", "post", "app.write", True),
        ("/api/v1/voices/{voiceId}", "patch", "app.write", False),
        ("/api/v1/voices/{voiceId}/samples", "get", "app.read", False),
        ("/api/v1/voices/{voiceId}/samples/from-preview", "post", "app.run", True),
        (
            "/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe",
            "post",
            "app.run",
            True,
        ),
        (
            "/api/v1/voices/{voiceId}/samples/{sampleId}/transcript",
            "patch",
            "app.write",
            True,
        ),
        ("/api/v1/voices/{voiceId}/providers/{serviceId}", "post", "app.run", True),
        ("/api/v1/voices/{voiceId}/providers/{serviceId}", "delete", "app.run", True),
        ("/api/v1/services/tts/{serviceId}/preview", "post", "app.run", True),
        ("/api/v1/sessions/{sessionId}", "delete", "app.write", False),
        ("/api/v1/sessions/{sessionId}/restore", "post", "app.write", False),
        (
            "/api/v1/sessions/{sessionId}/outputs/{artifactId}",
            "delete",
            "app.write",
            False,
        ),
    ]:
        definition = paths[path][method]
        definition["security"] = [
            {"cookieAuth": []},
            {"bearerToken": []},
            {"nativeOAuth": [scope]},
        ]
        if idempotent:
            definition.setdefault("parameters", []).append(
                {
                    "name": "Idempotency-Key",
                    "in": "header",
                    "required": False,
                    "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                }
            )
    paths["/api/v1/sessions/{sessionId}/restore"]["post"].setdefault(
        "parameters", []
    ).append(
        {
            "name": "If-Match",
            "in": "header",
            "required": True,
            "schema": {"type": "string"},
        }
    )
    paths["/api/v1/sessions"]["get"].setdefault("parameters", []).append(
        {
            "name": "include_trashed",
            "in": "query",
            "schema": {"type": "boolean", "default": False},
        }
    )

    for path, method in (
        ("/api/v1/voices", "post"),
        ("/api/v1/voices/{voiceId}", "patch"),
        ("/api/v1/voices/{voiceId}", "delete"),
        ("/api/v1/voices/{voiceId}/samples", "get"),
        ("/api/v1/voices/{voiceId}/samples/{sampleId}", "delete"),
        ("/api/v1/voices/{voiceId}/samples/{sampleId}/transcribe", "post"),
        ("/api/v1/voices/{voiceId}/samples/{sampleId}/transcript", "patch"),
        ("/api/v1/voices/{voiceId}/samples", "post"),
        ("/api/v1/voices/{voiceId}/samples/from-preview", "post"),
        ("/api/v1/voices/{voiceId}/samples/from-artifact", "post"),
        ("/api/v1/voices/{voiceId}/samples/{sampleId}/replace", "post"),
        ("/api/v1/voices/{voiceId}/providers/{serviceId}", "post"),
        ("/api/v1/voices/{voiceId}/providers/{serviceId}", "delete"),
    ):
        definition = paths[path][method]
        definition.setdefault("parameters", []).append({
            "name": "workspace_id",
            "in": "query",
            "required": False,
            "description": (
                "Targets the caller's personal workspace when omitted."
                if path == "/api/v1/voices"
                else "Optionally constrains the voice's stored workspace; current workspace access is required."
            ),
            "schema": {"type": "string", "minLength": 1, "maxLength": 80},
        })
        scopes = ["app.read"] if method == "get" else ["app.read", "app.write"]
        if path.endswith(("/transcribe", "/from-preview", "/from-artifact", "/replace", "/providers/{serviceId}")) or (
            path == "/api/v1/voices/{voiceId}/samples" and method == "post"
        ):
            scopes.append("app.run")
        if path.endswith("/transcribe"):
            definition["requestBody"] = {
                "required": False,
                "description": "Public transcription choices. Services and executable paths come from the workspace configuration. The original owner's personal workspace retains its legacy settings contract.",
                "content": {"application/json": {"schema": {"$ref": "#/components/schemas/VoiceTranscriptionChoices"}}},
            }
        definition["security"] = [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": scopes}]
        definition["x-required-scopes"] = scopes

    for path, method in [
        ("/api/v1/sessions/{sessionId}/restore", "post"),
        ("/api/v1/sessions/{sessionId}/outputs/{artifactId}", "delete"),
    ]:
        for name in ("sessionId", "artifactId"):
            if "{" + name + "}" in path and not any(
                p.get("name") == name for p in paths[path][method].get("parameters", [])
            ):
                paths[path][method].setdefault("parameters", []).append(
                    {
                        "name": name,
                        "in": "path",
                        "required": True,
                        "schema": {"type": "string"},
                    }
                )
