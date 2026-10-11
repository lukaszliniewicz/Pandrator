"""Typed, bounded audio uploads shared by sounds and managed voice references."""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import unquote

from flask import jsonify, request

from .audio_imports import (
    cleanup_prepared_audio_import,
    prepare_audio_import,
    publish_audio_import_in_session,
)
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext

MAX_AUDIO_BYTES = 50 * 1024 * 1024


def register_audio_import_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards
    retry = MutationIdempotency(context)

    def upload(*, voice_reference: bool):
        key = request.headers.get("Idempotency-Key", "").strip()
        if not key:
            return guards.error_response(
                "idempotency_key_required", "Idempotency-Key is required for audio import.", 400
            )
        # The bounded source body is required even for a retry: its digest is the
        # idempotency identity. Neither role nor artifact kind comes from input.
        file = request.files.get("file") if request.method == "POST" else None
        name = (
            file.filename
            if file is not None
            else unquote(request.headers.get("X-Audio-Filename", ""))
        )
        if not name or len(name) > 255 or any(character in name for character in "/\\\r\n\x00"):
            return guards.error_response(
                "validation_error",
                "Supply an audio filename without paths or control characters.",
                422,
            )
        if request.method == "POST" and file is None:
            return guards.error_response("validation_error", "An audio file is required.", 422)
        stream = file.stream if file is not None else request.stream
        upload_root = services.paths.root / "sounds" / "uploads"
        upload_root.mkdir(parents=True, exist_ok=True)
        prepared = None
        try:
            with tempfile.TemporaryDirectory(prefix="audio-", dir=upload_root) as temporary:
                stage = Path(temporary) / name
                size = 0
                with stage.open("xb") as destination:
                    while chunk := stream.read(1024 * 1024):
                        size += len(chunk)
                        if size > MAX_AUDIO_BYTES:
                            raise ValueError("Audio imports may not exceed 50 MiB.")
                        destination.write(chunk)
                prepared = prepare_audio_import(services.paths, services.artifacts, stage, name)
                body = {
                    "filename": name,
                    "size_bytes": prepared.size_bytes,
                    "sha256": prepared.sha256,
                }
                operation = "importVoiceReferenceAudio" if voice_reference else "importSoundAudio"
                with services.database.immediate_session() as session:
                    reservation = services.idempotency.begin(
                        session,
                        principal=retry.principal(),
                        operation_id=operation,
                        idempotency_key=key,
                        payload=body,
                    )
                    if reservation.replayed:
                        return retry.replay(reservation)
                    if voice_reference:
                        artifact = publish_audio_import_in_session(
                            session,
                            prepared,
                            artifacts=services.artifacts,
                            paths=services.paths,
                            role="voice.reference",
                        )
                        result = {
                            "artifact_id": artifact.id,
                            "sha256": prepared.sha256,
                            "size_bytes": prepared.size_bytes,
                        }
                    else:
                        result = services.sounds.import_in_session(session, prepared)["candidate"]
                    services.idempotency.complete(
                        session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="artifact",
                        resource_id=result.get("artifact_id"),
                    )
                return jsonify(result), 201
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return retry.failure(error)
        except ValueError as error:
            return guards.error_response("invalid_audio", str(error), 422)
        except (RuntimeError, OSError):
            return guards.error_response(
                "invalid_audio", "The audio file could not be inspected or stored. Check the format and try again.", 422
            )
        finally:
            if prepared is not None:
                cleanup_prepared_audio_import(prepared)

    @app.post("/api/v1/sounds/import")
    @guards.require_auth
    def import_sound_multipart():
        return upload(voice_reference=False)

    @app.put("/api/v1/sounds/import")
    @guards.require_auth
    def import_sound_binary():
        return upload(voice_reference=False)

    @app.put("/api/v1/voices/reference-audio/import")
    @guards.require_auth
    def import_voice_reference_binary():
        return upload(voice_reference=True)
