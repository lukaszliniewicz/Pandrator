import tempfile
import unittest

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import AppSettingHistory


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap)
        self.client = self.app.test_client()
        self.csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_wizard_visibility_is_revisioned_and_history_is_retained(self):
        missing = self.client.get("/api/v1/settings/wizard")
        self.assertEqual(missing.status_code, 404)
        created = self.client.put(
            "/api/v1/settings/wizard",
            json={"value": {"visible": False, "version": 1, "setup_completed": False}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(created.status_code, 200)
        self.assertEqual(created.headers["ETag"], '"1"')
        conflict = self.client.put(
            "/api/v1/settings/wizard",
            json={"value": {"visible": True}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(conflict.status_code, 409)
        updated = self.client.put(
            "/api/v1/settings/wizard",
            json={"value": {"visible": True, "version": 2, "setup_completed": True}},
            headers={**self.headers, "If-Match": '"1"'},
        )
        self.assertEqual(updated.get_json()["revision"], 2)
        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(AppSettingHistory)), 1)


if __name__ == "__main__":
    unittest.main()
