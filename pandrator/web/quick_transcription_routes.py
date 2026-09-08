"""Authenticated transport adapters for temporary transcription."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from flask import Response, jsonify, request

from .idempotency import IdempotencyError
from .quick_transcription import MIME_TYPES, TERMINAL, TranscriptionError
from .quick_transcription_schemas import (
    CHUNK_SIZE,
    MAX_SOURCE_BYTES,
    TranscriptionCreate,
    TranscriptionWait,
)
from .route_context import RouteContext


def register_quick_transcription_routes(app, context: RouteContext):
    service = context.services.quick_transcriptions
    guards = context.guards

    def subject():
        principal = guards.principal()
        assert principal is not None
        return principal.subject

    def snapshot(identifier, *, wait: float = 0):
        result = service.get(
            identifier, subject(), format=request.args.get("format"), wait_seconds=wait
        )
        return jsonify(result), 200 if result["status"] in TERMINAL else 202

    def problem(error):
        if isinstance(error, TranscriptionError):
            return guards.error_response(error.code, str(error), error.status)
        if isinstance(error, IdempotencyError):
            return guards.error_response(error.code, str(error), 409)
        return guards.error_response("validation_error", str(error), 400)

    @app.post("/api/v1/transcriptions")
    @guards.require_scope("app.run")
    def transcription_create():
        try:
            # Route-specific limits apply before Flask parses multipart form data.
            request.max_content_length = MAX_SOURCE_BYTES + 65536
            principal = guards.principal()
            assert principal is not None
            key = request.headers.get("Idempotency-Key", "")
            service.idempotency.validate_key(key)
            wait = TranscriptionWait.model_validate(
                {"wait_seconds": request.args.get("wait_seconds", 0)}
            )
            if request.args.get("response", "json") not in {"json", "raw"}:
                raise TranscriptionError(
                    "invalid_response", "Choose json or raw response mode."
                )
            if request.args.get("format") and request.args["format"] not in MIME_TYPES:
                raise TranscriptionError("invalid_format", "Choose txt, srt, or json.")
            if request.mimetype == "multipart/form-data":
                source = request.files.get("file")
                if source is None or len(list(request.files.items(multi=True))) != 1:
                    raise TranscriptionError(
                        "source_required", "Supply exactly one audio or video file."
                    )
                options = json.loads(request.form.get("options", "{}"))
                if not isinstance(options, dict):
                    raise TranscriptionError(
                        "validation_error", "Options must be a JSON object."
                    )
                forbidden = {"filename", "size_bytes", "sha256"} & options.keys()
                if forbidden:
                    raise TranscriptionError(
                        "validation_error",
                        "Source metadata is derived from the uploaded file.",
                    )
                digest = hashlib.sha256()
                size = 0
                while chunk := source.stream.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > MAX_SOURCE_BYTES:
                        raise TranscriptionError(
                            "source_too_large",
                            "Quick transcription supports files up to 256 MiB.",
                            413,
                        )
                    digest.update(chunk)
                payload = TranscriptionCreate.model_validate(
                    {
                        **options,
                        "filename": Path(source.filename or "audio.webm").name,
                        "size_bytes": size,
                        "sha256": digest.hexdigest(),
                    }
                )
                result = service.create(principal, payload, key)
                if result["status"] == "uploading":
                    source.stream.seek(result["next_chunk_index"] * CHUNK_SIZE)
                    index = result["next_chunk_index"]
                    while chunk := source.stream.read(CHUNK_SIZE):
                        service.upload_chunk(result["id"], subject(), index, chunk)
                        index += 1
                    service.start(result["id"], subject())
                if request.args.get("response") == "raw":
                    result = service.get(
                        result["id"], subject(), wait_seconds=wait.wait_seconds
                    )
                    if result["status"] == "succeeded":
                        return raw_result(result["id"], payload.format)
                    return jsonify(result), 202 if result[
                        "status"
                    ] not in TERMINAL else 200
                return snapshot(result["id"], wait=wait.wait_seconds)
            request.max_content_length = 65536
            payload = TranscriptionCreate.model_validate(
                request.get_json(silent=True) or {}
            )
            return jsonify(service.create(principal, payload, key)), 201
        except (TranscriptionError, IdempotencyError, ValueError) as error:
            return problem(error)

    @app.put("/api/v1/transcriptions/<transcription_id>/chunks/<int:index>")
    @guards.require_scope("app.run")
    def transcription_chunk(transcription_id, index):
        request.max_content_length = CHUNK_SIZE
        try:
            return jsonify(
                service.upload_chunk(
                    transcription_id, subject(), index, request.get_data()
                )
            )
        except TranscriptionError as error:
            return problem(error)

    @app.post("/api/v1/transcriptions/<transcription_id>/start")
    @guards.require_scope("app.run")
    def transcription_start(transcription_id):
        try:
            wait = TranscriptionWait.model_validate(request.get_json(silent=True) or {})
            service.start(transcription_id, subject())
            return snapshot(transcription_id, wait=wait.wait_seconds)
        except (TranscriptionError, ValueError) as error:
            return problem(error)

    @app.get("/api/v1/transcriptions/<transcription_id>")
    @guards.require_scope("app.read")
    def transcription_get(transcription_id):
        try:
            wait = TranscriptionWait.model_validate(
                {"wait_seconds": request.args.get("wait_seconds", 0)}
            )
            return snapshot(transcription_id, wait=wait.wait_seconds)
        except (TranscriptionError, ValueError) as error:
            return problem(error)

    def raw_result(identifier, format):
        content = service.result(identifier, subject(), format)
        response = Response(
            content, content_type=f"{MIME_TYPES[format]}; charset=utf-8"
        )
        response.headers["Content-Disposition"] = (
            f'attachment; filename="transcript.{format}"'
        )
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/v1/transcriptions/<transcription_id>/result")
    @guards.require_scope("app.read")
    def transcription_result(transcription_id):
        try:
            format = request.args.get("format", "txt")
            if "offset" not in request.args and "limit" not in request.args:
                return raw_result(transcription_id, format)
            offset = int(request.args.get("offset", 0))
            limit = int(request.args.get("limit", 16000))
            if offset < 0 or not 1 <= limit <= 32768:
                raise TranscriptionError(
                    "invalid_page",
                    "Use a nonnegative offset and a limit of 1–32768 characters.",
                )
            content = service.result(transcription_id, subject(), format)
            end = min(len(content), offset + limit)
            return jsonify(
                {
                    "format": format,
                    "content": content[offset:end],
                    "offset": offset,
                    "total_chars": len(content),
                    "next_offset": end if end < len(content) else None,
                }
            )
        except (TranscriptionError, ValueError) as error:
            return problem(error)

    @app.post("/api/v1/transcriptions/<transcription_id>/cancel")
    @guards.require_scope("app.run")
    def transcription_cancel(transcription_id):
        try:
            return jsonify(service.cancel(transcription_id, subject()))
        except TranscriptionError as error:
            return problem(error)

    @app.delete("/api/v1/transcriptions/<transcription_id>")
    @guards.require_scope("app.run")
    def transcription_delete(transcription_id):
        try:
            return jsonify(service.cancel(transcription_id, subject(), delete=True))
        except TranscriptionError as error:
            return problem(error)
