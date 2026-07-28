import inspect
import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.http_lifecycle import ApiGuards
from pandrator.web.models import JobEvent
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
