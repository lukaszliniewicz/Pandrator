"""Cross-platform process supervision shared by Qt and headless launchers."""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import urlopen

from .subprocess_env import external_subprocess_environment

try:
    import psutil
except ImportError:  # pragma: no cover - installer dependencies include psutil
    psutil = None


class InstanceAlreadyRunning(RuntimeError):
    pass


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if psutil is not None:
        return bool(psutil.pid_exists(pid))
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class InstanceLock:
    """One supervisor owner per data root, using an atomic lock record."""

    def __init__(self, path: str | os.PathLike[str]):
        self.path = Path(path)
        self.instance_id = str(uuid.uuid4())
        self.acquired = False

    def acquire(self) -> str:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"instance_id": self.instance_id, "pid": os.getpid(), "created_at": time.time()}
        if psutil is not None:
            try:
                process = psutil.Process(os.getpid())
                payload["process_create_time"] = process.create_time()
                payload["executable"] = process.exe()
            except (psutil.Error, OSError):
                pass
        for _attempt in range(2):
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                try:
                    current = json.loads(self.path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    current = {}
                pid = int(current.get("pid") or 0)
                if _pid_exists(pid):
                    raise InstanceAlreadyRunning(
                        f"Pandrator data root is already supervised by PID {pid}."
                    )
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                continue
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
                handle.flush()
                os.fsync(handle.fileno())
            self.acquired = True
            return self.instance_id
        raise InstanceAlreadyRunning("Could not acquire the Pandrator instance lock.")

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            current = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
        if current.get("instance_id") == self.instance_id:
            try:
                self.path.unlink()
            except FileNotFoundError:
                pass
        self.acquired = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.release()


@dataclass(frozen=True, slots=True)
class ManagedProcessSpec:
    key: str
    label: str
    command: tuple[str, ...]
    cwd: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    health_url: str | None = None
    required: bool = True
    restart_limit: int = 1
    startup_timeout_seconds: float = 30.0

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ManagedProcessSpec":
        command = payload.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise ValueError("Managed process command must be a non-empty string array.")
        return cls(
            key=str(payload.get("key") or "").strip(),
            label=str(payload.get("label") or payload.get("key") or "Process").strip(),
            command=tuple(command),
            cwd=str(payload.get("cwd") or "") or None,
            env={str(key): str(value) for key, value in dict(payload.get("env") or {}).items()},
            health_url=str(payload.get("health_url") or "") or None,
            required=bool(payload.get("required", True)),
            restart_limit=max(0, int(payload.get("restart_limit", 1))),
            startup_timeout_seconds=max(1.0, float(payload.get("startup_timeout_seconds", 30))),
        )


@dataclass(slots=True)
class ManagedProcess:
    spec: ManagedProcessSpec
    process: subprocess.Popen
    log_handle: Any
    restarts: int = 0
    started_at: float = field(default_factory=time.time)


class ProcessSupervisor:
    def __init__(
        self,
        *,
        data_root: str | os.PathLike[str],
        specs: list[ManagedProcessSpec],
        status_callback=None,
        ready_callback=None,
    ):
        self.data_root = Path(data_root).expanduser().resolve()
        self.specs = list(specs)
        self.status_callback = status_callback or (lambda _message: None)
        self.ready_callback = ready_callback or (lambda: None)
        self.lock = InstanceLock(self.data_root / "pandrator.instance.lock")
        self.processes: dict[str, ManagedProcess] = {}
        self.stop_event = threading.Event()
        self.ready = False
        self.ready_at: float | None = None
        self.logs_dir = self.data_root / "logs" / "supervisor"
        self.runtime_state = self.data_root / "runtime-processes.json"
        self.runtime_control = self.data_root / "runtime-control.json"
        self.supervisor_create_time: float | None = None
        self.supervisor_executable = str(Path(sys.executable).resolve())
        if psutil is not None:
            try:
                current_process = psutil.Process(os.getpid())
                self.supervisor_create_time = current_process.create_time()
                self.supervisor_executable = current_process.exe()
            except (psutil.Error, OSError):
                pass

    def _status(self, message: str) -> None:
        logging.info(message)
        self.status_callback(message)

    @staticmethod
    def _popen_options() -> dict[str, Any]:
        if os.name == "nt":
            return {
                "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            }
        return {"start_new_session": True}

    def _write_state(self) -> None:
        payload = {
            "instance_id": self.lock.instance_id,
            "supervisor_pid": os.getpid(),
            "supervisor_create_time": self.supervisor_create_time,
            "supervisor_executable": self.supervisor_executable,
            "ready": self.ready,
            "ready_at": self.ready_at,
            "processes": {
                key: {
                    "pid": managed.process.pid,
                    "label": managed.spec.label,
                    "command": list(managed.spec.command),
                    "started_at": managed.started_at,
                    "restarts": managed.restarts,
                }
                for key, managed in self.processes.items()
                if managed.process.poll() is None
            },
        }
        temporary = self.runtime_state.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            attempts = 10
            for attempt in range(1, attempts + 1):
                try:
                    os.replace(temporary, self.runtime_state)
                    return
                except PermissionError:
                    if attempt == attempts:
                        raise
                    # A Windows reader opens runtime-processes.json without
                    # FILE_SHARE_DELETE, briefly preventing an atomic replace.
                    time.sleep(0.1)
        except OSError as error:
            # Runtime state is informational and rewritten on every monitor
            # tick. A stale snapshot is safer than terminating the supervised
            # process tree over a failed status-file update.
            logging.warning(
                "Could not update %s; keeping the previous state file: %s",
                self.runtime_state,
                error,
            )
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
            except OSError as error:
                logging.warning("Could not remove temporary runtime state %s: %s", temporary, error)

    def _managed_process_environment(self, spec: ManagedProcessSpec) -> dict[str, str]:
        # The frozen installer needs its private libraries, but the installed
        # application and host tools (notably FFmpeg) must use their own ABI.
        environment = external_subprocess_environment()
        environment.update(spec.env)
        environment["PANDRATOR_DATA_DIR"] = str(self.data_root)
        environment["PANDRATOR_SUPERVISOR_INSTANCE"] = self.lock.instance_id
        return environment

    def _healthy(self, spec: ManagedProcessSpec) -> bool:
        if not spec.health_url:
            return True
        try:
            with urlopen(spec.health_url, timeout=2) as response:
                return 200 <= int(response.status) < 500
        except (URLError, OSError, ValueError):
            return False

    def _start_one(self, spec: ManagedProcessSpec, restarts: int = 0) -> ManagedProcess:
        if not spec.key:
            raise ValueError("Managed process key is required.")
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.logs_dir / f"{spec.key}.log"
        log_handle = log_path.open("a", encoding="utf-8")
        environment = self._managed_process_environment(spec)
        self._status(f"Starting {spec.label}...")
        process = subprocess.Popen(
            list(spec.command),
            cwd=spec.cwd or None,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            shell=False,
            **self._popen_options(),
        )
        managed = ManagedProcess(spec=spec, process=process, log_handle=log_handle, restarts=restarts)
        self.processes[spec.key] = managed

        deadline = time.monotonic() + spec.startup_timeout_seconds
        started = time.monotonic()
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            if (spec.health_url and self._healthy(spec)) or (
                not spec.health_url and time.monotonic() - started >= 1.0
            ):
                self._status(f"{spec.label} is ready.")
                self._write_state()
                return managed
            time.sleep(0.25)
        if process.poll() is None and not spec.health_url:
            self._write_state()
            return managed
        self._stop_one(managed)
        raise RuntimeError(f"{spec.label} did not become healthy. See {log_path}.")

    def start_all(self) -> None:
        self.lock.acquire()
        try:
            try:
                self.runtime_control.unlink()
            except FileNotFoundError:
                pass
            for spec in self.specs:
                try:
                    self._start_one(spec)
                except Exception:
                    if spec.required:
                        raise
                    logging.exception("Optional managed process %s failed to start", spec.key)
            self._write_state()
            self.ready_callback()
            self.ready = True
            self.ready_at = time.time()
            self._write_state()
            self._status("Pandrator web application is ready.")
        except Exception:
            self.stop_all()
            raise

    def monitor_once(self) -> None:
        self._apply_control_requests()
        for key, managed in list(self.processes.items()):
            return_code = managed.process.poll()
            if return_code is None:
                continue
            managed.log_handle.close()
            del self.processes[key]
            spec = managed.spec
            if self.stop_event.is_set():
                continue
            if managed.restarts < spec.restart_limit:
                self._status(f"{spec.label} exited with code {return_code}; restarting.")
                self._start_one(spec, restarts=managed.restarts + 1)
            elif spec.required:
                raise RuntimeError(f"Required process {spec.label} exited with code {return_code}.")
            else:
                self._status(f"Optional process {spec.label} exited with code {return_code}.")
        self._write_state()

    def _apply_control_requests(self) -> None:
        try:
            payload = json.loads(self.runtime_control.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            logging.exception("Could not read supervisor control request")
            return

        try:
            self.runtime_control.unlink()
        except FileNotFoundError:
            pass

        requested = {
            str(key)
            for key in payload.get("stop_processes", [])
            if str(key).startswith("service-")
        }
        if not requested:
            return

        self.specs = [spec for spec in self.specs if spec.key not in requested]
        for key in requested:
            managed = self.processes.pop(key, None)
            if managed is None:
                continue
            self._stop_one(managed)
            self._status(f"{managed.spec.label} was stopped from the installer.")
        self._write_state()

    def run_foreground(self, interval: float = 1.0) -> None:
        previous_handlers: dict[int, Any] = {}

        def request_stop(_signum, _frame):
            self.stop_event.set()

        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                previous_handlers[signum] = signal.getsignal(signum)
                signal.signal(signum, request_stop)
            except (ValueError, OSError):
                pass
        self.start_all()
        try:
            while not self.stop_event.wait(max(0.1, interval)):
                self.monitor_once()
        except KeyboardInterrupt:
            self._status("Stopping Pandrator...")
        finally:
            self.stop_all()
            for signum, previous in previous_handlers.items():
                try:
                    signal.signal(signum, previous)
                except (ValueError, OSError):
                    pass

    @staticmethod
    def _terminate_process_tree(process: subprocess.Popen, timeout: float = 10.0) -> None:
        if process.poll() is not None:
            return
        if psutil is not None:
            try:
                parent = psutil.Process(process.pid)
                descendants = parent.children(recursive=True)
                for child in descendants:
                    child.terminate()
                parent.terminate()
                _, alive = psutil.wait_procs([*descendants, parent], timeout=timeout)
                for item in alive:
                    item.kill()
                if alive:
                    psutil.wait_procs(alive, timeout=2)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
                return
            except (psutil.Error, OSError):
                pass
        try:
            if os.name == "nt":
                process.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()

    def _stop_one(self, managed: ManagedProcess) -> None:
        self._status(f"Stopping {managed.spec.label}...")
        self._terminate_process_tree(managed.process)
        try:
            managed.log_handle.close()
        except OSError:
            pass

    def stop_all(self) -> None:
        self.stop_event.set()
        self.ready = False
        self.ready_at = None
        for _key, managed in reversed(list(self.processes.items())):
            self._stop_one(managed)
        self.processes.clear()
        try:
            self.runtime_state.unlink()
        except FileNotFoundError:
            pass
        try:
            self.runtime_control.unlink()
        except FileNotFoundError:
            pass
        self.lock.release()


def load_runtime_manifest(path: str | os.PathLike[str]) -> list[ManagedProcessSpec]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    raw_specs = payload.get("processes") if isinstance(payload, dict) else None
    if not isinstance(raw_specs, list):
        raise ValueError("Runtime manifest must contain a 'processes' array.")
    specs = [ManagedProcessSpec.from_dict(item) for item in raw_specs if isinstance(item, dict)]
    keys = [spec.key for spec in specs]
    if len(keys) != len(set(keys)):
        raise ValueError("Runtime process keys must be unique.")
    return specs
