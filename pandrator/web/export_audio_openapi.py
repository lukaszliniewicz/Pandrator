"""HTTP contract for naming the audio version an export would use."""


def export_audio_paths() -> dict:
    version = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "assembly_id": {"type": ["string", "null"]},
            "artifact_id": {"type": "string"},
            "generation_run_id": {"type": ["string", "null"]},
            "run_sequence_number": {"type": ["integer", "null"]},
            "plan_revision_id": {"type": ["string", "null"]},
            "plan_revision_number": {"type": ["integer", "null"]},
            "active_revision": {"type": "boolean"},
            "assembled_at": {"type": "string"},
        },
        "required": [
            "assembly_id",
            "artifact_id",
            "generation_run_id",
            "run_sequence_number",
            "plan_revision_id",
            "plan_revision_number",
            "active_revision",
            "assembled_at",
        ],
    }
    return {
        "/api/v1/sessions/{sessionId}/export-audio": {
            "get": {
                "operationId": "getExportAudioVersion",
                "summary": "Audio version an export with no run chosen would use",
                "description": (
                    "Read-only. Names the one current assembled audio an export that "
                    "selects no generation run would use, or lists the candidates when "
                    "it cannot tell, each with its run, assembly time and plan "
                    "revision. Never changes the choice."
                ),
                "security": [
                    {"cookieAuth": []},
                    {"bearerToken": []},
                    {"nativeOAuth": ["app.read"]},
                ],
                "responses": {
                    "200": {
                        "description": "The version, the candidates, or neither",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "type": "object",
                                    "additionalProperties": False,
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "enum": ["named", "ambiguous", "none"],
                                        },
                                        "version": {
                                            "anyOf": [version, {"type": "null"}],
                                        },
                                        "candidates": {
                                            "type": "array",
                                            "items": version,
                                        },
                                    },
                                    "required": ["status", "version", "candidates"],
                                }
                            }
                        },
                    },
                    "401": {"description": "Authentication required"},
                    "403": {"description": "Insufficient scope"},
                    "404": {"description": "Session not found"},
                },
            }
        }
    }
