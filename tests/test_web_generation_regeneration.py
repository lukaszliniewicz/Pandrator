import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import GenerationRun, Job


class GenerationRegenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bootstrap = BootstrapTokenStore()
        token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.database = self.app.extensions["pandrator"]["database"]
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Regeneration", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.session_id = created.get_json()["id"]
        plan = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan",
            json={"segments": [{"text": "One"}, {"text": "Two"}]},
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        self.segment_ids = [
            item["id"]
            for item in self.client.get(
                f"/api/v1/sessions/{self.session_id}/generation-segments"
            ).get_json()["items"]
        ]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _start(self, **payload):
        response = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-runs",
            json=payload,
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        return response.get_json()

    def test_repeated_default_regeneration_replaces_single_root_baton(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])

        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            second_run = session.get(GenerationRun, second["id"])
            first_job = session.get(Job, first["job_id"])
            second_job = session.get(Job, second["job_id"])
            self.assertEqual(root["id"], first_run.source_generation_run_id)
            self.assertEqual(root["id"], second_run.source_generation_run_id)
            self.assertTrue(root_run.pause_requested)
            self.assertEqual("pausing", root_run.status)
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertNotIn(
                "auto_resume_source_generation_run_id", first_job.payload_json
            )
            self.assertTrue(second_run.resume_source_on_completion)
            self.assertEqual(
                root["id"],
                second_job.payload_json["auto_resume_source_generation_run_id"],
            )

            root_run.status = "paused"

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        self.assertIsNone(
            handlers._resume_generation_after_regeneration(first["id"], root["id"])
        )
        resumed_job_id = handlers._resume_generation_after_regeneration(
            second["id"], root["id"]
        )
        self.assertIsNotNone(resumed_job_id)
        with self.database.session() as session:
            self.assertEqual("queued", session.get(GenerationRun, root["id"]).status)
            self.assertEqual(
                resumed_job_id, session.get(GenerationRun, root["id"]).job_id
            )

    def test_new_regeneration_pauses_root_again_after_previous_resume(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "paused"
            session.get(GenerationRun, first["id"]).status = "completed"

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        resumed_job_id = handlers._resume_generation_after_regeneration(
            first["id"], root["id"]
        )
        self.assertIsNotNone(resumed_job_id)
        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])

        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            second_run = session.get(GenerationRun, second["id"])
            self.assertEqual("pausing", root_run.status)
            self.assertTrue(root_run.pause_requested)
            self.assertEqual(root["id"], second_run.source_generation_run_id)
            self.assertTrue(second_run.resume_source_on_completion)

    def test_running_regeneration_replacement_keeps_one_baton_when_root_paused(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            first_job = session.get(Job, first["job_id"])
            root_run.status = "paused"
            first_run.status = "running"
            first_job.status = "running"

        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            second_run = session.get(GenerationRun, second["id"])
            first_job = session.get(Job, first["job_id"])
            second_job = session.get(Job, second["job_id"])
            self.assertEqual(root["id"], first_run.source_generation_run_id)
            self.assertEqual(root["id"], second_run.source_generation_run_id)
            self.assertTrue(root_run.pause_requested)
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertNotIn(
                "auto_resume_source_generation_run_id", first_job.payload_json
            )
            self.assertTrue(second_run.resume_source_on_completion)
            self.assertEqual(
                root["id"],
                second_job.payload_json["auto_resume_source_generation_run_id"],
            )

    def test_replacement_transfers_baton_after_child_terminal_status_commits(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "paused"
            # run_generation commits this status immediately before opening
            # the transaction that consumes the resume baton.
            session.get(GenerationRun, first["id"]).status = "partial"

        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])
        with self.database.session() as session:
            first_run = session.get(GenerationRun, first["id"])
            second_run = session.get(GenerationRun, second["id"])
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertTrue(second_run.resume_source_on_completion)
            self.assertEqual(root["id"], second_run.source_generation_run_id)

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        self.assertIsNone(
            handlers._resume_generation_after_regeneration(first["id"], root["id"])
        )
        self.assertIsNotNone(
            handlers._resume_generation_after_regeneration(second["id"], root["id"])
        )

    def test_default_selection_flattens_legacy_nested_regeneration_chain(self):
        root = self._start()
        first = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[0]],
            generation_run_id=root["id"],
        )
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "failed"
            session.get(Job, root["job_id"]).status = "failed"
            session.get(GenerationRun, first["id"]).status = "failed"
            session.get(Job, first["job_id"]).status = "failed"
        nested = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[1]],
            generation_run_id=first["id"],
        )
        with self.database.session() as session:
            session.get(GenerationRun, nested["id"]).status = "failed"
            session.get(Job, nested["job_id"]).status = "failed"

        flattened = self._start(
            operation="regenerate", segment_ids=[self.segment_ids[0]]
        )
        self.assertEqual(root["id"], flattened["source_generation_run_id"])

    def test_explicit_pause_cancels_stale_terminal_baton(self):
        root = self._start()
        first = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[0]],
            generation_run_id=root["id"],
        )
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            root_run.status = "paused"
            first_run.status = "completed"

        pause = self.client.post(
            f"/api/v1/generation-runs/{root['id']}/pause", headers=self.headers
        )
        self.assertEqual(202, pause.status_code, pause.get_json())

        replacement = self._start(
            operation="regenerate", segment_ids=[self.segment_ids[0]]
        )
        with self.database.session() as session:
            replacement_run = session.get(GenerationRun, replacement["id"])
            self.assertEqual(root["id"], replacement_run.source_generation_run_id)
            self.assertFalse(replacement_run.resume_source_on_completion)
            first_run = session.get(GenerationRun, first["id"])
            first_job = session.get(Job, first["job_id"])
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertNotIn(
                "auto_resume_source_generation_run_id", first_job.payload_json
            )

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        self.assertIsNone(
            handlers._resume_generation_after_regeneration(first["id"], root["id"])
        )
        with self.database.session() as session:
            self.assertEqual("paused", session.get(GenerationRun, root["id"]).status)


if __name__ == "__main__":
    unittest.main()
