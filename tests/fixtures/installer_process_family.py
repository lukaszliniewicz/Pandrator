"""Disposable Linux subreaper for installer process-group lifetime tests."""

from __future__ import annotations

import argparse
import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import psutil

sys.path.insert(0, str(Path.cwd()))
from pandrator_installer import supervisor


def measure(*, root_ignores_term: bool, hold_reap: bool, revision: str | None) -> dict[str, object]:
    assert sys.platform == "linux"
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, *([ctypes.c_ulong] * 4)]
    libc.prctl.restype = ctypes.c_int
    assert libc.prctl(36, 1, 0, 0, 0) == 0, ctypes.get_errno()
    result: dict[str, object] = {}
    with tempfile.TemporaryDirectory(prefix="pandrator-native-family-") as temporary:
        root = Path(temporary)
        source = supervisor
        if revision is not None:
            snapshot = root / "supervisor.py"
            snapshot.write_bytes(
                subprocess.check_output(
                    ["git", "show", f"{revision}:pandrator_installer/supervisor.py"]
                )
            )
            module_name = "pandrator_installer._family_snapshot"
            spec = importlib.util.spec_from_file_location(module_name, snapshot)
            assert spec is not None and spec.loader is not None
            source = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = source
            spec.loader.exec_module(source)
        marker = root / "grandchild.json"
        descendant_script = (
            "import json,os,signal,time; from pathlib import Path; "
            "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
            f"Path({str(marker)!r}).write_text(json.dumps({{'pid':os.getpid(),'pgid':os.getpgrp()}})); "
            "time.sleep(30)"
        )
        root_script = (
            "import signal,subprocess,sys,time; "
            + ("signal.signal(signal.SIGTERM,signal.SIG_IGN); " if root_ignores_term else "")
            + f"subprocess.Popen([sys.executable,'-c',{descendant_script!r}]); time.sleep(30)"
        )
        managed_spec = source.ManagedProcessSpec(
            key="service-fixture",
            label="Native family fixture",
            command=(sys.executable, "-c", root_script),
            startup_timeout_seconds=2,
            restart_limit=0,
        )
        owner = source.ProcessSupervisor(data_root=root, specs=[managed_spec])
        managed = None
        child = None
        grandchild = None
        reaper = None
        allow_reap = threading.Event()
        reaped: list[int] = []
        if not hold_reap:
            allow_reap.set()
        try:
            owner.start_all()
            managed = owner.processes[managed_spec.key]
            child = managed.process
            identity = json.loads(marker.read_text())
            grandchild = psutil.Process(identity["pid"])
            creation = grandchild.create_time()
            assert identity["pgid"] == child.pid == os.getpgid(child.pid)
            assert grandchild.ppid() == child.pid
            assert grandchild.status() != psutil.STATUS_ZOMBIE

            def reap_descendant() -> None:
                allow_reap.wait()
                while not reaped:
                    try:
                        waited, status = os.waitpid(identity["pid"], os.WNOHANG)
                    except ChildProcessError:
                        # Before root exit the child still belongs to that root.
                        waited, status = 0, 0
                    if waited:
                        reaped.append(os.waitstatus_to_exitcode(status))
                    else:
                        time.sleep(0.01)

            reaper = threading.Thread(target=reap_descendant, daemon=True)
            reaper.start()
            terminate = owner._terminate_process_tree
            started = time.monotonic()
            with (
                mock.patch.object(psutil, "Process", side_effect=psutil.AccessDenied(child.pid)),
                mock.patch.object(
                    owner,
                    "_terminate_process_tree",
                    side_effect=lambda process: terminate(process, timeout=0.1),
                ),
            ):
                try:
                    owner.stop_all()
                    result["stop_error"] = None
                except subprocess.TimeoutExpired:
                    result["stop_error"] = "TimeoutExpired"
            result.update(
                stop_seconds=time.monotonic() - started,
                root_returncode=child.returncode,
                log_closed=managed.log_handle.closed,
                process_map_empty=not owner.processes,
                lock_acquired=owner.lock.acquired,
                state_exists=owner.runtime_state.exists(),
                descendant_reaped_before_retirement=bool(reaped),
            )
            try:
                result["grandchild_status"] = grandchild.status()
                result["grandchild_active"] = (
                    grandchild.create_time() == creation
                    and grandchild.status()
                    not in (
                        psutil.STATUS_ZOMBIE,
                        psutil.STATUS_DEAD,
                    )
                )
            except psutil.NoSuchProcess:
                result.update(grandchild_status="absent", grandchild_active=False)
            if hold_reap and result["stop_error"] is not None:
                # A retry must retain ownership while the same group still exists.
                try:
                    owner.stop_all()
                    result["retry_error"] = None
                except subprocess.TimeoutExpired:
                    result["retry_error"] = "TimeoutExpired"
                result["retry_retained_owner"] = (
                    owner.lock.acquired
                    and managed_spec.key in owner.processes
                    and not managed.log_handle.closed
                )
        finally:
            if grandchild is None and marker.exists():
                grandchild = psutil.Process(json.loads(marker.read_text())["pid"])
            if grandchild is not None:
                try:
                    if grandchild.is_running() and grandchild.status() != psutil.STATUS_ZOMBIE:
                        grandchild.kill()
                except psutil.NoSuchProcess:
                    pass
            if child is not None:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                result["root_reaped"] = child.returncode is not None
            allow_reap.set()
            if reaper is not None:
                reaper.join(timeout=5)
                assert not reaper.is_alive() and reaped
                result.update(grandchild_reaped=True, grandchild_exitcode=reaped[0])
            elif grandchild is not None:
                os.waitpid(grandchild.pid, 0)
                result["grandchild_reaped"] = True
            owner.stop_all()
            if managed is not None:
                managed.log_handle.close()
            result["final_owner_released"] = not owner.lock.acquired and not owner.processes
    result["root_removed"] = not root.exists()
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root-ignores-term", action="store_true")
    parser.add_argument("--hold-reap", action="store_true")
    parser.add_argument("--revision")
    args = parser.parse_args()
    print(
        json.dumps(
            measure(
                root_ignores_term=args.root_ignores_term,
                hold_reap=args.hold_reap,
                revision=args.revision,
            )
        )
    )
