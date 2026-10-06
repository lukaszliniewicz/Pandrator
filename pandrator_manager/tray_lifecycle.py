"""Workspace tray process identity and shutdown, independent of the desktop UI."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path

import psutil

from .context import WorkspaceLayout


def _tray_instance_path(layout: WorkspaceLayout) -> Path:
    return layout.state / "tray.pid"



def stop_tray_background(
    layout: WorkspaceLayout,
    *,
    timeout_seconds: float = 10,
) -> tuple[bool, str]:
    """Stop this workspace's tray without risking an unrelated reused PID."""

    path = _tray_instance_path(layout)
    try:
        raw_identity = path.read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return False, ""
    try:
        decoded = json.loads(raw_identity)
        if isinstance(decoded, dict):
            pid = int(decoded["pid"])
            expected_create_time = float(decoded["create_time"])
        else:
            pid = int(decoded)
            expected_create_time = None
    except (KeyError, TypeError, ValueError, json.JSONDecodeError):
        return False, "The tray process identity file is invalid."

    try:
        process = psutil.Process(pid)
        actual_create_time = process.create_time()
        command = process.cmdline()
    except psutil.NoSuchProcess:
        path.unlink(missing_ok=True)
        return False, ""
    except (OSError, psutil.Error) as error:
        return False, f"The tray process identity could not be inspected: {error}"

    if (
        expected_create_time is not None
        and abs(actual_create_time - expected_create_time) > 1e-3
    ):
        return False, "The tray process identity no longer matches."
    normalized = [str(argument).casefold() for argument in command]
    workspace = str(layout.workspace).casefold()
    is_tray = "tray" in normalized or "pandrator_manager.tray" in normalized
    has_workspace = any(argument == workspace for argument in normalized)
    if not is_tray or not has_workspace:
        return False, "The tray process command no longer matches this workspace."

    try:
        process.terminate()
        _gone, alive = psutil.wait_procs(
            [process],
            timeout=max(0.1, timeout_seconds),
        )
        for remaining in alive:
            remaining.kill()
        if alive:
            _gone, still_alive = psutil.wait_procs(
                alive,
                timeout=max(0.1, timeout_seconds),
            )
            if still_alive:
                return False, "The desktop tray did not exit before uninstall."
    except psutil.NoSuchProcess:
        pass
    except (OSError, psutil.Error) as error:
        return False, f"The desktop tray could not be stopped: {error}"
    path.unlink(missing_ok=True)
    return True, ""



def _claim_tray_instance(layout: WorkspaceLayout) -> Path | None:
    layout.state.mkdir(parents=True, exist_ok=True)
    path = _tray_instance_path(layout)
    for _attempt in range(2):
        try:
            descriptor = os.open(
                path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
                0o600,
            )
        except FileExistsError:
            decoded = None
            try:
                raw_identity = path.read_text(encoding="ascii").strip()
                decoded = json.loads(raw_identity)
                pid = (
                    int(decoded["pid"])
                    if isinstance(decoded, dict)
                    else int(decoded)
                )
            except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
                pid = 0
            if pid > 0 and psutil.pid_exists(pid):
                # Modern identity records distinguish a live tray from a PID
                # reused after the original process exited. Legacy PID-only
                # records and inaccessible processes remain conservative.
                if not isinstance(decoded, dict):
                    return None
                try:
                    expected_create_time = float(decoded["create_time"])
                    actual_create_time = psutil.Process(pid).create_time()
                except psutil.NoSuchProcess:
                    pass
                except (KeyError, TypeError, ValueError, OverflowError, OSError, psutil.Error):
                    return None
                else:
                    if (
                        not math.isfinite(expected_create_time)
                        or abs(actual_create_time - expected_create_time) <= 1e-3
                    ):
                        return None
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(
                json.dumps(
                    {
                        "pid": os.getpid(),
                        "create_time": psutil.Process().create_time(),
                    },
                    sort_keys=True,
                )
            )
        return path
    return None
