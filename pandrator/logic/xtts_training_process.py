"""Stream trainer output while retaining ownership of its process lifetime."""

from __future__ import annotations

import codecs
import logging
import os
import queue
import signal
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from typing import BinaryIO

import psutil

from .cancellable_process import ProcessCancelled

_TOKEN_VARIABLE = "PANDRATOR_XTTS_TRAINING_TOKEN"
_LOG = logging.getLogger(__name__)
_OutputItem = bytes | BaseException | None


def _alive(process: psutil.Process) -> bool:
    try:
        return process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.NoSuchProcess:
        return False
    except psutil.AccessDenied:
        # A positively owned process remains our responsibility when inspection fails.
        return process.is_running()


class _OwnedFamily:
    def __init__(self, process: subprocess.Popen[bytes], token: str) -> None:
        self.process = process
        self.token = token
        self.members: dict[int, psutil.Process] = {}
        self.root: psutil.Process | None = None
        try:
            root = psutil.Process(process.pid)
            root.create_time()
            self.root = root
            self.members[root.pid] = root
        except psutil.NoSuchProcess:
            pass

    def refresh(self) -> list[psutil.Process]:
        root = self.root
        if root is not None and root.is_running():
            try:
                for child in root.children(recursive=True):
                    child.create_time()
                    self.members[child.pid] = child
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied as error:
                raise RuntimeError("Cannot inspect the owned trainer process family.") from error
        group_current = False
        if os.name != "nt":
            try:
                current_root = psutil.Process(self.process.pid)
                group_current = (
                    root is not None and current_root.create_time() == root.create_time()
                )
            except psutil.NoSuchProcess:
                # A live isolated group retains its identity after the leader exits.
                group_current = True
        # Fresh instances avoid process_iter's cached identities and environments.
        for pid in psutil.pids():
            try:
                candidate = psutil.Process(pid)
                candidate.create_time()
                if group_current:
                    try:
                        if os.getpgid(pid) == self.process.pid:
                            self.members[pid] = candidate
                    except (ProcessLookupError, PermissionError):
                        pass
                if candidate.environ().get(_TOKEN_VARIABLE) == self.token:
                    self.members[pid] = candidate
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                # Visibility of unrelated processes is not required for ownership.
                continue
        return [member for member in self.members.values() if _alive(member)]

    def signal(self, members: list[psutil.Process], *, kill: bool) -> None:
        if os.name != "nt":
            # The isolated group can outlive its launcher. Guard against a reused
            # group ID by requiring a still-current, positively owned member.
            for member in members:
                if not _alive(member):
                    continue
                try:
                    if os.getpgid(member.pid) == self.process.pid:
                        os.killpg(self.process.pid, signal.SIGKILL if kill else signal.SIGTERM)
                        break
                except ProcessLookupError:
                    continue
                except PermissionError as error:
                    raise RuntimeError("Cannot signal the owned trainer process group.") from error
        for member in members:
            if not _alive(member):
                continue
            try:
                if kill:
                    member.kill()
                else:
                    member.terminate()
            except psutil.NoSuchProcess:
                pass
            except psutil.AccessDenied as error:
                raise RuntimeError("Cannot signal an owned trainer process.") from error

    def stop(self) -> None:
        members = self.refresh()
        for kill in (False, True):
            if not members:
                break
            signaled: set[tuple[int, float]] = set()
            deadline = time.monotonic() + 2.0
            self.signal(members, kill=kill)
            signaled.update((member.pid, member.create_time()) for member in members)
            while True:
                members = self.refresh()
                if not members or time.monotonic() >= deadline:
                    break
                # Newly discovered children must receive this phase's signal too.
                new_members = [
                    member
                    for member in members
                    if (member.pid, member.create_time()) not in signaled
                ]
                self.signal(new_members, kill=kill)
                signaled.update((member.pid, member.create_time()) for member in new_members)
                time.sleep(0.05)
        if members:
            raise RuntimeError("Owned trainer processes survived termination.")
        try:
            self.process.wait(timeout=2.0)
        except subprocess.TimeoutExpired as error:
            raise RuntimeError("The trainer launcher could not be reaped.") from error


def _read_output(pipe: BinaryIO, output: queue.Queue[_OutputItem], stop: threading.Event) -> None:
    def put(item: _OutputItem) -> None:
        while not stop.is_set():
            try:
                output.put(item, timeout=0.1)
                return
            except queue.Full:
                continue

    try:
        while not stop.is_set():
            chunk = pipe.read(64 * 1024)
            if not chunk:
                put(None)
                return
            put(chunk)
    except BaseException as error:
        put(error)
    finally:
        # If bounded teardown could not join this reader, it still releases its
        # descriptor when an externally retained writer eventually disappears.
        try:
            pipe.close()
        except BaseException as error:
            put(error)


class _OutputLines:
    def __init__(
        self, callback: Callable[[str], None] | None, cancel_event: threading.Event | None
    ) -> None:
        self.callback = callback
        self.cancel_event = cancel_event
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self.pending = ""

    def feed(self, chunk: bytes, *, final: bool = False) -> None:
        text = self.pending + self.decoder.decode(chunk, final=final)
        start = 0
        index = 0
        while index < len(text):
            character = text[index]
            if character not in "\r\n":
                index += 1
                continue
            if character == "\r" and index + 1 == len(text) and not final:
                break
            self.deliver(text[start:index])
            index += 1
            if character == "\r" and index < len(text) and text[index] == "\n":
                index += 1
            start = index
        self.pending = text[start:]
        if final:
            self.deliver(self.pending)
            self.pending = ""

    def deliver(self, line: str) -> None:
        line = line.strip()
        if line:
            if self.cancel_event is not None and self.cancel_event.is_set():
                raise ProcessCancelled("Process was canceled.")
            if self.callback is not None:
                self.callback(line)


def run_training_process(
    command: Sequence[str],
    *,
    cwd: str,
    env: dict[str, str],
    cancel_event: threading.Event | None,
    output_callback: Callable[[str], None] | None,
) -> int:
    """Run a trainer, stream lines on the caller thread, and clean its family.

    Environment markers recover detached descendants where their environment is
    visible. Children that remove the marker and detach beyond a vanished root's
    ancestry are not discoverable; this is not a process containment sandbox.
    The post-exit EOF timeout measures output inactivity: a continuously noisy,
    undiscoverable pipe writer can keep output draining until cancellation.
    """
    if cancel_event is not None and cancel_event.is_set():
        raise ProcessCancelled("Process was canceled.")
    token = uuid.uuid4().hex
    child_env = dict(env)
    child_env[_TOKEN_VARIABLE] = token
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    output: queue.Queue[_OutputItem] = queue.Queue(maxsize=8)
    reader_stop = threading.Event()
    reader: threading.Thread | None = None
    family: _OwnedFamily | None = None
    cleanup_attempted = False
    primary: BaseException | None = None
    reader_started = False
    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=child_env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        bufsize=0,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
    )
    pipe = process.stdout
    assert pipe is not None
    try:
        family = _OwnedFamily(process, token)
        reader = threading.Thread(
            target=_read_output,
            args=(pipe, output, reader_stop),
            name=f"xtts-training-output-{process.pid}",
            daemon=True,
        )
        reader.start()
        reader_started = True
        lines = _OutputLines(output_callback, cancel_event)
        eof = False
        drain_deadline: float | None = None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise ProcessCancelled("Process was canceled.")
            returncode = process.poll()
            if returncode is not None and not cleanup_attempted:
                cleanup_attempted = True
                family.stop()
                drain_deadline = time.monotonic() + 2.0
            if eof and returncode is not None:
                return returncode
            try:
                item = output.get(timeout=0.1)
            except queue.Empty:
                if drain_deadline is not None and time.monotonic() >= drain_deadline:
                    raise RuntimeError(
                        "The trainer output pipe remained open after process cleanup."
                    ) from None
                continue
            if item is None:
                eof = True
                lines.feed(b"", final=True)
            elif isinstance(item, BaseException):
                raise item
            else:
                lines.feed(item)
                if drain_deadline is not None:
                    drain_deadline = time.monotonic() + 2.0
    except BaseException as error:
        primary = error
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        if not cleanup_attempted:
            try:
                if family is None:
                    family = _OwnedFamily(process, token)
                family.stop()
            except BaseException as error:
                cleanup_errors.append(error)
        reader_stop.set()
        if reader_started and reader is not None:
            try:
                reader.join(timeout=2.0)
            except BaseException as error:
                cleanup_errors.append(error)
        if reader is not None and reader.is_alive():
            cleanup_errors.append(RuntimeError("The trainer output reader could not be stopped."))
        else:
            # Closing a pipe while another thread reads it can block on an IO lock.
            try:
                pipe.close()
            except BaseException as error:
                cleanup_errors.append(error)
        if cleanup_errors:
            if primary is not None:
                _LOG.warning("XTTS training cleanup failed; owned resources could not be stopped.")
            else:
                raise cleanup_errors[0]
