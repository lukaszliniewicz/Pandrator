import tempfile
import unittest
import json
import os
from unittest import mock

from sqlalchemy import func, select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import hydrate_tts_settings, shared_provider_credential_key
from pandrator.web.models import AppSetting, AppSettingHistory, StoredCredential
from pandrator.web.tts_optimization import DEFAULT_FIRST_PROMPT, DEFAULT_PROMPT, DEFAULT_SECOND_PROMPT, DEFAULT_THIRD_PROMPT
from tests.web_test_support import prepare_web_test_data_root


class SettingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
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
        self.assertIn("Spell out abbreviations and titles", DEFAULT_PROMPT)
        self.assertIn("Convert Roman numerals to English words", DEFAULT_PROMPT)
        self.assertIn("OCR artifacts", DEFAULT_SECOND_PROMPT)
        self.assertIn("FOREIGN, NON-ENGLISH", DEFAULT_THIRD_PROMPT)

    def test_generation_prompt_has_an_empty_tts_default(self):
        payload = self.client.get("/api/v1/defaults/tts").get_json()["builtin"]

        self.assertEqual("", payload["generation_prompt"])

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

    def test_tts_environment_reference_is_verified_and_persisted_without_value(self):
        setting = {
            "provider_configs": [
                {
                    "id": "voxcpm",
                    "name": "VoxCPM",
                    "credential_backend": "environment",
                    "credential_reference": "PANDRATOR_VOXCPM_TEST_KEY",
                }
            ]
        }
        with mock.patch.dict(os.environ, {"PANDRATOR_VOXCPM_TEST_KEY": ""}):
            rejected = self.client.put(
                "/api/v1/settings/services.tts",
                json={"value": setting},
                headers={**self.headers, "If-Match": '"0"'},
            )
        self.assertEqual(422, rejected.status_code)
        self.assertEqual(
            404,
            self.client.get("/api/v1/settings/services.tts").status_code,
        )

        secret = "voxcpm-environment-secret"
        with mock.patch.dict(
            os.environ,
            {"PANDRATOR_VOXCPM_TEST_KEY": secret},
        ):
            saved = self.client.put(
                "/api/v1/settings/services.tts",
                json={"value": setting},
                headers={**self.headers, "If-Match": '"0"'},
            )
            self.assertEqual(200, saved.status_code, saved.get_json())
            self.assertNotIn(secret, json.dumps(saved.get_json()))
            service = next(
                item
                for item in self.client.get(
                    "/api/v1/services/tts"
                ).get_json()["services"]
                if item["id"] == "voxcpm"
            )
            self.assertEqual("environment", service["credential_backend"])
            self.assertEqual("environment", service["credential_source"])
            self.assertEqual(
                "PANDRATOR_VOXCPM_TEST_KEY",
                service["credential_reference"],
            )
            database = self.app.extensions["pandrator"]["database"]
            hydrated = hydrate_tts_settings(
                database,
                self.app.extensions["pandrator"]["paths"],
                {
                    **saved.get_json()["value"],
                    "service": "VoxCPM",
                },
            )
            from pandrator.logic import tts_handler

            runtime = tts_handler.get_service_config(hydrated, "voxcpm")
            self.assertEqual(
                "PANDRATOR_VOXCPM_TEST_KEY",
                runtime["api_key_env"],
            )
            self.assertEqual("", runtime["api_key"])

    def test_managed_tts_binding_is_typed_and_does_not_change_the_default(self):
        saved = self.client.put(
            "/api/v1/settings/services.tts",
            json={
                "value": {
                    "provider_configs": [
                        {
                            "id": "xtts",
                            "name": "XTTS",
                            "api_base": "http://external.example.test:8020",
                            "connection_mode": "managed_local",
                            "managed_service_id": "tts.xtts",
                        }
                    ]
                }
            },
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        stored = saved.get_json()["value"]["provider_configs"][0]
        self.assertEqual("managed_local", stored["connection_mode"])
        self.assertEqual("tts.xtts", stored["managed_service_id"])
        self.assertEqual(
            "XTTS",
            self.client.get("/api/v1/services/tts").get_json()["default_service"],
        )

        rejected = self.client.put(
            "/api/v1/settings/services.tts",
            json={
                "value": {
                    "provider_configs": [
                        {
                            **stored,
                            "managed_service_id": "tts.chatterbox",
                        }
                    ]
                }
            },
            headers={**self.headers, "If-Match": '"1"'},
        )
        self.assertEqual(422, rejected.status_code)

    def test_managed_tts_endpoint_is_resolved_at_execution_time(self):
        class FakeManager:
            @staticmethod
            def managed_service(service_id):
                self.assertEqual("tts.xtts", service_id)
                return {
                    "id": service_id,
                    "endpoint": "http://127.0.0.1:9123",
                    "health": {"state": "healthy"},
                }

        database = self.app.extensions["pandrator"]["database"]
        hydrated = hydrate_tts_settings(
            database,
            self.app.extensions["pandrator"]["paths"],
            {
                "service": "XTTS",
                "provider_configs": [
                    {
                        "id": "xtts",
                        "connection_mode": "managed_local",
                        "managed_service_id": "tts.xtts",
                        "api_base": "http://external.example.test:8020",
                    }
                ],
            },
            manager_bridge=FakeManager(),
        )
        self.assertEqual(
            "http://127.0.0.1:9123",
            hydrated["xtts_base_url"],
        )
        from pandrator.logic import tts_handler

        runtime = tts_handler.get_service_config(hydrated, "xtts")
        self.assertEqual("http://127.0.0.1:9123", runtime["api_base"])

    def test_tts_catalogue_projects_manager_state_without_persisting_endpoint(self):
        saved = self.client.put(
            "/api/v1/settings/services.tts",
            json={
                "value": {
                    "provider_configs": [
                        {
                            "id": "xtts",
                            "name": "XTTS",
                            "api_base": "http://external.example.test:8020",
                            "connection_mode": "managed_local",
                            "managed_service_id": "tts.xtts",
                        }
                    ]
                }
            },
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        inventory = {
            "status": {"configuration_revision": 4},
            "components": [
                {
                    "definition": {
                        "id": "xtts",
                        "supported_actions": [
                            "install",
                            "update",
                            "repair",
                            "remove",
                            "start",
                            "stop",
                        ],
                    },
                    "inspection": {"state": "present"},
                }
            ],
            "services": [
                {
                    "id": "tts.xtts",
                    "endpoint": "http://127.0.0.1:9234",
                    "health": {"state": "healthy"},
                    "process": {"pid": 42},
                }
            ],
        }
        bridge = self.app.extensions["pandrator"]["manager_bridge"]
        with mock.patch.object(bridge, "inventory", return_value=inventory):
            catalogue = self.client.get("/api/v1/services/tts").get_json()
        xtts = next(
            service for service in catalogue["services"] if service["id"] == "xtts"
        )
        self.assertEqual("managed_local", xtts["connection_mode"])
        self.assertEqual("http://127.0.0.1:9234", xtts["api_base"])
        self.assertEqual("present", xtts["manager_component_state"])
        self.assertEqual("healthy", xtts["manager_service"]["health"]["state"])
        self.assertTrue(catalogue["manager"]["available"])

        persisted = self.client.get(
            "/api/v1/settings/services.tts"
        ).get_json()["value"]["provider_configs"][0]
        self.assertEqual(
            "http://external.example.test:8020",
            persisted["api_base"],
        )

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
