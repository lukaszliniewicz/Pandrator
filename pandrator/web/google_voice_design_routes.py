"""Authenticated workspace admission for native Google prompted voices."""

from __future__ import annotations

import hashlib
from dataclasses import replace
from functools import wraps
from typing import Any

from flask import jsonify, request
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .access.contracts import AccessContext, AccessDenied, WorkspaceNotFound
from .access.job_policy import JobAction, JobNotFound, require_job_access
from .access.workspace_policy import WorkspaceAction, require_workspace_access
from .accounts.actor_authorization import require_actor_in_session
from .accounts.execution_actor import execution_actor_scope
from .credential_identity import credential_namespace
from .domain_blueprints import DomainBlueprints
from .google_voice_design import configured_google_design_service, google_voice_scope
from .google_voice_design_schemas import GoogleVoiceDesignCreate
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .models import Job, Voice
from .route_context import RouteContext
from .speech_catalogue_access import CatalogueChanged, SpeechScopeInvalid, scope_identifier
from .voice_provider_settings import capture_voice_provider_settings, check_voice_provider_settings
from .voice_queries import voice_name_owner


def register_google_voice_design_routes(app: DomainBlueprints, context: RouteContext) -> None:
    services = context.services
    database = services.database
    error_response = context.guards.error_response
    require_auth = context.guards.require_auth
    idempotency = MutationIdempotency(context)

    def scope_errors(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            try:
                return function(*args, **kwargs)
            except (WorkspaceNotFound, JobNotFound):
                return error_response("not_found", "Voice design workspace or work not found.", 404)
            except AccessDenied:
                return error_response("permission_denied", "The requested action is not permitted.", 403)
        return wrapped

    def prove_workspace(session: Session, access: AccessContext, workspace_id: str | None):
        with session.no_autoflush:
            try:
                require_actor_in_session(session, access.actor,
                    required_scopes=("app.read", "app.write", "app.run"))
            except PermissionError:
                raise AccessDenied() from None
            return require_workspace_access(session, access, workspace_id, action=WorkspaceAction.EDIT)

    def begin_receipt(session: Session, access: AccessContext, workspace_id: str, key: str, body: dict[str, Any]):
        principal = idempotency.principal()
        legacy = credential_namespace(session, workspace_id).legacy_host_access
        if not legacy:
            principal = replace(principal, subject=f"workspace:{workspace_id}:user:{access.actor.user_id}")
        reservation = services.idempotency.begin(session, principal=principal,
            operation_id="designGoogleVoice", idempotency_key=key, payload=body)
        if reservation.response is not None:
            response, _status = reservation.response
            if (not legacy and response.get("workspace_id") != workspace_id) or (
                "workspace_id" in response and response["workspace_id"] != workspace_id):
                raise WorkspaceNotFound()
            job_id = response.get("id")
            if type(job_id) is not str or session.scalar(select(Job.id).where(
                Job.id == job_id, Job.kind == "voice.google_design", Job.workspace_id == workspace_id,
                Job.project_id.is_(None), Job.session_id.is_(None), Job.workflow_run_id.is_(None),
            )) is None:
                raise JobNotFound()
            require_job_access(session, access, job_id, JobAction.RUN)
        return reservation

    @app.post("/api/v1/voices/design/google")
    @require_auth
    @scope_errors
    def design_google_voice():
        if "session_id" in request.args:
            return error_response("validation_error", "Session scope is not available for voice design.", 422)
        try:
            workspace_hint = scope_identifier(request.args.get("workspace_id"))
            payload = GoogleVoiceDesignCreate.model_validate(request.get_json(silent=True) or {})
        except (ValidationError, SpeechScopeInvalid):
            return error_response("validation_error", "Invalid Google voice design request.", 422)
        request_body = payload.model_dump(mode="json")
        inline_secret_error = context.guards.inline_credential_error(request_body)
        if inline_secret_error is not None:
            return inline_secret_error
        key = str(request.headers.get("Idempotency-Key") or "").strip()
        if not key:
            return error_response("idempotency_key_required", "Idempotency-Key is required for Google voice design.", 400)
        access = context.guards.access_context()
        try:
            # Retry proof precedes mutable Voice/configuration reads, including
            # retries after the original voice has been removed.
            with database.immediate_session() as session:
                workspace = prove_workspace(session, access, workspace_hint)
                reservation = begin_receipt(session, access, workspace.id, key, request_body)
                replay = idempotency.replay(reservation)
                if replay is not None:
                    return replay
                idempotency.abandon(session, reservation)
            with database.snapshot_session() as session:
                workspace = prove_workspace(session, access, workspace_hint)
                workspace_id = workspace.id
                if voice_name_owner(session, payload.name, workspace_id=workspace_id) is not None:
                    return error_response("already_exists", "A voice with that name already exists.", 409)
                captured = capture_voice_provider_settings(session, workspace_id)
                host_allowed = credential_namespace(session, workspace_id).host_credentials_allowed

            def fence():
                with database.snapshot_session() as session:
                    prove_workspace(session, access, workspace_id)
                    if (
                        credential_namespace(session, workspace_id).host_credentials_allowed
                        != host_allowed
                    ):
                        raise CatalogueChanged()
                    try:
                        check_voice_provider_settings(session, captured, workspace_id)
                    except ValueError:
                        raise CatalogueChanged() from None

            fence()
            service, credential = configured_google_design_service(database, services.paths,
                payload.service_id, workspace_id=workspace_id)
            fence()
            if payload.model not in service.get("models", []):
                raise ValueError("The selected Google TTS model is not configured.")
            scope = google_voice_scope(
                payload.service_id,
                service,
                credential,
                allow_host_adc=host_allowed,
                before_io=fence,
            )
            fence()
            credential_hash = hashlib.sha256(credential.encode("utf-8")).hexdigest()
        except CatalogueChanged as error:
            return error_response("configuration_changed", str(error), 409)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency.failure(error)

        try:
            with database.immediate_session() as session:
                workspace = prove_workspace(session, access, workspace_hint)
                reservation = begin_receipt(session, access, workspace.id, key, request_body)
                replay = idempotency.replay(reservation)
                if replay is not None:
                    return replay
                if workspace.id != workspace_id:
                    raise WorkspaceNotFound()
                try:
                    check_voice_provider_settings(session, captured, workspace_id)
                except ValueError as error:
                    idempotency.abandon(session, reservation)
                    return error_response("configuration_changed", str(error), 409)
                if voice_name_owner(session, payload.name, workspace_id=workspace_id) is not None:
                    idempotency.abandon(session, reservation)
                    return error_response("already_exists", "A voice with that name already exists.", 409)
                voice = Voice(workspace_id=workspace_id, name=payload.name,
                    language=payload.language, description=payload.prompt,
                    metadata_json={"voice_category": payload.voice_category,
                        "google_voice_design": {"state": "queued", "service_id": payload.service_id,
                            "model": payload.model, "prompt": payload.prompt,
                            "scope_fingerprint": scope["fingerprint"], "project": scope["project"]}})
                session.add(voice)
                session.flush()
                job_payload: dict[str, Any] = {"workspace_id": workspace_id, "voice_id": voice.id,
                    "service_id": payload.service_id, "model": payload.model, "name": payload.name,
                    "prompt": payload.prompt, "language": payload.language,
                    "scope_fingerprint": scope["fingerprint"], "project": scope["project"],
                    "google_credential_sha256": credential_hash, **captured}
                with execution_actor_scope(access.actor):
                    job = services.jobs.enqueue_in_session(session, "voice.google_design", dict(job_payload),
                        max_attempts=1, resource_keys=[f"voice:{voice.id}", f"service:tts:{payload.service_id}"],
                        workspace_id=workspace_id)
                job.payload_json = {**job_payload, "job_id": job.id}
                metadata = dict(voice.metadata_json or {})
                metadata["google_voice_design"] = {**metadata["google_voice_design"], "job_id": job.id}
                voice.metadata_json = metadata
                session.flush()
                receipt = {**_job_payload(job), "voice_id": voice.id, "workspace_id": workspace_id}
                services.idempotency.complete(session, reservation, response=receipt, status_code=202,
                    resource_kind="voice", resource_id=voice.id)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency.failure(error)
        except IntegrityError:
            return error_response("already_exists", "A voice with that name already exists.", 409)
        return jsonify(receipt), 202
