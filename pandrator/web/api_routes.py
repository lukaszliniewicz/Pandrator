"""HTTP route definitions for the browser and API clients."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
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
from sqlalchemy import func, select
from werkzeug.utils import secure_filename

from pandrator.logic.tts_provider_switch import (
    normalize_tts_voice_aliases,
    prepare_tts_provider_switch,
)
from pandrator.version import PANDRATOR_VERSION

from .agentic_runs import AgenticRunStore
from .artifact_routes import register_artifact_routes
from .artifact_selection import (
    choose_artifact,
    clear_selection,
    rerun_impact,
    stage_history,
    trash_stage_artifact,
)
from .artifacts import sha256_file
from .auth import ALL_SCOPES, MCP_BOOTSTRAP_SCOPES, normalize_scopes
from .automation_routes import register_automation_routes
from .credentials import (
    contains_inline_secret,
    prepare_stt_settings_for_storage,
    prepare_tts_settings_for_storage,
    redact_inline_secrets,
)
from .dispatch_routes import register_dispatch_routes
from .domain_blueprints import DomainBlueprints
from .http_idempotency import MutationIdempotency
from .http_serialization import job_payload as _job_payload
from .http_serialization import model_payload as _model_dict
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .knowledge import KnowledgeLedgerStore, KnowledgeValidationError
from .managed_services import binding_for_provider, normalize_tts_provider_id
from .manager_proxy import register_manager_routes
from .media_edit_dispatch_routes import register_media_edit_dispatch_routes
from .media_edit_routes import register_media_edit_routes
from .models import (
    AgentRun,
    AgentStep,
    AppSetting,
    AppSettingHistory,
    Artifact,
    Document,
    DocumentRevision,
    Job,
    OutputAssembly,
    Segment,
    SessionRecord,
    SourceAsset,
    SourceRecord,
    TimedWord,
    TrainingRun,
    Voice,
    new_id,
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
    GenerationPlanCreate,
    GenerationPlanTopologyRequest,
    GenerationSegmentBatchUpdate,
    GenerationSegmentUpdate,
    GenerationStartRequest,
    JobCreate,
    LoginRequest,
    ManagerBootstrapRequest,
    OutcomePlanUpdate,
    OutputAssemblyCreateRequest,
    OutputMixPreviewRequest,
    PdfEditRequest,
    PronunciationCreate,
    PronunciationUpdate,
    RvcConvertRequest,
    RvcModelUploadRequest,
    SettingUpdate,
    SourceAttachRequest,
    SourceReuseRequest,
    SourceUpdateRequest,
    SourceUrlRequest,
    StageSelectionUpdate,
    SubtitleEvidenceCreateRequest,
    SubtitleEvidenceResolveRequest,
    SubtitlePassageReviewRequest,
    SubtitleReviewRequest,
    TokenCreateRequest,
    TrainingCreateRequest,
)
from .service_routes import register_service_routes
from .session_routes import (
    register_session_lifecycle_routes,
    register_session_list_routes,
)
from .session_settings_routes import register_session_settings_routes
from .settings_policy import BUILTIN_DEFAULTS, SETTING_SECTIONS
from .settings_policy import RevisionConflict as WorkspaceRevisionConflict
from .source_cleaning_dispatch_routes import (
    register_source_cleaning_dispatch_routes,
)
from .source_resolution import resolve_media_source
from .speech_optimization_dispatch_routes import (
    register_speech_optimization_dispatch_routes,
)
from .voice_routes import register_voice_routes
from .workflow_improvements_routes import register_workflow_improvements_routes
from .workflow_plan_routes import register_workflow_plan_routes
from .workflow_routes import (
    WorkflowRouteContext,
    register_workflow_job_routes,
    register_workflow_session_routes,
)


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
            "name",
            "storage_key",
            "workflow_kind",
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
    capability_service = services.capabilities
    jobs = services.jobs
    work = services.work
    sessions = services.sessions
    artifacts = services.artifacts
    workflows = services.workflows
    workflow_handlers = services.workflow_handlers
    outcome_plans = services.outcome_plans
    source_library = services.source_library
    generation = services.generation
    pronunciations = services.pronunciations
    chunk_uploads = services.chunk_uploads
    subtitle_review = services.subtitle_review
    subtitle_evidence = services.subtitle_evidence
    bootstrap = services.bootstrap
    static_dir = context.static_dir
    error_response = context.guards.error_response
    inline_credential_error = context.guards.inline_credential_error
    authenticated = context.guards.authenticated
    require_auth = context.guards.require_auth


    idempotency = MutationIdempotency(context)
    mutation_idempotency_key = idempotency.require_key
    idempotency_failure = idempotency.failure
    abandon_idempotency = idempotency.abandon

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
            setting = db_session.get(AppSetting, "services.tts")
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
    from .audiobook_routes import register_audiobook_routes
    from .speech_selection_routes import register_speech_selection_routes

    register_audiobook_routes(app, context)
    from .session_purge_routes import register_session_purge_routes

    register_session_purge_routes(app, context)
    register_speech_selection_routes(app, context)
    from .voice_catalog_routes import register_voice_catalog_routes

    register_voice_catalog_routes(app, context)
    register_voice_routes(app, context)
    register_quick_transcription_routes(app, context)

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
                "initialized": auth.initialized(),
                "authenticated": authenticated(),
                "csrf_token": session.get("csrf_token")
                if session.get("authenticated")
                else None,
                "principal": (
                    {
                        "subject": principal.subject,
                        "kind": principal.kind,
                        "scopes": sorted(principal.scopes),
                        "client_id": principal.client_id,
                    }
                    if principal is not None
                    else None
                ),
                "remote_access": remote_access,
                "secure_transport": bool(request.is_secure),
                "security_warning": warning,
            }
        )

    @app.post("/api/v1/auth/bootstrap")
    def auth_bootstrap():
        payload = BootstrapRequest.model_validate(request.get_json(silent=True) or {})
        grant = bootstrap.consume_grant(payload.token)
        if grant is None:
            return error_response(
                "invalid_bootstrap_token",
                "The local bootstrap token is invalid or expired.",
                401,
            )
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(24)
        session["principal_subject"] = grant.subject
        session["principal_kind"] = grant.kind
        session["principal_scopes"] = sorted(grant.scopes)
        if grant.client_id:
            session["principal_client_id"] = grant.client_id
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
        return jsonify(
            {
                "token": bootstrap.issue(
                    subject="owner",
                    kind="owner_session",
                    scopes=ALL_SCOPES,
                    client_id="pandrator-manager-browser",
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
                    "app.admin",
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
                ),
                "expires_in_seconds": 120,
                "scopes": sorted(selected_scopes),
            }
        )

    @app.post("/api/v1/auth/login")
    def auth_login():
        payload = LoginRequest.model_validate(request.get_json(silent=True) or {})
        client_key = request.remote_addr or "unknown"
        remote_access = not _is_loopback_address(client_key)
        retry_after = login_throttle.retry_after(client_key) if remote_access else 0
        if retry_after:
            response, status = error_response(
                "login_throttled",
                "Too many failed sign-in attempts. Try again later.",
                429,
                {"retry_after_seconds": retry_after},
            )
            response.headers["Retry-After"] = str(retry_after)
            return response, status
        if not auth.verify_password(payload.password):
            retry_after = (
                login_throttle.record_failure(client_key) if remote_access else 0
            )
            response, status = error_response(
                "invalid_credentials",
                "The password is incorrect.",
                401,
                {"retry_after_seconds": retry_after} if retry_after else None,
            )
            if retry_after:
                response.headers["Retry-After"] = str(retry_after)
            return response, status
        if remote_access:
            login_throttle.reset(client_key)
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(24)
        session["principal_subject"] = "owner"
        session["principal_kind"] = "owner_session"
        session["principal_scopes"] = sorted(ALL_SCOPES)
        return jsonify({"authenticated": True, "csrf_token": session["csrf_token"]})

    @app.post("/api/v1/auth/logout")
    @require_auth
    def auth_logout():
        session.clear()
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
                    for item in auth.list_tokens()
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
        token, raw = auth.create_api_token(
            payload.label,
            scopes=payload.scopes,
            expires_at=expires_at,
            created_by=principal.subject,
            target_instance_id=identity.instance_id,
            canonical_origin=identity.canonical_origin,
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
            auth.revoke_token(token_id)
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

    @app.get("/api/v1/defaults/<section>")
    @require_auth
    def global_default_get(section: str):
        from .settings_policy import split_legacy_stt_settings

        if section not in SETTING_SECTIONS:
            return error_response("not_found", "Settings section not found.", 404)
        with database.session() as db_session:
            record = db_session.get(AppSetting, f"defaults.{section}")
            value = (
                dict(record.value_json or {})
                if record and isinstance(record.value_json, dict)
                else {}
            )
            revision = record.revision if record else 0
            if section == "stt":
                value, _ = split_legacy_stt_settings(value, reject_conflicts=False)
            elif section == "subtitles":
                legacy = db_session.get(AppSetting, "defaults.stt")
                _, legacy_subtitles = split_legacy_stt_settings(
                    dict(legacy.value_json or {}) if legacy else {}, reject_conflicts=False,
                )
                value = {**legacy_subtitles, **value}
        response = jsonify(
            redact_inline_secrets(
                {
                    "section": section,
                    "builtin": BUILTIN_DEFAULTS[section],
                    "value": value,
                    "effective": {**BUILTIN_DEFAULTS[section], **value},
                    "revision": revision,
                }
            )
        )
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.get("/api/v1/settings/<setting_key>")
    @require_auth
    def setting_get(setting_key: str):
        with database.session() as db_session:
            record = db_session.get(AppSetting, setting_key)
            if record is None:
                return error_response("not_found", "Setting not found.", 404)
            response = jsonify(
                {
                    "key": record.key,
                    "value": redact_inline_secrets(record.value_json),
                    "revision": record.revision,
                    "updated_at": record.updated_at.isoformat(),
                }
            )
            response.headers["ETag"] = f'"{record.revision}"'
            return response

    @app.put("/api/v1/settings/<setting_key>")
    @require_auth
    def setting_put(setting_key: str):
        if not setting_key or len(setting_key) > 120:
            return error_response("validation_error", "Invalid setting key.", 422)
        payload = SettingUpdate.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            with database.immediate_session() as db_session:
                record = db_session.get(AppSetting, setting_key)
                if record is None:
                    if raw_etag not in {"", "0", "*"}:
                        return error_response(
                            "revision_conflict",
                            "The setting does not exist at that revision.",
                            409,
                        )
                else:
                    try:
                        expected = int(raw_etag)
                    except ValueError:
                        return error_response(
                            "precondition_required",
                            "If-Match must contain the current setting revision.",
                            428,
                        )
                    if expected != record.revision:
                        return error_response(
                            "revision_conflict",
                            "The setting changed in another client.",
                            409,
                        )
                prepared_value = (
                    prepare_tts_settings_for_storage(
                        db_session,
                        database,
                        paths,
                        payload.value,
                        record.value_json if record is not None else {},
                    )
                    if setting_key == "services.tts"
                    else prepare_stt_settings_for_storage(
                        db_session,
                        database,
                        paths,
                        payload.value,
                        record.value_json if record is not None else {},
                    )
                    if setting_key == "services.stt"
                    else payload.value
                )
                if setting_key == "defaults.stt":
                    from .settings_policy import split_legacy_stt_settings, validate_stt_replacement
                    from .workspace_settings import migrate_legacy_subtitle_settings

                    prepared_value, incoming_subtitles = split_legacy_stt_settings(prepared_value)
                    previous_stt, previous_subtitles = split_legacy_stt_settings(
                        dict(record.value_json or {}) if record else {}, reject_conflicts=False,
                    )
                    validate_stt_replacement(prepared_value, previous_stt)
                    migrate_legacy_subtitle_settings(db_session, {**previous_subtitles, **incoming_subtitles})
                if setting_key == "defaults.tts":
                    from .settings_policy import validate_voiceover_repair_settings

                    validate_voiceover_repair_settings(prepared_value)
                    previous = {
                        **BUILTIN_DEFAULTS["tts"],
                        **(record.value_json if record is not None else {}),
                    }
                    prepared_value = normalize_tts_voice_aliases(
                        prepare_tts_provider_switch(previous, prepared_value)
                    )
                if setting_key == "defaults.source_passages":
                    from pandrator.logic.dubbing.source_passage_settings import (
                        SOURCE_PASSAGE_DEFAULTS,
                        normalize_source_passage_settings,
                    )

                    if not isinstance(prepared_value, dict):
                        raise ValueError(
                            "source_passages defaults must be an object."
                        )
                    unknown = set(prepared_value) - set(SOURCE_PASSAGE_DEFAULTS)
                    if unknown:
                        raise ValueError(
                            f"Unknown source_passages keys: {sorted(unknown)}"
                        )
                    # Like other settings, PUT replaces the sparse defaults.
                    # In particular, {} restores built-in defaults.
                    try:
                        normalize_source_passage_settings(prepared_value)
                    except TypeError as error:
                        raise ValueError(str(error)) from error
                if setting_key not in {
                    "services.tts",
                    "services.stt",
                } and contains_inline_secret(prepared_value):
                    raise ValueError(
                        "API keys and other credentials must be saved in provider settings."
                    )
                if record is None:
                    record = AppSetting(
                        key=setting_key, value_json=prepared_value, revision=1
                    )
                    db_session.add(record)
                else:
                    db_session.add(
                        AppSettingHistory(
                            key=record.key,
                            value_json=record.value_json,
                            revision=record.revision,
                        )
                    )
                    record.value_json = prepared_value
                    record.revision += 1
                    record.updated_at = utcnow()
                db_session.flush()
                result = {
                    "key": record.key,
                    "value": redact_inline_secrets(record.value_json),
                    "revision": record.revision,
                    "updated_at": record.updated_at.isoformat(),
                }
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        except (OSError, RuntimeError) as error:
            return error_response("credential_unavailable", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    register_session_lifecycle_routes(
        app, context, idempotency=idempotency,
        _session_payload=lambda record: _session_payload(record),
    )

    register_session_settings_routes(app, context, idempotency=idempotency)

    @app.get("/api/v1/sessions/<session_id>/outcome-plan")
    @require_auth
    def outcome_plan_get(session_id: str):
        try:
            result = outcome_plans.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.put("/api/v1/sessions/<session_id>/outcome-plan")
    @require_auth
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

    @app.get("/api/v1/sources")
    @require_auth
    def source_library_list():
        return jsonify(
            {
                "items": source_library.list(
                    include_trashed=request.args.get("include_trashed") == "true"
                )
            }
        )

    @app.patch("/api/v1/sources/<source_asset_id>")
    @require_auth
    def source_library_update(source_asset_id: str):
        payload = SourceUpdateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current source revision.",
                428,
            )
        try:
            result = source_library.rename(
                source_asset_id, expected, payload.display_name
            )
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/sources/<source_asset_id>")
    @require_auth
    def source_library_trash(source_asset_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current source revision.",
                428,
            )
        try:
            result = source_library.set_state(source_asset_id, expected, "trashed")
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("source_in_use", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sources/<source_asset_id>/restore")
    @require_auth
    def source_library_restore(source_asset_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current source revision.",
                428,
            )
        try:
            result = source_library.set_state(source_asset_id, expected, "current")
        except KeyError:
            return error_response("not_found", "Source asset not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/sources")
    @require_auth
    def session_source_list(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"items": source_library.list(session_id=session_id)})

    @app.post("/api/v1/sessions/<session_id>/sources")
    @require_auth
    def session_source_attach(session_id: str):
        payload = SourceAttachRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            raw_etag = request.headers.get(
                "If-Match",
                "",
            ).strip('W/" ')
            try:
                expected_session_revision = int(raw_etag)
            except ValueError:
                return error_response(
                    "precondition_required",
                    "If-Match must contain the current session revision.",
                    428,
                )
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="attachSessionSource",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "source_asset_id": (payload.source_asset_id),
                            "role": payload.role,
                            "expected_session_revision": (expected_session_revision),
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("session_revision") is not None:
                            response.headers["ETag"] = f'"{result["session_revision"]}"'
                        return response
                    session_record = db_session.get(
                        SessionRecord,
                        session_id,
                    )
                    if session_record is None:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "not_found",
                            "Session not found.",
                            404,
                        )
                    if session_record.revision != expected_session_revision:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "revision_conflict",
                            "The session changed before its source was attached.",
                            409,
                            {"current_revision": (session_record.revision)},
                        )
                    if (
                        db_session.get(
                            SourceAsset,
                            payload.source_asset_id,
                        )
                        is None
                    ):
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "not_found",
                            "Source asset not found.",
                            404,
                        )
                    result = source_library.attach(
                        session_id,
                        payload.source_asset_id,
                        role=payload.role,
                        expected_session_revision=(expected_session_revision),
                        db_session=db_session,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="session_source",
                        resource_id=str(result["id"]),
                    )
                    g.audit_resource_kind = "session_source"
                    g.audit_resource_id = str(result["id"])
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                if isinstance(error, WorkspaceRevisionConflict):
                    return error_response(
                        "revision_conflict",
                        str(error),
                        409,
                    )
                return idempotency_failure(error)
            response = jsonify(result)
            response.status_code = 201
            response.headers["ETag"] = f'"{result["session_revision"]}"'
            return response
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag) if raw_etag else None
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current session revision.", 428)
        try:
            with database.immediate_session() as db_session:
                record = db_session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                result = source_library.attach(
                    session_id, payload.source_asset_id, role=payload.role,
                    expected_session_revision=record.revision if expected is None else expected,
                    db_session=db_session,
                )
        except KeyError:
            return error_response(
                "not_found", "Session or source asset not found.", 404
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["session_revision"]}"'
        return response, 201

    from .subtitle_source_routes import register_subtitle_source_routes

    register_subtitle_source_routes(app, services, require_auth, error_response)
    from .source_passage_routes import register_source_passage_routes

    register_source_passage_routes(app, context)
    from .session_flow_routes import register_session_flow_routes

    register_session_flow_routes(app, services, require_auth, error_response, context.guards.principal)

    from .translation_project_routes import register_translation_project_routes

    register_translation_project_routes(
        app, services, require_auth, error_response, context.guards.principal
    )
    from .project_operation_routes import register_project_operation_routes

    register_project_operation_routes(app, context)
    from .project_export_bundle_routes import register_project_export_bundle_routes

    register_project_export_bundle_routes(app, context)

    @app.post("/api/v1/sessions/<session_id>/sources/adopt-subtitles")
    @require_auth
    def session_source_adopt_subtitles(session_id: str):
        payload = SourceAttachRequest.model_validate(request.get_json(silent=True) or {})
        if payload.role != "primary":
            return error_response("validation_error", "Only the primary subtitle source can be adopted.", 422)
        raw_revision = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_revision) if raw_revision else None
            result = source_library.adopt_subtitles(
                session_id, payload.source_asset_id, expected_session_revision=expected
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except KeyError:
            return error_response("not_found", "Attach this subtitle source as the session's primary source first.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["session_revision"]}"'
        return response, 200 if result["reused"] else 201

    @app.delete("/api/v1/sessions/<session_id>/sources/<attachment_id>")
    @require_auth
    def session_source_detach(session_id: str, attachment_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the attachment revision.",
                428,
            )
        try:
            source_library.detach(session_id, attachment_id, expected)
        except KeyError:
            return error_response(
                "not_found", "Session source attachment not found.", 404
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        return "", 204

    @app.get("/api/v1/sessions/<session_id>/documents")
    @require_auth
    def session_documents(session_id: str):
        with database.session() as db_session:
            if db_session.get(SessionRecord, session_id) is None:
                return error_response("not_found", "Session not found.", 404)
            documents = list(
                db_session.scalars(
                    select(Document)
                    .where(Document.session_id == session_id)
                    .order_by(Document.created_at)
                ).all()
            )
            revision_artifacts = {
                str((item.metadata_json or {}).get("revision_id") or ""): item
                for item in db_session.scalars(
                    select(Artifact).where(Artifact.session_id == session_id)
                ).all()
                if (item.metadata_json or {}).get("revision_id")
            }
            revisions_by_document: dict[
                str, list[tuple[DocumentRevision, int, int]]
            ] = {}
            if documents:
                revision_rows = db_session.execute(
                    select(
                        DocumentRevision,
                        func.count(Segment.id),
                        func.max(Segment.end_ms),
                    )
                    .outerjoin(Segment, Segment.revision_id == DocumentRevision.id)
                    .where(
                        DocumentRevision.document_id.in_(
                            document.id for document in documents
                        ),
                        ~DocumentRevision.id.in_(
                            select(Segment.revision_id).where(
                                Segment.node_kind == "logical_passage"
                            )
                        ),
                    )
                    .group_by(*DocumentRevision.__table__.columns)
                    .order_by(
                        DocumentRevision.document_id,
                        DocumentRevision.revision_number.desc(),
                    )
                ).all()
                for revision, segment_count, duration_ms in revision_rows:
                    revisions_by_document.setdefault(revision.document_id, []).append(
                        (
                            revision,
                            int(segment_count or 0),
                            int(duration_ms or 0),
                        )
                    )
            items = []
            for document in documents:
                revision_items = []
                for revision, segment_count, duration_ms in revisions_by_document.get(
                    document.id, []
                ):
                    artifact = revision_artifacts.get(revision.id)
                    revision_items.append(
                        {
                            "id": revision.id,
                            "revision_number": revision.revision_number,
                            "parent_revision_id": revision.parent_revision_id,
                            "reviewed": revision.reviewed,
                            "content_hash": revision.content_hash,
                            "created_at": revision.created_at.isoformat(),
                            "segment_count": int(segment_count or 0),
                            "duration_ms": int(duration_ms or 0),
                            "artifact": _model_dict(
                                artifact,
                                (
                                    "id",
                                    "kind",
                                    "role",
                                    "relative_path",
                                    "mime_type",
                                    "size_bytes",
                                    "state",
                                    "metadata_json",
                                    "created_at",
                                ),
                            )
                            if artifact
                            else None,
                        }
                    )
                items.append(
                    {
                        "id": document.id,
                        "stage": document.stage,
                        "language": document.language,
                        "active_revision_id": document.active_revision_id,
                        "created_at": document.created_at.isoformat(),
                        "revisions": revision_items,
                    }
                )
            return jsonify({"items": items})

    @app.get("/api/v1/document-revisions/<revision_id>/words")
    @require_auth
    def revision_words(revision_id: str):
        try:
            cursor = max(0, int(request.args.get("cursor") or 0))
            limit = max(1, min(1000, int(request.args.get("limit") or 500)))
        except ValueError:
            return error_response("validation_error", "Invalid pagination value.", 422)
        with database.session() as db_session:
            if db_session.get(DocumentRevision, revision_id) is None:
                return error_response("not_found", "Document revision not found.", 404)
            rows = list(
                db_session.scalars(
                    select(TimedWord)
                    .where(
                        TimedWord.revision_id == revision_id,
                        TimedWord.ordinal >= cursor,
                    )
                    .order_by(TimedWord.ordinal)
                    .limit(limit + 1)
                ).all()
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            return jsonify(
                {
                    "items": [
                        _model_dict(
                            word,
                            (
                                "id",
                                "revision_id",
                                "segment_id",
                                "ordinal",
                                "text",
                                "start_ms",
                                "end_ms",
                                "speaker",
                                "confidence",
                                "metadata_json",
                            ),
                        )
                        for word in rows
                    ],
                    "next_cursor": rows[-1].ordinal + 1 if rows and has_more else None,
                }
            )

    @app.post("/api/v1/sessions/<session_id>/generation-plan")
    @require_auth
    def generation_plan_create(session_id: str):
        payload = GenerationPlanCreate.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.settings):
            return rejected
        try:
            result = generation.create_plan(
                session_id,
                source_revision_id=payload.source_revision_id,
                segments=[item.model_dump() for item in payload.segments],
                settings=payload.settings,
            )
        except KeyError:
            return error_response(
                "not_found", "Session or source revision not found.", 404
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

    @app.post("/api/v1/sessions/<session_id>/generation-plan/topology")
    @require_auth
    def generation_plan_topology(session_id: str):
        payload = GenerationPlanTopologyRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if not raw_etag:
            return error_response(
                "precondition_required",
                "If-Match must contain the current plan revision ID.",
                428,
            )
        if raw_etag != payload.expected_revision_id:
            return error_response(
                "revision_conflict",
                "The request revision does not match If-Match.",
                409,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is None:
            return error_response(
                "idempotency_key_required",
                "Speech-block topology revisions require Idempotency-Key.",
                400,
            )
        operation = payload.model_dump(
            exclude={"expected_revision_id"}, exclude_none=True
        )
        try:
            with database.immediate_session() as db_session:
                reservation = services.idempotency.begin(
                    db_session,
                    principal=context.guards.principal(),
                    operation_id="reviseGenerationPlanTopology",
                    idempotency_key=idempotency_key,
                    payload={
                        "session_id": session_id,
                        "expected_revision_id": payload.expected_revision_id,
                        **operation,
                    },
                )
                if reservation.response is not None:
                    result, status_code = reservation.response
                    response = jsonify(result)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
                    return response
                result = generation.revise_topology_in_session(
                    db_session,
                    session_id,
                    payload.expected_revision_id,
                    operation,
                )
                services.idempotency.complete(
                    db_session,
                    reservation,
                    response=result,
                    status_code=201,
                    resource_kind="generation_plan_revision",
                    resource_id=result["plan_revision_id"],
                )
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            if isinstance(error, WorkspaceRevisionConflict):
                return error_response("revision_conflict", str(error), 409)
            return idempotency_failure(error)
        except KeyError:
            return error_response(
                "not_found",
                "Session, generation plan revision, or segment not found.",
                404,
            )
        response = jsonify(result)
        response.status_code = 201
        response.headers["Idempotency-Replayed"] = "false"
        response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/generation-plan/revisions")
    @require_auth
    def generation_plan_revisions(session_id: str):
        from .generation_review import parse_summary_flag, revision_history

        try:
            result = revision_history(
                database, session_id, limit=int(request.args.get("limit", 50)),
                before_revision_number=int(request.args["before_revision_number"]) if "before_revision_number" in request.args else None,
                include_audio_reuse=not parse_summary_flag(request.args.get("summary")),
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.get("/api/v1/sessions/<session_id>/generation-plan/revisions/<revision_id>")
    @require_auth
    def generation_plan_revision_detail(session_id: str, revision_id: str):
        from .generation_review import revision_detail

        try:
            return jsonify(revision_detail(database, session_id, revision_id))
        except KeyError:
            return error_response("not_found", "Session or speech-plan revision not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.post("/api/v1/sessions/<session_id>/generation-plan/topology/batch")
    @require_auth
    def generation_plan_topology_batch(session_id: str):
        from .generation_review import revise_topology_batch_in_session
        from .schemas import GenerationPlanBatchRequest

        payload = GenerationPlanBatchRequest.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        if not raw_etag:
            return error_response("precondition_required", "If-Match must contain the current speech-plan revision ID.", 428)
        if raw_etag != payload.expected_revision_id:
            return error_response("revision_conflict", "The request revision does not match If-Match.", 409)
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is None:
            return error_response("idempotency_key_required", "Atomic topology batches require Idempotency-Key.", 400)
        request_payload = {"session_id": session_id, **payload.model_dump(exclude_none=True)}
        try:
            with database.immediate_session() as db_session:
                reservation = services.idempotency.begin(
                    db_session, principal=context.guards.principal(), operation_id="reviseGenerationPlanTopologyBatch",
                    idempotency_key=idempotency_key, payload=request_payload,
                )
                if reservation.response is not None:
                    result, status_code = reservation.response
                    response = jsonify(result)
                    response.status_code = status_code
                    response.headers["Idempotency-Replayed"] = "true"
                    response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
                    return response
                result = revise_topology_batch_in_session(
                    generation, db_session, session_id, payload.expected_revision_id,
                    [operation.model_dump(exclude_none=True) for operation in payload.operations],
                )
                services.idempotency.complete(
                    db_session, reservation, response=result, status_code=201,
                    resource_kind="generation_plan_revision", resource_id=result["plan_revision_id"],
                )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
            return idempotency_failure(error)
        except KeyError:
            return error_response("not_found", "Session, revision, or segment not found.", 404)
        response = jsonify(result)
        response.status_code = 201
        response.headers["ETag"] = f'"{result["plan_revision_id"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/generation-segments")
    @require_auth
    def generation_segment_list(session_id: str):
        def optional_bool_arg(name: str, *, default: bool | None = None) -> bool | None:
            raw = request.args.get(name)
            if raw is None:
                return default
            normalized = raw.strip().lower()
            if normalized not in {"true", "false"}:
                raise ValueError(f"{name} must be true or false.")
            return normalized == "true"

        try:
            marked = optional_bool_arg("marked")
            result = generation.list_segments(
                session_id,
                cursor=request.args.get("cursor", 0, type=int),
                limit=request.args.get("limit", 100, type=int),
                status=request.args.get("status"),
                marked=marked,
                verification=request.args.get("verification"),
                generation_run_id=request.args.get("generation_run_id"),
                plan_revision_id=request.args.get("plan_revision_id"),
                view=request.args.get("view", "full"),
                fields=request.args["fields"].split(",") if "fields" in request.args else None,
                end_ordinal=int(request.args["end_ordinal"]) if "end_ordinal" in request.args else None,
                around_ordinal=int(request.args["around_ordinal"]) if "around_ordinal" in request.args else None,
                source_cue_id=request.args.get("source_cue_id"),
                radius=int(request.args.get("radius", 2)),
                q=request.args.get("q"),
                match_case=optional_bool_arg("match_case", default=False),
                whole_word=optional_bool_arg("whole_word", default=False),
                text_field=request.args.get("text_field", "text"),
                boundary_flags=optional_bool_arg("boundary_flags"),
            )
        except KeyError:
            return error_response(
                "not_found",
                "Session or generation run not found.",
                404,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.patch("/api/v1/sessions/<session_id>/generation-segments")
    @require_auth
    def generation_segments_update(session_id: str):
        payload = GenerationSegmentBatchUpdate.model_validate(
            request.get_json(silent=True) or {}
        )
        clearable = {"optimized_text", "voice_id", "voice", "language"}
        updates = []
        for item in payload.updates:
            changes = item.changes.model_dump(exclude_unset=True)
            if not changes:
                return error_response(
                    "validation_error",
                    "Every generation segment update requires at least one change.",
                    422,
                )
            null_fields = [
                key
                for key, value in changes.items()
                if value is None and key not in clearable
            ]
            if null_fields:
                return error_response(
                    "validation_error",
                    f"{', '.join(null_fields)} cannot be null.",
                    422,
                )
            updates.append(
                {
                    "id": item.id,
                    "revision": item.revision,
                    "changes": changes,
                }
            )
        try:
            result = generation.update_segments(session_id, updates)
        except KeyError:
            return error_response(
                "not_found",
                "Session or generation segment not found.",
                404,
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.patch("/api/v1/generation-segments/<segment_id>")
    @require_auth
    def generation_segment_update(segment_id: str):
        payload = GenerationSegmentUpdate.model_validate(
            request.get_json(silent=True) or {}
        )
        changes = payload.model_dump(exclude_unset=True)
        clearable = {"optimized_text", "voice_id", "voice", "language"}
        null_fields = [
            key
            for key, value in changes.items()
            if value is None and key not in clearable
        ]
        if null_fields:
            return error_response(
                "validation_error",
                f"{', '.join(null_fields)} cannot be null.",
                422,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current segment revision.",
                428,
            )
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="updateGenerationSegment",
                        idempotency_key=idempotency_key,
                        payload={
                            "segment_id": segment_id,
                            "expected_revision": expected,
                            "changes": changes,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    result = generation.update_segment_in_session(
                        db_session, segment_id, expected, changes
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="generation_segment",
                        resource_id=segment_id,
                    )
            except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
                if isinstance(error, WorkspaceRevisionConflict):
                    return error_response("revision_conflict", str(error), 409)
                return idempotency_failure(error)
            except KeyError:
                return error_response("not_found", "Generation segment not found.", 404)
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            # Explicit null clears a segment override back to the inherited
            # session value; omitted fields remain unchanged.
            result = generation.update_segment(segment_id, expected, changes)
        except KeyError:
            return error_response("not_found", "Generation segment not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/generation-segments/<segment_id>/takes/<take_id>/select")
    @require_auth
    def generation_take_select(segment_id: str, take_id: str):
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current segment revision.",
                428,
            )
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="selectGenerationTake",
                        idempotency_key=idempotency_key,
                        payload={
                            "segment_id": segment_id,
                            "take_id": take_id,
                            "expected_revision": expected,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    result = generation.select_take_in_session(
                        db_session, segment_id, take_id, expected
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="generation_segment",
                        resource_id=segment_id,
                    )
            except (IdempotencyConflict, IdempotencyInProgress, ValueError) as error:
                if isinstance(error, WorkspaceRevisionConflict):
                    return error_response("revision_conflict", str(error), 409)
                if isinstance(error, ValueError):
                    return error_response("invalid_take", str(error), 409)
                return idempotency_failure(error)
            except KeyError:
                return error_response(
                    "not_found", "Generation segment or audio take not found.", 404
                )
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            result = generation.select_take(segment_id, take_id, expected)
        except KeyError:
            return error_response(
                "not_found", "Generation segment or audio take not found.", 404
            )
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("invalid_take", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.get("/api/v1/sessions/<session_id>/generation-runs/latest")
    @require_auth
    def generation_run_latest(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"item": generation.latest_run(session_id)})

    @app.get("/api/v1/sessions/<session_id>/generation-runs")
    @require_auth
    def generation_run_list(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        raw_limit = request.args.get("limit")
        try:
            limit = int(raw_limit) if raw_limit is not None else None
            if limit is not None and not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            return error_response(
                "validation_error", "limit must be an integer from 1 through 100", 422
            )
        include_repairs = request.args.get("include_repairs", "true")
        if include_repairs not in {"true", "false"}:
            return error_response(
                "validation_error", "include_repairs must be true or false", 422
            )
        return jsonify({
            "items": generation.list_runs(
                session_id, include_repairs=include_repairs == "true", limit=limit
            )
        })

    @app.post("/api/v1/sessions/<session_id>/generation-runs")
    @require_auth
    def generation_run_start(session_id: str):
        payload = GenerationStartRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.run_override):
            return rejected
        if rejected := inline_credential_error(payload.selected_segment_override):
            return rejected
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            principal = context.guards.principal()
            request_payload = {
                "session_id": session_id,
                **payload.model_dump(mode="json"),
            }
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=principal,
                        operation_id="startGenerationRun",
                        idempotency_key=idempotency_key,
                        payload=request_payload,
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except ValueError as error:
                return error_response("generation_unavailable", str(error), 409)

            reservation_id = reservation.record.id

            def abandon_generation_reservation() -> None:
                try:
                    with database.immediate_session() as db_session:
                        fresh = services.idempotency.load_in_progress(
                            db_session, reservation_id, principal=principal
                        )
                        services.idempotency.abandon_in_progress(db_session, fresh)
                except (IdempotencyInProgress, KeyError):
                    # A completed reservation must never be deleted.  A process
                    # crash can also intentionally leave its marker for stale
                    # recovery rather than turning it into a second execution.
                    pass

            try:
                prepared = generation.prepare_start(
                    session_id,
                    run_override=payload.run_override,
                    selected_segment_override=payload.selected_segment_override,
                    segment_ids=payload.segment_ids,
                    generation_run_id=payload.generation_run_id,
                    settings_source_run_id=payload.settings_source_run_id,
                    operation=payload.operation,
                    speech_plan_revision_id=payload.speech_plan_revision_id,
                    stale_only=payload.stale_only,
                    missing_only=payload.missing_only,
                    expected_selection_hash=payload.expected_selection_hash,
                )
            except KeyError:
                abandon_generation_reservation()
                return error_response("not_found", "Session not found.", 404)
            except ValueError as error:
                abandon_generation_reservation()
                return error_response("generation_unavailable", str(error), 409)
            except Exception:
                abandon_generation_reservation()
                raise

            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.load_in_progress(
                        db_session, reservation_id, principal=principal
                    )
                    result = generation.start_in_session(
                        db_session, session_id, prepared=prepared
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="generation_run",
                        resource_id=str(result["id"]),
                    )
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except KeyError:
                abandon_generation_reservation()
                return error_response("not_found", "Session not found.", 404)
            except ValueError as error:
                abandon_generation_reservation()
                return error_response("generation_unavailable", str(error), 409)
            except Exception:
                abandon_generation_reservation()
                raise
            return jsonify(result), 202
        try:
            result = generation.start(
                session_id,
                run_override=payload.run_override,
                selected_segment_override=payload.selected_segment_override,
                segment_ids=payload.segment_ids,
                generation_run_id=payload.generation_run_id,
                settings_source_run_id=payload.settings_source_run_id,
                operation=payload.operation,
                speech_plan_revision_id=payload.speech_plan_revision_id,
                stale_only=payload.stale_only,
                missing_only=payload.missing_only,
                expected_selection_hash=payload.expected_selection_hash,
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("generation_unavailable", str(error), 409)
        return jsonify(result), 202

    @app.post("/api/v1/sessions/<session_id>/generation-runs/preview")
    @require_auth
    def generation_run_preview(session_id: str):
        payload = GenerationStartRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.run_override):
            return rejected
        if rejected := inline_credential_error(payload.selected_segment_override):
            return rejected
        try:
            result = generation.preview_selection(
                session_id,
                run_override=payload.run_override,
                selected_segment_override=payload.selected_segment_override,
                segment_ids=payload.segment_ids,
                generation_run_id=payload.generation_run_id,
                settings_source_run_id=payload.settings_source_run_id,
                operation=payload.operation,
                speech_plan_revision_id=payload.speech_plan_revision_id,
                stale_only=payload.stale_only,
                missing_only=payload.missing_only,
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("generation_unavailable", str(error), 409)
        return jsonify(result), 200

    @app.post("/api/v1/generation-runs/<run_id>/pause")
    @require_auth
    def generation_run_pause(run_id: str):
        try:
            return jsonify(generation.request_pause(run_id)), 202
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)

    @app.post("/api/v1/generation-runs/<run_id>/resume")
    @require_auth
    def generation_run_resume(run_id: str):
        try:
            return jsonify(generation.resume(run_id)), 202
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)

    @app.post("/api/v1/generation-runs/<run_id>/cancel")
    @require_auth
    def generation_run_cancel(run_id: str):
        try:
            return jsonify(generation.cancel(run_id)), 202
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)

    @app.delete("/api/v1/generation-runs/<run_id>")
    @require_auth
    def generation_run_delete(run_id: str):
        try:
            generation.delete_run(run_id)
        except KeyError:
            return error_response("not_found", "Generation run not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)
        return "", 204

    @app.get("/api/v1/sessions/<session_id>/output-assemblies/latest")
    @require_auth
    def output_assembly_latest(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify({"item": generation.latest_assembly(session_id)})

    @app.post("/api/v1/sessions/<session_id>/output-assemblies")
    @require_auth
    def output_assembly_create(session_id: str):
        payload = OutputAssemblyCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.run_override):
            return rejected
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="createOutputAssembly",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            **payload.model_dump(mode="json"),
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    prepared = generation.prepare_assembly(
                        session_id, run_override=payload.run_override
                    )
                    result = generation.create_assembly_in_session(
                        db_session,
                        session_id,
                        generation_run_id=payload.generation_run_id,
                        prepared=prepared,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=202,
                        resource_kind="output_assembly",
                        resource_id=str(result["id"]),
                    )
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except KeyError:
                return error_response(
                    "not_found", "Session or generation run not found.", 404
                )
            except ValueError as error:
                return error_response("assembly_unavailable", str(error), 409)
            return jsonify(result), 202
        try:
            result = generation.create_assembly(
                session_id,
                generation_run_id=payload.generation_run_id,
                run_override=payload.run_override,
            )
        except KeyError:
            return error_response(
                "not_found", "Session or generation run not found.", 404
            )
        except ValueError as error:
            return error_response("assembly_unavailable", str(error), 409)
        return jsonify(result), 202

    @app.post("/api/v1/sessions/<session_id>/output-mix-preview")
    @require_auth
    def output_mix_preview(session_id: str):
        """Queue a bounded audio preview from server-resolved mix inputs."""

        payload = OutputMixPreviewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        with database.session() as db_session:
            record = db_session.get(SessionRecord, session_id)
            if record is None:
                return error_response("not_found", "Session not found.", 404)
            if record.workflow_kind != "voiceover":
                return error_response(
                    "mix_preview_unavailable",
                    "Soundtrack mix previews are available for voiceover sessions.",
                    409,
                )
            source_resolution = resolve_media_source(db_session, session_id)
            source = source_resolution.artifact
            if source is None or not source_resolution.has_audio:
                return error_response(
                    "mix_preview_unavailable",
                    "Attach an audio or video source before previewing the soundtrack mix.",
                    409,
                )
            assembly = db_session.scalar(
                select(OutputAssembly)
                .where(
                    OutputAssembly.session_id == session_id,
                    OutputAssembly.generation_run_id == payload.generation_run_id,
                    OutputAssembly.status == "completed",
                    OutputAssembly.artifact_id.is_not(None),
                )
                .order_by(OutputAssembly.created_at.desc())
            )
            if assembly is None:
                return error_response(
                    "mix_preview_unavailable",
                    "Assemble the selected audio version before previewing its soundtrack mix.",
                    409,
                )
            dubbing = db_session.get(Artifact, assembly.artifact_id)
            if dubbing is None or dubbing.state == "deleted":
                return error_response(
                    "mix_preview_unavailable",
                    "The selected audio version's assembly is unavailable.",
                    409,
                )
            source_artifact_id = source.id
            dubbing_artifact_id = dubbing.id

        mix_fields = (
            "mix_source_gain_db",
            "mix_voice_gain_db",
            "mix_voice_lufs",
            "mix_ducking",
            "mix_attack_ms",
            "mix_release_ms",
        )
        job = jobs.enqueue(
            "output.mix_preview",
            {
                "session_id": session_id,
                "generation_run_id": payload.generation_run_id,
                "source_artifact_id": source_artifact_id,
                "dubbing_artifact_id": dubbing_artifact_id,
                "start_seconds": payload.start_seconds,
                "duration_seconds": payload.duration_seconds,
                "settings": {field: getattr(payload, field) for field in mix_fields},
            },
            session_id=session_id,
            resource_keys=[f"session:{session_id}"],
        )
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/sessions/<session_id>/agent-runs")
    @require_auth
    def agent_run_list(session_id: str):
        with database.session() as db_session:
            if db_session.get(SessionRecord, session_id) is None:
                return error_response("not_found", "Session not found.", 404)
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
    def agent_run_create(session_id: str):
        payload = AgentRunCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.settings):
            return rejected
        try:
            sessions.get(session_id)
            source_artifact, source_path = artifacts.resolve(payload.source_artifact_id)
        except KeyError:
            return error_response(
                "not_found", "Session or source artifact not found.", 404
            )
        attached_source = next(
            (
                item
                for item in source_library.list(session_id=session_id)
                if item.get("artifact_id") == source_artifact.id
                and bool((item.get("attachment") or {}).get("is_current"))
            ),
            None,
        )
        if attached_source is None:
            return error_response(
                "invalid_source",
                "Source cleaning requires a source attached to this session.",
                422,
            )
        if source_path.suffix.lower() not in {
            ".docx",
            ".epub",
            ".mobi",
            ".pdf",
            ".txt",
        }:
            return error_response(
                "unsupported_source",
                "Source cleaning is available for text documents, not audio, video, or subtitle sources.",
                422,
            )
        run_id = new_id()
        with database.session() as db_session:
            db_session.add(
                AgentRun(
                    id=run_id,
                    kind="source_cleaning",
                    session_id=session_id,
                    source_artifact_id=payload.source_artifact_id,
                    status="queued",
                    settings_json={**payload.settings, "agentic": True},
                )
            )
        job = jobs.enqueue(
            "source.clean",
            {
                "session_id": session_id,
                "source_artifact_id": payload.source_artifact_id,
                "agent_run_id": run_id,
                "settings": {**payload.settings, "agentic": True},
            },
            session_id=session_id,
            resource_keys=[f"session:{session_id}", "service:llm"],
        )
        with database.session() as db_session:
            run = db_session.get(AgentRun, run_id)
            run.job_id = job.id
            run.updated_at = utcnow()
        return jsonify({"id": run_id, "job_id": job.id, "status": "queued"}), 202

    @app.get("/api/v1/agent-runs/<run_id>/steps")
    @require_auth
    def agent_step_list(run_id: str):
        with database.session() as db_session:
            if db_session.get(AgentRun, run_id) is None:
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
        store = AgenticRunStore(database)
        try:
            run, previous_job = store.prepare_resume(run_id)
        except KeyError:
            return error_response("not_found", "Agentic operation not found.", 404)
        except ValueError as error:
            return error_response("invalid_state", str(error), 409)
        payload = dict(previous_job.payload_json or {})
        run_ids = dict(payload.get("_agent_run_ids") or {})
        run_ids[run.kind] = run.id
        payload["_agent_run_ids"] = run_ids
        payload["_agent_run_id"] = run.id
        payload["agent_run_id"] = run.id
        try:
            job = jobs.enqueue(
                previous_job.kind,
                payload,
                session_id=run.session_id,
                workflow_run_id=previous_job.workflow_run_id,
                max_attempts=previous_job.max_attempts,
                resource_keys=list(previous_job.resource_keys_json or []),
            )
        except Exception as error:
            store.fail(run.id, error)
            raise
        with database.session() as db_session:
            managed = db_session.get(AgentRun, run.id)
            if managed is not None:
                managed.job_id = job.id
                managed.status = "retrying"
                managed.error_message = None
                managed.updated_at = utcnow()
        return jsonify({"id": run.id, "job_id": job.id, "status": "retrying"}), 202

    @app.get("/api/v1/sessions/<session_id>/knowledge/<kind>")
    @require_auth
    def knowledge_get(session_id: str, kind: str):
        try:
            sessions.get(session_id)
            result = KnowledgeLedgerStore(database).get(
                session_id,
                kind,
                source_language=str(request.args.get("source_language") or "auto"),
                target_language=str(request.args.get("target_language") or ""),
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result)

    @app.patch("/api/v1/knowledge/<ledger_id>")
    @require_auth
    def knowledge_update(ledger_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current ledger revision.",
                428,
            )
        body = request.get_json(silent=True) or {}
        payload = body.get("payload")
        if not isinstance(payload, dict):
            return error_response("validation_error", "payload must be an object.", 422)
        try:
            result = KnowledgeLedgerStore(database).replace(
                ledger_id,
                revision,
                payload,
            )
        except KeyError:
            return error_response("not_found", "Knowledge ledger not found.", 404)
        except KnowledgeValidationError as error:
            return error_response("validation_error", str(error), 422)
        except ValueError as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/agent-runs/<run_id>/accept")
    @require_auth
    def agent_run_accept(run_id: str):
        with database.session() as db_session:
            run = db_session.get(AgentRun, run_id)
            if run is None:
                return error_response(
                    "not_found", "Agentic cleaning run not found.", 404
                )
            if run.status != "completed" or not run.result_artifact_id:
                return error_response(
                    "invalid_state",
                    "Only a completed cleaning result can be accepted.",
                    409,
                )
            run.status = "accepted"
            run.updated_at = utcnow()
            return jsonify(
                _model_dict(run, ("id", "status", "result_artifact_id", "updated_at"))
            )

    @app.post("/api/v1/sessions/<session_id>/bundle")
    @require_auth
    def session_bundle_export(session_id: str):
        payload = BundleExportRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        job = jobs.enqueue(
            "session.bundle.export",
            {"session_id": session_id, "include_sources": payload.include_sources},
            session_id=session_id,
        )
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/session-bundles/import")
    @require_auth
    def session_bundle_import():
        payload = BundleImportRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            artifacts.resolve(payload.source_artifact_id)
        except KeyError:
            return error_response("not_found", "Bundle artifact not found.", 404)
        job = jobs.enqueue("session.bundle.import", payload.model_dump())
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/jobs")
    @require_auth
    def job_list():
        items = work.diagnostic_list(request.args.get("limit", 100, type=int))
        principal = context.guards.principal()
        assert principal is not None
        hidden = services.quick_transcriptions.hidden_job_ids(
            principal.subject, (item.id for item in items)
        )
        return jsonify(
            {
                "items": [
                    _job_payload(item)
                    for item in items
                    if item.id not in hidden
                ]
            }
        )

    @app.post("/api/v1/jobs")
    @require_auth
    def job_create():
        payload = JobCreate.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.payload):
            return rejected
        job = jobs.enqueue(
            payload.kind,
            payload.payload,
            session_id=payload.session_id,
            max_attempts=payload.max_attempts,
        )
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/jobs/<job_id>")
    @require_auth
    def job_get(job_id: str):
        try:
            return jsonify(_job_payload(work.diagnostic_get(job_id)))
        except KeyError:
            return error_response("not_found", "Job not found.", 404)

    @app.get("/api/v1/jobs/<job_id>/logs")
    @require_auth
    def job_logs(job_id: str):
        """Return the job's durable event and captured worker-log timeline."""
        try:
            events = work.diagnostic_events(
                job_id,
                request.args.get("limit", 1000, type=int),
            )
        except KeyError:
            return error_response("not_found", "Job not found.", 404)
        return jsonify(
            {
                "items": [
                    redact_inline_secrets(
                        {
                            "id": event.id,
                            "event_type": event.event_type,
                            "payload_json": event.payload_json,
                            "created_at": event.created_at.isoformat(),
                        }
                    )
                    for event in events
                ]
            }
        )

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
        items = work.list(
            session_id=str(request.args.get("session_id") or "").strip() or None,
            kinds=_query_values("kind"),
            states=_query_values("state"),
            limit=request.args.get("limit", 50, type=int) or 50,
        )
        principal = context.guards.principal()
        assert principal is not None
        hidden = services.quick_transcriptions.hidden_job_ids(
            principal.subject, (item.id for item in items)
        )
        return jsonify(
            {
                "schema_version": "1",
                "items": [
                    item.model_dump(mode="json")
                    for item in items
                    if item.id not in hidden
                ],
            }
        )

    @app.get("/api/v1/work/<job_id>")
    @require_auth
    def work_get(job_id: str):
        try:
            return jsonify(work.get(job_id).model_dump(mode="json"))
        except KeyError:
            return error_response("not_found", "Work item not found.", 404)

    @app.get("/api/v1/work/<job_id>/events")
    @require_auth
    def work_events(job_id: str):
        try:
            page = work.events(
                job_id,
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
                payload = work.cancel_in_session(
                    db_session,
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

    @app.post("/api/v1/sessions/<session_id>/subtitles/<stage>/review")
    @require_auth
    def subtitle_save_review(session_id: str, stage: str):
        payload = SubtitleReviewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            published_paths: list[Path] = []

            def cleanup_published() -> None:
                for published_path in published_paths:
                    try:
                        published_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="saveSubtitleReview",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "stage": stage,
                            **payload.model_dump(mode="json"),
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    result = subtitle_review.save_review_in_session(
                        db_session,
                        session_id,
                        stage,
                        payload.expected_revision,
                        [item.model_dump() for item in payload.segments],
                        source_artifact_id=payload.source_artifact_id,
                        expected_source_hash=payload.expected_source_hash,
                        published_paths=published_paths,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="subtitle_document",
                        resource_id=str(result["document_id"]),
                    )
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except KeyError:
                cleanup_published()
                return error_response("not_found", "Subtitle document not found.", 404)
            except RuntimeError as error:
                cleanup_published()
                return error_response("revision_conflict", str(error), 409)
            except ValueError as error:
                cleanup_published()
                return error_response("validation_error", str(error), 422)
            except Exception:
                cleanup_published()
                raise
            return jsonify(result), 201
        try:
            result = subtitle_review.save_review(
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.segments],
                source_artifact_id=payload.source_artifact_id,
                expected_source_hash=payload.expected_source_hash,
            )
        except KeyError:
            return error_response("not_found", "Subtitle document not found.", 404)
        except RuntimeError as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

    @app.post(
        "/api/v1/sessions/<session_id>/subtitles/<stage>/passage-review"
    )
    @require_auth
    def subtitle_save_passage_review(session_id: str, stage: str):
        payload = SubtitlePassageReviewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            published_paths: list[Path] = []

            def cleanup_published() -> None:
                for published_path in published_paths:
                    try:
                        published_path.unlink(missing_ok=True)
                    except OSError:
                        pass

            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="saveSubtitlePassageReview",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "stage": stage,
                            **payload.model_dump(mode="json"),
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    result = subtitle_review.save_passage_review_in_session(
                        db_session,
                        session_id,
                        stage,
                        payload.expected_revision,
                        [item.model_dump() for item in payload.passages],
                        source_artifact_id=payload.source_artifact_id,
                        expected_source_hash=payload.expected_source_hash,
                        expected_composition_hash=payload.expected_composition_hash,
                        published_paths=published_paths,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="subtitle_document",
                        resource_id=str(result["document_id"]),
                    )
            except (IdempotencyConflict, IdempotencyInProgress) as error:
                return idempotency_failure(error)
            except KeyError:
                cleanup_published()
                return error_response("not_found", "Subtitle document not found.", 404)
            except RuntimeError as error:
                cleanup_published()
                return error_response("revision_conflict", str(error), 409)
            except ValueError as error:
                cleanup_published()
                return error_response("validation_error", str(error), 422)
            except Exception:
                cleanup_published()
                raise
            return jsonify(result), 201
        try:
            result = subtitle_review.save_passage_review(
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.passages],
                source_artifact_id=payload.source_artifact_id,
                expected_source_hash=payload.expected_source_hash,
                expected_composition_hash=payload.expected_composition_hash,
            )
        except KeyError:
            return error_response("not_found", "Subtitle document not found.", 404)
        except RuntimeError as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

    @app.post("/api/v1/sessions/<session_id>/subtitle-evidence")
    @require_auth
    def subtitle_evidence_create(session_id: str):
        payload = SubtitleEvidenceCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        values = payload.model_dump(mode="json")
        if not values.get("force_refresh"):
            values.pop("force_refresh", None)
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        try:
            if idempotency_key is None:
                result = subtitle_evidence.request(session_id, values)
            else:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="requestSubtitleEvidence",
                        idempotency_key=idempotency_key,
                        payload={"session_id": session_id, **values},
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    result = subtitle_evidence.request_in_session(
                        db_session, session_id, values
                    )
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
        try:
            sessions.get(session_id)
            return jsonify(
                subtitle_evidence.list(
                    session_id,
                    request.args.get("source_artifact_id") or None,
                )
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.get("/api/v1/subtitle-evidence/<evidence_id>")
    @require_auth
    def subtitle_evidence_get(evidence_id: str):
        try:
            return jsonify(subtitle_evidence.get(evidence_id))
        except KeyError:
            return error_response("not_found", "Subtitle evidence not found.", 404)

    @app.post("/api/v1/sessions/<session_id>/subtitle-evidence/<evidence_id>/resolve")
    @require_auth
    def subtitle_evidence_resolve(session_id: str, evidence_id: str):
        payload = SubtitleEvidenceResolveRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        values = payload.model_dump(mode="json")
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        try:
            if idempotency_key is None:
                result = subtitle_evidence.resolve(session_id, evidence_id, values)
            else:
                with database.immediate_session() as db_session:
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
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response
                    result = subtitle_evidence.resolve_in_session(
                        db_session, session_id, evidence_id, values
                    )
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
            return jsonify(_job_payload(work.diagnostic_cancel(job_id)))
        except KeyError:
            return error_response("not_found", "Job not found.", 404)

    @app.get("/api/v1/events/snapshot")
    @require_auth
    def event_snapshot():
        # Capture the cursor before reading resources. Events committed while
        # the snapshot is assembled are replayed after this cursor.
        bounds = work.event_bounds()
        local_mode = _is_loopback_address(request.remote_addr)
        capability_payload = capability_service.get(local_mode=local_mode)
        capability_payload["application"] = {"version": PANDRATOR_VERSION}
        items = work.diagnostic_list(40)
        principal = context.guards.principal()
        assert principal is not None
        hidden = services.quick_transcriptions.hidden_job_ids(
            principal.subject, (item.id for item in items)
        )
        return jsonify(
            {
                "cursor": bounds.latest,
                "retained_after": bounds.retained_after,
                "sessions": {
                    "items": [_session_payload(item) for item in sessions.list()]
                },
                "jobs": {
                    "items": [
                        _job_payload(item) for item in items if item.id not in hidden
                    ]
                },
                "capabilities": capability_payload,
            }
        )

    @app.get("/api/v1/events")
    @require_auth
    def events():
        principal = context.guards.principal()
        assert principal is not None
        subject = principal.subject
        bounds = work.event_bounds()
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
                retained = work.event_bounds()
                if retained.oldest and cursor < retained.oldest - 1:
                    cursor = retained.latest
                    yield (
                        f"id: {cursor}\n"
                        "event: stream.reset\n"
                        f"data: {json.dumps({'cursor': cursor, 'reason': 'cursor_expired'})}\n\n"
                    )
                    continue
                new_events = work.events_after(cursor).items
                if new_events:
                    hidden = services.quick_transcriptions.hidden_job_ids(
                        subject, (event.work_id for event in new_events)
                    )
                    last_visible_id = cursor
                    for event in new_events:
                        cursor = event.id
                        if event.event_type == "job.log" or event.work_id in hidden:
                            continue
                        last_visible_id = event.id
                        payload = _sse_event_payload(event)
                        yield (
                            f"id: {event.id}\n"
                            f"event: {event.event_type}\n"
                            f"data: {json.dumps(payload)}\n\n"
                        )
                    if cursor > last_visible_id:
                        # Advance the browser reconnect cursor without exposing
                        # worker log records to every open tab.
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
    @require_auth
    def upload():
        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return error_response("missing_file", "A multipart file is required.", 400)
        filename = secure_filename(incoming.filename) or f"upload-{uuid.uuid4()}"
        temporary = paths.temporary / f"upload-{uuid.uuid4()}.part"
        destination = paths.uploads / f"{uuid.uuid4()}-{filename}"
        requested_session_id = str(request.form.get("session_id") or "") or None
        purpose = str(request.form.get("purpose") or "source").strip().lower()
        if purpose not in {"source", "cover"}:
            return error_response(
                "validation_error", "Unsupported upload purpose.", 422
            )
        if purpose == "cover" and not requested_session_id:
            return error_response(
                "validation_error", "Cover artwork must belong to a session.", 422
            )
        if requested_session_id:
            try:
                sessions.get(requested_session_id)
            except KeyError:
                return error_response("not_found", "Session not found.", 404)
        try:
            incoming.save(temporary)
            if purpose == "cover":
                if temporary.stat().st_size > 25 * 1024 * 1024:
                    return error_response(
                        "cover_too_large",
                        "Cover artwork must be 25 MiB or smaller.",
                        413,
                    )
                try:
                    from PIL import Image, UnidentifiedImageError

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
            digest = sha256_file(temporary)
            os.replace(temporary, destination)
            artifact = artifacts.register(
                destination,
                kind="image" if purpose == "cover" else "source",
                role="cover" if purpose == "cover" else "upload",
                session_id=requested_session_id,
                calculate_hash=False,
                metadata={"original_filename": incoming.filename, "purpose": purpose},
            )
            with database.session() as db_session:
                managed = db_session.get(Artifact, artifact.id)
                managed.content_hash = digest
                if requested_session_id and purpose == "source":
                    db_session.add(
                        SourceRecord(
                            session_id=requested_session_id,
                            kind=Path(filename).suffix.lower().lstrip(".") or "file",
                            display_name=incoming.filename,
                            artifact_id=artifact.id,
                            content_hash=digest,
                        )
                    )
            source_asset = None
            attachment = None
            if purpose == "source":
                source_asset = source_library.ensure_for_artifact(
                    artifact.id,
                    display_name=incoming.filename,
                    kind=Path(filename).suffix.lower().lstrip(".") or "file",
                )
                attachment = (
                    source_library.attach(requested_session_id, source_asset.id)
                    if requested_session_id
                    else None
                )
            return jsonify(
                {
                    "artifact_id": artifact.id,
                    "source_asset_id": source_asset.id if source_asset else None,
                    "attachment": attachment,
                    "filename": filename,
                    "size_bytes": destination.stat().st_size,
                    "sha256": digest,
                }
            ), 201
        finally:
            if temporary.exists():
                temporary.unlink()

    @app.post("/api/v1/uploads/init")
    @require_auth
    def chunk_upload_init():
        payload = ChunkUploadInitialize.model_validate(
            request.get_json(silent=True) or {}
        )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            upload_id = str(uuid.uuid4())
            created_directory: Path | None = None
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="initializeChunkUpload",
                        idempotency_key=idempotency_key,
                        payload=payload.model_dump(mode="json"),
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
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
                        expected_hash=payload.sha256,
                        chunk_size=payload.chunk_size,
                        max_size=int(
                            app.config.get(
                                "MAX_UPLOAD_SIZE",
                                100 * 1024 * 1024 * 1024,
                            )
                        ),
                        upload_id=upload_id,
                    )
                    created_directory = paths.managed_path(
                        record.temporary_relative_path
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
                if created_directory is not None:
                    shutil.rmtree(created_directory, ignore_errors=True)
                return error_response("not_found", "Session not found.", 404)
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                ValueError,
            ) as error:
                if created_directory is not None:
                    shutil.rmtree(created_directory, ignore_errors=True)
                return idempotency_failure(error)
            except Exception:
                if created_directory is not None:
                    shutil.rmtree(created_directory, ignore_errors=True)
                raise
        try:
            result = chunk_uploads.initialize(
                filename=payload.filename,
                size_bytes=payload.size_bytes,
                mime_type=payload.mime_type,
                session_id=payload.session_id,
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

    @app.post("/api/v1/sessions/<session_id>/sources/url")
    @require_auth
    def source_download_url(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        payload = SourceUrlRequest.model_validate(request.get_json(silent=True) or {})
        job = jobs.enqueue(
            "source.download_url",
            {"session_id": session_id, "url": payload.url},
            session_id=session_id,
        )
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/sessions/<session_id>/sources/reuse")
    @require_auth
    def source_reuse(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        payload = SourceReuseRequest.model_validate(request.get_json(silent=True) or {})
        try:
            artifacts.resolve(payload.artifact_id)
        except KeyError:
            return error_response(
                "not_found", "Reusable source artifact not found.", 404
            )
        job = jobs.enqueue(
            "source.reuse",
            {"session_id": session_id, "artifact_id": payload.artifact_id},
            session_id=session_id,
        )
        return jsonify(_job_payload(job)), 202

    register_artifact_routes(app, context)

    @app.get("/api/v1/artifacts/<artifact_id>/pdf")
    @require_auth
    def pdf_metadata(artifact_id: str):
        from .pdf_editor import inspect_pdf

        try:
            _artifact, path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Artifact not found.", 404)
        if path.suffix.lower() != ".pdf" or not path.is_file():
            return error_response(
                "invalid_pdf", "Artifact is not an available PDF.", 422
            )
        first_page_side = request.args.get("first_page_side", "right")
        try:
            return jsonify(inspect_pdf(path, first_page_side=first_page_side))
        except (ValueError, RuntimeError) as error:
            return error_response("invalid_pdf", str(error), 422)

    @app.post("/api/v1/sessions/<session_id>/pdf/apply")
    @require_auth
    def pdf_apply(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        payload = PdfEditRequest.model_validate(request.get_json(silent=True) or {})
        job = jobs.enqueue(
            "pdf.apply_edits",
            {
                "session_id": session_id,
                "source_artifact_id": payload.source_artifact_id,
                "plan": payload.model_dump(exclude={"source_artifact_id"}),
            },
            session_id=session_id,
        )
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
        payload = RvcModelUploadRequest.model_validate(
            request.get_json(silent=True) or {}
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
        job = jobs.enqueue("rvc.model.upload", payload.model_dump())
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/rvc/convert")
    @require_auth
    def rvc_convert():
        payload = RvcConvertRequest.model_validate(request.get_json(silent=True) or {})
        if rejected := inline_credential_error(payload.settings):
            return rejected
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
        job = jobs.enqueue(
            "rvc.convert",
            payload.model_dump(),
            session_id=payload.session_id,
            resource_keys=["service:rvc", "gpu:default"],
        )
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/training")
    @require_auth
    def training_list():
        with database.session() as db_session:
            records = list(
                db_session.scalars(
                    select(TrainingRun)
                    .order_by(TrainingRun.created_at.desc())
                    .limit(200)
                ).all()
            )
            for record in records:
                job = db_session.get(Job, record.job_id) if record.job_id else None
                if (
                    record.status in {"queued", "running", "cancel_requested"}
                    and job is not None
                    and job.status in {"failed", "canceled", "interrupted"}
                ):
                    record.status = job.status
                    record.error_message = job.error_message
                    record.updated_at = utcnow()
            return jsonify(
                {
                    "items": [
                        _model_dict(
                            item,
                            (
                                "id",
                                "kind",
                                "voice_id",
                                "job_id",
                                "source_artifact_id",
                                "source_text_artifact_id",
                                "output_artifact_id",
                                "model_name",
                                "status",
                                "settings_json",
                                "error_message",
                                "created_at",
                                "updated_at",
                            ),
                        )
                        for item in records
                    ]
                }
            )

    @app.post("/api/v1/training")
    @require_auth
    def training_create():
        payload = TrainingCreateRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.settings):
            return rejected
        try:
            artifacts.resolve(payload.source_artifact_id)
            if payload.source_text_artifact_id:
                artifacts.resolve(payload.source_text_artifact_id)
        except KeyError:
            return error_response(
                "not_found", "A training source artifact was not found.", 404
            )
        training_id = new_id()
        with database.session() as db_session:
            if payload.voice_id and db_session.get(Voice, payload.voice_id) is None:
                return error_response("not_found", "Voice not found.", 404)
            db_session.add(
                TrainingRun(
                    id=training_id,
                    kind="xtts",
                    voice_id=payload.voice_id,
                    source_artifact_id=payload.source_artifact_id,
                    source_text_artifact_id=payload.source_text_artifact_id,
                    model_name=payload.model_name,
                    settings_json=payload.settings,
                )
            )
        job = jobs.enqueue(
            "training.xtts",
            {
                "training_id": training_id,
                "model_name": payload.model_name,
                "source_artifact_id": payload.source_artifact_id,
                "source_text_artifact_id": payload.source_text_artifact_id,
                "settings": payload.settings,
            },
            resource_keys=["training:xtts", "gpu:default"],
        )
        with database.session() as db_session:
            training = db_session.get(TrainingRun, training_id)
            training.job_id = job.id
            training.updated_at = utcnow()
        response = _job_payload(job)
        response["training_id"] = training_id
        return jsonify(response), 202

    @app.post("/api/v1/training/<training_id>/retry")
    @require_auth
    def training_retry(training_id: str):
        with database.session() as db_session:
            previous = db_session.get(TrainingRun, training_id)
            if previous is None:
                return error_response("not_found", "Training run not found.", 404)
            if previous.status not in {"failed", "canceled", "interrupted"}:
                return error_response(
                    "training_active",
                    "Only failed, canceled, or interrupted training can be retried.",
                    409,
                )
            retry_id = new_id()
            db_session.add(
                TrainingRun(
                    id=retry_id,
                    kind=previous.kind,
                    voice_id=previous.voice_id,
                    source_artifact_id=previous.source_artifact_id,
                    source_text_artifact_id=previous.source_text_artifact_id,
                    model_name=previous.model_name,
                    settings_json=dict(previous.settings_json or {}),
                )
            )
            source_artifact_id = previous.source_artifact_id
            source_text_artifact_id = previous.source_text_artifact_id
            model_name = previous.model_name
            settings = dict(previous.settings_json or {})
        job = jobs.enqueue(
            "training.xtts",
            {
                "training_id": retry_id,
                "model_name": model_name,
                "source_artifact_id": source_artifact_id,
                "source_text_artifact_id": source_text_artifact_id,
                "settings": settings,
            },
            resource_keys=["training:xtts", "gpu:default"],
        )
        with database.session() as db_session:
            retry = db_session.get(TrainingRun, retry_id)
            retry.job_id = job.id
            retry.updated_at = utcnow()
        response = _job_payload(job)
        response["training_id"] = retry_id
        response["retried_from"] = training_id
        return jsonify(response), 202

    @app.post("/api/v1/training/<training_id>/cancel")
    @require_auth
    def training_cancel(training_id: str):
        with database.session() as db_session:
            training = db_session.get(TrainingRun, training_id)
            if training is None:
                return error_response("not_found", "Training run not found.", 404)
            job_id = training.job_id
            training.status = "cancel_requested"
            training.updated_at = utcnow()
        if job_id:
            try:
                jobs.request_cancel(job_id)
            except KeyError:
                pass
        return jsonify(
            {"id": training_id, "job_id": job_id, "status": "cancel_requested"}
        ), 202

    @app.get("/_app/<path:asset_path>")
    def frontend_asset(asset_path: str):
        response = send_from_directory(static_dir / "_app", asset_path)
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

    app.register(flask_app)
