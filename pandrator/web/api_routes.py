"""HTTP route definitions for the browser and API clients."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import time
import uuid
from datetime import timedelta
from pathlib import Path
from typing import Any

from flask import (
    Flask,
    Response,
    g,
    jsonify,
    request,
    send_file,
    send_from_directory,
    session,
)
from pydantic import ValidationError
from sqlalchemy import select
from werkzeug.utils import secure_filename

from pandrator.version import PANDRATOR_VERSION

from .access.agent_run_policy import require_current_agent_run_access
from .access.artifact_policy import ArtifactNotFound, require_artifact_access
from .access.contracts import AccessDenied, ProjectAction
from .access.current_session_policy import require_current_session_access
from .access.job_policy import JobAction, JobNotFound, job_visibility_clause, require_job_access
from .access.local_tools_policy import (
    LocalToolOperation,
    require_local_tool_inputs,
    require_local_tool_workspace,
)
from .access.session_policy import require_session_access
from .access.session_presentation import session_access_payloads
from .access.session_queries import list_session_records
from .access.upload_policy import require_upload_creation_access
from .account_routes import register_account_routes
from .accounts.actor_authorization import require_actor_in_session
from .accounts.administration import PRIVILEGED_JOB_SCOPES, AdministratorRequired
from .accounts.auth_state import account_state_in_session
from .accounts.browser_auth import establish_browser_session, revoke_browser_session
from .accounts.credential_resources import default_personal_resources, require_delegation_resources
from .accounts.execution_actor import execution_actor_scope
from .accounts.installation_mode import installation_mode_in_session
from .agent_run_commands import AgentRunAdmissionError, AgentRunStateError
from .artifact_routes import register_artifact_routes
from .artifact_selection import (
    choose_artifact,
    clear_selection,
    rerun_impact,
    stage_history,
    trash_stage_artifact,
)
from .auth import ALL_SCOPES, MCP_BOOTSTRAP_SCOPES, LoginThrottle, normalize_scopes
from .automation_routes import register_automation_routes
from .credentials import (
    redact_inline_secrets,
)
from .dispatch_routes import register_dispatch_routes
from .domain_blueprints import DomainBlueprints
from .evidence_access import (
    EvidenceRevisionConflict,
    EvidenceRevisionRequired,
    authorize_evidence_request,
    evidence_revision,
    get_evidence_in_session,
    list_evidence_in_session,
    require_evidence_access,
    require_evidence_revision,
)
from .generation_routes import register_generation_routes
from .google_voice_design_routes import register_google_voice_design_routes
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .http_serialization import job_projects
from .http_serialization import model_payload as _model_dict
from .http_session_access import require_session_action
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .job_commands import (
    BundleExportJobPayload,
    enqueue_bundle_export_in_session,
    enqueue_bundle_import_in_session,
    enqueue_diagnostic_in_session,
    parse_diagnostic_payload,
)
from .job_inputs import artifact_job_ownership
from .managed_services import binding_for_provider, normalize_tts_provider_id
from .manager_proxy import register_manager_routes
from .media_edit_dispatch_routes import register_media_edit_dispatch_routes
from .media_edit_routes import register_media_edit_routes
from .models import (
    AgentRun,
    AgentStep,
    ApiIdempotency,
    SubtitleEvidence,
    utcnow,
)
from .openapi import build_openapi_document
from .parameter_definitions import describe_parameters
from .parity_registry import build_registry
from .provider_routes import register_provider_routes
from .quick_transcription_routes import register_quick_transcription_routes
from .route_context import RouteContext
from .schemas import (
    AgentRunCreateRequest,
    BootstrapRequest,
    BundleExportRequest,
    BundleImportRequest,
    ChunkUploadInitialize,
    JobCreate,
    LoginRequest,
    ManagerBootstrapRequest,
    OutcomePlanUpdate,
    PdfEditRequest,
    PronunciationCreate,
    PronunciationUpdate,
    RvcConvertRequest,
    RvcModelUploadRequest,
    StageSelectionUpdate,
    SubtitleEvidenceCreateRequest,
    SubtitleEvidenceResolveRequest,
    SubtitlePassageReviewRequest,
    SubtitleReviewRequest,
    TokenCreateRequest,
)
from .service_routes import register_service_routes
from .session_routes import (
    register_session_lifecycle_routes,
    register_session_list_routes,
)
from .session_settings_routes import register_session_settings_routes
from .settings_identity import settings_workspace_id, workspace_setting_in_workspace
from .settings_policy import RevisionConflict as WorkspaceRevisionConflict
from .settings_routes import register_settings_routes
from .source_cleaning_dispatch_routes import (
    register_source_cleaning_dispatch_routes,
)
from .source_routes import (
    register_source_document_routes,
    register_source_ingestion_routes,
    register_source_library_routes,
)
from .speech_optimization_dispatch_routes import (
    register_speech_optimization_dispatch_routes,
)
from .training_routes import register_training_routes
from .transcription_readiness_routes import register_transcription_readiness_routes
from .upload_publication import publish_multipart_upload
from .uploads import cleanup_initialized_directories
from .voice_routes import register_voice_routes
from .workflow_improvements_routes import register_workflow_improvements_routes
from .workflow_plan_routes import register_workflow_plan_routes
from .workflow_routes import (
    WorkflowRouteContext,
    register_workflow_job_routes,
    register_workflow_session_routes,
)
from .workspace_recovery_routes import register_workspace_recovery_routes
from .workspace_routes import register_workspace_routes


def _is_loopback_address(value: object) -> bool:
    candidate = str(value or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped and mapped.is_loopback))


def _session_payload(record) -> dict[str, Any]:
    return _model_dict(
        record,
        (
            "id",
            "project_id",
            "name",
            "storage_key",
            "workflow_kind",
            "interface_mode",
            "source_language",
            "target_language",
            "workflow_preset",
            "included_stages_json",
            "status",
            "revision",
            "created_at",
            "updated_at",
            "trashed_at",
            "purge_after",
        ),
    )


SSE_EVENT_FIELDS = {
    "job_kind",
    "session_id",
    "workflow_run_id",
    "generation_run_id",
    "output_assembly_id",
    "source_id",
    "source_asset_id",
    "source_artifact_id",
    "artifact_id",
    "agent_run_id",
    "training_id",
    "training_run_id",
    "voice_id",
    "sample_id",
    "upload_id",
    "document_id",
    "model_id",
    "status",
    "progress",
    "detail",
    "code",
    "reason",
    "retry_after_ms",
    "changed_entities",
}


def _sse_event_payload(event) -> dict[str, Any]:
    """Project a durable event onto the small, secret-free browser contract."""
    source = event.data if isinstance(event.data, dict) else {}
    payload = {key: source[key] for key in SSE_EVENT_FIELDS if key in source}
    payload["job_id"] = event.work_id
    payload["created_at"] = event.created_at.isoformat()
    return redact_inline_secrets(payload)


def register_routes(flask_app: Flask, context: RouteContext) -> None:
    """Register route handlers on Blueprints grouped by backend domain."""

    services = context.services
    app = DomainBlueprints(flask_app)
    paths = services.paths
    migration = services.migration
    database = services.database
    auth = services.auth
    login_throttle = services.login_throttle
    account_login_throttle = LoginThrottle(max_attempts=20, initial_block_seconds=30)
    capability_service = services.capabilities
    jobs = services.jobs
    work = services.work
    sessions = services.sessions
    artifacts = services.artifacts
    workflows = services.workflows
    workflow_handlers = services.workflow_handlers
    outcome_plans = services.outcome_plans
    source_library = services.source_library
    pronunciations = services.pronunciations
    chunk_uploads = services.chunk_uploads
    subtitle_review = services.subtitle_review
    subtitle_evidence = services.subtitle_evidence
    bootstrap = services.bootstrap
    static_dir = context.static_dir
    error_response = context.guards.error_response
    inline_credential_error = context.guards.inline_credential_error
    require_auth = context.guards.require_auth


    idempotency = MutationIdempotency(context)
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    replay_idempotency = idempotency.replay

    def enrich_manager_plan(
        manager_plan: dict[str, Any],
        requested_plan: dict[str, Any],
    ) -> dict[str, Any]:
        desired = requested_plan.get("desired")
        if not isinstance(desired, dict):
            return manager_plan
        removals = {
            str(component_id)
            for component_id, state in desired.items()
            if isinstance(state, dict) and state.get("present") is False
        }
        if not removals:
            return manager_plan
        with database.session() as db_session:
            workspace_id = settings_workspace_id(db_session)
            setting = workspace_setting_in_workspace(db_session, workspace_id, "services.tts")
            value = (
                dict(setting.value_json)
                if setting is not None and isinstance(setting.value_json, dict)
                else {}
            )
        selected_provider = normalize_tts_provider_id(
            value.get("service") or value.get("tts_service")
        )
        impacts: list[dict[str, Any]] = []
        for record in value.get("provider_configs") or []:
            if not isinstance(record, dict):
                continue
            provider_id = normalize_tts_provider_id(
                record.get("id") or record.get("name") or record.get("provider")
            )
            binding = binding_for_provider(provider_id)
            if (
                binding is None
                or binding.component_id not in removals
                or str(record.get("connection_mode") or "external") != "managed_local"
            ):
                continue
            impacts.append(
                {
                    "kind": "managed_tts_binding",
                    "component_id": binding.component_id,
                    "provider_id": binding.provider_id,
                    "service_id": binding.service_id,
                    "label": str(record.get("name") or binding.provider_id),
                    "selected_default": provider_id == selected_provider,
                    "message": (
                        f"{record.get('name') or binding.provider_id} is "
                        "configured to use this managed local component. "
                        "Switch it to an external endpoint or reinstall the "
                        "component before generating speech."
                    ),
                }
            )
        if not impacts:
            return manager_plan
        enriched = dict(manager_plan)
        enriched["application_impacts"] = {
            "managed_provider_bindings": impacts,
        }
        return enriched

    register_manager_routes(
        app,
        require_auth=require_auth,
        error_response=error_response,
        proxy=services.manager_bridge,
        plan_response_transform=enrich_manager_plan,
    )
    register_workspace_routes(app, context)
    register_workspace_recovery_routes(app, context)
    register_automation_routes(app, context)
    register_workflow_plan_routes(app, context)
    register_media_edit_routes(app, context)
    register_media_edit_dispatch_routes(app, context)
    register_dispatch_routes(app, context)
    register_workflow_improvements_routes(app, context)
    register_source_cleaning_dispatch_routes(app, context)
    register_speech_optimization_dispatch_routes(app, context)
    from .performance_routes import register_performance_routes

    register_performance_routes(app, context)
    from .generation_control_routes import register_generation_control_routes

    register_generation_control_routes(app, context)
    from .generation_recast_routes import register_generation_recast_routes

    register_generation_recast_routes(app, context)
    from .audiobook_routes import register_audiobook_routes
    from .speech_selection_routes import register_speech_selection_routes

    register_audiobook_routes(app, context)
    from .session_purge_routes import register_session_purge_routes

    register_session_purge_routes(app, context)
    from .comparison_export_routes import register_comparison_export_routes
    from .session_purge_job_routes import register_session_purge_job_routes

    register_session_purge_job_routes(app, context)
    register_comparison_export_routes(app, context)
    register_speech_selection_routes(app, context)
    from .voice_catalog_routes import register_voice_catalog_routes

    register_voice_catalog_routes(app, context)
    from .audio_import_routes import register_audio_import_routes
    from .sound_job_routes import register_sound_job_routes
    from .sound_project_routes import register_project_sound_routes
    from .sound_routes import register_sound_routes

    register_sound_routes(app, context)
    register_project_sound_routes(app, context)
    register_sound_job_routes(app, context)
    register_audio_import_routes(app, context)
    register_voice_routes(app, context)
    register_google_voice_design_routes(app, context)
    register_quick_transcription_routes(app, context)
    register_transcription_readiness_routes(app, context)

    @app.get("/api/v1/health")
    def health():
        return jsonify(
            {
                "status": "ok",
                "service": "pandrator",
                "version": PANDRATOR_VERSION,
                "protocol_version": "v1",
                "database": paths.database.name,
                "migration": migration.get("status"),
            }
        )

    @app.get("/api/v1/system/identity")
    @require_auth
    def system_identity():
        identity = services.identity.snapshot(observed_origin=request.url_root)
        return jsonify(identity.model_dump(mode="json"))

    @app.get("/api/v1/parameter-definitions")
    @require_auth
    def parameter_definitions():
        raw_limit = request.args.get("limit")
        if raw_limit is None:
            limit = 100
        else:
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                return error_response(
                    "validation_error",
                    "limit must be an integer from 1 through 300",
                    422,
                )
        try:
            payload = describe_parameters(
                sections=request.args.getlist("section"),
                names=request.args.getlist("name"),
                workflow_kind=request.args.get("workflow_kind"),
                query=request.args.get("query"),
                limit=limit,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(payload)

    @app.get("/api/v1/openapi.json")
    def openapi():
        return jsonify(build_openapi_document())

    @app.get("/api/v1/auth/status")
    def auth_status():
        principal = context.guards.principal()
        account = None
        if principal is not None and principal.user_id is not None:
            try:
                with database.snapshot_session() as account_session:
                    account = account_state_in_session(
                        account_session, principal.user_id,
                        auth_revision=principal.auth_revision,
                    )
            except PermissionError:
                principal = None
        with database.snapshot_session() as mode_session:
            access_mode = installation_mode_in_session(mode_session)
        public_links = context.guards.app.extensions["pandrator_public_links"].status()
        guest = context.guards.project_link_context()
        remote_access = not _is_loopback_address(request.remote_addr)
        warning = ""
        if remote_access:
            warning = (
                "Remote access is active. Use an HTTPS reverse proxy and a strong, unique owner password."
                if request.is_secure
                else "Remote access is using plain HTTP. Put Pandrator behind HTTPS before sending passwords or provider credentials."
            )
        return jsonify(
            {
                "instance_id": services.identity.instance_id,
                "installation": access_mode.payload(),
                "initialized": auth.initialized(),
                "authenticated": principal is not None,
                "csrf_token": session.get("csrf_token")
                if principal is not None and principal.browser_session_id is not None and guest is None
                else None,
                "principal": (
                    {
                        "subject": principal.subject,
                        "kind": principal.kind,
                        "scopes": sorted(principal.scopes),
                        "client_id": principal.client_id,
                        "user_id": principal.user_id,
                        "auth_revision": principal.auth_revision,
                        "display_name": guest.link["label"] if guest else (account.display_name if account else None),
                        "login_name": None if guest else (account.login_name if account else None),
                        "role": (
                            "administrator" if account.is_instance_admin and guest is None else "user"
                        ) if account else None,
                    }
                    if principal is not None
                    else None
                ),
                "remote_access": remote_access,
                "secure_transport": bool(request.is_secure),
                "security_warning": warning,
                "public_links": public_links,
                "project_link": guest.link if guest else None,
            }
        )

    @app.post("/api/v1/auth/bootstrap")
    def auth_bootstrap():
        payload = BootstrapRequest.model_validate(request.get_json(silent=True) or {})
        grant = bootstrap.consume_grant(payload.token)
        if grant is not None and grant.kind == "owner_session" and auth.collaboration_enabled():
            return error_response("login_required", "Sign in with your user account.", 401)
        if grant is None:
            return error_response(
                "invalid_bootstrap_token",
                "The local bootstrap token is invalid or expired.",
                401,
            )
        try:
            if grant.user_id is not None and grant.auth_revision is not None:
                with services.database.session() as db_session:
                    account = account_state_in_session(
                        db_session, grant.user_id, auth_revision=grant.auth_revision
                    )
            elif grant.subject == "owner" and grant.kind == "owner_session":
                # A local launch token can precede application construction.
                account = auth.initial_account()
            else:
                raise PermissionError("The bootstrap account identity is missing.")
            if not auth.solo_access_allowed(account.user_id, account.auth_revision):
                raise PermissionError("Named-account access is not enabled.")
            establish_browser_session(
                auth, account, subject=grant.subject, principal_kind=grant.kind,
                scopes=grant.scopes, client_id=grant.client_id,
            )
        except (PermissionError, ValueError):
            return error_response(
                "invalid_bootstrap_token", "The local bootstrap token is invalid or expired.", 401
            )
        return jsonify({"authenticated": True, "csrf_token": session["csrf_token"]})

    def manager_authentication_error():
        """Validate the Manager's loopback-only launch credential."""

        if not _is_loopback_address(request.remote_addr):
            return error_response(
                "manager_authentication_failed",
                "The manager launch credential is invalid.",
                401,
            )
        credential_value = str(
            os.environ.get("PANDRATOR_MANAGER_CREDENTIAL") or ""
        ).strip()
        if not credential_value:
            return error_response(
                "manager_not_configured",
                "This Pandrator process was not started by Pandrator Manager.",
                503,
            )
        try:
            credential_path = Path(credential_value).expanduser().resolve(strict=True)
            if credential_path.stat().st_size > 4096:
                raise OSError("Manager credential file is unexpectedly large.")
            expected = credential_path.read_text(encoding="utf-8").strip()
        except OSError:
            return error_response(
                "manager_credential_unavailable",
                "The manager launch credential is unavailable.",
                503,
            )
        authorization = request.headers.get("Authorization", "")
        supplied = (
            authorization[7:].strip() if authorization.startswith("Bearer ") else ""
        )
        if (
            not supplied
            or not expected
            or not secrets.compare_digest(supplied, expected)
        ):
            return error_response(
                "manager_authentication_failed",
                "The manager launch credential is invalid.",
                401,
            )
        return None

    @app.post("/api/v1/auth/manager-browser-bootstrap")
    def auth_manager_browser_bootstrap():
        """Mint a full local-owner browser token for the trusted Manager."""

        authentication_error = manager_authentication_error()
        if authentication_error is not None:
            return authentication_error
        if auth.collaboration_enabled():
            return jsonify({"login_required": True})
        try:
            account = auth.initial_account()
        except PermissionError:
            return error_response("administrator_required", "The initial Administrator is inactive.", 403)
        return jsonify(
            {
                "token": bootstrap.issue(
                    subject="owner",
                    kind="owner_session",
                    scopes=ALL_SCOPES,
                    client_id="pandrator-manager-browser",
                    user_id=account.user_id,
                    auth_revision=account.auth_revision,
                ),
                "expires_in_seconds": 120,
                "scopes": sorted(ALL_SCOPES),
            }
        )

    @app.post("/api/v1/auth/manager-bootstrap")
    def auth_manager_bootstrap():
        """Mint a least-privilege automation token for the loopback Manager."""

        payload = ManagerBootstrapRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        authentication_error = manager_authentication_error()
        if authentication_error is not None:
            return authentication_error
        try:
            account = auth.initial_account()
        except PermissionError:
            return error_response("administrator_required", "The initial Administrator is inactive.", 403)
        extra_policy = normalize_scopes(
            os.environ.get("PANDRATOR_MCP_BOOTSTRAP_EXTRA_SCOPES", ""),
            allow_empty=True,
        )
        allowed = MCP_BOOTSTRAP_SCOPES | (
            extra_policy
            & frozenset(
                {
                    "app.credentials.read",
                    "app.credentials.write",
                }
            )
        )
        selected_scopes = normalize_scopes(payload.scopes) & allowed
        if not selected_scopes:
            return error_response(
                "scope_denied",
                "None of the requested scopes are allowed for Manager bootstrap.",
                403,
            )
        manager_instance_id = services.identity.manager_instance_id or "local-manager"
        return jsonify(
            {
                "token": bootstrap.issue(
                    subject=f"manager:{manager_instance_id}",
                    kind="manager_bootstrap",
                    scopes=selected_scopes,
                    client_id="pandrator-mcp-local",
                    user_id=account.user_id,
                    auth_revision=account.auth_revision,
                ),
                "expires_in_seconds": 120,
                "scopes": sorted(selected_scopes),
            }
        )

    @app.post("/api/v1/auth/login")
    def auth_login():
        request.max_content_length = 8 * 1024
        payload = LoginRequest.model_validate(request.get_json(silent=True) or {})
        client_key = request.remote_addr or "unknown"
        collaboration = auth.collaboration_enabled()
        throttle_enabled = collaboration or not _is_loopback_address(client_key)
        account_key = (
            (payload.login_name or "").strip().casefold()
            if collaboration else "solo-owner"
        )
        retry_after = max(
            login_throttle.retry_after(client_key),
            account_login_throttle.retry_after(account_key),
        ) if throttle_enabled else 0
        if retry_after:
            context.guards.security_event("authentication_throttled", 429)
            response, status = error_response(
                "login_throttled",
                "Too many failed sign-in attempts. Try again later.",
                429,
                {"retry_after_seconds": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, status
        account = auth.authenticate_login(payload.login_name, payload.password)
        if account is None:
            context.guards.security_event("authentication_failed", 401)
            retry_after = (
                max(login_throttle.record_failure(client_key),
                    account_login_throttle.record_failure(account_key))
                if throttle_enabled else 0
            )
            response, status = error_response(
                "invalid_credentials",
                "The sign-in details are incorrect.",
                401,
                {"retry_after_seconds": retry_after} if retry_after else None,
            )
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response, status
        if throttle_enabled:
            login_throttle.reset(client_key)
            account_login_throttle.reset(account_key)
        try:
            establish_browser_session(
                auth, account, subject=f"user:{account.user_id}",
                principal_kind="owner_session", scopes=auth.browser_scopes(account)
            )
        except (PermissionError, ValueError):
            return error_response("invalid_credentials", "The sign-in details are incorrect.", 401)
        return jsonify({"authenticated": True, "csrf_token": session["csrf_token"]})

    @app.post("/api/v1/auth/logout")
    @require_auth
    def auth_logout():
        revoke_browser_session(auth)
        return jsonify({"authenticated": False})

    @app.get("/api/v1/auth/tokens")
    @require_auth
    def token_list():
        return jsonify(
            {
                "items": [
                    _model_dict(
                        item,
                        (
                            "id",
                            "label",
                            "token_prefix",
                            "subject",
                            "scopes_json",
                            "expires_at",
                            "principal_kind",
                            "created_by",
                            "client_id",
                            "target_instance_id",
                            "canonical_origin",
                            "created_at",
                            "last_used_at",
                            "revoked_at",
                        ),
                    )
                    for item in auth.list_tokens(actor=context.guards.access_context().actor)
                ]
            }
        )

    @app.post("/api/v1/auth/tokens")
    @require_auth
    def token_create():
        payload = TokenCreateRequest.model_validate(request.get_json(silent=True) or {})
        principal = context.guards.principal()
        assert principal is not None
        identity = services.identity.snapshot(observed_origin=request.url_root)
        expires_at = (
            utcnow() + timedelta(days=payload.expires_in_days)
            if payload.expires_in_days is not None
            else None
        )
        try:
            actor = context.guards.access_context().actor
            selected_scopes = normalize_scopes(payload.scopes)
            if not selected_scopes <= actor.scopes:
                raise PermissionError("The requested scopes exceed this credential.")
            with database.immediate_session() as token_session:
                require_actor_in_session(token_session, actor, required_scopes=selected_scopes | {"app.credentials.write"})
                resources = require_delegation_resources(token_session, actor, default_personal_resources(token_session, actor.user_id))
                token, raw = auth.create_api_token_in_session(
                    token_session,
                    payload.label,
                    scopes=payload.scopes,
                    expires_at=expires_at,
                    created_by=principal.subject,
                    target_instance_id=identity.instance_id,
                    canonical_origin=identity.canonical_origin,
                    user_id=principal.user_id,
                    auth_revision=principal.auth_revision,
                    resource_grants=resources,
                )
        except PermissionError:
            return error_response(
                "scope_denied", "This credential cannot approve the requested token.", 403
            )
        return (
            jsonify(
                {
                    "id": token.id,
                    "label": token.label,
                    "token": raw,
                    "subject": token.subject,
                    "scopes": list(token.scopes_json or []),
                    "expires_at": (
                        token.expires_at.isoformat() if token.expires_at else None
                    ),
                    "target_instance_id": token.target_instance_id,
                }
            ),
            201,
        )

    @app.delete("/api/v1/auth/tokens/<token_id>")
    @require_auth
    def token_revoke(token_id: str):
        try:
            auth.revoke_token(token_id, actor=context.guards.access_context().actor)
        except KeyError:
            return error_response("not_found", "API token not found.", 404)
        return "", 204

    @app.get("/api/v1/capabilities")
    @require_auth
    def capabilities():
        force = request.args.get("refresh", "").lower() in {"1", "true", "yes"}
        payload = capability_service.get(
            local_mode=_is_loopback_address(request.remote_addr),
            force=force,
        )
        payload["application"] = {"version": PANDRATOR_VERSION}
        return jsonify(payload)

    @app.get("/pandrator-logo.png")
    def pandrator_logo_png():
        """Retain the legacy application-mark URL for older cached shells."""
        return send_from_directory(static_dir, "pandrator-logo.png")

    @app.get("/pandrator-logo.webp")
    def pandrator_logo_webp():
        """Serve the web-sized application mark used by the SPA shell."""
        return send_from_directory(
            static_dir,
            "pandrator-logo.webp",
            mimetype="image/webp",
        )

    @app.get("/favicon-32.png")
    def pandrator_favicon():
        """Serve the browser icon without requiring authentication."""
        return send_from_directory(static_dir, "favicon-32.png")

    @app.get("/api/v1/parity")
    @require_auth
    def parity_registry():
        return jsonify(build_registry())

    register_service_routes(app, context, idempotency=idempotency)

    register_session_list_routes(
        app, context, _session_payload=lambda record: _session_payload(record),
    )

    register_settings_routes(app, context)

    register_session_lifecycle_routes(
        app, context, idempotency=idempotency,
        _session_payload=lambda record: _session_payload(record),
    )

    register_session_settings_routes(app, context, idempotency=idempotency)

    @app.get("/api/v1/sessions/<session_id>/outcome-plan")
    @context.guards.require_scope("app.read")
    def outcome_plan_get(session_id: str):
        try:
            result = outcome_plans.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.put("/api/v1/sessions/<session_id>/outcome-plan")
    @context.guards.require_scope("app.write")
    def outcome_plan_put(session_id: str):
        payload = OutcomePlanUpdate.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current outcome-plan revision.",
                428,
            )
        try:
            result = outcome_plans.update(session_id, expected, payload.value)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    register_source_library_routes(app, context, idempotency=idempotency)

    from .subtitle_source_routes import register_subtitle_source_routes

    register_subtitle_source_routes(app, services, require_auth, error_response)
    from .source_passage_routes import register_source_passage_routes

    register_source_passage_routes(app, context)
    from .session_flow_routes import register_session_flow_routes

    register_session_flow_routes(app, context)

    from .project_folder_routes import register_project_folder_routes

    register_project_folder_routes(app, context)

    from .project_sharing_routes import register_project_sharing_routes

    register_project_sharing_routes(app, context)

    from .public_link_routes import register_public_link_routes
    from .public_links.reviews import PublicTranslationReviewService
    from .public_links.service import PublicLinkService

    public_links = PublicLinkService(services.database, services.auth, services.audit)
    public_reviews = PublicTranslationReviewService(services.database, services.subtitle_review, services.audit)
    context.guards.app.extensions["pandrator_public_links"] = public_links
    context.guards.app.extensions["pandrator_public_reviews"] = public_reviews
    register_public_link_routes(app, context, public_links, public_reviews)

    from .project_copy_routes import register_project_copy_routes

    register_project_copy_routes(app, context)

    from .knowledge_routes import register_knowledge_routes

    register_knowledge_routes(app, context)

    from .translation_project_routes import register_translation_project_routes

    register_translation_project_routes(
        app, services, require_auth, error_response, context.guards.principal,
        access_context=context.guards.access_context,
    )
    from .project_operation_routes import register_project_operation_routes

    register_project_operation_routes(app, context)
    from .project_export_bundle_routes import register_project_export_bundle_routes

    register_project_export_bundle_routes(app, context)

    register_source_document_routes(app, context)

    register_generation_routes(app, context, idempotency=idempotency)

    @app.get("/api/v1/sessions/<session_id>/agent-runs")
    @require_auth
    def agent_run_list(session_id: str):
        with database.session() as db_session:
            require_current_session_access(db_session, session_id, ProjectAction.READ)
            statement = select(AgentRun).where(AgentRun.session_id == session_id)
            requested_kind = str(request.args.get("kind") or "").strip()
            if requested_kind:
                statement = statement.where(AgentRun.kind == requested_kind)
            records = list(
                db_session.scalars(statement.order_by(AgentRun.created_at.desc())).all()
            )
            return jsonify(
                {
                    "items": [
                        _model_dict(
                            item,
                            (
                                "id",
                                "kind",
                                "session_id",
                                "source_artifact_id",
                                "result_artifact_id",
                                "job_id",
                                "status",
                                "source_content_hash",
                                "settings_hash",
                                "checkpoint_revision",
                                "error_message",
                                "settings_json",
                                "created_at",
                                "updated_at",
                            ),
                        )
                        for item in records
                    ]
                }
            )

    @app.post("/api/v1/sessions/<session_id>/agent-runs")
    @require_auth
    @require_session_action(context, ProjectAction.RUN)
    def agent_run_create(session_id: str):
        payload = AgentRunCreateRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.settings):
            return rejected
        try:
            result = services.agent_runs.create(session_id, payload.source_artifact_id, payload.settings)
        except KeyError:
            return error_response("not_found", "Session or source artifact not found.", 404)
        except AgentRunAdmissionError as error:
            return error_response(error.code, str(error), error.status)
        return jsonify(result), 202

    @app.get("/api/v1/agent-runs/<run_id>/steps")
    @require_auth
    def agent_step_list(run_id: str):
        with database.session() as db_session:
            try:
                require_current_agent_run_access(db_session, run_id, ProjectAction.READ)
            except KeyError:
                return error_response(
                    "not_found", "Agentic cleaning run not found.", 404
                )
            records = list(
                db_session.scalars(
                    select(AgentStep)
                    .where(AgentStep.agent_run_id == run_id)
                    .order_by(AgentStep.ordinal)
                ).all()
            )
            return jsonify(
                {
                    "items": [
                        _model_dict(
                            item,
                            (
                                "id",
                                "agent_run_id",
                                "ordinal",
                                "unit_key",
                                "input_hash",
                                "phase",
                                "status",
                                "summary",
                                "input_json",
                                "output_json",
                                "cost_usd",
                                "created_at",
                                "updated_at",
                            ),
                        )
                        for item in records
                    ]
                }
            )

    @app.post("/api/v1/agent-runs/<run_id>/resume")
    @require_auth
    def agent_run_resume(run_id: str):
        try:
            result = services.agent_runs.resume(run_id)
        except KeyError:
            return error_response("not_found", "Agentic operation not found.", 404)
        except AgentRunStateError as error:
            return error_response("invalid_state", str(error), 409)
        return jsonify(result), 202

    @app.post("/api/v1/agent-runs/<run_id>/accept")
    @require_auth
    def agent_run_accept(run_id: str):
        try:
            result = services.agent_runs.accept(run_id)
        except KeyError:
            return error_response("not_found", "Agentic cleaning run not found.", 404)
        except AgentRunStateError as error:
            return error_response("invalid_state", str(error), 409)
        return jsonify(result)

    def authorize_job_submission(db_session, access, kind):
        privileged = PRIVILEGED_JOB_SCOPES.get(kind)
        scopes = {"app.admin"} | set(privileged or ())
        if kind in {"noop", "session.bundle.export"}:
            scopes.add("app.run")
        try:
            require_actor_in_session(
                db_session, access.actor, required_scopes=scopes, require_admin=True,
            )
        except PermissionError:
            if privileged is not None:
                raise AdministratorRequired() from None
            raise

    def bundle_replay_job(db_session, access, job_id, *, session_id=None):
        saved = require_job_access(db_session, access, job_id, JobAction.RUN)
        if (
            saved.kind != "session.bundle.export" or saved.session_id is None
            or (session_id is not None and saved.session_id != session_id)
        ):
            raise JobNotFound()
        return saved

    def bundle_export_response(command, *, max_attempts=1, raw_submission=False):
        access = context.guards.access_context()
        key, key_error = mutation_idempotency_key()
        if key_error is not None:
            return key_error
        canonical = {**command.model_dump(mode="json"), "max_attempts": max_attempts}
        try:
            with database.immediate_session() as db_session:
                if raw_submission:
                    authorize_job_submission(db_session, access, "session.bundle.export")
                require_session_access(db_session, access, command.session_id, ProjectAction.RUN)
                reservation = None
                if key is not None:
                    try:
                        services.idempotency.validate_key(key)
                    except ValueError as error:
                        return idempotency_failure(error)
                    existing = db_session.execute(
                        select(ApiIdempotency.resource_kind, ApiIdempotency.resource_id).where(
                            ApiIdempotency.principal_subject == access.actor.subject,
                            ApiIdempotency.operation_id == "exportSessionBundle",
                            ApiIdempotency.idempotency_key == key,
                        )
                    ).one_or_none()
                    if existing is not None:
                        if existing.resource_kind != "job" or not existing.resource_id:
                            raise JobNotFound()
                        bundle_replay_job(db_session, access, existing.resource_id)
                    reservation = services.idempotency.begin(
                        db_session, principal=idempotency.principal(),
                        operation_id="exportSessionBundle", idempotency_key=key,
                        payload=canonical,
                    )
                    if reservation.replayed:
                        bundle_replay_job(
                            db_session, access, reservation.record.resource_id,
                            session_id=command.session_id,
                        )
                        return replay_idempotency(reservation)
                job = enqueue_bundle_export_in_session(
                    db_session, access, jobs, command, max_attempts=max_attempts,
                )
                result = _job_payload(job)
                if reservation is not None:
                    services.idempotency.complete(
                        db_session, reservation, response=result, status_code=202,
                        resource_kind="job", resource_id=job.id,
                    )
        except KeyError:
            return error_response("not_found", "Session or bundle job not found.", 404)
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return idempotency_failure(error)
        except ValueError as error:
            return error_response("job_conflict", str(error), 409)
        return jsonify(result), 202

    @app.post("/api/v1/sessions/<session_id>/bundle")
    @require_auth
    @require_session_action(context, ProjectAction.RUN)
    def session_bundle_export(session_id: str):
        payload = BundleExportRequest.model_validate(request.get_json(silent=True) or {})
        return bundle_export_response(
            BundleExportJobPayload(session_id=session_id, include_sources=payload.include_sources)
        )

    @app.post("/api/v1/session-bundles/import")
    @require_auth
    def session_bundle_import():
        payload = BundleImportRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            with database.immediate_session() as db_session:
                job = enqueue_bundle_import_in_session(
                    db_session,
                    context.guards.access_context(),
                    jobs,
                    source_artifact_id=payload.source_artifact_id,
                    name=payload.name,
                )
        except KeyError:
            return error_response("not_found", "Bundle artifact not found.", 404)
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/jobs")
    @require_auth
    def job_list():
        with database.snapshot_session() as db_session:
            items = work.diagnostic_list_in_session(
                db_session, context.guards.access_context(),
                request.args.get("limit", 100, type=int),
                session_id=request.args.get("session_id"),
                kind=request.args.get("kind"),
            )
            waiting_for_jobs = work.waiting_for_jobs_in_session(db_session, context.guards.access_context(), items)
            projects = job_projects(database, items, db_session=db_session)
            return jsonify(
                {
                    "items": [
                        _job_payload(
                            item, waiting_for_job=waiting_for_jobs.get(item.id),
                            project=projects.get(item.id),
                        )
                        for item in items
                    ]
                }
            )

    @app.post("/api/v1/jobs")
    @require_auth
    def job_create():
        payload = JobCreate.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.payload):
            return rejected
        if payload.kind == "session.bundle.export":
            body = dict(payload.payload)
            if payload.session_id is not None:
                body.setdefault("session_id", payload.session_id)
            command = BundleExportJobPayload.model_validate(body)
            if payload.session_id is not None and payload.session_id.strip() != command.session_id:
                return error_response("validation_error", "The job session and payload session disagree.", 422)
            return bundle_export_response(
                command, max_attempts=payload.max_attempts, raw_submission=True,
            )
        access = context.guards.access_context()
        try:
            with database.immediate_session() as db_session:
                authorize_job_submission(db_session, access, payload.kind)
                if payload.kind != "noop":
                    return error_response(
                        "job_operation_required",
                        "Use the workflow, generation, voice, sound or model endpoint for this job.",
                        422, {"kind": payload.kind, "supported_kinds": ["noop", "session.bundle.export"]},
                    )
                command = parse_diagnostic_payload(payload.payload)
                job = enqueue_diagnostic_in_session(
                    db_session, access, jobs, command, session_id=payload.session_id,
                    max_attempts=payload.max_attempts,
                )
                result = _job_payload(job)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValidationError:
            raise
        except ValueError as error:
            return error_response("job_conflict", str(error), 409)
        return jsonify(result), 202

    @app.get("/api/v1/jobs/<job_id>")
    @require_auth
    def job_get(job_id: str):
        try:
            with database.snapshot_session() as db_session:
                job = work.diagnostic_get_in_session(db_session, context.guards.access_context(), job_id)
                waiting_for_jobs = work.waiting_for_jobs_in_session(db_session, context.guards.access_context(), [job])
                projects = job_projects(database, [job], db_session=db_session)
                return jsonify(
                    _job_payload(
                        job,
                        waiting_for_job=waiting_for_jobs.get(job.id),
                        project=projects.get(job.id),
                    )
                )
        except KeyError:
            return error_response("not_found", "Job not found.", 404)

    @app.get("/api/v1/jobs/<job_id>/logs")
    @require_auth
    def job_logs(job_id: str):
        """Return the job's durable event and captured worker-log timeline."""
        try:
            with database.snapshot_session() as db_session:
                events = work.diagnostic_events_in_session(
                    db_session, context.guards.access_context(), job_id,
                    request.args.get("limit", 1000, type=int),
                )
                payload = [
                    redact_inline_secrets({
                        "id": event.id,
                        "event_type": event.event_type,
                        "payload_json": event.payload_json,
                        "created_at": event.created_at.isoformat(),
                    }) for event in events
                ]
        except KeyError:
            return error_response("not_found", "Job not found.", 404)
        return jsonify({"items": payload})

    def _query_values(name: str) -> tuple[str, ...]:
        values: list[str] = []
        for supplied in request.args.getlist(name):
            values.extend(
                item.strip() for item in str(supplied).split(",") if item.strip()
            )
        return tuple(values)

    @app.get("/api/v1/work")
    @require_auth
    def work_list():
        with database.snapshot_session() as db_session:
            items = work.list_in_session(
                db_session, context.guards.access_context(),
                session_id=str(request.args.get("session_id") or "").strip() or None,
                kinds=_query_values("kind"),
                states=_query_values("state"),
                limit=request.args.get("limit", 50, type=int) or 50,
            )
            payload = [item.model_dump(mode="json") for item in items]
        return jsonify(
            {
                "schema_version": "1",
                "items": payload,
            }
        )

    @app.get("/api/v1/work/<job_id>")
    @require_auth
    def work_get(job_id: str):
        try:
            with database.snapshot_session() as db_session:
                return jsonify(work.get_in_session(db_session, context.guards.access_context(), job_id).model_dump(mode="json"))
        except KeyError:
            return error_response("not_found", "Work item not found.", 404)

    @app.get("/api/v1/generation-operations/<operation_id>")
    @require_auth
    def generation_operation_get(operation_id: str):
        from .generation_operations import get_generation_operation

        include = request.args.get("include_coverage", "true")
        if include not in {"true", "false"}:
            return error_response("validation_error", "include_coverage must be true or false", 422)
        try:
            return jsonify(get_generation_operation(work, operation_id, include_coverage=include == "true"))
        except KeyError:
            return error_response("not_found", "Generation operation not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.get("/api/v1/generation-runs/<run_id>/coverage")
    @require_auth
    def generation_run_coverage_get(run_id: str):
        from .generation_operations import get_generation_run_coverage

        side = request.args.get("audio_version_side", "after")
        if side not in {"before", "after"}:
            return error_response("validation_error", "audio_version_side must be before or after", 422)
        try:
            return jsonify(get_generation_run_coverage(database, run_id, audio_version_side="before" if side == "before" else "after"))
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.get("/api/v1/work/<job_id>/events")
    @require_auth
    def work_events(job_id: str):
        try:
            with database.snapshot_session() as db_session:
                page = work.events_in_session(
                    db_session, context.guards.access_context(), job_id,
                    after=request.args.get("after", 0, type=int) or 0,
                    limit=request.args.get("limit", 200, type=int) or 200,
                )
        except KeyError:
            return error_response("not_found", "Work item not found.", 404)
        return jsonify(page.model_dump(mode="json"))

    @app.post("/api/v1/work/<job_id>/cancel")
    @require_auth
    def work_cancel(job_id: str):
        principal = context.guards.principal()
        if principal is None:
            return error_response(
                "authentication_required",
                "Authentication is required.",
                401,
            )
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                work.diagnostic_get_in_session(
                    db_session, access, job_id, action=JobAction.CANCEL
                )
                reservation = services.idempotency.begin(
                    db_session,
                    principal=principal,
                    operation_id="cancelWork",
                    idempotency_key=request.headers.get("Idempotency-Key"),
                    payload={"work_id": job_id},
                )
                if reservation.replayed:
                    replay = reservation.response
                    assert replay is not None
                    payload, status_code = replay
                    response = jsonify(payload)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    return response
                payload = work.cancel_authorized_in_session(
                    db_session,
                    access,
                    job_id,
                ).model_dump(mode="json")
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=payload,
                    status_code=200,
                    resource_kind="work",
                    resource_id=job_id,
                )
            g.audit_resource_kind = "work"
            g.audit_resource_id = job_id
            return jsonify(payload)
        except KeyError:
            return error_response("not_found", "Work item not found.", 404)
        except ValueError as error:
            return error_response(
                "idempotency_key_required",
                str(error),
                400,
            )
        except IdempotencyConflict as error:
            return error_response(error.code, str(error), 409)
        except IdempotencyInProgress as error:
            return error_response(
                error.code,
                str(error),
                409,
                {"retryable": True},
            )

    workflow_route_context = WorkflowRouteContext(
        database=database,
        sessions=sessions,
        workflows=workflows,
        workflow_handlers=workflow_handlers,
        require_auth=require_auth,
        error_response=error_response,
        inline_credential_error=inline_credential_error,
        jsonify=lambda value: jsonify(value),
        request=lambda: request,
        stage_history=lambda session, session_id, stage_key, **kwargs: stage_history(
            session, session_id, stage_key, **kwargs
        ),
        trash_stage_artifact=lambda session, session_id, stage_key, artifact_id: (
            trash_stage_artifact(session, session_id, stage_key, artifact_id)
        ),
        rerun_impact=lambda session, session_id, stage_key: rerun_impact(
            session, session_id, stage_key
        ),
        choose_artifact=lambda session, session_id, stage_key, artifact_id: (
            choose_artifact(session, session_id, stage_key, artifact_id)
        ),
        clear_selection=lambda session, session_id, stage_key: clear_selection(
            session, session_id, stage_key
        ),
        selection_update_type=lambda: StageSelectionUpdate,
        job_payload=lambda job: _job_payload(job),
        access_context=context.guards.access_context,
    )
    register_workflow_session_routes(app, workflow_route_context)

    @app.get("/api/v1/sessions/<session_id>/subtitles")
    @require_auth
    def subtitle_documents(session_id: str):
        try:
            sessions.get(session_id)
            return jsonify(subtitle_review.documents(session_id))
        except KeyError:
            return error_response(
                "not_found", "Session or subtitle document not found.", 404
            )

    @app.get("/api/v1/sessions/<session_id>/subtitles/catalog")
    @require_auth
    def subtitle_catalog(session_id: str):
        try:
            sessions.get(session_id)
            return jsonify(subtitle_review.catalog(session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.get("/api/v1/sessions/<session_id>/subtitles/review")
    @require_auth
    def subtitle_review_exact(session_id: str):
        try:
            sessions.get(session_id)
            artifact_ids = request.args.getlist("artifact_id")
            return jsonify(subtitle_review.review(session_id, artifact_ids))
        except KeyError:
            return error_response(
                "not_found", "Subtitle artifact not found in this session.", 404
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    def save_subtitle_mutation(session_id: str, stage: str, *, canonical: bool):
        body = request.get_json(silent=True) or {}
        payload = (SubtitlePassageReviewRequest.model_validate(body) if canonical
                   else SubtitleReviewRequest.model_validate(body))
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        try:
            if isinstance(payload, SubtitlePassageReviewRequest):
                result = services.subtitle_mutations.save_passage_review(
                    session_id, stage, payload, principal=idempotency.principal(),
                    idempotency_key=idempotency_key,
                )
            else:
                result = services.subtitle_mutations.save_review(
                    session_id, stage, payload, principal=idempotency.principal(),
                    idempotency_key=idempotency_key,
                )
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return idempotency_failure(error)
        except KeyError:
            return error_response("not_found", "Subtitle document not found.", 404)
        except RuntimeError as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result.payload)
        response.status_code = result.status_code
        if result.replayed:
            response.headers["Idempotency-Replayed"] = "true"
        return response

    @app.post("/api/v1/sessions/<session_id>/subtitles/<stage>/review")
    @require_auth
    def subtitle_save_review(session_id: str, stage: str):
        return save_subtitle_mutation(session_id, stage, canonical=False)

    @app.post("/api/v1/sessions/<session_id>/subtitles/<stage>/passage-review")
    @require_auth
    def subtitle_save_passage_review(session_id: str, stage: str):
        return save_subtitle_mutation(session_id, stage, canonical=True)

    @app.post("/api/v1/sessions/<session_id>/subtitle-evidence")
    @context.guards.require_scope("app.run")
    def subtitle_evidence_create(session_id: str):
        request.max_content_length = 32 * 1024
        payload = SubtitleEvidenceCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        values = payload.model_dump(mode="json")
        if not values.get("force_refresh"):
            values.pop("force_refresh", None)
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        reservation = None
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                authorize_evidence_request(db_session, access, session_id, values)
                if idempotency_key is not None:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="requestSubtitleEvidence",
                        idempotency_key=idempotency_key,
                        payload={"session_id": session_id, **values},
                    )
                    if reservation.response is not None:
                        if reservation.record.resource_kind != "subtitle_evidence" or not reservation.record.resource_id:
                            raise KeyError("Subtitle evidence not found.")
                        require_evidence_access(db_session, access, reservation.record.resource_id, ProjectAction.RUN, expected_session_id=session_id)
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                with execution_actor_scope(access.actor):
                    result = subtitle_evidence.request_in_session(
                        db_session, session_id, values
                    )
                record = db_session.get(SubtitleEvidence, str(result["record"]["id"]))
                assert record is not None and record.session_id == session_id
                result["record"]["revision"] = evidence_revision(record)
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="subtitle_evidence",
                        resource_id=str(result["record"]["id"]),
                    )
        except KeyError:
            return error_response(
                "not_found", "Session or subtitle artifact not found.", 404
            )
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return idempotency_failure(error)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        g.audit_resource_kind = "subtitle_evidence"
        g.audit_resource_id = str(result["record"]["id"])
        return jsonify(result), 202

    @app.get("/api/v1/sessions/<session_id>/subtitle-evidence")
    @require_auth
    def subtitle_evidence_list(session_id: str):
        include_referenced = request.args.get("include_referenced", "false")
        source_artifact_id = request.args.get("source_artifact_id") or None
        if include_referenced not in {"true", "false"}:
            return error_response("validation_error", "include_referenced must be true or false.", 422)
        if include_referenced == "true" and not source_artifact_id:
            return error_response("validation_error", "Choose a subtitle artifact to include its saved evidence.", 422)
        try:
            with database.snapshot_session() as db_session:
                return jsonify(list_evidence_in_session(
                    db_session, context.guards.access_context(), session_id,
                    source_artifact_id,
                    include_referenced=include_referenced == "true",
                ))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.get("/api/v1/subtitle-evidence/<evidence_id>")
    @require_auth
    def subtitle_evidence_get(evidence_id: str):
        try:
            with database.snapshot_session() as db_session:
                return jsonify(get_evidence_in_session(db_session, context.guards.access_context(), evidence_id))
        except KeyError:
            return error_response("not_found", "Subtitle evidence not found.", 404)

    @app.post("/api/v1/sessions/<session_id>/subtitle-evidence/<evidence_id>/resolve")
    @context.guards.require_scope("app.write")
    def subtitle_evidence_resolve(session_id: str, evidence_id: str):
        request.max_content_length = 128 * 1024
        payload = SubtitleEvidenceResolveRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        values = payload.model_dump(mode="json")
        if values.get("expected_revision") is None:
            values.pop("expected_revision", None)
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        reservation = None
        try:
            with database.immediate_session() as db_session:
                access = context.guards.access_context()
                record, _root = require_evidence_access(db_session, access, evidence_id, ProjectAction.REVIEW, expected_session_id=session_id)
                if idempotency_key is not None:
                    principal = context.guards.principal()
                    assert principal is not None
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=principal,
                        operation_id="resolveSubtitleEvidence",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "evidence_id": evidence_id,
                            **values,
                        },
                    )
                    if reservation.response is not None:
                        if reservation.record.resource_kind != "subtitle_evidence" or reservation.record.resource_id != evidence_id:
                            raise KeyError("Subtitle evidence not found.")
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                require_evidence_revision(record, payload.expected_revision, required=installation_mode_in_session(db_session).enabled)
                result = subtitle_evidence.resolve_in_session(
                        db_session, session_id, evidence_id, values
                    )
                result["record"]["revision"] = evidence_revision(record)
                services.audit.record_command_in_session(
                    db_session, access, action="subtitle_evidence.resolve",
                    workspace_id=evidence_id, resource_kind="subtitle_evidence", metadata={},
                )
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="subtitle_evidence",
                        resource_id=evidence_id,
                    )
        except KeyError:
            return error_response("not_found", "Subtitle evidence not found.", 404)
        except (IdempotencyConflict, IdempotencyInProgress) as error:
            return idempotency_failure(error)
        except EvidenceRevisionRequired as error:
            return error_response("precondition_required", str(error), 428)
        except EvidenceRevisionConflict as error:
            return error_response("evidence_revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("evidence_conflict", str(error), 409)
        g.audit_resource_kind = "subtitle_evidence"
        g.audit_resource_id = evidence_id
        return jsonify(result)

    register_workflow_job_routes(app, workflow_route_context)

    @app.post("/api/v1/jobs/<job_id>/cancel")
    @require_auth
    def job_cancel(job_id: str):
        try:
            with database.immediate_session() as db_session:
                work.diagnostic_get_in_session(
                    db_session, context.guards.access_context(), job_id,
                    action=JobAction.CANCEL,
                )
                return jsonify(_job_payload(jobs.request_cancel_in_session(db_session, job_id)))
        except KeyError:
            return error_response("not_found", "Job not found.", 404)

    @app.get("/api/v1/events/snapshot")
    @require_auth
    def event_snapshot():
        from .session_routes import session_project_memberships

        view = request.args.get("view", "full")
        if view not in {"full", "compact"}:
            return error_response("validation_error", "Unknown event snapshot view.", 422)
        # Capture the cursor before reading resources. Events committed while
        # the snapshot is assembled are replayed after this cursor.
        local_mode = _is_loopback_address(request.remote_addr)
        capability_payload = capability_service.get(local_mode=local_mode)
        capability_payload["application"] = {"version": PANDRATOR_VERSION}
        with database.snapshot_session() as db_session:
            access = context.guards.access_context()
            bounds = work.event_bounds_in_session(db_session)
            items = work.diagnostic_list_in_session(db_session, access, 40)
            waiting_for_jobs = work.waiting_for_jobs_in_session(db_session, context.guards.access_context(), items)
            projects = job_projects(database, items, db_session=db_session)
            session_items = [
                _session_payload(item) for item in list_session_records(db_session, access)
            ]
            memberships = session_project_memberships(
                database, [item["id"] for item in session_items], db_session=db_session
            )
            metadata = session_access_payloads(db_session, access, [item["id"] for item in session_items]) if auth.collaboration_enabled() else {}
            for item in session_items:
                item["translation_project"] = memberships.get(item["id"])
                item.update(metadata.get(item["id"], {}))
            job_items = [
                _job_payload(
                    item, include_details=view != "compact",
                    waiting_for_job=waiting_for_jobs.get(item.id),
                    project=projects.get(item.id),
                ) for item in items
            ]
        return jsonify(
            {
                "cursor": bounds.latest,
                "retained_after": bounds.retained_after,
                "sessions": {"items": session_items},
                "jobs": {
                    "items": job_items
                },
                "capabilities": capability_payload,
            }
        )

    @app.get("/api/v1/events")
    @require_auth
    def events():
        access = context.guards.access_context()
        with database.snapshot_session() as db_session:
            # Prove the subscription before yielding any stream metadata.
            job_visibility_clause(db_session, access)
            bounds = work.event_bounds_in_session(db_session)
        supplied_cursor = request.headers.get("Last-Event-ID")
        if supplied_cursor is None:
            supplied_cursor = request.args.get("after")
        reset_reason: str | None = None
        if supplied_cursor is None:
            # A new tab subscribes to future changes instead of replaying the
            # complete retained job history.
            cursor = bounds.latest
        else:
            try:
                cursor = max(0, int(supplied_cursor))
            except (TypeError, ValueError):
                cursor = bounds.latest
                reset_reason = "invalid_cursor"
            if cursor > bounds.latest:
                cursor = bounds.latest
                reset_reason = "cursor_ahead"
            elif bounds.oldest and cursor < bounds.oldest - 1:
                cursor = bounds.latest
                reset_reason = "cursor_expired"

        def stream():
            nonlocal cursor, reset_reason
            if reset_reason:
                yield (
                    f"id: {cursor}\n"
                    "event: stream.reset\n"
                    f"data: {json.dumps({'cursor': cursor, 'reason': reset_reason})}\n\n"
                )
                reset_reason = None
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                try:
                    with database.snapshot_session() as db_session:
                        page = work.events_after_in_session(db_session, access, cursor)
                        retained = work.event_bounds_in_session(db_session)
                except AccessDenied:
                    return
                if retained.oldest and cursor < retained.oldest - 1:
                    cursor = retained.latest
                    yield (
                        f"id: {cursor}\n"
                        "event: stream.reset\n"
                        f"data: {json.dumps({'cursor': cursor, 'reason': 'cursor_expired'})}\n\n"
                    )
                    continue
                new_events = page.items
                if new_events:
                    last_visible_id = cursor
                    for event in new_events:
                        cursor = event.id
                        last_visible_id = event.id
                        payload = _sse_event_payload(event)
                        yield (
                            f"id: {event.id}\n"
                            f"event: {event.event_type}\n"
                            f"data: {json.dumps(payload)}\n\n"
                        )
                    cursor = page.next_cursor
                    if cursor > last_visible_id:
                        # Advance the browser reconnect cursor without exposing
                        # worker log records to every open tab.
                        yield (f"id: {cursor}\nevent: stream.cursor\ndata: {{}}\n\n")
                elif page.next_cursor > cursor:
                    cursor = page.next_cursor
                    yield (f"id: {cursor}\nevent: stream.cursor\ndata: {{}}\n\n")
                else:
                    yield ": heartbeat\n\n"
                time.sleep(1)

        return Response(
            stream(),
            mimetype="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/uploads")
    @context.guards.require_scope("app.write")
    def upload():
        query_session_id = request.args.get("session_id") or None
        query_workspace_id = request.args.get("workspace_id") or None
        if query_session_id or query_workspace_id:
            with database.snapshot_session() as unit:
                require_upload_creation_access(
                    unit, session_id=query_session_id, workspace_id=query_workspace_id
                )
        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return error_response("missing_file", "A multipart file is required.", 400)
        filename = secure_filename(incoming.filename) or f"upload-{uuid.uuid4()}"
        temporary = paths.temporary / f"upload-{uuid.uuid4()}.part"
        form_session_id = request.form.get("session_id") or None
        form_workspace_id = request.form.get("workspace_id") or None
        if (
            query_session_id and form_session_id and query_session_id != form_session_id
        ) or (
            query_workspace_id and form_workspace_id and query_workspace_id != form_workspace_id
        ):
            return error_response("validation_error", "Upload selectors must agree.", 422)
        requested_session_id = query_session_id or form_session_id
        requested_workspace_id = query_workspace_id or form_workspace_id
        attach_values = request.form.getlist("attach_to_session")
        if len(attach_values) > 1 or (attach_values and attach_values[0] not in {"true", "false"}):
            return error_response("validation_error", "attach_to_session must be true or false.", 422)
        attach_to_session = not attach_values or attach_values[0] == "true"
        purpose = str(request.form.get("purpose") or "source").strip().lower()
        if purpose not in {"source", "cover"}:
            return error_response(
                "validation_error", "Unsupported upload purpose.", 422
            )
        if purpose == "cover" and not requested_session_id:
            return error_response(
                "validation_error", "Cover artwork must belong to a session.", 422
            )
        if not attach_to_session and (not requested_session_id or purpose != "source"):
            return error_response("validation_error", "Staged source uploads require an explicit session.", 422)
        try:
            with database.snapshot_session() as unit:
                require_upload_creation_access(
                    unit, session_id=requested_session_id, workspace_id=requested_workspace_id
                )
            incoming.save(temporary)
            if purpose == "cover":
                if temporary.stat().st_size > 25 * 1024 * 1024:
                    return error_response(
                        "cover_too_large",
                        "Cover artwork must be 25 MiB or smaller.",
                        413,
                    )
                from PIL import Image, UnidentifiedImageError
                try:
                    with Image.open(temporary) as image:
                        if image.format not in {"JPEG", "PNG", "WEBP"}:
                            raise ValueError("Use JPEG, PNG, or WebP artwork.")
                        width, height = image.size
                        if width < 1 or height < 1 or width * height > 100_000_000:
                            raise ValueError(
                                "Artwork dimensions are invalid or exceed 100 megapixels."
                            )
                        image.verify()
                except (
                    Image.DecompressionBombError,
                    OSError,
                    UnidentifiedImageError,
                    ValueError,
                ) as error:
                    return error_response(
                        "invalid_cover",
                        f"Cover artwork is not a readable image: {error}",
                        422,
                    )
            result = publish_multipart_upload(
                temporary,
                filename=filename,
                original_filename=incoming.filename,
                session_id=requested_session_id,
                purpose=purpose,
                attach_to_session=attach_to_session,
                database=database,
                paths=paths,
                artifacts=artifacts,
                sources=source_library,
                workspace_id=requested_workspace_id,
            )
            return jsonify(result), 201
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                flask_app.logger.warning("Could not remove staged upload %s", temporary, exc_info=True)

    @app.post("/api/v1/uploads/init")
    @context.guards.require_scope("app.write")
    def chunk_upload_init():
        payload = ChunkUploadInitialize.model_validate(
            request.get_json(silent=True) or {}
        )
        canonical_payload = payload.model_dump(
            mode="json", exclude={"workspace_id"} if payload.workspace_id is None else set(),
        )
        if payload.attach_to_session:
            canonical_payload.pop("attach_to_session", None)
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            upload_id = str(uuid.uuid4())
            created_directories = []
            try:
                with database.immediate_session() as db_session:
                    require_upload_creation_access(
                        db_session, session_id=payload.session_id, workspace_id=payload.workspace_id
                    )
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=idempotency.principal(),
                        operation_id="initializeChunkUpload",
                        idempotency_key=idempotency_key,
                        payload=canonical_payload,
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        replay_record = chunk_uploads._scoped_record(
                            db_session, str(result.get("id") or ""),
                            workspace_id=payload.workspace_id, action=ProjectAction.EDIT,
                            validate_result=True,
                        )
                        if replay_record.attach_to_session is not payload.attach_to_session:
                            raise ValueError("The upload attachment mode changed.")
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    record = chunk_uploads.initialize_in_session(
                        db_session,
                        filename=payload.filename,
                        size_bytes=payload.size_bytes,
                        mime_type=payload.mime_type,
                        session_id=payload.session_id,
                        attach_to_session=payload.attach_to_session,
                        workspace_id=payload.workspace_id,
                        expected_hash=payload.sha256,
                        chunk_size=payload.chunk_size,
                        max_size=int(
                            app.config.get(
                                "MAX_UPLOAD_SIZE",
                                100 * 1024 * 1024 * 1024,
                            )
                        ),
                        upload_id=upload_id,
                        created_directories=created_directories,
                    )
                    result = chunk_uploads.status_payload(record)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="upload",
                        resource_id=upload_id,
                    )
                return jsonify(result), 201
            except KeyError:
                cleanup_initialized_directories(created_directories)
                return error_response("not_found", "Session not found.", 404)
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                cleanup_initialized_directories(created_directories)
                return idempotency_failure(error)
            except Exception:
                cleanup_initialized_directories(created_directories)
                raise
        try:
            result = chunk_uploads.initialize(
                filename=payload.filename,
                size_bytes=payload.size_bytes,
                mime_type=payload.mime_type,
                session_id=payload.session_id,
                attach_to_session=payload.attach_to_session,
                workspace_id=payload.workspace_id,
                expected_hash=payload.sha256,
                chunk_size=payload.chunk_size,
                max_size=int(
                    app.config.get("MAX_UPLOAD_SIZE", 100 * 1024 * 1024 * 1024)
                ),
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

    @app.get("/api/v1/uploads/<upload_id>")
    @require_auth
    def chunk_upload_status(upload_id: str):
        try:
            return jsonify(chunk_uploads.status(upload_id))
        except KeyError:
            return error_response("not_found", "Upload not found.", 404)

    @app.put("/api/v1/uploads/<upload_id>/chunks/<int:index>")
    @require_auth
    def chunk_upload_write(upload_id: str, index: int):
        if (
            request.content_length is not None
            and request.content_length > 16 * 1024 * 1024
        ):
            return error_response(
                "chunk_too_large", "Upload chunks may not exceed 16 MiB.", 413
            )
        try:
            result = chunk_uploads.write_chunk(
                upload_id,
                index,
                request.stream,
                supplied_hash=request.headers.get("X-Chunk-SHA256"),
            )
        except KeyError:
            return error_response("not_found", "Upload not found.", 404)
        except ValueError as error:
            return error_response("invalid_chunk", str(error), 422)
        return jsonify(result)

    @app.post("/api/v1/uploads/<upload_id>/complete")
    @require_auth
    def chunk_upload_complete(upload_id: str):
        try:
            return jsonify(chunk_uploads.complete(upload_id)), 201
        except KeyError:
            return error_response("not_found", "Upload not found.", 404)
        except ValueError as error:
            return error_response("upload_incomplete", str(error), 409)

    @app.delete("/api/v1/uploads/<upload_id>")
    @require_auth
    def chunk_upload_cancel(upload_id: str):
        try:
            chunk_uploads.cancel(upload_id)
        except KeyError:
            return error_response("not_found", "Upload not found.", 404)
        except ValueError as error:
            return error_response("upload_conflict", str(error), 409)
        return "", 204

    register_source_ingestion_routes(app, context)

    register_artifact_routes(app, context)

    @app.get("/api/v1/artifacts/<artifact_id>/pdf")
    @require_auth
    def pdf_metadata(artifact_id: str):
        from .pdf_editor import inspect_pdf

        access = context.guards.access_context()
        try:
            with database.snapshot_session() as db_session:
                artifact = require_artifact_access(db_session, access, artifact_id)
                if artifact.state == "deleted":
                    raise ArtifactNotFound()
                path = paths.managed_path(artifact.relative_path)
                if path.suffix.lower() != ".pdf" or not path.is_file():
                    return error_response(
                        "invalid_pdf", "Artifact is not an available PDF.", 422
                    )
                first_page_side = request.args.get("first_page_side", "right")
                try:
                    return jsonify(inspect_pdf(path, first_page_side=first_page_side))
                except (ValueError, RuntimeError) as error:
                    return error_response("invalid_pdf", str(error), 422)
        except ArtifactNotFound:
            return error_response("not_found", "Artifact not found.", 404)

    @app.post("/api/v1/sessions/<session_id>/pdf/apply")
    @require_auth
    @require_session_action(context, ProjectAction.RUN)
    def pdf_apply(session_id: str):
        from .access.pdf_edit_policy import require_pdf_edit_access

        payload = PdfEditRequest.model_validate(request.get_json(silent=True) or {})
        access = context.guards.access_context()
        try:
            with database.immediate_session() as db_session, execution_actor_scope(access.actor):
                source = require_pdf_edit_access(db_session, session_id, payload.source_artifact_id)
                if source is None:
                    raise AccessDenied()
                job = jobs.enqueue_in_session(
                    db_session,
                    "pdf.apply_edits",
                    {
                        "session_id": session_id,
                        "source_artifact_id": payload.source_artifact_id,
                        "plan": payload.model_dump(exclude={"source_artifact_id"}),
                    },
                    session_id=session_id,
                    workspace_id=source.workspace_id,
                    project_id=source.target_project_id,
                )
        except KeyError:
            return error_response("not_found", "Source artifact not found.", 404)
        return jsonify(_job_payload(job)), 202

    register_provider_routes(app, context)

    @app.get("/api/v1/pronunciations")
    @require_auth
    def pronunciation_list():
        return jsonify(
            {
                "items": pronunciations.list(
                    query=str(request.args.get("q") or ""),
                    language=str(request.args.get("language") or ""),
                    status=str(request.args.get("status") or ""),
                    scope=str(request.args.get("scope") or ""),
                    session_id=str(request.args.get("session_id") or ""),
                    limit=request.args.get("limit", 500, type=int),
                )
            }
        )

    @app.post("/api/v1/pronunciations")
    @require_auth
    def pronunciation_create():
        payload = PronunciationCreate.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            created = pronunciations.create(payload.model_dump())
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(created)
        response.headers["ETag"] = f'"{created["revision"]}"'
        return response, 201

    @app.patch("/api/v1/pronunciations/<entry_id>")
    @require_auth
    def pronunciation_update(entry_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current pronunciation revision.",
                428,
            )
        payload = PronunciationUpdate.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            updated = pronunciations.update(
                entry_id,
                expected_revision,
                payload.model_dump(exclude_unset=True),
            )
        except KeyError:
            return error_response("not_found", "Pronunciation not found.", 404)
        except ValueError as error:
            code = (
                "revision_conflict"
                if "another client" in str(error)
                else "validation_error"
            )
            status = 409 if code == "revision_conflict" else 422
            return error_response(code, str(error), status)
        response = jsonify(updated)
        response.headers["ETag"] = f'"{updated["revision"]}"'
        return response

    @app.delete("/api/v1/pronunciations/<entry_id>")
    @require_auth
    def pronunciation_delete(entry_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current pronunciation revision.",
                428,
            )
        try:
            pronunciations.delete(entry_id, expected_revision)
        except KeyError:
            return error_response("not_found", "Pronunciation not found.", 404)
        except ValueError as error:
            return error_response("revision_conflict", str(error), 409)
        return "", 204

    @app.get("/api/v1/audit/events")
    @require_auth
    def audit_events():
        principal_subject = (
            str(request.args.get("principal_subject") or "").strip() or None
        )
        return jsonify(
            {
                "items": services.audit.list(
                    principal_subject=principal_subject,
                    limit=request.args.get("limit", 100, type=int) or 100,
                )
            }
        )


    @app.get("/api/v1/rvc/models")
    @require_auth
    def rvc_model_list():
        from pandrator.logic import rvc_handler

        with database.snapshot_session() as db_session:
            require_local_tool_workspace(db_session, LocalToolOperation.RVC_READ)
        available = rvc_handler.is_rvc_available()
        return jsonify(
            {
                "available": available,
                "items": rvc_handler.get_rvc_models(str(paths.models / "rvc"))
                if available
                else [],
            }
        )

    @app.post("/api/v1/rvc/models")
    @require_auth
    def rvc_model_upload():
        with database.snapshot_session() as db_session:
            workspace_id = require_local_tool_workspace(
                db_session, LocalToolOperation.RVC_MODEL_WRITE
            )
        payload = RvcModelUploadRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        with database.snapshot_session() as db_session:
            require_local_tool_inputs(
                db_session, LocalToolOperation.RVC_MODEL_WRITE,
                artifact_ids=[payload.pth_artifact_id, payload.index_artifact_id],
                workspace_id=workspace_id,
            )
        for artifact_id in (payload.pth_artifact_id, payload.index_artifact_id):
            try:
                _record, source = artifacts.resolve(artifact_id)
            except KeyError:
                return error_response(
                    "not_found", "An RVC upload artifact was not found.", 404
                )
            if not source.is_file():
                return error_response(
                    "artifact_missing", "An RVC upload artifact is missing.", 410
                )
        with database.immediate_session() as db_session:
            require_local_tool_inputs(
                db_session, LocalToolOperation.RVC_MODEL_WRITE,
                artifact_ids=[payload.pth_artifact_id, payload.index_artifact_id],
                workspace_id=workspace_id,
            )
            owner = artifact_job_ownership(
                db_session, [payload.pth_artifact_id, payload.index_artifact_id],
                project_scoped=False,
            )
            job = jobs.enqueue_in_session(
                db_session, "rvc.model.upload", payload.model_dump(),
                workspace_id=owner.workspace_id,
            )
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/rvc/convert")
    @require_auth
    def rvc_convert():
        with database.snapshot_session() as db_session:
            workspace_id = require_local_tool_workspace(
                db_session, LocalToolOperation.RVC_CONVERT
            )
        payload = RvcConvertRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.settings):
            return rejected
        with database.snapshot_session() as db_session:
            require_local_tool_inputs(
                db_session, LocalToolOperation.RVC_CONVERT,
                artifact_ids=[payload.source_artifact_id],
                session_id=payload.session_id, workspace_id=workspace_id,
            )
        try:
            artifacts.resolve(payload.source_artifact_id)
            if payload.session_id:
                sessions.get(payload.session_id)
        except KeyError:
            return error_response(
                "not_found",
                "The requested session or source artifact was not found.",
                404,
            )
        with database.immediate_session() as db_session:
            require_local_tool_inputs(
                db_session, LocalToolOperation.RVC_CONVERT,
                artifact_ids=[payload.source_artifact_id],
                session_id=payload.session_id, workspace_id=workspace_id,
            )
            owner = artifact_job_ownership(
                db_session, [payload.source_artifact_id], session_id=payload.session_id
            )
            job = jobs.enqueue_in_session(
                db_session, "rvc.convert", payload.model_dump(),
                session_id=payload.session_id,
                workspace_id=owner.workspace_id, project_id=owner.project_id,
                resource_keys=["service:rvc", "gpu:default"],
            )
        return jsonify(_job_payload(job)), 202

    register_training_routes(app, context)

    @app.get("/_app/<path:asset_path>")
    def frontend_asset(asset_path: str):
        suffix = Path(asset_path).suffix.lower()
        mimetype = "text/javascript" if suffix in {".js", ".mjs"} else None
        response = send_from_directory(
            static_dir / "_app",
            asset_path,
            mimetype=mimetype,
        )
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response

    @app.get("/")
    @app.get("/<path:client_path>")
    def spa(client_path: str = ""):
        if client_path.startswith("api/"):
            return error_response("not_found", "API route not found.", 404)
        index = static_dir / "index.html"
        if not index.is_file():
            return Response(
                "Pandrator web assets have not been built. Run the frontend build first.",
                status=503,
                mimetype="text/plain",
            )
        response = send_file(index)
        response.headers["Cache-Control"] = "no-store"
        return response

    register_account_routes(app, context)
    app.register(flask_app)
