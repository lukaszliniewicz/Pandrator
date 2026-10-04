"""Retain native child ownership until fallback termination is confirmed."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from unittest import mock

import psutil
import pytest

from pandrator_installer.supervisor import ManagedProcess, ManagedProcessSpec, ProcessSupervisor


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[tuple[ProcessSupervisor, ManagedProcess]]:
    marker = tmp_path / "ready"
    script = (
        "import signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        f"Path({str(marker)!r}).write_text('ready'); time.sleep(30)"
    )
    spec = ManagedProcessSpec(
        key="service-fixture",
        label="Owned child fixture",
        command=(sys.executable, "-c", script),
        startup_timeout_seconds=2,
        restart_limit=0,
    )
    owner = ProcessSupervisor(data_root=tmp_path, specs=[spec])
    owner.start_all()
    managed = owner.processes[spec.key]
    child = managed.process
    try:
        assert marker.exists()
        assert os.getpgid(child.pid) == child.pid
        yield owner, managed
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        owner.stop_all()
        managed.log_handle.close()
        assert managed.log_handle.closed
        assert not owner.lock.acquired


@pytest.mark.skipif(os.name == "nt", reason="Native fixture uses a POSIX process group")
def test_fallback_reaps_native_root_before_retiring_resources(
    owned: tuple[ProcessSupervisor, ManagedProcess],
) -> None:
    owner, managed = owned
    child = managed.process
    native_terminate = owner._terminate_process_tree
    with (
        mock.patch.object(psutil, "Process", side_effect=psutil.AccessDenied(child.pid)),
        mock.patch.object(
            owner,
            "_terminate_process_tree",
            side_effect=lambda process: native_terminate(process, timeout=0.01),
        ),
    ):
        owner.stop_all()
    # Reading returncode preserves the distinction from an observer calling poll to reap it.
    assert child.returncode == -signal.SIGKILL
    assert managed.log_handle.closed
    assert not owner.processes and not owner.lock.acquired
    assert not owner.runtime_state.exists()


@pytest.mark.skipif(os.name == "nt", reason="Native fixture uses a POSIX process group")
@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_failed_final_reap_retains_native_process_and_resources(
    owned: tuple[ProcessSupervisor, ManagedProcess], failure: str
) -> None:
    owner, managed = owned
    child = managed.process
    native_terminate = owner._terminate_process_tree
    error = (
        subprocess.TimeoutExpired(child.args, 2) if failure == "timeout" else OSError("reap failed")
    )
    with (
        mock.patch.object(psutil, "Process", side_effect=psutil.AccessDenied(child.pid)),
        mock.patch.object(
            child, "wait", side_effect=[subprocess.TimeoutExpired(child.args, 0.01), error]
        ),
        mock.patch.object(
            owner,
            "_terminate_process_tree",
            side_effect=lambda process: native_terminate(process, timeout=0.01),
        ),
    ):
        with pytest.raises(type(error)):
            owner.stop_all()
    assert owner.processes[managed.spec.key] is managed
    assert not managed.log_handle.closed
    assert owner.lock.acquired and owner.lock.path.exists()
    assert owner.runtime_state.exists()


@pytest.mark.skipif(os.name == "nt", reason="Native fixture uses a POSIX process group")
@pytest.mark.parametrize("failure", ["status", "stop"])
def test_control_stop_failure_retains_child_for_foreground_cleanup(
    owned: tuple[ProcessSupervisor, ManagedProcess], failure: str
) -> None:
    owner, managed = owned
    owner.runtime_control.write_text(json.dumps({"stop_processes": [managed.spec.key]}))
    if failure == "status":
        target, attribute = owner, "status_callback"
    else:
        target, attribute = owner, "_stop_one"
    with mock.patch.object(target, attribute, side_effect=RuntimeError("stop interrupted")):
        with pytest.raises(RuntimeError, match="stop interrupted"):
            owner._apply_control_requests()
    assert managed.process.poll() is None
    assert owner.processes[managed.spec.key] is managed
    assert not managed.log_handle.closed
    assert owner.lock.acquired
    assert owner.runtime_state.exists()
    assert not any(spec.key == managed.spec.key for spec in owner.specs)
