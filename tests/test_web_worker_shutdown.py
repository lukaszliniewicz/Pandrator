"""Real CLI worker shutdown and completion of its current durable job."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from pandrator.runtime import DataPaths
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.legacy_migration import import_legacy_data

REPO = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "Windows process termination does not deliver POSIX SIGTERM")
class WorkerShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pandrator-worker-shutdown-")
        self.addCleanup(temporary.cleanup)
        self.paths = DataPaths.from_value(temporary.name).ensure()
        import_legacy_data(self.paths)
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.queue = JobQueue(self.database)
        self.children: list[subprocess.Popen] = []
        self.addCleanup(self._stop_children)

    def _stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)

    def _start_worker(self, *, poll_interval=0.05):
        log = (self.paths.root / "worker.log").open("w")
        self.addCleanup(log.close)
        child = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "pandrator",
                "--data-dir",
                str(self.paths.root),
                "worker",
                "--worker-id",
                "shutdown-fixture",
                "--poll-interval",
                str(poll_interval),
            ],
            cwd=REPO,
            stdout=log,
            stderr=log,
        )
        self.children.append(child)
        return child

    def _wait_job(self, child, job_id, state, timeout=20):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            job = self.queue.get(job_id)
            if job.status == state:
                return job
            if child.poll() is not None:
                self.fail(
                    f"Worker exited {child.returncode}: {(self.paths.root / 'worker.log').read_text()}"
                )
            time.sleep(0.02)
        self.fail(f"Worker did not reach job state {state}")

    def test_sigterm_finishes_current_job_and_leaves_next_job_queued(self):
        current = self.queue.enqueue("noop", {"duration": 1.5, "echo": "finish current"})
        following = self.queue.enqueue("noop", {"echo": "leave queued"})
        child = self._start_worker()
        self._wait_job(child, current.id, "running")
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), 0)
        finished = self.queue.get(current.id)
        self.assertEqual((finished.status, finished.attempts), ("succeeded", 1))
        assert finished.result_json is not None
        self.assertEqual(finished.result_json["echo"], "finish current")
        deferred = self.queue.get(following.id)
        self.assertEqual((deferred.status, deferred.attempts), ("queued", 0))
        self.assertFalse(self.paths.worker_presence.exists())

    def test_sigterm_wakes_idle_worker_with_long_poll_interval(self):
        warmup = self.queue.enqueue("noop")
        child = self._start_worker(poll_interval=30)
        self._wait_job(child, warmup.id, "succeeded")
        time.sleep(0.1)
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=5), 0)
        self.assertFalse(self.paths.worker_presence.exists())

    def test_signal_handler_defers_stop_outside_interrupted_event_lock(self):
        # A signal callback must not reacquire a lock held by its own thread.
        script = """
import json, signal, threading
from pandrator.web.worker_shutdown import worker_termination
stopped = threading.Event()
previous = signal.getsignal(signal.SIGTERM)
with worker_termination(stopped.set):
    with stopped._cond:
        signal.raise_signal(signal.SIGTERM)
    assert stopped.wait(2), 'Signal did not request worker stop'
assert signal.getsignal(signal.SIGTERM) == previous, 'Signal handler not restored'
try:
    with worker_termination(stopped.set):
        raise RuntimeError('body failure')
except RuntimeError:
    pass
assert signal.getsignal(signal.SIGTERM) == previous, 'Handler not restored after failure'
print(json.dumps({'stop_observed': True}))
"""
        completed = subprocess.run(
            [sys.executable, "-c", script],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue(json.loads(completed.stdout)["stop_observed"])


if __name__ == "__main__":
    unittest.main()
