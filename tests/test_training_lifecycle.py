"""Persisted training lifecycle contracts across HTTP, queue, and local CLI."""

from __future__ import annotations

import copy
import io
import json
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stderr
from datetime import timedelta
from unittest import mock

from sqlalchemy import event, func, select

from pandrator.web import cli
from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, JobEvent, ResourceClaim, TrainingRun, utcnow
from pandrator.web.session_purge import SessionPurgeService
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


class TrainingLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="training-lifecycle-")
        self.paths = prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
            background_maintenance=False,
        )
        self.database = self.app.extensions["pandrator"]["database"]
        self.queue = JobQueue(self.database)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()[
            "csrf_token"
        ]
        self.source_id = self.upload("training.wav", b"training audio fixture")
        self.text_id = self.upload("training.txt", b"A training transcript.")
        self.payload = {
            "model_name": "narrator",
            "source_artifact_id": self.source_id,
            "source_text_artifact_id": self.text_id,
            "settings": {"epochs": 3},
        }

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def upload(self, name, body):
        response = self.client.post(
            "/api/v1/uploads",
            data={"file": (io.BytesIO(body), name)},
            content_type="multipart/form-data",
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()["artifact_id"]

    def post(self, suffix="", payload=None):
        return self.client.post(
            f"/api/v1/training{suffix}",
            json=self.payload if payload is None and not suffix else payload,
            headers={"X-CSRF-Token": self.csrf},
        )

    def snapshot(self):
        """Compare durable rows and both source files, including timestamps/events."""
        result = {}
        with self.database.snapshot_session() as session:
            for model in (TrainingRun, Job, JobEvent, ResourceClaim):
                records = session.scalars(
                    select(model).order_by(*model.__table__.primary_key)
                ).all()
                result[model.__tablename__] = [
                    copy.deepcopy(
                        {
                            column.name: getattr(row, column.name)
                            for column in model.__table__.columns
                        }
                    )
                    for row in records
                ]
        artifacts = ArtifactService(self.database, self.paths)
        result["source_bytes"] = [
            artifacts.resolve(artifact_id)[1].read_bytes()
            for artifact_id in (self.source_id, self.text_id)
        ]
        return result

    def seed(self, status="queued", job_status="queued", *, linked=True, error=None):
        with self.database.session() as session:
            training = TrainingRun(
                model_name="narrator",
                source_artifact_id=self.source_id,
                source_text_artifact_id=self.text_id,
                settings_json={"epochs": 3},
                status=status,
            )
            session.add(training)
            session.flush()
            if linked:
                job = self.queue.enqueue_in_session(
                    session,
                    "training.xtts",
                    {"training_id": training.id},
                    max_attempts=1,
                    resource_keys=["training:xtts", "gpu:default"],
                )
                job.status = job_status
                job.error_message = error
                training.job_id = job.id
            training_id = training.id
        return training_id

    def assert_admission_rollback(self, suffix=""):
        before = self.snapshot()
        with mock.patch(
            "pandrator.web.jobs.ensure_app_job_allowed",
            side_effect=ValueError("Training admission refused"),
        ):
            response = self.post(suffix)
            self.assertEqual(422, response.status_code, response.get_json())
        self.assertEqual(before, self.snapshot())

    def test_create_admission_failure_rolls_back_domain_job_events_and_sources(self):
        self.assert_admission_rollback()

    def test_retry_admission_failure_rolls_back_new_domain_job_and_events(self):
        training_id = self.seed(status="failed", job_status="failed")
        self.assert_admission_rollback(f"/{training_id}/retry")

    def test_create_engine_commit_refusal_rolls_back_domain_job_and_events(self):
        before = self.snapshot()

        def refuse_training_commit(connection):
            count = connection.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "training.xtts")
            )
            if count > 0:
                raise RuntimeError("Training commit refused")

        event.listen(self.database.engine, "commit", refuse_training_commit)
        try:
            with self.assertRaisesRegex(RuntimeError, "Training commit refused"):
                self.post()
        finally:
            event.remove(self.database.engine, "commit", refuse_training_commit)
        self.assertEqual(before, self.snapshot())

    def test_cancel_failure_after_native_queue_mutation_rolls_back_all_rows(self):
        training_id = self.seed()
        before = self.snapshot()
        native_cancel = JobQueue.request_cancel_in_session

        def fail_after_cancel(queue, session, job_id):
            native_cancel(queue, session, job_id)
            raise RuntimeError("Cancellation commit refused")

        with mock.patch.object(JobQueue, "request_cancel_in_session", fail_after_cancel):
            with self.assertRaisesRegex(RuntimeError, "Cancellation commit refused"):
                self.post(f"/{training_id}/cancel")
        self.assertEqual(before, self.snapshot())

    def test_terminal_cancel_preserves_training_job_and_events_and_returns_actual_status(self):
        for status in ("succeeded", "failed", "canceled", "interrupted"):
            with self.subTest(status=status):
                training_id = self.seed(status=status, job_status=status)
                before = self.snapshot()
                response = self.post(f"/{training_id}/cancel")
                self.assertEqual(202, response.status_code)
                self.assertEqual(before, self.snapshot())
                self.assertEqual(status, response.get_json()["status"])

    def test_queued_cancel_persists_canceled_training_and_job(self):
        training_id = self.seed()
        response = self.post(f"/{training_id}/cancel")
        self.assertEqual(202, response.status_code)
        with self.database.snapshot_session() as session:
            training = session.get(TrainingRun, training_id)
            self.assertEqual("canceled", training.status)
            self.assertEqual("canceled", session.get(Job, training.job_id).status)
        self.assertEqual("canceled", response.get_json()["status"])

    def test_running_cancel_retains_owned_lease_and_resource_claims(self):
        training_id = self.seed()
        claimed = self.queue.claim("training-worker", lease_seconds=120)
        self.assertIsNotNone(claimed)
        self.assertTrue(
            self.queue.acquire_resources(
                claimed.id,
                "training-worker",
                claimed.resource_keys_json,
                lease_generation=claimed.lease_generation,
                lease_seconds=120,
            )
        )
        with self.database.session() as session:
            session.get(TrainingRun, training_id).status = "running"
        before = self.snapshot()
        response = self.post(f"/{training_id}/cancel")
        self.assertEqual(202, response.status_code)
        after = self.snapshot()
        self.assertEqual("cancel_requested", after["training_runs"][0]["status"])
        self.assertEqual("cancel_requested", after["jobs"][0]["status"])
        for field in ("lease_owner", "lease_generation", "lease_expires_at"):
            self.assertEqual(before["jobs"][0][field], after["jobs"][0][field])
        self.assertEqual(before["resource_claims"], after["resource_claims"])

    def test_list_projects_terminal_job_without_mutating_training(self):
        training_id = self.seed(job_status="failed", error="Worker failed")
        before = self.snapshot()
        for _ in range(2):
            response = self.client.get("/api/v1/training")
            self.assertEqual(200, response.status_code)
            item = next(item for item in response.get_json()["items"] if item["id"] == training_id)
            self.assertEqual("failed", item["status"])
            self.assertEqual("Worker failed", item["error_message"])
            self.assertEqual(before, self.snapshot())

    def test_native_queue_failure_persists_training_failure_without_get(self):
        training_id = self.seed()
        claimed = self.queue.claim("training-worker")
        self.assertIsNotNone(claimed)
        self.assertTrue(
            self.queue.fail(
                claimed.id,
                "training-worker",
                "training_failed",
                "Worker failed",
                lease_generation=claimed.lease_generation,
            )
        )
        with self.database.snapshot_session() as session:
            training = session.get(TrainingRun, training_id)
            self.assertEqual("failed", training.status)
            self.assertEqual("Worker failed", training.error_message)
            self.assertEqual("failed", session.get(Job, training.job_id).status)

    def test_startup_reconcile_repairs_orphans_and_preserves_terminal_training(self):
        queued_id = self.seed(linked=False)
        cancel_id = self.seed(status="cancel_requested", linked=False)
        foreign_id = self.seed()
        with self.database.session() as session:
            training = session.get(TrainingRun, foreign_id)
            session.get(Job, training.job_id).kind = "noop"
        terminal_ids = [
            self.seed(status=status, linked=False)
            for status in ("succeeded", "failed", "canceled", "interrupted")
        ]
        before = self.snapshot()
        self.database.dispose()
        reopened = create_app(
            data_root=self.temporary.name, testing=True, background_maintenance=False
        )
        try:
            database = reopened.extensions["pandrator"]["database"]
            with database.snapshot_session() as session:
                self.assertEqual("interrupted", session.get(TrainingRun, queued_id).status)
                self.assertEqual("canceled", session.get(TrainingRun, cancel_id).status)
                self.assertEqual("interrupted", session.get(TrainingRun, foreign_id).status)
                for training_id in terminal_ids:
                    old = next(row for row in before["training_runs"] if row["id"] == training_id)
                    actual = session.get(TrainingRun, training_id)
                    self.assertEqual(
                        old,
                        {
                            column.name: getattr(actual, column.name)
                            for column in TrainingRun.__table__.columns
                        },
                    )
        finally:
            reopened.extensions["pandrator"]["database"].dispose()

    def cli_args(self):
        return Namespace(
            model_name="narrator",
            artifact_id=self.source_id,
            text_artifact_id=self.text_id,
            settings=json.dumps({"epochs": 3}),
            json=True,
        )

    def test_local_cli_start_persists_transcript_and_training_resources(self):
        emitted = []
        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            mock.patch.object(
                cli, "_emit", side_effect=lambda payload, _json: emitted.append(payload)
            ),
            mock.patch.object(self.database, "dispose", wraps=self.database.dispose) as dispose,
        ):
            self.assertEqual(0, cli.command_training_start(self.cli_args()))
            dispose.assert_called_once_with()
        self.assertEqual(1, len(emitted))
        with self.database.snapshot_session() as session:
            training = session.get(TrainingRun, emitted[0]["training_id"])
            self.assertEqual(self.text_id, training.source_text_artifact_id)
            self.assertEqual(self.source_id, training.source_artifact_id)
            self.assertEqual({"epochs": 3}, training.settings_json)
            job = session.get(Job, training.job_id)
            self.assertEqual(emitted[0]["id"], job.id)
            self.assertEqual(["gpu:default", "training:xtts"], job.resource_keys_json)
            self.assertEqual(self.text_id, job.payload_json["source_text_artifact_id"])

    def test_local_cli_admission_failure_rolls_back_domain_job_and_events(self):
        before = self.snapshot()
        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            mock.patch.object(cli, "_emit") as emit,
            mock.patch.object(self.database, "dispose", wraps=self.database.dispose) as dispose,
            mock.patch(
                "pandrator.web.jobs.ensure_app_job_allowed",
                side_effect=ValueError("Training admission refused"),
            ),
        ):
            with self.assertRaisesRegex(ValueError, "Training admission refused"):
                cli.command_training_start(self.cli_args())
            dispose.assert_called_once_with()
            emit.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def test_local_cli_rejects_inline_credentials_before_writes(self):
        before = self.snapshot()
        args = self.cli_args()
        args.settings = json.dumps({"api_key": "synthetic-secret", "epochs": 2})
        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            mock.patch.object(cli, "_emit") as emit,
        ):
            with self.assertRaises(ValueError):
                cli.command_training_start(args)
            emit.assert_not_called()
        self.assertEqual(before, self.snapshot())

    def seed_legacy_credentials(self):
        training_id = self.seed(status="failed", job_status="failed")
        with self.database.session() as session:
            session.get(TrainingRun, training_id).settings_json = {
                "api_key": "synthetic-secret",
                "epochs": 2,
            }
        return training_id

    def test_retry_rejects_legacy_inline_credentials_before_writes(self):
        training_id = self.seed_legacy_credentials()
        before = self.snapshot()
        response = self.post(f"/{training_id}/retry")
        self.assertEqual(422, response.status_code)
        self.assertEqual(before, self.snapshot())

    def test_list_redacts_legacy_inline_credentials_without_changing_storage(self):
        training_id = self.seed_legacy_credentials()
        before = self.snapshot()
        response = self.client.get("/api/v1/training")
        self.assertEqual(200, response.status_code)
        item = next(item for item in response.get_json()["items"] if item["id"] == training_id)
        self.assertNotIn("synthetic-secret", json.dumps(item))
        self.assertEqual(2, item["settings_json"]["epochs"])
        self.assertEqual(before, self.snapshot())

    def test_successful_http_start_preserves_sources_settings_and_job_association(self):
        sources = self.snapshot()["source_bytes"]
        response = self.post()
        self.assertEqual(202, response.status_code)
        payload = response.get_json()
        with self.database.snapshot_session() as session:
            training = session.get(TrainingRun, payload["training_id"])
            self.assertEqual(self.source_id, training.source_artifact_id)
            self.assertEqual(self.text_id, training.source_text_artifact_id)
            self.assertEqual({"epochs": 3}, training.settings_json)
            job = session.get(Job, training.job_id)
            self.assertEqual(payload["id"], job.id)
            self.assertEqual("queued", job.status)
            self.assertEqual(training.id, job.payload_json["training_id"])
            self.assertEqual(["gpu:default", "training:xtts"], job.resource_keys_json)
            self.assertEqual(
                1, len(session.scalars(select(JobEvent).where(JobEvent.job_id == job.id)).all())
            )
        self.assertEqual(sources, self.snapshot()["source_bytes"])

    def test_unauthorized_http_start_rejects_before_writes(self):
        before = self.snapshot()
        response = self.app.test_client().post("/api/v1/training", json=self.payload)
        self.assertEqual(401, response.status_code)
        self.assertEqual(before, self.snapshot())

    def test_list_uses_one_joined_query_and_stable_projected_timestamps(self):
        for _ in range(20):
            self.seed(job_status="failed", error="Before execution failed")
        before = self.snapshot()
        queries = []

        def capture(_connection, _cursor, statement, _parameters, _context, _many):
            sql = statement.lower()
            if sql.startswith("select") and ("from training_runs" in sql or "from jobs" in sql):
                queries.append(sql)

        event.listen(self.database.engine, "before_cursor_execute", capture)
        try:
            first = self.client.get("/api/v1/training").get_json()
            second = self.client.get("/api/v1/training").get_json()
        finally:
            event.remove(self.database.engine, "before_cursor_execute", capture)
        self.assertEqual(first, second)
        self.assertEqual(20, len(first["items"]))
        self.assertEqual(2, len(queries), queries)
        self.assertTrue(all("join jobs" in sql for sql in queries))
        self.assertEqual(before, self.snapshot())

    def test_retry_can_repair_terminal_job_without_a_prior_get(self):
        previous_id = self.seed(job_status="failed", error="Before execution failed")
        response = self.post(f"/{previous_id}/retry")
        self.assertEqual(202, response.status_code, response.get_json())
        self.assertEqual(previous_id, response.get_json()["retried_from"])
        with self.database.snapshot_session() as session:
            self.assertEqual("failed", session.get(TrainingRun, previous_id).status)
            retry = session.get(TrainingRun, response.get_json()["training_id"])
            self.assertEqual("queued", retry.status)
            self.assertIsNone(retry.error_message)
            self.assertIsNone(retry.output_artifact_id)

    def test_retry_with_deleted_source_refuses_atomically(self):
        training_id = self.seed(status="failed", job_status="failed")
        with self.database.session() as session:
            session.get(Artifact, self.text_id).state = "deleted"
        # Snapshot source bytes through their known path while resolution is refused.
        artifact = ArtifactService(self.database, self.paths).resolve(self.source_id)
        with self.database.snapshot_session() as session:
            before = [(item.id, item.job_id) for item in session.scalars(select(TrainingRun))]
            job_ids = list(session.scalars(select(Job.id)))
            event_ids = list(session.scalars(select(JobEvent.id)))
        response = self.post(f"/{training_id}/retry")
        self.assertEqual(404, response.status_code, response.get_json())
        with self.database.snapshot_session() as session:
            self.assertEqual(
                before, [(item.id, item.job_id) for item in session.scalars(select(TrainingRun))]
            )
            self.assertEqual(job_ids, list(session.scalars(select(Job.id))))
            self.assertEqual(event_ids, list(session.scalars(select(JobEvent.id))))
        self.assertEqual(b"training audio fixture", artifact[1].read_bytes())

    def test_expired_exhausted_job_repairs_training_without_get(self):
        training_id = self.seed()
        claimed = self.queue.claim("lost-worker")
        with self.database.session() as session:
            session.get(Job, claimed.id).lease_expires_at = utcnow() - timedelta(seconds=1)
        self.assertIsNone(self.queue.claim("replacement-worker"))
        with self.database.snapshot_session() as session:
            self.assertEqual("failed", session.get(TrainingRun, training_id).status)

    def test_owned_cancel_repairs_training_and_rejects_stale_generation(self):
        training_id = self.seed()
        claimed = self.queue.claim("training-worker")
        self.queue.request_cancel(claimed.id)
        before = self.snapshot()
        self.assertFalse(
            self.queue.cancel_owned(
                claimed.id, "training-worker", lease_generation=claimed.lease_generation + 1
            )
        )
        self.assertEqual(before, self.snapshot())
        self.assertTrue(
            self.queue.cancel_owned(
                claimed.id, "training-worker", lease_generation=claimed.lease_generation
            )
        )
        with self.database.snapshot_session() as session:
            self.assertEqual("canceled", session.get(TrainingRun, training_id).status)

    def test_completion_requires_domain_outcome_and_preserves_published_success(self):
        for published in (False, True):
            with self.subTest(published=published):
                training_id = self.seed()
                claimed = self.queue.claim("training-worker")
                if published:
                    with self.database.session() as session:
                        session.get(TrainingRun, training_id).status = "succeeded"
                self.queue.complete(
                    claimed.id, "training-worker", lease_generation=claimed.lease_generation
                )
                with self.database.snapshot_session() as session:
                    training = session.get(TrainingRun, training_id)
                    self.assertEqual("succeeded" if published else "failed", training.status)
                    if not published:
                        self.assertIn("final training outcome", training.error_message)

    def test_foreign_job_cannot_drive_or_be_canceled_by_training(self):
        for wrong_kind in (False, True):
            with self.subTest(wrong_kind=wrong_kind):
                training_id = self.seed()
                with self.database.session() as session:
                    training = session.get(TrainingRun, training_id)
                    job = session.get(Job, training.job_id)
                    if wrong_kind:
                        job.kind = "noop"
                    else:
                        job.payload_json = {"training_id": "different-training"}
                before = self.snapshot()
                response = self.post(f"/{training_id}/cancel")
                self.assertEqual("canceled", response.get_json()["status"])
                self.assertEqual(before["jobs"], self.snapshot()["jobs"])
                self.assertEqual(before["job_events"], self.snapshot()["job_events"])

    def test_failure_removes_training_purge_blocker_without_get(self):
        owner = SessionService(self.database).create("Training source owner")
        training_id = self.seed()
        with self.database.session() as session:
            session.get(Artifact, self.source_id).session_id = owner.id
        SessionService(self.database).trash(owner.id, owner.revision)
        purge = SessionPurgeService(self.database, self.paths)
        self.assertIn("unfinished:training_runs:queued", purge.preview(owner.id)["blockers"])
        claimed = self.queue.claim("training-worker")
        self.queue.fail(
            claimed.id,
            "training-worker",
            "training_failed",
            "Before execution failed",
            lease_generation=claimed.lease_generation,
        )
        self.assertFalse(
            any("unfinished:training_runs:" in item for item in purge.preview(owner.id)["blockers"])
        )
        with self.database.snapshot_session() as session:
            self.assertEqual("failed", session.get(TrainingRun, training_id).status)

    def test_http_missing_inputs_and_active_retry_preserve_rows(self):
        training_id = self.seed()
        cases = [
            ("/missing/retry", None, 404, "not_found"),
            ("/missing/cancel", None, 404, "not_found"),
            (f"/{training_id}/retry", None, 409, "training_active"),
            ("", {**self.payload, "source_artifact_id": "missing"}, 404, "not_found"),
            ("", {**self.payload, "voice_id": "missing"}, 404, "not_found"),
            ("", {**self.payload, "model_name": ""}, 422, "validation_error"),
        ]
        for suffix, payload, status, code in cases:
            with self.subTest(suffix=suffix, payload=payload):
                before = self.snapshot()
                response = self.post(suffix, payload)
                self.assertEqual(status, response.status_code, response.get_json())
                self.assertEqual(code, response.get_json()["error"]["code"])
                self.assertEqual(before, self.snapshot())

    def test_local_cli_list_projects_status_and_preserves_output_fields(self):
        training_id = self.seed(job_status="failed", error="Before execution failed")
        before = self.snapshot()
        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            mock.patch.object(cli, "_emit") as emit,
        ):
            self.assertEqual(0, cli.command_training_list(Namespace(json=True)))
        items, json_output = emit.call_args.args
        self.assertTrue(json_output)
        self.assertEqual(1, len(items))
        self.assertEqual(training_id, items[0]["id"])
        self.assertEqual("failed", items[0]["status"])
        self.assertEqual("Before execution failed", items[0]["error"])
        self.assertEqual(
            {
                "id",
                "model_name",
                "status",
                "job_id",
                "source_artifact_id",
                "output_artifact_id",
                "error",
            },
            set(items[0]),
        )
        self.assertEqual(before, self.snapshot())

    def test_local_cli_cancel_rollback_disposal_and_success(self):
        training_id = self.seed()
        args = Namespace(training_id=training_id, json=True)
        before = self.snapshot()
        native_cancel = JobQueue.request_cancel_in_session

        def fail_after_cancel(queue, session, job_id):
            native_cancel(queue, session, job_id)
            raise RuntimeError("Cancellation commit refused")

        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            mock.patch.object(cli, "_emit") as emit,
            mock.patch.object(self.database, "dispose", wraps=self.database.dispose) as dispose,
            mock.patch.object(JobQueue, "request_cancel_in_session", fail_after_cancel),
        ):
            with self.assertRaisesRegex(RuntimeError, "Cancellation commit refused"):
                cli.command_training_cancel(args)
            dispose.assert_called_once_with()
            emit.assert_not_called()
        self.assertEqual(before, self.snapshot())
        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            mock.patch.object(cli, "_emit") as emit,
        ):
            self.assertEqual(0, cli.command_training_cancel(args))
        self.assertEqual("canceled", emit.call_args.args[0]["status"])
        with self.database.snapshot_session() as session:
            self.assertEqual("canceled", session.get(TrainingRun, training_id).status)

    def test_local_cli_missing_training_remains_a_managed_error(self):
        stderr = io.StringIO()
        before = self.snapshot()
        with (
            mock.patch.object(cli, "_database", return_value=(self.paths, self.database)),
            redirect_stderr(stderr),
        ):
            self.assertEqual(5, cli.main(["--json", "training", "cancel", "missing"]))
        self.assertIn("not found", stderr.getvalue().lower())
        self.assertEqual(before, self.snapshot())
