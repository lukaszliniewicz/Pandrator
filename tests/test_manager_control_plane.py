import hashlib
import json
import socket
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import psutil
import requests

from pandrator_manager.api import create_api
from pandrator_manager.api.app import RECOVERY_COOKIE, _browser_handoff_url
from pandrator_manager.application import create_application
from pandrator_manager.auth import RecoverySessionManager
from pandrator_manager.autostart import LinuxSystemdAutostart, WindowsAutostart
from pandrator_manager.client import ManagerClient
from pandrator_manager.daemon import ManagerAlreadyRunning, ManagerInstanceLock
from pandrator_manager.models import (
    DesiredComponentState,
    HealthProbeSpec,
    ManagedProcessSpec,
    ProcessIdentity,
    RestartPolicy,
)
from pandrator_manager.network import EndpointExposure
from pandrator_manager.supervisor import ProcessSupervisor
from pandrator_manager.tray import (
    TrayApplication,
    configure_tray_autostart,
    stop_tray_background,
    tray_available,
)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def fake_http_spec(
    directory: str,
    *,
    port: int,
    bad_marker: Path | None = None,
    restart: RestartPolicy | None = None,
):
    marker_expression = repr(str(bad_marker)) if bad_marker else "''"
    code = f"""
import json
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
bad_marker = Path({marker_expression}) if {bool(bad_marker)!r} else None
class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path != '/health':
            self.send_response(404); self.end_headers(); return
        service = 'wrong' if bad_marker is not None and bad_marker.exists() else 'fake-service'
        payload = json.dumps({{'status':'ok','service':service,'protocol_version':'v1'}}).encode()
        self.send_response(200)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, *_args):
        pass
HTTPServer(('127.0.0.1',{port}), Handler).serve_forever()
"""
    return ManagedProcessSpec(
        service_id="fake.service",
        component_id="fake",
        label="Fake service",
        executable=sys.executable,
        arguments=("-c", code),
        cwd=directory,
        ports=(port,),
        readiness=HealthProbeSpec(
            kind="http",
            url=f"http://127.0.0.1:{port}/health",
            expected_service="fake-service",
            expected_protocol="v1",
            timeout_seconds=1,
        ),
        startup_timeout_seconds=5,
        shutdown_timeout_seconds=3,
        restart=restart or RestartPolicy(maximum_restarts=1),
    )


class SupervisorIntegrationTests(unittest.TestCase):
    def test_owned_service_starts_reports_identity_and_stops(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-test",
                monitor_interval_seconds=0.1,
            )
            spec = fake_http_spec(directory, port=free_port())
            supervisor.register(spec)
            service = supervisor.start(spec.service_id)
            pid = service.process.pid
            try:
                self.assertTrue(psutil.pid_exists(pid))
                self.assertEqual(service.health.state.value, "healthy")
                self.assertEqual(service.process.manager_instance_id, "manager-test")
                persisted = application.store.list_services()[0]
                self.assertEqual(persisted.process.pid, pid)
            finally:
                supervisor.stop(spec.service_id)
            self.assertFalse(psutil.pid_exists(pid))

    def test_unowned_port_conflict_is_reported_and_listener_is_not_killed(self):
        with tempfile.TemporaryDirectory() as directory, socket.socket(
            socket.AF_INET, socket.SOCK_STREAM
        ) as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            application = create_application(directory)
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-test",
            )
            spec = fake_http_spec(directory, port=port)
            supervisor.register(spec)
            with self.assertRaisesRegex(RuntimeError, "unrecognized process"):
                supervisor.start(spec.service_id)
            self.assertEqual(listener.getsockname()[1], port)

    def test_health_probe_rejects_expected_json_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-test",
            )
            original = fake_http_spec(directory, port=free_port())
            spec = original.model_copy(
                update={
                    "readiness": original.readiness.model_copy(
                        update={"expected_json": {"ready": True}}
                    ),
                    "startup_timeout_seconds": 0.5,
                }
            )
            supervisor.register(spec)
            with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                supervisor.start(spec.service_id)
            self.assertIsNone(supervisor.snapshot()[0].process)

    def test_live_unhealthy_service_restarts_with_backoff_policy(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            bad_marker = Path(directory) / "unhealthy"
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-test",
            )
            spec = fake_http_spec(
                directory,
                port=free_port(),
                bad_marker=bad_marker,
                restart=RestartPolicy(
                    maximum_restarts=2,
                    base_backoff_seconds=0,
                    maximum_backoff_seconds=0,
                    health_failure_threshold=1,
                    stable_after_seconds=60,
                ),
            )
            supervisor.register(spec)
            original = supervisor.start(spec.service_id)
            original_pid = original.process.pid
            try:
                bad_marker.write_text("", encoding="utf-8")
                supervisor.monitor_once()
                self.assertFalse(psutil.pid_exists(original_pid))
                bad_marker.unlink()
                supervisor.monitor_once()
                restarted = supervisor.snapshot()[0]
                self.assertNotEqual(restarted.process.pid, original_pid)
                self.assertEqual(restarted.restart_count, 1)
            finally:
                supervisor.stop(spec.service_id)

    def test_stop_refuses_reused_or_tampered_process_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            supervisor = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-test",
            )
            spec = fake_http_spec(directory, port=free_port())
            supervisor.register(spec)
            supervisor.start(spec.service_id)
            runtime = supervisor._runtime[spec.service_id]
            original_identity = runtime.identity
            runtime.identity = ProcessIdentity(
                pid=original_identity.pid,
                create_time=1.0,
                executable=original_identity.executable,
                manager_instance_id=original_identity.manager_instance_id,
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "unverifiable PID"):
                    supervisor.stop(spec.service_id)
                self.assertTrue(psutil.pid_exists(original_identity.pid))
            finally:
                runtime.identity = original_identity
                supervisor._runtime[spec.service_id] = runtime
                supervisor.stop(spec.service_id)

    def test_new_manager_instance_adopts_a_surviving_owned_service(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            spec = fake_http_spec(directory, port=free_port())
            first = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-one",
            )
            first.register(spec)
            initial = first.start(spec.service_id)
            pid = initial.process.pid
            first.shutdown(stop_children=False)
            second = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-two",
            )
            try:
                second.register(spec)
                adopted = second.snapshot()[0]
                self.assertEqual(adopted.process.pid, pid)
                self.assertEqual(
                    adopted.process.manager_instance_id,
                    "manager-two",
                )
                self.assertTrue(psutil.pid_exists(pid))
            finally:
                second.stop(spec.service_id)
            self.assertFalse(psutil.pid_exists(pid))

    def test_new_manager_restores_a_persisted_desired_service_that_exited(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            spec = fake_http_spec(directory, port=free_port())
            first = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-one",
            )
            first.register(spec)
            original = first.start(spec.service_id)
            original_pid = original.process.pid
            first.stop(spec.service_id)
            desired = application.store.list_services()[0]
            desired.desired_running = True
            application.store.save_service(desired)

            second = ProcessSupervisor(
                application.context,
                application.store,
                manager_instance_id="manager-two",
            )
            second.register(spec)
            try:
                self.assertEqual(second.restore_desired(), {})
                restored = second.snapshot()[0]
                self.assertTrue(restored.desired_running)
                self.assertIsNotNone(restored.process)
                self.assertNotEqual(restored.process.pid, original_pid)
            finally:
                second.stop(spec.service_id)


class RecoverySessionManagerTests(unittest.TestCase):
    def test_transient_and_remembered_expiry_are_enforced_and_sliding(self):
        now = [1_000.0]
        sessions = RecoverySessionManager(
            token_ttl_seconds=10,
            session_ttl_seconds=60,
            remembered_idle_ttl_seconds=120,
            remembered_absolute_ttl_seconds=300,
            touch_interval_seconds=30,
            clock=lambda: now[0],
        )

        transient = sessions.exchange(
            sessions.mint_launch_token(),
            remember=False,
        )
        self.assertIsNotNone(transient)
        now[0] += 61
        self.assertFalse(sessions.validate(transient.session_id))

        now[0] = 2_000
        remembered = sessions.exchange(
            sessions.mint_launch_token(),
            remember=True,
        )
        self.assertIsNotNone(remembered)
        now[0] = 2_100
        touched = sessions.authenticate(remembered.session_id)
        self.assertIsNotNone(touched)
        self.assertEqual(2_220, touched.expires_at)
        now[0] = 2_210
        self.assertTrue(sessions.validate(remembered.session_id))
        now[0] = 2_301
        self.assertFalse(sessions.validate(remembered.session_id))


class ApiContractTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.application = create_application(self.temporary.name)
        self.application.instance_id = "api-test"
        self.supervisor = ProcessSupervisor(
            self.application.context,
            self.application.store,
            manager_instance_id="api-test",
        )
        self.secret = "s" * 43
        self.sessions = RecoverySessionManager()
        self.api = create_api(
            self.application,
            self.supervisor,
            client_secret=self.secret,
            recovery_sessions=self.sessions,
        )
        self.client = self.api.test_client()
        self.auth = {"Authorization": f"Bearer {self.secret}"}

    def tearDown(self):
        self.temporary.cleanup()

    def test_loopback_host_and_bearer_boundaries(self):
        self.assertEqual(self.client.get("/v1/status").status_code, 401)
        self.assertEqual(
            self.client.get("/v1/status", headers=self.auth).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/v1/status",
                headers={**self.auth, "Host": "attacker.example"},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.get(
                "/v1/status",
                headers=self.auth,
                environ_base={"REMOTE_ADDR": "10.0.0.20"},
            ).status_code,
            403,
        )

    def test_plans_and_operations_are_exact_and_idempotent(self):
        body = {
            "kind": "install",
            "desired": {"silero": DesiredComponentState().model_dump(mode="json")},
        }
        self.assertEqual(
            self.client.post("/v1/plans", headers=self.auth, json=body).status_code,
            400,
        )
        plan_headers = {**self.auth, "Idempotency-Key": "plan-key"}
        first = self.client.post("/v1/plans", headers=plan_headers, json=body)
        repeated = self.client.post("/v1/plans", headers=plan_headers, json=body)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.get_json()["id"], repeated.get_json()["id"])
        changed = self.client.post(
            "/v1/plans",
            headers=plan_headers,
            json={
                "kind": "install",
                "desired": {"xtts": DesiredComponentState().model_dump(mode="json")},
            },
        )
        self.assertEqual(changed.status_code, 409)

        plan = first.get_json()
        operation_body = {
            "plan_id": plan["id"],
            "plan_digest": plan["digest"],
            "accepted_confirmations": [],
        }
        operation_headers = {**self.auth, "Idempotency-Key": "operation-key"}
        operation = self.client.post(
            "/v1/operations",
            headers=operation_headers,
            json=operation_body,
        )
        operation_repeat = self.client.post(
            "/v1/operations",
            headers=operation_headers,
            json=operation_body,
        )
        self.assertEqual(operation.status_code, 202)
        self.assertEqual(
            operation.get_json()["id"],
            operation_repeat.get_json()["id"],
        )

    def test_recovery_token_is_single_use_and_cookie_auth_needs_csrf_for_writes(self):
        response = self.client.post(
            "/v1/recovery-sessions",
            headers={**self.auth, "Idempotency-Key": "recovery-key"},
            json={},
        )
        recovery_url = response.get_json()["url"]
        token = parse_qs(urlsplit(recovery_url).fragment)["token"][0]
        exchange = self.client.post(
            "/v1/recovery/exchange",
            json={"token": token},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(exchange.status_code, 200)
        exchanged_csrf = exchange.get_json()["csrf_token"]
        resumed = self.client.get("/v1/session")
        self.assertEqual(resumed.status_code, 200)
        self.assertEqual(exchanged_csrf, resumed.get_json()["csrf_token"])
        self.assertEqual(1, resumed.get_json()["active_session_count"])
        csrf = resumed.get_json()["csrf_token"]
        self.assertEqual(self.client.get("/v1/status").status_code, 200)
        self.assertEqual(
            self.client.post("/v1/plans", json={}).status_code,
            401,
        )
        self.assertNotEqual(
            self.client.post(
                "/v1/plans",
                json={},
                headers={
                    "X-CSRF-Token": csrf,
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            ).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/v1/recovery/exchange",
                json={"token": token},
                headers={"Origin": "http://localhost"},
            ).status_code,
            401,
        )

    def test_recovery_assets_are_external_and_served_with_strict_headers(self):
        response = self.client.get("/recovery")
        self.addCleanup(response.close)
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('src="/recovery/app.js"', html)
        self.assertNotIn("<script>", html)
        self.assertIn('role="tablist"', html)
        self.assertIn('id="tab-install"', html)
        self.assertIn('id="tab-maintenance"', html)
        self.assertIn('id="tab-activity"', html)
        self.assertIn('id="application-state"', html)
        self.assertIn('id="selection-bar"', html)
        self.assertIn('class="health-indicator"', html)
        self.assertNotIn("application-mark", html)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        policy = response.headers["Content-Security-Policy"]
        self.assertIn("script-src 'self'", policy)
        self.assertNotIn("'unsafe-inline'", policy)
        self.assertEqual(response.headers["X-Frame-Options"], "DENY")
        script = self.client.get("/recovery/app.js")
        self.addCleanup(script.close)
        self.assertEqual(script.status_code, 200)
        source = script.get_data(as_text=True)
        self.assertIn("Detected components", source)
        self.assertIn("Browser authorization expired", source)
        self.assertIn('fetch("/v1/session"', source)
        self.assertIn("Browser remembered", source)
        self.assertIn('typeof crypto.randomUUID === "function"', source)
        self.assertIn("crypto.getRandomValues", source)
        self.assertIn("buildComponentDetails", source)
        self.assertIn("if (nodes.detailsBuilt) return", source)
        self.assertIn('component.definition.id !== "pandrator"', source)
        self.assertIn('"voice_design"', source)
        self.assertIn('"emotion_steering"', source)
        self.assertNotIn('"gpu_required", "GPU required"', source)
        self.assertIn("sectionState", source)
        self.assertNotIn("makeCapabilityChips", source)
        stylesheet = self.client.get("/recovery/styles.css")
        self.addCleanup(stylesheet.close)
        self.assertEqual(stylesheet.status_code, 200)
        css = stylesheet.get_data(as_text=True)
        self.assertIn(".manager-tabs", css)
        self.assertIn(".selection-bar", css)
        self.assertIn(".engine-chevron", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", css)
        self.assertNotIn(".compact-chips", css)
        self.assertNotIn(".detail-box", css)

    def test_legacy_application_launch_falls_back_to_owner_login(self):
        legacy = mock.Mock(status_code=405)
        self.assertEqual(
            _browser_handoff_url(
                legacy,
                "http://192.168.1.164:8097",
            ),
            "http://192.168.1.164:8097/",
        )
        current = mock.Mock(status_code=200)
        current.json.return_value = {"token": "one-use-token"}
        self.assertEqual(
            _browser_handoff_url(
                current,
                "https://pandrator.example/",
            ),
            "https://pandrator.example/#bootstrap=one-use-token",
        )

    def test_event_retention_reports_when_a_snapshot_is_required(self):
        self.application.store.event_retention = 100
        for index in range(105):
            self.application.store.append_event(
                "test.event",
                {"index": index},
            )
        response = self.client.get(
            "/v1/events?after=1",
            headers=self.auth,
        )
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.get_json()["error"]["snapshot_required"])

    def test_unknown_routes_and_unexpected_errors_use_redacted_json(self):
        missing = self.client.get("/v1/not-a-route", headers=self.auth)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.content_type, "application/json")

        with mock.patch.object(
            self.application,
            "list_components",
            side_effect=RuntimeError("secret implementation detail"),
        ):
            failed = self.client.get("/v1/components", headers=self.auth)
        self.assertEqual(failed.status_code, 500)
        self.assertEqual(failed.get_json()["error"]["code"], "internal_error")
        self.assertNotIn("secret implementation detail", failed.get_data(as_text=True))

    def test_runtime_errors_are_typed_without_stopping_unknown_processes(self):
        response = self.client.post(
            "/v1/runtime/start",
            headers={**self.auth, "Idempotency-Key": "missing-service"},
            json={"service_ids": ["unknown.service"]},
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.get_json()["error"]["code"], "not_found")

    def test_legacy_import_requires_exact_review_and_is_idempotent(self):
        source = self.application.context.layout.root / "config.json"
        source.write_text(
            json.dumps({"silero_support": False}),
            encoding="utf-8",
        )
        inspection = self.client.get("/v1/legacy", headers=self.auth)
        self.assertEqual(inspection.status_code, 200)
        report = inspection.get_json()["report"]
        self.assertTrue(report["valid"])

        headers = {
            **self.auth,
            "Idempotency-Key": "legacy-import",
        }
        rejected = self.client.post(
            "/v1/legacy/import",
            headers={**self.auth, "Idempotency-Key": "legacy-unconfirmed"},
            json={
                "source_digest": report["source_digest"],
                "confirmed": False,
            },
        )
        self.assertEqual(rejected.status_code, 409)

        imported = self.client.post(
            "/v1/legacy/import",
            headers=headers,
            json={
                "source_digest": report["source_digest"],
                "confirmed": True,
            },
        )
        replay = self.client.post(
            "/v1/legacy/import",
            headers=headers,
            json={
                "source_digest": report["source_digest"],
                "confirmed": True,
            },
        )
        self.assertEqual(imported.status_code, 200)
        self.assertEqual(imported.get_json(), replay.get_json())


class DurableBrowserSessionTests(unittest.TestCase):
    secret = "d" * 43

    @staticmethod
    def _api(directory, *, secret=None, exposure=None):
        application = create_application(directory)
        application.instance_id = "durable-session-test"
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id="durable-session-test",
        )
        api = create_api(
            application,
            supervisor,
            client_secret=secret or DurableBrowserSessionTests.secret,
            manager_exposure=exposure,
        )
        return api, application

    @staticmethod
    def _authorize(api, *, key, secret=None, remember=True):
        client = api.test_client()
        bearer = secret or DurableBrowserSessionTests.secret
        issued = client.post(
            "/v1/recovery-sessions",
            headers={
                "Authorization": f"Bearer {bearer}",
                "Idempotency-Key": key,
            },
            json={},
        )
        token = parse_qs(
            urlsplit(issued.get_json()["url"]).fragment
        )["token"][0]
        exchanged = client.post(
            "/v1/recovery/exchange",
            json={"token": token, "remember": remember},
        )
        if exchanged.status_code != 200:
            raise AssertionError(exchanged.get_data(as_text=True))
        return client, exchanged.get_json()

    def test_remembered_cookie_and_reload_survive_manager_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            first_api, first_application = self._api(directory)
            first_client, exchanged = self._authorize(
                first_api,
                key="durable-first",
            )
            cookie = first_client.get_cookie(RECOVERY_COOKIE)
            self.assertIsNotNone(cookie)
            raw_cookie = cookie.value

            with closing(
                sqlite3.connect(first_application.context.layout.database)
            ) as database:
                row = database.execute(
                    "SELECT token_digest FROM browser_sessions"
                ).fetchone()
                columns = {
                    item[1]
                    for item in database.execute(
                        "PRAGMA table_info(browser_sessions)"
                    )
                }
            self.assertEqual(hashlib.sha256(raw_cookie.encode()).hexdigest(), row[0])
            self.assertNotEqual(raw_cookie, row[0])
            self.assertNotIn("csrf_token", columns)

            second_api, _second_application = self._api(directory)
            second_client = second_api.test_client()
            second_client.set_cookie(RECOVERY_COOKIE, raw_cookie)
            resumed = second_client.get("/v1/session")
            self.assertEqual(200, resumed.status_code)
            self.assertEqual(exchanged["csrf_token"], resumed.get_json()["csrf_token"])
            self.assertTrue(resumed.get_json()["session"]["remembered"])
            self.assertNotEqual(
                401,
                second_client.post(
                    "/v1/plans",
                    headers={
                        "X-CSRF-Token": resumed.get_json()["csrf_token"],
                        "Idempotency-Key": "durable-after-restart",
                    },
                    json={},
                ).status_code,
            )

    def test_sign_out_and_forget_all_are_revocable_per_browser(self):
        with tempfile.TemporaryDirectory() as directory:
            api, _application = self._api(directory)
            first, first_payload = self._authorize(api, key="browser-first")
            second, second_payload = self._authorize(api, key="browser-second")

            listed = first.get("/v1/browser-sessions")
            self.assertEqual(200, listed.status_code)
            self.assertEqual(2, len(listed.get_json()["items"]))
            self.assertEqual(
                1,
                sum(item["current"] for item in listed.get_json()["items"]),
            )

            signed_out = first.delete(
                "/v1/session",
                headers={"X-CSRF-Token": first_payload["csrf_token"]},
            )
            self.assertEqual(200, signed_out.status_code)
            self.assertIsNone(first.get_cookie(RECOVERY_COOKIE))
            self.assertEqual(401, first.get("/v1/status").status_code)
            self.assertEqual(200, second.get("/v1/status").status_code)

            forgotten = second.delete(
                "/v1/browser-sessions",
                headers={"X-CSRF-Token": second_payload["csrf_token"]},
            )
            self.assertEqual(200, forgotten.status_code)
            self.assertEqual(1, forgotten.get_json()["revoked"])
            self.assertIsNone(second.get_cookie(RECOVERY_COOKIE))
            self.assertEqual(401, second.get("/v1/status").status_code)

    def test_fresh_launch_link_rotates_an_existing_browser_session(self):
        with tempfile.TemporaryDirectory() as directory:
            api, _application = self._api(directory)
            client, _payload = self._authorize(api, key="rotation-first")
            previous_cookie = client.get_cookie(RECOVERY_COOKIE).value
            issued = client.post(
                "/v1/recovery-sessions",
                headers={
                    "Authorization": f"Bearer {self.secret}",
                    "Idempotency-Key": "rotation-second",
                },
                json={},
            )
            token = parse_qs(
                urlsplit(issued.get_json()["url"]).fragment
            )["token"][0]
            exchanged = client.post(
                "/v1/recovery/exchange",
                json={"token": token, "remember": True},
            )

            self.assertEqual(200, exchanged.status_code)
            self.assertEqual(1, exchanged.get_json()["active_session_count"])
            self.assertNotEqual(
                previous_cookie,
                client.get_cookie(RECOVERY_COOKIE).value,
            )
            stale_client = api.test_client()
            stale_client.set_cookie(RECOVERY_COOKIE, previous_cookie)
            self.assertEqual(401, stale_client.get("/v1/session").status_code)

    def test_secret_or_network_boundary_change_invalidates_authorization(self):
        with tempfile.TemporaryDirectory() as directory:
            first_api, _application = self._api(directory)
            first_client, _payload = self._authorize(
                first_api,
                key="boundary-first",
            )
            raw_cookie = first_client.get_cookie(RECOVERY_COOKIE).value

            changed_api, _application = self._api(
                directory,
                exposure=EndpointExposure(port=18098),
            )
            changed_client = changed_api.test_client()
            changed_client.set_cookie(RECOVERY_COOKIE, raw_cookie)
            self.assertEqual(401, changed_client.get("/v1/session").status_code)

            original_api, _application = self._api(directory)
            original_client, _payload = self._authorize(
                original_api,
                key="boundary-second",
            )
            raw_cookie = original_client.get_cookie(RECOVERY_COOKIE).value
            rotated_api, _application = self._api(
                directory,
                secret="r" * 43,
            )
            rotated_client = rotated_api.test_client()
            rotated_client.set_cookie(RECOVERY_COOKIE, raw_cookie)
            self.assertEqual(401, rotated_client.get("/v1/session").status_code)


class ManagerLockTests(unittest.TestCase):
    def test_lock_refuses_a_live_owner_and_replaces_reused_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manager.lock"
            first = ManagerInstanceLock(path)
            first.acquire()
            try:
                with self.assertRaises(ManagerAlreadyRunning):
                    ManagerInstanceLock(path).acquire()
            finally:
                first.release()

            path.write_text(
                json.dumps(
                    {
                        "pid": psutil.Process().pid,
                        "create_time": 1.0,
                        "executable": sys.executable,
                        "manager_instance_id": "stale",
                    }
                ),
                encoding="utf-8",
            )
            replacement = ManagerInstanceLock(path)
            replacement.acquire()
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(
                    payload["manager_instance_id"],
                    replacement.instance_id,
                )
            finally:
                replacement.release()


class DaemonClientIntegrationTests(unittest.TestCase):
    def test_client_bootstraps_and_stops_manager_without_tray(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            client = ManagerClient.ensure_running(directory, timeout_seconds=20)
            descriptor = client.layout.descriptor
            self.assertTrue(client.status()["ready"])
            self.assertFalse(client.capabilities()["tray_required"])
            self.assertTrue(descriptor.is_file())
            original_request = client.request

            with (
                mock.patch.object(
                    client,
                    "request",
                    side_effect=requests.ConnectionError(
                        "fixture connection failure"
                    ),
                ),
                mock.patch.object(
                    client,
                    "_wait_for_shutdown_confirmation",
                    return_value=False,
                ),
                self.assertRaises(requests.ConnectionError),
            ):
                client.stop_manager()

            def disconnect_after_shutdown(*args, **kwargs):
                original_request(*args, **kwargs)
                raise requests.ConnectionError(
                    "fixture dropped the shutdown response"
                )

            with mock.patch.object(
                client,
                "request",
                side_effect=disconnect_after_shutdown,
            ):
                client.stop_manager()
            deadline = time.monotonic() + 10
            while descriptor.exists() and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertFalse(descriptor.exists())
            log = client.layout.logs / "manager.log"
            content = log.read_text(encoding="utf-8")
            self.assertNotIn("Bad file descriptor", content)
            self.assertNotIn("Traceback", content)


class OptionalDesktopIntegrationTests(unittest.TestCase):
    def test_headless_linux_tray_check_does_not_import_x11_backend(self):
        with (
            mock.patch("pandrator_manager.tray.sys.platform", "linux"),
            mock.patch.dict(
                "pandrator_manager.tray.os.environ",
                {},
                clear=True,
            ),
        ):
            available, reason = tray_available()

        self.assertFalse(available)
        self.assertEqual(
            reason,
            "No graphical desktop session is available.",
        )

    def test_autostart_files_are_per_user_explicit_and_shell_payload_is_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(Path(directory) / "workspace")
            integrations = Path(directory) / "integrations"
            windows = WindowsAutostart(
                application.context.layout,
                startup_directory=integrations / "startup",
            )
            windows_status = windows.install()
            content = Path(windows_status.path).read_text(encoding="utf-8")
            self.assertIn("pandrator_manager.cli", content)
            self.assertIn("start-manager", content)
            self.assertIn(str(application.context.layout.workspace), content)
            self.assertNotIn("Users:", content)
            self.assertFalse(windows.remove().installed)

            linux = LinuxSystemdAutostart(
                application.context.layout,
                unit_directory=integrations / "systemd",
                systemctl="systemctl",
            )
            linux_status = linux.install(activate=False)
            unit = Path(linux_status.path).read_text(encoding="utf-8")
            self.assertIn("ExecStart=", unit)
            self.assertIn("pandrator_manager.cli", unit)
            self.assertIn("start-manager", unit)
            self.assertIn("Restart=on-failure", unit)
            self.assertNotIn("WantedBy=multi-user.target", unit)

    def test_linux_tray_autostart_targets_the_stable_launcher_command(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = create_application(Path(directory) / "workspace").context.layout
            config = Path(directory) / "config"
            with (
                mock.patch("pandrator_manager.tray.sys.platform", "linux"),
                mock.patch.dict(
                    "pandrator_manager.tray.os.environ",
                    {"XDG_CONFIG_HOME": str(config)},
                    clear=True,
                ),
                mock.patch(
                    "pandrator_manager.tray._tray_command",
                    return_value=(
                        "/opt/pandrator/PandratorManager",
                        "tray",
                        "--workspace",
                        str(layout.workspace),
                    ),
                ),
            ):
                path = configure_tray_autostart(layout, enabled=True)

            content = path.read_text(encoding="utf-8")
            self.assertIn(
                "Exec=/opt/pandrator/PandratorManager tray --workspace",
                content,
            )
            self.assertIn("X-KDE-autostart-after=panel", content)
            self.assertNotIn("-m pandrator_manager.tray", content)

    @unittest.skipUnless(
        sys.platform.startswith("win"),
        "Windows registry startup integration requires winreg.",
    )
    def test_windows_tray_autostart_uses_registry_without_a_batch_window(self):
        import winreg

        with tempfile.TemporaryDirectory() as directory:
            layout = create_application(Path(directory) / "workspace").context.layout
            legacy = Path(directory) / "PandratorTray.cmd"
            legacy.write_text("@echo off\n", encoding="utf-8")
            key = mock.MagicMock()
            opened = mock.MagicMock()
            opened.__enter__.return_value = key
            with (
                mock.patch("pandrator_manager.tray.sys.platform", "win32"),
                mock.patch(
                    "pandrator_manager.tray._tray_autostart_path",
                    return_value=legacy,
                ),
                mock.patch(
                    "pandrator_manager.tray._tray_command",
                    return_value=(
                        r"C:\Program Files\Pandrator\manager.exe",
                        "tray",
                        "--workspace",
                        str(layout.workspace),
                    ),
                ),
                mock.patch.object(
                    winreg,
                    "CreateKeyEx",
                    return_value=opened,
                ),
                mock.patch.object(winreg, "SetValueEx") as set_value,
            ):
                location = configure_tray_autostart(layout, enabled=True)

            command = set_value.call_args.args[4]
            self.assertIn(r'"C:\Program Files\Pandrator\manager.exe"', command)
            self.assertIn(" tray ", command)
            self.assertNotIn("cmd.exe", command.casefold())
            self.assertTrue(str(location).startswith("HKCU\\"))
            self.assertFalse(legacy.exists())

    def test_quitting_tray_only_stops_the_tray_icon(self):
        client = mock.Mock()
        tray = TrayApplication(client)
        icon = mock.Mock()
        tray.quit_tray(icon)
        icon.stop.assert_called_once_with()
        client.runtime.assert_not_called()
        client.stop_manager.assert_not_called()

    def test_stopping_tray_validates_identity_before_terminating_process(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = create_application(Path(directory) / "workspace").context.layout
            layout.state.mkdir(parents=True, exist_ok=True)
            identity = layout.state / "tray.pid"
            identity.write_text(
                json.dumps({"pid": 8123, "create_time": 45.5}),
                encoding="ascii",
            )
            process = mock.Mock()
            process.create_time.return_value = 45.5
            process.cmdline.return_value = [
                str(layout.bin / "pandrator-manager-launcher.exe"),
                "tray",
                "--workspace",
                str(layout.workspace),
            ]
            with (
                mock.patch(
                    "pandrator_manager.tray.psutil.Process",
                    return_value=process,
                ),
                mock.patch(
                    "pandrator_manager.tray.psutil.wait_procs",
                    return_value=([process], []),
                ),
            ):
                stopped, reason = stop_tray_background(layout)

            self.assertTrue(stopped)
            self.assertEqual("", reason)
            process.terminate.assert_called_once_with()
            self.assertFalse(identity.exists())


if __name__ == "__main__":
    unittest.main()
