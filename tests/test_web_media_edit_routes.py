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


if __name__ == "__main__":
    unittest.main()
