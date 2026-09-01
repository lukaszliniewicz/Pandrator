import json
import tempfile
import unittest
from unittest import mock

from pandrator.logic.llm_handler import ModelDiscoveryResult
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import (
    hydrate_stt_settings,
    shared_provider_credential_key,
    stt_service_credential_key,
)
from pandrator.web.models import StoredCredential
from pandrator.web.provider_settings import build_llm_settings
from tests.web_test_support import prepare_web_test_data_root


class ProviderApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name, testing=True, bootstrap_tokens=bootstrap
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": self.csrf}

    def tearDown(self):
        self.app.extensions["pandrator"]["database"].dispose()
        self.temporary.cleanup()

    def test_zero_temperature_is_preserved_and_default_deletion_requires_replacement(
        self,
    ):
        provider = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "openai",
                "label": "OpenAI",
                "secret_ref": "env:OPENAI_API_KEY",
            },
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
        self.assertFalse(second["is_active"])
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
        records = self.client.get(
            f"/api/v1/providers/{provider['id']}/models"
        ).get_json()["items"]
        self.assertEqual(records, [{**second, "is_active": True, "is_default": True}])

    def test_refresh_merges_discovery_without_overwriting_manual_model(self):
        provider = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "openai",
                "label": "Local",
                "base_url": "http://127.0.0.1:1234/v1",
            },
            headers=self.headers,
        ).get_json()
        self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={
                "model_id": "manual",
                "is_default": True,
                "default_reasoning_effort": "custom-fast",
            },
            headers=self.headers,
        )
        with mock.patch(
            "pandrator.logic.llm_handler.discover_provider_models",
            return_value=ModelDiscoveryResult(
                models=("manual", "discovered"),
                source="endpoint",
                endpoint="http://127.0.0.1:1234/v1/models",
            ),
        ):
            result = self.client.post(
                f"/api/v1/providers/{provider['id']}/models/refresh",
                json={},
                headers=self.headers,
            )
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.get_json()["added"], ["discovered"])
        self.assertEqual(result.get_json()["source"], "endpoint")
        records = self.client.get(
            f"/api/v1/providers/{provider['id']}/models"
        ).get_json()["items"]
        manual = next(item for item in records if item["model_id"] == "manual")
        discovered = next(item for item in records if item["model_id"] == "discovered")
        self.assertEqual(manual["default_reasoning_effort"], "custom-fast")
        self.assertTrue(manual["is_active"])
        self.assertFalse(discovered["is_active"])

        activated = self.client.patch(
            f"/api/v1/providers/{provider['id']}/models/{discovered['id']}",
            json={"is_active": True},
            headers={**self.headers, "If-Match": f'"{discovered["revision"]}"'},
        )
        self.assertEqual(200, activated.status_code, activated.get_json())
        self.assertTrue(activated.get_json()["is_active"])
        blocked = self.client.patch(
            f"/api/v1/providers/{provider['id']}/models/{manual['id']}",
            json={"is_active": False},
            headers={**self.headers, "If-Match": f'"{manual["revision"]}"'},
        )
        self.assertEqual(422, blocked.status_code)

    def test_new_and_discovered_models_remain_inactive_until_selected(self):
        provider = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "openai",
                "label": "Opt in",
                "base_url": "http://127.0.0.1:1234/v1",
            },
            headers=self.headers,
        ).get_json()
        manual = self.client.post(
            f"/api/v1/providers/{provider['id']}/models",
            json={"model_id": "manual"},
            headers=self.headers,
        ).get_json()
        with mock.patch(
            "pandrator.logic.llm_handler.discover_provider_models",
            return_value=ModelDiscoveryResult(
                models=("manual", "discovered"),
                source="endpoint",
            ),
        ):
            self.client.post(
                f"/api/v1/providers/{provider['id']}/models/refresh",
                json={},
                headers=self.headers,
            )

        records = self.client.get(
            f"/api/v1/providers/{provider['id']}/models"
        ).get_json()["items"]
        self.assertFalse(manual["is_active"])
        self.assertTrue(
            all(not item["is_active"] and not item["is_default"] for item in records)
        )

    def test_provider_profiles_update_and_global_default_selection(self):
        profiles = self.client.get("/api/v1/providers/profiles").get_json()["items"]
        self.assertIn("lm-studio", {item["id"] for item in profiles})
        first_provider = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "openai",
                "label": "Local",
                "base_url": "http://127.0.0.1:1234/v1",
            },
            headers=self.headers,
        ).get_json()
        first_model = self.client.post(
            f"/api/v1/providers/{first_provider['id']}/models",
            json={"model_id": "local-model", "is_default": True},
            headers=self.headers,
        ).get_json()
        second_provider = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "anthropic",
                "label": "Cloud",
                "secret_ref": "env:ANTHROPIC_API_KEY",
            },
            headers=self.headers,
        ).get_json()
        self.client.post(
            f"/api/v1/providers/{second_provider['id']}/models",
            json={"model_id": "cloud-model", "is_default": True},
            headers=self.headers,
        ).get_json()
        refreshed_first = self.client.get(
            f"/api/v1/providers/{first_provider['id']}/models"
        ).get_json()["items"]
        self.assertFalse(
            next(item for item in refreshed_first if item["id"] == first_model["id"])[
                "is_default"
            ]
        )
        updated = self.client.patch(
            f"/api/v1/providers/{first_provider['id']}",
            json={
                "label": "LM Studio",
                "options": {
                    "profile_id": "lm-studio",
                    "request_options": {"organization": "studio"},
                },
            },
            headers={**self.headers, "If-Match": f'"{first_provider["revision"]}"'},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.get_json()["label"], "LM Studio")
        blocked = self.client.delete(
            f"/api/v1/providers/{second_provider['id']}", json={}, headers=self.headers
        )
        self.assertEqual(blocked.status_code, 409)
        removed = self.client.delete(
            f"/api/v1/providers/{second_provider['id']}",
            json={"replacement_model_record_id": first_model["id"]},
            headers=self.headers,
        )
        self.assertEqual(removed.status_code, 204)
        remaining = self.client.get(
            f"/api/v1/providers/{first_provider['id']}/models"
        ).get_json()["items"]
        self.assertTrue(remaining[0]["is_default"])

    def test_database_api_key_is_write_only_replaceable_and_removable(self):
        secret = "provider-secret-value"
        created_response = self.client.post(
            "/api/v1/providers",
            json={"provider_key": "openai", "label": "OpenAI", "api_key": secret},
            headers=self.headers,
        )
        self.assertEqual(201, created_response.status_code, created_response.get_json())
        created = created_response.get_json()
        self.assertTrue(created["credential_configured"])
        self.assertEqual("database", created["credential_source"])
        self.assertNotIn(secret, json.dumps(created))
        listed = self.client.get("/api/v1/providers").get_json()
        self.assertNotIn(secret, json.dumps(listed))

        database = self.app.extensions["pandrator"]["database"]
        with database.session() as session:
            stored = session.get(
                StoredCredential, shared_provider_credential_key("openai")
            )
            self.assertEqual(secret, stored.secret_value)

        rejected = self.client.patch(
            f"/api/v1/providers/{created['id']}",
            json={"options": {"request_options": {"azure_ad_token": "inline-secret"}}},
            headers={**self.headers, "If-Match": f'"{created["revision"]}"'},
        )
        self.assertEqual(422, rejected.status_code)
        rejected_header = self.client.patch(
            f"/api/v1/providers/{created['id']}",
            json={
                "options": {
                    "request_options": {
                        "headers": {"Authorization": "Bearer inline-secret"}
                    }
                }
            },
            headers={**self.headers, "If-Match": f'"{created["revision"]}"'},
        )
        self.assertEqual(422, rejected_header.status_code)
        removed = self.client.patch(
            f"/api/v1/providers/{created['id']}",
            json={"clear_api_key": True},
            headers={**self.headers, "If-Match": f'"{created["revision"]}"'},
        )
        self.assertEqual(200, removed.status_code, removed.get_json())
        self.assertFalse(removed.get_json()["credential_configured"])
        with database.session() as session:
            self.assertIsNone(
                session.get(StoredCredential, shared_provider_credential_key("openai"))
            )

    def test_vertex_service_account_json_is_validated_and_hydrated_for_litellm(self):
        credentials = json.dumps(
            {
                "type": "service_account",
                "project_id": "vertex-project",
                "client_email": "pandrator@vertex-project.iam.gserviceaccount.com",
                "private_key": "-----BEGIN PRIVATE KEY-----\nfixture\n-----END PRIVATE KEY-----\n",
                "token_uri": "https://oauth2.googleapis.com/token",
            }
        )
        rejected = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "vertex_ai",
                "label": "Vertex",
                "api_key": '{"type":"authorized_user"}',
            },
            headers=self.headers,
        )
        self.assertEqual(422, rejected.status_code)

        created = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "vertex_ai",
                "label": "Vertex",
                "api_key": credentials,
                "options": {
                    "request_options": {
                        "vertex_location": "global",
                        "max-output-tokens": 123,
                    }
                },
            },
            headers=self.headers,
        ).get_json()
        self.client.post(
            f"/api/v1/providers/{created['id']}/models",
            json={"model_id": "gemini-2.5-flash", "is_default": True},
            headers=self.headers,
        )

        extension = self.app.extensions["pandrator"]
        settings, model_name = build_llm_settings(
            extension["database"], extension["paths"]
        )
        vertex = settings.provider_configs[0]
        self.assertEqual(f"custom:{created['id']}/gemini-2.5-flash", model_name)
        self.assertEqual("", vertex["api_key"])
        self.assertEqual(credentials, vertex["request_options"]["vertex_credentials"])
        self.assertEqual("vertex-project", vertex["request_options"]["vertex_project"])
        self.assertEqual("global", vertex["request_options"]["vertex_location"])
        self.assertNotIn("max-output-tokens", vertex["request_options"])
        self.assertNotIn(credentials, json.dumps(created))

    def test_auxiliary_api_keys_share_write_only_storage(self):
        profiles = self.client.get("/api/v1/credentials").get_json()["items"]
        self.assertIn("jina", {item["id"] for item in profiles})
        secret = "jina-secret-value"
        saved = self.client.put(
            "/api/v1/credentials/jina",
            json={"api_key": secret},
            headers=self.headers,
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        self.assertEqual("database", saved.get_json()["credential_source"])
        self.assertNotIn(secret, json.dumps(saved.get_json()))
        self.assertNotIn(
            secret, json.dumps(self.client.get("/api/v1/credentials").get_json())
        )

        invalid_secret = "must-not-be-echoed-" + ("x" * 65536)
        rejected = self.client.put(
            "/api/v1/credentials/jina",
            json={"api_key": invalid_secret},
            headers=self.headers,
        )
        self.assertEqual(422, rejected.status_code)
        self.assertNotIn("must-not-be-echoed", rejected.get_data(as_text=True))

    def test_azure_mai_stt_profile_uses_write_only_connection_credentials(self):
        catalogue_response = self.client.get("/api/v1/services/stt")
        self.assertEqual(200, catalogue_response.status_code)
        catalogue = catalogue_response.get_json()
        profile = next(
            item
            for item in catalogue["profiles"]
            if item["id"] == "azure_mai_transcribe_1_5"
        )
        self.assertTrue(profile["word_timestamps"])
        self.assertFalse(profile["diarization"])

        secret = "azure-speech-secret-value"
        profile["api_base"] = "https://example.cognitiveservices.azure.com"
        profile["api_key"] = secret
        saved = self.client.put(
            "/api/v1/settings/services.stt",
            json={"value": {"provider_configs": [profile]}},
            headers={**self.headers, "If-Match": '"0"'},
        )
        self.assertEqual(200, saved.status_code, saved.get_json())
        self.assertNotIn(secret, saved.get_data(as_text=True))

        refreshed = self.client.get("/api/v1/services/stt").get_json()
        configured = refreshed["services"][0]
        self.assertTrue(configured["credential_configured"])
        self.assertNotIn(secret, json.dumps(refreshed))

        extension = self.app.extensions["pandrator"]
        hydrated = hydrate_stt_settings(
            extension["database"],
            extension["paths"],
            {
                "stt_engine": "azure_mai_transcribe_1_5",
                "provider_configs": refreshed["value"]["provider_configs"],
            },
        )
        self.assertEqual(
            secret,
            hydrated["provider_configs"][0]["api_key"],
        )
        with extension["database"].session() as session:
            stored = session.get(
                StoredCredential,
                stt_service_credential_key("azure_mai_transcribe_1_5"),
            )
            self.assertEqual(secret, stored.secret_value)

        stored_value = refreshed["value"]["provider_configs"][0]
        self.assertEqual("AZURE_SPEECH_KEY", stored_value["api_key_env"])

        rejected_endpoint = self.client.put(
            "/api/v1/settings/services.stt",
            json={
                "value": {
                    "provider_configs": [
                        {
                            **stored_value,
                            "api_base": "https://attacker.example",
                            "api_key_env": "PROCESS_SECRET",
                        }
                    ]
                }
            },
            headers={**self.headers, "If-Match": '"1"'},
        )
        self.assertEqual(422, rejected_endpoint.status_code)


if __name__ == "__main__":
    unittest.main()
