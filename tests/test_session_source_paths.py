"""Sources expose the actual managed file path without per-source lookups."""

import tempfile
import unittest
from pathlib import Path

from pandrator.web.api import create_app
from pandrator.web.artifacts import ArtifactService
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, SourceAsset
from tests.web_test_support import prepare_web_test_data_root


class SessionSourcePathTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        response = self.client.post(
            "/api/v1/sessions",
            json={"name": "Source paths", "workflow_kind": "audiobook"},
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(201, response.status_code, response.get_json())
        self.session_id = response.get_json()["id"]
        self.extension = self.app.extensions["pandrator"]
        self.path = Path(self.temporary.name) / "Zażółć źródło (1).txt"
        self.path.write_text("A managed source file.", encoding="utf-8")
        artifact = ArtifactService(
            self.extension["database"], self.extension["paths"]
        ).register(
            self.path,
            kind="source",
            role="upload",
            session_id=self.session_id,
        )
        self.artifact_id = artifact.id
        asset = self.extension["source_library"].ensure_for_artifact(
            artifact.id, display_name=self.path.name, kind="txt"
        )
        self.asset_id = asset.id
        self.extension["source_library"].attach(self.session_id, asset.id)

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def source(self):
        response = self.client.get(
            f"/api/v1/sessions/{self.session_id}/sources"
        )
        self.assertEqual(200, response.status_code, response.get_json())
        return response.get_json()["items"][0]

    def test_uploaded_source_exposes_absolute_unicode_path(self):
        source = self.source()
        self.assertIsNone(source["external_path"])
        self.assertEqual(str(self.path.resolve()), source["path"])
        self.assertEqual(self.artifact_id, source["artifact_id"])

    def test_original_location_is_distinct_from_managed_path(self):
        with self.extension["database"].session() as session:
            asset = session.get(SourceAsset, self.asset_id)
            asset.external_path = "/old/location/source.txt"
        source = self.source()
        self.assertEqual("/old/location/source.txt", source["external_path"])
        self.assertEqual(str(self.path.resolve()), source["path"])

    def test_invalid_legacy_path_does_not_break_source_list(self):
        with self.extension["database"].session() as session:
            artifact = session.get(Artifact, self.artifact_id)
            artifact.relative_path = "../outside-managed-root.txt"
        source = self.source()
        self.assertIsNone(source["path"])
        self.assertEqual(self.asset_id, source["id"])


if __name__ == "__main__":
    unittest.main()
