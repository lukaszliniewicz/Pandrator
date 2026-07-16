import tempfile
import unittest
import json

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import hydrate_tts_settings, shared_provider_credential_key
from pandrator.web.models import AppSetting, AppSettingHistory, StoredCredential
from pandrator.web.tts_optimization import DEFAULT_FIRST_PROMPT, DEFAULT_PROMPT, DEFAULT_SECOND_PROMPT, DEFAULT_THIRD_PROMPT


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

    def test_tts_optimization_prompts_are_visible_in_builtin_settings(self):
        payload = self.client.get("/api/v1/defaults/text").get_json()["builtin"]

        self.assertEqual(DEFAULT_PROMPT, payload["combined_prompt"])
        self.assertEqual(DEFAULT_FIRST_PROMPT, payload["first_prompt"])
        self.assertEqual(DEFAULT_SECOND_PROMPT, payload["second_prompt"])
        self.assertEqual(DEFAULT_THIRD_PROMPT, payload["third_prompt"])

    def test_tts_api_key_is_extracted_from_settings_and_never_returned(self):
        secret = "speech-secret-value"
        response = self.client.put(
            "/api/v1/settings/services.tts",
            json={"value": {"provider_configs": [{"id": "openai", "name": "OpenAI", "api_key": secret, "credential_configured": True, "credential_source": "request"}]}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertNotIn(secret, json.dumps(response.get_json()))
        fetched = self.client.get("/api/v1/settings/services.tts").get_json()
        self.assertNotIn(secret, json.dumps(fetched))
        catalogue = self.client.get("/api/v1/services/tts").get_json()
        self.assertNotIn(secret, json.dumps(catalogue))
        openai = next(item for item in catalogue["services"] if item["id"] == "openai")
        self.assertTrue(openai["credential_configured"])
        self.assertEqual("database", openai["credential_source"])

        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            setting = session.get(AppSetting, "services.tts")
            self.assertNotIn("api_key", json.dumps(setting.value_json))
            self.assertNotIn("credential_configured", json.dumps(setting.value_json))
            self.assertNotIn("credential_source", json.dumps(setting.value_json))
            stored = session.get(StoredCredential, shared_provider_credential_key("openai"))
            self.assertEqual(secret, stored.secret_value)
            stored_value = dict(setting.value_json)
        hydrated = hydrate_tts_settings(
            database,
            self.app.extensions["pandrator"]["paths"],
            {**stored_value, "service": "OpenAI"},
        )
        from pandrator.logic import tts_handler
        runtime_service = tts_handler.get_service_config(hydrated, "openai")
        self.assertEqual(secret, runtime_service["api_key"])
        self.assertEqual("", runtime_service["api_key_env"])

        cleared_value = fetched["value"]
        cleared_value["provider_configs"][0]["clear_api_key"] = True
        cleared = self.client.put(
            "/api/v1/settings/services.tts",
            json={"value": cleared_value},
            headers={**self.headers, "If-Match": '"1"'},
        )
        self.assertEqual(200, cleared.status_code, cleared.get_json())
        with database.session() as session:
            self.assertIsNone(session.get(StoredCredential, shared_provider_credential_key("openai")))

    def test_openai_key_saved_for_llm_is_reused_by_tts(self):
        secret = "one-openai-key"
        provider = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "OpenAI", "api_key": secret},
            headers=self.headers,
        )
        self.assertEqual(201, provider.status_code, provider.get_json())
        catalogue = self.client.get("/api/v1/services/tts").get_json()
        openai = next(item for item in catalogue["services"] if item["id"] == "openai")
        self.assertTrue(openai["credential_configured"])

        database = self.app.extensions["pandrator"]["database"]
        hydrated = hydrate_tts_settings(
            database,
            self.app.extensions["pandrator"]["paths"],
            {"service": "OpenAI"},
        )
        from pandrator.logic import tts_handler
        self.assertEqual(secret, tts_handler.get_service_config(hydrated, "openai")["api_key"])

    def test_generic_settings_reject_inline_credentials_but_allow_token_counts(self):
        rejected = self.client.put(
            "/api/v1/settings/custom",
            json={"value": {"api_key": "secret"}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(422, rejected.status_code)
        allowed = self.client.put(
            "/api/v1/settings/custom",
            json={"value": {"max_tokens": 2048}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, allowed.status_code, allowed.get_json())


if __name__ == "__main__":
    unittest.main()
