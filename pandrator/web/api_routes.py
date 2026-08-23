"""HTTP route definitions for the browser and API clients."""

from __future__ import annotations

import ipaddress
import json
import os
import secrets
import shutil
import time
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any, Self
from urllib.parse import quote, urlsplit

import requests
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

from pandrator.runtime import DataPaths
from pandrator.version import PANDRATOR_VERSION

from .agentic_runs import AgenticRunStore
from .artifact_selection import (
    choose_artifact,
    clear_selection,
    rerun_impact,
    stage_history,
)
from .artifacts import sha256_file
from .auth import ALL_SCOPES, MCP_BOOTSTRAP_SCOPES, normalize_scopes
from .automation_routes import register_automation_routes
from .credentials import (
    AUXILIARY_CREDENTIALS,
    DEFAULT_PROVIDER_ENVS,
    auxiliary_credential_key,
    auxiliary_profiles,
    auxiliary_reference_map,
    configure_credential_reference,
    contains_inline_secret,
    credential_backend,
    credential_backend_profiles,
    credential_reference_input,
    database_reference,
    delete_managed_reference,
    llm_provider_credential_key,
    prepare_tts_settings_for_storage,
    provider_credential_key,
    provider_credential_status,
    redact_inline_secrets,
    resolve_provider_credential,
    set_auxiliary_reference,
    validate_provider_options,
    validate_vertex_service_account_json,
)
from .database import Database
from .domain_blueprints import DomainBlueprints
from .idempotency import IdempotencyConflict, IdempotencyInProgress
from .knowledge import KnowledgeLedgerStore, KnowledgeValidationError
from .managed_services import binding_for_provider, normalize_tts_provider_id
from .manager_proxy import register_manager_routes
from .models import (
    AgentRun,
    AgentStep,
    AppSetting,
    AppSettingHistory,
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Job,
    OutputAssembly,
    Provider,
    ProviderModel,
    Segment,
    SessionRecord,
    SessionSetting,
    SourceAsset,
    SourceRecord,
    TimedWord,
    TrainingRun,
    UsageEvent,
    Voice,
    VoiceSample,
    new_id,
    utcnow,
)
from .openapi import build_openapi_document
from .parity_registry import build_registry
from .route_context import RouteContext
from .schemas import (
    AgentRunCreateRequest,
    BootstrapRequest,
    BundleExportRequest,
    BundleImportRequest,
    ChunkUploadInitialize,
    CredentialUpdate,
    GenerationPlanCreate,
    GenerationSegmentBatchUpdate,
    GenerationSegmentUpdate,
    GenerationStartRequest,
    JobCreate,
    LoginRequest,
    ManagerBootstrapRequest,
    ModelCreate,
    ModelUpdate,
    OptimizationReviewRequest,
    OutcomePlanUpdate,
    OutputAssemblyCreateRequest,
    OutputMixPreviewRequest,
    PdfEditRequest,
    PronunciationCreate,
    PronunciationUpdate,
    ProviderCreate,
    ProviderTestRequest,
    ProviderUpdate,
    RvcConvertRequest,
    RvcModelUploadRequest,
    SessionCreate,
    SessionForkRequest,
    SessionSettingsUpdate,
    SessionUpdate,
    SettingUpdate,
    SourceAttachRequest,
    SourceReuseRequest,
    SourceUpdateRequest,
    SourceUrlRequest,
    StageSelectionUpdate,
    SubtitleReviewRequest,
    TokenCreateRequest,
    TrainingCreateRequest,
    TtsEndpointDiscoveryRequest,
    TtsVoicePreviewRequest,
    VoiceCreate,
    VoiceTranscriptReview,
    VoiceUpdate,
)
from .sessions import RevisionConflict
from .source_resolution import resolve_primary_source
from .voice_library import (
    ensure_bundled_voice,
    is_bundled_voice,
    mark_provider_registrations_stale,
    remove_managed_files,
    retire_sample_artifact,
    sample_file_status,
    voice_payload,
    voice_payloads,
    voice_sample_payload,
)
from .workflow_plan_routes import register_workflow_plan_routes
from .workspace import BUILTIN_DEFAULTS, SETTING_SECTIONS
from .workspace import RevisionConflict as WorkspaceRevisionConflict

XTTS_MODEL_BUNDLE_FILENAMES = (
    "config.json",
    "model.pth",
    "speakers_xtts.pth",
    "vocab.json",
)
_XTTS_MODEL_BUNDLE_FILENAME_SET = frozenset(XTTS_MODEL_BUNDLE_FILENAMES)
_XTTS_MODEL_UPLOAD_CHUNK_BYTES = 1024 * 1024
_XTTS_MODEL_UPLOAD_TIMEOUT_SECONDS = 60 * 60


def _is_loopback_address(value: object) -> bool:
    candidate = str(value or "").split("%", 1)[0]
    try:
        address = ipaddress.ip_address(candidate)
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped and mapped.is_loopback))


def _xtts_uploaded_file_size(uploaded_file: Any) -> int:
    """Return a spooled multipart file's size without materializing its body."""

    stream = uploaded_file.stream
    try:
        stream.seek(0, os.SEEK_END)
        size = stream.tell()
        stream.seek(0)
    except (AttributeError, OSError, ValueError) as error:
        raise ValueError(
            f"Could not stream '{uploaded_file.filename}' to the XTTS service."
        ) from error
    if size < 1:
        raise ValueError(f"'{uploaded_file.filename}' must not be empty.")
    return int(size)


class _SizedMultipartStream:
    """Expose a single-pass multipart iterator with a known content length."""

    def __init__(self, chunks: Iterator[bytes], content_length: int):
        self._chunks = chunks
        self._content_length = content_length

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> bytes:
        return next(self._chunks)

    def __len__(self) -> int:
        return self._content_length


def _xtts_model_bundle_upload_parts(
    model_id: str,
    uploaded_files: list[Any],
) -> tuple[str, Iterator[bytes]]:
    """Build a bounded-memory multipart stream for the first-party wrapper."""

    boundary = f"----PandratorXtts{secrets.token_hex(16)}"
    field_part = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="model_id"\r\n\r\n'
        f"{model_id}\r\n"
    ).encode()
    file_parts: list[tuple[bytes, Any, int]] = []
    total_size = len(field_part)
    content_types = {
        "config.json": "application/json",
        "model.pth": "application/octet-stream",
        "speakers_xtts.pth": "application/octet-stream",
        "vocab.json": "application/json",
    }
    for uploaded_file in uploaded_files:
        filename = str(uploaded_file.filename)
        header = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="files"; '
            f'filename="{filename}"\r\n'
            f"Content-Type: {content_types[filename]}\r\n\r\n"
        ).encode()
        size = _xtts_uploaded_file_size(uploaded_file)
        file_parts.append((header, uploaded_file, size))
        total_size += len(header) + size + len(b"\r\n")
    final_part = f"--{boundary}--\r\n".encode("ascii")
    total_size += len(final_part)

    def stream() -> Iterator[bytes]:
        yield field_part
        for header, uploaded_file, _size in file_parts:
            yield header
            file_stream = uploaded_file.stream
            file_stream.seek(0)
            while chunk := file_stream.read(_XTTS_MODEL_UPLOAD_CHUNK_BYTES):
                yield chunk
            yield b"\r\n"
        yield final_part

    return boundary, _SizedMultipartStream(stream(), total_size)


def _xtts_models_endpoint(base_url: str, model_id: str = "") -> str:
    parsed = urlsplit(str(base_url or "").strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("The configured XTTS endpoint is invalid.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("The configured XTTS endpoint is invalid.")
    base = str(base_url).strip().rstrip("/")
    if parsed.path.rstrip("/").endswith("/v1"):
        base = base[: -len("/v1")]
    endpoint = f"{base}/v1/models"
    if model_id:
        # Model identifiers are relative slash paths in the first-party XTTS
        # wrapper.  Preserve their hierarchy while escaping every path part.
        endpoint = f"{endpoint}/{quote(model_id, safe='/')}"
    return endpoint


def _xtts_health_endpoint(base_url: str) -> str:
    return _xtts_models_endpoint(base_url).removesuffix("/v1/models") + "/health"


def _xtts_model_id_error(model_id: str) -> str:
    """Apply only transport-safety checks; the wrapper owns full validation."""

    if not model_id or model_id != model_id.strip():
        return "model_id must be a non-empty relative identifier without surrounding spaces."
    if len(model_id) > 512:
        return "model_id is too long."
    if "\\" in model_id or model_id.startswith("/"):
        return "model_id must be a relative slash-separated path."
    parts = model_id.split("/")
    if any(not part or part in {".", ".."} or part.startswith(".") for part in parts):
        return "model_id contains an unsafe path part."
    if any(ord(character) < 32 for character in model_id):
        return "model_id must not contain control characters."
    return ""


def _xtts_service_endpoint_base(catalogue: dict[str, Any]) -> tuple[str, str | None]:
    xtts_service = next(
        (
            service
            for service in catalogue.get("services", [])
            if str(service.get("id") or "").strip().lower() == "xtts"
        ),
        None,
    )
    if not isinstance(xtts_service, dict):
        return "", "The XTTS service is not configured."
    endpoint_base = str(xtts_service.get("api_base") or "").strip()
    if str(xtts_service.get("connection_mode") or "") == "managed_local":
        managed = xtts_service.get("manager_service")
        endpoint_base = (
            str(managed.get("endpoint") or "").strip()
            if isinstance(managed, dict)
            else ""
        )
        if not endpoint_base:
            return "", "Pandrator Manager has not provided an XTTS endpoint for model management."
    try:
        # Validate here once, so all lifecycle proxy routes produce the same
        # connection error rather than constructing an unsafe outbound URL.
        _xtts_models_endpoint(endpoint_base)
    except ValueError as error:
        return "", str(error)
    return endpoint_base, None


def _xtts_lifecycle_item(item: Any) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    model_id = str(item.get("id") or "").strip()
    if not model_id:
        return None
    lifecycle_supported = any(
        key in item
        for key in (
            "is_default",
            "is_local",
            "removable",
            "source",
            "relative_path",
            "bundle_complete",
        )
    )
    created = item.get("created")
    if isinstance(created, bool):
        created = 0
    try:
        created_at = max(0, int(created or 0))
    except (TypeError, ValueError):
        created_at = 0
    return {
        "id": model_id,
        "object": str(item.get("object") or "model"),
        "created": created_at,
        "owned_by": str(item.get("owned_by") or "xtts-fapi"),
        "is_default": bool(item.get("is_default")) if lifecycle_supported else False,
        "is_local": bool(item.get("is_local")) if lifecycle_supported else False,
        "removable": bool(item.get("removable")) if lifecycle_supported else False,
        "source": str(item.get("source") or ("local" if lifecycle_supported else "unknown")),
        "relative_path": item.get("relative_path") if isinstance(item.get("relative_path"), str) else None,
        "bundle_complete": bool(item.get("bundle_complete")) if lifecycle_supported else True,
        "lifecycle_supported": lifecycle_supported,
    }


def _xtts_wrapper_error_details(response: requests.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _xtts_wrapper_delete_unsupported(response: requests.Response) -> bool:
    """Recognize only missing route implementations, never missing models."""

    if response.status_code == 405:
        return True
    if response.status_code != 404:
        return False
    try:
        payload = response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict) or isinstance(payload.get("error"), dict):
        return False
    # FastAPI/Starlette's unimplemented-route response. A wrapper lifecycle
    # error uses the OpenAI-style ``error`` envelope and must stay actionable.
    return str(payload.get("detail") or "").strip() == "Not Found"


def _xtts_wrapper_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return "The XTTS model service rejected the upload."
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()[:1000]
    detail = payload.get("detail") if isinstance(payload, dict) else None
    if isinstance(detail, str) and detail.strip():
        return detail.strip()[:1000]
    if isinstance(detail, list):
        messages = [
            str(item.get("msg") or "").strip()
            for item in detail
            if isinstance(item, dict) and str(item.get("msg") or "").strip()
        ]
        if messages:
            return "; ".join(messages[:5])[:1000]
    return "The XTTS model service rejected the upload."


def _model_dict(record, fields: tuple[str, ...]) -> dict[str, Any]:
    payload = {field: getattr(record, field) for field in fields}
    for key, value in list(payload.items()):
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


def _provider_payload(
    provider: Provider, database: Database, paths: DataPaths
) -> dict[str, Any]:
    payload = _model_dict(
        provider,
        (
            "id",
            "kind",
            "provider_key",
            "label",
            "enabled",
            "base_url",
            "secret_ref",
            "options_json",
            "revision",
        ),
    )
    fallback_env = str(
        (provider.options_json or {}).get("api_key_env")
        or DEFAULT_PROVIDER_ENVS.get(provider.provider_key.lower(), "")
    )
    profile_id = (
        str((provider.options_json or {}).get("profile_id") or "").strip().lower()
    )
    share_credential = not bool(
        (provider.options_json or {}).get("is_custom")
        or profile_id in {"custom-openai", "lm-studio", "ollama"}
    )
    payload.update(
        provider_credential_status(
            database,
            paths,
            provider.provider_key,
            provider.secret_ref,
            fallback_environment_variable=fallback_env,
            shared=share_credential,
        )
    )
    payload["credential_backend"] = credential_backend(provider.secret_ref)
    payload["credential_reference"] = credential_reference_input(provider.secret_ref)
    payload["options_json"] = redact_inline_secrets(payload.get("options_json") or {})
    return payload


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
        ),
    )


def _job_payload(record) -> dict[str, Any]:
    return redact_inline_secrets(
        _model_dict(
            record,
            (
                "id",
                "kind",
                "session_id",
                "workflow_run_id",
                "status",
                "payload_json",
                "result_json",
                "progress",
                "progress_detail",
                "error_code",
                "error_message",
                "attempts",
                "max_attempts",
                "created_at",
                "started_at",
                "finished_at",
                "updated_at",
            ),
        )
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
    session_forks = services.session_forks
    artifacts = services.artifacts
    workflows = services.workflows
    workflow_handlers = services.workflow_handlers
    tts_catalogue = services.tts_catalogue
    workspace_settings = services.workspace_settings
    outcome_plans = services.outcome_plans
    source_library = services.source_library
    generation = services.generation
    pronunciations = services.pronunciations
    chunk_uploads = services.chunk_uploads
    subtitle_review = services.subtitle_review
    bootstrap = services.bootstrap
    static_dir = context.static_dir
    error_response = context.guards.error_response
    inline_credential_error = context.guards.inline_credential_error
    authenticated = context.guards.authenticated
    require_auth = context.guards.require_auth

    def mutation_idempotency_key():
        """Require retry identity for MCP principals, preserve browser UX."""

        key = str(request.headers.get("Idempotency-Key") or "").strip()
        principal = context.guards.principal()
        if key:
            return key, None
        if principal is not None and principal.kind in {
            "automation_client",
            "manager_bootstrap",
        }:
            return None, error_response(
                "idempotency_key_required",
                "This automation write requires Idempotency-Key.",
                400,
            )
        return None, None

    def idempotency_failure(error):
        if isinstance(
            error,
            (IdempotencyConflict, IdempotencyInProgress),
        ):
            return error_response(
                error.code,
                str(error),
                409,
                {"retryable": error.retryable},
            )
        return error_response(
            "validation_error",
            str(error),
            422,
        )

    def abandon_idempotency(db_session, reservation) -> None:
        """Do not retain a reservation when no domain mutation occurred."""

        if not reservation.replayed:
            db_session.delete(reservation.record)
            db_session.flush()

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

    @app.get("/api/v1/services/tts")
    @require_auth
    def tts_services():
        payload, revision = tts_catalogue.snapshot(
            refresh=request.args.get("refresh", "").lower() in {"1", "true", "yes"}
        )
        response = jsonify(payload)
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.get("/api/v1/services/tts/xtts/models")
    @require_auth
    def xtts_models_list():
        catalogue, _revision = tts_catalogue.snapshot()
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)
        try:
            wrapper_response = requests.get(
                _xtts_models_endpoint(endpoint_base),
                headers={"Accept": "application/json", "Authorization": "Bearer sk-placeholder"},
                timeout=(5, 20),
            )
        except requests.Timeout:
            return error_response(
                "xtts_model_list_timeout",
                "The XTTS model service did not respond while listing models.",
                504,
            )
        except requests.ConnectionError:
            return error_response(
                "xtts_service_unavailable",
                "Could not connect to the selected XTTS service.",
                503,
            )
        except requests.RequestException:
            return error_response(
                "xtts_model_list_failed",
                "The XTTS model service could not list models.",
                502,
            )
        if wrapper_response.status_code >= 400:
            return error_response(
                "xtts_model_list_rejected",
                _xtts_wrapper_error_message(wrapper_response),
                wrapper_response.status_code if wrapper_response.status_code < 500 else 502,
                _xtts_wrapper_error_details(wrapper_response),
            )
        try:
            wrapper_payload = wrapper_response.json()
            wrapper_items = wrapper_payload.get("data") if isinstance(wrapper_payload, dict) else None
        except ValueError:
            wrapper_items = None
        if not isinstance(wrapper_items, list):
            return error_response(
                "xtts_model_list_failed",
                "The XTTS model service returned an unexpected model list.",
                502,
            )
        items = [
            normalized
            for item in wrapper_items
            if (normalized := _xtts_lifecycle_item(item)) is not None
        ]
        lifecycle_supported = bool(items) and all(
            item["lifecycle_supported"] for item in items
        )
        wrapper: dict[str, str] | None = None
        try:
            health_response = requests.get(
                _xtts_health_endpoint(endpoint_base),
                headers={"Accept": "application/json", "Authorization": "Bearer sk-placeholder"},
                timeout=(2, 5),
            )
            health_payload = health_response.json()
            if isinstance(health_payload, dict):
                version = str(health_payload.get("version") or "").strip()
                status = str(health_payload.get("status") or "").strip()
                if version or status:
                    wrapper = {key: value for key, value in {"version": version, "status": status}.items() if value}
        except (requests.RequestException, ValueError):
            # Listing remains useful when old wrappers do not expose health.
            pass
        return jsonify(
            {
                "object": "list",
                "data": items,
                "lifecycle_supported": lifecycle_supported,
                "compatibility": (
                    None
                    if lifecycle_supported
                    else "This XTTS component can list and use models, but cannot safely remove them. Update or Repair XTTS in Pandrator Manager to enable lifecycle management."
                ),
                "wrapper": wrapper,
            }
        )

    @app.post("/api/v1/services/tts/xtts/models")
    @require_auth
    def xtts_model_upload():
        if set(request.form.keys()) != {"model_id"}:
            return error_response(
                "validation_error",
                "Upload exactly one model_id field with the XTTS bundle.",
                422,
            )
        model_ids = [
            str(value or "").strip() for value in request.form.getlist("model_id")
        ]
        model_id_error = _xtts_model_id_error(model_ids[0]) if len(model_ids) == 1 else ""
        if len(model_ids) != 1 or model_id_error:
            return error_response(
                "validation_error",
                model_id_error or "Upload exactly one model_id field with the XTTS bundle.",
                422,
            )
        if set(request.files.keys()) != {"files"}:
            return error_response(
                "validation_error",
                "Upload the XTTS bundle as repeated 'files' fields.",
                422,
            )
        uploaded_files = request.files.getlist("files")
        filenames = [
            str(uploaded_file.filename or "") for uploaded_file in uploaded_files
        ]
        if (
            len(uploaded_files) != len(XTTS_MODEL_BUNDLE_FILENAMES)
            or set(filenames) != _XTTS_MODEL_BUNDLE_FILENAME_SET
        ):
            return error_response(
                "validation_error",
                "XTTS model bundles must contain exactly config.json, model.pth, speakers_xtts.pth, and vocab.json. Nested folders and incomplete training outputs are not supported.",
                422,
            )
        try:
            boundary, body = _xtts_model_bundle_upload_parts(
                model_ids[0],
                uploaded_files,
            )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

        catalogue, _revision = tts_catalogue.snapshot()
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)

        try:
            wrapper_response = requests.post(
                _xtts_models_endpoint(endpoint_base),
                data=body,
                headers={
                    "Accept": "application/json",
                    "Authorization": "Bearer sk-placeholder",
                    "Content-Type": f"multipart/form-data; boundary={boundary}",
                },
                timeout=(10, _XTTS_MODEL_UPLOAD_TIMEOUT_SECONDS),
            )
        except requests.Timeout:
            return error_response(
                "xtts_model_upload_timeout",
                "The XTTS model service did not finish the upload before the one-hour timeout.",
                504,
            )
        except requests.ConnectionError:
            return error_response(
                "xtts_service_unavailable",
                "Could not connect to the selected XTTS service.",
                503,
            )
        except requests.RequestException:
            return error_response(
                "xtts_model_upload_failed",
                "The XTTS model service could not accept the upload.",
                502,
            )

        if wrapper_response.status_code >= 400:
            if wrapper_response.status_code in {404, 405}:
                return error_response(
                    "xtts_model_upload_unsupported",
                    "This XTTS component does not support model uploads. Update the XTTS component in Pandrator Manager, then retry.",
                    wrapper_response.status_code,
                )
            if 400 <= wrapper_response.status_code < 500:
                return error_response(
                    "xtts_model_upload_rejected",
                    _xtts_wrapper_error_message(wrapper_response),
                    wrapper_response.status_code,
                )
            return error_response(
                "xtts_model_upload_failed",
                "The XTTS model service failed while installing the model.",
                502,
            )
        try:
            wrapper_payload = wrapper_response.json()
        except ValueError:
            wrapper_payload = None
        if (
            wrapper_response.status_code != 201
            or not isinstance(wrapper_payload, dict)
            or not isinstance(wrapper_payload.get("id"), str)
            or not str(wrapper_payload["id"]).strip()
            or not isinstance(wrapper_payload.get("object"), str)
            or not isinstance(wrapper_payload.get("owned_by"), str)
            or isinstance(wrapper_payload.get("bytes"), bool)
            or not isinstance(wrapper_payload.get("bytes"), int)
        ):
            return error_response(
                "xtts_model_upload_failed",
                "The XTTS model service returned an unexpected upload response.",
                502,
            )
        return (
            jsonify(
                {
                    "id": str(wrapper_payload["id"]),
                    "object": str(wrapper_payload["object"]),
                    "owned_by": str(wrapper_payload["owned_by"]),
                    "bytes": int(wrapper_payload["bytes"]),
                    **(
                        {
                            key: wrapper_payload.get(key)
                            for key in (
                                "created",
                                "is_default",
                                "is_local",
                                "removable",
                                "source",
                                "relative_path",
                                "bundle_complete",
                            )
                            if key in wrapper_payload
                        }
                    ),
                }
            ),
            201,
        )

    @app.delete("/api/v1/services/tts/xtts/models/<path:model_id>")
    @require_auth
    def xtts_model_delete(model_id: str):
        model_id_error = _xtts_model_id_error(model_id)
        if model_id_error:
            return error_response("validation_error", model_id_error, 422)
        catalogue, _revision = tts_catalogue.snapshot()
        endpoint_base, endpoint_error = _xtts_service_endpoint_base(catalogue)
        if endpoint_error:
            return error_response("xtts_service_unavailable", endpoint_error, 503)
        try:
            wrapper_response = requests.delete(
                _xtts_models_endpoint(endpoint_base, model_id),
                headers={"Accept": "application/json", "Authorization": "Bearer sk-placeholder"},
                timeout=(10, 120),
            )
        except requests.Timeout:
            return error_response(
                "xtts_model_delete_timeout",
                "The XTTS model service did not finish removing the model.",
                504,
            )
        except requests.ConnectionError:
            return error_response(
                "xtts_service_unavailable",
                "Could not connect to the selected XTTS service.",
                503,
            )
        except requests.RequestException:
            return error_response(
                "xtts_model_delete_failed",
                "The XTTS model service could not remove the model.",
                502,
            )
        if wrapper_response.status_code >= 400:
            if _xtts_wrapper_delete_unsupported(wrapper_response):
                return error_response(
                    "xtts_model_delete_unsupported",
                    "This XTTS component cannot safely remove models. Update or Repair XTTS in Pandrator Manager, then retry.",
                    wrapper_response.status_code,
                    _xtts_wrapper_error_details(wrapper_response),
                )
            return error_response(
                "xtts_model_delete_rejected",
                _xtts_wrapper_error_message(wrapper_response),
                wrapper_response.status_code if wrapper_response.status_code < 500 else 502,
                _xtts_wrapper_error_details(wrapper_response),
            )
        try:
            wrapper_payload = wrapper_response.json()
        except ValueError:
            wrapper_payload = None
        if (
            not isinstance(wrapper_payload, dict)
            or wrapper_payload.get("id") != model_id
            or wrapper_payload.get("deleted") is not True
        ):
            return error_response(
                "xtts_model_delete_failed",
                "The XTTS model service returned an unexpected removal response.",
                502,
            )
        return jsonify(
            {
                "id": model_id,
                "object": str(wrapper_payload.get("object") or "model"),
                "deleted": True,
                "evicted": bool(wrapper_payload.get("evicted")),
            }
        )

    @app.post("/api/v1/services/tts/discover")
    @require_auth
    def tts_service_discover():
        from pandrator.logic import tts_handler
        from pandrator.logic.tts_endpoint_discovery import discover_tts_endpoint

        payload = TtsEndpointDiscoveryRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        api_key = str(payload.api_key or "").strip() or tts_catalogue.discovery_api_key(
            payload.service_id
        )
        result = discover_tts_endpoint(payload.base_url, api_key=api_key)
        result["models"] = tts_handler.normalize_tts_model_catalog(
            payload.service_id,
            result.get("models") or [],
        )
        return jsonify(result), 200 if result.get("success") else 422

    @app.post("/api/v1/services/tts/<service_id>/preview")
    @require_auth
    def tts_voice_preview(service_id: str):
        payload = TtsVoicePreviewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        settings = tts_catalogue.preview_settings(
            service_id,
            model=payload.model,
            voice=payload.voice,
            language=payload.language,
        )
        if settings is None:
            return error_response("not_found", "TTS service not found.", 404)
        job = jobs.enqueue(
            "tts.preview",
            {"text": payload.text, "settings": settings},
            max_attempts=2,
            resource_keys=[f"service:tts:{service_id}"],
        )
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/sessions")
    @require_auth
    def session_list():
        return jsonify(
            {
                "items": [
                    _session_payload(item)
                    for item in sessions.list(
                        include_trashed=request.args.get("include_trashed") == "true"
                    )
                ]
            }
        )

    @app.get("/api/v1/defaults/<section>")
    @require_auth
    def global_default_get(section: str):
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
            with database.session() as db_session:
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
                    else payload.value
                )
                if setting_key != "services.tts" and contains_inline_secret(
                    prepared_value
                ):
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

    @app.post("/api/v1/sessions")
    @require_auth
    def session_create():
        payload = SessionCreate.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            request_payload = payload.model_dump(mode="json")
            created_directory: Path | None = None
            try:
                with database.immediate_session() as db_session:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=context.guards.principal(),
                            operation_id="createSession",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    existing = sessions.find_active_by_name_in_session(
                        db_session,
                        payload.name,
                    )
                    if (
                        existing is not None
                        and payload.overwrite_session_id != existing.id
                    ):
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "duplicate_session",
                            f'A session named "{existing.name}" already exists.',
                            409,
                            {"existing_session": _session_payload(existing)},
                        )
                    if payload.overwrite_session_id and (
                        existing is None or existing.id != payload.overwrite_session_id
                    ):
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "overwrite_conflict",
                            "The session selected for replacement no longer "
                            "matches this name.",
                            409,
                        )
                    if existing is not None:
                        active = db_session.scalar(
                            select(Job).where(
                                Job.session_id == existing.id,
                                Job.status.in_(
                                    (
                                        "queued",
                                        "running",
                                        "cancel_requested",
                                    )
                                ),
                            )
                        )
                        if active is not None:
                            abandon_idempotency(
                                db_session,
                                reservation,
                            )
                            return error_response(
                                "session_busy",
                                "Stop or cancel active work before replacing "
                                "this session.",
                                409,
                            )
                    if existing is not None:
                        sessions.trash(
                            existing.id,
                            existing.revision,
                            db_session=db_session,
                        )
                    record_id = new_id()
                    storage_key = new_id()
                    record = sessions.create(
                        payload.name,
                        workflow_kind=payload.workflow_kind,
                        source_language=payload.source_language,
                        target_language=payload.target_language,
                        workflow_preset=payload.workflow_preset,
                        included_stages=payload.included_stages,
                        record_id=record_id,
                        storage_key=storage_key,
                        db_session=db_session,
                    )
                    created_directory = paths.sessions / record.storage_key
                    created_directory.mkdir(
                        parents=True,
                        exist_ok=False,
                    )
                    result = _session_payload(record)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="session",
                        resource_id=record.id,
                    )
                    g.audit_resource_kind = "session"
                    g.audit_resource_id = record.id
                response = jsonify(result)
                response.status_code = 201
                response.headers["ETag"] = f'"{result["revision"]}"'
                return response
            except Exception:
                if created_directory is not None and created_directory.is_dir():
                    try:
                        created_directory.rmdir()
                    except OSError:
                        pass
                raise
        existing = sessions.find_active_by_name(payload.name)
        if existing is not None and payload.overwrite_session_id != existing.id:
            return error_response(
                "duplicate_session",
                f'A session named "{existing.name}" already exists.',
                409,
                {"existing_session": _session_payload(existing)},
            )
        if payload.overwrite_session_id and (
            existing is None or existing.id != payload.overwrite_session_id
        ):
            return error_response(
                "overwrite_conflict",
                "The session selected for replacement no longer matches this name. Review the current session list and try again.",
                409,
            )
        if existing is not None:
            with database.session() as db_session:
                active = db_session.scalar(
                    select(Job).where(
                        Job.session_id == existing.id,
                        Job.status.in_(("queued", "running", "cancel_requested")),
                    )
                )
                if active is not None:
                    return error_response(
                        "session_busy",
                        "Stop or cancel active work before replacing this session.",
                        409,
                    )
            sessions.trash(existing.id, existing.revision)
        record = sessions.create(
            payload.name,
            workflow_kind=payload.workflow_kind,
            source_language=payload.source_language,
            target_language=payload.target_language,
            workflow_preset=payload.workflow_preset,
            included_stages=payload.included_stages,
        )
        (paths.sessions / record.storage_key).mkdir(parents=True, exist_ok=False)
        response = jsonify(_session_payload(record))
        response.status_code = 201
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.get("/api/v1/sessions/<session_id>")
    @require_auth
    def session_get(session_id: str):
        try:
            record = sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/forks")
    @require_auth
    def session_fork(session_id: str):
        payload = SessionForkRequest.model_validate(request.get_json(silent=True) or {})
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        created_directory: Path | None = None
        try:
            with database.immediate_session() as db_session:
                reservation = None
                request_payload = {
                    "session_id": session_id,
                    **payload.model_dump(mode="json"),
                }
                if idempotency_key is not None:
                    try:
                        reservation = services.idempotency.begin(
                            db_session,
                            principal=context.guards.principal(),
                            operation_id="forkSession",
                            idempotency_key=idempotency_key,
                            payload=request_payload,
                        )
                    except (
                        IdempotencyConflict,
                        IdempotencyInProgress,
                        ValueError,
                    ) as error:
                        return idempotency_failure(error)
                    if reservation.response is not None:
                        replayed, status_code = reservation.response
                        response = jsonify(replayed)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        return response

                forked = session_forks.fork_in_session(
                    db_session,
                    session_id,
                    payload.checkpoint_artifact_id,
                    name=payload.name or "",
                )
                created_directory = forked.directory
                result = {
                    **_session_payload(forked.record),
                    "forked_from_session_id": session_id,
                    "checkpoint_artifact_id": forked.checkpoint_artifact_id,
                    "copied_stages": list(forked.copied_stages),
                }
                if reservation is not None:
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=201,
                        resource_kind="session",
                        resource_id=forked.record.id,
                    )
                g.audit_resource_kind = "session"
                g.audit_resource_id = forked.record.id
            response = jsonify(result)
            response.status_code = 201
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        except KeyError:
            return error_response(
                "not_found",
                "The session or selected checkpoint was not found.",
                404,
            )
        except FileNotFoundError as error:
            return error_response("checkpoint_missing", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        except OSError as error:
            return error_response(
                "fork_failed",
                f"The session fork could not be created: {error}",
                409,
            )
        except Exception:
            if created_directory is not None:
                shutil.rmtree(created_directory, ignore_errors=True)
            raise

    @app.patch("/api/v1/sessions/<session_id>")
    @require_auth
    def session_update(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current revision.",
                428,
            )
        payload = SessionUpdate.model_validate(request.get_json(silent=True) or {})
        raw_changes = payload.model_dump(exclude_unset=True)
        changes = {
            key: value
            for key, value in raw_changes.items()
            if value is not None or key == "target_language"
        }
        if "included_stages" in changes:
            changes["included_stages_json"] = changes.pop("included_stages")
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="updateSession",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "expected_revision": revision,
                            "changes": changes,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
                        return response
                    current = db_session.get(
                        SessionRecord,
                        session_id,
                    )
                    if current is None:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "not_found",
                            "Session not found.",
                            404,
                        )
                    if current.revision != revision:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "revision_conflict",
                            f"Expected revision {revision}, found {current.revision}.",
                            409,
                            {"current_revision": (current.revision)},
                        )
                    record = sessions.update(
                        session_id,
                        revision,
                        changes,
                        db_session=db_session,
                    )
                    result = _session_payload(record)
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="session",
                        resource_id=session_id,
                    )
                    g.audit_resource_kind = "session"
                    g.audit_resource_id = session_id
            except (
                IdempotencyConflict,
                IdempotencyInProgress,
                RevisionConflict,
                ValueError,
            ) as error:
                if isinstance(error, RevisionConflict):
                    return error_response(
                        "revision_conflict",
                        str(error),
                        409,
                    )
                return idempotency_failure(error)
            response = jsonify(result)
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            record = sessions.update(session_id, revision, changes)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.delete("/api/v1/sessions/<session_id>")
    @require_auth
    def session_trash(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current session revision.",
                428,
            )
        with database.session() as db_session:
            active = db_session.scalar(
                select(Job).where(
                    Job.session_id == session_id,
                    Job.status.in_(("queued", "running", "cancel_requested")),
                )
            )
            if active is not None:
                return error_response(
                    "session_busy",
                    "Stop or cancel active work before moving this session to trash.",
                    409,
                )
        try:
            record = sessions.trash(session_id, revision)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/restore")
    @require_auth
    def session_restore(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current session revision.",
                428,
            )
        try:
            record = sessions.restore(session_id, revision)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except RevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        response = jsonify(_session_payload(record))
        response.headers["ETag"] = f'"{record.revision}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/reindex")
    @require_auth
    def session_reindex(session_id: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify(
            {"session_id": session_id, "reports": artifacts.reconcile(session_id)}
        )

    @app.get("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    def session_settings_get(session_id: str, section: str):
        try:
            result = workspace_settings.get(session_id, section)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(redact_inline_secrets(result))
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.put("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    def session_settings_put(session_id: str, section: str):
        payload = SessionSettingsUpdate.model_validate(
            request.get_json(silent=True) or {}
        )
        if rejected := inline_credential_error(payload.value):
            return rejected
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current settings revision.",
                428,
            )
        idempotency_key, idempotency_error = mutation_idempotency_key()
        if idempotency_error is not None:
            return idempotency_error
        if idempotency_key is not None:
            try:
                with database.immediate_session() as db_session:
                    reservation = services.idempotency.begin(
                        db_session,
                        principal=context.guards.principal(),
                        operation_id="putSessionSettings",
                        idempotency_key=idempotency_key,
                        payload={
                            "session_id": session_id,
                            "section": section,
                            "expected_revision": expected,
                            "value": payload.value,
                        },
                    )
                    if reservation.response is not None:
                        result, status_code = reservation.response
                        response = jsonify(result)
                        response.status_code = status_code
                        response.headers["Idempotency-Replayed"] = "true"
                        if result.get("revision") is not None:
                            response.headers["ETag"] = f'"{result["revision"]}"'
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
                    current = db_session.get(
                        SessionSetting,
                        (session_id, section),
                    )
                    current_revision = current.revision if current is not None else 0
                    if current_revision != expected:
                        abandon_idempotency(
                            db_session,
                            reservation,
                        )
                        return error_response(
                            "revision_conflict",
                            "Session settings changed in another client.",
                            409,
                            {"current_revision": (current_revision)},
                        )
                    result = workspace_settings.update(
                        session_id,
                        section,
                        expected,
                        payload.value,
                        db_session=db_session,
                    )
                    services.idempotency.complete(
                        db_session,
                        reservation,
                        response=result,
                        status_code=200,
                        resource_kind="session_settings",
                        resource_id=f"{session_id}:{section}",
                    )
                    g.audit_resource_kind = "session_settings"
                    g.audit_resource_id = f"{session_id}:{section}"
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
            response.headers["ETag"] = f'"{result["revision"]}"'
            return response
        try:
            result = workspace_settings.update(
                session_id, section, expected, payload.value
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sessions/<session_id>/settings/resolve")
    @require_auth
    def session_settings_resolve(session_id: str):
        body = request.get_json(silent=True) or {}
        sections = (
            body.get("sections") if isinstance(body.get("sections"), list) else None
        )
        overrides = (
            body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
        )
        if rejected := inline_credential_error(overrides):
            return rejected
        try:
            value, digest = workspace_settings.resolve(
                session_id, sections=sections, run_override=overrides
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify({"value": value, "settings_hash": digest})

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
        try:
            result = source_library.attach(
                session_id, payload.source_asset_id, role=payload.role
            )
        except KeyError:
            return error_response(
                "not_found", "Session or source asset not found.", 404
            )
        return jsonify(result), 201

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
            items = []
            for document in documents:
                revisions = list(
                    db_session.scalars(
                        select(DocumentRevision)
                        .where(DocumentRevision.document_id == document.id)
                        .order_by(DocumentRevision.revision_number.desc())
                    ).all()
                )
                revision_items = []
                for revision in revisions:
                    segment_count, duration_ms = db_session.execute(
                        select(func.count(Segment.id), func.max(Segment.end_ms)).where(
                            Segment.revision_id == revision.id
                        )
                    ).one()
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

    @app.get("/api/v1/sessions/<session_id>/generation-segments")
    @require_auth
    def generation_segment_list(session_id: str):
        marked_arg = request.args.get("marked")
        marked = None if marked_arg is None else marked_arg.lower() == "true"
        try:
            result = generation.list_segments(
                session_id,
                cursor=request.args.get("cursor", 0, type=int),
                limit=request.args.get("limit", 100, type=int),
                status=request.args.get("status"),
                marked=marked,
                verification=request.args.get("verification"),
                generation_run_id=request.args.get("generation_run_id"),
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
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current segment revision.",
                428,
            )
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
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current segment revision.",
                428,
            )
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
        return jsonify({"items": generation.list_runs(session_id)})

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
        try:
            result = generation.start(
                session_id,
                run_override=payload.run_override,
                selected_segment_override=payload.selected_segment_override,
                segment_ids=payload.segment_ids,
                generation_run_id=payload.generation_run_id,
                operation=payload.operation,
            )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("generation_unavailable", str(error), 409)
        return jsonify(result), 202

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
            source_resolution = resolve_primary_source(db_session, session_id)
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
        return jsonify(
            {
                "items": [
                    _job_payload(item)
                    for item in work.diagnostic_list(
                        request.args.get("limit", 100, type=int)
                    )
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
        return jsonify(
            {
                "schema_version": "1",
                "items": [item.model_dump(mode="json") for item in items],
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

    @app.get("/api/v1/sessions/<session_id>/workflow")
    @require_auth
    def workflow_get(session_id: str):
        try:
            return jsonify(workflows.snapshot(session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.get("/api/v1/sessions/<session_id>/stages/<stage_key>/artifacts")
    @require_auth
    def workflow_stage_artifacts(session_id: str, stage_key: str):
        try:
            sessions.get(session_id)
            with database.session() as db_session:
                return jsonify(
                    stage_history(
                        db_session,
                        session_id,
                        stage_key,
                        limit=request.args.get("limit", 50, type=int),
                        before_version=request.args.get(
                            "before_version",
                            type=int,
                        ),
                    )
                )
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("stage_unavailable", str(error), 409)

    @app.get("/api/v1/sessions/<session_id>/stages/<stage_key>/impact")
    @require_auth
    def workflow_stage_impact(session_id: str, stage_key: str):
        try:
            sessions.get(session_id)
            with database.session() as db_session:
                return jsonify(rerun_impact(db_session, session_id, stage_key))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("stage_unavailable", str(error), 409)

    @app.get("/api/v1/sessions/<session_id>/stages/<stage_key>/settings-mismatches")
    @require_auth
    def workflow_stage_settings_mismatches(session_id: str, stage_key: str):
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify(
            {"mismatches": workflow_handlers.settings_mismatches(session_id, stage_key)}
        )

    @app.put("/api/v1/sessions/<session_id>/stages/<stage_key>/selection")
    @require_auth
    def workflow_stage_selection(session_id: str, stage_key: str):
        payload = StageSelectionUpdate.model_validate(
            request.get_json(silent=True) or {}
        )
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current selection revision.",
                428,
            )
        try:
            with database.session() as db_session:
                history = stage_history(db_session, session_id, stage_key)
                if int(history["revision"]) != expected:
                    return error_response(
                        "revision_conflict",
                        "The selected stage artifact changed in another client.",
                        409,
                    )
                if payload.artifact_id:
                    result = choose_artifact(
                        db_session, session_id, stage_key, payload.artifact_id
                    )
                else:
                    result = clear_selection(db_session, session_id, stage_key)
            return jsonify(result)
        except KeyError:
            return error_response("not_found", "Session or artifact not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)

    @app.post("/api/v1/sessions/<session_id>/stages/<stage_key>/run")
    @require_auth
    def workflow_run_stage(session_id: str, stage_key: str):
        settings = request.get_json(silent=True) or {}
        if not isinstance(settings, dict):
            return error_response(
                "validation_error", "Stage settings must be an object.", 422
            )
        if rejected := inline_credential_error(settings):
            return rejected
        try:
            job = workflows.run_stage(session_id, stage_key, settings)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("stage_unavailable", str(error), 409)
        return jsonify(_job_payload(job)), 202

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
        try:
            result = subtitle_review.save_review(
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.segments],
                source_artifact_id=payload.source_artifact_id,
            )
        except KeyError:
            return error_response("not_found", "Subtitle document not found.", 404)
        except RuntimeError as error:
            return error_response("revision_conflict", str(error), 409)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

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
        return jsonify(
            {
                "cursor": bounds.latest,
                "retained_after": bounds.retained_after,
                "sessions": {
                    "items": [_session_payload(item) for item in sessions.list()]
                },
                "jobs": {
                    "items": [_job_payload(item) for item in work.diagnostic_list(40)]
                },
                "capabilities": capability_payload,
            }
        )

    @app.get("/api/v1/events")
    @require_auth
    def events():
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
                    last_visible_id = cursor
                    for event in new_events:
                        cursor = event.id
                        if event.event_type == "job.log":
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
                    from PIL import Image

                    with Image.open(temporary) as image:
                        if image.format not in {"JPEG", "PNG", "WEBP"}:
                            raise ValueError("Use JPEG, PNG, or WebP artwork.")
                        width, height = image.size
                        if width < 1 or height < 1 or width * height > 100_000_000:
                            raise ValueError(
                                "Artwork dimensions are invalid or exceed 100 megapixels."
                            )
                        image.verify()
                except Exception as error:
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

    @app.get("/api/v1/artifacts")
    @require_auth
    def artifact_list():
        with database.session() as db_session:
            statement = select(Artifact).order_by(Artifact.created_at.desc())
            if request.args.get("include_deleted") != "true":
                statement = statement.where(Artifact.state != "deleted")
            requested_session = str(request.args.get("session_id") or "")
            if requested_session:
                statement = statement.where(Artifact.session_id == requested_session)
            try:
                limit = max(1, min(500, int(request.args.get("limit") or 500)))
            except ValueError:
                limit = 500
            records = list(db_session.scalars(statement.limit(limit)).all())
            items = []
            for item in records:
                serialized = _model_dict(
                    item,
                    (
                        "id",
                        "session_id",
                        "kind",
                        "role",
                        "relative_path",
                        "mime_type",
                        "size_bytes",
                        "content_hash",
                        "state",
                        "metadata_json",
                        "created_at",
                    ),
                )
                # The owner UI may copy the server-local path for completed
                # outputs. The relative managed key remains the durable API
                # identifier; this presentation field is intentionally
                # read-only and never accepted back as an input path.
                serialized["path"] = str(
                    paths.managed_path(item.relative_path).resolve()
                )
                items.append(serialized)
            return jsonify({"items": items})

    @app.delete("/api/v1/sessions/<session_id>/outputs/<artifact_id>")
    @require_auth
    def output_artifact_delete(session_id: str, artifact_id: str):
        try:
            result = artifacts.remove_output(session_id, artifact_id)
        except KeyError:
            return error_response("not_found", "Export not found.", 404)
        except ValueError as error:
            return error_response("invalid_artifact", str(error), 409)
        except OSError as error:
            return error_response(
                "artifact_delete_failed",
                f"The export file could not be removed: {error}",
                409,
            )
        return jsonify(result)

    @app.post("/api/v1/artifacts/<artifact_id>/optimization-review")
    @require_auth
    def artifact_optimization_review(artifact_id: str):
        payload = OptimizationReviewRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        try:
            source, source_path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response(
                "not_found", "Speech-optimized artifact not found.", 404
            )
        if source.role != "tts_optimized" or source_path.suffix.lower() != ".json":
            return error_response(
                "validation_error",
                "Only JSON speech-optimization artifacts use this review endpoint.",
                422,
            )
        try:
            rows = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            return error_response(
                "artifact_invalid",
                f"The optimization artifact cannot be reviewed: {error}",
                422,
            )
        if not isinstance(rows, list):
            return error_response(
                "artifact_invalid",
                "The optimization artifact must contain a list.",
                422,
            )
        edits = {item.index: item.text.strip() for item in payload.items}
        if set(edits) != set(range(len(rows))):
            return error_response(
                "validation_error",
                "Reviewed text must preserve every item index exactly once.",
                422,
            )
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return error_response(
                    "artifact_invalid",
                    "Every optimization item must be an object.",
                    422,
                )
            row["source_text"] = str(
                row.get("source_text")
                or row.get("original_sentence")
                or row.get("text")
                or ""
            )
            row["text"] = edits[index]
            row["processed_sentence"] = edits[index]
            row["tts_optimized_sentence"] = edits[index]
            row["optimization_reviewed"] = True
        destination = source_path.parent / f"tts-optimized-reviewed-{new_id()}.json"
        destination.write_text(
            json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        reviewed = artifacts.register(
            destination,
            kind="json",
            role="tts_optimized",
            session_id=source.session_id,
            parent_ids=[source.id],
            metadata={
                **(source.metadata_json or {}),
                "reviewed": True,
                "reviewed_from": source.id,
            },
        )
        return jsonify(
            _model_dict(
                reviewed,
                (
                    "id",
                    "session_id",
                    "kind",
                    "role",
                    "relative_path",
                    "mime_type",
                    "size_bytes",
                    "content_hash",
                    "state",
                    "metadata_json",
                    "created_at",
                ),
            )
        ), 201

    @app.get("/api/v1/artifacts/<artifact_id>/context")
    @require_auth
    def artifact_context(artifact_id: str):
        """Return lightweight lineage metadata used by review and comparison UIs."""
        fields = (
            "id",
            "session_id",
            "kind",
            "role",
            "relative_path",
            "mime_type",
            "size_bytes",
            "content_hash",
            "state",
            "metadata_json",
            "created_at",
        )
        with database.session() as db_session:
            artifact = db_session.get(Artifact, artifact_id)
            if artifact is None:
                return error_response("not_found", "Artifact not found.", 404)
            parent_ids = list(
                db_session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == artifact_id
                    )
                ).all()
            )
            parents = (
                list(
                    db_session.scalars(
                        select(Artifact).where(Artifact.id.in_(parent_ids))
                    ).all()
                )
                if parent_ids
                else []
            )
            parents.sort(
                key=lambda item: (item.role != "extracted_text", item.created_at)
            )
            child_ids = list(
                db_session.scalars(
                    select(ArtifactEdge.child_artifact_id).where(
                        ArtifactEdge.parent_artifact_id == artifact_id
                    )
                ).all()
            )
            children = (
                list(
                    db_session.scalars(
                        select(Artifact).where(Artifact.id.in_(child_ids))
                    ).all()
                )
                if child_ids
                else []
            )
            children.sort(key=lambda item: (item.created_at, item.id))
            usage_artifact_ids = {artifact.id, *parent_ids}
            events = list(
                db_session.scalars(
                    select(UsageEvent).where(
                        UsageEvent.artifact_id.in_(usage_artifact_ids)
                    )
                ).all()
            )
            generation_run_id = str(
                (artifact.metadata_json or {}).get("generation_run_id") or ""
            )
            output_assembly_id = str(
                (artifact.metadata_json or {}).get("output_assembly_id") or ""
            )
            if output_assembly_id:
                assembly = db_session.get(OutputAssembly, output_assembly_id)
                generation_run_id = (
                    str(assembly.generation_run_id or "")
                    if assembly is not None
                    else generation_run_id
                )
            if generation_run_id:
                events.extend(
                    db_session.scalars(
                        select(UsageEvent).where(
                            UsageEvent.generation_run_id == generation_run_id
                        )
                    ).all()
                )
            from .usage import usage_summary

            return jsonify(
                {
                    "artifact": _model_dict(artifact, fields),
                    "parents": [_model_dict(item, fields) for item in parents],
                    "children": [_model_dict(item, fields) for item in children],
                    "usage": usage_summary(events),
                }
            )

    @app.get("/api/v1/artifacts/<artifact_id>/content")
    @require_auth
    def artifact_content(artifact_id: str):
        try:
            artifact, path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Artifact not found.", 404)
        if not path.is_file():
            return error_response(
                "artifact_missing", "The artifact file is missing.", 410
            )
        return send_file(
            path,
            mimetype=artifact.mime_type,
            conditional=True,
            etag=artifact.content_hash,
        )

    @app.get("/api/v1/artifacts/<artifact_id>/waveform")
    @require_auth
    def artifact_waveform(artifact_id: str):
        try:
            source, _path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Audio artifact not found.", 404)
        with database.session() as db_session:
            peak_artifact = db_session.scalar(
                select(Artifact)
                .join(ArtifactEdge, ArtifactEdge.child_artifact_id == Artifact.id)
                .where(
                    ArtifactEdge.parent_artifact_id == artifact_id,
                    Artifact.role == "waveform_peaks",
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc())
            )
            if peak_artifact is not None:
                peak_id = peak_artifact.id
            else:
                peak_id = None
        if peak_id:
            _artifact, peak_path = artifacts.resolve(peak_id)
            return send_file(
                peak_path,
                mimetype="application/json",
                conditional=True,
                etag=_artifact.content_hash,
            )
        job = jobs.enqueue(
            "audio.waveform",
            {
                "source_artifact_id": artifact_id,
                "max_points": request.args.get("points", 1600, type=int),
            },
            session_id=source.session_id,
            resource_keys=[f"session:{source.session_id}"] if source.session_id else [],
        )
        return jsonify({"status": "queued", "job_id": job.id}), 202

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

    @app.get("/api/v1/providers")
    @require_auth
    def provider_list():
        with database.session() as db_session:
            providers = list(
                db_session.scalars(select(Provider).order_by(Provider.label)).all()
            )
            return jsonify(
                {
                    "items": [
                        _provider_payload(item, database, paths) for item in providers
                    ]
                }
            )

    @app.get("/api/v1/credential-backends")
    @require_auth
    def credential_backends():
        return jsonify({"items": credential_backend_profiles()})

    @app.get("/api/v1/providers/profiles")
    @require_auth
    def provider_profiles():
        from .provider_settings import list_llm_provider_profiles

        return jsonify({"items": list_llm_provider_profiles()})

    @app.post("/api/v1/providers")
    @require_auth
    def provider_create():
        payload = ProviderCreate.model_validate(request.get_json(silent=True) or {})
        try:
            validate_provider_options(payload.options)
            if (
                payload.provider_key.strip().lower() == "vertex_ai"
                and str(payload.api_key or "").strip()
            ):
                validate_vertex_service_account_json(payload.api_key)
            if payload.credential_backend is not None and payload.secret_ref:
                raise ValueError(
                    "Use the structured credential storage fields or a legacy secret_ref, not both."
                )
            if (
                payload.secret_ref
                and credential_backend(payload.secret_ref) == "unavailable"
            ):
                raise ValueError(
                    "The legacy secret_ref uses an unsupported credential scheme."
                )
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        try:
            with database.session() as db_session:
                provider = Provider(
                    kind=payload.kind,
                    provider_key=payload.provider_key,
                    label=payload.label,
                    enabled=payload.enabled,
                    base_url=payload.base_url,
                    secret_ref=payload.secret_ref,
                    options_json=payload.options,
                )
                db_session.add(provider)
                db_session.flush()
                if (
                    payload.credential_backend is not None
                    or str(payload.api_key or "").strip()
                ):
                    key = llm_provider_credential_key(
                        provider.provider_key, provider.id, provider.options_json
                    )
                    credential_kind = (
                        "credentials"
                        if provider.provider_key.strip().lower() == "vertex_ai"
                        else "API key"
                    )
                    configured = configure_credential_reference(
                        db_session,
                        database,
                        paths,
                        key=key,
                        label=f"{provider.label} {credential_kind}",
                        current_reference="",
                        backend=payload.credential_backend or "database",
                        locator=payload.credential_reference or "",
                        secret_value=payload.api_key or "",
                    )
                    provider.secret_ref = configured.reference
                db_session.flush()
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            return error_response("credential_unavailable", str(error), 422)
        result = _provider_payload(provider, database, paths)
        return jsonify(result), 201

    @app.patch("/api/v1/providers/<provider_id>")
    @require_auth
    def provider_update(provider_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current provider revision.",
                428,
            )
        payload = ProviderUpdate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            if provider.revision != expected_revision:
                return error_response(
                    "revision_conflict", "The provider changed in another client.", 409
                )
            previous_secret_ref = str(provider.secret_ref or "")
            changes = payload.model_dump(exclude_unset=True)
            submitted_key = str(changes.pop("api_key", "") or "").strip()
            clear_key = bool(changes.pop("clear_api_key", False))
            requested_backend = changes.pop("credential_backend", None)
            requested_locator = changes.pop("credential_reference", "")
            delete_previous = bool(changes.pop("delete_previous_credential", False))
            if submitted_key and clear_key:
                return error_response(
                    "validation_error",
                    "Choose either a replacement API key or remove the current key.",
                    422,
                )
            if requested_backend is not None and "secret_ref" in changes:
                return error_response(
                    "validation_error",
                    "Use the structured credential storage fields or a legacy secret_ref, not both.",
                    422,
                )
            effective_provider_key = (
                str(changes.get("provider_key") or provider.provider_key or "")
                .strip()
                .lower()
            )
            if submitted_key and effective_provider_key == "vertex_ai":
                try:
                    validate_vertex_service_account_json(submitted_key)
                except ValueError as error:
                    return error_response("validation_error", str(error), 422)
            if "options" in changes:
                try:
                    validate_provider_options(changes["options"])
                except ValueError as error:
                    return error_response("validation_error", str(error), 422)
                changes["options_json"] = changes.pop("options")
            if (
                "secret_ref" in changes
                and changes["secret_ref"]
                and credential_backend(changes["secret_ref"]) == "unavailable"
            ):
                return error_response(
                    "validation_error",
                    "The legacy secret_ref uses an unsupported credential scheme.",
                    422,
                )
            previous_retained = False
            configured_reference = None
            configured_reference_changed = False
            if requested_backend is not None or submitted_key:
                effective_options = changes.get("options_json", provider.options_json)
                key = llm_provider_credential_key(
                    effective_provider_key,
                    provider.id,
                    effective_options,
                )
                credential_kind = (
                    "credentials"
                    if effective_provider_key == "vertex_ai"
                    else "API key"
                )
                try:
                    configured = configure_credential_reference(
                        db_session,
                        database,
                        paths,
                        key=key,
                        label=f"{changes.get('label') or provider.label} {credential_kind}",
                        current_reference=previous_secret_ref,
                        backend=requested_backend or "database",
                        locator=requested_locator or "",
                        secret_value=submitted_key,
                        delete_previous=delete_previous,
                    )
                except (
                    OSError,
                    RuntimeError,
                    ValueError,
                    json.JSONDecodeError,
                ) as error:
                    db_session.rollback()
                    return error_response("credential_unavailable", str(error), 422)
                configured_reference = configured.reference
                configured_reference_changed = True
                previous_retained = configured.previous_credential_retained
            elif clear_key:
                current_reference = previous_secret_ref
                if current_reference:
                    try:
                        delete_managed_reference(
                            db_session,
                            current_reference,
                            preserve_shared=False,
                        )
                    except RuntimeError as error:
                        db_session.rollback()
                        return error_response("credential_unavailable", str(error), 422)
                configured_reference_changed = True
            for key, value in changes.items():
                setattr(provider, key, value)
            if configured_reference_changed:
                provider.secret_ref = configured_reference
            provider.revision += 1
            provider.updated_at = utcnow()
            db_session.flush()
        result = _provider_payload(provider, database, paths)
        result["previous_credential_retained"] = previous_retained
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/providers/<provider_id>")
    @require_auth
    def provider_delete(provider_id: str):
        replacement_id = str(
            (request.get_json(silent=True) or {}).get("replacement_model_record_id")
            or ""
        )
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            active = db_session.scalar(
                select(ProviderModel).where(
                    ProviderModel.provider_id == provider_id,
                    ProviderModel.is_default.is_(True),
                )
            )
            if active is not None:
                replacement = (
                    db_session.get(ProviderModel, replacement_id)
                    if replacement_id
                    else None
                )
                if replacement is None or replacement.provider_id == provider_id:
                    return error_response(
                        "replacement_required",
                        "Select a default model from another provider before removing this provider.",
                        409,
                    )
                replacement.is_active = True
                replacement.is_default = True
            try:
                delete_managed_reference(
                    db_session,
                    str(
                        provider.secret_ref
                        or database_reference(provider_credential_key(provider.id))
                    ),
                )
            except RuntimeError as error:
                return error_response("credential_unavailable", str(error), 422)
            db_session.delete(provider)
        return "", 204

    @app.post("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_create(provider_id: str):
        payload = ModelCreate.model_validate(request.get_json(silent=True) or {})
        try:
            validate_provider_options(payload.options)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            if payload.is_default:
                for existing in db_session.scalars(select(ProviderModel)):
                    existing.is_default = False
            model = ProviderModel(
                provider_id=provider_id,
                model_id=payload.model_id,
                is_active=payload.is_active or payload.is_default,
                is_default=payload.is_default,
                default_temperature=payload.default_temperature,
                default_reasoning_effort=payload.default_reasoning_effort,
                input_cost_per_million=payload.input_cost_per_million,
                cached_input_cost_per_million=payload.cached_input_cost_per_million,
                output_cost_per_million=payload.output_cost_per_million,
                context_window_tokens=payload.context_window_tokens,
                max_output_tokens=payload.max_output_tokens,
                options_json=payload.options,
            )
            db_session.add(model)
            db_session.flush()
            result = _model_dict(
                model,
                (
                    "id",
                    "provider_id",
                    "model_id",
                    "is_active",
                    "is_default",
                    "default_temperature",
                    "default_reasoning_effort",
                    "input_cost_per_million",
                    "cached_input_cost_per_million",
                    "output_cost_per_million",
                    "context_window_tokens",
                    "max_output_tokens",
                    "options_json",
                    "revision",
                ),
            )
        return jsonify(result), 201

    @app.post("/api/v1/providers/<provider_id>/test")
    @require_auth
    def provider_test(provider_id: str):
        from pandrator.logic.llm_handler import chat_completion_with_metadata

        from .provider_settings import build_llm_settings

        payload = ProviderTestRequest.model_validate(
            request.get_json(silent=True) or {}
        )
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            selected = payload.model_id or db_session.scalar(
                select(ProviderModel.model_id).where(
                    ProviderModel.provider_id == provider_id,
                    ProviderModel.is_active.is_(True),
                    ProviderModel.is_default.is_(True),
                )
            )
            if not selected:
                selected = db_session.scalar(
                    select(ProviderModel.model_id)
                    .where(
                        ProviderModel.provider_id == provider_id,
                        ProviderModel.is_active.is_(True),
                    )
                    .order_by(ProviderModel.model_id)
                )
            if not selected:
                return error_response(
                    "validation_error",
                    "Activate at least one model before testing this provider.",
                    422,
                )
        settings, model_name = build_llm_settings(
            database, paths, requested_model=selected
        )
        result = chat_completion_with_metadata(
            messages=[{"role": "user", "content": "Reply with exactly OK."}],
            model_name=model_name,
            llm_settings=settings,
        )
        if not result.content:
            return error_response(
                "provider_test_failed",
                "The provider returned no usable response. Check its URL, secret reference, and model ID.",
                422,
            )
        return jsonify(
            {
                "ok": True,
                "model": result.model or model_name,
                "response": result.content[:80],
                "cost": result.cost,
                "cost_source": result.cost_source,
            }
        )

    @app.get("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_list(provider_id: str):
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            records = list(
                db_session.scalars(
                    select(ProviderModel)
                    .where(ProviderModel.provider_id == provider_id)
                    .order_by(ProviderModel.model_id)
                ).all()
            )
            return jsonify(
                {
                    "items": [
                        _model_dict(
                            item,
                            (
                                "id",
                                "provider_id",
                                "model_id",
                                "is_active",
                                "is_default",
                                "default_temperature",
                                "default_reasoning_effort",
                                "input_cost_per_million",
                                "cached_input_cost_per_million",
                                "output_cost_per_million",
                                "context_window_tokens",
                                "max_output_tokens",
                                "options_json",
                                "revision",
                            ),
                        )
                        for item in records
                    ]
                }
            )

    @app.patch("/api/v1/providers/<provider_id>/models/<model_record_id>")
    @require_auth
    def model_update(provider_id: str, model_record_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response(
                "precondition_required",
                "If-Match must contain the current model revision.",
                428,
            )
        payload = ModelUpdate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            model = db_session.get(ProviderModel, model_record_id)
            if model is None or model.provider_id != provider_id:
                return error_response("not_found", "Model not found.", 404)
            if model.revision != expected_revision:
                return error_response(
                    "revision_conflict",
                    "The model settings changed in another client.",
                    409,
                )
            changes = payload.model_dump(exclude_unset=True)
            if changes.get("is_active") is False and model.is_default:
                return error_response(
                    "validation_error",
                    "Choose another application default before deactivating this model.",
                    422,
                )
            if "options" in changes:
                try:
                    validate_provider_options(changes["options"])
                except ValueError as error:
                    return error_response("validation_error", str(error), 422)
            if changes.pop("is_default", False):
                for existing in db_session.scalars(select(ProviderModel)):
                    existing.is_default = existing.id == model.id
                changes["is_active"] = True
            if "options" in changes:
                changes["options_json"] = changes.pop("options")
            for key, value in changes.items():
                setattr(model, key, value)
            model.revision += 1
            db_session.flush()
            result = _model_dict(
                model,
                (
                    "id",
                    "provider_id",
                    "model_id",
                    "is_active",
                    "is_default",
                    "default_temperature",
                    "default_reasoning_effort",
                    "input_cost_per_million",
                    "cached_input_cost_per_million",
                    "output_cost_per_million",
                    "context_window_tokens",
                    "max_output_tokens",
                    "options_json",
                    "revision",
                ),
            )
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/providers/<provider_id>/models/<model_record_id>")
    @require_auth
    def model_delete(provider_id: str, model_record_id: str):
        body = request.get_json(silent=True) or {}
        replacement_record_id = str(body.get("replacement_model_record_id") or "")
        replacement_model_id = str(body.get("replacement_model_id") or "")
        with database.session() as db_session:
            model = db_session.get(ProviderModel, model_record_id)
            if model is None or model.provider_id != provider_id:
                return error_response("not_found", "Model not found.", 404)
            if model.is_default:
                replacement = (
                    db_session.get(ProviderModel, replacement_record_id)
                    if replacement_record_id
                    else None
                )
                if replacement is None and replacement_model_id:
                    replacement = db_session.scalar(
                        select(ProviderModel).where(
                            ProviderModel.provider_id == provider_id,
                            ProviderModel.model_id == replacement_model_id,
                        )
                    )
                if replacement is None or replacement.id == model.id:
                    return error_response(
                        "replacement_required",
                        "Select a replacement before deleting the active default model.",
                        409,
                    )
                replacement.is_active = True
                replacement.is_default = True
            db_session.delete(model)
        return "", 204

    @app.post("/api/v1/providers/<provider_id>/models/refresh")
    @require_auth
    def model_refresh(provider_id: str):
        from pandrator.logic.llm_handler import discover_provider_models

        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            existing = list(
                db_session.scalars(
                    select(ProviderModel).where(
                        ProviderModel.provider_id == provider_id
                    )
                ).all()
            )
            fallback_env = str(
                (provider.options_json or {}).get("api_key_env")
                or DEFAULT_PROVIDER_ENVS.get(provider.provider_key.lower(), "")
            )
            profile_id = (
                str((provider.options_json or {}).get("profile_id") or "")
                .strip()
                .lower()
            )
            share_credential = not bool(
                (provider.options_json or {}).get("is_custom")
                or profile_id in {"custom-openai", "lm-studio", "ollama"}
            )
            credential = resolve_provider_credential(
                database,
                paths,
                provider.provider_key,
                provider.secret_ref,
                fallback_environment_variable=fallback_env,
                shared=share_credential,
            )
            discovery = discover_provider_models(
                {
                    "provider": provider.provider_key,
                    "api_base": provider.base_url,
                    "api_key_env": credential.environment_variable,
                    "api_key": credential.value,
                    "models": [item.model_id for item in existing],
                }
            )
            detected = list(discovery.models) if discovery.source != "preserved" else []
            known = {item.model_id for item in existing}
            added = []
            for model_id in detected:
                if model_id in known:
                    continue
                model = ProviderModel(
                    provider_id=provider_id,
                    model_id=model_id,
                    is_active=False,
                    is_default=False,
                    options_json={"discovery_source": discovery.source},
                )
                db_session.add(model)
                added.append(model_id)
            return jsonify(
                {
                    "detected": detected,
                    "added": added,
                    "preserved": sorted(known),
                    "source": discovery.source,
                    "endpoint": discovery.endpoint,
                    "warning": discovery.warning,
                }
            )

    @app.get("/api/v1/credentials")
    @require_auth
    def credential_list():
        return jsonify({"items": auxiliary_profiles(database, paths)})

    @app.put("/api/v1/credentials/<credential_id>")
    @require_auth
    def credential_update(credential_id: str):
        profile = next(
            (item for item in AUXILIARY_CREDENTIALS if item["id"] == credential_id),
            None,
        )
        if profile is None:
            return error_response("not_found", "Credential setting not found.", 404)
        payload = CredentialUpdate.model_validate(request.get_json(silent=True) or {})
        submitted_key = str(payload.api_key or "").strip()
        if submitted_key and payload.clear:
            return error_response(
                "validation_error",
                "Choose either a replacement API key or remove the current key.",
                422,
            )
        if (
            not submitted_key
            and not payload.clear
            and payload.credential_backend is None
        ):
            return error_response(
                "validation_error",
                "Enter an API key, choose an external credential backend, or remove the saved credential.",
                422,
            )
        key = auxiliary_credential_key(credential_id)
        try:
            with database.session() as db_session:
                references = auxiliary_reference_map(db_session)
                current_reference = references.get(
                    credential_id,
                    database_reference(key),
                )
                if payload.clear:
                    delete_managed_reference(
                        db_session,
                        current_reference,
                        preserve_shared=False,
                    )
                    set_auxiliary_reference(db_session, credential_id, None)
                else:
                    configured = configure_credential_reference(
                        db_session,
                        database,
                        paths,
                        key=key,
                        label=f"{profile['label']} API key",
                        current_reference=current_reference,
                        backend=payload.credential_backend or "database",
                        locator=payload.credential_reference or "",
                        secret_value=submitted_key,
                        delete_previous=payload.delete_previous_credential,
                    )
                    set_auxiliary_reference(
                        db_session,
                        credential_id,
                        configured.reference,
                    )
        except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
            return error_response("credential_unavailable", str(error), 422)
        updated = next(
            item
            for item in auxiliary_profiles(database, paths)
            if item["id"] == credential_id
        )
        if not payload.clear:
            updated["previous_credential_retained"] = (
                configured.previous_credential_retained
            )
        return jsonify(updated)

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
        name = payload.name.strip()
        if not name:
            return error_response("validation_error", "Voice name is required.", 422)
        with database.session() as db_session:
            if db_session.scalar(select(Voice).where(Voice.name == name)) is not None:
                return error_response(
                    "already_exists", "A voice with that name already exists.", 409
                )
            voice = Voice(
                name=name,
                language=str(payload.language or "").strip() or None,
                description=str(payload.description or "").strip() or None,
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
        with database.session() as db_session:
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

    @app.post("/api/v1/voices/<voice_id>/samples")
    @require_auth
    def voice_sample_upload(voice_id: str):
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
        suffix = Path(secure_filename(incoming.filename)).suffix or ".webm"
        temporary = paths.temporary / f"voice-{uuid.uuid4()}{suffix}"
        incoming.save(temporary)
        source_artifact = artifacts.register(
            temporary, kind="audio", role="recording_upload"
        )
        job = jobs.enqueue(
            "voice.normalize_recording",
            {
                "voice_id": voice_id,
                "source_artifact_id": source_artifact.id,
                "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
                "expected_voice_revision": expected_revision,
            },
        )
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/voices/<voice_id>/samples/<sample_id>/replace")
    @require_auth
    def voice_sample_replace(voice_id: str, sample_id: str):
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
        suffix = Path(secure_filename(incoming.filename)).suffix or ".webm"
        temporary = paths.temporary / f"voice-{uuid.uuid4()}{suffix}"
        incoming.save(temporary)
        source_artifact = artifacts.register(
            temporary,
            kind="audio",
            role="recording_upload",
        )
        job = jobs.enqueue(
            "voice.normalize_recording",
            {
                "voice_id": voice_id,
                "replace_sample_id": sample_id,
                "source_artifact_id": source_artifact.id,
                "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg",
                "expected_voice_revision": expected_revision,
            },
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
        job = jobs.enqueue(
            "voice.publish",
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
        if not bool(service.get("supports_voice_deletion")):
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
        settings = request.get_json(silent=True) or {}
        with database.session() as db_session:
            sample = db_session.get(VoiceSample, sample_id)
            if sample is None or sample.voice_id != voice_id:
                return error_response("not_found", "Voice sample not found.", 404)
            artifact_id = sample.artifact_id
        job = jobs.enqueue(
            "voice.transcribe",
            {
                "voice_id": voice_id,
                "sample_id": sample_id,
                "sample_artifact_id": artifact_id,
                "settings": settings,
            },
            resource_keys=[f"stt:{str(settings.get('stt_compute_backend') or 'auto')}"]
            + (
                [f"gpu:{str(settings.get('stt_compute_backend'))}"]
                if str(settings.get("stt_compute_backend") or "").lower()
                in {"cuda", "vulkan", "metal"}
                else []
            ),
        )
        return jsonify(_job_payload(job)), 202

    @app.patch("/api/v1/voices/<voice_id>/samples/<sample_id>/transcript")
    @require_auth
    def voice_sample_transcript(voice_id: str, sample_id: str):
        payload = VoiceTranscriptReview.model_validate(
            request.get_json(silent=True) or {}
        )
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
            raw_etag = request.headers.get("If-Match", "").strip('W/" ')
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
