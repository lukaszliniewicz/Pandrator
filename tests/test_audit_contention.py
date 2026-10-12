"""Durable audit behavior when another SQLite writer holds the database."""

import json
import os
import sqlite3
import stat
import tempfile
import time
import unittest
from contextlib import closing
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

    def _record(self):
        self.audit.record(
            principal=self.principal,
            request_id="request-1",
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
