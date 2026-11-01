"""Typed, bounded audio uploads shared by sounds and managed voice references."""

from __future__ import annotations

import tempfile
from dataclasses import replace
from pathlib import Path
from urllib.parse import unquote

from flask import jsonify, request

from .access.contracts import AccessDenied, ProjectAction, WorkspaceNotFound
from .access.sound_library_policy import require_sound_library_workspace
from .access.workspace_policy import WorkspaceAction, require_workspace_access
from .audio_imports import (
    cleanup_prepared_audio_import,
    prepare_audio_import,
    publish_audio_import_in_session,
)
from .credential_identity import credential_namespace
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .route_context import RouteContext
from .speech_catalogue_access import SpeechScopeInvalid, scope_identifier

MAX_AUDIO_BYTES = 50 * 1024 * 1024


def register_audio_import_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services, guards = context.services, context.guards
    retry = MutationIdempotency(context)

    def upload(*, voice_reference: bool):
        access = guards.access_context()
        scope_name = "Voice reference" if voice_reference else "Sound import"
        requested_workspace = None
        if voice_reference:
            if "session_id" in request.args:
                return guards.error_response(
                    "validation_error", "Session scope is not available for voice references.", 422
                )
            try:
                requested_workspace = scope_identifier(request.args.get("workspace_id"))
            except SpeechScopeInvalid as error:
                return guards.error_response("validation_error", str(error), 422)
        else:
            if "session_id" in request.args:
                return guards.error_response(
                    "validation_error", "Session scope is not available for sound imports.", 422
                )
            requested_workspace = request.args.get("workspace_id")
            if requested_workspace is not None and (
                not requested_workspace
                or requested_workspace != requested_workspace.strip()
                or len(requested_workspace) > 80
            ):
                return guards.error_response(
                    "validation_error", "Choose a valid sound workspace.", 422
                )

        def require_upload_workspace(session, selected):
            if voice_reference:
                return require_workspace_access(
                    session, access, selected, action=WorkspaceAction.EDIT
                )
            return require_sound_library_workspace(
                session, ProjectAction.EDIT, workspace_id=selected
            )

        try:
            with services.database.snapshot_session() as session:
                workspace_id = require_upload_workspace(session, requested_workspace).id
        except WorkspaceNotFound:
            return guards.error_response("not_found", f"{scope_name} workspace not found.", 404)
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
                with services.database.snapshot_session() as session:
                    require_upload_workspace(session, workspace_id)
                prepared = prepare_audio_import(services.paths, services.artifacts, stage, name)
                body = {
                    "filename": name,
                    "size_bytes": prepared.size_bytes,
                    "sha256": prepared.sha256,
                }
                operation = "importVoiceReferenceAudio" if voice_reference else "importSoundAudio"
                with services.database.immediate_session() as session:
                    principal = retry.principal()
                    proved = require_upload_workspace(session, workspace_id)
                    legacy = credential_namespace(session, proved.id).legacy_host_access
                    if not legacy:
                        principal = replace(
                            principal,
                            subject=f"workspace:{proved.id}:user:{access.actor.user_id}",
                        )
                    reservation = services.idempotency.begin(
                        session,
                        principal=principal,
                        operation_id=operation,
                        idempotency_key=key,
                        payload=body,
                    )
                    if reservation.replayed:
                        if voice_reference and reservation.response is not None:
                            previous, _status = reservation.response
                            if (not legacy and previous.get("workspace_id") != workspace_id) or (
                                "workspace_id" in previous
                                and previous["workspace_id"] != workspace_id
                            ):
                                raise WorkspaceNotFound()
                        return retry.replay(reservation)
                    if voice_reference:
                        artifact = publish_audio_import_in_session(
                            session,
                            prepared,
                            artifacts=services.artifacts,
                            paths=services.paths,
                            role="voice.reference",
                            workspace_id=workspace_id,
                        )
                        result = {
                            "artifact_id": artifact.id,
                            "sha256": prepared.sha256,
                            "size_bytes": prepared.size_bytes,
                            "workspace_id": workspace_id,
                        }
                    else:
                        result = services.sounds.import_in_session(
                            session, prepared, workspace_id=proved.id
                        )["candidate"]
                    services.idempotency.complete(
                        session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="artifact",
                        resource_id=result.get("artifact_id"),
                    )
                return jsonify(result), 201
        except WorkspaceNotFound:
            return guards.error_response("not_found", f"{scope_name} workspace not found.", 404)
        except AccessDenied:
            raise
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return retry.failure(error)
        except ValueError as error:
            return guards.error_response("invalid_audio", str(error), 422)
        except (RuntimeError, OSError):
            return guards.error_response(
                "invalid_audio",
                "The audio file could not be inspected or stored. Check the format and try again.",
                422,
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
