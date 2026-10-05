"""Backend shutdown keeps native child ownership until root exit is confirmed."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import TextIO
from unittest import mock

import psutil
import pytest

from pandrator_installer import components
from pandrator_installer.service import HeadlessInstaller

pytestmark = pytest.mark.skipif(
    sys.platform != "linux", reason="Native backend fixture uses Linux SIGTERM handling"
)


@pytest.fixture(autouse=True)
def immutable_before() -> Iterator[None]:
    """Optional source snapshot exercises regressions without changing product files."""
    revision = os.environ.get("PANDRATOR_TEST_BACKEND_REVISION")
    if revision is None:
        yield
        return
    modules = []
    for name in ("components", "runtime"):
        module = ModuleType(f"pandrator_installer._backend_before_{name}")
        module.__package__ = "pandrator_installer"
        source = subprocess.check_output(
            ["git", "show", f"{revision}:pandrator_installer/{name}.py"], text=True
        )
        exec(compile(source, f"{revision}:{name}.py", "exec"), module.__dict__)
        modules.append(module)
    with (
        mock.patch.object(
            HeadlessInstaller,
            "terminate_process_tree",
            modules[0].ComponentOperationsMixin.terminate_process_tree,
        ),
        mock.patch.object(
            HeadlessInstaller,
            "_shutdown_owned_backend",
            modules[1].RuntimeMixin._shutdown_owned_backend,
        ),
        # Keep the old function's psutil binding patchable through the same target.
        mock.patch.object(components, "terminate_before_module", modules[0], create=True),
    ):
        yield


class NativeChild(subprocess.Popen[str]):
    log_handle: TextIO | None


@dataclass
class Ownership:
    installer: HeadlessInstaller
    child: NativeChild
    log: TextIO


@pytest.fixture
def owned(tmp_path: Path) -> Iterator[Ownership]:
    marker = tmp_path / "ready"
    script = (
        "import signal,time;from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        f"Path({str(marker)!r}).write_text('ready');time.sleep(30)"
    )
    installer = HeadlessInstaller(working_dir=tmp_path)
    log = (tmp_path / "child.log").open("a", encoding="utf-8")
    child = NativeChild(
        [sys.executable, "-c", script], stdout=log, stderr=subprocess.STDOUT, text=True
    )
    child.log_handle = log
    installer.xtts_process = child
    # The legacy method accepts fractional grace periods but infers an int default.
    native_stop: Callable[..., None] = installer.terminate_process_tree
    try:
        deadline = time.monotonic() + 2
        while not marker.exists() and child.poll() is None:
            assert time.monotonic() < deadline, "Fixture child did not become ready"
            time.sleep(0.01)
        assert marker.exists()
        with mock.patch.object(
            installer,
            "terminate_process_tree",
            side_effect=lambda process: native_stop(process, timeout=0.01),
        ):
            yield Ownership(installer, child, log)
    finally:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=5)
        installer.shutdown_apps()
        log.close()
        assert child.returncode is not None and log.closed
        assert installer.xtts_process is None


def psutil_lookup(lookup: object):
    """Patch only a module binding; psutil's native Process class stays intact."""
    proxy = SimpleNamespace(
        Process=lookup,
        NoSuchProcess=psutil.NoSuchProcess,
        AccessDenied=psutil.AccessDenied,
        wait_procs=psutil.wait_procs,
    )
    target = getattr(components, "terminate_before_module", components)
    return mock.patch.object(target, "psutil", proxy)


def assert_retained(owned: Ownership) -> None:
    assert owned.installer.xtts_process is owned.child
    assert owned.child.log_handle is owned.log and not owned.log.closed


def assert_completed(owned: Ownership) -> None:
    assert owned.child.returncode is not None
    assert owned.installer.xtts_process is None
    assert owned.log.closed and owned.child.log_handle is None


def test_normal_shutdown_reaps_native_child_before_retirement(owned: Ownership) -> None:
    owned.installer.shutdown_xtts()
    assert_completed(owned)


@pytest.mark.parametrize("error_type", [OSError, KeyboardInterrupt])
def test_termination_failure_retains_live_child_and_log_for_retry(
    owned: Ownership, error_type: type[BaseException]
) -> None:
    creation = psutil.Process(owned.child.pid).create_time()
    with mock.patch.object(
        owned.installer, "terminate_process_tree", side_effect=error_type("stop failed")
    ):
        with pytest.raises(error_type, match="stop failed"):
            owned.installer.shutdown_xtts()
    assert_retained(owned)
    assert owned.child.poll() is None
    assert psutil.Process(owned.child.pid).create_time() == creation
    owned.installer.shutdown_xtts()
    assert_completed(owned)


@pytest.mark.parametrize("failure", ["timeout", "error"])
def test_missing_lookup_requires_native_root_wait_before_retirement(
    owned: Ownership, failure: str
) -> None:
    lookup = mock.Mock(side_effect=psutil.NoSuchProcess(owned.child.pid))
    with psutil_lookup(lookup):
        if failure == "timeout":
            # This is an actual Popen wait deadline on a still-running native child.
            with pytest.raises(subprocess.TimeoutExpired):
                owned.installer.shutdown_xtts()
        else:
            with mock.patch.object(owned.child, "wait", side_effect=OSError("wait failed")):
                with pytest.raises(OSError, match="wait failed"):
                    owned.installer.shutdown_xtts()
    assert_retained(owned)
    assert owned.child.poll() is None
    owned.installer.shutdown_xtts()
    assert_completed(owned)


def test_final_wait_error_retains_resources_until_confirmed_retry(owned: Ownership) -> None:
    with mock.patch.object(owned.child, "wait", side_effect=OSError("final wait failed")):
        with pytest.raises(OSError, match="final wait failed"):
            owned.installer.shutdown_xtts()
    # psutil may already have reaped the root; the injected wait error must still propagate.
    assert_retained(owned)
    assert owned.child.poll() is not None
    with psutil_lookup(mock.Mock(side_effect=AssertionError("reaped PID queried"))):
        owned.installer.shutdown_xtts()
    assert_completed(owned)


def test_reaped_handle_never_targets_controlled_stale_pid_fixture(owned: Ownership) -> None:
    stale = NativeChild([sys.executable, "-c", "pass"], text=True)
    stale.wait(timeout=2)
    creation = psutil.Process(owned.child.pid).create_time()
    native_lookup = psutil.Process
    lookups: list[int] = []

    def lookup(pid: int) -> psutil.Process:
        lookups.append(pid)
        # A controlled lookup proxy, not a claim of native kernel PID reuse.
        return native_lookup(owned.child.pid if pid == stale.pid else pid)

    with psutil_lookup(lookup):
        owned.installer.terminate_process_tree(stale)
    assert stale.pid not in lookups
    assert owned.child.poll() is None
    assert psutil.Process(owned.child.pid).create_time() == creation
    assert_retained(owned)


def test_poll_error_retains_live_child_without_numeric_lookup(owned: Ownership) -> None:
    with (
        mock.patch.object(owned.child, "poll", side_effect=OSError("poll failed")),
        psutil_lookup(mock.Mock(side_effect=AssertionError("uncertain PID queried"))),
    ):
        with pytest.raises(OSError, match="poll failed"):
            owned.installer.shutdown_xtts()
    assert_retained(owned)
    assert owned.child.poll() is None
    owned.installer.shutdown_xtts()
    assert_completed(owned)


@pytest.mark.parametrize("lookup", ["backends", "rvc"])
def test_discovery_poll_error_preserves_live_process_and_log(
    owned: Ownership, lookup: str
) -> None:
    if lookup == "rvc":
        owned.installer.xtts_process = None
        owned.installer.rvc_process = owned.child
    with mock.patch.object(owned.child, "poll", side_effect=OSError("poll failed")):
        if lookup == "rvc":
            assert owned.installer._get_running_rvc_process() is owned.child
            assert owned.installer.rvc_process is owned.child
        else:
            running = owned.installer._collect_running_backends()
            assert any(item[0] == "xtts" and item[2] is owned.child for item in running)
            assert owned.installer.xtts_process is owned.child
        assert owned.child.log_handle is owned.log and not owned.log.closed
    assert owned.child.poll() is None
