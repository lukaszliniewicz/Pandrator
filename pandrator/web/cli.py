"""Pandrator browser-server, worker, and automation CLI."""

from __future__ import annotations

import argparse
import json
import os
import socket
import shutil
import sys
import uuid
import webbrowser
from pathlib import Path
from typing import Any

import requests

from waitress import serve as waitress_serve
from sqlalchemy import select

from pandrator.runtime import DataPaths

from .api import create_app
from .artifacts import ArtifactService, sha256_file
from .auth import AuthService, BootstrapTokenStore
from .bundles import SessionBundleService
from .database import Database
from .jobs import JobQueue, Worker, noop_handler
from .legacy_migration import import_legacy_data
from .models import Artifact, Provider, ProviderModel, SessionRecord, SourceRecord, TrainingRun, Voice, VoiceSample, new_id, utcnow
from .openapi import build_openapi_document
from .sessions import SessionService
from .subtitle_review import SubtitleReviewService
from .workflow_handlers import WorkflowHandlers
from .workflows import WorkflowService


def _emit(value: Any, json_output: bool = False) -> None:
    if json_output:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))
    elif isinstance(value, str):
        print(value)
    elif isinstance(value, list):
        for item in value:
            print(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, default=str))
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False, default=str))


def _paths(args) -> DataPaths:
    return DataPaths.from_value(getattr(args, "data_dir", None)).ensure()


def _database(args) -> tuple[DataPaths, Database]:
    paths = _paths(args)
    lock_path = paths.instance_lock
    if lock_path.is_file() and getattr(args, "command", "") not in {"serve", "worker"}:
        try:
            lock = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            lock = {}
        expected = str(os.environ.get("PANDRATOR_SUPERVISOR_INSTANCE") or "")
        owner = str(lock.get("instance_id") or "")
        pid = int(lock.get("pid") or 0)
        try:
            import psutil
            alive = pid > 0 and psutil.pid_exists(pid)
        except ImportError:
            alive = pid > 0
        if alive and (not expected or expected != owner):
            raise RuntimeError(f"Data root is owned by running Pandrator supervisor PID {pid}; use --server instead of standalone access.")
    import_legacy_data(paths)
    return paths, Database(paths.database)


def _remote_api(args, method: str, path: str, *, payload: Any = None, files: Any = None) -> Any:
    base = str(args.server or "").rstrip("/")
    token = str(args.token or os.environ.get("PANDRATOR_API_TOKEN") or "").strip()
    if not token:
        raise RuntimeError("API-client mode requires --token or PANDRATOR_API_TOKEN.")
    headers = {"Authorization": f"Bearer {token}", "X-Request-ID": str(uuid.uuid4())}
    try:
        response = requests.request(method, f"{base}/api/v1{path}", headers=headers, json=payload if files is None else None, files=files, timeout=300)
    except requests.RequestException as error:
        raise RuntimeError(f"Could not reach Pandrator API: {error}") from error
    if not response.ok:
        body = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
        message = body.get("error", {}).get("message") if isinstance(body, dict) else None
        raise RuntimeError(message or f"Pandrator API returned HTTP {response.status_code}.")
    if response.status_code == 204:
        return None
    return response.json()


def _remote_upload(args, source: str) -> str:
    path = Path(source).expanduser().resolve(strict=True)
    with path.open("rb") as handle:
        result = _remote_api(args, "POST", "/uploads", files={"file": (path.name, handle)})
    return str(result["artifact_id"])


def command_remote(args) -> int:
    """Dispatch supported operational commands through the versioned HTTP API."""

    command = args.command
    if command == "session":
        if args.session_command == "list":
            result = _remote_api(args, "GET", "/sessions")
        elif args.session_command == "show":
            result = _remote_api(args, "GET", f"/sessions/{args.session_id}")
        elif args.session_command == "create":
            result = _remote_api(args, "POST", "/sessions", payload={"name": args.name, "workflow_kind": args.kind, "source_language": args.source_language, "target_language": args.target_language, "workflow_preset": args.preset, "included_stages": args.include or []})
        else:
            raise RuntimeError("Remote session bundle transfer is not supported by this command; use managed artifact upload/download endpoints.")
    elif command == "job":
        if args.job_command == "list":
            result = _remote_api(args, "GET", f"/jobs?limit={args.limit}")
        elif args.job_command == "show":
            result = _remote_api(args, "GET", f"/jobs/{args.job_id}")
        elif args.job_command == "cancel":
            result = _remote_api(args, "POST", f"/jobs/{args.job_id}/cancel")
        else:
            result = _remote_api(args, "POST", "/jobs", payload={"kind": args.kind, "session_id": args.session_id, "payload": json.loads(args.payload), "max_attempts": args.max_attempts})
    elif command == "source":
        snapshot = _remote_api(args, "GET", f"/sessions/{args.session_id}/workflow")
        result = {"items": snapshot.get("sources", [])}
    elif command == "workflow":
        result = _remote_api(args, "GET", f"/sessions/{args.session_id}/workflow") if args.workflow_command == "show" else _remote_api(args, "POST", f"/sessions/{args.session_id}/stages/{args.stage}/run", payload=json.loads(args.settings))
    elif command == "artifact":
        query = f"?session_id={args.session_id}" if args.session_id else ""
        result = _remote_api(args, "GET", f"/artifacts{query}")
    elif command == "document":
        result = _remote_api(args, "GET", f"/sessions/{args.session_id}/subtitles")
    elif command == "provider":
        result = _remote_api(args, "GET", "/providers")
    elif command == "voice":
        result = _remote_api(args, "GET", "/voices")
    elif command == "export":
        result = _remote_api(args, "POST", f"/sessions/{args.session_id}/stages/export/run", payload=json.loads(args.options))
    elif command == "rvc":
        if args.rvc_command == "list":
            result = _remote_api(args, "GET", "/rvc/models")
        elif args.rvc_command == "upload":
            result = _remote_api(args, "POST", "/rvc/models", payload={"pth_artifact_id": _remote_upload(args, args.weights), "index_artifact_id": _remote_upload(args, args.index)})
        else:
            settings = json.loads(args.settings); settings["rvc_model"] = args.model
            result = _remote_api(args, "POST", "/rvc/convert", payload={"source_artifact_id": args.artifact_id, "session_id": args.session_id, "settings": settings})
    elif command == "training":
        if args.training_command == "list":
            result = _remote_api(args, "GET", "/training")
        elif args.training_command == "cancel":
            result = _remote_api(args, "POST", f"/training/{args.training_id}/cancel")
        else:
            result = _remote_api(args, "POST", "/training", payload={"model_name": args.model_name, "source_artifact_id": args.artifact_id, "source_text_artifact_id": args.text_artifact_id, "settings": json.loads(args.settings)})
    elif command == "openapi":
        result = _remote_api(args, "GET", "/openapi.json")
    else:
        raise RuntimeError(f"'{command}' is a standalone or server-management command and cannot use --server.")
    _emit(result, args.json)
    return 0


def _session_dict(record) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "workflow_kind": record.workflow_kind,
        "source_language": record.source_language,
        "target_language": record.target_language,
        "workflow_preset": record.workflow_preset,
        "status": record.status,
        "revision": record.revision,
    }


def _job_dict(job) -> dict:
    return {
        "id": job.id,
        "kind": job.kind,
        "session_id": job.session_id,
        "status": job.status,
        "progress": job.progress,
        "attempts": job.attempts,
        "error": job.error_message,
        "result": job.result_json,
    }


def command_migrate(args) -> int:
    result = import_legacy_data(_paths(args))
    _emit(result, args.json)
    return 0


def command_serve(args) -> int:
    paths, database = _database(args)
    auth = AuthService(database)
    database.dispose()
    remote = args.host not in {"127.0.0.1", "localhost", "::1"}
    if remote and not auth.initialized():
        print("Remote mode requires an initialized owner password. Run 'pandrator auth init' first.", file=sys.stderr)
        return 3
    if remote and not args.allow_insecure_remote:
        print("Refusing non-loopback HTTP without --allow-insecure-remote. Put Pandrator behind an HTTPS reverse proxy.", file=sys.stderr)
        return 3
    if remote and not args.trusted_host:
        print("Remote mode requires at least one --trusted-host value.", file=sys.stderr)
        return 3

    bootstrap = BootstrapTokenStore()
    initial = str(os.environ.get("PANDRATOR_BOOTSTRAP_TOKEN") or "").strip()
    if initial:
        bootstrap.add(initial)
    app = create_app(
        data_root=paths.root,
        trusted_hosts=args.trusted_host or None,
        proxy_hops=args.proxy_hops,
        secure_cookies=remote and not args.allow_insecure_remote,
        bootstrap_tokens=bootstrap,
    )
    url_host = "127.0.0.1" if args.host in {"0.0.0.0", "::"} else args.host
    if args.open_browser:
        token = bootstrap.issue()
        webbrowser.open(f"http://{url_host}:{args.port}/#bootstrap={token}")
    print(f"Pandrator API listening on http://{args.host}:{args.port}")
    waitress_serve(app, host=args.host, port=args.port, threads=max(6, args.threads), url_scheme="http")
    return 0


def command_worker(args) -> int:
    paths, database = _database(args)
    queue = JobQueue(database)
    artifacts = ArtifactService(database, paths)

    def hash_artifact(payload, progress, cancel_event):
        artifact_id = str(payload.get("artifact_id") or "")
        artifact, path = artifacts.resolve(artifact_id)
        progress(0.1, "Reading artifact")
        digest = sha256_file(path)
        if cancel_event.is_set():
            return {}
        with database.session() as session:
            managed = session.get(type(artifact), artifact.id)
            managed.content_hash = digest
        progress(1.0, "Artifact hash updated")
        return {"artifact_id": artifact_id, "sha256": digest}

    worker = Worker(
        queue,
        args.worker_id or f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}",
        {"noop": noop_handler, "artifact.hash": hash_artifact, **WorkflowHandlers(database, paths).handlers()},
    )
    try:
        if args.once:
            worker.run_once()
        else:
            worker.run_forever(args.poll_interval)
    except KeyboardInterrupt:
        worker.stop()
    finally:
        database.dispose()
    return 0


def command_auth_init(args) -> int:
    _, database = _database(args)
    password = args.password or os.environ.get("PANDRATOR_OWNER_PASSWORD")
    if not password:
        import getpass

        password = getpass.getpass("Owner password: ")
        confirmation = getpass.getpass("Confirm password: ")
        if password != confirmation:
            print("Passwords do not match.", file=sys.stderr)
            return 2
    AuthService(database).initialize_owner(password, replace=args.replace)
    database.dispose()
    _emit({"initialized": True}, args.json)
    return 0


def command_auth_token_create(args) -> int:
    _, database = _database(args)
    record, raw = AuthService(database).create_api_token(args.label)
    database.dispose()
    _emit({"id": record.id, "label": record.label, "token": raw}, args.json)
    return 0


def command_auth_token_list(args) -> int:
    _, database = _database(args)
    records = AuthService(database).list_tokens()
    database.dispose()
    _emit(
        [
            {
                "id": item.id,
                "label": item.label,
                "prefix": item.token_prefix,
                "created_at": item.created_at,
                "revoked_at": item.revoked_at,
            }
            for item in records
        ],
        args.json,
    )
    return 0


def command_auth_token_revoke(args) -> int:
    _, database = _database(args)
    AuthService(database).revoke_token(args.token_id)
    database.dispose()
    _emit({"revoked": args.token_id}, args.json)
    return 0


def command_session_list(args) -> int:
    _, database = _database(args)
    records = SessionService(database).list(include_trashed=args.include_trashed)
    database.dispose()
    _emit([_session_dict(record) for record in records], args.json)
    return 0


def command_session_create(args) -> int:
    paths, database = _database(args)
    record = SessionService(database).create(
        args.name,
        workflow_kind=args.kind,
        source_language=args.source_language,
        target_language=args.target_language,
        workflow_preset=args.preset,
        included_stages=args.include or [],
    )
    (paths.sessions / record.storage_key).mkdir(parents=True, exist_ok=False)
    database.dispose()
    _emit(_session_dict(record), args.json)
    return 0


def command_session_show(args) -> int:
    _, database = _database(args)
    record = SessionService(database).get(args.session_id)
    database.dispose()
    _emit(_session_dict(record), args.json)
    return 0


def command_session_export(args) -> int:
    paths, database = _database(args)
    try:
        destination = Path(args.output).expanduser().resolve()
        result = SessionBundleService(database, paths).export_bundle(args.session_id, destination, include_sources=not args.exclude_sources)
        _emit(result, args.json)
        return 0
    finally:
        database.dispose()


def command_session_import(args) -> int:
    paths, database = _database(args)
    try:
        result = SessionBundleService(database, paths).import_bundle(Path(args.bundle), name=args.name)
        _emit(result, args.json)
        return 0
    finally:
        database.dispose()


def command_job_list(args) -> int:
    _, database = _database(args)
    records = JobQueue(database).list(args.limit)
    database.dispose()
    _emit([_job_dict(record) for record in records], args.json)
    return 0


def command_job_enqueue(args) -> int:
    _, database = _database(args)
    try:
        payload = json.loads(args.payload)
    except json.JSONDecodeError as error:
        print(f"Invalid --payload JSON: {error}", file=sys.stderr)
        return 2
    record = JobQueue(database).enqueue(args.kind, payload, session_id=args.session_id, max_attempts=args.max_attempts)
    database.dispose()
    _emit(_job_dict(record), args.json)
    return 0


def command_job_show(args) -> int:
    _, database = _database(args)
    record = JobQueue(database).get(args.job_id)
    database.dispose()
    _emit(_job_dict(record), args.json)
    return 0


def command_job_cancel(args) -> int:
    _, database = _database(args)
    record = JobQueue(database).request_cancel(args.job_id)
    database.dispose()
    _emit(_job_dict(record), args.json)
    return 0


def command_source_list(args) -> int:
    _, database = _database(args)
    try:
        with database.session() as session:
            records = list(session.scalars(select(SourceRecord).where(SourceRecord.session_id == args.session_id).order_by(SourceRecord.created_at)).all())
            _emit([{"id": item.id, "kind": item.kind, "name": item.display_name, "artifact_id": item.artifact_id, "content_hash": item.content_hash} for item in records], args.json)
        return 0
    finally:
        database.dispose()


def command_workflow_show(args) -> int:
    _, database = _database(args)
    try:
        _emit(WorkflowService(database, JobQueue(database)).snapshot(args.session_id), args.json)
        return 0
    finally:
        database.dispose()


def command_workflow_run(args) -> int:
    _, database = _database(args)
    try:
        settings = json.loads(args.settings)
        job = WorkflowService(database, JobQueue(database)).run_stage(args.session_id, args.stage, settings)
        _emit(_job_dict(job), args.json)
        return 0
    finally:
        database.dispose()


def command_artifact_list(args) -> int:
    _, database = _database(args)
    try:
        with database.session() as session:
            statement = select(Artifact).order_by(Artifact.created_at.desc()).limit(args.limit)
            if args.session_id:
                statement = statement.where(Artifact.session_id == args.session_id)
            records = list(session.scalars(statement).all())
            _emit([{"id": item.id, "session_id": item.session_id, "role": item.role, "kind": item.kind, "path": item.relative_path, "state": item.state, "sha256": item.content_hash} for item in records], args.json)
        return 0
    finally:
        database.dispose()


def command_document_show(args) -> int:
    paths, database = _database(args)
    try:
        def session_dir(session_id):
            with database.session() as session:
                record = session.get(SessionRecord, session_id)
                if record is None:
                    raise KeyError(session_id)
                return paths.sessions / record.storage_key
        review = SubtitleReviewService(database, ArtifactService(database, paths), session_dir)
        _emit(review.documents(args.session_id), args.json)
        return 0
    finally:
        database.dispose()


def command_provider_list(args) -> int:
    _, database = _database(args)
    try:
        with database.session() as session:
            providers = list(session.scalars(select(Provider).order_by(Provider.label)).all())
            payload = []
            for provider in providers:
                models = list(session.scalars(select(ProviderModel).where(ProviderModel.provider_id == provider.id).order_by(ProviderModel.model_id)).all())
                payload.append({"id": provider.id, "key": provider.provider_key, "label": provider.label, "base_url": provider.base_url, "models": [{"id": item.model_id, "active": item.is_active, "default": item.is_default, "temperature": item.default_temperature, "reasoning": item.default_reasoning_effort} for item in models]})
            _emit(payload, args.json)
        return 0
    finally:
        database.dispose()


def command_voice_list(args) -> int:
    _, database = _database(args)
    try:
        with database.session() as session:
            voices = list(session.scalars(select(Voice).order_by(Voice.name)).all())
            payload = []
            for voice in voices:
                samples = list(session.scalars(select(VoiceSample).where(VoiceSample.voice_id == voice.id)).all())
                payload.append({"id": voice.id, "name": voice.name, "language": voice.language, "samples": len(samples), "reviewed_transcripts": sum(item.transcript_reviewed for item in samples)})
            _emit(payload, args.json)
        return 0
    finally:
        database.dispose()


def command_rvc_list(args) -> int:
    from pandrator.logic import rvc_handler

    paths = _paths(args)
    available = rvc_handler.is_rvc_available()
    _emit({"available": available, "items": rvc_handler.get_rvc_models(str(paths.models / "rvc")) if available else []}, args.json)
    return 0


def _import_cli_artifact(paths: DataPaths, database: Database, source: str, role: str) -> Artifact:
    source_path = Path(source).expanduser().resolve(strict=True)
    destination = paths.uploads / f"{uuid.uuid4()}-{source_path.name}"
    shutil.copy2(source_path, destination)
    return ArtifactService(database, paths).register(destination, kind="source", role=role, metadata={"original_filename": source_path.name})


def command_rvc_upload(args) -> int:
    paths, database = _database(args)
    try:
        weights = _import_cli_artifact(paths, database, args.weights, "rvc_upload")
        index = _import_cli_artifact(paths, database, args.index, "rvc_upload")
        job = JobQueue(database).enqueue("rvc.model.upload", {"pth_artifact_id": weights.id, "index_artifact_id": index.id})
        _emit(_job_dict(job), args.json)
        return 0
    finally:
        database.dispose()


def command_rvc_convert(args) -> int:
    _, database = _database(args)
    try:
        settings = json.loads(args.settings)
        settings["rvc_model"] = args.model
        job = JobQueue(database).enqueue("rvc.convert", {"source_artifact_id": args.artifact_id, "session_id": args.session_id, "settings": settings}, session_id=args.session_id)
        _emit(_job_dict(job), args.json)
        return 0
    finally:
        database.dispose()


def command_training_list(args) -> int:
    _, database = _database(args)
    try:
        with database.session() as session:
            records = list(session.scalars(select(TrainingRun).order_by(TrainingRun.created_at.desc())).all())
            _emit([{"id": item.id, "model_name": item.model_name, "status": item.status, "job_id": item.job_id, "source_artifact_id": item.source_artifact_id, "output_artifact_id": item.output_artifact_id, "error": item.error_message} for item in records], args.json)
        return 0
    finally:
        database.dispose()


def command_training_start(args) -> int:
    _, database = _database(args)
    try:
        settings = json.loads(args.settings)
        training_id = new_id()
        with database.session() as session:
            if session.get(Artifact, args.artifact_id) is None:
                raise KeyError(args.artifact_id)
            if args.text_artifact_id and session.get(Artifact, args.text_artifact_id) is None:
                raise KeyError(args.text_artifact_id)
            session.add(TrainingRun(id=training_id, model_name=args.model_name, source_artifact_id=args.artifact_id, settings_json=settings))
        job = JobQueue(database).enqueue("training.xtts", {"training_id": training_id, "model_name": args.model_name, "source_artifact_id": args.artifact_id, "source_text_artifact_id": args.text_artifact_id, "settings": settings})
        with database.session() as session:
            record = session.get(TrainingRun, training_id)
            record.job_id = job.id
            record.updated_at = utcnow()
        payload = _job_dict(job); payload["training_id"] = training_id
        _emit(payload, args.json)
        return 0
    finally:
        database.dispose()


def command_training_cancel(args) -> int:
    _, database = _database(args)
    try:
        with database.session() as session:
            record = session.get(TrainingRun, args.training_id)
            if record is None:
                raise KeyError(args.training_id)
            job_id = record.job_id
            record.status = "cancel_requested"
            record.updated_at = utcnow()
        if job_id:
            JobQueue(database).request_cancel(job_id)
        _emit({"id": args.training_id, "job_id": job_id, "status": "cancel_requested"}, args.json)
        return 0
    finally:
        database.dispose()


def command_export_create(args) -> int:
    _, database = _database(args)
    try:
        job = WorkflowService(database, JobQueue(database)).run_stage(args.session_id, "export", json.loads(args.options))
        _emit(_job_dict(job), args.json)
        return 0
    finally:
        database.dispose()


def command_doctor(args) -> int:
    paths, database = _database(args)
    reports = ArtifactService(database, paths).reconcile()
    database.dispose()
    result = {"ok": not reports, "data_root": str(paths.root), "artifact_issues": reports}
    _emit(result, args.json)
    return 0 if not reports else 4


def command_openapi(args) -> int:
    payload = build_openapi_document()
    if args.output:
        Path(args.output).write_text(json.dumps(payload, indent=2), encoding="utf-8")
        _emit({"written": str(Path(args.output).resolve())}, args.json)
    else:
        _emit(payload, True)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pandrator", description="Pandrator browser server and automation CLI")
    parser.add_argument("--data-dir", help="Pandrator data root (or PANDRATOR_DATA_DIR).")
    parser.add_argument("--server", help="Use a running Pandrator API instead of standalone data-root access.")
    parser.add_argument("--token", help="API token for --server (or PANDRATOR_API_TOKEN).")
    parser.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON.")
    commands = parser.add_subparsers(dest="command", required=True)

    migrate = commands.add_parser("migrate", help="Import legacy Qt metadata into the web database.")
    migrate.set_defaults(handler=command_migrate)

    serve = commands.add_parser("serve", help="Run the Flask API and Svelte application with Waitress.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8097)
    serve.add_argument("--threads", type=int, default=12)
    serve.add_argument("--trusted-host", action="append", default=[])
    serve.add_argument("--proxy-hops", type=int, choices=range(0, 4), default=0, help="Trust this many explicitly configured reverse proxies.")
    serve.add_argument("--allow-insecure-remote", action="store_true")
    serve.add_argument("--open-browser", action=argparse.BooleanOptionalAction, default=True)
    serve.set_defaults(handler=command_serve)

    worker = commands.add_parser("worker", help="Run the durable job worker.")
    worker.add_argument("--worker-id")
    worker.add_argument("--once", action="store_true")
    worker.add_argument("--poll-interval", type=float, default=0.5)
    worker.set_defaults(handler=command_worker)

    auth = commands.add_parser("auth", help="Initialize the owner and manage API tokens.")
    auth_commands = auth.add_subparsers(dest="auth_command", required=True)
    auth_init = auth_commands.add_parser("init")
    auth_init.add_argument("--password")
    auth_init.add_argument("--replace", action="store_true")
    auth_init.set_defaults(handler=command_auth_init)
    token = auth_commands.add_parser("token")
    token_commands = token.add_subparsers(dest="token_command", required=True)
    token_create = token_commands.add_parser("create")
    token_create.add_argument("--label", default="CLI token")
    token_create.set_defaults(handler=command_auth_token_create)
    token_list = token_commands.add_parser("list")
    token_list.set_defaults(handler=command_auth_token_list)
    token_revoke = token_commands.add_parser("revoke")
    token_revoke.add_argument("token_id")
    token_revoke.set_defaults(handler=command_auth_token_revoke)

    session = commands.add_parser("session", help="Manage sessions.")
    session_commands = session.add_subparsers(dest="session_command", required=True)
    session_list = session_commands.add_parser("list")
    session_list.add_argument("--include-trashed", action="store_true")
    session_list.set_defaults(handler=command_session_list)
    session_create = session_commands.add_parser("create")
    session_create.add_argument("name")
    session_create.add_argument("--kind", choices=["audiobook", "subtitles", "voiceover"], default="audiobook")
    session_create.add_argument("--source-language", default="auto")
    session_create.add_argument("--target-language")
    session_create.add_argument("--preset", default="custom")
    session_create.add_argument("--include", action="append")
    session_create.set_defaults(handler=command_session_create)
    session_show = session_commands.add_parser("show")
    session_show.add_argument("session_id")
    session_show.set_defaults(handler=command_session_show)
    session_export = session_commands.add_parser("export")
    session_export.add_argument("session_id")
    session_export.add_argument("output")
    session_export.add_argument("--exclude-sources", action="store_true")
    session_export.set_defaults(handler=command_session_export)
    session_import = session_commands.add_parser("import")
    session_import.add_argument("bundle")
    session_import.add_argument("--name")
    session_import.set_defaults(handler=command_session_import)

    job = commands.add_parser("job", help="Inspect and control durable jobs.")
    job_commands = job.add_subparsers(dest="job_command", required=True)
    job_list = job_commands.add_parser("list")
    job_list.add_argument("--limit", type=int, default=100)
    job_list.set_defaults(handler=command_job_list)
    job_enqueue = job_commands.add_parser("enqueue")
    job_enqueue.add_argument("kind")
    job_enqueue.add_argument("--payload", default="{}")
    job_enqueue.add_argument("--session-id")
    job_enqueue.add_argument("--max-attempts", type=int, default=1)
    job_enqueue.set_defaults(handler=command_job_enqueue)
    job_show = job_commands.add_parser("show")
    job_show.add_argument("job_id")
    job_show.set_defaults(handler=command_job_show)
    job_cancel = job_commands.add_parser("cancel")
    job_cancel.add_argument("job_id")
    job_cancel.set_defaults(handler=command_job_cancel)

    source = commands.add_parser("source", help="Inspect imported session sources.")
    source_commands = source.add_subparsers(dest="source_command", required=True)
    source_list = source_commands.add_parser("list")
    source_list.add_argument("session_id")
    source_list.set_defaults(handler=command_source_list)

    workflow = commands.add_parser("workflow", help="Inspect and run workflow stages.")
    workflow_commands = workflow.add_subparsers(dest="workflow_command", required=True)
    workflow_show = workflow_commands.add_parser("show")
    workflow_show.add_argument("session_id")
    workflow_show.set_defaults(handler=command_workflow_show)
    workflow_run = workflow_commands.add_parser("run")
    workflow_run.add_argument("session_id")
    workflow_run.add_argument("stage")
    workflow_run.add_argument("--settings", default="{}")
    workflow_run.set_defaults(handler=command_workflow_run)

    artifact = commands.add_parser("artifact", help="Inspect managed artifacts.")
    artifact_commands = artifact.add_subparsers(dest="artifact_command", required=True)
    artifact_list = artifact_commands.add_parser("list")
    artifact_list.add_argument("--session-id")
    artifact_list.add_argument("--limit", type=int, default=100)
    artifact_list.set_defaults(handler=command_artifact_list)

    document = commands.add_parser("document", help="Inspect subtitle revisions and comparison lineage.")
    document_commands = document.add_subparsers(dest="document_command", required=True)
    document_show = document_commands.add_parser("show")
    document_show.add_argument("session_id")
    document_show.set_defaults(handler=command_document_show)

    provider = commands.add_parser("provider", help="Inspect configured providers and model defaults.")
    provider_commands = provider.add_subparsers(dest="provider_command", required=True)
    provider_list = provider_commands.add_parser("list")
    provider_list.set_defaults(handler=command_provider_list)

    voice = commands.add_parser("voice", help="Inspect voice references and transcript readiness.")
    voice_commands = voice.add_subparsers(dest="voice_command", required=True)
    voice_list = voice_commands.add_parser("list")
    voice_list.set_defaults(handler=command_voice_list)

    rvc = commands.add_parser("rvc", help="Manage RVC models and conversion jobs.")
    rvc_commands = rvc.add_subparsers(dest="rvc_command", required=True)
    rvc_list = rvc_commands.add_parser("list")
    rvc_list.set_defaults(handler=command_rvc_list)
    rvc_upload = rvc_commands.add_parser("upload")
    rvc_upload.add_argument("weights")
    rvc_upload.add_argument("index")
    rvc_upload.set_defaults(handler=command_rvc_upload)
    rvc_convert = rvc_commands.add_parser("convert")
    rvc_convert.add_argument("artifact_id")
    rvc_convert.add_argument("--model", required=True)
    rvc_convert.add_argument("--session-id")
    rvc_convert.add_argument("--settings", default="{}")
    rvc_convert.set_defaults(handler=command_rvc_convert)

    training = commands.add_parser("training", help="Manage durable XTTS training runs.")
    training_commands = training.add_subparsers(dest="training_command", required=True)
    training_list = training_commands.add_parser("list")
    training_list.set_defaults(handler=command_training_list)
    training_start = training_commands.add_parser("start")
    training_start.add_argument("model_name")
    training_start.add_argument("artifact_id")
    training_start.add_argument("--text-artifact-id")
    training_start.add_argument("--settings", default="{}")
    training_start.set_defaults(handler=command_training_start)
    training_cancel = training_commands.add_parser("cancel")
    training_cancel.add_argument("training_id")
    training_cancel.set_defaults(handler=command_training_cancel)

    export = commands.add_parser("export", help="Queue a session export.")
    export_commands = export.add_subparsers(dest="export_command", required=True)
    export_create = export_commands.add_parser("create")
    export_create.add_argument("session_id")
    export_create.add_argument("--options", default="{}")
    export_create.set_defaults(handler=command_export_create)

    doctor = commands.add_parser("doctor", help="Validate managed artifacts and state.")
    doctor.set_defaults(handler=command_doctor)
    openapi = commands.add_parser("openapi", help="Print or write the OpenAPI contract.")
    openapi.add_argument("--output")
    openapi.set_defaults(handler=command_openapi)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.server:
            return int(command_remote(args) or 0)
        return int(args.handler(args) or 0)
    except KeyError as error:
        print(f"Not found: {error.args[0]}", file=sys.stderr)
        return 5
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
