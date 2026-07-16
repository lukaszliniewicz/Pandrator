"""Headless installer lifecycle CLI shared with the Qt launcher."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import secrets
import shutil
import signal
import socket
import sys
import webbrowser
import subprocess
import sqlite3
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

import psutil

from .catalog import COMPONENTS
from .models import InstallSelection, LaunchSelection, WorkspacePaths
from .platforms import is_windows, resolve_launcher_workspace
from .service import HeadlessInstaller
from .supervisor import ManagedProcessSpec, ProcessSupervisor
from .update import health_check, install_wheel, restore_database, restore_installed_package, run_migrations, snapshot_installed_package, snapshot_sqlite, verify_release_manifest


LIFECYCLE_COMMANDS = {"list", "probe", "plan", "install", "update", "repair", "launch", "service", "stop", "uninstall"}

SERVICE_HEALTH_URLS = {
    "xtts": "http://127.0.0.1:8020/docs",
    "xtts_cpu": "http://127.0.0.1:8020/docs",
    "voxcpm": "http://127.0.0.1:8020/docs",
    "fishs2": "http://127.0.0.1:8020/docs",
    "fishs2_cpu": "http://127.0.0.1:8020/docs",
    "voxtral": "http://127.0.0.1:8000/docs",
    "silero": "http://127.0.0.1:8001/ready",
    "kokoro": "http://127.0.0.1:8880/docs",
    "kokoro_cpu": "http://127.0.0.1:8880/docs",
    "chatterbox": "http://127.0.0.1:8040/docs",
    "chatterbox_cpu": "http://127.0.0.1:8040/docs",
    "kobold_qwen": "http://127.0.0.1:8042/docs",
    "kobold_qwen_cpu": "http://127.0.0.1:8042/docs",
    "magpie": "http://127.0.0.1:8030/docs",
    "magpie_cpu": "http://127.0.0.1:8030/docs",
    "rvc": "http://127.0.0.1:8050/health",
    "rvc_cpu": "http://127.0.0.1:8050/health",
}


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


def _normalized_executable(path: str | os.PathLike[str]) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.fspath(path))))


def _validated_supervisor_process(paths: WorkspacePaths, payload: dict[str, Any]):
    """Return the recorded supervisor only when durable process identity matches."""
    try:
        supervisor_pid = int(payload.get("supervisor_pid") or 0)
        expected_create_time = float(payload.get("supervisor_create_time"))
    except (TypeError, ValueError):
        raise RuntimeError("Runtime state does not contain a verifiable supervisor identity.") from None

    instance_id = str(payload.get("instance_id") or "").strip()
    expected_executable = str(payload.get("supervisor_executable") or "").strip()
    if supervisor_pid <= 0 or not instance_id or not expected_executable:
        raise RuntimeError("Runtime state does not contain a verifiable supervisor identity.")

    try:
        process = psutil.Process(supervisor_pid)
        actual_create_time = process.create_time()
        actual_executable = process.exe()
    except psutil.NoSuchProcess:
        return None
    except (psutil.AccessDenied, OSError) as error:
        raise RuntimeError("Could not validate the recorded Pandrator supervisor process.") from error

    if abs(actual_create_time - expected_create_time) > 0.01 or (
        _normalized_executable(actual_executable) != _normalized_executable(expected_executable)
    ):
        raise RuntimeError(
            "Runtime state points to a different process; refusing to signal a potentially unrelated PID."
        )

    lock_path = paths.install_root / "pandrator.instance.lock"
    try:
        lock_payload = json.loads(lock_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("The Pandrator supervisor lock is missing or invalid; refusing to signal it.") from error
    if not isinstance(lock_payload, dict):
        raise RuntimeError("The Pandrator supervisor lock must contain a JSON object.")
    try:
        lock_pid = int(lock_payload.get("pid") or 0)
        lock_create_time = lock_payload.get("process_create_time")
        if lock_create_time is not None:
            lock_create_time = float(lock_create_time)
    except (TypeError, ValueError) as error:
        raise RuntimeError("The Pandrator supervisor lock contains an invalid process identity.") from error
    if str(lock_payload.get("instance_id") or "") != instance_id or lock_pid != supervisor_pid:
        raise RuntimeError("Runtime state and supervisor lock do not identify the same process.")
    if lock_create_time is not None and abs(float(lock_create_time) - expected_create_time) > 0.01:
        raise RuntimeError("Runtime state and supervisor lock have different process creation times.")

    return process


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
            "installed": (
                (paths.pandrator_repo / "pyproject.toml").is_file()
                and (paths.pandrator_repo / "pandrator" / "web" / "static" / "index.html").is_file()
            ),
            "repository": str(paths.pandrator_repo),
        },
        "components": [_component_status(paths, key) for key in COMPONENTS],
        "runtime_state": str(paths.install_root / "runtime-processes.json"),
    }
    _emit(payload, args.json)
    return 0


def _selection(args) -> InstallSelection:
    components = [item.strip() for raw in (args.components or []) for item in raw.split(",") if item.strip()]
    return InstallSelection.from_components(
        components,
        install_pandrator=not args.skip_pandrator,
        crispasr_backend=str(getattr(args, "crispasr_backend", "auto") or "auto"),
        crispasr_engine=str(getattr(args, "crispasr_engine", "whisper-large-v3") or "whisper-large-v3"),
        crispasr_model_quantization=str(getattr(args, "crispasr_model_quantization", "f16") or "f16"),
        kobold_qwen_backend=str(getattr(args, "qwen_backend", "auto") or "auto"),
        kobold_qwen_model_size=str(getattr(args, "qwen_model_size", "0.6b") or "0.6b"),
        kobold_qwen_quantization=str(getattr(args, "qwen_quantization", "f16") or "f16"),
        kobold_qwen_initial_model=str(getattr(args, "qwen_initial_model", "base") or "base"),
    )


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
    completed = False
    try:
        installer.run_headless_install(
            set(selection.selected_components()),
            install_pandrator=selection.pandrator,
            crispasr_backend=selection.crispasr_backend,
            crispasr_engine=selection.crispasr_engine,
            crispasr_model_quantization=selection.crispasr_model_quantization,
            kobold_qwen_backend=selection.kobold_qwen_backend,
            kobold_qwen_model_size=selection.kobold_qwen_model_size,
            kobold_qwen_quantization=selection.kobold_qwen_quantization,
            kobold_qwen_initial_model=selection.kobold_qwen_initial_model,
        )
        completed = True
    finally:
        # Component bootstrap methods own and stop their temporary validation
        # processes.  A second broad shutdown after success can act on stale
        # PIDs after reuse and has caused a successful Linux CLI install to
        # terminate itself with SIGTERM.  Failure cleanup remains best-effort.
        if not completed:
            installer.shutdown_apps()
        installer.shutdown_logging()
    _emit({**plan, "status": "installed"}, args.json)
    return 0


def _runtime_python(paths: WorkspacePaths) -> Path:
    executable = "python.exe" if is_windows() else "bin/python"
    candidates = (
        paths.pandrator_repo / ".pixi" / "envs" / "default" / executable,
        paths.install_root / ".pixi" / "envs" / "default" / executable,
        paths.environment("pandrator_installer") / ".pixi" / "envs" / "default" / executable,
        Path(sys.executable),
    )
    return next((candidate for candidate in candidates if candidate.is_file()), Path(sys.executable))


def _runtime_specs(paths: WorkspacePaths, args, bootstrap_token: str = "") -> list[ManagedProcessSpec]:
    python = str(_runtime_python(paths))
    data_root = str(paths.install_root)
    cwd = str(paths.pandrator_repo if paths.pandrator_repo.is_dir() else Path.cwd())
    host = str(args.host)
    port = int(args.port)
    crispasr_executable = paths.install_root / "CrispASR" / ("crispasr.exe" if is_windows() else "crispasr")
    speech_environment = {
        "CRISPASR_CACHE_DIR": str(paths.install_root / "cache" / "crispasr"),
    }
    if crispasr_executable.is_file():
        speech_environment["CRISPASR_EXECUTABLE"] = str(crispasr_executable)
    raw_components = [item.strip() for raw in (getattr(args, "components", []) or []) for item in raw.split(",") if item.strip()]
    unknown = sorted(set(raw_components).difference(SERVICE_HEALTH_URLS))
    if unknown:
        raise ValueError("Unsupported supervised service(s): " + ", ".join(unknown))
    launcher = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, "-m", "pandrator_installer.lifecycle"]
    service_specs = [
        ManagedProcessSpec(
            key=f"service-{component}",
            label=f"Pandrator service {component}",
            command=tuple([*launcher, "service", "--workspace", str(paths.workspace), "--component", component]),
            cwd=cwd,
            health_url=SERVICE_HEALTH_URLS[component],
            restart_limit=1,
            startup_timeout_seconds=180,
        )
        for component in raw_components
    ]
    api_command = [
        python, "-m", "pandrator", "--data-dir", data_root, "serve",
        "--host", host, "--port", str(port), "--no-open-browser",
    ]
    if host not in {"127.0.0.1", "localhost", "::1"}:
        if getattr(args, "allow_insecure_remote", False):
            api_command.append("--allow-insecure-remote")
        requested_trusted_hosts = list(getattr(args, "trusted_host", []) or [])
        trusted_hosts = ["localhost", "127.0.0.1", "::1", *requested_trusted_hosts]
        if not requested_trusted_hosts:
            trusted_hosts.append(socket.gethostname())
            try:
                trusted_hosts.extend(socket.gethostbyname_ex(socket.gethostname())[2])
            except OSError:
                pass
        for trusted_host in dict.fromkeys(trusted_hosts):
            api_command.extend(["--trusted-host", trusted_host])
    api_environment = dict(speech_environment)
    if bootstrap_token:
        api_environment["PANDRATOR_BOOTSTRAP_TOKEN"] = bootstrap_token
    return [
        *service_specs,
        ManagedProcessSpec(
            key="api",
            label="Pandrator API",
            command=tuple(api_command),
            cwd=cwd,
            env=api_environment,
            health_url=f"http://127.0.0.1:{port}/api/v1/health",
            restart_limit=2,
            startup_timeout_seconds=45,
        ),
        ManagedProcessSpec(
            key="worker",
            label="Pandrator worker",
            command=(python, "-m", "pandrator", "--data-dir", data_root, "worker"),
            cwd=cwd,
            env=speech_environment,
            restart_limit=2,
        ),
    ]


def _service_selection(component: str) -> LaunchSelection:
    values = {"pandrator": False}
    base = component[:-4] if component.endswith("_cpu") else component
    if base == "rvc":
        values.update(rvc=True, rvc_cpu=component.endswith("_cpu"))
    else:
        values[base] = True
        cpu_field = f"{base}_cpu"
        if cpu_field in LaunchSelection.__dataclass_fields__:
            values[cpu_field] = component.endswith("_cpu")
    return LaunchSelection(**values)


def _owned_service_processes(installer) -> list[Any]:
    return [
        process
        for _key, _label, process in installer._collect_running_backends()
        if process is not None
    ]


def command_service(args) -> int:
    """Internal owned child used by ProcessSupervisor for one speech service."""
    if args.component not in SERVICE_HEALTH_URLS:
        raise ValueError(f"Unsupported supervised service: {args.component}")
    paths = _workspace(args)
    installer = HeadlessInstaller(working_dir=str(paths.workspace))
    stop_requested = False

    def request_stop(_signum, _frame):
        nonlocal stop_requested
        stop_requested = True

    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, request_stop)
        except (OSError, ValueError):
            pass
    try:
        installer.launch_process(_service_selection(args.component))
        owned = _owned_service_processes(installer)
        if not owned:
            raise RuntimeError(f"Service {args.component} is available but is not owned by this supervisor.")
        while not stop_requested:
            if any(process.poll() is not None for process in owned):
                return 1
            time.sleep(0.5)
        return 0
    finally:
        installer.shutdown_apps()
        installer.shutdown_logging()
        for signum, handler in previous.items():
            try:
                signal.signal(signum, handler)
            except (OSError, ValueError):
                pass


def _open_browser(url: str) -> None:
    old_env = {}
    for key in ("LD_LIBRARY_PATH", "QT_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        if key in os.environ:
            old_env[key] = os.environ[key]
            del os.environ[key]
    try:
        opened = webbrowser.open_new_tab(url)
        if not opened:
            try:
                if is_windows():
                    os.startfile(url)  # type: ignore[attr-defined]
                elif sys.platform == "darwin":
                    subprocess.Popen(["open", url])
                else:
                    subprocess.Popen(["xdg-open", url])
            except OSError:
                print(f"Open Pandrator in a browser: {url}", flush=True)
    finally:
        for key, value in old_env.items():
            os.environ[key] = value


def command_launch(args) -> int:
    paths = _workspace(args)
    paths.install_root.mkdir(parents=True, exist_ok=True)
    remote = args.host not in {"127.0.0.1", "localhost", "::1"}
    configured_scope = getattr(args, "password_scope", None)
    password_scope = str(
        configured_scope if configured_scope is not None else ("remote" if remote else "none")
    ).strip().lower()
    if password_scope not in {"none", "local", "remote", "all"}:
        raise RuntimeError(f"Unsupported password scope: {password_scope}")
    if remote and password_scope not in {"remote", "all"}:
        raise RuntimeError("LAN access requires password protection for remote clients or all clients.")

    automatic_local_sign_in = password_scope in {"none", "remote"}
    token = secrets.token_urlsafe(32) if automatic_local_sign_in else ""
    url = f"http://127.0.0.1:{args.port}/"
    if token:
        url += f"#bootstrap={token}"
    password = str(os.environ.pop("PANDRATOR_OWNER_PASSWORD", "") or "")
    database_path = paths.install_root / "pandrator.sqlite3"
    initialized = False
    if database_path.is_file():
        try:
            with sqlite3.connect(database_path) as connection:
                initialized = bool(connection.execute("SELECT COUNT(*) FROM owner_account").fetchone()[0])
        except sqlite3.Error:
            initialized = False

    protection_enabled = password_scope != "none" or remote
    if password and protection_enabled:
        if len(password) < 10:
            raise RuntimeError("The owner password must contain at least 10 characters.")
        environment = os.environ.copy()
        environment["PANDRATOR_OWNER_PASSWORD"] = password
        auth_command = [
            str(_runtime_python(paths)), "-m", "pandrator", "--data-dir", str(paths.install_root),
            "auth", "init",
        ]
        if initialized:
            auth_command.append("--replace")
        result = subprocess.run(
            auth_command,
            cwd=str(paths.pandrator_repo),
            env=environment,
            capture_output=True,
            text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "Could not save the owner password.")
        initialized = True
    if protection_enabled and not initialized:
        raise RuntimeError("Password protection requires an owner password of at least 10 characters.")
    if password_scope in {"local", "all"}:
        # A local-password policy must not be bypassed by a session cookie from
        # an earlier automatic-bootstrap launch. Rotating the signing secret at
        # startup invalidates those cookies without storing server-side sessions.
        try:
            (paths.install_root / ".flask-secret").unlink(missing_ok=True)
        except OSError as exc:
            raise RuntimeError("Could not invalidate existing browser sessions.") from exc
    supervisor = ProcessSupervisor(
        data_root=paths.install_root,
        specs=_runtime_specs(paths, args, token),
        status_callback=lambda message: print(message, flush=True),
        ready_callback=(lambda: None) if args.no_browser else (lambda: _open_browser(url)),
    )
    supervisor.run_foreground()
    return 0


def command_stop(args) -> int:
    paths = _workspace(args)
    state_path = paths.install_root / "runtime-processes.json"
    if not state_path.is_file():
        _emit({"status": "not_running"}, args.json)
        return 0
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("Runtime state is unreadable; refusing to signal an unknown PID.") from error
    if not isinstance(payload, dict):
        raise RuntimeError("Runtime state must contain a JSON object.")
    supervisor = _validated_supervisor_process(paths, payload)
    if supervisor is None:
        state_path.unlink(missing_ok=True)
        _emit({"status": "not_running"}, args.json)
        return 0
    supervisor_pid = supervisor.pid
    try:
        supervisor.terminate()
    except psutil.NoSuchProcess:
        state_path.unlink(missing_ok=True)
        _emit({"status": "not_running"}, args.json)
        return 0
    except psutil.AccessDenied as error:
        raise RuntimeError("Access was denied while stopping the Pandrator supervisor.") from error
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
    plan = {"workspace": str(paths.workspace), "wheel": str(wheel), "sha256": digest, "operations": ["verify_signature", "stop_intake", "drain_or_cancel", "stop", "snapshot", "install_wheel", "migrate", "health_check", "rollback_on_failure", "restart"]}
    if args.dry_run:
        _emit({**plan, "dry_run": True}, args.json)
        return 0
    if not args.manifest or not args.public_key:
        raise RuntimeError("Live update requires --manifest and --public-key for Ed25519 verification.")
    verified = verify_release_manifest(Path(args.manifest).expanduser().resolve(), Path(args.public_key).expanduser().resolve())
    if verified.wheel_name != wheel.name or not secrets.compare_digest(verified.wheel_sha256, digest.lower()):
        raise ValueError("The selected wheel does not match the signed release manifest.")

    data_root = paths.install_root
    data_root.mkdir(parents=True, exist_ok=True)
    maintenance = data_root / "maintenance.json"
    maintenance.write_text(json.dumps({"reason": "update", "version": verified.version, "started_at": time.time()}), encoding="utf-8")
    runtime_state = data_root / "runtime-processes.json"
    restart_command: list[str] | None = None
    restart_cwd: str | None = None
    original_supervisor = None
    stop_attempted = False
    stop_confirmed = False
    try:
        database_path = data_root / "pandrator.sqlite3"
        if database_path.is_file():
            deadline = time.monotonic() + max(0.0, float(args.drain_timeout))
            while True:
                try:
                    with sqlite3.connect(database_path) as connection:
                        running = int(connection.execute("SELECT COUNT(*) FROM jobs WHERE status = 'running'").fetchone()[0])
                        if running and args.cancel_running:
                            connection.execute("UPDATE jobs SET status = 'cancel_requested' WHERE status = 'running'")
                            connection.commit()
                except sqlite3.OperationalError:
                    running = 0
                if not running:
                    break
                if time.monotonic() >= deadline:
                    if not args.cancel_running:
                        raise RuntimeError("Running jobs did not drain before the update timeout; retry with --cancel-running to request cancellation.")
                    break
                time.sleep(0.5)

        if runtime_state.is_file():
            try:
                runtime = json.loads(runtime_state.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                raise RuntimeError("Runtime state is unreadable; refusing to stop an unknown PID.") from error
            if not isinstance(runtime, dict):
                raise RuntimeError("Runtime state must contain a JSON object.")
            original_supervisor = _validated_supervisor_process(paths, runtime)
            if original_supervisor is None:
                runtime_state.unlink(missing_ok=True)
            else:
                try:
                    restart_command = original_supervisor.cmdline()
                    restart_cwd = original_supervisor.cwd()
                except (psutil.NoSuchProcess, psutil.AccessDenied, OSError) as error:
                    raise RuntimeError("Could not capture the Pandrator restart command safely.") from error
                try:
                    original_supervisor.terminate()
                except psutil.NoSuchProcess:
                    original_supervisor = None
                    restart_command = None
                    restart_cwd = None
                    runtime_state.unlink(missing_ok=True)
                except psutil.AccessDenied as error:
                    raise RuntimeError("Access was denied while stopping the Pandrator supervisor.") from error
                else:
                    stop_attempted = True
                    try:
                        original_supervisor.wait(timeout=40)
                    except psutil.TimeoutExpired as error:
                        raise RuntimeError("The running Pandrator supervisor did not stop cleanly.") from error
                    except psutil.NoSuchProcess:
                        pass
                    stop_confirmed = True
                    runtime_state.unlink(missing_ok=True)

        python = _runtime_python(paths)
        backup_dir = data_root / "backups" / f"update-{time.time_ns()}-{verified.version}"
        backup_dir.mkdir(parents=True, exist_ok=False)
        database_snapshot = backup_dir / "pandrator.sqlite3"
        snapshot_sqlite(data_root / "pandrator.sqlite3", database_snapshot)
        package_snapshot = backup_dir / "installed-package.zip"
        site_packages = snapshot_installed_package(python, package_snapshot)
        try:
            install_wheel(python, wheel)
            run_migrations(python, data_root)
            health_check(python, data_root)
        except Exception as activation_error:
            try:
                restore_installed_package(package_snapshot, site_packages)
                restore_database(database_snapshot, data_root / "pandrator.sqlite3")
                health_check(python, data_root)
            except Exception as rollback_error:
                raise RuntimeError(
                    f"Update activation failed ({activation_error}) and rollback verification also failed: {rollback_error}"
                ) from rollback_error
            raise RuntimeError(
                f"Update activation failed and the previous package/database were restored: {activation_error}"
            ) from activation_error
    finally:
        active_error = sys.exc_info()[0] is not None
        cleanup_error = None
        try:
            maintenance.unlink(missing_ok=True)
        except OSError as error:
            cleanup_error = error
            logging.exception("Could not clear Pandrator maintenance mode")

        restart_error = None
        supervisor_stopped = stop_confirmed
        if stop_attempted and not stop_confirmed and original_supervisor is not None:
            try:
                supervisor_stopped = not original_supervisor.is_running()
            except psutil.NoSuchProcess:
                supervisor_stopped = True
            except psutil.AccessDenied as error:
                restart_error = error
                logging.exception("Could not confirm that the Pandrator supervisor stopped")
        if restart_command and supervisor_stopped:
            try:
                subprocess.Popen(
                    restart_command,
                    cwd=restart_cwd or None,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            except (OSError, ValueError) as error:
                restart_error = error
                logging.exception("Could not restart Pandrator after the update attempt")

        if not active_error:
            if cleanup_error is not None:
                raise RuntimeError("Update completed but maintenance mode could not be cleared.") from cleanup_error
            if restart_error is not None:
                raise RuntimeError("Update completed but Pandrator could not be restarted.") from restart_error

    _emit({**plan, "status": "updated", "version": verified.version, "backup": str(backup_dir)}, args.json)
    return 0


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
        command.add_argument(
            "--crispasr-backend",
            choices=("auto", "cpu", "cuda", "vulkan", "metal"),
            default="auto",
        )
        command.add_argument("--crispasr-engine", choices=("whisper-large-v3", "parakeet-tdt-0.6b-v3"), default="whisper-large-v3")
        command.add_argument("--crispasr-model-quantization", choices=("f16", "q8_0", "q5_0", "q4_k"), default="f16")
        command.add_argument("--qwen-backend", choices=("auto", "cpu", "cuda", "vulkan", "metal"), default="auto")
        command.add_argument("--qwen-model-size", choices=("0.6b", "1.7b"), default="0.6b")
        command.add_argument("--qwen-quantization", choices=("f16", "q8_0"), default="f16")
        command.add_argument("--qwen-initial-model", choices=("base", "customvoice", "both"), default="base")
        command.set_defaults(handler=handler)
    update = commands.add_parser("update")
    update.add_argument("--wheel", required=True)
    update.add_argument("--sha256")
    update.add_argument("--manifest")
    update.add_argument("--public-key")
    update.add_argument("--drain-timeout", type=float, default=300.0)
    update.add_argument("--cancel-running", action="store_true")
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
    launch.add_argument("--components", action="append", default=[])
    launch.add_argument("--trusted-host", action="append", default=[])
    launch.add_argument("--allow-insecure-remote", action="store_true")
    launch.add_argument(
        "--password-scope",
        choices=("none", "local", "remote", "all"),
        default=None,
        help="Require the owner password locally, remotely, everywhere, or nowhere (loopback only).",
    )
    launch.set_defaults(handler=command_launch)
    service = commands.add_parser("service", help=argparse.SUPPRESS)
    service.add_argument("--component", required=True)
    service.set_defaults(handler=command_service)
    commands.add_parser("stop").set_defaults(handler=command_stop)
    uninstall = commands.add_parser("uninstall")
    uninstall.add_argument("--dry-run", action="store_true")
    uninstall.add_argument("--yes", action="store_true")
    uninstall.add_argument("--purge-data", action="store_true")
    uninstall.set_defaults(handler=command_uninstall)
    return parser


def main(argv: list[str] | None = None) -> int:
    normalized = list(sys.argv[1:] if argv is None else argv)
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


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
