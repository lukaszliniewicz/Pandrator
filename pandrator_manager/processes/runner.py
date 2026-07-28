"""Bounded, cancellable, shell-free subprocess execution."""

from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Mapping

import psutil

from ..context import CancellationToken
from ..errors import CancellationRequested


@dataclass(frozen=True, slots=True)
class CommandSpec:
    argv: tuple[str, ...]
    cwd: Path | None = None
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: float = 2 * 60 * 60
    output_limit_bytes: int = 2 * 1024 * 1024
    label: str = "command"
    redacted_arguments: frozenset[int] = frozenset()

    def __post_init__(self) -> None:
        if not self.argv or not all(isinstance(value, str) and value for value in self.argv):
            raise ValueError("Command argv must be a non-empty string tuple.")
        if self.timeout_seconds <= 0:
            raise ValueError("Command timeout must be positive.")
        if self.output_limit_bytes < 1024:
            raise ValueError("Command output limit must be at least 1024 bytes.")

    def display(self) -> str:
        return " ".join(
            "<redacted>" if index in self.redacted_arguments else argument
            for index, argument in enumerate(self.argv)
        )


@dataclass(frozen=True, slots=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    duration_seconds: float
    output_truncated: bool


class _BoundedOutput:
    def __init__(self, limit_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self._chunks: deque[bytes] = deque()
        self._size = 0
        self.truncated = False
        self._lock = threading.Lock()

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            while self._size > self.limit_bytes and self._chunks:
                removed = self._chunks.popleft()
                self._size -= len(removed)
                self.truncated = True

    def text(self) -> str:
        with self._lock:
            payload = b"".join(self._chunks)
        return payload.decode("utf-8", errors="replace")


class CommandRunner:
    def __init__(
        self,
        *,
        cancellation: CancellationToken | None = None,
        base_environment: Mapping[str, str] | None = None,
    ) -> None:
        self.cancellation = cancellation or CancellationToken()
        self.base_environment = dict(base_environment or os.environ)

    @staticmethod
    def _popen_options() -> dict:
        if os.name == "nt":
            return {
                "creationflags": (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
            }
        return {"start_new_session": True}

    def environment(self, additions: Mapping[str, str]) -> dict[str, str]:
        environment = dict(self.base_environment)
        environment.update({str(key): str(value) for key, value in additions.items()})
        environment.pop("_MEIPASS2", None)
        environment.pop("PYTHONHOME", None)
        if getattr(__import__("sys"), "frozen", False):
            environment.pop("LD_LIBRARY_PATH", None)
            environment.pop("DYLD_LIBRARY_PATH", None)
        return environment

    @staticmethod
    def _read_stream(stream: BinaryIO, output: _BoundedOutput) -> None:
        try:
            while True:
                chunk = stream.read(65536)
                if not chunk:
                    return
                output.append(chunk)
        except (OSError, ValueError):
            return

    @staticmethod
    def terminate_tree(process: subprocess.Popen, *, timeout: float = 10.0) -> None:
        try:
            parent = psutil.Process(process.pid)
            processes = [*parent.children(recursive=True), parent]
        except psutil.NoSuchProcess:
            processes = []
        for owned in reversed(processes):
            try:
                owned.terminate()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        _, alive = psutil.wait_procs(processes, timeout=max(0.1, timeout))
        for owned in alive:
            try:
                owned.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=5)
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            pass

    def run(self, spec: CommandSpec, *, check: bool = True) -> CommandResult:
        self.cancellation.raise_if_requested()
        started = time.monotonic()
        stdout_buffer = _BoundedOutput(spec.output_limit_bytes)
        stderr_buffer = _BoundedOutput(spec.output_limit_bytes)
        logging.info("Running %s: %s", spec.label, spec.display())
        process = subprocess.Popen(
            list(spec.argv),
            cwd=str(spec.cwd) if spec.cwd else None,
            env=self.environment(spec.env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            **self._popen_options(),
        )
        stdout_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stdout, stdout_buffer),
            name=f"{spec.label}-stdout",
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=self._read_stream,
            args=(process.stderr, stderr_buffer),
            name=f"{spec.label}-stderr",
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        deadline = started + spec.timeout_seconds
        failure: BaseException | None = None
        try:
            while process.poll() is None:
                if self.cancellation.requested:
                    failure = CancellationRequested()
                    break
                if time.monotonic() >= deadline:
                    failure = subprocess.TimeoutExpired(
                        spec.argv,
                        spec.timeout_seconds,
                    )
                    break
                time.sleep(0.05)
            if failure is not None:
                self.terminate_tree(process)
        finally:
            stdout_thread.join(timeout=10)
            stderr_thread.join(timeout=10)
            if process.poll() is None:
                self.terminate_tree(process)
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
            stdout_thread.join(timeout=1)
            stderr_thread.join(timeout=1)

        stdout = stdout_buffer.text()
        stderr = stderr_buffer.text()
        duration = time.monotonic() - started
        if isinstance(failure, subprocess.TimeoutExpired):
            failure.output = stdout
            failure.stderr = stderr
            raise failure
        if failure is not None:
            raise failure

        result = CommandResult(
            argv=spec.argv,
            returncode=int(process.returncode),
            stdout=stdout,
            stderr=stderr,
            duration_seconds=duration,
            output_truncated=stdout_buffer.truncated or stderr_buffer.truncated,
        )
        if check and result.returncode != 0:
            raise subprocess.CalledProcessError(
                result.returncode,
                spec.argv,
                output=result.stdout,
                stderr=result.stderr,
            )
        return result
