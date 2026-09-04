import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import psutil

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.manager_proxy import (
    LocalManagerProxy,
    ManagerProxyError,
)


class LocalManagerProxyTests(unittest.TestCase):
    def test_inventory_uses_aggregate_endpoint(self):
        proxy = LocalManagerProxy()
        payload = {
            "health": {"status": "ok"},
            "status": {"ready": True},
            "components": ("component",),
            "services": ("service",),
        }
        with mock.patch.object(
            proxy,
            "request_json",
            return_value=(payload, 200),
        ) as request_json:
            result = proxy.inventory()

        self.assertEqual(
            {
                "health": payload["health"],
                "status": payload["status"],
                "components": ["component"],
                "services": ["service"],
            },
            result,
        )
        request_json.assert_called_once_with(
            "GET",
            "/v1/inventory",
            timeout=10,
        )

    def test_inventory_falls_back_to_legacy_requests_on_not_found(self):
        proxy = LocalManagerProxy()
        with mock.patch.object(
            proxy,
            "request_json",
            side_effect=[
                ManagerProxyError(
                    "manager_request_failed",
                    "not found",
                    status=404,
                ),
                ({"status": "ok"}, 200),
                ({"ready": True}, 200),
                ({"items": ["component"]}, 200),
                ({"items": ["service"]}, 200),
            ],
        ) as request_json:
            result = proxy.inventory()

        self.assertEqual(
            {
                "health": {"status": "ok"},
                "status": {"ready": True},
                "components": ["component"],
                "services": ["service"],
            },
            result,
        )
        self.assertEqual(
            [
                mock.call("GET", "/v1/inventory", timeout=10),
                mock.call("GET", "/v1/health", timeout=3),
                mock.call("GET", "/v1/status", timeout=3),
                mock.call("GET", "/v1/components", timeout=10),
                mock.call("GET", "/v1/services", timeout=10),
            ],
            request_json.call_args_list,
        )

    def test_inventory_does_not_fall_back_for_other_errors(self):
        proxy = LocalManagerProxy()
        error = ManagerProxyError(
            "manager_unavailable",
            "manager unavailable",
            status=503,
        )
        with (
            mock.patch.object(
                proxy,
                "request_json",
                side_effect=error,
            ) as request_json,
            self.assertRaisesRegex(ManagerProxyError, "manager unavailable"),
        ):
            proxy.inventory()

        request_json.assert_called_once_with(
            "GET",
            "/v1/inventory",
            timeout=10,
        )

    def test_discovery_rejects_non_loopback_descriptor(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            descriptor = state / "connection.json"
            credential = state / "client.secret"
            credential.write_text("s" * 43, encoding="utf-8")
            process = psutil.Process()
            descriptor.write_text(
                json.dumps(
                    {
                        "base_url": "http://example.test:9000",
                        "instance_id": "fixture",
                        "pid": process.pid,
                        "process_create_time": process.create_time(),
                        "executable": process.exe(),
                    }
                ),
                encoding="utf-8",
            )
            proxy = LocalManagerProxy(
                descriptor_path=descriptor,
                credential_path=credential,
            )
            with self.assertRaisesRegex(
                ManagerProxyError,
                "safe loopback",
            ):
                proxy.discover()

    def test_discovery_rejects_credential_outside_manager_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            state = root / "state"
            state.mkdir()
            descriptor = state / "connection.json"
            credential = root / "other.secret"
            credential.write_text("s" * 43, encoding="utf-8")
            process = psutil.Process()
            descriptor.write_text(
                json.dumps(
                    {
                        "base_url": "http://127.0.0.1:9000",
                        "instance_id": "fixture",
                        "pid": process.pid,
                        "process_create_time": process.create_time(),
                        "executable": process.exe(),
                    }
                ),
                encoding="utf-8",
            )
            proxy = LocalManagerProxy(
                descriptor_path=descriptor,
                credential_path=credential,
            )
            with self.assertRaisesRegex(
                ManagerProxyError,
                "outside manager state",
            ):
                proxy.discover()


class ManagerProxyRouteTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        bootstrap = BootstrapTokenStore()
        token = bootstrap.issue()
        self.environment = mock.patch.dict(
            os.environ,
            {"PANDRATOR_MANAGER_DESCRIPTOR": str(Path(self.temporary.name) / "missing.json")},
        )
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=bootstrap,
        )
        self.addCleanup(
            self.app.extensions["pandrator"]["database"].dispose
        )
        self.client = self.app.test_client()
        self.csrf = self.client.post(
            "/api/v1/auth/bootstrap",
            json={"token": token},
        ).get_json()["csrf_token"]

    def test_unavailable_manager_is_a_nonfatal_status(self):
        response = self.client.get("/api/v1/manager/status")
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["available"])
        self.assertTrue(response.get_json()["configured"])

    def test_remote_browser_cannot_mutate_host_by_default(self):
        with mock.patch.object(
            LocalManagerProxy,
            "request_json",
            return_value=({"id": "not-called"}, 201),
        ) as request_json:
            response = self.client.post(
                "/api/v1/manager/plans",
                json={"kind": "install", "desired": {}},
                headers={"X-CSRF-Token": self.csrf},
                environ_base={"REMOTE_ADDR": "10.2.3.4"},
            )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "local_manager_access_required",
        )
        request_json.assert_not_called()

    def test_loopback_browser_forwards_only_typed_manager_resource(self):
        manager_plan = {"id": "plan-one", "digest": "digest"}
        with mock.patch.object(
            LocalManagerProxy,
            "request_json",
            return_value=(manager_plan, 201),
        ) as request_json:
            response = self.client.post(
                "/api/v1/manager/plans",
                json={"kind": "install", "desired": {}},
                headers={
                    "X-CSRF-Token": self.csrf,
                    "Idempotency-Key": "browser-key",
                },
            )
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.get_json(), manager_plan)
        request_json.assert_called_once_with(
            "POST",
            "/v1/plans",
            body={"kind": "install", "desired": {}},
            idempotency_key="browser-key",
            timeout=30,
        )

    def test_signed_release_plan_is_typed_and_forwarded_without_keys(self):
        signed_manifest = {
            "signed": {
                "schema_version": 1,
                "product": "pandrator",
            },
            "signatures": [{"key_id": "release", "signature": "value"}],
        }
        with mock.patch.object(
            LocalManagerProxy,
            "request_json",
            return_value=({"id": "release-plan"}, 201),
        ) as request_json:
            response = self.client.post(
                "/api/v1/manager/releases/plans",
                json={
                    "manifest": signed_manifest,
                    "expected_revision": 7,
                    "offline": True,
                    "start_after_activation": False,
                },
                headers={
                    "X-CSRF-Token": self.csrf,
                    "Idempotency-Key": "signed-release-plan",
                },
            )
        self.assertEqual(response.status_code, 201)
        request_json.assert_called_once_with(
            "POST",
            "/v1/releases/plans",
            body={
                "manifest": signed_manifest,
                "expected_revision": 7,
                "offline": True,
                "start_after_activation": False,
            },
            idempotency_key="signed-release-plan",
            timeout=30,
        )

    def test_release_plan_rejects_ad_hoc_trust_inputs(self):
        response = self.client.post(
            "/api/v1/manager/releases/plans",
            json={
                "manifest": {"signed": {}, "signatures": []},
                "public_key": "caller-controlled",
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(
            response.get_json()["error"]["code"],
            "validation_error",
        )

    def test_uninstall_plan_is_typed_and_local_only(self):
        with mock.patch.object(
            LocalManagerProxy,
            "request_json",
            return_value=({"id": "uninstall-plan"}, 201),
        ) as request_json:
            response = self.client.post(
                "/api/v1/manager/uninstall/plans",
                json={
                    "expected_revision": 4,
                    "purge_data": False,
                    "export_data": "C:/Backups/Pandrator-data.zip",
                },
                headers={
                    "X-CSRF-Token": self.csrf,
                    "Idempotency-Key": "uninstall-plan",
                },
            )
        self.assertEqual(response.status_code, 201)
        request_json.assert_called_once_with(
            "POST",
            "/v1/uninstall/plans",
            body={
                "expected_revision": 4,
                "purge_data": False,
                "export_data": "C:/Backups/Pandrator-data.zip",
            },
            idempotency_key="uninstall-plan",
            timeout=30,
        )

        remote = self.client.post(
            "/api/v1/manager/uninstall/plans",
            json={"purge_data": True},
            headers={"X-CSRF-Token": self.csrf},
            environ_base={"REMOTE_ADDR": "10.2.3.4"},
        )
        self.assertEqual(remote.status_code, 403)
        self.assertEqual(
            remote.get_json()["error"]["code"],
            "local_manager_access_required",
        )

    def test_legacy_import_forwards_only_reviewed_digest_and_confirmation(self):
        digest = "a" * 64
        with mock.patch.object(
            LocalManagerProxy,
            "request_json",
            return_value=({"status": "imported"}, 200),
        ) as request_json:
            response = self.client.post(
                "/api/v1/manager/legacy/import",
                json={
                    "source_digest": digest,
                    "confirmed": True,
                },
                headers={
                    "X-CSRF-Token": self.csrf,
                    "Idempotency-Key": "legacy-import",
                },
            )
        self.assertEqual(response.status_code, 200)
        request_json.assert_called_once_with(
            "POST",
            "/v1/legacy/import",
            body={
                "source_digest": digest,
                "confirmed": True,
            },
            idempotency_key="legacy-import",
            timeout=120,
        )

        invalid = self.client.post(
            "/api/v1/manager/legacy/import",
            json={
                "source_digest": "../not-a-digest",
                "confirmed": True,
            },
            headers={"X-CSRF-Token": self.csrf},
        )
        self.assertEqual(invalid.status_code, 422)

    def test_remove_plan_reports_managed_provider_binding_impact(self):
        configured = self.client.put(
            "/api/v1/settings/services.tts",
            json={
                "value": {
                    "service": "XTTS",
                    "provider_configs": [
                        {
                            "id": "xtts",
                            "name": "XTTS",
                            "connection_mode": "managed_local",
                            "managed_service_id": "tts.xtts",
                            "api_base": "http://external.example.test:8020",
                        }
                    ],
                }
            },
            headers={
                "X-CSRF-Token": self.csrf,
                "If-Match": '"0"',
            },
        )
        self.assertEqual(configured.status_code, 200, configured.get_json())
        with mock.patch.object(
            LocalManagerProxy,
            "request_json",
            return_value=({"id": "remove-plan", "digest": "d" * 64}, 201),
        ):
            response = self.client.post(
                "/api/v1/manager/plans",
                json={
                    "kind": "remove",
                    "desired": {"xtts": {"present": False}},
                },
                headers={
                    "X-CSRF-Token": self.csrf,
                    "Idempotency-Key": "remove-plan-key",
                },
            )
        self.assertEqual(response.status_code, 201, response.get_json())
        impacts = response.get_json()["application_impacts"][
            "managed_provider_bindings"
        ]
        self.assertEqual(impacts[0]["service_id"], "tts.xtts")
        self.assertTrue(impacts[0]["selected_default"])


if __name__ == "__main__":
    unittest.main()
