"""Durable audit behavior when another SQLite writer holds the database."""

import json
import os
import sqlite3
import stat
import tempfile
import threading
import time
import unittest
from contextlib import closing
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy.exc import OperationalError

from pandrator.web.audit import AuditService
from pandrator.web.auth import Principal
from pandrator.web.database import Database
from pandrator.web.models import AuditEvent
from tests.web_test_support import prepare_web_test_data_root


class _Redactor:
    def redact(self, value):
        return str(value).replace("secret-value", "[REDACTED]")


class AuditContentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.database_path = prepare_web_test_data_root(self.temporary.name).database
        self.database = Database(self.database_path)
        self.audit = AuditService(self.database, _Redactor())
        self.principal = Principal(
            subject="test-user",
            kind="user",
            scopes=frozenset(),
            token_id=None,
            network_zone="local",
            target_instance_id="test",
        )

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _record(self, request_id="request-1"):
        self.audit.record(
            principal=self.principal,
            request_id=request_id,
            traceparent="00-abc-123-01",
            action="test.action",
            method="POST",
            path="/api/secret-value",
            status_code=201,
            duration_ms=3,
            metadata={"retryable": "secret-value"},
        )

    def _assert_busy_event_spools_and_replays_once(self):
        with closing(sqlite3.connect(self.database_path)) as writer, writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertLogs("pandrator.web.audit", level="WARNING"):
                self._record()
            self.assertLess(time.monotonic() - started, 1.0)
            pending = self.database_path.parent / "audit-pending"
            files = list(pending.glob("*.json"))
            self.assertEqual(1, len(files))
            if os.name != "nt":
                self.assertEqual(0o700, stat.S_IMODE(pending.stat().st_mode))
                self.assertEqual(0o600, stat.S_IMODE(files[0].stat().st_mode))
            contents = files[0].read_text()
            self.assertNotIn("secret-value", contents)
            payload = json.loads(contents)
            self.assertIn("+00:00", payload["created_at"])
            writer.rollback()

        self.database.dispose()
        self.database = Database(self.database_path)
        self.audit = AuditService(self.database, _Redactor())
        self.assertEqual(0, self.audit.cleanup())
        self.assertFalse(files[0].exists())
        listed = self.audit.list()
        self.assertEqual(1, len(listed))
        self.assertEqual("00-abc-123-01", listed[0]["traceparent"])
        self.assertEqual("/api/[REDACTED]", listed[0]["path"])
        self.assertNotIn("spool_id", listed[0]["metadata"])

        # Simulate a commit followed by a failed unlink.
        files[0].write_text(contents)
        self.audit.cleanup()
        self.assertFalse(files[0].exists())
        with self.database.session() as session:
            self.assertEqual(1, session.query(AuditEvent).count())

    def test_busy_event_spools_and_replays_once_after_restart(self):
        self._assert_busy_event_spools_and_replays_once()

    def test_busy_event_spools_and_replays_once_without_directory_capability(self):
        with patch.object(os, "O_DIRECTORY", None, create=True):
            self._assert_busy_event_spools_and_replays_once()

    def test_scoped_timeout_is_restored_after_busy_and_success(self):
        with self.database.session(busy_timeout_ms=13) as session:
            self.assertEqual(
                13, session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )
        with self.database.immediate_session(busy_timeout_ms=17) as session:
            self.assertEqual(
                17, session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )
        with self.database.session() as session:
            self.assertEqual(
                30_000, session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )

        with closing(sqlite3.connect(self.database_path)) as writer, writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertRaises(OperationalError):
                with self.database.immediate_session(busy_timeout_ms=15):
                    pass
            writer.rollback()
        with self.database.session() as session:
            self.assertEqual(
                30_000, session.connection().exec_driver_sql("PRAGMA busy_timeout").scalar_one()
            )

    def test_ordinary_record_replays_pending_event_without_cleanup(self):
        with closing(sqlite3.connect(self.database_path)) as writer, writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertLogs("pandrator.web.audit", level="WARNING"):
                self._record()
            pending = self.database_path.parent / "audit-pending"
            file = next(pending.glob("*.json"))
            contents = file.read_text()
            writer.rollback()

        self._record()
        self.assertFalse(file.exists())
        self.assertEqual(2, len(self.audit.list()))
        with self.database.session() as session:
            spool_id = json.loads(contents)["metadata_json"]["spool_id"]
            self.assertEqual(
                1,
                session.query(AuditEvent)
                .filter(AuditEvent.metadata_json["spool_id"].as_string() == spool_id)
                .count(),
            )

        # A commit followed by an interrupted unlink must stay idempotent.
        file.write_text(contents)
        self._record()
        self.assertFalse(file.exists())
        self.assertEqual(3, len(self.audit.list()))

    def _slow_inserts(self, *gates):
        """Hold the Nth batch write until gates[N] is set, as a slow commit would."""
        original = self.audit._insert
        entered = [threading.Event() for _ in gates]
        calls = []

        def insert(batch):
            index = len(calls)
            calls.append([entry.fields["request_id"] for entry in batch])
            if index < len(gates):
                entered[index].set()
                gates[index].wait(10)
            original(batch)

        return patch.object(self.audit, "_insert", side_effect=insert), entered, calls

    def _recorder(self, request_id):
        thread = threading.Thread(target=self._record, args=(request_id,), daemon=True)
        thread.start()
        return thread

    def _audit_ids(self):
        with self.database.session() as session:
            return sorted(
                (event.request_id, "spool_id" in (event.metadata_json or {}))
                for event in session.query(AuditEvent).all()
            )

    def test_request_behind_a_slow_commit_spools_within_the_budget(self):
        # One slow commit used to hold every authenticated request in after_request.
        release = threading.Event()
        slow, entered, _calls = self._slow_inserts(release)
        try:
            with slow:
                leader = self._recorder("leader")
                self.assertTrue(entered[0].wait(5))
                started = time.monotonic()
                with self.assertLogs("pandrator.web.audit", level="WARNING"):
                    self._record("follower")
                elapsed = time.monotonic() - started
                self.assertTrue(leader.is_alive())
                release.set()
                leader.join(10)
        finally:
            release.set()
        self.assertFalse(leader.is_alive())
        self.assertLess(elapsed, 2.0)
        self.audit.cleanup()
        self.assertEqual([("follower", True), ("leader", False)], self._audit_ids())
        self.assertEqual([], list((self.database_path.parent / "audit-pending").glob("*.json")))

    def test_entry_taken_into_a_slow_batch_is_never_spooled(self):
        first, second = threading.Event(), threading.Event()
        slow, entered, calls = self._slow_inserts(first, second)
        # The waiting budget lapses only when the test moves this clock.
        frozen = time.monotonic()
        clock = SimpleNamespace(offset=0.0)
        audit_time = SimpleNamespace(monotonic=lambda: frozen + clock.offset)
        try:
            with slow, patch("pandrator.web.audit.time", audit_time):
                head = self._recorder("head")
                self.assertTrue(entered[0].wait(5))
                waiters = [self._recorder("a"), self._recorder("b")]
                deadline = time.monotonic() + 5
                while len(self.audit._commit_queue) < 2 and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertEqual(2, len(self.audit._commit_queue))
                first.set()
                self.assertTrue(entered[1].wait(5))
                # Both entries are in the batch being written; let their budget lapse.
                clock.offset = 60.0
                time.sleep(1.0)
                self.assertTrue(all(thread.is_alive() for thread in waiters))
                self.assertEqual([], list(self.database_path.parent.glob("audit-pending/*.json")))
                second.set()
                for thread in (head, *waiters):
                    thread.join(10)
        finally:
            first.set()
            second.set()
        self.assertEqual([["head"], ["a", "b"]], [sorted(batch) for batch in calls])
        self.assertEqual([("a", False), ("b", False), ("head", False)], self._audit_ids())
        self.assertEqual([], list(self.database_path.parent.glob("audit-pending/*.json")))

    def test_busy_opportunistic_drain_stays_bounded_and_keeps_file(self):
        with closing(sqlite3.connect(self.database_path)) as writer, writer:
            writer.execute("BEGIN IMMEDIATE")
            with self.assertLogs("pandrator.web.audit", level="WARNING"):
                self._record()
            writer.rollback()
        file = next((self.database_path.parent / "audit-pending").glob("*.json"))

        with closing(sqlite3.connect(self.database_path)) as writer, writer:
            writer.execute("BEGIN IMMEDIATE")
            started = time.monotonic()
            with self.assertLogs("pandrator.web.audit", level="WARNING"):
                self.audit._drain_pending(limit=5, budget_ms=250)
            self.assertLess(time.monotonic() - started, 0.6)
            self.assertTrue(file.exists())
            writer.rollback()


if __name__ == "__main__":
    unittest.main()
