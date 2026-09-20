"""HTTP contracts shared by catalog clients and portable agents."""

from .voice_catalog import VoiceCatalogQuery
from .voice_catalog_routes import CatalogVoiceUpdate
from .voice_catalog_schemas import VoiceCollectionCreate, VoiceCollectionUpdate
from .voice_lifecycle_schemas import VoiceReferenceImportRequest

VOICE_CATALOG_SCHEMAS = {
    "VoiceCatalogQuery": VoiceCatalogQuery,
    "VoiceReferenceImportRequest": VoiceReferenceImportRequest,
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
                {"nativeOAuth": ["app.read" if method == "get" else "app.write"]},
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
    return paths


def extend_voice_lifecycle_paths(paths):
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
