import inspect
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

from sqlalchemy import event

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Job, JobEvent, SessionPurge, SessionRecord, utcnow
from pandrator.web.work import WorkService, _work_state
from tests.web_test_support import prepare_web_test_data_root


class WorkApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        self.bootstrap = BootstrapTokenStore()
        self.bootstrap_token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
            public_origin="HTTPS://PANDRATOR.EXAMPLE/",
        )
        self.client = self.app.test_client()
        response = self.client.post(
            "/api/v1/auth/bootstrap",
            json={"token": self.bootstrap_token},
        )
        self.assertEqual(200, response.status_code)
        self.csrf_token = response.get_json()["csrf_token"]

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_job_activity_projects_and_recovery_survive_compact_snapshots(self):
        project = self.client.post(
            "/api/v1/sessions", json={"name": "A recoverable project"},
            headers={"X-CSRF-Token": self.csrf_token},
        ).get_json()
        queue = self.app.extensions["pandrator"]["jobs"]
        job = queue.enqueue(
            "generation.run", {"session_id": project["id"], "generation_run_id": "failed-run"},
            session_id=project["id"],
        )
        for path in ("/api/v1/jobs", "/api/v1/events/snapshot?view=compact", f"/api/v1/jobs/{job.id}"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(200, response.status_code)
                body = response.get_json()
                if "snapshot" in path:
                    body = body["jobs"]
                item = next(row for row in body["items"] if row["id"] == job.id) if "items" in body else body
                self.assertEqual(item["project"], {
                    "id": project["id"], "name": "A recoverable project",
                    "available": True, "trashed": False,
                })
                self.assertEqual(item["generation_run_id"], "failed-run")
                if "compact" in path:
                    self.assertNotIn("payload_json", item)

    def test_job_project_metadata_identifies_trash_without_offering_recovery(self):
        project = self.client.post(
            "/api/v1/sessions", json={"name": "Retained project name"},
            headers={"X-CSRF-Token": self.csrf_token},
        ).get_json()
        services = self.app.extensions["pandrator"]
        job = services["jobs"].enqueue("noop", {}, session_id=project["id"])
        with services["database"].session() as session:
            record = session.get(SessionRecord, project["id"])
            record.trashed_at = utcnow()
        item = self.client.get(f"/api/v1/jobs/{job.id}").get_json()
        self.assertEqual(item["project"], {
            "id": project["id"], "name": "Retained project name",
            "available": False, "trashed": True,
        })
        self.assertIsNone(item["generation_run_id"])

    def test_work_projection_omits_payload_and_preserves_legacy_job_contract(self):
        queue = self.app.extensions["pandrator"]["jobs"]
        job = queue.enqueue(
            "source.clean",
            {
                "source_artifact_id": "artifact-1",
                "api_key": "must-never-be-model-visible",
            },
        )

        work_response = self.client.get(f"/api/v1/work/{job.id}")
        legacy_response = self.client.get(f"/api/v1/jobs/{job.id}")

        self.assertEqual(200, work_response.status_code)
        work_payload = work_response.get_json()
        self.assertEqual("1", work_payload["schema_version"])
        self.assertEqual("job", work_payload["type"])
        self.assertNotIn("payload_json", work_payload)
        self.assertNotIn(
            "must-never-be-model-visible", work_response.get_data(as_text=True)
        )
        self.assertEqual(200, legacy_response.status_code)
        self.assertIn("payload_json", legacy_response.get_json())

    def test_work_filters_and_events_are_bounded_redacted_projections(self):
        queue = self.app.extensions["pandrator"]["jobs"]
        first = queue.enqueue("source.clean", {"token": "not-visible"})
        queue.enqueue("noop", {})
        queue.log(
            first.id,
            "INFO",
            "Authorization: Bearer remote-secret-token",
            logger="provider.client",
            trace="x" * 12_000,
        )

        listing = self.client.get("/api/v1/work?kind=source.clean&state=queued")
        events = self.client.get(f"/api/v1/work/{first.id}/events")

        self.assertEqual(200, listing.status_code)
        self.assertEqual(
            [first.id], [item["id"] for item in listing.get_json()["items"]]
        )
        self.assertEqual(200, events.status_code)
        serialized = events.get_data(as_text=True)
        self.assertNotIn("remote-secret-token", serialized)
        self.assertNotIn("not-visible", serialized)
        log = next(
            item
            for item in events.get_json()["items"]
            if item["event_type"] == "job.log"
        )
        self.assertIn("[REDACTED]", log["data"]["message"])
        self.assertLessEqual(len(log["data"]["trace"]), 8_020)

    def test_legacy_success_does_not_project_previous_attempt_as_current_failure(self):
        services = self.app.extensions["pandrator"]
        queue = services["jobs"]
        job = queue.enqueue("noop", {}, max_attempts=2)
        first = queue.claim("test-worker")
        queue.fail(
            job.id, "test-worker", "worker_lease_lost", "Previous lease failure",
            lease_generation=first.lease_generation,
        )
        second = queue.claim("test-worker")
        queue.complete(job.id, "test-worker", {}, lease_generation=second.lease_generation)
        with services["database"].session() as session:
            historical = session.get(Job, job.id)
            historical.error_code = "worker_lease_lost"
            historical.error_message = "Previous lease failure"
            historical.progress_detail = "Retry scheduled after attempt 1 of 2"

        current = self.client.get(f"/api/v1/work/{job.id}").get_json()
        self.assertEqual("succeeded", current["state"])
        self.assertIsNone(current["error"])
        self.assertIsNone(current["detail"])
        events = self.client.get(f"/api/v1/work/{job.id}/events").get_json()["items"]
        self.assertIn("job.retry_scheduled", [event["event_type"] for event in events])
        self.assertEqual("worker_lease_lost", queue.get(job.id).error_code)

    def test_work_cancel_uses_safe_projection(self):
        queue = self.app.extensions["pandrator"]["jobs"]
        job = queue.enqueue("noop", {"private": "payload"})

        response = self.client.post(
            f"/api/v1/work/{job.id}/cancel",
            headers={
                "X-CSRF-Token": self.csrf_token,
                "Idempotency-Key": "cancel-safe-projection",
            },
        )

        self.assertEqual(200, response.status_code)
        payload = response.get_json()
        self.assertEqual("cancelled", payload["state"])
        self.assertFalse(payload["cancellable"])
        self.assertNotIn("payload_json", payload)

    def test_work_cancel_is_idempotent_and_conflicts_on_new_arguments(self):
        queue = self.app.extensions["pandrator"]["jobs"]
        first = queue.enqueue("noop", {})
        second = queue.enqueue("noop", {})
        headers = {
            "X-CSRF-Token": self.csrf_token,
            "Idempotency-Key": "same-logical-cancel",
        }

        initial = self.client.post(
            f"/api/v1/work/{first.id}/cancel",
            headers=headers,
        )
        replay = self.client.post(
            f"/api/v1/work/{first.id}/cancel",
            headers=headers,
        )
        conflict = self.client.post(
            f"/api/v1/work/{second.id}/cancel",
            headers=headers,
        )

        self.assertEqual(200, initial.status_code)
        self.assertEqual(200, replay.status_code)
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(initial.get_json(), replay.get_json())
        self.assertEqual(409, conflict.status_code)
        self.assertEqual(
            "idempotency_conflict",
            conflict.get_json()["error"]["code"],
        )
        canceled = [
            event
            for event in queue.events_for(first.id)
            if event.event_type == "job.canceled"
        ]
        self.assertEqual(1, len(canceled))

    def test_work_cancel_requires_a_key_and_rolls_back_failed_domain_work(
        self,
    ):
        from unittest.mock import patch

        extension = self.app.extensions["pandrator"]
        queue = extension["jobs"]
        work = extension["work"]
        job = queue.enqueue("noop", {})
        headers = {
            "X-CSRF-Token": self.csrf_token,
            "Idempotency-Key": "rollback-domain-cancel",
        }

        missing = self.client.post(
            f"/api/v1/work/{job.id}/cancel",
            headers={"X-CSRF-Token": self.csrf_token},
        )
        self.assertEqual(400, missing.status_code)
        self.assertEqual(
            "idempotency_key_required",
            missing.get_json()["error"]["code"],
        )

        original = work.cancel_in_session

        def fail_after_cancel(db_session, job_id):
            original(db_session, job_id)
            raise RuntimeError("injected rollback")

        with (
            patch.object(
                work,
                "cancel_in_session",
                side_effect=fail_after_cancel,
            ),
            self.assertRaisesRegex(
                RuntimeError,
                "injected rollback",
            ),
        ):
            self.client.post(
                f"/api/v1/work/{job.id}/cancel",
                headers=headers,
            )
        self.assertEqual("queued", queue.get(job.id).status)

        retry = self.client.post(
            f"/api/v1/work/{job.id}/cancel",
            headers=headers,
        )
        self.assertEqual(200, retry.status_code)
        self.assertEqual("cancelled", retry.get_json()["state"])

    def test_enqueue_in_session_commits_and_rolls_back_with_its_caller(self):
        extension = self.app.extensions["pandrator"]
        database = extension["database"]
        queue = extension["jobs"]

        with (
            self.assertRaisesRegex(RuntimeError, "rollback"),
            database.session() as session,
        ):
            rolled_back = queue.enqueue_in_session(
                session,
                "noop",
                {"case": "rolled-back"},
            )
            rolled_back_id = rolled_back.id
            raise RuntimeError("rollback")
        with self.assertRaises(KeyError):
            queue.get(rolled_back_id)

        with database.session() as session:
            committed = queue.enqueue_in_session(
                session,
                "noop",
                {"case": "committed"},
            )
            committed_id = committed.id
        self.assertEqual("queued", queue.get(committed_id).status)
        with database.session() as session:
            event_types = [
                item.event_type
                for item in session.query(JobEvent)
                .filter(JobEvent.job_id == committed_id)
                .all()
            ]
        self.assertEqual(["job.queued"], event_types)

    def test_redactor_is_composed_once_and_guards_do_not_reach_through_jobs(self):
        extension = self.app.extensions["pandrator"]
        self.assertIs(extension["redactor"], extension["jobs"].secret_redactor)
        source = inspect.getsource(ApiGuards)
        self.assertNotIn("services.jobs", source)


class WorkServiceListingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(paths.database)
        self.queue = JobQueue(self.database)
        self.work = WorkService(self.queue, SecretRedactor(self.database))

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _insert_jobs(self, rows):
        session_ids = {
            row.get("session_id") for row in rows if row.get("session_id")
        }
        now = utcnow()
        with self.database.session() as session:
            session.add_all(
                SessionRecord(
                    id=session_id,
                    name=session_id,
                    storage_key=f"storage-{session_id}",
                    workflow_kind="audiobook",
                    source_language="en",
                    included_stages_json=[],
                    status="idle",
                    revision=1,
                )
                for session_id in session_ids
            )
            session.flush()
            session.add_all(
                Job(
                    id=row["id"],
                    kind=row.get("kind", "noop"),
                    session_id=row.get("session_id"),
                    status=row.get("status", "queued"),
                    payload_json=row.get("payload_json", {}),
                    result_json=row.get("result_json"),
                    progress=row.get("progress", 0.0),
                    progress_detail=row.get("progress_detail"),
                    error_code=row.get("error_code"),
                    error_message=row.get("error_message"),
                    attempts=row.get("attempts", 0),
                    max_attempts=row.get("max_attempts", 1),
                    created_at=row.get("created_at", now),
                    updated_at=row.get("updated_at", now),
                    started_at=row.get("started_at"),
                    finished_at=row.get("finished_at"),
                )
                for row in rows
            )

    def test_old_matching_active_job_is_not_hidden_by_new_terminal_history(self):
        now = utcnow()
        rows = [
            {
                "id": f"terminal-{index}",
                "kind": "noop",
                "status": "succeeded",
                "created_at": now - timedelta(seconds=index),
            }
            for index in range(501)
        ]
        rows.append(
            {
                "id": "old-queued",
                "kind": "generation.audio",
                "status": "queued",
                "created_at": now - timedelta(seconds=1_000),
            }
        )
        self._insert_jobs(rows)

        listing = self.work.list(
            states=("queued", "running", "cancel_requested"), limit=100
        )

        self.assertEqual(["old-queued"], [item.id for item in listing])
        self.assertEqual("queued", listing[0].state)

    def test_sql_filters_and_limits_before_projection_without_loading_raw_payloads(self):
        now = utcnow()
        ignored_payload = {"history": "x" * 64_000}
        self._insert_jobs(
            [
                {
                    "id": "match-newest",
                    "session_id": "session-a",
                    "kind": "alpha",
                    "status": "queued",
                    "created_at": now,
                    "payload_json": ignored_payload,
                },
                {
                    "id": "match-middle",
                    "session_id": "session-a",
                    "kind": "alpha",
                    "status": "queued",
                    "created_at": now - timedelta(seconds=1),
                    "payload_json": ignored_payload,
                },
                {
                    "id": "match-oldest",
                    "session_id": "session-a",
                    "kind": "alpha",
                    "status": "queued",
                    "created_at": now - timedelta(seconds=2),
                    "payload_json": ignored_payload,
                },
                {
                    "id": "other-session",
                    "session_id": "session-b",
                    "kind": "alpha",
                    "status": "queued",
                    "created_at": now + timedelta(seconds=3),
                    "payload_json": ignored_payload,
                },
                {
                    "id": "other-kind",
                    "session_id": "session-a",
                    "kind": "beta",
                    "status": "queued",
                    "created_at": now + timedelta(seconds=2),
                    "payload_json": ignored_payload,
                },
                {
                    "id": "other-state",
                    "session_id": "session-a",
                    "kind": "alpha",
                    "status": "succeeded",
                    "created_at": now + timedelta(seconds=1),
                    "payload_json": ignored_payload,
                },
            ]
        )
        statements = []

        def capture_sql(_connection, _cursor, statement, parameters, _context, _many):
            statements.append((statement, parameters))

        event.listen(self.database.engine, "before_cursor_execute", capture_sql)
        try:
            with patch.object(self.work, "_project", wraps=self.work._project) as project:
                listing = self.work.list(
                    session_id="session-a",
                    kinds=(" alpha ",),
                    states=("queued",),
                    limit=2,
                )
        finally:
            event.remove(self.database.engine, "before_cursor_execute", capture_sql)

        self.assertEqual(["match-newest", "match-middle"], [item.id for item in listing])
        self.assertEqual(2, project.call_count)
        public_views = [item.model_dump(mode="json") for item in listing]
        self.assertTrue(all("payload_json" not in item for item in public_views))
        self.assertNotIn(ignored_payload["history"], repr(public_views))
        job_selects = [
            (statement, parameters)
            for statement, parameters in statements
            if "SELECT jobs.id, jobs.kind" in statement and "FROM jobs" in statement
        ]
        self.assertEqual(1, len(job_selects))
        statement, parameters = job_selects[0]
        select_clause = statement.split("FROM jobs", maxsplit=1)[0].lower()
        for excluded_column in (
            "payload_json",
            "resource_keys_json",
            "lease_owner",
            "lease_expires_at",
            "lease_generation",
            "available_at",
        ):
            self.assertNotIn(excluded_column, select_clause)
        self.assertIn("jobs.session_id =", statement)
        self.assertIn("jobs.kind in", statement.lower())
        self.assertIn("lower(trim(coalesce(nullif(jobs.status", statement.lower())
        self.assertIn("order by jobs.created_at desc", statement.lower())
        self.assertIn("limit ?", statement.lower())
        self.assertEqual((2, 0), tuple(parameters[-2:]))

    def test_purge_payloads_are_hydrated_in_one_bounded_batch(self):
        now = utcnow()
        targets = {
            "purging-job": "session-purging",
            "failed-job": "session-failed",
            "complete-job": "session-complete",
        }
        self._insert_jobs(
            [
                {
                    "id": "ordinary-job",
                    "kind": "noop",
                    "status": "queued",
                    "created_at": now,
                    "payload_json": {"private": "ordinary-payload-canary"},
                },
                *(
                    {
                        "id": job_id,
                        "kind": "maintenance.session_purge",
                        "status": "queued",
                        "created_at": now - timedelta(seconds=index + 1),
                        "payload_json": {
                            "target_session_id": target_id,
                            "private": f"{job_id}-payload-canary",
                        },
                    }
                    for index, (job_id, target_id) in enumerate(targets.items())
                ),
                {
                    "id": "preview-job",
                    "kind": "maintenance.session_purge_preview",
                    "status": "queued",
                    "created_at": now - timedelta(seconds=4),
                    "payload_json": {
                        "target_session_id": "session-preview",
                        "private": "preview-payload-canary",
                    },
                },
            ]
        )
        with self.database.session() as session:
            session.add_all(
                SessionPurge(
                    session_id=target_id,
                    state=state,
                    manifest_json=[],
                    expected_revision=1,
                    impact_token="x" * 64,
                )
                for target_id, state in zip(
                    targets.values(), ("purging", "failed", "complete"), strict=True
                )
            )

        statements = []

        def capture_sql(_connection, _cursor, statement, parameters, _context, _many):
            statements.append((statement, parameters))

        event.listen(self.database.engine, "before_cursor_execute", capture_sql)
        try:
            listing = self.work.list(limit=10)
        finally:
            event.remove(self.database.engine, "before_cursor_execute", capture_sql)

        views = {item.id: item for item in listing}
        self.assertEqual(
            {"ordinary-job", *targets, "preview-job"}, set(views)
        )
        self.assertTrue(views["ordinary-job"].cancellable)
        for job_id, target_id in targets.items():
            self.assertEqual(target_id, views[job_id].target_session_id)
            self.assertFalse(views[job_id].cancellable)
        self.assertEqual("session-preview", views["preview-job"].target_session_id)
        self.assertTrue(views["preview-job"].cancellable)
        public_views = [item.model_dump(mode="json") for item in listing]
        self.assertTrue(all("payload_json" not in item for item in public_views))
        serialized = repr(public_views)
        for canary in (
            "ordinary-payload-canary",
            *(f"{job_id}-payload-canary" for job_id in targets),
            "preview-payload-canary",
        ):
            self.assertNotIn(canary, serialized)

        payload_selects = [
            (statement, parameters)
            for statement, parameters in statements
            if "jobs.payload_json" in statement.lower() and "from jobs" in statement.lower()
        ]
        self.assertEqual(1, len(payload_selects))
        statement, parameters = payload_selects[0]
        self.assertIn("select jobs.id, jobs.payload_json", statement.lower())
        self.assertIn("jobs.id in", statement.lower())
        self.assertEqual(4, len(parameters))

    def test_sql_state_filter_matches_public_alias_normalization(self):
        rows = [
            ("canceled", "canceled"),
            ("cancelled", "cancelled"),
            ("cancel-request", "cancel_requested"),
            ("running-uppercase", " RUNNING "),
            ("queued-uppercase", " QUEUED "),
            ("empty-status", ""),
            ("unknown-status", "mystery"),
            ("spaces-status", "   "),
        ]
        self._insert_jobs(
            [{"id": job_id, "status": status} for job_id, status in rows]
        )
        requests = (
            "CANCELED",
            "cancelled",
            " cancel_requested ",
            "RUNNING",
            " QUEUED ",
            "waiting",
            "unknown",
        )

        with patch.object(self.queue, "_reconcile_stale") as reconcile:
            for requested in requests:
                expected_state = _work_state(requested)
                expected_ids = [
                    job_id
                    for job_id, status in rows
                    if _work_state(status) == expected_state
                ]
                actual_ids = [
                    item.id
                    for item in self.work.list(states=(requested,), limit=100)
                ]
                self.assertCountEqual(expected_ids, actual_ids, requested)

            no_filter = self.work.list(states=("  ",), limit=100)
            self.assertCountEqual(
                [job_id for job_id, _status in rows],
                [item.id for item in no_filter],
            )
        self.assertEqual(len(requests) + 1, reconcile.call_count)

    def test_diagnostic_list_retains_complete_raw_job_payload(self):
        raw_payload = {"private_input": "diagnostic-only"}
        self._insert_jobs(
            [{"id": "diagnostic-job", "payload_json": raw_payload}]
        )

        result = self.work.diagnostic_list(10)

        self.assertEqual(1, len(result))
        self.assertIsInstance(result[0], Job)
        self.assertEqual(raw_payload, result[0].payload_json)


class ApplicationIdentityTests(unittest.TestCase):
    def test_identity_requires_auth_and_survives_application_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            first_bootstrap = BootstrapTokenStore()
            first_token = first_bootstrap.issue()
            first_app = create_app(
                data_root=directory,
                testing=True,
                bootstrap_tokens=first_bootstrap,
                public_origin="HTTPS://PANDRATOR.EXAMPLE/",
            )
            first_client = first_app.test_client()
            self.assertEqual(
                401,
                first_client.get("/api/v1/system/identity").status_code,
            )
            first_client.post(
                "/api/v1/auth/bootstrap",
                json={"token": first_token},
            )
            first = first_client.get("/api/v1/system/identity").get_json()
            first_app.extensions["pandrator"]["database"].dispose()

            second_bootstrap = BootstrapTokenStore()
            second_token = second_bootstrap.issue()
            second_app = create_app(
                data_root=directory,
                testing=True,
                bootstrap_tokens=second_bootstrap,
                public_origin="https://pandrator.example",
            )
            second_client = second_app.test_client()
            second_client.post(
                "/api/v1/auth/bootstrap",
                json={"token": second_token},
            )
            second = second_client.get("/api/v1/system/identity").get_json()
            second_app.extensions["pandrator"]["database"].dispose()

        self.assertEqual(first["instance_id"], second["instance_id"])
        self.assertEqual("https://pandrator.example", first["canonical_origin"])
        self.assertEqual("1", first["schema_version"])
        self.assertEqual("v1", first["api_version"])


if __name__ == "__main__":
    unittest.main()
