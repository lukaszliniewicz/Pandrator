import tempfile
import unittest

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Voice
from tests.web_test_support import prepare_web_test_data_root


class VoiceCategoryApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
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

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def _headers(self, revision: int | None = None) -> dict[str, str]:
        headers = {"X-CSRF-Token": self.csrf}
        if revision is not None:
            headers["If-Match"] = f'"{revision}"'
        return headers

    def _read(self, voice_id: str) -> dict:
        items = self.client.get("/api/v1/voices").get_json()["items"]
        return next(item for item in items if item["id"] == voice_id)

    def test_create_update_and_readback_preserve_metadata(self):
        created_response = self.client.post(
            "/api/v1/voices",
            json={"name": "Category narrator", "voice_category": "male"},
            headers=self._headers(),
        )
        self.assertEqual(201, created_response.status_code, created_response.get_json())
        created = created_response.get_json()
        self.assertEqual("male", created["voice_category"])
        self.assertEqual("male", created["metadata_json"]["voice_category"])

        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            voice = session.get(Voice, created["id"])
            voice.metadata_json = {
                "providers": {"custom": {"status": "linked"}},
                "custom_metadata": {"keep": True},
                "voice_category": "male",
            }

        updated_response = self.client.patch(
            f"/api/v1/voices/{created['id']}",
            json={"voice_category": "female"},
            headers=self._headers(created["revision"]),
        )
        self.assertEqual(200, updated_response.status_code, updated_response.get_json())
        updated = updated_response.get_json()
        self.assertEqual(created["revision"] + 1, updated["revision"])
        self.assertEqual("female", updated["voice_category"])
        self.assertEqual("female", updated["metadata_json"]["voice_category"])
        self.assertEqual(
            {"status": "linked"}, updated["metadata_json"]["providers"]["custom"]
        )
        self.assertEqual({"keep": True}, updated["metadata_json"]["custom_metadata"])

        unspecified_response = self.client.patch(
            f"/api/v1/voices/{created['id']}",
            json={"voice_category": None},
            headers=self._headers(updated["revision"]),
        )
        self.assertEqual(200, unspecified_response.status_code)
        unspecified = unspecified_response.get_json()
        self.assertEqual("unspecified", unspecified["voice_category"])
        self.assertEqual(
            "unspecified", unspecified["metadata_json"]["voice_category"]
        )

        readback = self._read(created["id"])
        self.assertEqual("unspecified", readback["voice_category"])
        self.assertEqual(
            "unspecified", readback["metadata_json"]["voice_category"]
        )

    def test_category_update_obeys_stale_etag(self):
        created = self.client.post(
            "/api/v1/voices",
            json={"name": "Stale category narrator"},
            headers=self._headers(),
        ).get_json()
        updated = self.client.patch(
            f"/api/v1/voices/{created['id']}",
            json={"voice_category": "androgynous"},
            headers=self._headers(created["revision"]),
        )
        self.assertEqual(200, updated.status_code, updated.get_json())

        stale = self.client.patch(
            f"/api/v1/voices/{created['id']}",
            json={"voice_category": "female"},
            headers=self._headers(created["revision"]),
        )
        self.assertEqual(409, stale.status_code, stale.get_json())
        self.assertEqual("revision_conflict", stale.get_json()["error"]["code"])
        current = self._read(created["id"])
        self.assertEqual("androgynous", current["voice_category"])
        self.assertEqual(updated.get_json()["revision"], current["revision"])

    def test_invalid_category_is_rejected_on_create_and_update(self):
        invalid_create = self.client.post(
            "/api/v1/voices",
            json={"name": "Invalid category narrator", "voice_category": "unknown"},
            headers=self._headers(),
        )
        self.assertEqual(422, invalid_create.status_code, invalid_create.get_json())
        self.assertEqual("validation_error", invalid_create.get_json()["error"]["code"])

        created = self.client.post(
            "/api/v1/voices",
            json={"name": "Valid category narrator"},
            headers=self._headers(),
        ).get_json()
        invalid_update = self.client.patch(
            f"/api/v1/voices/{created['id']}",
            json={"voice_category": "unknown"},
            headers=self._headers(created["revision"]),
        )
        self.assertEqual(422, invalid_update.status_code, invalid_update.get_json())
        self.assertEqual("validation_error", invalid_update.get_json()["error"]["code"])


if __name__ == "__main__":
    unittest.main()
