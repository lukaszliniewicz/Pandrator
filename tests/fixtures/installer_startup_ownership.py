"""Disposable native child and log observations for supervisor startup failures."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, TextIO
from unittest import mock

import psutil

sys.path.insert(0, str(Path.cwd()))
from pandrator_installer import supervisor

CUTS = (
    "control",
    "oneshot",
    "persistent",
    "optional-persistent",
    "optional-control",
    "interrupt",
    "transfer",
    "environment",
    "status",
    "popen",
)


def measure(cut: str, revision: str | None) -> dict[str, object]:
    assert sys.platform == "linux"
    result: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="pandrator-startup-test-") as temporary:
        root = Path(temporary)
        source = supervisor
        if revision is not None:
            snapshot = root / "supervisor.py"
            snapshot.write_bytes(
                subprocess.check_output(
                    ["git", "show", f"{revision}:pandrator_installer/supervisor.py"]
                )
            )
            name = "pandrator_installer._startup_snapshot"
            module_spec = importlib.util.spec_from_file_location(name, snapshot)
            assert module_spec is not None and module_spec.loader is not None
            source = importlib.util.module_from_spec(module_spec)
            sys.modules[name] = source
            module_spec.loader.exec_module(source)
        marker = root / "ready"
        script = (
            "import signal,time;from pathlib import Path;"
            "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
            f"Path({str(marker)!r}).write_text('ready');time.sleep(30)"
        )
        spec = source.ManagedProcessSpec(
            key="service-fixture",
            label="Native startup fixture",
            command=(sys.executable, "-c", script),
            startup_timeout_seconds=2,
            restart_limit=0,
            required=not cut.startswith("optional-"),
        )
        owner = source.ProcessSupervisor(data_root=root, specs=[spec])
        native_popen = subprocess.Popen
        native_open = Path.open
        native_capture = source.capture_process_identity
        native_terminate = owner._terminate_process_tree
        children: list[subprocess.Popen] = []
        logs: list[TextIO] = []
        identities: list[float] = []
        termination_calls: list[int] = []

        def observe_open(path: Path, *args: Any, **kwargs: Any) -> Any:
            handle = native_open(path, *args, **kwargs)
            if path == owner.logs_dir / f"{spec.key}.log":
                # This observer holds the handle; it proves explicit close, not a GC leak.
                logs.append(handle)
            return handle

        def observe_popen(*args: Any, **kwargs: Any) -> subprocess.Popen:
            if cut == "popen":
                raise OSError("Controlled process-construction failure")
            child = native_popen(*args, **kwargs)
            children.append(child)
            deadline = time.monotonic() + 2
            while not marker.exists() and child.poll() is None:
                assert time.monotonic() < deadline, "Child readiness not observed"
                time.sleep(0.01)
            assert marker.exists() and os.getpgid(child.pid) == child.pid
            identities.append(psutil.Process(child.pid).create_time())
            assert kwargs["stdout"] is logs[0]
            return child

        def capture(process: psutil.Process, **kwargs: Any) -> Any:
            if process.pid != os.getpid() and cut != "transfer":
                if cut == "interrupt":
                    raise KeyboardInterrupt("Controlled child identity interruption")
                raise psutil.AccessDenied(process.pid)
            return native_capture(process, **kwargs)

        def terminate(process: subprocess.Popen) -> None:
            termination_calls.append(process.pid)
            if cut in ("persistent", "optional-persistent") or (
                cut == "oneshot" and len(termination_calls) == 1
            ):
                raise OSError("Controlled process termination failure")
            native_terminate(process, timeout=0.1)

        try:
            with (
                mock.patch.object(Path, "open", autospec=True, side_effect=observe_open),
                mock.patch.object(source.subprocess, "Popen", side_effect=observe_popen),
                mock.patch.object(source, "capture_process_identity", side_effect=capture),
                mock.patch.object(owner, "_terminate_process_tree", side_effect=terminate),
            ):
                if cut == "environment":
                    owner._managed_process_environment = mock.Mock(
                        side_effect=RuntimeError("Controlled environment failure")
                    )
                if cut == "status":
                    owner.status_callback = mock.Mock(
                        side_effect=RuntimeError("Controlled status failure")
                    )
                try:
                    owner.start_all()
                    result["startup_error"] = None
                except BaseException as error:
                    result["startup_error"] = type(error).__name__
            # Remove setup fault before invoking actual final cleanup.
            owner.status_callback = lambda _message: None
            active = False
            status = "absent"
            if children:
                try:
                    actual = psutil.Process(children[0].pid)
                    status = actual.status()
                    active = actual.create_time() == identities[0] and status not in (
                        psutil.STATUS_ZOMBIE,
                        psutil.STATUS_DEAD,
                    )
                except psutil.NoSuchProcess:
                    pass
            result.update(
                children_created=len(children),
                root_active=active,
                root_status=status,
                root_returncode=children[0].returncode if children else None,
                log_closed=logs[0].closed,
                managed_owned=spec.key in owner.processes,
                starting_owned=spec.key in getattr(owner, "starting_processes", {}),
                termination_calls=len(termination_calls),
                lock_acquired=owner.lock.acquired,
                lock_exists=owner.lock.path.exists(),
                ready=owner.ready,
                published_processes=json.loads(owner.runtime_state.read_text())["processes"]
                if owner.runtime_state.exists()
                else {},
            )
            if cut == "transfer":
                managed = owner.processes[spec.key]
                result["verified_identity_exact"] = (
                    managed.process_create_time == identities[0]
                    and managed.executable == psutil.Process(children[0].pid).exe()
                )
        finally:
            owner.status_callback = lambda _message: None
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
            owner.stop_all()
            for handle in logs:
                handle.close()
            result.update(
                roots_reaped=all(child.returncode is not None for child in children),
                final_owner_released=not owner.lock.acquired
                and not owner.processes
                and not getattr(owner, "starting_processes", {}),
            )
    result["root_removed"] = not root.exists()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("cut", choices=CUTS)
    parser.add_argument("--revision")
    args = parser.parse_args()
    print(json.dumps(measure(args.cut, args.revision)))
