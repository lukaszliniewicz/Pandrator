"""OpenAPI fragments for sound assets and independently versioned cues."""

from __future__ import annotations

from .sound_job_schemas import SoundMixRequest, SoundResolveRequest
from .sound_project_schemas import (
    SoundAnchor,
    SoundCueCreate,
    SoundCueUpdate,
    SoundPlanRestore,
    SoundPlayback,
)
from .sound_schemas import SoundGenerate, SoundSave, SoundUpdate

SOUND_SCHEMAS = {
    model.__name__: model
    for model in (
        SoundGenerate,
        SoundSave,
        SoundUpdate,
        SoundAnchor,
        SoundPlayback,
        SoundCueCreate,
        SoundCueUpdate,
        SoundPlanRestore,
        SoundMixRequest,
        SoundResolveRequest,
    )
}


def _parameter(
    name: str, location: str = "path", *, required: bool = True, schema: dict | None = None
) -> dict:
    return {
        "name": name,
        "in": location,
        "required": required,
        "schema": schema or {"type": "string"},
    }


def _operation(
    operation_id: str,
    *,
    scope: str = "app.read",
    schema: str | None = None,
    status: str = "200",
    parameters: list[dict] | None = None,
    write: bool = False,
) -> dict:
    params = list(parameters or [])
    if write:
        params.append(
            _parameter(
                "Idempotency-Key",
                "header",
                schema={"type": "string", "minLength": 8, "maxLength": 200},
            )
        )
    operation = {
        "operationId": operation_id,
        "security": [{"cookieAuth": []}, {"bearerToken": []}, {"nativeOAuth": [scope]}],
        "parameters": params,
        "responses": {
            status: {
                "description": "Sound-domain result.",
                "content": {"application/json": {"schema": {"type": "object"}}},
            },
            "401": {"description": "Authentication required."},
            "403": {"description": "Required scope is missing."},
            "404": {"description": "Sound or project was not found."},
            "409": {"description": "Revision or idempotency conflict."},
            "422": {"description": "Invalid request or unavailable placement/runtime."},
        },
    }
    if schema:
        operation["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": f"#/components/schemas/{schema}"}}},
        }
    return operation


def sound_paths() -> dict:
    page = [
        _parameter("cursor", "query", required=False),
        _parameter(
            "limit",
            "query",
            required=False,
            schema={"type": "integer", "minimum": 1, "maximum": 100},
        ),
    ]
    item = [_parameter("itemId")]
    candidate = [_parameter("candidateId")]
    project = [_parameter("sessionId")]
    cue = [*project, _parameter("cueId")]
    paths = {
        "/api/v1/sounds/catalogue": {"get": _operation("soundCatalogue")},
        "/api/v1/sounds": {
            "get": _operation(
                "listSounds",
                parameters=[
                    *page,
                    _parameter(
                        "query",
                        "query",
                        required=False,
                        schema={"type": "string", "maxLength": 200},
                    ),
                    _parameter(
                        "tag", "query", required=False, schema={"type": "string", "maxLength": 64}
                    ),
                    _parameter(
                        "archived",
                        "query",
                        required=False,
                        schema={"type": "boolean", "default": False},
                    ),
                ],
            )
        },
        "/api/v1/sounds/{itemId}": {
            "get": _operation("getSound", parameters=[*item, *page]),
            "patch": _operation(
                "updateSound", scope="app.write", schema="SoundUpdate", parameters=item, write=True
            ),
        },
        "/api/v1/sounds/candidates": {"get": _operation("listSoundCandidates", parameters=page)},
        "/api/v1/sounds/candidates/{candidateId}": {
            "get": _operation("getSoundCandidate", parameters=candidate),
            "delete": _operation(
                "discardSoundCandidate", scope="app.write", parameters=candidate, write=True
            ),
        },
        "/api/v1/sounds/generate": {
            "post": _operation(
                "generateSound", scope="app.run", schema="SoundGenerate", status="202", write=True
            )
        },
        "/api/v1/sounds/save": {
            "post": _operation("saveSound", scope="app.write", schema="SoundSave", write=True)
        },
        "/api/v1/sessions/{sessionId}/sound-plan": {
            "get": _operation(
                "getProjectSoundPlan",
                parameters=[
                    *project,
                    *page,
                    _parameter(
                        "segment_id",
                        "query",
                        required=False,
                        schema={"type": "array", "maxItems": 100, "items": {"type": "string"}},
                    ),
                ],
            )
        },
        "/api/v1/sessions/{sessionId}/sound-plan/cues": {
            "post": _operation(
                "addProjectSound",
                scope="app.write",
                schema="SoundCueCreate",
                status="201",
                parameters=project,
                write=True,
            )
        },
        "/api/v1/sessions/{sessionId}/sound-plan/cues/{cueId}": {
            "patch": _operation(
                "updateProjectSound",
                scope="app.write",
                schema="SoundCueUpdate",
                parameters=cue,
                write=True,
            ),
            "delete": _operation(
                "removeProjectSound",
                scope="app.write",
                parameters=[
                    *cue,
                    _parameter(
                        "If-Match", "header", schema={"type": "string", "pattern": '^"?[0-9]+"?$'}
                    ),
                ],
                write=True,
            ),
        },
        "/api/v1/sessions/{sessionId}/sound-plan/restore": {
            "post": _operation(
                "restoreProjectSounds",
                scope="app.write",
                schema="SoundPlanRestore",
                parameters=project,
                write=True,
            )
        },
        "/api/v1/sessions/{sessionId}/sound-plan/mix": {
            "get": _operation("getProjectSoundMix", parameters=[
                *project, _parameter("base_artifact_id", "query", required=False),
            ]),
        },
        "/api/v1/sessions/{sessionId}/sound-plan/render": {
            "post": _operation("renderProjectSounds", scope="app.run", schema="SoundMixRequest", parameters=project, status="202", write=True),
        },
        "/api/v1/sessions/{sessionId}/sound-plan/resolve": {
            "post": _operation("resolveProjectSounds", scope="app.run", schema="SoundResolveRequest", parameters=project, status="202", write=True),
        },
    }
    for path, operation_id in (
        ("/api/v1/sounds/import", "importSoundAudio"),
        ("/api/v1/voices/reference-audio/import", "importVoiceReferenceAudio"),
    ):
        operation = _operation(
            operation_id,
            scope="app.write",
            status="201",
            write=True,
            parameters=[_parameter("X-Audio-Filename", "header")],
        )
        operation["description"] = (
            "Import original audio bytes (maximum 50 MiB and 300 seconds), with a fixed managed audio role. No source kind or role is caller-controlled."
        )
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
            },
        }
        paths[path] = {"put": operation}
    multipart = _operation("importSoundMultipart", scope="app.write", status="201", write=True)
    multipart["requestBody"] = {
        "required": True,
        "content": {
            "multipart/form-data": {
                "schema": {
                    "type": "object",
                    "required": ["file"],
                    "properties": {"file": {"type": "string", "format": "binary"}},
                }
            }
        },
    }
    paths["/api/v1/sounds/import"]["post"] = multipart
    return paths
