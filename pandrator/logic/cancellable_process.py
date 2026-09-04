"""Small subprocess runner with cooperative cancellation for worker-owned tools."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import threading
from collections.abc import Sequence
from typing import Any


class ProcessCancelled(RuntimeError):
    """Raised when a worker cancellation stops a child process."""


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    """Terminate only the child process tree created for this invocation."""

    if process.poll() is not None:
        return
    try:
        if os.name == "nt":
            process.terminate()
        else:
            os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=2.0)
        return
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass
    try:
        if os.name == "nt":
            process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=2.0)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        pass


def run_cancellable(
    command: Sequence[str | os.PathLike[str]],
    *,
    cancel_event: threading.Event | None = None,
    check: bool = False,
    capture_output: bool = False,
    text: bool = False,
    **kwargs: Any,
) -> subprocess.CompletedProcess[Any]:
    """Run a child in its own process group and stop it when cancellation is requested."""

    if cancel_event is None:
        return subprocess.run(
            command,
            check=check,
            capture_output=capture_output,
            text=text,
            **kwargs,
        )
    if cancel_event.is_set():
        raise ProcessCancelled("Process was canceled.")

    normalized = [os.fspath(value) for value in command]
    creationflags = int(kwargs.pop("creationflags", 0))
    if os.name == "nt":
        creationflags |= getattr(subprocess, "CREATE_NO_WINDOW", 0)
        creationflags |= getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    start_new_session = bool(kwargs.pop("start_new_session", os.name != "nt"))

    with (
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        process = subprocess.Popen(
            normalized,
            stdin=kwargs.pop("stdin", subprocess.DEVNULL),
            stdout=stdout_file
            if capture_output
            else kwargs.pop("stdout", subprocess.DEVNULL),
            stderr=stderr_file
            if capture_output
            else kwargs.pop("stderr", subprocess.DEVNULL),
            creationflags=creationflags,
            start_new_session=start_new_session,
            **kwargs,
        )
        try:
            while process.poll() is None:
                if cancel_event.wait(0.1):
                    _stop_process(process)
                    raise ProcessCancelled("Process was canceled.")
        except BaseException:
            _stop_process(process)
            raise

        stdout: str | bytes | None = None
        stderr: str | bytes | None = None
        if capture_output:
            stdout_file.seek(0)
            stderr_file.seek(0)
            stdout_bytes = stdout_file.read()
            stderr_bytes = stderr_file.read()
            if text:
                stdout = stdout_bytes.decode("utf-8", errors="replace")
                stderr = stderr_bytes.decode("utf-8", errors="replace")
            else:
                stdout = stdout_bytes
                stderr = stderr_bytes
        completed = subprocess.CompletedProcess(
            normalized,
            int(process.returncode or 0),
            stdout=stdout,
            stderr=stderr,
        )
        if check and completed.returncode:
            raise subprocess.CalledProcessError(
                completed.returncode,
                normalized,
                output=stdout,
                stderr=stderr,
            )
        return completed
