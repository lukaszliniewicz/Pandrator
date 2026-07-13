"""Flask application factory for the browser and API clients."""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import uuid
import base64
import hashlib
import re
from functools import wraps
from pathlib import Path
from typing import Any

from flask import Flask, Response, g, jsonify, request, send_file, send_from_directory, session
from pydantic import ValidationError
from sqlalchemy import select
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.utils import secure_filename

from pandrator.runtime import DataPaths

from .artifacts import ArtifactService, sha256_file
from .auth import AuthService, BootstrapTokenStore
from .capabilities import probe_capabilities
from .database import Database
from .jobs import JobQueue
from .legacy_migration import import_legacy_data
from .maintenance import apply_retention
from .models import AgentRun, AgentStep, AppSetting, AppSettingHistory, Artifact, ArtifactEdge, Document, DocumentRevision, Job, Provider, ProviderModel, Segment, SessionRecord, SourceRecord, TimedWord, TrainingRun, Voice, VoiceSample, new_id, utcnow
from .openapi import build_openapi_document
from .parity_registry import build_registry
from .schemas import AgentRunCreateRequest, BootstrapRequest, BundleExportRequest, BundleImportRequest, ChunkUploadInitialize, GenerationPlanCreate, GenerationSegmentUpdate, GenerationStartRequest, JobCreate, LoginRequest, ModelCreate, ModelUpdate, OptimizationReviewRequest, OutcomePlanUpdate, OutputAssemblyCreateRequest, PdfEditRequest, ProviderCreate, ProviderTestRequest, ProviderUpdate, RvcConvertRequest, RvcModelUploadRequest, SessionCreate, SessionSettingsUpdate, SessionUpdate, SettingUpdate, SourceAttachRequest, SourceReuseRequest, SourceUpdateRequest, SourceUrlRequest, SubtitleReviewRequest, TokenCreateRequest, TrainingCreateRequest, TtsEndpointDiscoveryRequest, TtsVoicePreviewRequest, VoiceCreate, VoiceTranscriptReview
from .sessions import RevisionConflict, SessionService
from .subtitle_review import SubtitleReviewService
from .workflows import WorkflowService
from .uploads import ChunkUploadService
from .workspace import BUILTIN_DEFAULTS, SETTING_SECTIONS, GenerationService, OutcomePlanService, RevisionConflict as WorkspaceRevisionConflict, SourceLibraryService, WorkspaceSettingsService


def _load_or_create_flask_secret(paths: DataPaths) -> str:
    target = paths.root / ".flask-secret"
    try:
        secret = target.read_text(encoding="utf-8").strip()
        if secret:
            return secret
    except OSError:
        pass
    secret = secrets.token_hex(32)
    target.write_text(secret, encoding="utf-8")
    try:
        target.chmod(0o600)
    except OSError:
        pass
    return secret


def _model_dict(record, fields: tuple[str, ...]) -> dict[str, Any]:
    payload = {field: getattr(record, field) for field in fields}
    for key, value in list(payload.items()):
        if hasattr(value, "isoformat"):
            payload[key] = value.isoformat()
    return payload


def _frontend_script_policy(static_dir: Path) -> str:
    index = static_dir / "index.html"
    try:
        html = index.read_text(encoding="utf-8")
    except OSError:
        return "'self'"
    hashes = []
    for script in re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", html, flags=re.DOTALL | re.IGNORECASE):
        digest = base64.b64encode(hashlib.sha256(script.encode("utf-8")).digest()).decode("ascii")
        hashes.append(f"'sha256-{digest}'")
    return " ".join(["'self'", *hashes])


def _session_payload(record) -> dict[str, Any]:
    return _model_dict(
        record,
        ("id", "name", "storage_key", "workflow_kind", "source_language", "target_language", "workflow_preset", "included_stages_json", "status", "revision", "created_at", "updated_at"),
    )


def _job_payload(record) -> dict[str, Any]:
    return _model_dict(
        record,
        ("id", "kind", "session_id", "workflow_run_id", "status", "payload_json", "result_json", "progress", "error_code", "error_message", "attempts", "max_attempts", "created_at", "started_at", "finished_at", "updated_at"),
    )


def create_app(
    *,
    data_root: str | os.PathLike[str] | None = None,
    testing: bool = False,
    trusted_hosts: list[str] | None = None,
    proxy_hops: int = 0,
    secure_cookies: bool = False,
    bootstrap_tokens: BootstrapTokenStore | None = None,
) -> Flask:
    paths = DataPaths.from_value(data_root).ensure()
    migration = import_legacy_data(paths)
    database = Database(paths.database)
    with database.session() as retention_session:
        retention_record = retention_session.get(AppSetting, "web.preferences")
        retention_days = int((retention_record.value_json or {}).get("retention_days", 30)) if retention_record and isinstance(retention_record.value_json, dict) else 30
    apply_retention(database, paths, retention_days)
    auth = AuthService(database)
    jobs = JobQueue(database)
    sessions = SessionService(database)
    artifacts = ArtifactService(database, paths)
    workflows = WorkflowService(database, jobs)
    workspace_settings = WorkspaceSettingsService(database)
    outcome_plans = OutcomePlanService(database)
    source_library = SourceLibraryService(database)
    source_library.backfill_legacy()
    generation = GenerationService(database, jobs, workspace_settings)
    chunk_uploads = ChunkUploadService(database, paths, artifacts, source_library)
    chunk_uploads.cleanup_expired()

    def session_dir(session_id: str) -> Path:
        with database.session() as db_session:
            record = db_session.get(SessionRecord, session_id)
            if record is None:
                raise KeyError(session_id)
            destination = paths.sessions / record.storage_key
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    subtitle_review = SubtitleReviewService(database, artifacts, session_dir)
    bootstrap = bootstrap_tokens or BootstrapTokenStore()

    static_dir = Path(__file__).with_name("static")
    frontend_script_policy = _frontend_script_policy(static_dir)
    app = Flask(__name__, static_folder=str(static_dir), static_url_path="/assets")
    app.config.update(
        SECRET_KEY=_load_or_create_flask_secret(paths),
        TESTING=testing,
        MAX_CONTENT_LENGTH=10 * 1024 * 1024 * 1024,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=secure_cookies,
        TRUSTED_HOSTS=trusted_hosts or ["localhost", "127.0.0.1", "[::1]"],
    )
    if proxy_hops:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=proxy_hops, x_proto=proxy_hops, x_host=proxy_hops, x_port=proxy_hops)
    app.extensions["pandrator"] = {
        "paths": paths,
        "database": database,
        "auth": auth,
        "jobs": jobs,
        "sessions": sessions,
        "artifacts": artifacts,
        "workflows": workflows,
        "workspace_settings": workspace_settings,
        "outcome_plans": outcome_plans,
        "source_library": source_library,
        "generation": generation,
        "chunk_uploads": chunk_uploads,
        "bootstrap": bootstrap,
        "migration": migration,
    }

    def error_response(code: str, message: str, status: int, details: Any = None):
        return jsonify({"error": {"code": code, "message": message, "details": details, "request_id": getattr(g, "request_id", "")}}), status

    def bearer_authenticated() -> bool:
        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return False
        return auth.verify_api_token(header.split(" ", 1)[1].strip())

    def authenticated() -> bool:
        return bool(session.get("authenticated")) or bearer_authenticated()

    def require_auth(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            if not authenticated():
                return error_response("authentication_required", "Authentication is required.", 401)
            return function(*args, **kwargs)

        return wrapped

    @app.before_request
    def _request_context():
        g.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        if (paths.root / "maintenance.json").is_file() and request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint not in {
            "auth_login",
            "auth_logout",
            "auth_bootstrap",
            "job_cancel",
            "training_cancel",
        }:
            return error_response("maintenance", "Pandrator is draining work for an update. Try again after it restarts.", 503)
        if request.method in {"POST", "PUT", "PATCH", "DELETE"} and request.endpoint not in {
            "auth_login",
            "auth_bootstrap",
            "health",
        }:
            if bearer_authenticated():
                return None
            if session.get("authenticated"):
                supplied = request.headers.get("X-CSRF-Token", "")
                expected = str(session.get("csrf_token") or "")
                if not expected or not secrets.compare_digest(supplied, expected):
                    return error_response("csrf_failed", "The CSRF token is missing or invalid.", 403)
        return None

    @app.after_request
    def _security_headers(response):
        response.headers["X-Request-ID"] = getattr(g, "request_id", "")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        # PDF artifacts are rendered in a same-origin preview frame.  Keep
        # cross-origin framing blocked while allowing that managed viewer.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        response.headers["Content-Security-Policy"] = f"default-src 'self'; frame-ancestors 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src {frontend_script_policy}"
        return response

    @app.errorhandler(ValidationError)
    def _validation_error(error):
        return error_response("validation_error", "The request payload is invalid.", 422, error.errors())

    @app.errorhandler(HTTPException)
    def _http_error(error):
        return error_response(error.name.lower().replace(" ", "_"), error.description, error.code or 500)

    @app.errorhandler(Exception)
    def _unexpected_error(error):
        if testing:
            raise error
        app.logger.exception("Unhandled API error")
        return error_response("internal_error", "An unexpected error occurred.", 500)

    @app.get("/api/v1/health")
    def health():
        return jsonify({"status": "ok", "database": paths.database.name, "migration": migration.get("status")})

    @app.get("/api/v1/openapi.json")
    def openapi():
        return jsonify(build_openapi_document())

    @app.get("/api/v1/auth/status")
    def auth_status():
        return jsonify({"initialized": auth.initialized(), "authenticated": authenticated(), "csrf_token": session.get("csrf_token") if session.get("authenticated") else None})

    @app.post("/api/v1/auth/bootstrap")
    def auth_bootstrap():
        payload = BootstrapRequest.model_validate(request.get_json(silent=True) or {})
        if not bootstrap.consume(payload.token):
            return error_response("invalid_bootstrap_token", "The local bootstrap token is invalid or expired.", 401)
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(24)
        return jsonify({"authenticated": True, "csrf_token": session["csrf_token"]})

    @app.post("/api/v1/auth/login")
    def auth_login():
        payload = LoginRequest.model_validate(request.get_json(silent=True) or {})
        if not auth.verify_password(payload.password):
            return error_response("invalid_credentials", "The password is incorrect.", 401)
        session.clear()
        session["authenticated"] = True
        session["csrf_token"] = secrets.token_urlsafe(24)
        return jsonify({"authenticated": True, "csrf_token": session["csrf_token"]})

    @app.post("/api/v1/auth/logout")
    @require_auth
    def auth_logout():
        session.clear()
        return jsonify({"authenticated": False})

    @app.get("/api/v1/auth/tokens")
    @require_auth
    def token_list():
        return jsonify({"items": [_model_dict(item, ("id", "label", "token_prefix", "created_at", "last_used_at", "revoked_at")) for item in auth.list_tokens()]})

    @app.post("/api/v1/auth/tokens")
    @require_auth
    def token_create():
        payload = TokenCreateRequest.model_validate(request.get_json(silent=True) or {})
        token, raw = auth.create_api_token(payload.label)
        return jsonify({"id": token.id, "label": token.label, "token": raw}), 201

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
        return jsonify(probe_capabilities(paths, local_mode=request.remote_addr in {"127.0.0.1", "::1"}))

    @app.get("/pandrator-logo.png")
    def pandrator_logo():
        """Serve the application mark at the stable URL used by the SPA shell."""
        return send_from_directory(static_dir, "pandrator-logo.png")

    @app.get("/api/v1/parity")
    @require_auth
    def parity_registry():
        return jsonify(build_registry())

    @app.get("/api/v1/services/tts")
    @require_auth
    def tts_services():
        import socket
        from concurrent.futures import ThreadPoolExecutor
        from urllib.parse import urlparse

        from pandrator.logic import tts_handler
        from pandrator.logic.tts_provider_profiles import list_tts_provider_profiles

        with database.session() as db_session:
            connections = db_session.get(AppSetting, "services.tts")
            defaults = db_session.get(AppSetting, "defaults.tts")
            connection_value = dict(connections.value_json or {}) if connections and isinstance(connections.value_json, dict) else {}
            default_value = dict(defaults.value_json or {}) if defaults and isinstance(defaults.value_json, dict) else {}
            if not connection_value and isinstance(default_value.get("provider_configs"), list):
                connection_value = {"provider_configs": list(default_value["provider_configs"])}
            revision = connections.revision if connections else 0
            default_revision = defaults.revision if defaults else 0
        services = [dict(item) for item in tts_handler.get_service_configs({**default_value, **connection_value})]
        for service in services:
            key_env = str(service.get("api_key_env") or "").strip()
            credential_configured = bool(
                str(service.get("api_key") or "").strip()
                or (key_env and os.getenv(key_env, "").strip())
            )
            service["credential_configured"] = (
                credential_configured if service.get("kind") == "commercial" else True
            )
        if request.args.get("refresh", "").lower() in {"1", "true", "yes"}:
            def probe(item: dict[str, Any]) -> bool:
                parsed = urlparse(str(item.get("api_base") or ""))
                if not parsed.hostname:
                    return False
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                try:
                    with socket.create_connection((parsed.hostname, port), timeout=0.35):
                        return True
                except OSError:
                    return False

            with ThreadPoolExecutor(max_workers=min(12, max(1, len(services)))) as executor:
                states = list(executor.map(probe, services))
            for service, online in zip(services, states):
                service["online"] = online
                if service.get("kind") == "commercial":
                    service["available"] = bool(service["credential_configured"])
                    service["availability_reason"] = "" if service["available"] else "API key not configured"
                else:
                    service["available"] = online
                    service["availability_reason"] = "" if online else "Service is not running"

        previews = []
        with database.session() as db_session:
            preview_artifacts = list(
                db_session.scalars(
                    select(Artifact)
                    .where(Artifact.role == "tts_voice_preview", Artifact.state == "current")
                    .order_by(Artifact.updated_at.desc())
                ).all()
            )
            for artifact in preview_artifacts:
                metadata = dict(artifact.metadata_json or {})
                if not metadata.get("voice"):
                    continue
                try:
                    if not paths.managed_path(artifact.relative_path).is_file():
                        continue
                except ValueError:
                    continue
                previews.append(
                    {
                        "artifact_id": artifact.id,
                        "service_id": str(metadata.get("service_id") or ""),
                        "model": str(metadata.get("model") or ""),
                        "voice": str(metadata.get("voice") or ""),
                        "language": str(metadata.get("language") or ""),
                        "preview_text": str(metadata.get("preview_text") or ""),
                        "updated_at": artifact.updated_at.isoformat(),
                    }
                )

        response = jsonify({"value": connection_value, "revision": revision, "default_value": default_value, "default_service": str(default_value.get("service") or BUILTIN_DEFAULTS["tts"]["service"]), "default_revision": default_revision, "builtin_defaults": BUILTIN_DEFAULTS["tts"], "services": services, "profiles": list_tts_provider_profiles(), "previews": previews})
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.post("/api/v1/services/tts/discover")
    @require_auth
    def tts_service_discover():
        from pandrator.logic.tts_endpoint_discovery import discover_tts_endpoint

        payload = TtsEndpointDiscoveryRequest.model_validate(request.get_json(silent=True) or {})
        result = discover_tts_endpoint(payload.base_url)
        return jsonify(result), 200 if result.get("success") else 422

    @app.post("/api/v1/services/tts/<service_id>/preview")
    @require_auth
    def tts_voice_preview(service_id: str):
        from pandrator.logic import tts_handler

        payload = TtsVoicePreviewRequest.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            connections = db_session.get(AppSetting, "services.tts")
            defaults = db_session.get(AppSetting, "defaults.tts")
            connection_value = dict(connections.value_json or {}) if connections and isinstance(connections.value_json, dict) else {}
            default_value = dict(defaults.value_json or {}) if defaults and isinstance(defaults.value_json, dict) else {}
        catalogue_settings = {**default_value, **connection_value}
        service = tts_handler.get_service_config(catalogue_settings, service_id)
        if service is None:
            return error_response("not_found", "TTS service not found.", 404)
        model = payload.model or str(service.get("default_model") or "")
        # This endpoint is specifically for pre-built catalogue voices.  Qwen's
        # Base checkpoint is cloning-only, so never let stale UI state or a
        # manually supplied request route a named voice to that model.  The
        # Qwen service lazily downloads CustomVoice when it receives this ID.
        if str(service.get("id") or service_id).lower().replace("-", "_") == "kobold_qwen":
            model = tts_handler.KOBOLD_QWEN_DEFAULT_MODEL
        default_voices = service.get("default_voices") if isinstance(service.get("default_voices"), dict) else {}
        voice = payload.voice or str(default_voices.get(model) or service.get("default_voice") or "")
        service_name = "OpenAI Compatible" if service.get("is_custom") else str(service.get("name") or service_id)
        settings = {
            **BUILTIN_DEFAULTS["tts"],
            **default_value,
            **(service.get("settings") if isinstance(service.get("settings"), dict) else {}),
            **connection_value,
            "service": service_name,
            "model": model,
            "xtts_model": model,
            "voice": voice,
            "speaker": voice,
            "language": payload.language or str(default_value.get("language") or "en"),
            "preview_service_id": str(service.get("id") or service_id),
            "preview_api_base": str(service.get("api_base") or ""),
        }
        if service.get("is_custom"):
            settings["openai_audio_endpoint"] = str(service.get("id") or service_id)
        job = jobs.enqueue("tts.preview", {"text": payload.text, "settings": settings}, resource_keys=[f"service:tts:{service_id}"])
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/sessions")
    @require_auth
    def session_list():
        return jsonify({"items": [_session_payload(item) for item in sessions.list(include_trashed=request.args.get("include_trashed") == "true")]})

    @app.get("/api/v1/defaults/<section>")
    @require_auth
    def global_default_get(section: str):
        if section not in SETTING_SECTIONS:
            return error_response("not_found", "Settings section not found.", 404)
        with database.session() as db_session:
            record = db_session.get(AppSetting, f"defaults.{section}")
            value = dict(record.value_json or {}) if record and isinstance(record.value_json, dict) else {}
            revision = record.revision if record else 0
        response = jsonify({"section": section, "builtin": BUILTIN_DEFAULTS[section], "value": value, "effective": {**BUILTIN_DEFAULTS[section], **value}, "revision": revision})
        response.headers["ETag"] = f'"{revision}"'
        return response

    @app.get("/api/v1/settings/<setting_key>")
    @require_auth
    def setting_get(setting_key: str):
        with database.session() as db_session:
            record = db_session.get(AppSetting, setting_key)
            if record is None:
                return error_response("not_found", "Setting not found.", 404)
            response = jsonify({"key": record.key, "value": record.value_json, "revision": record.revision, "updated_at": record.updated_at.isoformat()})
            response.headers["ETag"] = f'"{record.revision}"'
            return response

    @app.put("/api/v1/settings/<setting_key>")
    @require_auth
    def setting_put(setting_key: str):
        if not setting_key or len(setting_key) > 120:
            return error_response("validation_error", "Invalid setting key.", 422)
        payload = SettingUpdate.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        with database.session() as db_session:
            record = db_session.get(AppSetting, setting_key)
            if record is None:
                if raw_etag not in {"", "0", "*"}:
                    return error_response("revision_conflict", "The setting does not exist at that revision.", 409)
                record = AppSetting(key=setting_key, value_json=payload.value, revision=1)
                db_session.add(record)
            else:
                try:
                    expected = int(raw_etag)
                except ValueError:
                    return error_response("precondition_required", "If-Match must contain the current setting revision.", 428)
                if expected != record.revision:
                    return error_response("revision_conflict", "The setting changed in another client.", 409)
                db_session.add(AppSettingHistory(key=record.key, value_json=record.value_json, revision=record.revision))
                record.value_json = payload.value
                record.revision += 1
                record.updated_at = utcnow()
            db_session.flush()
            result = {"key": record.key, "value": record.value_json, "revision": record.revision, "updated_at": record.updated_at.isoformat()}
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.post("/api/v1/sessions")
    @require_auth
    def session_create():
        payload = SessionCreate.model_validate(request.get_json(silent=True) or {})
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

    @app.patch("/api/v1/sessions/<session_id>")
    @require_auth
    def session_update(session_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            revision = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current revision.", 428)
        payload = SessionUpdate.model_validate(request.get_json(silent=True) or {})
        raw_changes = payload.model_dump(exclude_unset=True)
        changes = {key: value for key, value in raw_changes.items() if value is not None or key == "target_language"}
        if "included_stages" in changes:
            changes["included_stages_json"] = changes.pop("included_stages")
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
            return error_response("precondition_required", "If-Match must contain the current session revision.", 428)
        with database.session() as db_session:
            active = db_session.scalar(select(Job).where(Job.session_id == session_id, Job.status.in_(("queued", "running", "cancel_requested"))))
            if active is not None:
                return error_response("session_busy", "Stop or cancel active work before moving this session to trash.", 409)
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
            return error_response("precondition_required", "If-Match must contain the current session revision.", 428)
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
        return jsonify({"session_id": session_id, "reports": artifacts.reconcile(session_id)})

    @app.get("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    def session_settings_get(session_id: str, section: str):
        try:
            result = workspace_settings.get(session_id, section)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.put("/api/v1/sessions/<session_id>/settings/<section>")
    @require_auth
    def session_settings_put(session_id: str, section: str):
        payload = SessionSettingsUpdate.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current settings revision.", 428)
        try:
            result = workspace_settings.update(session_id, section, expected, payload.value)
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
        sections = body.get("sections") if isinstance(body.get("sections"), list) else None
        overrides = body.get("overrides") if isinstance(body.get("overrides"), dict) else {}
        try:
            value, digest = workspace_settings.resolve(session_id, sections=sections, run_override=overrides)
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
            return error_response("precondition_required", "If-Match must contain the current outcome-plan revision.", 428)
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
        return jsonify({"items": source_library.list(include_trashed=request.args.get("include_trashed") == "true")})

    @app.patch("/api/v1/sources/<source_asset_id>")
    @require_auth
    def source_library_update(source_asset_id: str):
        payload = SourceUpdateRequest.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current source revision.", 428)
        try:
            result = source_library.rename(source_asset_id, expected, payload.display_name)
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
            return error_response("precondition_required", "If-Match must contain the current source revision.", 428)
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
            return error_response("precondition_required", "If-Match must contain the current source revision.", 428)
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
        payload = SourceAttachRequest.model_validate(request.get_json(silent=True) or {})
        try:
            result = source_library.attach(session_id, payload.source_asset_id, role=payload.role)
        except KeyError:
            return error_response("not_found", "Session or source asset not found.", 404)
        return jsonify(result), 201

    @app.delete("/api/v1/sessions/<session_id>/sources/<attachment_id>")
    @require_auth
    def session_source_detach(session_id: str, attachment_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the attachment revision.", 428)
        try:
            source_library.detach(session_id, attachment_id, expected)
        except KeyError:
            return error_response("not_found", "Session source attachment not found.", 404)
        except WorkspaceRevisionConflict as error:
            return error_response("revision_conflict", str(error), 409)
        return "", 204

    @app.get("/api/v1/sessions/<session_id>/documents")
    @require_auth
    def session_documents(session_id: str):
        with database.session() as db_session:
            if db_session.get(SessionRecord, session_id) is None:
                return error_response("not_found", "Session not found.", 404)
            documents = list(db_session.scalars(select(Document).where(Document.session_id == session_id).order_by(Document.created_at)).all())
            items = []
            for document in documents:
                revisions = list(db_session.scalars(select(DocumentRevision).where(DocumentRevision.document_id == document.id).order_by(DocumentRevision.revision_number.desc())).all())
                items.append({
                    "id": document.id,
                    "stage": document.stage,
                    "language": document.language,
                    "active_revision_id": document.active_revision_id,
                    "revisions": [{"id": revision.id, "revision_number": revision.revision_number, "parent_revision_id": revision.parent_revision_id, "reviewed": revision.reviewed, "content_hash": revision.content_hash, "created_at": revision.created_at.isoformat()} for revision in revisions],
                })
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
            rows = list(db_session.scalars(select(TimedWord).where(TimedWord.revision_id == revision_id, TimedWord.ordinal >= cursor).order_by(TimedWord.ordinal).limit(limit + 1)).all())
            has_more = len(rows) > limit
            rows = rows[:limit]
            return jsonify({
                "items": [_model_dict(word, ("id", "revision_id", "segment_id", "ordinal", "text", "start_ms", "end_ms", "speaker", "confidence", "metadata_json")) for word in rows],
                "next_cursor": rows[-1].ordinal + 1 if rows and has_more else None,
            })

    @app.post("/api/v1/sessions/<session_id>/generation-plan")
    @require_auth
    def generation_plan_create(session_id: str):
        payload = GenerationPlanCreate.model_validate(request.get_json(silent=True) or {})
        try:
            result = generation.create_plan(session_id, source_revision_id=payload.source_revision_id, segments=[item.model_dump() for item in payload.segments], settings=payload.settings)
        except KeyError:
            return error_response("not_found", "Session or source revision not found.", 404)
        except ValueError as error:
            return error_response("validation_error", str(error), 422)
        return jsonify(result), 201

    @app.get("/api/v1/sessions/<session_id>/generation-segments")
    @require_auth
    def generation_segment_list(session_id: str):
        marked_arg = request.args.get("marked")
        marked = None if marked_arg is None else marked_arg.lower() == "true"
        try:
            result = generation.list_segments(session_id, cursor=request.args.get("cursor", 0, type=int), limit=request.args.get("limit", 100, type=int), status=request.args.get("status"), marked=marked)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        return jsonify(result)

    @app.patch("/api/v1/generation-segments/<segment_id>")
    @require_auth
    def generation_segment_update(segment_id: str):
        payload = GenerationSegmentUpdate.model_validate(request.get_json(silent=True) or {})
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current segment revision.", 428)
        try:
            result = generation.update_segment(segment_id, expected, payload.model_dump(exclude_none=True))
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
            return error_response("precondition_required", "If-Match must contain the current segment revision.", 428)
        try:
            result = generation.select_take(segment_id, take_id, expected)
        except KeyError:
            return error_response("not_found", "Generation segment or audio take not found.", 404)
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

    @app.post("/api/v1/sessions/<session_id>/generation-runs")
    @require_auth
    def generation_run_start(session_id: str):
        payload = GenerationStartRequest.model_validate(request.get_json(silent=True) or {})
        try:
            result = generation.start(session_id, run_override=payload.run_override, segment_ids=payload.segment_ids, operation=payload.operation)
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
        payload = OutputAssemblyCreateRequest.model_validate(request.get_json(silent=True) or {})
        try:
            result = generation.create_assembly(
                session_id,
                generation_run_id=payload.generation_run_id,
                run_override=payload.run_override,
            )
        except KeyError:
            return error_response("not_found", "Session or generation run not found.", 404)
        except ValueError as error:
            return error_response("assembly_unavailable", str(error), 409)
        return jsonify(result), 202

    @app.get("/api/v1/sessions/<session_id>/agent-runs")
    @require_auth
    def agent_run_list(session_id: str):
        with database.session() as db_session:
            if db_session.get(SessionRecord, session_id) is None:
                return error_response("not_found", "Session not found.", 404)
            records = list(db_session.scalars(select(AgentRun).where(AgentRun.session_id == session_id).order_by(AgentRun.created_at.desc())).all())
            return jsonify({"items": [_model_dict(item, ("id", "session_id", "source_artifact_id", "result_artifact_id", "job_id", "status", "settings_json", "created_at", "updated_at")) for item in records]})

    @app.post("/api/v1/sessions/<session_id>/agent-runs")
    @require_auth
    def agent_run_create(session_id: str):
        payload = AgentRunCreateRequest.model_validate(request.get_json(silent=True) or {})
        try:
            sessions.get(session_id)
            artifacts.resolve(payload.source_artifact_id)
        except KeyError:
            return error_response("not_found", "Session or source artifact not found.", 404)
        run_id = new_id()
        with database.session() as db_session:
            db_session.add(AgentRun(id=run_id, session_id=session_id, source_artifact_id=payload.source_artifact_id, status="queued", settings_json={**payload.settings, "agentic": True}))
        job = jobs.enqueue("source.clean", {"session_id": session_id, "source_artifact_id": payload.source_artifact_id, "agent_run_id": run_id, "settings": {**payload.settings, "agentic": True}}, session_id=session_id, resource_keys=[f"session:{session_id}", "service:llm"])
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
                return error_response("not_found", "Agentic cleaning run not found.", 404)
            records = list(db_session.scalars(select(AgentStep).where(AgentStep.agent_run_id == run_id).order_by(AgentStep.ordinal)).all())
            return jsonify({"items": [_model_dict(item, ("id", "agent_run_id", "ordinal", "phase", "status", "summary", "input_json", "output_json", "cost_usd", "created_at")) for item in records]})

    @app.post("/api/v1/agent-runs/<run_id>/accept")
    @require_auth
    def agent_run_accept(run_id: str):
        with database.session() as db_session:
            run = db_session.get(AgentRun, run_id)
            if run is None:
                return error_response("not_found", "Agentic cleaning run not found.", 404)
            if run.status != "completed" or not run.result_artifact_id:
                return error_response("invalid_state", "Only a completed cleaning result can be accepted.", 409)
            run.status = "accepted"
            run.updated_at = utcnow()
            return jsonify(_model_dict(run, ("id", "status", "result_artifact_id", "updated_at")))

    @app.post("/api/v1/sessions/<session_id>/bundle")
    @require_auth
    def session_bundle_export(session_id: str):
        payload = BundleExportRequest.model_validate(request.get_json(silent=True) or {})
        try:
            sessions.get(session_id)
        except KeyError:
            return error_response("not_found", "Session not found.", 404)
        job = jobs.enqueue("session.bundle.export", {"session_id": session_id, "include_sources": payload.include_sources}, session_id=session_id)
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/session-bundles/import")
    @require_auth
    def session_bundle_import():
        payload = BundleImportRequest.model_validate(request.get_json(silent=True) or {})
        try:
            artifacts.resolve(payload.source_artifact_id)
        except KeyError:
            return error_response("not_found", "Bundle artifact not found.", 404)
        job = jobs.enqueue("session.bundle.import", payload.model_dump())
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/jobs")
    @require_auth
    def job_list():
        return jsonify({"items": [_job_payload(item) for item in jobs.list(request.args.get("limit", 100, type=int))]})

    @app.post("/api/v1/jobs")
    @require_auth
    def job_create():
        payload = JobCreate.model_validate(request.get_json(silent=True) or {})
        job = jobs.enqueue(payload.kind, payload.payload, session_id=payload.session_id, max_attempts=payload.max_attempts)
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/jobs/<job_id>")
    @require_auth
    def job_get(job_id: str):
        try:
            return jsonify(_job_payload(jobs.get(job_id)))
        except KeyError:
            return error_response("not_found", "Job not found.", 404)

    @app.get("/api/v1/sessions/<session_id>/workflow")
    @require_auth
    def workflow_get(session_id: str):
        try:
            return jsonify(workflows.snapshot(session_id))
        except KeyError:
            return error_response("not_found", "Session not found.", 404)

    @app.post("/api/v1/sessions/<session_id>/stages/<stage_key>/run")
    @require_auth
    def workflow_run_stage(session_id: str, stage_key: str):
        settings = request.get_json(silent=True) or {}
        if not isinstance(settings, dict):
            return error_response("validation_error", "Stage settings must be an object.", 422)
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
            return error_response("not_found", "Session or subtitle document not found.", 404)

    @app.post("/api/v1/sessions/<session_id>/subtitles/<stage>/review")
    @require_auth
    def subtitle_save_review(session_id: str, stage: str):
        payload = SubtitleReviewRequest.model_validate(request.get_json(silent=True) or {})
        try:
            result = subtitle_review.save_review(
                session_id,
                stage,
                payload.expected_revision,
                [item.model_dump() for item in payload.segments],
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
            return jsonify(_job_payload(jobs.request_cancel(job_id)))
        except KeyError:
            return error_response("not_found", "Job not found.", 404)

    @app.get("/api/v1/events")
    @require_auth
    def events():
        start_id = request.headers.get("Last-Event-ID") or request.args.get("after") or "0"
        try:
            cursor = int(start_id)
        except ValueError:
            cursor = 0

        def stream():
            nonlocal cursor
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                new_events = jobs.events_after(cursor)
                if new_events:
                    for event in new_events:
                        cursor = event.id
                        payload = {"job_id": event.job_id, **event.payload_json}
                        yield f"id: {event.id}\nevent: {event.event_type}\ndata: {json.dumps(payload)}\n\n"
                else:
                    yield ": heartbeat\n\n"
                time.sleep(1)

        return Response(stream(), mimetype="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

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
            return error_response("validation_error", "Unsupported upload purpose.", 422)
        if purpose == "cover" and not requested_session_id:
            return error_response("validation_error", "Cover artwork must belong to a session.", 422)
        if requested_session_id:
            try:
                sessions.get(requested_session_id)
            except KeyError:
                return error_response("not_found", "Session not found.", 404)
        try:
            incoming.save(temporary)
            if purpose == "cover":
                if temporary.stat().st_size > 25 * 1024 * 1024:
                    return error_response("cover_too_large", "Cover artwork must be 25 MiB or smaller.", 413)
                try:
                    from PIL import Image

                    with Image.open(temporary) as image:
                        if image.format not in {"JPEG", "PNG", "WEBP"}:
                            raise ValueError("Use JPEG, PNG, or WebP artwork.")
                        width, height = image.size
                        if width < 1 or height < 1 or width * height > 100_000_000:
                            raise ValueError("Artwork dimensions are invalid or exceed 100 megapixels.")
                        image.verify()
                except Exception as error:
                    return error_response("invalid_cover", f"Cover artwork is not a readable image: {error}", 422)
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
                source_asset = source_library.ensure_for_artifact(artifact.id, display_name=incoming.filename, kind=Path(filename).suffix.lower().lstrip(".") or "file")
                attachment = source_library.attach(requested_session_id, source_asset.id) if requested_session_id else None
            return jsonify({"artifact_id": artifact.id, "source_asset_id": source_asset.id if source_asset else None, "attachment": attachment, "filename": filename, "size_bytes": destination.stat().st_size, "sha256": digest}), 201
        finally:
            if temporary.exists():
                temporary.unlink()

    @app.post("/api/v1/uploads/init")
    @require_auth
    def chunk_upload_init():
        payload = ChunkUploadInitialize.model_validate(request.get_json(silent=True) or {})
        try:
            result = chunk_uploads.initialize(
                filename=payload.filename,
                size_bytes=payload.size_bytes,
                mime_type=payload.mime_type,
                session_id=payload.session_id,
                expected_hash=payload.sha256,
                chunk_size=payload.chunk_size,
                max_size=int(app.config.get("MAX_UPLOAD_SIZE", 100 * 1024 * 1024 * 1024)),
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
        if request.content_length is not None and request.content_length > 16 * 1024 * 1024:
            return error_response("chunk_too_large", "Upload chunks may not exceed 16 MiB.", 413)
        try:
            result = chunk_uploads.write_chunk(upload_id, index, request.stream, supplied_hash=request.headers.get("X-Chunk-SHA256"))
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
        job = jobs.enqueue("source.download_url", {"session_id": session_id, "url": payload.url}, session_id=session_id)
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
            return error_response("not_found", "Reusable source artifact not found.", 404)
        job = jobs.enqueue("source.reuse", {"session_id": session_id, "artifact_id": payload.artifact_id}, session_id=session_id)
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/artifacts")
    @require_auth
    def artifact_list():
        with database.session() as db_session:
            statement = select(Artifact).order_by(Artifact.created_at.desc())
            requested_session = str(request.args.get("session_id") or "")
            if requested_session:
                statement = statement.where(Artifact.session_id == requested_session)
            try:
                limit = max(1, min(500, int(request.args.get("limit") or 500)))
            except ValueError:
                limit = 500
            records = list(db_session.scalars(statement.limit(limit)).all())
            return jsonify({"items": [_model_dict(item, ("id", "session_id", "kind", "role", "relative_path", "mime_type", "size_bytes", "content_hash", "state", "metadata_json", "created_at")) for item in records]})

    @app.post("/api/v1/artifacts/<artifact_id>/optimization-review")
    @require_auth
    def artifact_optimization_review(artifact_id: str):
        payload = OptimizationReviewRequest.model_validate(request.get_json(silent=True) or {})
        try:
            source, source_path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Speech-optimized artifact not found.", 404)
        if source.role != "tts_optimized" or source_path.suffix.lower() != ".json":
            return error_response("validation_error", "Only JSON speech-optimization artifacts use this review endpoint.", 422)
        try:
            rows = json.loads(source_path.read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError) as error:
            return error_response("artifact_invalid", f"The optimization artifact cannot be reviewed: {error}", 422)
        if not isinstance(rows, list):
            return error_response("artifact_invalid", "The optimization artifact must contain a list.", 422)
        edits = {item.index: item.text.strip() for item in payload.items}
        if set(edits) != set(range(len(rows))):
            return error_response("validation_error", "Reviewed text must preserve every item index exactly once.", 422)
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return error_response("artifact_invalid", "Every optimization item must be an object.", 422)
            row["source_text"] = str(row.get("source_text") or row.get("original_sentence") or row.get("text") or "")
            row["text"] = edits[index]
            row["processed_sentence"] = edits[index]
            row["tts_optimized_sentence"] = edits[index]
            row["optimization_reviewed"] = True
        destination = source_path.parent / f"tts-optimized-reviewed-{new_id()}.json"
        destination.write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
        reviewed = artifacts.register(
            destination,
            kind="json",
            role="tts_optimized",
            session_id=source.session_id,
            parent_ids=[source.id],
            metadata={**(source.metadata_json or {}), "reviewed": True, "reviewed_from": source.id},
        )
        return jsonify(_model_dict(reviewed, ("id", "session_id", "kind", "role", "relative_path", "mime_type", "size_bytes", "content_hash", "state", "metadata_json", "created_at"))), 201

    @app.get("/api/v1/artifacts/<artifact_id>/context")
    @require_auth
    def artifact_context(artifact_id: str):
        """Return lightweight lineage metadata used by review and comparison UIs."""
        fields = ("id", "session_id", "kind", "role", "relative_path", "mime_type", "size_bytes", "content_hash", "state", "metadata_json", "created_at")
        with database.session() as db_session:
            artifact = db_session.get(Artifact, artifact_id)
            if artifact is None:
                return error_response("not_found", "Artifact not found.", 404)
            parent_ids = list(db_session.scalars(select(ArtifactEdge.parent_artifact_id).where(ArtifactEdge.child_artifact_id == artifact_id)).all())
            parents = list(db_session.scalars(select(Artifact).where(Artifact.id.in_(parent_ids))).all()) if parent_ids else []
            parents.sort(key=lambda item: (item.role != "extracted_text", item.created_at))
            return jsonify({"artifact": _model_dict(artifact, fields), "parents": [_model_dict(item, fields) for item in parents]})

    @app.get("/api/v1/artifacts/<artifact_id>/content")
    @require_auth
    def artifact_content(artifact_id: str):
        try:
            artifact, path = artifacts.resolve(artifact_id)
        except KeyError:
            return error_response("not_found", "Artifact not found.", 404)
        if not path.is_file():
            return error_response("artifact_missing", "The artifact file is missing.", 410)
        return send_file(path, mimetype=artifact.mime_type, conditional=True, etag=artifact.content_hash)

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
            return send_file(peak_path, mimetype="application/json", conditional=True, etag=_artifact.content_hash)
        job = jobs.enqueue(
            "audio.waveform",
            {"source_artifact_id": artifact_id, "max_points": request.args.get("points", 1600, type=int)},
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
            return error_response("invalid_pdf", "Artifact is not an available PDF.", 422)
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
            providers = list(db_session.scalars(select(Provider).order_by(Provider.label)).all())
            return jsonify({"items": [_model_dict(item, ("id", "kind", "provider_key", "label", "enabled", "base_url", "secret_ref", "options_json", "revision")) for item in providers]})

    @app.get("/api/v1/providers/profiles")
    @require_auth
    def provider_profiles():
        from .provider_settings import list_llm_provider_profiles

        return jsonify({"items": list_llm_provider_profiles()})

    @app.post("/api/v1/providers")
    @require_auth
    def provider_create():
        payload = ProviderCreate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = Provider(kind=payload.kind, provider_key=payload.provider_key, label=payload.label, enabled=payload.enabled, base_url=payload.base_url, secret_ref=payload.secret_ref, options_json=payload.options)
            db_session.add(provider)
            db_session.flush()
            result = _model_dict(provider, ("id", "kind", "provider_key", "label", "enabled", "base_url", "secret_ref", "options_json", "revision"))
        return jsonify(result), 201

    @app.patch("/api/v1/providers/<provider_id>")
    @require_auth
    def provider_update(provider_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current provider revision.", 428)
        payload = ProviderUpdate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            if provider.revision != expected_revision:
                return error_response("revision_conflict", "The provider changed in another client.", 409)
            changes = payload.model_dump(exclude_unset=True)
            if "options" in changes:
                changes["options_json"] = changes.pop("options")
            for key, value in changes.items():
                setattr(provider, key, value)
            provider.revision += 1
            provider.updated_at = utcnow()
            db_session.flush()
            result = _model_dict(provider, ("id", "kind", "provider_key", "label", "enabled", "base_url", "secret_ref", "options_json", "revision"))
        response = jsonify(result)
        response.headers["ETag"] = f'"{result["revision"]}"'
        return response

    @app.delete("/api/v1/providers/<provider_id>")
    @require_auth
    def provider_delete(provider_id: str):
        replacement_id = str((request.get_json(silent=True) or {}).get("replacement_model_record_id") or "")
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            active = db_session.scalar(select(ProviderModel).where(ProviderModel.provider_id == provider_id, ProviderModel.is_default.is_(True)))
            if active is not None:
                replacement = db_session.get(ProviderModel, replacement_id) if replacement_id else None
                if replacement is None or replacement.provider_id == provider_id:
                    return error_response("replacement_required", "Select a default model from another provider before removing this provider.", 409)
                replacement.is_default = True
            db_session.delete(provider)
        return "", 204

    @app.post("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_create(provider_id: str):
        payload = ModelCreate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            if payload.is_default:
                for existing in db_session.scalars(select(ProviderModel)):
                    existing.is_default = False
            model = ProviderModel(provider_id=provider_id, model_id=payload.model_id, is_default=payload.is_default, default_temperature=payload.default_temperature, default_reasoning_effort=payload.default_reasoning_effort, input_cost_per_million=payload.input_cost_per_million, cached_input_cost_per_million=payload.cached_input_cost_per_million, output_cost_per_million=payload.output_cost_per_million, options_json=payload.options)
            db_session.add(model)
            db_session.flush()
            result = _model_dict(model, ("id", "provider_id", "model_id", "is_default", "default_temperature", "default_reasoning_effort", "input_cost_per_million", "cached_input_cost_per_million", "output_cost_per_million", "options_json", "revision"))
        return jsonify(result), 201

    @app.post("/api/v1/providers/<provider_id>/test")
    @require_auth
    def provider_test(provider_id: str):
        from pandrator.logic.llm_handler import chat_completion_with_metadata
        from .provider_settings import build_llm_settings

        payload = ProviderTestRequest.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            selected = payload.model_id or db_session.scalar(select(ProviderModel.model_id).where(ProviderModel.provider_id == provider_id, ProviderModel.is_default.is_(True)))
        settings, model_name = build_llm_settings(database, paths, requested_model=selected)
        result = chat_completion_with_metadata(messages=[{"role": "user", "content": "Reply with exactly OK."}], model_name=model_name, llm_settings=settings, max_tokens=8)
        if not result.content:
            return error_response("provider_test_failed", "The provider returned no usable response. Check its URL, secret reference, and model ID.", 422)
        return jsonify({"ok": True, "model": result.model or model_name, "response": result.content[:80], "cost": result.cost, "cost_source": result.cost_source})

    @app.get("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_list(provider_id: str):
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            records = list(db_session.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider_id).order_by(ProviderModel.model_id)).all())
            return jsonify({"items": [_model_dict(item, ("id", "provider_id", "model_id", "is_default", "default_temperature", "default_reasoning_effort", "input_cost_per_million", "cached_input_cost_per_million", "output_cost_per_million", "options_json", "revision")) for item in records]})

    @app.patch("/api/v1/providers/<provider_id>/models/<model_record_id>")
    @require_auth
    def model_update(provider_id: str, model_record_id: str):
        raw_etag = request.headers.get("If-Match", "").strip('W/" ')
        try:
            expected_revision = int(raw_etag)
        except ValueError:
            return error_response("precondition_required", "If-Match must contain the current model revision.", 428)
        payload = ModelUpdate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            model = db_session.get(ProviderModel, model_record_id)
            if model is None or model.provider_id != provider_id:
                return error_response("not_found", "Model not found.", 404)
            if model.revision != expected_revision:
                return error_response("revision_conflict", "The model settings changed in another client.", 409)
            changes = payload.model_dump(exclude_unset=True)
            if changes.pop("is_default", False):
                for existing in db_session.scalars(select(ProviderModel)):
                    existing.is_default = existing.id == model.id
            if "options" in changes:
                changes["options_json"] = changes.pop("options")
            for key, value in changes.items():
                setattr(model, key, value)
            model.revision += 1
            db_session.flush()
            result = _model_dict(model, ("id", "provider_id", "model_id", "is_default", "default_temperature", "default_reasoning_effort", "input_cost_per_million", "cached_input_cost_per_million", "output_cost_per_million", "options_json", "revision"))
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
                replacement = db_session.get(ProviderModel, replacement_record_id) if replacement_record_id else None
                if replacement is None and replacement_model_id:
                    replacement = db_session.scalar(select(ProviderModel).where(ProviderModel.provider_id == provider_id, ProviderModel.model_id == replacement_model_id))
                if replacement is None or replacement.id == model.id:
                    return error_response("replacement_required", "Select a replacement before deleting the active default model.", 409)
                replacement.is_default = True
            db_session.delete(model)
        return "", 204

    @app.post("/api/v1/providers/<provider_id>/models/refresh")
    @require_auth
    def model_refresh(provider_id: str):
        from pandrator.logic.llm_handler import _detect_models_for_builtin_provider

        with database.session() as db_session:
            provider = db_session.get(Provider, provider_id)
            if provider is None:
                return error_response("not_found", "Provider not found.", 404)
            existing = list(db_session.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider_id)).all())
            detected = _detect_models_for_builtin_provider({
                "provider": provider.provider_key,
                "api_base": provider.base_url,
                "api_key_env": str((provider.options_json or {}).get("api_key_env") or ""),
                "models": [item.model_id for item in existing],
            })
            known = {item.model_id for item in existing}
            added = []
            for model_id in detected:
                if model_id in known:
                    continue
                model = ProviderModel(provider_id=provider_id, model_id=model_id, is_default=not existing and not added)
                db_session.add(model)
                added.append(model_id)
            return jsonify({"detected": detected, "added": added, "preserved": sorted(known)})

    @app.get("/api/v1/voices")
    @require_auth
    def voice_list():
        with database.session() as db_session:
            records = list(db_session.scalars(select(Voice).order_by(Voice.name)).all())
            return jsonify({"items": [_model_dict(item, ("id", "name", "language", "description", "rvc_model_ref", "metadata_json", "revision")) for item in records]})

    @app.post("/api/v1/voices")
    @require_auth
    def voice_create():
        payload = VoiceCreate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            if db_session.scalar(select(Voice).where(Voice.name == payload.name)) is not None:
                return error_response("already_exists", "A voice with that name already exists.", 409)
            voice = Voice(name=payload.name, language=payload.language, description=payload.description)
            db_session.add(voice)
            db_session.flush()
            result = _model_dict(voice, ("id", "name", "language", "description", "rvc_model_ref", "metadata_json", "revision"))
        return jsonify(result), 201

    @app.get("/api/v1/voices/<voice_id>/samples")
    @require_auth
    def voice_sample_list(voice_id: str):
        with database.session() as db_session:
            if db_session.get(Voice, voice_id) is None:
                return error_response("not_found", "Voice not found.", 404)
            records = list(db_session.scalars(select(VoiceSample).where(VoiceSample.voice_id == voice_id).order_by(VoiceSample.created_at.desc())).all())
            return jsonify({"items": [_model_dict(item, ("id", "voice_id", "artifact_id", "transcript", "transcript_language", "transcript_reviewed", "created_at")) for item in records]})

    @app.post("/api/v1/voices/<voice_id>/samples")
    @require_auth
    def voice_sample_upload(voice_id: str):
        incoming = request.files.get("file")
        if incoming is None or not incoming.filename:
            return error_response("missing_file", "An audio recording is required.", 400)
        with database.session() as db_session:
            if db_session.get(Voice, voice_id) is None:
                return error_response("not_found", "Voice not found.", 404)
        suffix = Path(secure_filename(incoming.filename)).suffix or ".webm"
        temporary = paths.temporary / f"voice-{uuid.uuid4()}{suffix}"
        incoming.save(temporary)
        source_artifact = artifacts.register(temporary, kind="audio", role="recording_upload")
        job = jobs.enqueue(
            "voice.normalize_recording",
            {"voice_id": voice_id, "source_artifact_id": source_artifact.id, "ffmpeg_executable": shutil.which("ffmpeg") or "ffmpeg"},
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
            {"voice_id": voice_id, "sample_id": sample_id, "sample_artifact_id": artifact_id, "settings": settings},
            resource_keys=[f"stt:{str(settings.get('stt_compute_backend') or 'auto')}"] + ([f"gpu:{str(settings.get('stt_compute_backend'))}"] if str(settings.get("stt_compute_backend") or "").lower() in {"cuda", "vulkan", "metal"} else []),
        )
        return jsonify(_job_payload(job)), 202

    @app.patch("/api/v1/voices/<voice_id>/samples/<sample_id>/transcript")
    @require_auth
    def voice_sample_transcript(voice_id: str, sample_id: str):
        payload = VoiceTranscriptReview.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            sample = db_session.get(VoiceSample, sample_id)
            if sample is None or sample.voice_id != voice_id:
                return error_response("not_found", "Voice sample not found.", 404)
            sample.transcript = payload.transcript
            sample.transcript_language = payload.language
            sample.transcript_reviewed = True
            result = _model_dict(sample, ("id", "voice_id", "artifact_id", "transcript", "transcript_language", "transcript_reviewed", "created_at"))
        return jsonify(result)

    @app.get("/api/v1/rvc/models")
    @require_auth
    def rvc_model_list():
        from pandrator.logic import rvc_handler

        available = rvc_handler.is_rvc_available()
        return jsonify({"available": available, "items": rvc_handler.get_rvc_models(str(paths.models / "rvc")) if available else []})

    @app.post("/api/v1/rvc/models")
    @require_auth
    def rvc_model_upload():
        payload = RvcModelUploadRequest.model_validate(request.get_json(silent=True) or {})
        for artifact_id in (payload.pth_artifact_id, payload.index_artifact_id):
            try:
                _record, source = artifacts.resolve(artifact_id)
            except KeyError:
                return error_response("not_found", "An RVC upload artifact was not found.", 404)
            if not source.is_file():
                return error_response("artifact_missing", "An RVC upload artifact is missing.", 410)
        job = jobs.enqueue("rvc.model.upload", payload.model_dump())
        return jsonify(_job_payload(job)), 202

    @app.post("/api/v1/rvc/convert")
    @require_auth
    def rvc_convert():
        payload = RvcConvertRequest.model_validate(request.get_json(silent=True) or {})
        try:
            artifacts.resolve(payload.source_artifact_id)
            if payload.session_id:
                sessions.get(payload.session_id)
        except KeyError:
            return error_response("not_found", "The requested session or source artifact was not found.", 404)
        job = jobs.enqueue("rvc.convert", payload.model_dump(), session_id=payload.session_id, resource_keys=["service:rvc", "gpu:default"])
        return jsonify(_job_payload(job)), 202

    @app.get("/api/v1/training")
    @require_auth
    def training_list():
        with database.session() as db_session:
            records = list(db_session.scalars(select(TrainingRun).order_by(TrainingRun.created_at.desc()).limit(200)).all())
            for record in records:
                job = db_session.get(Job, record.job_id) if record.job_id else None
                if record.status in {"queued", "running", "cancel_requested"} and job is not None and job.status in {"failed", "canceled", "interrupted"}:
                    record.status = job.status
                    record.error_message = job.error_message
                    record.updated_at = utcnow()
            return jsonify({"items": [_model_dict(item, ("id", "kind", "voice_id", "job_id", "source_artifact_id", "source_text_artifact_id", "output_artifact_id", "model_name", "status", "settings_json", "error_message", "created_at", "updated_at")) for item in records]})

    @app.post("/api/v1/training")
    @require_auth
    def training_create():
        payload = TrainingCreateRequest.model_validate(request.get_json(silent=True) or {})
        try:
            artifacts.resolve(payload.source_artifact_id)
            if payload.source_text_artifact_id:
                artifacts.resolve(payload.source_text_artifact_id)
        except KeyError:
            return error_response("not_found", "A training source artifact was not found.", 404)
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
                return error_response("training_active", "Only failed, canceled, or interrupted training can be retried.", 409)
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
        return jsonify({"id": training_id, "job_id": job_id, "status": "cancel_requested"}), 202

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
            return Response("Pandrator web assets have not been built. Run the frontend build first.", status=503, mimetype="text/plain")
        response = send_file(index)
        response.headers["Cache-Control"] = "no-store"
        return response

    return app
