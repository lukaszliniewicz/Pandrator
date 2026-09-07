import tempfile
import unittest
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Job
from pandrator.web.workspace import stable_hash


class MediaEditProposalRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.extension = self.app.extensions["pandrator"]
        self.session = self.extension["sessions"].create(
            "Media edit", workflow_kind="media_edit"
        )
        self.correction_settings_before = self.extension["workspace_settings"].get(
            self.session.id, "correction"
        )

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def test_model_override_is_job_local_and_hashed(self):
        with patch.object(
            self.extension["media_edit"],
            "state",
            return_value={"plan": {"revision": 1}},
        ):
            response = self.client.post(
                f"/api/v1/sessions/{self.session.id}/media-edit/propose",
                json={
                    "revision": 1,
                    "instructions": "  remove pauses  ",
                    "model": "  custom/provider-model  ",
                },
                headers={"X-CSRF-Token": self.csrf},
            )

        self.assertEqual(202, response.status_code, response.get_json())
        with self.extension["database"].session() as session:
            job = session.scalar(select(Job).where(Job.kind == "media_edit.propose"))
            self.assertIsNotNone(job)
            self.assertEqual(
                "custom/provider-model",
                job.payload_json["settings"]["correction_model"],
            )
            self.assertEqual(
                stable_hash(job.payload_json["settings"]),
                job.payload_json["settings_hash"],
            )
        correction_settings_after = self.extension["workspace_settings"].get(
            self.session.id, "correction"
        )
        self.assertEqual(
            self.correction_settings_before["global"],
            correction_settings_after["global"],
        )
        self.assertEqual(
            self.correction_settings_before["override"],
            correction_settings_after["override"],
        )

    def test_boundary_refinement_requires_revision_and_replays_with_etag(self):
        endpoint = f"/api/v1/sessions/{self.session.id}/media-edit/boundary"
        missing = self.client.patch(
            endpoint,
            json={"cut_index": 1, "edge": "start", "delta_ms": -100},
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(428, missing.status_code)

        result = {
            "schema_version": "1",
            "session_id": self.session.id,
            "previous_revision": 2,
            "current_revision": {
                "revision": 3,
                "revision_id": "revision-3",
                "content_hash": "hash-3",
                "reviewed": False,
            },
            "change": {
                "type": "boundary_refinement",
                "cut_index": 1,
                "edge": "start",
                "from_ms": 1_000,
                "to_ms": 900,
                "no_op": False,
            },
            "affected_cut": {"index": 1, "start_ms": 900, "end_ms": 2_000},
        }
        headers = {
            "X-CSRF-Token": self.csrf,
            "If-Match": '"2"',
            "Idempotency-Key": "boundary-test-0001",
        }
        with patch.object(
            self.extension["media_edit"],
            "refine_boundary",
            return_value=result,
        ) as refine:
            response = self.client.patch(
                endpoint,
                json={"cut_index": 1, "edge": "start", "delta_ms": -100},
                headers=headers,
            )
            replay = self.client.patch(
                endpoint,
                json={"cut_index": 1, "edge": "start", "delta_ms": -100},
                headers=headers,
            )

        self.assertEqual(200, response.status_code, response.get_json())
        self.assertEqual('"3"', response.headers["ETag"])
        self.assertEqual(200, replay.status_code, replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual('"3"', replay.headers["ETag"])
        self.assertEqual(result, replay.get_json())
        refine.assert_called_once_with(
            self.session.id,
            2,
            cut_index=1,
            edge="start",
            position_ms=None,
            delta_ms=-100,
        )

    def test_cut_inspection_query_is_bounded_before_service_dispatch(self):
        endpoint = f"/api/v1/sessions/{self.session.id}/media-edit/cuts"
        with patch.object(
            self.extension["media_edit"],
            "list_cuts",
            return_value={"schema_version": "1", "cuts": []},
        ) as list_cuts:
            response = self.client.get(
                endpoint,
                query_string={
                    "revision": 2,
                    "cut_index": 1,
                    "edge": "end",
                    "context_ms": 750,
                    "cue_limit": 3,
                },
            )

        self.assertEqual(200, response.status_code, response.get_json())
        list_cuts.assert_called_once_with(
            self.session.id,
            2,
            cut_index=1,
            edge="end",
            context_ms=750,
            cue_limit=3,
        )
        too_wide = self.client.get(
            endpoint,
            query_string={
                "cut_index": 1,
                "edge": "end",
                "context_ms": 30_001,
            },
        )
        self.assertEqual(422, too_wide.status_code)


if __name__ == "__main__":
    unittest.main()
