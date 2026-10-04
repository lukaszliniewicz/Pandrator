"""Native file coordination and process doubles for installer ownership."""

import contextlib
import io
import json
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest.mock import Mock, patch

import psutil

from pandrator_installer.lifecycle import main
from pandrator_installer.runtime_metadata_files import (
    discard_runtime_metadata,
    read_runtime_metadata,
    runtime_metadata_guard,
)
from pandrator_installer.supervisor import InstanceAlreadyRunning, InstanceLock, ProcessSupervisor

OLD_PID = 2000000001
NEW_PID = 2000000002
EXE = "/fake/metadata/python"


def _lock_payload(pid: int, timestamp: float, instance: str) -> dict[str, object]:
    return {
        "pid": pid,
        "process_create_time": timestamp,
        "executable": EXE,
        "instance_id": instance,
    }


def _replace(path: Path, payload: object) -> bytes:
    data = json.dumps(payload).encode("utf-8")
    temporary = path.with_suffix(".replacement")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    return data


_PUBLISHER = """
import errno, json, os, sys
from pandrator_installer import runtime_metadata_files as files
from pandrator_installer.supervisor import ProcessSupervisor
root, token = sys.argv[1:]
def report(event):
    print(json.dumps({'event':event,'pid':os.getpid(),'token':token}),flush=True)
supervisor = ProcessSupervisor(data_root=root, specs=[])
report('ready')
if sys.stdin.readline().strip() != token:
    raise RuntimeError('Invalid fixture command')
original = files._try_lock
def observe(fd):
    try:
        original(fd)
    except OSError as error:
        if error.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
            report('contended')
        raise
files._try_lock = observe
supervisor._write_state()
report('published')
"""


class InstallerMetadataCoordinationTests(unittest.TestCase):
    def test_native_publisher_waits_for_guard_and_replacement_survives_old_discard(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "runtime-processes.json"
            state.write_text('{"instance_id":"old"}', encoding="utf-8")
            old = read_runtime_metadata(state)
            self.assertIsNotNone(old)
            token = uuid.uuid4().hex
            child = subprocess.Popen(
                [sys.executable, "-c", _PUBLISHER, directory, token],
                cwd=Path(__file__).resolve().parents[1],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            messages: queue.Queue[str | None] = queue.Queue()
            assert child.stdout is not None

            def read_messages() -> None:
                assert child.stdout is not None
                for line in child.stdout:
                    messages.put(line)
                messages.put(None)

            reader = threading.Thread(target=read_messages, daemon=True)
            reader.start()

            def receive(event: str) -> None:
                deadline = time.monotonic() + 5
                while True:
                    line = messages.get(timeout=max(0.001, deadline - time.monotonic()))
                    self.assertIsNotNone(line, "Publisher exited before expected event")
                    assert line is not None
                    message = json.loads(line)
                    self.assertEqual(message["pid"], child.pid)
                    self.assertEqual(message["token"], token)
                    if message["event"] == event:
                        return
                    self.assertLess(time.monotonic(), deadline)

            try:
                receive("ready")
                with runtime_metadata_guard(root):
                    guard_inode = (root / ".runtime-metadata.guard").stat().st_ino
                    assert child.stdin is not None
                    child.stdin.write(token + "\n")
                    child.stdin.flush()
                    receive("contended")
                    self.assertEqual(state.read_text(encoding="utf-8"), '{"instance_id":"old"}')
                receive("published")
                self.assertEqual(child.wait(timeout=5), 0)
                current = state.read_bytes()
                self.assertEqual(json.loads(current)["supervisor_pid"], child.pid)
                assert old is not None
                self.assertFalse(discard_runtime_metadata(old))
                self.assertEqual(state.read_bytes(), current)
                self.assertEqual((root / ".runtime-metadata.guard").stat().st_ino, guard_inode)
            finally:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=5)
                reader.join(timeout=2)
                for stream in (child.stdin, child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
            self.assertFalse(reader.is_alive())

    def test_instance_acquire_preserves_lock_replaced_during_stale_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pandrator.instance.lock"
            path.write_text(json.dumps(_lock_payload(OLD_PID, 1000.0, "old")), encoding="utf-8")
            replacement = json.dumps(_lock_payload(NEW_PID, 2000.0, "new")).encode("utf-8")
            owner = InstanceLock(path)

            def inspect(pid: int) -> Mock:
                if pid == OLD_PID:
                    _replace(path, _lock_payload(NEW_PID, 2000.0, "new"))
                    raise psutil.NoSuchProcess(pid)
                self.assertIn(pid, {os.getpid(), NEW_PID})
                process = Mock(pid=pid)
                process.create_time.return_value = 2000.0
                process.exe.return_value = EXE
                return process

            with (
                patch("pandrator_installer.process_identity.psutil.Process", side_effect=inspect),
                patch(
                    "pandrator_installer.supervisor.psutil.pid_exists", side_effect=AssertionError
                ),
            ):
                with self.assertRaises(InstanceAlreadyRunning):
                    owner.acquire()
            self.assertFalse(owner.acquired)
            self.assertEqual(path.read_bytes(), replacement)

    def test_instance_acquire_refuses_unreadable_existing_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pandrator.instance.lock"
            original = _replace(path, _lock_payload(OLD_PID, 1000.0, "unknown"))
            owner = InstanceLock(path)
            process = Mock(pid=os.getpid())
            process.create_time.return_value = 2000.0
            process.exe.return_value = EXE
            with (
                patch("pandrator_installer.supervisor.psutil.Process", return_value=process),
                patch("pandrator_installer.supervisor.read_runtime_metadata", return_value=None),
            ):
                with self.assertRaises(InstanceAlreadyRunning):
                    owner.acquire()
            self.assertEqual(path.read_bytes(), original)
            self.assertFalse(owner.acquired)

    def test_release_preserves_lock_replaced_after_ownership_read(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pandrator.instance.lock"
            owner = InstanceLock(path)
            owner.acquired = True
            _replace(path, _lock_payload(OLD_PID, 1000.0, owner.instance_id))
            read = read_runtime_metadata
            replacement = json.dumps(_lock_payload(NEW_PID, 2000.0, "new")).encode("utf-8")

            def read_then_replace(target):
                snapshot = read(target)
                _replace(path, _lock_payload(NEW_PID, 2000.0, "new"))
                return snapshot

            with patch(
                "pandrator_installer.supervisor.read_runtime_metadata",
                side_effect=read_then_replace,
            ):
                owner.release()
            self.assertFalse(owner.acquired)
            self.assertEqual(path.read_bytes(), replacement)

    def test_stop_all_preserves_foreign_runtime_state(self):
        with tempfile.TemporaryDirectory() as directory:
            supervisor = ProcessSupervisor(data_root=directory, specs=[])
            state = Path(directory) / "runtime-processes.json"
            original = _replace(state, {"instance_id": "different-owner"})
            supervisor.stop_all()
            self.assertEqual(state.read_bytes(), original)

    def test_cli_stop_preserves_state_replaced_before_stale_or_disappeared_cleanup(self):
        for disappeared in (False, True):
            with self.subTest(disappeared=disappeared), tempfile.TemporaryDirectory() as directory:
                root = Path(directory) / "Pandrator"
                root.mkdir()
                state = root / "runtime-processes.json"
                state.write_text('{"instance_id":"old"}', encoding="utf-8")
                new_state = {"instance_id": "new", "supervisor_pid": NEW_PID}
                replacement = json.dumps(new_state).encode("utf-8")
                process = Mock(pid=OLD_PID)
                process.terminate.side_effect = psutil.NoSuchProcess(OLD_PID)

                def inspect(
                    _paths,
                    _payload,
                    _state=state,
                    _new_state=new_state,
                    _process=process,
                    _disappeared=disappeared,
                ):
                    _replace(_state, _new_state)
                    return _process if _disappeared else None

                output, error = io.StringIO(), io.StringIO()
                with (
                    patch(
                        "pandrator_installer.lifecycle._validated_supervisor_process",
                        side_effect=inspect,
                    ),
                    contextlib.redirect_stdout(output),
                    contextlib.redirect_stderr(error),
                ):
                    code = main(["stop", "--workspace", directory, "--json"])
                self.assertEqual(code, 0, error.getvalue())
                self.assertEqual(json.loads(output.getvalue())["status"], "not_running")
                self.assertEqual(state.read_bytes(), replacement)
