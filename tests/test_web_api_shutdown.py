"""Native API termination and durable generation-start retry receipts."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import requests
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.api import create_app
from pandrator.web.database import Database
from pandrator.web.legacy_migration import import_legacy_data
from pandrator.web.models import ApiIdempotency, GenerationRun, Job

REPO = Path(__file__).resolve().parents[1]


@unittest.skipIf(os.name == "nt", "Windows termination does not deliver POSIX SIGTERM")
class ApiShutdownTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="pandrator-api-shutdown-")
        self.addCleanup(temporary.cleanup)
        self.paths = DataPaths.from_value(temporary.name).ensure()
        import_legacy_data(self.paths)
        app = create_app(data_root=self.paths.root, testing=True, background_maintenance=False)
        extensions = app.extensions["pandrator"]
        try:
            extensions["auth"].initialize_owner("API shutdown synthetic password")
            _, self.token = extensions["auth"].create_api_token(
                "API shutdown fixture", scopes=("app.read", "app.write", "app.run")
            )
            session = extensions["sessions"].create("API shutdown fixture")
            self.session_id = session.id
            extensions["generation"].create_plan(
                session.id, source_revision_id=None, segments=[{"text": "Queue without synthesis."}]
            )
        finally:
            extensions["tts_providers"].close()
            extensions["database"].dispose()
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.children: list[subprocess.Popen] = []
        self.threads: list[threading.Thread] = []
        self.addCleanup(self._stop_children)
        self.marker = self.paths.root / "prepared.json"
        self.restored = self.paths.root / "handler-restored.json"
        self.closed = self.paths.root / "resources-closed.json"
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            self.port = reservation.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}/api/v1/sessions/{self.session_id}/generation-runs"

    def _stop_children(self):
        for child in self.children:
            if child.poll() is None:
                child.kill()
            child.wait(timeout=5)
        for thread in self.threads:
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive(), "HTTP fixture thread survived cleanup")

    def _start_api(self, delay=1.5):
        script = self.paths.root / "api-fixture.py"
        script.write_text(
            f"""import json, signal, sys, time
from pathlib import Path
sys.path.insert(0, {str(REPO)!r})
from pandrator.web.workspace import GenerationService
from pandrator.web import cli
from pandrator.web.cli import main
native_create_app = cli.create_app
owners = []
def observed_create_app(*args, **kwargs):
    app = native_create_app(*args, **kwargs)
    services = app.extensions['pandrator']['services']
    owners.append((services, services.database.engine.pool))
    return app
cli.create_app = observed_create_app
marker = Path({str(self.marker)!r})
original = GenerationService.prepare_start
def controlled(self, *args, **kwargs):
    prepared = original(self, *args, **kwargs)
    if not marker.exists():
        temporary = marker.with_suffix('.tmp')
        temporary.write_text(json.dumps({{'pid': __import__('os').getpid()}}))
        temporary.replace(marker)
        time.sleep({delay!r})
    return prepared
GenerationService.prepare_start = controlled
previous = signal.getsignal(signal.SIGTERM)
code = main(sys.argv[1:])
Path({str(self.restored)!r}).write_text(json.dumps(signal.getsignal(signal.SIGTERM) == previous))
services, original_pool = owners[0]
periodic = services.startup_maintenance._periodic_thread
quick = services.quick_transcriptions._thread
Path({str(self.closed)!r}).write_text(json.dumps({{
    'startup_stop': services.startup_maintenance._stop.is_set(),
    'quick_stop': services.quick_transcriptions._stop.is_set(),
    'periodic_alive': bool(periodic and periodic.is_alive()),
    'quick_alive': bool(quick and quick.is_alive()),
    'database_pool_replaced': services.database.engine.pool is not original_pool,
}}))
raise SystemExit(code)
"""
        )
        log_path = self.paths.root / f"api-{len(self.children)}.log"
        with log_path.open("w") as log:
            child = subprocess.Popen(
                [
                    sys.executable,
                    str(script),
                    "--data-dir",
                    str(self.paths.root),
                    "serve",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-open-browser",
                ],
                cwd=REPO,
                stdout=log,
                stderr=log,
            )
        self.children.append(child)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if child.poll() is not None:
                self.fail(f"API exited {child.returncode}: {log_path.read_text()}")
            try:
                with requests.Session() as client:
                    client.trust_env = False
                    response = client.get(
                        f"http://127.0.0.1:{self.port}/api/v1/health", timeout=0.25
                    )
                    if response.status_code == 200:
                        return child
            except requests.RequestException:
                pass
            time.sleep(0.02)
        self.fail("API startup observation timeout")

    def _post(self):
        with requests.Session() as client:
            client.trust_env = False
            response = client.post(
                self.url,
                json={},
                headers={"Authorization": "Bearer " + self.token, "Idempotency-Key": "shutdown"},
                timeout=15,
            )
            return (
                response.status_code,
                response.json(),
                response.headers.get("Idempotency-Replayed"),
            )

    def _start_request(self):
        result = []

        def request():
            try:
                result.append(self._post())
            except requests.RequestException as error:
                result.append(type(error).__name__)

        thread = threading.Thread(target=request, name="api-shutdown-http", daemon=True)
        self.threads.append(thread)
        thread.start()
        return thread, result

    def _wait_preparation(self, child):
        deadline = time.monotonic() + 20
        while not self.marker.exists() and time.monotonic() < deadline:
            self.assertIsNone(child.poll(), "API exited before preparation")
            time.sleep(0.02)
        self.assertTrue(self.marker.is_file(), "Preparation observation timeout")
        with self.database.snapshot_session() as db:
            reservation = db.scalar(select(ApiIdempotency))
            assert reservation is not None
            self.assertEqual((reservation.state, reservation.resource_id), ("in_progress", None))
            self.assertEqual(list(db.scalars(select(GenerationRun))), [])
            self.assertEqual(list(db.scalars(select(Job))), [])
            return reservation.id

    def _assert_clean_shutdown(self):
        self.assertTrue(json.loads(self.restored.read_text()))
        self.assertEqual(
            json.loads(self.closed.read_text()),
            {
                "startup_stop": True,
                "quick_stop": True,
                "periodic_alive": False,
                "quick_alive": False,
                "database_pool_replaced": True,
            },
        )

    def _exercise_active_cut(self, signum, *, repeat=False):
        child = self._start_api()
        thread, original_http = self._start_request()
        reservation_id = self._wait_preparation(child)
        child.send_signal(signum)
        if repeat:
            time.sleep(0.15)
            self.assertIsNone(child.poll())
            child.send_signal(signum)
        self.assertEqual(child.wait(timeout=7), 0)
        self._assert_clean_shutdown()
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(original_http), 1)
        with self.database.snapshot_session() as db:
            reservations = list(db.scalars(select(ApiIdempotency)))
            runs = list(db.scalars(select(GenerationRun)))
            jobs = list(db.scalars(select(Job)))
            self.assertEqual((len(reservations), len(runs), len(jobs)), (1, 1, 1))
            reservation, run, job = reservations[0], runs[0], jobs[0]
            self.assertEqual((reservation.id, reservation.state), (reservation_id, "completed"))
            self.assertEqual(reservation.status_code, 202)
            self.assertEqual(reservation.resource_id, run.id)
            self.assertEqual((run.job_id, job.status, job.attempts), (job.id, "queued", 0))
            receipt = reservation.response_json
        self._start_api()
        status, body, replayed = self._post()
        self.assertEqual((status, body, replayed), (202, receipt, "true"))
        with self.database.snapshot_session() as db:
            self.assertEqual(len(list(db.scalars(select(GenerationRun)))), 1)
            self.assertEqual(len(list(db.scalars(select(Job)))), 1)

    def test_sigterm_finishes_prepared_start_and_replay_does_not_duplicate_job(self):
        self._exercise_active_cut(signal.SIGTERM)

    def test_repeated_sigterm_does_not_interrupt_request_shutdown(self):
        self._exercise_active_cut(signal.SIGTERM, repeat=True)

    def test_keyboard_interrupt_retains_bounded_request_shutdown(self):
        self._exercise_active_cut(signal.SIGINT)

    def test_idle_sigterm_exits_and_restores_handler(self):
        child = self._start_api()
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=3), 0)
        self._assert_clean_shutdown()
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", self.port), timeout=0.1)

    def test_shutdown_is_bounded_when_preparation_outlasts_waitress_window(self):
        child = self._start_api(delay=8)
        thread, original_http = self._start_request()
        reservation_id = self._wait_preparation(child)
        start = time.monotonic()
        child.send_signal(signal.SIGTERM)
        self.assertEqual(child.wait(timeout=7), 0)
        self.assertLess(time.monotonic() - start, 7)
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(original_http), 1)
        self._assert_clean_shutdown()
        with self.database.snapshot_session() as db:
            reservation = db.scalar(select(ApiIdempotency))
            assert reservation is not None
            self.assertEqual(
                (reservation.id, reservation.state, reservation.resource_id),
                (reservation_id, "in_progress", None),
            )
            self.assertEqual(list(db.scalars(select(GenerationRun))), [])
            self.assertEqual(list(db.scalars(select(Job))), [])
