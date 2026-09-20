"""A duration warning stops rendering; a decision reuses the captured export."""

from copy import deepcopy
import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import Worker
from pandrator.web.models import Job
from pandrator.web.soundtrack_export import resolve_video_tail_extension_ms


class ExportVideoTailDecisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.services = self.app.extensions["pandrator"]["services"]
        self.addCleanup(self.services.database.dispose)
        self.sid = self.client.post(
            "/api/v1/sessions",
            headers=self.headers,
            json={"name": "Duration decision", "workflow_kind": "voiceover"},
        ).get_json()["id"]
        self.queue = self.services.jobs
        self.payload = {
            "session_id": self.sid,
            "settings": {
                "audio_mode": "mixed",
                "generation_run_id": "pinned-run",
                "video_tail_extension_policy": "ask",
            },
            "export_contract": {
                "source_artifact_id": "pinned-source",
                "source_content_hash": "original",
            },
            "resolved_settings_snapshot": {
                "output": {
                    "video_tail_extension_policy": "ask",
                    "mix_voice_gain_db": -3,
                },
                "audio": {"synchronization_speed": 1.1},
            },
        }
        self.renders = []

        def export(payload, _progress, _cancel):
            tail = resolve_video_tail_extension_ms(
                reference_duration=3,
                generated_duration=38,
                fps=25,
                settings=payload["settings"],
            )
            self.renders.append(tail)
            return {"artifact_ids": ["output"]}

        self.worker = Worker(
            self.queue, "duration-test-worker", {"export.variant": export}
        )

    def warning_job(self):
        job = self.queue.enqueue(
            "export.variant",
            deepcopy(self.payload),
            session_id=self.sid,
            resource_keys=[f"session:{self.sid}"],
        )
        self.assertTrue(self.worker.run_once())
        warning = self.queue.get(job.id)
        self.assertEqual(warning.error_code, "VideoTailExtensionRequired")
        self.assertEqual(warning.status, "failed")
        self.assertIn("35.00 seconds longer", warning.error_message)
        self.assertEqual(self.renders, [])
        return warning

    def decide(self, job_id, action):
        return self.client.post(
            f"/api/v1/jobs/{job_id}/video-tail-decision",
            headers=self.headers,
            json={"action": action},
        )

    def test_extend_is_idempotent_and_preserves_original_export(self):
        warning = self.warning_job()
        response = self.decide(warning.id, "extend")
        self.assertEqual(response.status_code, 202, response.get_json())
        continued_id = response.get_json()["id"]
        self.assertEqual(
            self.decide(warning.id, "extend").get_json()["id"], continued_id
        )
        continued = self.queue.get(continued_id)
        expected = deepcopy(self.payload)
        expected["settings"]["video_tail_extension_policy"] = "extend"
        expected["resolved_settings_snapshot"]["output"][
            "video_tail_extension_policy"
        ] = "extend"
        self.assertEqual(continued.payload_json, expected)
        self.assertEqual(continued.resource_keys_json, [f"session:{self.sid}"])
        self.assertEqual(self.queue.get(warning.id).payload_json, self.payload)
        self.assertEqual(self.decide(warning.id, "stop").status_code, 409)
        self.assertTrue(self.worker.run_once())
        self.assertEqual(self.renders, [35040])
        self.assertEqual(self.queue.get(continued_id).status, "succeeded")
        self.assertFalse(self.worker.run_once())

    def test_stop_persists_without_rendering_or_enqueuing(self):
        warning = self.warning_job()
        for _ in range(2):
            response = self.decide(warning.id, "stop")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(
                response.get_json()["result_json"]["video_tail_decision"], "stop"
            )
        self.assertEqual(self.decide(warning.id, "extend").status_code, 409)
        self.assertFalse(self.worker.run_once())
        self.assertEqual(self.renders, [])

    def test_rejects_unrelated_jobs_invalid_actions_and_missing_csrf(self):
        job = self.queue.enqueue("noop", {}, session_id=self.sid)
        self.assertEqual(self.decide(job.id, "extend").status_code, 409)
        self.assertEqual(self.decide(job.id, "invalid").status_code, 422)
        self.assertEqual(self.decide("missing", "extend").status_code, 404)
        response = self.client.post(
            f"/api/v1/jobs/{job.id}/video-tail-decision", json={"action": "extend"}
        )
        self.assertEqual(response.status_code, 403)
        with self.services.database.session() as session:
            self.assertEqual(session.get(Job, job.id).status, "queued")

    def test_extension_requires_run_scope(self):
        warning = self.warning_job()
        extension = self.app.extensions["pandrator"]
        identity = extension["identity"].snapshot(observed_origin="http://localhost")
        _record, raw = extension["auth"].create_api_token(
            "write without run",
            scopes=["app.write"],
            target_instance_id=identity.instance_id,
            canonical_origin=identity.canonical_origin,
        )
        client = self.app.test_client()
        response = client.post(
            f"/api/v1/jobs/{warning.id}/video-tail-decision",
            headers={"Authorization": f"Bearer {raw}"},
            json={"action": "extend"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.get_json()["error"]["code"], "scope_denied")
        self.assertFalse(self.worker.run_once())
