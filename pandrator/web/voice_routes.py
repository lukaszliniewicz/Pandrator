"""Local voices, reference samples, and provider voice lifecycle routes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

from flask import (
    jsonify,
    request,
)
from sqlalchemy import select

from pandrator.logic.audio_cpp_catalogue import package_metadata
from pandrator.logic.tts_provider_profiles import AUDIO_CPP_VOICE_DESIGN_MODELS

from .artifacts import sha256_file
from .credentials import (
    redact_inline_secrets,
)
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .managed_services import normalize_tts_provider_id
from .models import (
    AppSetting,
    Artifact,
    Job,
    Voice,
    VoiceSample,
    utcnow,
)
from .route_context import RouteContext
from .schemas import (
    VoiceCreate,
    VoiceDesignedSampleCreate,
    VoiceTranscriptReview,
    VoiceUpdate,
)
from .stt_resources import stt_resource_keys
from .voice_library import (
    ensure_bundled_voice,
    is_bundled_voice,
    mark_provider_registrations_stale,
    remove_managed_files,
    resolve_voice_sample_transcription_settings,
    retire_sample_artifact,
    sample_file_status,
    validate_profile_evidence,
    voice_payload,
    voice_payloads,
    voice_sample_payload,
)
from .voice_lifecycle_schemas import VoiceReferenceImportRequest
from .voice_recording_uploads import queue_voice_recording

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
    inspect_voice_idempotency = idempotency.inspect

    @app.get("/api/v1/voices")
    @require_auth
    def voice_list():
        ensure_bundled_voice(database, paths, artifacts)
        with database.session() as db_session:
            records = list(db_session.scalars(select(Voice).order_by(Voice.name)).all())
            return jsonify({"items": voice_payloads(db_session, paths, records)})


    @app.post("/api/v1/voices")
    @require_auth
    def voice_create():
        payload = VoiceCreate.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        name = payload.name.strip()
        if not name:
            return error_response("validation_error", "Voice name is required.", 422)
        request_payload = payload.model_dump(mode="json")
        replay = inspect_voice_idempotency(
            "createVoice",
            idempotency_key,
            request_payload,
            etag_key="revision",
        )
        if replay is not None:
            return replay
        with database.session() as db_session:
            try:
                validate_profile_evidence(db_session, paths, request_payload.get("profile"))
            except ValueError as error:
                return error_response("validation_error", str(error), 422)
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="createVoice",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation, etag_key="revision")
                    if replay is not None:
                        return replay
                    if db_session.scalar(select(Voice).where(Voice.name == name)) is not None:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "already_exists", "A voice with that name already exists.", 409
                        )
                    voice = Voice(
                        name=name,
                        language=str(payload.language or "").strip() or None,
                        description=str(payload.description or "").strip() or None,
                        metadata_json={
                            "voice_category": payload.voice_category,
                            **(
                                {"profile": payload.profile.model_dump(mode="json")}
                                if payload.profile
                                else {}
                            ),
                        },
                    )
                    db_session.add(voice)
                    db_session.flush()
                    result = voice_payload(db_session, paths, voice)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="voice",
                        resource_id=voice.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            response = jsonify(result)
            response.status_code = 201
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        with database.session() as db_session:
            if db_session.scalar(select(Voice).where(Voice.name == name)) is not None:
                return error_response(
                    "already_exists", "A voice with that name already exists.", 409
                )
            voice = Voice(
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
            result = voice_payload(db_session, paths, voice)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response, 201


    @app.patch("/api/v1/voices/<voice_id>")
    @require_auth
    def voice_update(voice_id: str):
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
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
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
                validate_profile_evidence(db_session, paths, changes.get("profile"))
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
                    select(Voice).where(Voice.name == name, Voice.id != voice.id)
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
            result = voice_payload(db_session, paths, voice)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response


    @app.delete("/api/v1/voices/<voice_id>")
    @require_auth
    def voice_delete(voice_id: str):
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
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
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
                    job
                    for job in db_session.scalars(
                        select(Job).where(
                            Job.status.in_(("queued", "running", "cancel_requested"))
                        )
                    ).all()
                    if f"voice:{voice_id}" in (job.resource_keys_json or [])
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
                    select(VoiceSample).where(VoiceSample.voice_id == voice.id)
                ).all()
            )
            for sample in samples:
                path = retire_sample_artifact(db_session, paths, sample)
                if path is not None:
                    removable.append(path)
                db_session.delete(sample)
            db_session.delete(voice)
        remove_managed_files(removable)
        return "", 204


    @app.get("/api/v1/voices/<voice_id>/samples")
    @require_auth
    def voice_sample_list(voice_id: str):
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
            records = list(
                db_session.scalars(
                    select(VoiceSample)
                    .where(VoiceSample.voice_id == voice_id)
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
                }
            )


    @app.post("/api/v1/voices/<voice_id>/samples/from-preview")
    @require_auth
    def voice_sample_from_preview(voice_id: str):
        payload = VoiceDesignedSampleCreate.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        idempotency_payload = {
            "voice_id": voice_id,
            **payload.model_dump(mode="json"),
        }
        replay = inspect_voice_idempotency(
            "createVoiceSampleFromPreview",
            idempotency_key,
            idempotency_payload,
            etag_key="voice_revision",
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
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
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
            artifact = db_session.get(Artifact, payload.artifact_id)
            if artifact is None or artifact.state == "deleted":
                return error_response(
                    "not_found",
                    "Preview artifact not found.",
                    404,
                )
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
        if idempotency_key is not None:
            request_payload = idempotency_payload
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="createVoiceSampleFromPreview",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                    current_voice = db_session.get(Voice, voice_id)
                    if current_voice is None:
                        abandon_idempotency(db_session, reservation)
                        return error_response("not_found", "Voice not found.", 404)
                    if is_bundled_voice(current_voice):
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "bundled_voice_protected",
                            "Samples cannot be added to the bundled reference voice.",
                            409,
                        )
                    if current_voice.revision != payload.expected_voice_revision:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "revision_conflict",
                            "The voice changed in another client.",
                            409,
                        )
                    job = jobs.enqueue_in_session(
                        db_session,
                        "voice.normalize_recording",
                        {
                            "voice_id": voice_id,
                            "source_artifact_id": artifact.id,
                            "source_artifact_sha256": preview_content_hash,
                            "source_artifact_role": "tts_voice_preview",
                            "expected_voice_revision": payload.expected_voice_revision,
                            "reviewed_transcript": transcript,
                            "transcript_language": language or preview_language,
                            "sample_provenance": preview_provenance,
                            "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
                        },
                        resource_keys=[f"voice:{voice_id}"],
                    )
                    result = _job_payload(job)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            return jsonify(result), 202
        job = jobs.enqueue(
            "voice.normalize_recording",
            {
                "voice_id": voice_id,
                "source_artifact_id": artifact.id,
                "source_artifact_sha256": preview_content_hash,
                "source_artifact_role": "tts_voice_preview",
                "expected_voice_revision": payload.expected_voice_revision,
                "reviewed_transcript": transcript,
                "transcript_language": language or preview_language,
                "sample_provenance": preview_provenance,
                "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
            },
            resource_keys=[f"voice:{voice_id}"],
        )
        return jsonify(_job_payload(job)), 202


    @app.post("/api/v1/voices/<voice_id>/samples/from-artifact")
    @require_auth
    def voice_sample_from_artifact(voice_id: str):
        payload = VoiceReferenceImportRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        idempotency_payload = {
            "voice_id": voice_id,
            **payload.model_dump(mode="json"),
        }
        replay = inspect_voice_idempotency(
            "createVoiceSampleFromArtifact",
            idempotency_key,
            idempotency_payload,
            etag_key="voice_revision",
        )
        if replay is not None:
            return replay
        transcript = str(payload.transcript or "").strip()
        language = str(payload.language or "").strip() or None

        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
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
            artifact = db_session.get(Artifact, payload.artifact_id)
            if artifact is None or artifact.state == "deleted":
                return error_response(
                    "not_found", "Voice reference artifact not found.", 404
                )
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

        job_payload = {
            "voice_id": voice_id,
            "source_artifact_id": artifact.id,
            "source_artifact_sha256": artifact_hash,
            "source_artifact_role": artifact.role,
            "expected_voice_revision": payload.expected_voice_revision,
            "transcript_language": language,
            "sample_provenance": {
                "source_kind": "imported_voice_reference",
                "source_artifact_id": artifact.id,
            },
            "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
        }
        if payload.transcript_reviewed:
            job_payload["reviewed_transcript"] = transcript
        elif transcript:
            job_payload["unreviewed_transcript"] = transcript
        if idempotency_key is not None:
            request_payload = idempotency_payload
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="createVoiceSampleFromArtifact",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                    current_voice = db_session.get(Voice, voice_id)
                    if current_voice is None:
                        abandon_idempotency(db_session, reservation)
                        return error_response("not_found", "Voice not found.", 404)
                    if is_bundled_voice(current_voice):
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "bundled_voice_protected",
                            "Samples cannot be added to the bundled reference voice.",
                            409,
                        )
                    if current_voice.revision != payload.expected_voice_revision:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "revision_conflict",
                            "The voice changed in another client.",
                            409,
                        )
                    job = jobs.enqueue_in_session(
                        db_session,
                        "voice.normalize_recording",
                        job_payload,
                        resource_keys=[f"voice:{voice_id}"],
                    )
                    result = _job_payload(job)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            return jsonify(result), 202
        job = jobs.enqueue(
            "voice.normalize_recording",
            job_payload,
            resource_keys=[f"voice:{voice_id}"],
        )
        return jsonify(_job_payload(job)), 202


    @app.post("/api/v1/voices/<voice_id>/samples")
    @require_auth
    def voice_sample_upload(voice_id: str):
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
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
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
        )
        return jsonify(_job_payload(job)), 202


    @app.post("/api/v1/voices/<voice_id>/samples/<sample_id>/replace")
    @require_auth
    def voice_sample_replace(voice_id: str, sample_id: str):
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
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            sample = db_session.get(VoiceSample, sample_id)
            if voice is None or sample is None or sample.voice_id != voice_id:
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
            replace_sample_id=sample_id,
        )
        return jsonify(_job_payload(job)), 202


    @app.delete("/api/v1/voices/<voice_id>/samples/<sample_id>")
    @require_auth
    def voice_sample_delete(voice_id: str, sample_id: str):
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
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            sample = db_session.get(VoiceSample, sample_id)
            if voice is None or sample is None or sample.voice_id != voice_id:
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
        remove_managed_files(removable)
        response = jsonify(
            {"id": sample_id, "status": "deleted", "voice_revision": next_revision}
        )
        response.headers["ETag"] = f'"{next_revision}"'
        return response


    @app.post("/api/v1/voices/<voice_id>/providers/<service_id>")
    @require_auth
    def voice_publish_to_provider(voice_id: str, service_id: str):
        """Upload a managed reference to a supported cloning provider."""
        from pandrator.logic import tts_handler

        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current voice revision.",
                428,
            )
        idempotency_payload = {
            "voice_id": voice_id,
            "service_id": service_id,
            "expected_revision": expected_revision,
        }
        replay = inspect_voice_idempotency(
            "publishVoiceToProvider",
            idempotency_key,
            idempotency_payload,
            etag_key="voice_revision",
        )
        if replay is not None:
            return replay
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
            if idempotency_key is None and voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            voice_samples = list(
                db_session.scalars(
                    select(VoiceSample)
                    .where(VoiceSample.voice_id == voice_id)
                    .order_by(VoiceSample.created_at.desc())
                ).all()
            )
            sample_count = sum(
                sample_file_status(db_session, paths, item)[0] == "ready"
                for item in voice_samples
            )
            preferred_sample = next(
                (
                    item
                    for item in voice_samples
                    if sample_file_status(db_session, paths, item)[0] == "ready"
                ),
                None,
            )
            connections = db_session.get(AppSetting, "services.tts")
            defaults = db_session.get(AppSetting, "defaults.tts")
            connection_value = (
                dict(connections.value_json or {})
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            default_value = (
                dict(defaults.value_json or {})
                if defaults and isinstance(defaults.value_json, dict)
                else {}
            )
        if not sample_count:
            return error_response(
                "missing_sample",
                "Add or replace a readable voice sample before uploading this voice.",
                422,
            )
        service = tts_handler.get_service_config(
            {**default_value, **connection_value}, service_id
        )
        if service is None:
            return error_response("not_found", "TTS service not found.", 404)
        if not bool(service.get("supports_voice_cloning")):
            return error_response(
                "unsupported",
                "This TTS service does not support managed voice uploads.",
                422,
            )
        resolved_service_id = str(service.get("id") or service_id)
        if (
            str(service.get("voice_reference_text") or "ignored") == "required"
            and preferred_sample is not None
            and not (
                preferred_sample.transcript_reviewed
                and str(preferred_sample.transcript or "").strip()
            )
        ):
            return error_response(
                "reviewed_transcript_required",
                f"{service.get('name') or resolved_service_id} requires a reviewed "
                "sample transcript before this voice can be used.",
                422,
            )
        job_payload = {
            "voice_id": voice_id,
            "service_id": resolved_service_id,
            "service": str(service.get("name") or resolved_service_id),
            "expected_voice_revision": expected_revision,
        }
        resource_keys = [
            f"voice:{voice_id}",
            f"service:tts:{resolved_service_id}",
        ]
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="publishVoiceToProvider",
                            idempotency_key=idempotency_key,
                            payload=idempotency_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                    current_voice = db_session.get(Voice, voice_id)
                    if current_voice is None:
                        abandon_idempotency(db_session, reservation)
                        return error_response("not_found", "Voice not found.", 404)
                    if current_voice.revision != expected_revision:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "revision_conflict",
                            "The voice changed in another client.",
                            409,
                        )
                    job = jobs.enqueue_in_session(
                        db_session,
                        "voice.publish",
                        job_payload,
                        resource_keys=resource_keys,
                    )
                    result = _job_payload(job)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            return jsonify(result), 202
        job = jobs.enqueue(
            "voice.publish",
            job_payload,
            resource_keys=resource_keys,
        )
        return jsonify(_job_payload(job)), 202


    @app.delete("/api/v1/voices/<voice_id>/providers/<service_id>")
    @require_auth
    def voice_remove_from_provider(voice_id: str, service_id: str):
        """Delete a Pandrator-owned voice copy from a configured provider."""
        from pandrator.logic import tts_handler

        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current voice revision.",
                428,
            )
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            if voice is None:
                return error_response("not_found", "Voice not found.", 404)
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
                )
            registration = dict(
                ((voice.metadata_json or {}).get("providers") or {}).get(service_id)
                or {}
            )
            connections = db_session.get(AppSetting, "services.tts")
            defaults = db_session.get(AppSetting, "defaults.tts")
            connection_value = (
                dict(connections.value_json or {})
                if connections and isinstance(connections.value_json, dict)
                else {}
            )
            default_value = (
                dict(defaults.value_json or {})
                if defaults and isinstance(defaults.value_json, dict)
                else {}
            )
        if not registration:
            return error_response(
                "not_found", "This voice is not registered with that service.", 404
            )
        if registration.get("managed_by") != "pandrator":
            return error_response(
                "legacy_registration",
                "This older registration has no ownership proof, so Pandrator will "
                "not delete it automatically.",
                409,
            )
        service = tts_handler.get_service_config(
            {**default_value, **connection_value}, service_id
        )
        if service is None:
            return error_response("not_found", "TTS service not found.", 404)
        service_adapter = (
            str(service.get("adapter") or "").strip().lower().replace("-", "_")
        )
        linked_reference = registration.get("resource_kind") == "linked_reference"
        if not bool(service.get("supports_voice_deletion")) and not (
            linked_reference and service_adapter == "audio_cpp"
        ):
            return error_response(
                "unsupported",
                "This TTS service does not advertise provider-side voice deletion.",
                422,
            )
        resolved_service_id = str(service.get("id") or service_id)
        job = jobs.enqueue(
            "voice.unpublish",
            {
                "voice_id": voice_id,
                "service_id": resolved_service_id,
                "service": str(service.get("name") or resolved_service_id),
                "expected_voice_revision": expected_revision,
            },
            resource_keys=[
                f"voice:{voice_id}",
                f"service:tts:{resolved_service_id}",
            ],
        )
        return jsonify(_job_payload(job)), 202


    @app.post("/api/v1/voices/<voice_id>/samples/<sample_id>/transcribe")
    @require_auth
    def voice_sample_transcribe(voice_id: str, sample_id: str):
        raw_settings = request.get_json(silent=True)
        settings = {} if raw_settings is None else raw_settings
        if not isinstance(settings, dict):
            return error_response(
                "validation_error", "Transcription settings must be an object.", 422
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="transcribeVoiceSample",
                            idempotency_key=idempotency_key,
                            payload={
                                "voice_id": voice_id,
                                "sample_id": sample_id,
                                "settings": settings,
                            },
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation)
                    if replay is not None:
                        return replay
                    voice = db_session.get(Voice, voice_id)
                    sample = db_session.get(VoiceSample, sample_id)
                    if voice is None or sample is None or sample.voice_id != voice_id:
                        abandon_idempotency(db_session, reservation)
                        return error_response("not_found", "Voice sample not found.", 404)
                    try:
                        resolved_settings = resolve_voice_sample_transcription_settings(
                            settings, voice.language
                        )
                    except ValueError as error:
                        abandon_idempotency(db_session, reservation)
                        return error_response("validation_error", str(error), 422)
                    job = jobs.enqueue_in_session(
                        db_session,
                        "voice.transcribe",
                        {
                            "voice_id": voice_id,
                            "sample_id": sample_id,
                            "sample_artifact_id": sample.artifact_id,
                            "settings": resolved_settings,
                        },
                        resource_keys=stt_resource_keys(resolved_settings),
                    )
                    result = _job_payload(job)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="job",
                        resource_id=job.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            return jsonify(result), 202
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            sample = db_session.get(VoiceSample, sample_id)
            if voice is None or sample is None or sample.voice_id != voice_id:
                return error_response("not_found", "Voice sample not found.", 404)
            artifact_id = sample.artifact_id
            settings = resolve_voice_sample_transcription_settings(
                settings, voice.language
            )
        job = jobs.enqueue(
            "voice.transcribe",
            {
                "voice_id": voice_id,
                "sample_id": sample_id,
                "sample_artifact_id": artifact_id,
                "settings": settings,
            },
            resource_keys=stt_resource_keys(settings),
        )
        return jsonify(_job_payload(job)), 202


    @app.patch("/api/v1/voices/<voice_id>/samples/<sample_id>/transcript")
    @require_auth
    def voice_sample_transcript(voice_id: str, sample_id: str):
        payload = VoiceTranscriptReview.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if idempotency_key is not None:
            request_payload = {
                "voice_id": voice_id,
                "sample_id": sample_id,
                "request": payload.model_dump(mode="json"),
                "if_match": raw_etag,
            }
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=idempotency.principal(),
                            operation_id="reviewVoiceSampleTranscript",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    replay = replay_idempotency(reservation, etag_key="voice_revision")
                    if replay is not None:
                        return replay
                    voice = db_session.get(Voice, voice_id)
                    sample = db_session.get(VoiceSample, sample_id)
                    if voice is None or sample is None or sample.voice_id != voice_id:
                        abandon_idempotency(db_session, reservation)
                        return error_response("not_found", "Voice sample not found.", 404)
                    if is_bundled_voice(voice):
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "bundled_voice_protected",
                            "The bundled reference transcript cannot be edited.",
                            409,
                        )
                    expected_revision = payload.expected_voice_revision
                    if expected_revision is None and raw_etag:
                        try:
                            expected_revision = int(raw_etag)
                        except ValueError:
                            abandon_idempotency(db_session, reservation)
                            return error_response(
                                "precondition_required",
                                "If-Match must contain the current voice revision.",
                                428,
                            )
                    if expected_revision is None:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "precondition_required",
                            "Provide the current voice revision before editing a transcript.",
                            428,
                        )
                    if voice.revision != expected_revision:
                        abandon_idempotency(db_session, reservation)
                        return error_response(
                            "revision_conflict",
                            "The voice changed in another client.",
                            409,
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
                    result = voice_sample_payload(
                        db_session,
                        paths,
                        sample,
                        voice_revision=voice.revision,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="voice_sample",
                        resource_id=sample.id,
                    )
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                return idempotency_failure(error)
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["voice_revision"]}"'
            return response
        with database.session() as db_session:
            voice = db_session.get(Voice, voice_id)
            sample = db_session.get(VoiceSample, sample_id)
            if voice is None or sample is None or sample.voice_id != voice_id:
                return error_response("not_found", "Voice sample not found.", 404)
            if is_bundled_voice(voice):
                return error_response(
                    "bundled_voice_protected",
                    "The bundled reference transcript cannot be edited.",
                    409,
                )
            expected_revision = payload.expected_voice_revision
            if expected_revision is None and raw_etag:
                try:
                    expected_revision = int(raw_etag)
                except ValueError:
                    return error_response(
                        "precondition_required",
                        "If-Match must contain the current voice revision.",
                        428,
                    )
            if expected_revision is None:
                return error_response(
                    "precondition_required",
                    "Provide the current voice revision before editing a transcript.",
                    428,
                )
            if voice.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The voice changed in another client.",
                    409,
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
            result = voice_sample_payload(
                db_session,
                paths,
                sample,
                voice_revision=voice.revision,
            )
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["voice_revision"]}"'
        return response

