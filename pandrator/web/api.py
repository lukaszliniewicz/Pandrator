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
from .models import AppSetting, AppSettingHistory, Artifact, Provider, ProviderModel, SessionRecord, SourceRecord, Voice, VoiceSample, utcnow
from .openapi import build_openapi_document
from .schemas import BootstrapRequest, JobCreate, LoginRequest, ModelCreate, ModelUpdate, PdfEditRequest, ProviderCreate, SessionCreate, SessionUpdate, SettingUpdate, SubtitleReviewRequest, TokenCreateRequest, VoiceCreate, VoiceTranscriptReview
from .sessions import RevisionConflict, SessionService
from .subtitle_review import SubtitleReviewService
from .workflows import WorkflowService


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
        ("id", "name", "storage_key", "workflow_kind", "workflow_preset", "included_stages_json", "status", "revision", "created_at", "updated_at"),
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
    auth = AuthService(database)
    jobs = JobQueue(database)
    sessions = SessionService(database)
    artifacts = ArtifactService(database, paths)
    workflows = WorkflowService(database, jobs)

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
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Permissions-Policy"] = "camera=(), geolocation=(), microphone=(self)"
        response.headers["Content-Security-Policy"] = f"default-src 'self'; img-src 'self' data: blob:; media-src 'self' blob:; connect-src 'self'; style-src 'self' 'unsafe-inline'; script-src {frontend_script_policy}"
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

    @app.get("/api/v1/sessions")
    @require_auth
    def session_list():
        return jsonify({"items": [_session_payload(item) for item in sessions.list()]})

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
        changes = payload.model_dump(exclude_none=True)
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
        if requested_session_id:
            try:
                sessions.get(requested_session_id)
            except KeyError:
                return error_response("not_found", "Session not found.", 404)
        try:
            incoming.save(temporary)
            digest = sha256_file(temporary)
            os.replace(temporary, destination)
            artifact = artifacts.register(destination, kind="source", role="upload", session_id=requested_session_id, calculate_hash=False, metadata={"original_filename": incoming.filename})
            with database.session() as db_session:
                managed = db_session.get(Artifact, artifact.id)
                managed.content_hash = digest
                if requested_session_id:
                    db_session.add(
                        SourceRecord(
                            session_id=requested_session_id,
                            kind=Path(filename).suffix.lower().lstrip(".") or "file",
                            display_name=incoming.filename,
                            artifact_id=artifact.id,
                            content_hash=digest,
                        )
                    )
            return jsonify({"artifact_id": artifact.id, "filename": filename, "size_bytes": destination.stat().st_size, "sha256": digest}), 201
        finally:
            if temporary.exists():
                temporary.unlink()

    @app.get("/api/v1/artifacts")
    @require_auth
    def artifact_list():
        with database.session() as db_session:
            records = list(db_session.scalars(select(Artifact).order_by(Artifact.created_at.desc()).limit(500)).all())
            return jsonify({"items": [_model_dict(item, ("id", "session_id", "kind", "role", "relative_path", "mime_type", "size_bytes", "content_hash", "state", "created_at")) for item in records]})

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

    @app.post("/api/v1/providers")
    @require_auth
    def provider_create():
        payload = ProviderCreate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            provider = Provider(kind=payload.kind, provider_key=payload.provider_key, label=payload.label, base_url=payload.base_url, secret_ref=payload.secret_ref, options_json=payload.options)
            db_session.add(provider)
            db_session.flush()
            result = _model_dict(provider, ("id", "kind", "provider_key", "label", "enabled", "base_url", "secret_ref", "options_json", "revision"))
        return jsonify(result), 201

    @app.post("/api/v1/providers/<provider_id>/models")
    @require_auth
    def model_create(provider_id: str):
        payload = ModelCreate.model_validate(request.get_json(silent=True) or {})
        with database.session() as db_session:
            if db_session.get(Provider, provider_id) is None:
                return error_response("not_found", "Provider not found.", 404)
            if payload.is_default:
                for existing in db_session.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider_id)):
                    existing.is_default = False
            model = ProviderModel(provider_id=provider_id, model_id=payload.model_id, is_default=payload.is_default, default_temperature=payload.default_temperature, default_reasoning_effort=payload.default_reasoning_effort, input_cost_per_million=payload.input_cost_per_million, cached_input_cost_per_million=payload.cached_input_cost_per_million, output_cost_per_million=payload.output_cost_per_million, options_json=payload.options)
            db_session.add(model)
            db_session.flush()
            result = _model_dict(model, ("id", "provider_id", "model_id", "is_default", "default_temperature", "default_reasoning_effort", "input_cost_per_million", "cached_input_cost_per_million", "output_cost_per_million", "options_json", "revision"))
        return jsonify(result), 201

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
                for existing in db_session.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider_id)):
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
        replacement_id = str((request.get_json(silent=True) or {}).get("replacement_model_id") or "")
        with database.session() as db_session:
            model = db_session.get(ProviderModel, model_record_id)
            if model is None or model.provider_id != provider_id:
                return error_response("not_found", "Model not found.", 404)
            if model.is_default:
                replacement = db_session.scalar(select(ProviderModel).where(ProviderModel.provider_id == provider_id, ProviderModel.model_id == replacement_id)) if replacement_id else None
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
