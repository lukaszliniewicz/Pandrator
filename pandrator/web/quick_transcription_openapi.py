"""Public contract for sessionless transcription transports."""


def transcription_paths():
    snapshot = {"$ref": "#/components/schemas/TranscriptionSnapshot"}
    json_result = {
        "description": "Temporary transcription status and a bounded inline result when complete",
        "content": {"application/json": {"schema": snapshot}},
    }
    wait = {
        "name": "wait_seconds",
        "in": "query",
        "schema": {"type": "number", "minimum": 0, "maximum": 30, "default": 0},
    }
    format = {
        "name": "format",
        "in": "query",
        "schema": {"type": "string", "enum": ["txt", "srt", "json"]},
    }

    def operation(identifier, scope, **fields):
        return {
            "operationId": identifier,
            "security": [
                {"cookieAuth": []},
                {"bearerToken": []},
                {"nativeOAuth": [scope]},
            ],
            "responses": {
                "200": json_result,
                "202": json_result,
                "404": {"description": "Not found or owned by another principal"},
                "410": {"description": "Temporary data expired or was deleted"},
            },
            **fields,
        }

    paths = {
        "/api/v1/transcriptions": {
            "post": operation(
                "createQuickTranscription",
                "app.run",
                description="Create a resumable temporary upload with JSON metadata, or submit file + JSON options in one multipart request. No permanent session or source is created. Media is limited to 256 MiB and two hours. Results expire one hour after completion. Idempotency retries reuse the same operation.",
                parameters=[
                    wait,
                    {
                        "name": "response",
                        "in": "query",
                        "schema": {
                            "type": "string",
                            "enum": ["json", "raw"],
                            "default": "json",
                        },
                    },
                    {
                        "name": "Idempotency-Key",
                        "in": "header",
                        "required": True,
                        "schema": {"type": "string", "minLength": 8, "maxLength": 200},
                    },
                ],
                requestBody={
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "$ref": "#/components/schemas/TranscriptionCreate"
                            }
                        },
                        "multipart/form-data": {
                            "schema": {
                                "type": "object",
                                "required": ["file"],
                                "properties": {
                                    "file": {"type": "string", "format": "binary"},
                                    "options": {
                                        "type": "string",
                                        "description": "JSON object with format, language, engine, model_quantization, compute_backend overrides.",
                                    },
                                },
                            }
                        },
                    },
                },
            )
        },
        "/api/v1/transcriptions/{transcriptionId}": {
            "get": operation(
                "getQuickTranscription", "app.read", parameters=[wait, format]
            ),
            "delete": operation(
                "deleteQuickTranscription",
                "app.run",
                description="Delete temporary data. Running work is canceled first; deletion completes after the worker releases it.",
            ),
        },
        "/api/v1/transcriptions/{transcriptionId}/chunks/{index}": {
            "put": operation(
                "uploadQuickTranscriptionChunk",
                "app.run",
                description="Upload the next 8 MiB chunk (or final remainder). Repeating an index with identical bytes is safe; different bytes conflict.",
                requestBody={
                    "required": True,
                    "content": {
                        "application/octet-stream": {
                            "schema": {"type": "string", "format": "binary"}
                        }
                    },
                },
            )
        },
        "/api/v1/transcriptions/{transcriptionId}/start": {
            "post": operation(
                "startQuickTranscription",
                "app.run",
                description="Verify the source checksum and enqueue once. Repeated starts return the same job.",
                requestBody={
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/TranscriptionWait"}
                        }
                    }
                },
            )
        },
        "/api/v1/transcriptions/{transcriptionId}/cancel": {
            "post": operation("cancelQuickTranscription", "app.run")
        },
        "/api/v1/transcriptions/{transcriptionId}/result": {
            "get": operation(
                "getQuickTranscriptionResult",
                "app.read",
                description="Download the full selected format, or supply offset/limit to get a JSON envelope containing a page of UTF-8-decoded text. Concatenate pages; JSON pages contain serialized JSON text.",
                parameters=[
                    format,
                    {
                        "name": "offset",
                        "in": "query",
                        "schema": {"type": "integer", "minimum": 0},
                    },
                    {
                        "name": "limit",
                        "in": "query",
                        "schema": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 32768,
                            "default": 16000,
                        },
                    },
                ],
                responses={
                    "200": {
                        "description": "Complete transcript or paginated result",
                        "content": {
                            "text/plain": {"schema": {"type": "string"}},
                            "application/x-subrip": {"schema": {"type": "string"}},
                            "application/json": {
                                "schema": {
                                    "oneOf": [
                                        {
                                            "$ref": "#/components/schemas/TranscriptionResultPage"
                                        },
                                        {
                                            "type": "object",
                                            "description": "Canonical pandrator.transcript.v1 with engine, compute_backend, text and timed segments",
                                        },
                                    ]
                                }
                            },
                        },
                    },
                    "409": {"description": "Result not ready"},
                    "410": {"description": "Result expired"},
                },
            )
        },
    }
    create = paths["/api/v1/transcriptions"]["post"]
    create["responses"]["201"] = json_result
    create["responses"]["200"] = {
        "description": "Completed transcript (raw mode) or status envelope",
        "content": {
            "application/json": {"schema": {"type": "object"}},
            "text/plain": {"schema": {"type": "string"}},
            "application/x-subrip": {"schema": {"type": "string"}},
        },
    }
    return paths
