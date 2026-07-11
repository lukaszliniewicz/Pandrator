"""Headless installer lifecycle CLI shared with the Qt launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import shutil
import signal
import sys
import webbrowser
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .catalog import COMPONENTS
from .models import InstallSelection, WorkspacePaths
from .platforms import is_windows, resolve_launcher_workspace
from .service import HeadlessInstaller
from .supervisor import ManagedProcessSpec, ProcessSupervisor


LIFECYCLE_COMMANDS = {"list", "probe", "plan", "install", "update", "repair", "launch", "stop", "uninstall"}


def _emit(payload: Any, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))
    elif isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    elif isinstance(payload, list):
        for item in payload:
            print(item if isinstance(item, str) else json.dumps(item, ensure_ascii=False, default=str))
    else:
        print(payload)


def _workspace(args) -> WorkspacePaths:
    return WorkspacePaths.from_value(resolve_launcher_workspace(args.workspace))


def _component_status(paths: WorkspacePaths, key: str) -> dict[str, Any]:
    definition = COMPONENTS[key]
    markers = [paths.install_root / marker for marker in definition.markers]
    return {
        "key": key,
        "label": definition.label,
        "dependencies": list(definition.dependencies),
        "variant_of": definition.variant_of,
        "installed": bool(markers) and all(marker.exists() for marker in markers),
        "markers": [str(marker) for marker in markers],
    }


def command_list(args) -> int:
    _emit([{"key": key, "label": value.label, "dependencies": list(value.dependencies), "variant_of": value.variant_of} for key, value in COMPONENTS.items()], args.json)
    return 0


def command_probe(args) -> int:
    paths = _workspace(args)
    payload = {
        "workspace": str(paths.workspace),
        "install_root": str(paths.install_root),
        "pandrator": {
            "installed": (paths.pandrator_repo / "pyproject.toml").is_file() or (paths.pandrator_repo / "main.py").is_file(),
            "repository": str(paths.pandrator_repo),
        },
        "components": [_component_status(paths, key) for key in COMPONENTS],
        "runtime_state": str(paths.install_root / "runtime-processes.json"),
    }
    _emit(payload, args.json)
    return 0


def _selection(args) -> InstallSelection:
    components = [item.strip() for raw in (args.components or []) for item in raw.split(",") if item.strip()]
    return InstallSelection.from_components(components, install_pandrator=not args.skip_pandrator)


def _plan(args) -> dict[str, Any]:
    paths = _workspace(args)
    selection = _selection(args)
    return {
        "workspace": str(paths.workspace),
        "install_root": str(paths.install_root),
        "dry_run": bool(getattr(args, "dry_run", False)),
        "pandrator": selection.pandrator,
        "components": list(selection.selected_components()),
        "operations": (["prepare_pixi_runtime", "install_pandrator_wheel"] if selection.pandrator else []) + [f"install_component:{key}" for key in selection.selected_components()],
    }


def command_plan(args) -> int:
    _emit(_plan(args), args.json)
    return 0


def command_install(args) -> int:
    plan = _plan(args)
    if args.dry_run:
        _emit(plan, args.json)
        return 0
    paths = _workspace(args)
    selection = _selection(args)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    installer = HeadlessInstaller(working_dir=str(paths.workspace))
    try:
        installer.run_headless_install(set(selection.selected_components()), install_pandrator=selection.pandrator)
    finally:
        installer.shutdown_apps()
        installer.shutdown_logging()
    _emit({**plan, "status": "installed"}, args.json)
    return 0


def _runtime_python(paths: WorkspacePaths) -> Path:
    executable = "python.exe" if is_windows() else "bin/python"
    candidates = (
        paths.pandrator_repo / ".pixi" / "envs" / "default" / executable,
        paths.install_root / ".pixi" / "envs" / "default" / executable,
        Path(sys.executable),
    )
    return next((candidate for candidate in candidates if candidate.is_file()), Path(sys.executable))


def _runtime_specs(paths: WorkspacePaths, args, bootstrap_token: str) -> list[ManagedProcessSpec]:
    python = str(_runtime_python(paths))
    data_root = str(paths.install_root)
    cwd = str(paths.pandrator_repo if paths.pandrator_repo.is_dir() else Path.cwd())
    host = str(args.host)
    port = int(args.port)
    return [
        ManagedProcessSpec(
            key="api",
            label="Pandrator API",
            command=(python, "-m", "pandrator", "--data-dir", data_root, "serve", "--host", host, "--port", str(port), "--no-open-browser"),
            cwd=cwd,
            env={"PANDRATOR_BOOTSTRAP_TOKEN": bootstrap_token},
            health_url=f"http://127.0.0.1:{port}/api/v1/health",
            restart_limit=2,
            startup_timeout_seconds=45,
        ),
        ManagedProcessSpec(
            key="worker",
            label="Pandrator worker",
            command=(python, "-m", "pandrator", "--data-dir", data_root, "worker"),
            cwd=cwd,
            restart_limit=2,
        ),
    ]


def command_launch(args) -> int:
    paths = _workspace(args)
    paths.install_root.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    url = f"http://127.0.0.1:{args.port}/#bootstrap={token}"
    supervisor = ProcessSupervisor(
        data_root=paths.install_root,
        specs=_runtime_specs(paths, args, token),
        status_callback=lambda message: print(message, flush=True),
        ready_callback=(lambda: None) if args.no_browser else (lambda: webbrowser.open(url)),
    )
    supervisor.run_foreground()
    return 0


def command_stop(args) -> int:
    paths = _workspace(args)
    state_path = paths.install_root / "runtime-processes.json"
    if not state_path.is_file():
        _emit({"status": "not_running"}, args.json)
        return 0
    payload = json.loads(state_path.read_text(encoding="utf-8"))
    supervisor_pid = int(payload.get("supervisor_pid") or 0)
    if supervisor_pid <= 0:
        raise RuntimeError("Runtime state does not contain a supervisor PID.")
    os.kill(supervisor_pid, signal.SIGTERM)
    _emit({"status": "stop_requested", "supervisor_pid": supervisor_pid}, args.json)
    return 0


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def command_update(args) -> int:
    paths = _workspace(args)
    wheel = Path(args.wheel).expanduser().resolve()
    if not wheel.is_file():
        raise ValueError(f"Wheel not found: {wheel}")
    digest = _sha256(wheel)
    if args.sha256 and not secrets.compare_digest(digest.lower(), args.sha256.lower()):
        raise ValueError("Wheel hash verification failed.")
    plan = {"workspace": str(paths.workspace), "wheel": str(wheel), "sha256": digest, "operations": ["stop", "snapshot", "install_wheel", "migrate", "health_check", "rollback_on_failure"]}
    if args.dry_run:
        _emit({**plan, "dry_run": True}, args.json)
        return 0
    raise RuntimeError("Live update activation requires a signed release manifest; use --dry-run for local wheel validation.")


def command_repair(args) -> int:
    paths = _workspace(args)
    problems = []
    if not paths.install_root.is_dir():
        problems.append("install_root_missing")
    if not paths.pandrator_repo.is_dir():
        problems.append("pandrator_runtime_missing")
    payload = {"workspace": str(paths.workspace), "problems": problems, "status": "healthy" if not problems else "repair_required"}
    if args.dry_run or not problems:
        _emit(payload, args.json)
        return 0 if not problems else 2
    install_values = dict(vars(args))
    install_values.update(components=[], skip_pandrator=False)
    install_args = argparse.Namespace(**install_values)
    return command_install(install_args)


def command_uninstall(args) -> int:
    paths = _workspace(args)
    targets = [paths.pandrator_repo, paths.install_root / "envs"]
    payload = {"workspace": str(paths.workspace), "remove": [str(path) for path in targets], "preserve_data": not args.purge_data}
    if args.dry_run:
        _emit({**payload, "dry_run": True}, args.json)
        return 0
    if not args.yes:
        raise RuntimeError("Uninstall requires --yes. User data is preserved unless --purge-data is also supplied.")
    if not args.purge_data and paths.pandrator_repo.is_dir():
        preserved_root = paths.install_root / "preserved-data"
        preserved_root.mkdir(parents=True, exist_ok=True)
        for name in ("Outputs", "voices", "models", "pandrator_state.sqlite3", "pandrator.sqlite3", "config.json"):
            source = paths.pandrator_repo / name
            if not source.exists():
                continue
            destination = preserved_root / name
            if destination.exists():
                raise RuntimeError(f"Cannot preserve {source}: {destination} already exists.")
            shutil.move(str(source), str(destination))
    for target in targets:
        resolved = target.resolve()
        if resolved.is_dir() and resolved != paths.install_root.resolve() and paths.install_root.resolve() in resolved.parents:
            shutil.rmtree(resolved)
    if args.purge_data and paths.install_root.is_dir():
        shutil.rmtree(paths.install_root)
    _emit({**payload, "status": "uninstalled"}, args.json)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="pandrator-installer", description="Pandrator installer lifecycle and supervisor CLI")
    parser.add_argument("--workspace")
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list").set_defaults(handler=command_list)
    commands.add_parser("probe").set_defaults(handler=command_probe)
    for name, handler in (("plan", command_plan), ("install", command_install)):
        command = commands.add_parser(name)
        command.add_argument("--components", action="append", default=[])
        command.add_argument("--skip-pandrator", action="store_true")
        command.add_argument("--dry-run", action="store_true")
        command.set_defaults(handler=handler)
    update = commands.add_parser("update")
    update.add_argument("--wheel", required=True)
    update.add_argument("--sha256")
    update.add_argument("--dry-run", action="store_true")
    update.set_defaults(handler=command_update)
    repair = commands.add_parser("repair")
    repair.add_argument("--dry-run", action="store_true")
    repair.set_defaults(handler=command_repair)
    launch = commands.add_parser("launch")
    launch.add_argument("--foreground", action="store_true", default=True)
    launch.add_argument("--host", default="127.0.0.1")
    launch.add_argument("--port", type=int, default=8097)
    launch.add_argument("--no-browser", action="store_true")
    launch.set_defaults(handler=command_launch)
    commands.add_parser("stop").set_defaults(handler=command_stop)
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--purge-data", action="store_true")
    uninstall.set_defaults(handler=command_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    normalized = list(argv or [])
    command_index = next((index for index, item in enumerate(normalized) if item in LIFECYCLE_COMMANDS), None)
    if command_index is not None:
        suffix = normalized[command_index + 1 :]
        prefix = normalized[:command_index]
        for flag in ("--workspace", "--json"):
            if flag not in suffix:
                continue
            index = suffix.index(flag)
            prefix.append(suffix.pop(index))
            if flag == "--workspace" and index < len(suffix):
                prefix.append(suffix.pop(index))
        normalized = [*prefix, normalized[command_index], *suffix]
    args = build_parser().parse_args(normalized)
    try:
        return int(args.handler(args) or 0)
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
