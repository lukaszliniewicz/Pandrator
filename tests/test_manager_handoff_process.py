"""Retained handoff ownership excludes replacement and unverified processes."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest import mock

import psutil
import pytest

from pandrator_manager.releases.handoff_process import LaunchedManagerProcess

_PROCESS_CLASS = psutil.Process


def retained_process():
    process = mock.Mock()
    process.pid = 100
    process.returncode = None
    process.poll.side_effect = lambda: process.returncode
    process.wait.side_effect = lambda timeout: process.returncode
    return process


def identity(pid: int, creation: float):
    process = mock.Mock(spec=_PROCESS_CLASS)
    process.pid = pid
    process.create_time.return_value = creation
    process.is_running.return_value = True
    process.children.return_value = []

    def stop():
        process.is_running.return_value = False

    process.terminate.side_effect = stop
    process.kill.side_effect = stop
    return process


@pytest.fixture
def launch(monkeypatch):
    process = retained_process()
    root = identity(100, 1.0)
    child = identity(101, 2.0)
    root.children.return_value = [child]

    def stop_root():
        root.is_running.return_value = False
        process.returncode = 0

    root.terminate.side_effect = stop_root
    root.kill.side_effect = stop_root
    lookup = mock.Mock(return_value=root)
    monkeypatch.setattr(psutil, "Process", lookup)
    wait = mock.Mock(side_effect=lambda processes, timeout: (processes, []))
    monkeypatch.setattr(psutil, "wait_procs", wait)
    return process, root, child, lookup, wait


def test_reaped_root_never_looks_up_or_signals_replacement(launch):
    process, root, child, lookup, _wait = launch
    owner = LaunchedManagerProcess(process)
    process.returncode = 0
    root.is_running.return_value = False
    replacement = identity(100, 99.0)
    lookup.return_value = replacement
    owner.terminate()
    lookup.assert_called_once_with(100)
    replacement.terminate.assert_not_called()
    replacement.kill.assert_not_called()
    child.terminate.assert_called_once()


def test_reused_child_identity_is_never_admitted_or_signaled(launch):
    process, root, child, _lookup, _wait = launch
    owner = LaunchedManagerProcess(process)
    child.is_running.return_value = False
    root.children.return_value = []
    replacement = identity(101, 999.0)
    assert not owner.belongs_to_launch(replacement)
    owner.terminate()
    child.terminate.assert_not_called()
    replacement.terminate.assert_not_called()
    replacement.kill.assert_not_called()


def test_inspection_denial_retains_owner_and_attempts_known_stop(launch):
    process, root, _child, _lookup, _wait = launch
    root.children.side_effect = psutil.AccessDenied(100)
    owner = LaunchedManagerProcess(process)
    assert owner.pid == process.pid
    with pytest.raises(RuntimeError, match="ownership is unresolved"):
        owner.observe()
    with pytest.raises(RuntimeError, match="shutdown is unresolved"):
        owner.terminate()
    root.terminate.assert_called_once()


def test_constructor_discovery_denial_does_not_discard_popen(launch):
    process, _root, _child, lookup, _wait = launch
    lookup.side_effect = psutil.AccessDenied(100)
    owner = LaunchedManagerProcess(process)
    assert owner.poll() is None
    with pytest.raises(RuntimeError):
        owner.observe()
    with pytest.raises(RuntimeError):
        owner.terminate()
    lookup.assert_called_once_with(100)


def test_stubborn_owned_child_prevents_confirmed_shutdown(launch):
    process, _root, child, _lookup, wait = launch
    child.terminate.side_effect = None
    child.kill.side_effect = None
    wait.side_effect = lambda processes, timeout: (
        [],
        [candidate for candidate in processes if candidate.is_running()],
    )
    owner = LaunchedManagerProcess(process)
    with pytest.raises(RuntimeError, match="still running"):
        owner.terminate()
    child.kill.assert_called_once()
    assert [call.kwargs["timeout"] for call in wait.call_args_list] == [10, 10]


def test_termination_denial_is_reported_and_other_owned_processes_are_stopped(launch):
    process, root, child, _lookup, wait = launch
    child.terminate.side_effect = psutil.AccessDenied(child.pid)
    child.kill.side_effect = psutil.AccessDenied(child.pid)
    wait.side_effect = lambda processes, timeout: (
        [],
        [candidate for candidate in processes if candidate.is_running()],
    )
    owner = LaunchedManagerProcess(process)
    with pytest.raises(RuntimeError, match="termination failed"):
        owner.terminate()
    child.terminate.assert_called_once()
    child.kill.assert_called_once()
    root.terminate.assert_called_once()


def test_native_descendants_remain_owned_after_root_exit(launch):
    process, root, child, lookup, _wait = launch
    owner = LaunchedManagerProcess(process, native_launcher=True)
    process.returncode = 0
    root.is_running.return_value = False
    grandchild = identity(102, 3.0)
    child.children.return_value = [grandchild]
    owner.observe()
    assert owner.belongs_to_launch(child)
    assert owner.belongs_to_launch(grandchild)
    owner.terminate()
    lookup.assert_called_once_with(100)
    grandchild.terminate.assert_called_once()
    child.terminate.assert_called_once()
    root.terminate.assert_not_called()


def test_unobserved_native_exit_refuses_confirmed_shutdown(launch):
    process, _root, _child, lookup, _wait = launch
    process.returncode = 0
    owner = LaunchedManagerProcess(process, native_launcher=True)
    with pytest.raises(RuntimeError, match="before a descendant"):
        owner.observe()
    with pytest.raises(RuntimeError, match="before a descendant"):
        owner.terminate()
    lookup.assert_not_called()


def test_exit_during_root_discovery_does_not_cache_numeric_root(launch):
    process, root, _child, lookup, _wait = launch

    def discovery(_pid):
        process.returncode = 0
        return root

    lookup.side_effect = discovery
    owner = LaunchedManagerProcess(process, native_launcher=True)
    with pytest.raises(RuntimeError, match="before a descendant"):
        owner.terminate()
    root.children.assert_not_called()
    root.terminate.assert_not_called()


def test_unresolved_root_reap_is_reported(launch):
    process, _root, _child, _lookup, _wait = launch
    owner = LaunchedManagerProcess(process)
    process.wait.side_effect = subprocess.TimeoutExpired(["fixture"], 1)
    with pytest.raises(RuntimeError, match="could not be reaped"):
        owner.terminate()


def test_children_are_signaled_before_root_and_unowned_identity_is_rejected(launch):
    process, root, child, _lookup, _wait = launch
    owner = LaunchedManagerProcess(process)
    unowned = identity(103, 4.0)
    assert not owner.belongs_to_launch(unowned)
    ordering = mock.Mock()
    ordering.attach_mock(child.terminate, "child")
    ordering.attach_mock(root.terminate, "root")
    owner.terminate()
    assert [call[0] for call in ordering.mock_calls] == ["child", "root"]
    unowned.terminate.assert_not_called()


@pytest.mark.skipif(os.name != "posix", reason="Disposable child reaping uses SIGTERM")
def test_native_python_root_and_child_are_stopped(tmp_path: Path):
    started = time.monotonic()
    ready = tmp_path / "child.json"
    script = tmp_path / "root.py"
    script.write_text(
        "import json,signal,subprocess,sys,time\n"
        "from pathlib import Path\n"
        "import psutil\n"
        "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
        "def retire(signum, frame):\n"
        "    child.wait(timeout=3)\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM,retire)\n"
        "Path(sys.argv[1]).write_text(json.dumps({'pid':child.pid,"
        "'create_time':psutil.Process(child.pid).create_time()}))\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    root = subprocess.Popen([sys.executable, str(script), str(ready)])
    root_witness = psutil.Process(root.pid)
    witnesses: list[tuple[psutil.Process, float]] = [(root_witness, root_witness.create_time())]
    try:
        deadline = started + 5
        while not ready.is_file() and root.poll() is None:
            assert time.monotonic() < deadline, "Disposable root readiness exceeded five seconds"
            time.sleep(0.01)
        assert ready.is_file()
        data = json.loads(ready.read_text(encoding="utf-8"))
        child = psutil.Process(data["pid"])
        assert child.create_time() == data["create_time"]
        witnesses.append((child, data["create_time"]))
        owner = LaunchedManagerProcess(root)
        owner.observe()
        assert owner.belongs_to_launch(child)
        owner.terminate()
        assert root.poll() is not None
        assert not child.is_running()
        assert time.monotonic() - started < 15
    finally:
        # Every cleanup target has a creation witness from this fixture's launch.
        for candidate, creation in reversed(witnesses):
            try:
                if candidate.is_running() and candidate.create_time() == creation:
                    candidate.kill()
            except psutil.NoSuchProcess:
                pass
        psutil.wait_procs([candidate for candidate, _creation in witnesses], timeout=5)
        root.wait(timeout=3)
