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
    ResolvedCredential,
    provider_credential_key,
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
