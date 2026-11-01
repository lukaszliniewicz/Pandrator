"""Local voices, reference samples, and provider voice lifecycle routes."""

from __future__ import annotations

import os
import shutil
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import Any

from flask import (
    jsonify,
    request,
)
from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.audio_cpp_catalogue import package_metadata
from pandrator.logic.tts_provider_profiles import AUDIO_CPP_VOICE_DESIGN_MODELS

from .access.contracts import AccessContext, AccessDenied, ProjectAction, WorkspaceNotFound
from .access.session_policy import SessionNotFound, require_session_access
from .access.voice_policy import require_voice_access, require_voice_run_access
from .access.workspace_policy import WorkspaceAction, require_workspace_access
from .accounts.actor_authorization import require_actor_in_session
from .accounts.identity import initial_personal_workspace
from .accounts.installation_mode import installation_mode_in_session
from .artifacts import sha256_file
from .credential_identity import credential_namespace
from .credentials import (
    redact_inline_secrets,
)
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress, IdempotencyReservation
from .managed_services import configured_tts_provider_ids, normalize_tts_provider_id
from .media_process import MediaProcessError
from .models import (
    Artifact,
    Job,
    Voice,
    VoiceSample,
    utcnow,
)
from .projects.artifact_identity import resolve_artifact_ownership
from .route_context import RouteContext
from .schemas import (
    VoiceCreate,
    VoiceTranscriptReview,
    VoiceUpdate,
)
from .settings_identity import workspace_setting_in_workspace
from .speech_catalogue_access import SpeechScopeInvalid, scope_identifier
from .stt_resources import stt_resource_keys
from .voice_library import (
    ensure_bundled_voice,
    is_bundled_voice,
    mark_provider_registrations_stale,
    remove_managed_files,
    retire_sample_artifact,
    sample_file_status,
    validate_profile_evidence,
    voice_payload,
    voice_payloads,
    voice_sample_payload,
)
from .voice_lifecycle_schemas import (
    VoiceDesignedSamplePreparationRequest,
    VoicePublishRequest,
    VoiceReferenceImportRequest,
)
from .voice_provider_settings import capture_voice_provider_settings, check_voice_provider_settings
from .voice_queries import (
    voice_name_owner,
    voice_sample_in_workspace,
    voices_in_workspace,
)
from .voice_recording_uploads import queue_voice_recording
from .voice_reference_reuse import reusable_design_reference, verified_sample_artifact
from .voice_reference_trim import (
    VoiceReferenceTrimError,
    inspect_voice_reference_trim,
    reference_trim_requested,
)
from .voice_sample_transcription import capture_voice_transcription_settings

VOICE_SAMPLE_NOISE_REDUCTION_OPTIONS = ("none", "deepfilternet2")


def _voice_sample_noise_reduction(form: Any) -> str | None:
    """Parse the microphone cleanup flag before any file or job side effect.

    Returns the normalized value, or None when the flag is unknown so the
    caller can reject the request before anything is saved or queued.
    """

    raw = str(form.get("noise_reduction") or "none").strip().lower()
    if raw not in VOICE_SAMPLE_NOISE_REDUCTION_OPTIONS:
        return None
    return raw


def register_voice_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    database, paths = services.database, services.paths
    artifacts, jobs = services.artifacts, services.jobs
    require_auth = context.guards.require_auth
    error_response = context.guards.error_response
    idempotency = MutationIdempotency(context)
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    abandon_idempotency = idempotency.abandon
    replay_idempotency = idempotency.replay

    def voice_record_scope(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if "session_id" in request.args:
                return error_response("validation_error", "Session scope is not available for voice records.", 422)
            try:
                workspace_id = scope_identifier(request.args.get("workspace_id"))
                return function(*args, workspace_id=workspace_id, **kwargs)
            except SpeechScopeInvalid as error:
                return error_response("validation_error", str(error), 422)
            except WorkspaceNotFound:
                return error_response("not_found", "Voice not found.", 404)
        return wrapped

    @app.get("/api/v1/voices")
    @require_auth
    def voice_list():
        workspace_id = request.args.get("workspace_id")
        session_id = request.args.get("session_id")
        if any(
            value is not None
            and (not value or value != value.strip() or not 1 <= len(value) <= 80)
            for value in (workspace_id, session_id)
        ):
            return error_response(
                "validation_error",
                "Choose a valid voice workspace or session scope.",
                422,
            )

        access = context.guards.access_context()

        def require_scope(db_session):
            selected_workspace_id = workspace_id
            if session_id is not None:
                root = require_session_access(
                    db_session, access, session_id, ProjectAction.READ
                )
                if workspace_id is not None and workspace_id != root.workspace_id:
                    raise WorkspaceNotFound()
                selected_workspace_id = root.workspace_id
            return require_workspace_access(db_session, access, selected_workspace_id)

        def initializer_eligible(db_session, proved) -> bool:
            if (
                proved.kind != "personal"
                or WorkspaceAction.EDIT not in proved.allowed_actions
                or proved.id != initial_personal_workspace(db_session).id
            ):
                return False
            return not installation_mode_in_session(db_session).enabled

        def response_for(db_session, proved):
            records = voices_in_workspace(db_session, workspace_id=proved.id)
            return jsonify(
                {
                    "items": voice_payloads(
                        db_session, paths, records, workspace_id=proved.id
                    ),
                    "workspace_id": proved.id,
                }
            )

        try:
            with database.snapshot_session() as db_session:
                proved = require_scope(db_session)
                should_initialize = initializer_eligible(db_session, proved)
                if not should_initialize:
                    return response_for(db_session, proved)

            with database.snapshot_session() as db_session:
                rechecked = require_scope(db_session)
                should_initialize = (
                    rechecked.id == proved.id
                    and initializer_eligible(db_session, rechecked)
                )
                if not should_initialize:
                    return response_for(db_session, rechecked)

            ensure_bundled_voice(
                database, paths, artifacts, workspace_id=proved.id
            )

            with database.snapshot_session() as db_session:
                recaptured = require_scope(db_session)
                return response_for(db_session, recaptured)
        except (WorkspaceNotFound, SessionNotFound):
            return error_response("not_found", "Voice workspace not found.", 404)


    @app.post("/api/v1/voices")
    @require_auth
    @voice_record_scope
    def voice_create(*, workspace_id=None):
        payload = VoiceCreate.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        name = payload.name.strip()
        if not name:
            return error_response("validation_error", "Voice name is required.", 422)
        request_payload = payload.model_dump(mode="json")
        access = context.guards.access_context()
        try:
            with database.immediate_session() as db_session:
                proved = require_workspace_access(db_session, access, workspace_id, action=WorkspaceAction.EDIT)
                legacy = credential_namespace(db_session, proved.id).legacy_host_access
                reservation = None
                if idempotency_key is not None:
                    principal = idempotency.principal()
                    if not legacy:
                        principal = replace(principal, subject=f"workspace:{proved.id}:user:{access.actor.user_id}")
                    reservation = services.idempotency.begin(
                        db_session, principal=principal, operation_id="createVoice",
                        idempotency_key=idempotency_key, payload=request_payload,
                    )
                    if reservation.response is not None:
                        previous, _status = reservation.response
                        if (not legacy and previous.get("workspace_id") != proved.id) or (
                            "workspace_id" in previous and previous["workspace_id"] != proved.id
                        ):
                            raise WorkspaceNotFound()
                        return replay_idempotency(reservation, etag_key="revision")
                validate_profile_evidence(db_session, paths, request_payload.get("profile"), workspace_id=proved.id)
                if voice_name_owner(db_session, name, workspace_id=proved.id) is not None:
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("already_exists", "A voice with that name already exists.", 409)
                voice = Voice(
                    workspace_id=proved.id,
                    name=name,
                    language=str(payload.language or "").strip() or None,
                    description=str(payload.description or "").strip() or None,
                    metadata_json={
                        "voice_category": payload.voice_category,
                        **({"profile": payload.profile.model_dump(mode="json")} if payload.profile else {}),
                    },
                )
                db_session.add(voice)
                db_session.flush()
                result = voice_payload(db_session, paths, voice, workspace_id=proved.id)
                result["workspace_id"] = proved.id
                if reservation is not None:
                    services.idempotency.complete(db_session, reservation, response=result, status_code=201,
                        resource_kind="voice", resource_id=voice.id)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        response = jsonify(result)
        response.status_code = 201
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response


    @app.patch("/api/v1/voices/<voice_id>")
    @require_auth
    @voice_record_scope
    def voice_update(voice_id: str, *, workspace_id=None):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current voice revision.",
                428,
            )
        payload = VoiceUpdate.model_validate(request.get_json(silent=True) or {})
        changes = payload.model_dump(exclude_unset=True)
        with database.immediate_session() as db_session:
            voice = require_voice_access(db_session, context.guards.access_context(), voice_id, workspace_id=workspace_id, action=WorkspaceAction.EDIT)
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "The bundled reference voice cannot be edited.",
                    409,
                )
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            try:
                validate_profile_evidence(
                    db_session, paths, changes.get("profile"), workspace_id=voice.workspace_id
                )
            except ValueError as error:
                return error_response("validation_error", str(error), 422)
            if "name" in changes:
                name = str(changes["name"] or "").strip()
                if not name:
                    return error_response(
                        "validation_error",
                        "Voice name cannot be blank.",
                        422,
                    )
                owner = db_session.scalar(
                    select(Voice).where(
                        Voice.name == name, Voice.id != voice.id,
                        Voice.workspace_id == voice.workspace_id,
                    )
                )
                if owner is not None:
                    return error_response(
                        "already_exists",
                        "A voice with that name already exists.",
                        409,
                    )
                voice.name = name
            if "language" in changes:
                voice.language = str(changes["language"] or "").strip() or None
            if "description" in changes:
                voice.description = str(changes["description"] or "").strip() or None
            if "voice_category" in changes:
                metadata = dict(voice.metadata_json or {})
                category = changes["voice_category"] or "unspecified"
                if category != metadata.get("voice_category") and "profile" not in changes:
                    profile = dict(metadata.get("profile") or {})
                    evidence = dict(profile.get("evidence") or {})
                    evidence.pop("voice_category", None)
                    profile["evidence"] = evidence
                    metadata["profile"] = profile
                metadata["voice_category"] = category
                voice.metadata_json = metadata
            if "profile" in changes:
                metadata = dict(voice.metadata_json or {})
                if changes["profile"] is None:
                    metadata.pop("profile", None)
                else:
                    metadata["profile"] = changes["profile"]
                voice.metadata_json = metadata
            if changes:
                voice.revision += 1
                voice.updated_at = utcnow()
            result = voice_payload(db_session, paths, voice, workspace_id=voice.workspace_id)
            result["workspace_id"] = voice.workspace_id
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response


    @app.delete("/api/v1/voices/<voice_id>")
    @require_auth
    @voice_record_scope
    def voice_delete(voice_id: str, *, workspace_id=None):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current voice revision.",
                428,
            )
        removable: list[Path] = []
        with database.immediate_session() as db_session:
            voice = require_voice_access(db_session, context.guards.access_context(), voice_id, workspace_id=workspace_id, action=WorkspaceAction.EDIT)
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "The bundled reference voice cannot be deleted.",
                    409,
                )
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            active_voice_job = next(
                (
                    job_id
                    for job_id, resource_keys in db_session.execute(
                        select(Job.id, Job.resource_keys_json).where(
                            Job.workspace_id == voice.workspace_id,
                            Job.status.in_(("queued", "running", "cancel_requested"))
                        )
                    ).all()
                    if f"voice:{voice_id}" in (resource_keys or [])
                ),
                None,
            )
            if active_voice_job is not None:
                return error_response(
                    "voice_busy",
                    "Wait for the provider voice operation to finish before "
                    "deleting the local voice.",
                    409,
                )
            samples = list(
                db_session.scalars(
                    select(VoiceSample).where(VoiceSample.voice_id == voice.id, VoiceSample.workspace_id == voice.workspace_id)
                ).all()
            )
            for sample in samples:
                path = retire_sample_artifact(db_session, paths, sample)
                if path is not None:
                    removable.append(path)
                db_session.delete(sample)
            # Finish explicit sample deletions before the voice FK cascade.
            db_session.flush()
            db_session.delete(voice)
        remove_managed_files(removable)
        return "", 204


    @app.get("/api/v1/voices/<voice_id>/samples")
    @require_auth
    @voice_record_scope
    def voice_sample_list(voice_id: str, *, workspace_id=None):
        with database.snapshot_session() as db_session:
            voice = require_voice_access(db_session, context.guards.access_context(), voice_id, workspace_id=workspace_id, action=WorkspaceAction.READ)
            records = list(
                db_session.scalars(
                    select(VoiceSample)
                    .where(VoiceSample.voice_id == voice_id, VoiceSample.workspace_id == voice.workspace_id)
                    .order_by(VoiceSample.created_at.desc())
                ).all()
            )
            return jsonify(
                {
                    "items": [
                        voice_sample_payload(
                            db_session,
                            paths,
                            item,
                            voice_revision=voice.revision,
                        )
                        for item in records
                    ],
                    "voice_revision": voice.revision,
                    "workspace_id": voice.workspace_id,
                }
            )


    def processing_receipt(
        db_session: Session,
        access: AccessContext,
        voice: Voice,
        operation_id: str,
        key: str | None,
        payload: dict[str, Any],
    ) -> IdempotencyReservation | None:
        if key is None:
            return None
        principal = idempotency.principal()
        legacy = credential_namespace(db_session, voice.workspace_id).legacy_host_access
        if not legacy:
            principal = replace(
                principal, subject=f"workspace:{voice.workspace_id}:user:{access.actor.user_id}"
            )
        reservation = services.idempotency.begin(
            db_session, principal=principal, operation_id=operation_id,
            idempotency_key=key, payload=payload,
        )
        if reservation.response is not None:
            previous, _status = reservation.response
            if (not legacy and previous.get("workspace_id") != voice.workspace_id) or (
                "workspace_id" in previous and previous["workspace_id"] != voice.workspace_id
            ):
                raise WorkspaceNotFound()
        return reservation

    def inspect_processing_retry(
        access: AccessContext, voice_id: str, workspace_id: str | None,
        operation_id: str, key: str | None, payload: dict[str, Any],
    ):
        if key is None:
            return None
        try:
            with database.immediate_session() as db_session:
                voice = require_voice_run_access(
                    db_session, access, voice_id, workspace_id=workspace_id
                )
                reservation = processing_receipt(
                    db_session, access, voice, operation_id, key, payload
                )
                if reservation is not None:
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                    abandon_idempotency(db_session, reservation)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        return None

    def unchanged_processing_source(db_session: Session, voice: Voice, source: Artifact) -> bool:
        return db_session.scalar(select(Artifact.id).where(
            Artifact.id == source.id, Artifact.workspace_id == voice.workspace_id,
            Artifact.state != "deleted", Artifact.kind == source.kind,
            Artifact.role == source.role, Artifact.relative_path == source.relative_path,
            Artifact.content_hash == source.content_hash,
        )) is not None


    @app.post("/api/v1/voices/<voice_id>/samples/from-preview")
    @require_auth
    @voice_record_scope
    def voice_sample_from_preview(voice_id: str, *, workspace_id=None):
        payload = VoiceDesignedSamplePreparationRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        idempotency_payload = {
            "voice_id": voice_id,
            **payload.model_dump(mode="json", exclude={"recipe_signature"}),
            **({"recipe_signature": payload.recipe_signature} if payload.recipe_signature is not None else {}),
        }
        access = context.guards.access_context()
        replay = inspect_processing_retry(
            access, voice_id, workspace_id, "createVoiceSampleFromPreview",
            idempotency_key,
            idempotency_payload,
        )
        if replay is not None:
            return replay
        transcript = payload.transcript.strip()
        language = str(payload.language or "").strip() or None
        if not transcript:
            return error_response(
                "validation_error",
                "A reviewed transcript is required.",
                422,
            )
        with database.snapshot_session() as db_session:
            voice = require_voice_run_access(
                db_session, access, voice_id, workspace_id=workspace_id
            )
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "Samples cannot be added to the bundled reference voice.",
                    409,
                )
            if idempotency_key is None and voice.revision != payload.expected_voice_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            artifact = db_session.scalar(select(Artifact).where(
                Artifact.id == payload.artifact_id, Artifact.workspace_id == voice.workspace_id,
            ))
            if artifact is None or artifact.state == "deleted":
                return error_response(
                    "not_found",
                    "Preview artifact not found.",
                    404,
                )
            if resolve_artifact_ownership(db_session, artifact=artifact).workspace_id != voice.workspace_id:
                return error_response("not_found", "Preview artifact not found.", 404)
            if artifact.role != "tts_voice_preview" or artifact.kind != "audio":
                return error_response(
                    "invalid_preview_artifact",
                    "The artifact is not a managed TTS voice preview.",
                    422,
                )
            metadata = dict(artifact.metadata_json or {})
            preview_text = str(metadata.get("preview_text") or "").strip()
            if not preview_text:
                return error_response(
                    "invalid_preview_artifact",
                    "The voice-design preview has no source transcript.",
                    422,
                )
            if transcript != preview_text:
                return error_response(
                    "preview_transcript_mismatch",
                    "The reviewed transcript must match the exact text used to generate the preview.",
                    422,
                )
            transcript = preview_text
            preview_service_id = normalize_tts_provider_id(metadata.get("service_id"))
            preview_adapter = normalize_tts_provider_id(
                metadata.get("service_adapter")
                or (
                    "audio_cpp"
                    if preview_service_id in {"audio_cpp", "audio_cpp_experimental"}
                    else ""
                )
            )
            preview_model = str(metadata.get("model") or "").strip().casefold()
            if preview_adapter != "audio_cpp":
                return error_response(
                    "unsupported_preview_provider",
                    "Only audio.cpp voice-design previews can become voice samples.",
                    422,
                )
            if preview_model not in {
                model.casefold() for model in AUDIO_CPP_VOICE_DESIGN_MODELS
            }:
                return error_response(
                    "unsupported_preview_model",
                    "Only supported audio.cpp VoiceDesign previews can become voice samples.",
                    422,
                )
            try:
                preview_path = paths.managed_path(artifact.relative_path)
            except (OSError, ValueError):
                return error_response(
                    "unsafe_preview_artifact",
                    "The preview artifact path is not safely managed.",
                    422,
                )
            if not preview_path.is_file() or not os.access(preview_path, os.R_OK):
                return error_response(
                    "invalid_preview_artifact",
                    "The preview artifact is not a readable regular file.",
                    422,
                )
            try:
                preview_content_hash = sha256_file(preview_path)
            except OSError:
                return error_response(
                    "invalid_preview_artifact",
                    "The preview artifact could not be read.",
                    422,
                )
            registered_hash = str(artifact.content_hash or "").strip().casefold()
            if registered_hash and registered_hash != preview_content_hash:
                return error_response(
                    "preview_artifact_changed",
                    "The voice-design preview changed after it was generated. Generate and review it again.",
                    409,
                )
            preview_language = str(metadata.get("language") or "").strip() or None
            preview_provenance = redact_inline_secrets(
                {
                    "source_kind": "generated_voice_design",
                    "source_preview_artifact_id": artifact.id,
                    "source_preview_sha256": preview_content_hash,
                    "service_id": preview_service_id,
                    "service_adapter": preview_adapter,
                    "service": metadata.get("service"),
                    "model": metadata.get("model"),
                    "model_family": metadata.get("model_family")
                    or metadata.get("family")
                    or package_metadata(preview_model).get("family"),
                    "generation_prompt": str(
                        metadata.get("generation_prompt") or ""
                    ).strip(),
                    "seed": metadata.get("seed"),
                    "preview_text": preview_text,
                    "sample_transcript": transcript,
                    "transcript": transcript,
                    "language": language or preview_language,
                    "generation_settings": metadata.get("generation_settings")
                    if isinstance(metadata.get("generation_settings"), dict)
                    else {},
                }
            )
        try:
            with database.immediate_session() as db_session:
                current_voice = require_voice_run_access(
                    db_session, access, voice_id, workspace_id=voice.workspace_id
                )
                reservation = processing_receipt(
                    db_session, access, current_voice, "createVoiceSampleFromPreview",
                    idempotency_key, idempotency_payload,
                )
                if reservation is not None:
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                if is_bundled_voice(current_voice):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("bundled_voice_protected",
                        "Samples cannot be added to the bundled reference voice.", 409)
                if current_voice.revision != payload.expected_voice_revision:
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("revision_conflict", "The voice changed in another client.", 409)
                if not unchanged_processing_source(db_session, current_voice, artifact):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("preview_artifact_changed",
                        "The voice-design preview changed after it was generated. Generate and review it again.", 409)
                reusable = reusable_design_reference(
                    db_session, paths, current_voice, source_artifact_id=artifact.id,
                    source_sha256=preview_content_hash, transcript=transcript,
                    language=language or preview_language,
                )
                if reusable is not None:
                    reusable["workspace_id"] = current_voice.workspace_id
                    if reservation is not None:
                        services.idempotency.complete(
                            db_session, reservation, response=reusable, status_code=200,
                            resource_kind="voice_sample", resource_id=reusable["sample_id"],
                        )
                    return jsonify(reusable)
                job = jobs.enqueue_in_session(
                    db_session, "voice.normalize_recording",
                    {
                        "workspace_id": current_voice.workspace_id, "voice_id": voice_id,
                        "source_artifact_id": artifact.id,
                        "source_artifact_sha256": preview_content_hash,
                        "source_artifact_role": "tts_voice_preview",
                        "expected_voice_revision": payload.expected_voice_revision,
                        "reviewed_transcript": transcript,
                        "transcript_language": language or preview_language,
                        "sample_provenance": preview_provenance,
                        "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
                    }, resource_keys=[f"voice:{voice_id}"], workspace_id=current_voice.workspace_id,
                )
                result = {**_job_payload(job), "workspace_id": current_voice.workspace_id}
                if reservation is not None:
                    services.idempotency.complete(
                        db_session, reservation, response=result, status_code=202,
                        resource_kind="job", resource_id=job.id,
                    )
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        return jsonify(result), 202


    @app.post("/api/v1/voices/<voice_id>/samples/from-artifact")
    @require_auth
    @voice_record_scope
    def voice_sample_from_artifact(voice_id: str, *, workspace_id=None):
        payload = VoiceReferenceImportRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        trim_requested = reference_trim_requested(
            payload.start_seconds, payload.end_seconds
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        request_fields = payload.model_dump(mode="json")
        if not trim_requested:
            # Keep existing no-crop idempotency identities stable across deploys.
            request_fields.pop("start_seconds", None)
            request_fields.pop("end_seconds", None)
        idempotency_payload = {
            "voice_id": voice_id,
            **request_fields,
        }
        access = context.guards.access_context()
        replay = inspect_processing_retry(
            access, voice_id, workspace_id, "createVoiceSampleFromArtifact",
            idempotency_key,
            idempotency_payload,
        )
        if replay is not None:
            return replay
        transcript = str(payload.transcript or "").strip()
        language = str(payload.language or "").strip() or None

        with database.snapshot_session() as db_session:
            voice = require_voice_run_access(
                db_session, access, voice_id, workspace_id=workspace_id
            )
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "Samples cannot be added to the bundled reference voice.",
                    409,
                )
            if (
                idempotency_key is None
                and voice.revision != payload.expected_voice_revision
            ):
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            artifact = db_session.scalar(select(Artifact).where(
                Artifact.id == payload.artifact_id, Artifact.workspace_id == voice.workspace_id,
            ))
            if artifact is None or artifact.state == "deleted":
                return error_response(
                    "not_found", "Voice reference artifact not found.", 404
                )
            if resolve_artifact_ownership(db_session, artifact=artifact).workspace_id != voice.workspace_id:
                return error_response("not_found", "Voice reference artifact not found.", 404)
            if artifact.kind != "audio":
                return error_response(
                    "invalid_reference_artifact",
                    "The voice reference artifact must be managed audio.",
                    422,
                )
            try:
                artifact_path = paths.managed_path(artifact.relative_path)
            except (OSError, ValueError):
                return error_response(
                    "unsafe_reference_artifact",
                    "The voice reference artifact path is not safely managed.",
                    422,
                )
            if not artifact_path.is_file() or not os.access(artifact_path, os.R_OK):
                return error_response(
                    "invalid_reference_artifact",
                    "The voice reference artifact is not a readable regular file.",
                    422,
                )
            try:
                artifact_hash = sha256_file(artifact_path)
            except OSError:
                return error_response(
                    "invalid_reference_artifact",
                    "The voice reference artifact could not be read.",
                    422,
                )
            registered_hash = str(artifact.content_hash or "").strip().casefold()
            if not registered_hash or registered_hash != artifact_hash:
                return error_response(
                    "reference_artifact_changed",
                    "The voice reference artifact changed after it was registered.",
                    409,
                )
            artifact_id = artifact.id
            artifact_role = artifact.role

        trim = None
        ffprobe_executable = shutil.which("ffprobe") or "ffprobe"
        if trim_requested:
            with database.snapshot_session() as db_session:
                require_voice_run_access(db_session, access, voice_id, workspace_id=voice.workspace_id)
            try:
                trim = inspect_voice_reference_trim(
                    artifact_path,
                    payload.start_seconds,
                    payload.end_seconds,
                    ffprobe_executable=ffprobe_executable,
                )
            except VoiceReferenceTrimError as error:
                return error_response("validation_error", str(error), 422)
            except MediaProcessError:
                return error_response(
                    "invalid_reference_artifact",
                    "The voice reference artifact could not be inspected.",
                    422,
                )
            try:
                probed_source_hash = sha256_file(artifact_path)
            except OSError:
                return error_response(
                    "invalid_reference_artifact",
                    "The voice reference artifact could not be read.",
                    422,
                )
            if probed_source_hash != artifact_hash:
                return error_response(
                    "reference_artifact_changed",
                    "The voice reference artifact changed after it was registered.",
                    409,
                )

        sample_provenance: dict[str, Any] = {
            "source_kind": "imported_voice_reference",
            "source_artifact_id": artifact_id,
        }
        if trim is not None:
            sample_provenance["reference_trim"] = trim.provenance()
        job_payload = {
            "workspace_id": voice.workspace_id,
            "voice_id": voice_id,
            "source_artifact_id": artifact_id,
            "source_artifact_sha256": artifact_hash,
            "source_artifact_role": artifact_role,
            "expected_voice_revision": payload.expected_voice_revision,
            "transcript_language": language,
            "sample_provenance": sample_provenance,
            "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
        }
        if trim is not None:
            job_payload["start_seconds"] = float(payload.start_seconds)
            job_payload["end_seconds"] = (
                float(payload.end_seconds) if payload.end_seconds is not None else None
            )
            job_payload["ffprobe_executable"] = ffprobe_executable
        if payload.transcript_reviewed:
            job_payload["reviewed_transcript"] = transcript
        elif transcript:
            job_payload["unreviewed_transcript"] = transcript
        try:
            with database.immediate_session() as db_session:
                current_voice = require_voice_run_access(
                    db_session, access, voice_id, workspace_id=voice.workspace_id
                )
                reservation = processing_receipt(
                    db_session, access, current_voice, "createVoiceSampleFromArtifact",
                    idempotency_key, idempotency_payload,
                )
                if reservation is not None:
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                if is_bundled_voice(current_voice):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("bundled_voice_protected",
                        "Samples cannot be added to the bundled reference voice.", 409)
                if current_voice.revision != payload.expected_voice_revision:
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("revision_conflict", "The voice changed in another client.", 409)
                if not unchanged_processing_source(db_session, current_voice, artifact):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("reference_artifact_changed",
                        "The voice reference artifact changed after it was registered.", 409)
                job = jobs.enqueue_in_session(
                    db_session, "voice.normalize_recording", job_payload,
                    resource_keys=[f"voice:{voice_id}"], workspace_id=current_voice.workspace_id,
                )
                result = {**_job_payload(job), "workspace_id": current_voice.workspace_id}
                if reservation is not None:
                    services.idempotency.complete(
                        db_session, reservation, response=result, status_code=202,
                        resource_kind="job", resource_id=job.id,
                    )
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        return jsonify(result), 202


    @app.post("/api/v1/voices/<voice_id>/samples")
    @require_auth
    @voice_record_scope
    def voice_sample_upload(voice_id: str, *, workspace_id=None):
        access = context.guards.access_context()
        noise_reduction = _voice_sample_noise_reduction(request.form)
        if noise_reduction is None:
            return error_response(
                "validation_error",
                "noise_reduction must be 'none' or 'deepfilternet2'.",
                422,
            )
        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return error_response(
                "missing_file", "An audio recording is required.", 400
            )
        with database.snapshot_session() as db_session:
            voice = require_voice_run_access(
                db_session, access, voice_id, workspace_id=workspace_id
            )
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "Samples cannot be added to the bundled reference voice.",
                    409,
                )
            expected_revision = request.form.get("expected_revision", type=int)
            if expected_revision is None:
                raw_etag = request.headers.get("If-Match", "").strip('W/" ')
                try:
                    expected_revision = int(raw_etag)
                except ValueError:
                    return error_response(
                        "precondition_required",
                        "Provide the current voice revision before adding a sample.",
                        428,
                    )
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
        job = queue_voice_recording(
            database, paths, artifacts, jobs, incoming,
            voice_id=voice_id,
            expected_revision=expected_revision,
            noise_reduction=noise_reduction,
            workspace_id=voice.workspace_id,
            access_context=access,
        )
        return jsonify({**_job_payload(job), "workspace_id": voice.workspace_id}), 202


    @app.post("/api/v1/voices/<voice_id>/samples/<sample_id>/replace")
    @require_auth
    @voice_record_scope
    def voice_sample_replace(voice_id: str, sample_id: str, *, workspace_id=None):
        access = context.guards.access_context()
        noise_reduction = _voice_sample_noise_reduction(request.form)
        if noise_reduction is None:
            return error_response(
                "validation_error",
                "noise_reduction must be 'none' or 'deepfilternet2'.",
                422,
            )
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current voice revision.",
                428,
            )
        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return error_response(
                "missing_file", "An audio recording is required.", 400
            )
        with database.snapshot_session() as db_session:
            voice = require_voice_run_access(
                db_session, access, voice_id, workspace_id=workspace_id
            )
            sample = voice_sample_in_workspace(
                db_session, sample_id, voice_id=voice_id, workspace_id=voice.workspace_id
            )
            if sample is None or sample.voice_id != voice_id:
                return error_response("not_found", "Voice sample not found.", 404)
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "The bundled reference sample cannot be replaced.",
                    409,
                )
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
        job = queue_voice_recording(
            database, paths, artifacts, jobs, incoming,
            voice_id=voice_id,
            expected_revision=expected_revision,
            noise_reduction=noise_reduction,
            workspace_id=voice.workspace_id,
            access_context=access,
            replace_sample_id=sample_id,
        )
        return jsonify({**_job_payload(job), "workspace_id": voice.workspace_id}), 202


    @app.delete("/api/v1/voices/<voice_id>/samples/<sample_id>")
    @require_auth
    @voice_record_scope
    def voice_sample_delete(voice_id: str, sample_id: str, *, workspace_id=None):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current voice revision.",
                428,
            )
        removable: list[Path] = []
        with database.immediate_session() as db_session:
            voice = require_voice_access(db_session, context.guards.access_context(), voice_id, workspace_id=workspace_id, action=WorkspaceAction.EDIT)
            sample = voice_sample_in_workspace(db_session, sample_id, voice_id=voice_id, workspace_id=voice.workspace_id)
            if sample is None or sample.voice_id != voice_id:
                return error_response("not_found", "Voice sample not found.", 404)
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "The bundled reference sample cannot be deleted.",
                    409,
                )
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            path = retire_sample_artifact(db_session, paths, sample)
            if path is not None:
                removable.append(path)
            db_session.delete(sample)
            mark_provider_registrations_stale(
                voice,
                "A local reference sample was removed.",
                sample_id=sample.id,
            )
            voice.revision += 1
            voice.updated_at = utcnow()
            next_revision = voice.revision
            proved_workspace_id = voice.workspace_id
        remove_managed_files(removable)
        response = jsonify(
            {"id": sample_id, "status": "deleted", "voice_revision": next_revision, "workspace_id": proved_workspace_id}
        )
        response.headers["ETag"] = f'"{next_revision}"'
        return response


    def provider_admission_settings(db_session: Session, voice: Voice, service_id: str):
        """Read approved configuration only after proving the owning voice."""
        from pandrator.logic import tts_handler

        captured = capture_voice_provider_settings(db_session, voice.workspace_id)
        values = {}
        for key in ("defaults.tts", "services.tts"):
            row = workspace_setting_in_workspace(db_session, voice.workspace_id, key)
            if row is not None and isinstance(row.value_json, dict):
                values.update(row.value_json)
        service = tts_handler.get_service_config(values, service_id)
        if service is not None and not credential_namespace(db_session, voice.workspace_id).legacy_host_access:
            identifier = normalize_tts_provider_id(service.get("id") or service_id)
            enabled = {}
            for key in ("provider_configs", "service_configs"):
                for record in values.get(key, []) if isinstance(values.get(key), list) else []:
                    if isinstance(record, dict):
                        enabled[normalize_tts_provider_id(record.get("id") or record.get("name") or record.get("provider"))] = record.get("enabled") is not False
            if identifier not in configured_tts_provider_ids(values) or not enabled.get(identifier, True):
                service = None
        return service, captured

    @app.post("/api/v1/voices/<voice_id>/providers/<service_id>")
    @require_auth
    @voice_record_scope
    def voice_publish_to_provider(voice_id: str, service_id: str, *, workspace_id=None):
        """Upload an exact managed reference using its workspace connection."""
        raw_payload = request.get_json(silent=True)
        payload = VoicePublishRequest.model_validate({} if raw_payload is None else raw_payload)
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        try:
            expected_revision = int(request.headers.get("If-Match", "").strip('W/" '))
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current voice revision.", 428)
        idempotency_payload = {
            "voice_id": voice_id, "service_id": service_id,
            "expected_revision": expected_revision,
            **payload.model_dump(mode="json", exclude_none=True),
        }
        access = context.guards.access_context()
        replay = inspect_processing_retry(access, voice_id, workspace_id,
            "publishVoiceToProvider", idempotency_key, idempotency_payload)
        if replay is not None:
            return replay
        with database.session() as db_session:
            voice = require_voice_run_access(db_session, access, voice_id, workspace_id=workspace_id)
            if voice.revision != expected_revision:
                return error_response("revision_conflict", "The voice changed in another client.", 409)
            voice_samples = list(db_session.scalars(select(VoiceSample).where(
                VoiceSample.voice_id == voice_id, VoiceSample.workspace_id == voice.workspace_id
            ).order_by(VoiceSample.created_at.desc())).all())
            preferred_sample = next((item for item in voice_samples
                if sample_file_status(db_session, paths, item)[0] == "ready"), None)
            newest_ready_sample_id = preferred_sample.id if preferred_sample is not None else None
            if payload.sample_id is not None:
                preferred_sample = next((item for item in voice_samples if item.id == payload.sample_id), None)
                if preferred_sample is None:
                    return error_response("not_found", "Voice sample not found.", 404)
            if preferred_sample is None:
                return error_response("missing_sample", "Add or replace a readable voice sample before uploading this voice.", 422)
            try:
                preferred_artifact, sample_hash = verified_sample_artifact(
                    db_session, paths, preferred_sample, expected_sha256=payload.sample_sha256)
            except ValueError as error:
                return error_response("sample_artifact_changed", str(error), 409)
            voice = require_voice_run_access(db_session, access, voice_id, workspace_id=workspace_id)
            service, captured = provider_admission_settings(db_session, voice, service_id)
        if service is None:
            return error_response("not_found", "TTS service not found in this workspace.", 404)
        if not bool(service.get("supports_voice_cloning")):
            return error_response("unsupported", "This TTS service does not support managed voice uploads.", 422)
        resolved_service_id = str(service.get("id") or service_id)
        if (normalize_tts_provider_id(service.get("adapter")) == "audio_cpp"
            and payload.sample_id is not None and payload.sample_id != newest_ready_sample_id):
            return error_response("current_reference_required", "audio.cpp linked voices use the newest ready local sample. Refresh the voice samples and publish the current reference.", 409)
        if (str(service.get("voice_reference_text") or "ignored") == "required"
            and not (preferred_sample.transcript_reviewed and str(preferred_sample.transcript or "").strip())):
            return error_response("reviewed_transcript_required", f"{service.get('name') or resolved_service_id} requires a reviewed sample transcript before this voice can be used.", 422)
        job_payload = {
            "workspace_id": voice.workspace_id, "voice_id": voice_id,
            "service_id": resolved_service_id, "service": str(service.get("name") or resolved_service_id),
            "expected_voice_revision": expected_revision,
            "sample_id": preferred_sample.id, "sample_artifact_id": preferred_artifact.id,
            "sample_sha256": sample_hash, **captured,
        }
        try:
            with database.immediate_session() as db_session:
                current_voice = require_voice_run_access(db_session, access, voice_id, workspace_id=workspace_id)
                reservation = processing_receipt(db_session, access, current_voice,
                    "publishVoiceToProvider", idempotency_key, idempotency_payload)
                if reservation is not None:
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                if current_voice.revision != expected_revision:
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("revision_conflict", "The voice changed in another client.", 409)
                check_voice_provider_settings(db_session, job_payload, current_voice.workspace_id)
                current_sample = db_session.scalar(select(VoiceSample).where(
                    VoiceSample.id == preferred_sample.id,
                    VoiceSample.voice_id == voice_id,
                    VoiceSample.workspace_id == current_voice.workspace_id))
                if (current_sample is None or current_sample.artifact_id != preferred_artifact.id
                    or not unchanged_processing_source(db_session, current_voice, preferred_artifact)):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("sample_artifact_changed", "The selected voice sample changed.", 409)
                job = jobs.enqueue_in_session(db_session, "voice.publish", job_payload,
                    resource_keys=[f"voice:{voice_id}", f"service:tts:{resolved_service_id}"],
                    workspace_id=current_voice.workspace_id)
                result = {**_job_payload(job), "workspace_id": current_voice.workspace_id}
                if reservation is not None:
                    services.idempotency.complete(db_session, reservation, response=result,
                        status_code=202, resource_kind="job", resource_id=job.id)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        return jsonify(result), 202

    @app.delete("/api/v1/voices/<voice_id>/providers/<service_id>")
    @require_auth
    @voice_record_scope
    def voice_remove_from_provider(voice_id: str, service_id: str, *, workspace_id=None):
        """Remove a managed provider copy under current workspace authority."""
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        try:
            expected_revision = int(request.headers.get("If-Match", "").strip('W/" '))
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current voice revision.", 428)
        idempotency_payload = {"voice_id": voice_id, "service_id": service_id, "expected_revision": expected_revision}
        access = context.guards.access_context()
        replay = inspect_processing_retry(access, voice_id, workspace_id,
            "removeVoiceFromProvider", idempotency_key, idempotency_payload)
        if replay is not None:
            return replay
        with database.session() as db_session:
            voice = require_voice_run_access(db_session, access, voice_id, workspace_id=workspace_id)
            if voice.revision != expected_revision:
                return error_response("revision_conflict", "The voice changed in another client.", 409)
            registration = dict(((voice.metadata_json or {}).get("providers") or {}).get(service_id) or {})
            if not registration:
                return error_response("not_found", "This voice is not registered with that service.", 404)
            if registration.get("managed_by") != "pandrator":
                return error_response("legacy_registration", "This older registration has no ownership proof, so Pandrator will not delete it automatically.", 409)
            service, captured = provider_admission_settings(db_session, voice, service_id)
        if service is None:
            return error_response("not_found", "TTS service not found in this workspace.", 404)
        linked_reference = registration.get("resource_kind") == "linked_reference"
        if not bool(service.get("supports_voice_deletion")) and not (
            linked_reference and normalize_tts_provider_id(service.get("adapter")) == "audio_cpp"):
            return error_response("unsupported", "This TTS service does not advertise provider-side voice deletion.", 422)
        resolved_service_id = str(service.get("id") or service_id)
        job_payload = {
            "workspace_id": voice.workspace_id, "voice_id": voice_id,
            "service_id": resolved_service_id, "service": str(service.get("name") or resolved_service_id),
            "expected_voice_revision": expected_revision, **captured,
        }
        try:
            with database.immediate_session() as db_session:
                current_voice = require_voice_run_access(db_session, access, voice_id, workspace_id=workspace_id)
                reservation = processing_receipt(db_session, access, current_voice,
                    "removeVoiceFromProvider", idempotency_key, idempotency_payload)
                if reservation is not None:
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                if current_voice.revision != expected_revision or (
                    ((current_voice.metadata_json or {}).get("providers") or {}).get(service_id) != registration):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("revision_conflict", "The voice changed in another client.", 409)
                check_voice_provider_settings(db_session, job_payload, current_voice.workspace_id)
                job = jobs.enqueue_in_session(db_session, "voice.unpublish", job_payload,
                    resource_keys=[f"voice:{voice_id}", f"service:tts:{resolved_service_id}"],
                    workspace_id=current_voice.workspace_id)
                result = {**_job_payload(job), "workspace_id": current_voice.workspace_id}
                if reservation is not None:
                    services.idempotency.complete(db_session, reservation, response=result,
                        status_code=202, resource_kind="job", resource_id=job.id)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        return jsonify(result), 202


    @app.post("/api/v1/voices/<voice_id>/samples/<sample_id>/transcribe")
    @require_auth
    @voice_record_scope
    def voice_sample_transcribe(voice_id: str, sample_id: str, *, workspace_id=None):
        raw_settings = request.get_json(silent=True)
        settings = {} if raw_settings is None else raw_settings
        if not isinstance(settings, dict):
            return error_response(
                "validation_error", "Transcription settings must be an object.", 422
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        access = context.guards.access_context()
        try:
            with database.immediate_session() as db_session:
                try:
                    require_actor_in_session(
                        db_session,
                        access.actor,
                        required_scopes=("app.read", "app.write", "app.run"),
                    )
                except PermissionError:
                    raise AccessDenied() from None
                voice = require_voice_access(
                    db_session,
                    access,
                    voice_id,
                    workspace_id=workspace_id,
                    action=WorkspaceAction.EDIT,
                )
                legacy = credential_namespace(db_session, voice.workspace_id).legacy_host_access
                reservation = None
                if idempotency_key is not None:
                    principal = idempotency.principal()
                    if not legacy:
                        principal = replace(
                            principal,
                            subject=f"workspace:{voice.workspace_id}:user:{access.actor.user_id}",
                        )
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=principal,
                        operation_id="transcribeVoiceSample",
                        idempotency_key=idempotency_key,
                        payload={
                            "voice_id": voice_id,
                            "sample_id": sample_id,
                            "settings": settings,
                        },
                    )
                    if reservation.response is not None:
                        previous, _status = reservation.response
                        if (not legacy and previous.get("workspace_id") != voice.workspace_id) or (
                            "workspace_id" in previous
                            and previous["workspace_id"] != voice.workspace_id
                        ):
                            raise WorkspaceNotFound()
                        return replay_idempotency(reservation)
                sample = voice_sample_in_workspace(
                    db_session, sample_id, voice_id=voice_id, workspace_id=voice.workspace_id
                )
                if sample is None:
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response("not_found", "Voice sample not found.", 404)
                captured = capture_voice_transcription_settings(
                    db_session, voice.workspace_id, settings, voice.language
                )
                job = jobs.enqueue_in_session(
                    db_session,
                    "voice.transcribe",
                    {
                        "workspace_id": voice.workspace_id,
                        "voice_id": voice_id,
                        "sample_id": sample_id,
                        "sample_artifact_id": sample.artifact_id,
                        **captured,
                    },
                    resource_keys=stt_resource_keys(captured["settings"]),
                    workspace_id=voice.workspace_id,
                )
                result = {**_job_payload(job), "workspace_id": voice.workspace_id}
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return idempotency_failure(error)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 202

    @app.patch("/api/v1/voices/<voice_id>/samples/<sample_id>/transcript")
    @require_auth
    @voice_record_scope
    def voice_sample_transcript(voice_id: str, sample_id: str, *, workspace_id=None):
        payload = VoiceTranscriptReview.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        request_payload = {
            "voice_id": voice_id,
            "sample_id": sample_id,
            "request": payload.model_dump(mode="json"),
            "if_match": raw_etag,
        }
        access = context.guards.access_context()
        try:
            with database.immediate_session() as db_session:
                voice = require_voice_access(
                    db_session,
                    access,
                    voice_id,
                    workspace_id=workspace_id,
                    action=WorkspaceAction.EDIT,
                )
                legacy = credential_namespace(db_session, voice.workspace_id).legacy_host_access
                reservation = None
                if idempotency_key is not None:
                    principal = idempotency.principal()
                    if not legacy:
                        principal = replace(
                            principal,
                            subject=f"workspace:{voice.workspace_id}:user:{access.actor.user_id}",
                        )
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=principal,
                        operation_id="reviewVoiceSampleTranscript",
                        idempotency_key=idempotency_key,
                        payload=request_payload,
                    )
                    if reservation.response is not None:
                        previous, _status = reservation.response
                        if (not legacy and previous.get("workspace_id") != voice.workspace_id) or (
                            "workspace_id" in previous
                            and previous["workspace_id"] != voice.workspace_id
                        ):
                            raise WorkspaceNotFound()
                        return replay_idempotency(reservation, etag_key="voice_revision")

                def rejected(code, message, status):
                    if reservation is not None:
                        abandon_idempotency(db_session, reservation)
                    return error_response(code, message, status)

                sample = voice_sample_in_workspace(
                    db_session, sample_id, voice_id=voice_id, workspace_id=voice.workspace_id
                )
                if sample is None:
                    return rejected("not_found", "Voice sample not found.", 404)
                if is_bundled_voice(voice):
                    return rejected(
                        "bundled_voice_protected",
                        "The bundled reference transcript cannot be edited.",
                        409,
                    )
                expected_revision = payload.expected_voice_revision
                if expected_revision is None and raw_etag:
                    try:
                        expected_revision = int(raw_etag)
                    except ValueError:
                        return rejected(
                            "precondition_required",
                            "If-Match must contain the current voice revision.",
                            428,
                        )
                if expected_revision is None:
                    return rejected(
                        "precondition_required",
                        "Provide the current voice revision before editing a transcript.",
                        428,
                    )
                if voice.revision != expected_revision:
                    return rejected(
                        "revision_conflict", "The voice changed in another client.", 409
                    )
                sample.transcript = payload.transcript
                sample.transcript_language = payload.language
                sample.transcript_reviewed = True
                mark_provider_registrations_stale(
                    voice,
                    "The reviewed transcript changed.",
                    sample_id=sample.id,
                    reference_text_only=True,
                )
                voice.revision += 1
                voice.updated_at = utcnow()
                result = {
                    **voice_sample_payload(
                        db_session, paths, sample, voice_revision=voice.revision
                    ),
                    "workspace_id": voice.workspace_id,
                }
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="voice_sample",
                        resource_id=sample.id,
                    )
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["voice_revision"]}"'
        return response
