"""Tray launch options and owned-child retirement without a desktop session."""

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

import pytest

from pandrator_manager import tray
from pandrator_manager.context import WorkspaceLayout


@pytest.mark.parametrize("platform", ["linux", "win32"])
def test_tray_launch_preserves_platform_options_and_keeps_a_waiter(platform):
    process = mock.Mock(pid=123)
    environment = {"PATH": "/host/tools"}
    command = ("launcher", "tray", "--workspace", "/workspace")
    with (
        mock.patch.object(tray, "tray_available", return_value=(True, "")),
        mock.patch.object(tray, "_tray_command", return_value=command),
        mock.patch.object(tray, "host_process_environment", return_value=environment),
        mock.patch.object(tray.sys, "platform", platform),
        mock.patch.object(tray.subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, create=True),
        mock.patch.object(tray.subprocess, "DETACHED_PROCESS", 0x8, create=True),
        mock.patch.object(tray.subprocess, "CREATE_NO_WINDOW", 0x8000000, create=True),
        mock.patch.object(tray.subprocess, "Popen", return_value=process) as launch,
        mock.patch.object(tray.threading, "Thread") as thread,
    ):
        assert tray.launch_tray_background(WorkspaceLayout.from_value("/workspace")) == (True, "")
    expected = {
        "env": environment,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if platform == "win32":
        expected["creationflags"] = 0x8000208
    else:
        expected["start_new_session"] = True
    launch.assert_called_once_with(command, **expected)
    thread.assert_called_once_with(
        target=process.wait, name="pandrator-tray-reaper-123", daemon=True
    )
    thread.return_value.start.assert_called_once_with()


def test_unavailable_or_failed_tray_launch_does_not_create_a_waiter():
    layout = WorkspaceLayout.from_value("/workspace")
    with (
        mock.patch.object(tray, "tray_available", return_value=(False, "headless")),
        mock.patch.object(tray.subprocess, "Popen") as launch,
        mock.patch.object(tray.threading, "Thread") as thread,
    ):
        assert tray.launch_tray_background(layout) == (False, "headless")
        launch.assert_not_called()
        thread.assert_not_called()
    with (
        mock.patch.object(tray, "tray_available", return_value=(True, "")),
        mock.patch.object(tray, "_tray_command", return_value=("launcher",)),
        mock.patch.object(tray.subprocess, "Popen", side_effect=OSError("launch failed")),
        mock.patch.object(tray.threading, "Thread") as thread,
    ):
        assert tray.launch_tray_background(layout) == (False, "launch failed")
        thread.assert_not_called()


def test_native_tray_child_is_reaped_after_exit():
    # Keep an actual Popen reference and observe returncode rather than poll(),
    # which would itself reap the child and hide the missing-waiter regression.
    processes = []
    original_popen = subprocess.Popen

    def capture(*args, **kwargs):
        process = original_popen(*args, **kwargs)
        processes.append(process)
        return process

    with tempfile.TemporaryDirectory(prefix="tray-reaper-") as directory:
        layout = WorkspaceLayout.from_value(Path(directory) / "workspace")
        with (
            mock.patch.object(tray, "tray_available", return_value=(True, "")),
            mock.patch.object(
                tray,
                "_tray_command",
                return_value=(sys.executable, "-c", "import time; time.sleep(0.1)"),
            ),
            mock.patch.object(tray.subprocess, "Popen", side_effect=capture),
        ):
            assert tray.launch_tray_background(layout) == (True, "")
        process = processes[0]
        try:
            deadline = time.monotonic() + 5
            while process.returncode is None and time.monotonic() < deadline:
                time.sleep(0.01)
            assert process.returncode == 0
            waiter_name = f"pandrator-tray-reaper-{process.pid}"
            for waiter in threading.enumerate():
                if waiter.name == waiter_name:
                    waiter.join(timeout=1)
                    assert not waiter.is_alive()
        finally:
            if process.returncode is None:
                process.kill()
            process.wait(timeout=5)
