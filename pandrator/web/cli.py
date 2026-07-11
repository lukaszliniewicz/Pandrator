"""Pandrator browser-server, worker, and automation CLI."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import uuid
import webbrowser
from pathlib import Path
from typing import Any

from waitress import serve as waitress_serve

from pandrator.runtime import DataPaths

from .api import create_app
from .artifacts import ArtifactService, sha256_file
from .auth import AuthService, BootstrapTokenStore
from .database import Database
from .jobs import JobQueue, Worker, noop_handler
from .legacy_migration import import_legacy_data
from .openapi import build_openapi_document
from .sessions import SessionService
from .workflow_handlers import WorkflowHandlers


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
    import_legacy_data(paths)
    return paths, Database(paths.database)


def _session_dict(record) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "workflow_kind": record.workflow_kind,
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

    bootstrap = BootstrapTokenStore()
    initial = str(os.environ.get("PANDRATOR_BOOTSTRAP_TOKEN") or "").strip()
    if initial:
        bootstrap.add(initial)
    app = create_app(data_root=paths.root, trusted_hosts=args.trusted_host or None, bootstrap_tokens=bootstrap)
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
    parser.add_argument("--json", action="store_true", help="Emit stable machine-readable JSON.")
    commands = parser.add_subparsers(dest="command", required=True)

    migrate = commands.add_parser("migrate", help="Import legacy Qt metadata into the web database.")
    migrate.set_defaults(handler=command_migrate)

    serve = commands.add_parser("serve", help="Run the Flask API and Svelte application with Waitress.")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8097)
    serve.add_argument("--threads", type=int, default=12)
    serve.add_argument("--trusted-host", action="append", default=[])
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
    session_create.add_argument("--preset", default="custom")
    session_create.add_argument("--include", action="append")
    session_create.set_defaults(handler=command_session_create)
    session_show = session_commands.add_parser("show")
    session_show.add_argument("session_id")
    session_show.set_defaults(handler=command_session_show)

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
        return int(args.handler(args) or 0)
    except KeyError as error:
        print(f"Not found: {error.args[0]}", file=sys.stderr)
        return 5
    except (ValueError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
