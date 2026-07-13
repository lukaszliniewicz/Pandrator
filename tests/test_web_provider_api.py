import tempfile
import unittest
from unittest import mock

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore


class ProviderApiTests(unittest.TestCase):
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

    def test_zero_temperature_is_preserved_and_default_deletion_requires_replacement(self):
        provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "OpenAI", "secret_ref": "env:OPENAI_API_KEY"},
            headers=self.headers,
        ).get_json()
        first = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "first", "is_default": True, "default_temperature": 0},
            headers=self.headers,
        ).get_json()
        second = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "second"},
            headers=self.headers,
        ).get_json()
        self.assertEqual(first["default_temperature"], 0)
        blocked = self.client.delete(
            f"/api/v1/providers/{provider['id']}/models/{first['id']}",
            json={},
            headers=self.headers,
        )
        self.assertEqual(blocked.status_code, 409)
        removed = self.client.delete(
            f"/api/v1/providers/{provider['id']}/models/{first['id']}",
            json={"replacement_model_id": "second"},
            headers=self.headers,
        )
        self.assertEqual(removed.status_code, 204)
        records = self.client.get(f"/api/v1/providers/{provider['id']}/models").get_json()["items"]
        self.assertEqual(records, [{**second, "is_default": True}])

    def test_refresh_merges_discovery_without_overwriting_manual_model(self):
        provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "Local", "base_url": "http://127.0.0.1:1234/v1"},
            headers=self.headers,
        ).get_json()
        self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "manual", "is_default": True, "default_reasoning_effort": "custom-fast"},
            headers=self.headers,
        )
        with mock.patch("pandrator.logic.llm_handler._detect_models_for_builtin_provider", return_value=["manual", "discovered"]):
            result = self.client.post(
                f"/api/v1/providers/{provider['id']}/models/refresh",
                json={},
                headers=self.headers,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["added"], ["discovered"])
        records = self.client.get(f"/api/v1/providers/{provider['id']}/models").get_json()["items"]
        manual = next(item for item in records if item["model_id"] == "manual")
        self.assertEqual(manual["default_reasoning_effort"], "custom-fast")

    def test_provider_profiles_update_and_global_default_selection(self):
        profiles = self.client.get("/api/v1/providers/profiles").get_json()["items"]
        self.assertIn("lm-studio", {item["id"] for item in profiles})
        first_provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "Local", "base_url": "http://127.0.0.1:1234/v1"},
            headers=self.headers,
        ).get_json()
        first_model = self.client.post(
            f"/api/v1/providers/{first_provider['id']}/models",
            json={"model_id": "local-model", "is_default": True},
            headers=self.headers,
        ).get_json()
        second_provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "anthropic", "label": "Cloud", "secret_ref": "env:ANTHROPIC_API_KEY"},
            headers=self.headers,
        ).get_json()
        second_model = self.client.post(
            f"/api/v1/providers/{second_provider['id']}/models",
            json={"model_id": "cloud-model", "is_default": True},
            headers=self.headers,
        ).get_json()
        refreshed_first = self.client.get(f"/api/v1/providers/{first_provider['id']}/models").get_json()["items"]
        self.assertFalse(next(item for item in refreshed_first if item["id"] == first_model["id"])["is_default"])
        updated = self.client.patch(
            f"/api/v1/providers/{first_provider['id']}",
            json={"label": "LM Studio", "options": {"profile_id": "lm-studio", "request_options": {"organization": "studio"}}},
            headers={**self.headers, "If-Match": f'"{first_provider["revision"]}"'},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["label"], "LM Studio")
        blocked = self.client.delete(f"/api/v1/providers/{second_provider['id']}", json={}, headers=self.headers)
        self.assertEqual(blocked.status_code, 409)
        removed = self.client.delete(
            f"/api/v1/providers/{second_provider['id']}",
            json={"replacement_model_record_id": first_model["id"]},
            headers=self.headers,
        )
        self.assertEqual(removed.status_code, 204)
        remaining = self.client.get(f"/api/v1/providers/{first_provider['id']}/models").get_json()["items"]
        self.assertTrue(remaining[0]["is_default"])


if __name__ == "__main__":
    unittest.main()
