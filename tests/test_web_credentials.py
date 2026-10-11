import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.credentials import (
    KEYRING_SERVICE_NAME,
    STT_SERVICE_ENVS,
    ResolvedCredential,
    database_reference,
    environment_reference,
    hydrate_stt_settings,
    hydrate_tts_settings,
    prepare_stt_settings_for_storage,
    provider_credential_key,
    resolve_provider_credential,
    resolve_provider_credentials,
    secret_file_reference,
    shared_provider_credential_key,
    stt_service_credential_key,
    upsert_credential,
)
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import StoredCredential
from tests.web_test_support import prepare_web_test_data_root


class _MemoryKeyring:
    def __init__(self):
        self.values: dict[tuple[str, str], str] = {}

    def set_password(self, service: str, username: str, value: str) -> None:
        self.values[(service, username)] = value

    def get_password(self, service: str, username: str) -> str | None:
        return self.values.get((service, username))

    def delete_password(self, service: str, username: str) -> None:
        del self.values[(service, username)]


class CredentialStorageTests(unittest.TestCase):
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
        response = self.client.post(
            "/api/v1/auth/bootstrap",
            json={"token": token},
        )
        self.headers = {"X-CSRF-Token": response.get_json()["csrf_token"]}
        self.database = self.app.extensions["pandrator"]["database"]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _create_provider(self, **overrides):
        payload = {
            "provider_key": "anthropic",
            "label": "Anthropic test",
            **overrides,
        }
        response = self.client.post(
            "/api/v1/providers",
            json=payload,
            headers=self.headers,
        )
        self.assertEqual(201, response.status_code, response.get_json())
        return response.get_json()

    def _patch_provider(self, provider: dict, payload: dict):
        return self.client.patch(
            f"/api/v1/providers/{provider['id']}",
            json=payload,
            headers={
                **self.headers,
                "If-Match": f'"{provider["revision"]}"',
            },
        )

    def test_backend_catalog_keeps_database_as_the_available_default(self):
        response = self.client.get("/api/v1/credential-backends")
        self.assertEqual(200, response.status_code)
        items = response.get_json()["items"]
        defaults = [item for item in items if item["default"]]
        self.assertEqual(["database"], [item["id"] for item in defaults])
        self.assertTrue(defaults[0]["available"])
        self.assertEqual(
            {"database", "keyring", "environment", "file"},
            {item["id"] for item in items},
        )

    def test_azure_mai_profiles_share_the_legacy_credential_key_and_environment(self):
        self.assertEqual(
            STT_SERVICE_ENVS["azure_mai_transcribe_2"], "AZURE_SPEECH_KEY"
        )
        self.assertEqual(
            STT_SERVICE_ENVS["azure_mai_transcribe_1_5"], "AZURE_SPEECH_KEY"
        )
        self.assertEqual(
            stt_service_credential_key("azure_mai_transcribe_2"),
            stt_service_credential_key("azure_mai_transcribe_1_5"),
        )

    def test_stt_shared_credential_survives_removing_one_azure_mai_profile(self):
        key = stt_service_credential_key("azure_mai_transcribe_1_5")
        reference = database_reference(key)
        previous = {
            "provider_configs": [
                {"id": "azure_mai_transcribe_2", "secret_ref": reference},
                {"id": "azure_mai_transcribe_1_5", "secret_ref": reference},
            ]
        }
        value = {
            "provider_configs": [
                {"id": "azure_mai_transcribe_1_5", "secret_ref": reference}
            ]
        }
        with self.database.session() as session:
            upsert_credential(session, key, "Azure Speech API key", "shared-secret")
            prepare_stt_settings_for_storage(
                session,
                self.database,
                self.app.extensions["pandrator"]["paths"],
                value,
                previous,
            )
        with self.database.session() as session:
            self.assertEqual(
                session.get(StoredCredential, key).secret_value, "shared-secret"
            )

    def test_stt_shared_credential_is_removed_when_both_azure_profiles_clear_it(self):
        key = stt_service_credential_key("azure_mai_transcribe_1_5")
        reference = database_reference(key)
        previous = {
            "provider_configs": [
                {"id": "azure_mai_transcribe_2", "secret_ref": reference},
                {"id": "azure_mai_transcribe_1_5", "secret_ref": reference},
            ]
        }
        value = {
            "provider_configs": [
                {"id": "azure_mai_transcribe_2", "clear_api_key": True},
                {"id": "azure_mai_transcribe_1_5", "clear_api_key": True},
            ]
        }
        with self.database.session() as session:
            upsert_credential(session, key, "Azure Speech API key", "shared-secret")
            prepare_stt_settings_for_storage(
                session,
                self.database,
                self.app.extensions["pandrator"]["paths"],
                value,
                previous,
            )
        with self.database.session() as session:
            self.assertIsNone(session.get(StoredCredential, key))

    def test_mai_2_hydration_reuses_legacy_credential_without_a_copied_reference(self):
        key = stt_service_credential_key("azure_mai_transcribe_1_5")
        with self.database.session() as session:
            upsert_credential(session, key, "Azure Speech API key", "shared-secret")
        hydrated = hydrate_stt_settings(
            self.database,
            self.app.extensions["pandrator"]["paths"],
            {
                "stt_engine": "azure_mai_transcribe_2",
                "provider_configs": [{"id": "azure_mai_transcribe_2"}],
            },
        )
        self.assertEqual(
            hydrated["provider_configs"][0]["api_key"], "shared-secret"
        )
        self.assertEqual(hydrated["provider_configs"][0]["api_key_env"], "")

    def test_vertex_file_reference_overrides_legacy_shared_database_credential(self):
        shared_key = shared_provider_credential_key("vertex_ai")
        stored_value = "stale-vertex-database-fixture"
        file_value = "current-vertex-file-fixture"
        secret_path = Path(self.temporary.name, "vertex-service-account.secret")
        secret_path.write_text(file_value + "\n", encoding="utf-8")
        if os.name != "nt":
            secret_path.chmod(0o600)

        with self.database.session() as session:
            upsert_credential(
                session, shared_key, "Vertex shared credential", stored_value
            )

        settings = {
            "service": "Vertex AI",
            "provider_configs": [
                {
                    "id": "vertex_ai",
                    "secret_ref": secret_file_reference(secret_path),
                }
            ],
        }
        hydrated = hydrate_tts_settings(
            self.database,
            self.app.extensions["pandrator"]["paths"],
            settings,
            manager_bridge=SimpleNamespace(configured=False),
        )
        from pandrator.logic import tts_handler

        runtime_service = tts_handler.get_service_config(hydrated, "vertex_ai")
        self.assertIsNotNone(runtime_service)
        self.assertEqual(file_value, runtime_service["api_key"])
        self.assertEqual("", runtime_service["api_key_env"])
        self.assertNotIn(file_value, json.dumps(settings))
        with self.database.session() as session:
            self.assertEqual(
                stored_value, session.get(StoredCredential, shared_key).secret_value
            )

    def test_explicit_environment_reference_overrides_shared_database_for_cloud_providers(self):
        providers = ("openai", "gemini", "vertex_ai")
        reference_variable = "PANDRATOR_TEST_CREDENTIAL_REFERENCE"
        environment_value = "selected-environment-fixture"
        stored_values = {
            provider: f"stale-{provider}-database-fixture" for provider in providers
        }
        with self.database.session() as session:
            for provider, stored_value in stored_values.items():
                upsert_credential(
                    session,
                    shared_provider_credential_key(provider),
                    f"{provider} shared credential",
                    stored_value,
                )

        with mock.patch.dict(os.environ, {reference_variable: environment_value}):
            for provider in providers:
                with self.subTest(provider=provider):
                    resolved = resolve_provider_credential(
                        self.database,
                        self.app.extensions["pandrator"]["paths"],
                        provider,
                        environment_reference(reference_variable),
                    )
                    self.assertEqual("environment", resolved.source)
                    self.assertEqual(environment_value, resolved.resolved_value())

        missing_variable = "PANDRATOR_TEST_MISSING_CREDENTIAL_REFERENCE"
        with mock.patch.dict(os.environ, {missing_variable: ""}):
            for provider in providers:
                with self.subTest(provider=provider, missing=True):
                    resolved = resolve_provider_credential(
                        self.database,
                        self.app.extensions["pandrator"]["paths"],
                        provider,
                        environment_reference(missing_variable),
                    )
                    self.assertEqual("environment", resolved.source)
                    self.assertFalse(resolved.configured)
                    self.assertEqual("", resolved.resolved_value())

    def test_explicit_missing_file_and_invalid_reference_do_not_fall_back_to_database(self):
        provider = "vertex_ai"
        shared_key = shared_provider_credential_key(provider)
        with self.database.session() as session:
            upsert_credential(
                session, shared_key, "Vertex shared credential", "stored-fixture"
            )

        missing_path = Path(self.temporary.name, "missing-vertex.secret")
        with self.assertRaises(FileNotFoundError):
            resolve_provider_credential(
                self.database,
                self.app.extensions["pandrator"]["paths"],
                provider,
                secret_file_reference(missing_path),
            )
        with self.assertRaises(ValueError):
            resolve_provider_credential(
                self.database,
                self.app.extensions["pandrator"]["paths"],
                provider,
                "invalid-explicit-reference",
            )

    def test_batched_resolution_preserves_explicit_reference_and_database_preference(self):
        providers = ("openai", "gemini", "vertex_ai")
        shared_values = {
            provider: f"shared-{provider}-fixture" for provider in providers
        }
        vertex_direct_value = "direct-vertex-database-fixture"
        vertex_direct_key = provider_credential_key("vertex_ai")
        with self.database.session() as session:
            for provider, secret_value in shared_values.items():
                upsert_credential(
                    session,
                    shared_provider_credential_key(provider),
                    f"{provider} shared credential",
                    secret_value,
                )
            upsert_credential(
                session,
                vertex_direct_key,
                "Vertex direct credential",
                vertex_direct_value,
            )

        variable = "PANDRATOR_TEST_BATCHED_CREDENTIAL_REFERENCE"
        environment_value = "batched-environment-fixture"
        with mock.patch.dict(os.environ, {variable: environment_value}):
            resolved = resolve_provider_credentials(
                self.database,
                self.app.extensions["pandrator"]["paths"],
                [
                    ("openai", environment_reference(variable), "", True),
                    ("gemini", None, "", True),
                    ("vertex_ai", database_reference(vertex_direct_key), "", True),
                ],
            )
            self.assertEqual("environment", resolved[0].source)
            self.assertEqual(environment_value, resolved[0].resolved_value())

        self.assertEqual("database", resolved[1].source)
        self.assertEqual(shared_values["gemini"], resolved[1].resolved_value())
        self.assertEqual("database", resolved[2].source)
        self.assertEqual(shared_values["vertex_ai"], resolved[2].resolved_value())

    def test_environment_move_is_verified_before_reference_and_old_value_change(self):
        old_secret = "database-only-secret"
        provider = self._create_provider(api_key=old_secret)
        credential_key = provider_credential_key(provider["id"])

        with mock.patch.dict(os.environ, {"PANDRATOR_TEST_PROVIDER_KEY": ""}):
            rejected = self._patch_provider(
                provider,
                {
                    "credential_backend": "environment",
                    "credential_reference": "PANDRATOR_TEST_PROVIDER_KEY",
                    "delete_previous_credential": True,
                },
            )
        self.assertEqual(422, rejected.status_code)
        current = next(
            item
            for item in self.client.get("/api/v1/providers").get_json()["items"]
            if item["id"] == provider["id"]
        )
        self.assertEqual("database", current["credential_backend"])
        self.assertEqual(provider["revision"], current["revision"])
        with self.database.session() as session:
            self.assertEqual(
                old_secret,
                session.get(StoredCredential, credential_key).secret_value,
            )

        new_secret = "environment-only-secret"
        with mock.patch.dict(
            os.environ,
            {"PANDRATOR_TEST_PROVIDER_KEY": new_secret},
        ):
            moved = self._patch_provider(
                provider,
                {
                    "credential_backend": "environment",
                    "credential_reference": "PANDRATOR_TEST_PROVIDER_KEY",
                    "delete_previous_credential": True,
                },
            )
            self.assertEqual(200, moved.status_code, moved.get_json())
            payload = moved.get_json()
            self.assertEqual("environment", payload["credential_backend"])
            self.assertEqual("environment", payload["credential_source"])
            self.assertFalse(payload["previous_credential_retained"])
            self.assertNotIn(new_secret, json.dumps(payload))
        with self.database.session() as session:
            self.assertIsNone(session.get(StoredCredential, credential_key))

    def test_absolute_secret_file_is_referenced_without_returning_contents(self):
        secret = "mounted-file-secret"
        path = Path(self.temporary.name, "provider.secret")
        path.write_text(secret + "\n", encoding="utf-8")
        if os.name != "nt":
            path.chmod(0o600)

        provider = self._create_provider(
            label="File-backed provider",
            credential_backend="file",
            credential_reference=str(path.resolve()),
        )
        self.assertEqual("file", provider["credential_backend"])
        self.assertEqual("file", provider["credential_source"])
        self.assertEqual(str(path.resolve()), provider["credential_reference"])
        self.assertNotIn(secret, json.dumps(provider))

        rejected = self.client.post(
            "/api/v1/providers",
            json={
                "provider_key": "mistral",
                "label": "Relative file",
                "credential_backend": "file",
                "credential_reference": "relative.secret",
            },
            headers=self.headers,
        )
        self.assertEqual(422, rejected.status_code)

    def test_keyring_value_is_verified_and_only_its_reference_is_persisted(self):
        keyring = _MemoryKeyring()
        backend = SimpleNamespace(priority=1)
        secret = "operating-system-secret"
        with mock.patch(
            "pandrator.web.credentials._load_keyring",
            return_value=(keyring, backend),
        ):
            provider = self._create_provider(
                label="Keyring provider",
                credential_backend="keyring",
                api_key=secret,
            )
            self.assertEqual("keyring", provider["credential_backend"])
            self.assertEqual("keyring", provider["credential_source"])
            self.assertNotIn(secret, json.dumps(provider))
            username = provider_credential_key(provider["id"])
            self.assertEqual(
                secret,
                keyring.get_password(KEYRING_SERVICE_NAME, username),
            )
            with self.database.session() as session:
                self.assertIsNone(session.get(StoredCredential, username))
            cleared = self._patch_provider(
                provider,
                {"clear_api_key": True},
            )
            self.assertEqual(200, cleared.status_code, cleared.get_json())
            self.assertFalse(cleared.get_json()["credential_configured"])
            self.assertIsNone(
                keyring.get_password(KEYRING_SERVICE_NAME, username)
            )

    def test_auxiliary_environment_reference_is_write_only_and_persistent(self):
        secret = "auxiliary-environment-secret"
        with mock.patch.dict(os.environ, {"PANDRATOR_JINA_KEY": secret}):
            response = self.client.put(
                "/api/v1/credentials/jina",
                json={
                    "credential_backend": "environment",
                    "credential_reference": "PANDRATOR_JINA_KEY",
                },
                headers=self.headers,
            )
            self.assertEqual(200, response.status_code, response.get_json())
            payload = response.get_json()
            self.assertEqual("environment", payload["credential_backend"])
            self.assertEqual("PANDRATOR_JINA_KEY", payload["credential_reference"])
            self.assertNotIn(secret, json.dumps(payload))
            listed = self.client.get("/api/v1/credentials").get_json()
            self.assertNotIn(secret, json.dumps(listed))
            jina = next(item for item in listed["items"] if item["id"] == "jina")
            self.assertEqual("PANDRATOR_JINA_KEY", jina["credential_reference"])

    def test_resolved_credential_repr_does_not_expose_its_value(self):
        secret = "repr-must-not-leak"
        self.assertNotIn(secret, repr(ResolvedCredential(value=secret)))

    def test_job_diagnostics_redact_known_values_and_secret_fields(self):
        secret = "durable-diagnostic-secret"
        with self.database.session() as session:
            upsert_credential(session, "test:diagnostics", "Diagnostic secret", secret)
        queue = JobQueue(self.database)

        success = queue.enqueue("success")
        claimed = queue.claim("worker-success")
        queue.heartbeat(
            success.id,
            "worker-success",
            lease_generation=claimed.lease_generation,
            progress=0.5,
            detail=f"Using {secret}",
        )
        queue.log(
            success.id,
            "INFO",
            f"Authorization: Bearer {secret}",
            worker_id="worker-success",
            lease_generation=claimed.lease_generation,
        )
        queue.complete(
            success.id,
            "worker-success",
            {
                "message": f"Completed with {secret}",
                "api_key": secret,
            },
            lease_generation=claimed.lease_generation,
        )

        def failing(_payload, _progress, _cancel):
            raise RuntimeError(f"Provider rejected {secret}")

        failed = queue.enqueue("failure")
        Worker(queue, "worker-failure", {"failure": failing}).run_once()
        persisted = {
            "success": queue.get(success.id).result_json,
            "success_events": [
                event.payload_json for event in queue.events_for(success.id)
            ],
            "failure": {
                "message": queue.get(failed.id).error_message,
                "events": [
                    event.payload_json for event in queue.events_for(failed.id)
                ],
            },
        }
        serialized = json.dumps(persisted)
        self.assertNotIn(secret, serialized)
        self.assertNotIn("api_key", serialized)
        self.assertIn("[REDACTED]", serialized)


if __name__ == "__main__":
    unittest.main()
