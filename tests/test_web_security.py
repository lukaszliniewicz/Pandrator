import tempfile
import unittest
import io
from pathlib import Path

from PIL import Image
from sqlalchemy import func, select

from pandrator.web.auth import BootstrapTokenStore

from pandrator.web.api import create_app
from pandrator.web.models import Artifact, SourceAsset
from tests.web_test_support import prepare_web_test_data_root


class WebSecurityBoundaryTests(unittest.TestCase):
    def test_source_lifecycle_is_revisioned_and_reference_aware(self):
        with tempfile.TemporaryDirectory() as directory:
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
            try:
                client = app.test_client()
                csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
                session_id = client.post(
                    "/api/v1/sessions",
                    json={"name": "Source lifecycle", "workflow_kind": "audiobook"},
                    headers={"X-CSRF-Token": csrf},
                ).get_json()["id"]
                uploaded = client.post(
                    "/api/v1/uploads",
                    data={"file": (io.BytesIO(b"source text"), "draft.txt")},
                    content_type="multipart/form-data",
                    headers={"X-CSRF-Token": csrf},
                ).get_json()
                asset = client.patch(
                    f"/api/v1/sources/{uploaded['source_asset_id']}",
                    json={"display_name": "Renamed source.txt"},
                    headers={"X-CSRF-Token": csrf, "If-Match": '"1"'},
                ).get_json()
                self.assertEqual(asset["display_name"], "Renamed source.txt")
                attachment = client.post(
                    f"/api/v1/sessions/{session_id}/sources",
                    json={"source_asset_id": asset["id"], "role": "primary"},
                    headers={"X-CSRF-Token": csrf},
                ).get_json()
                blocked = client.delete(
                    f"/api/v1/sources/{asset['id']}",
                    headers={"X-CSRF-Token": csrf, "If-Match": f'"{asset["revision"]}"'},
                )
                self.assertEqual(blocked.status_code, 409)
                self.assertEqual(blocked.get_json()["error"]["code"], "source_in_use")
                detached = client.delete(
                    f"/api/v1/sessions/{session_id}/sources/{attachment['id']}",
                    headers={"X-CSRF-Token": csrf, "If-Match": f'"{attachment["revision"]}"'},
                )
                self.assertEqual(detached.status_code, 204)
                trashed = client.delete(
                    f"/api/v1/sources/{asset['id']}",
                    headers={"X-CSRF-Token": csrf, "If-Match": f'"{asset["revision"]}"'},
                ).get_json()
                self.assertEqual(trashed["state"], "trashed")
                restored = client.post(
                    f"/api/v1/sources/{asset['id']}/restore",
                    headers={"X-CSRF-Token": csrf, "If-Match": f'"{trashed["revision"]}"'},
                ).get_json()
                self.assertEqual(restored["state"], "current")
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_cover_upload_is_validated_and_not_registered_as_a_reusable_source(self):
        with tempfile.TemporaryDirectory() as directory:
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
            try:
                client = app.test_client()
                csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
                session_id = client.post(
                    "/api/v1/sessions",
                    json={"name": "Cover test", "workflow_kind": "audiobook"},
                    headers={"X-CSRF-Token": csrf},
                ).get_json()["id"]
                cover = io.BytesIO()
                Image.new("RGB", (24, 24), color=(90, 50, 30)).save(cover, format="PNG")
                cover.seek(0)
                response = client.post(
                    "/api/v1/uploads",
                    data={"session_id": session_id, "purpose": "cover", "file": (cover, "cover.png")},
                    content_type="multipart/form-data",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 201)
                self.assertIsNone(response.get_json()["source_asset_id"])
                with app.extensions["pandrator"]["database"].session() as session:
                    artifact = session.get(Artifact, response.get_json()["artifact_id"])
                    self.assertEqual((artifact.kind, artifact.role), ("image", "cover"))
                    self.assertEqual(session.scalar(select(func.count()).select_from(SourceAsset)), 0)
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_security_headers_allow_only_same_origin_artifact_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True)
            try:
                response = app.test_client().get("/api/v1/health")
                self.assertEqual(response.headers["X-Frame-Options"], "SAMEORIGIN")
                self.assertIn("frame-ancestors 'self'", response.headers["Content-Security-Policy"])
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_untrusted_host_is_rejected_before_route_handling(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True, trusted_hosts=["trusted.example"])
            try:
                client = app.test_client()
                self.assertEqual(client.get("/api/v1/health", headers={"Host": "trusted.example"}).status_code, 200)
                self.assertEqual(client.get("/api/v1/health", headers={"Host": "evil.example"}).status_code, 400)
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_maintenance_mode_stops_new_mutations_but_keeps_health_available(self):
        with tempfile.TemporaryDirectory() as directory:
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
            try:
                client = app.test_client()
                csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
                Path(directory, "maintenance.json").write_text("{}", encoding="utf-8")
                response = client.post(
                    "/api/v1/sessions",
                    json={"name": "Blocked"},
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.get_json()["error"]["code"], "maintenance")
                self.assertEqual(client.get("/api/v1/health").status_code, 200)
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_uploaded_filename_cannot_escape_the_managed_upload_root(self):
        import io

        with tempfile.TemporaryDirectory() as directory:
            bootstrap = BootstrapTokenStore()
            token = bootstrap.issue()
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
            try:
                client = app.test_client()
                csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
                response = client.post(
                    "/api/v1/uploads",
                    data={"file": (io.BytesIO(b"safe"), "../../escaped.txt")},
                    content_type="multipart/form-data",
                    headers={"X-CSRF-Token": csrf},
                )
                self.assertEqual(response.status_code, 201)
                self.assertFalse(Path(directory).parent.joinpath("escaped.txt").exists())
                self.assertEqual(len(list(Path(directory, "uploads").glob("*-escaped.txt"))), 1)
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_secure_cookie_mode_marks_session_cookie_secure(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True, secure_cookies=True)
            try:
                self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
                self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
                self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_remote_login_failures_are_throttled_but_loopback_is_not(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True)
            try:
                client = app.test_client()
                throttle = app.extensions["pandrator"]["login_throttle"]
                throttle.max_attempts = 2
                throttle.initial_block_seconds = 30
                remote = {"REMOTE_ADDR": "192.0.2.10"}

                first = client.post(
                    "/api/v1/auth/login",
                    json={"password": "incorrect-password"},
                    environ_overrides=remote,
                )
                second = client.post(
                    "/api/v1/auth/login",
                    json={"password": "incorrect-password"},
                    environ_overrides=remote,
                )
                blocked = client.post(
                    "/api/v1/auth/login",
                    json={"password": "incorrect-password"},
                    environ_overrides=remote,
                )
                self.assertEqual(401, first.status_code)
                self.assertEqual(401, second.status_code)
                self.assertIn("Retry-After", second.headers)
                self.assertEqual(429, blocked.status_code)
                self.assertEqual(
                    "login_throttled",
                    blocked.get_json()["error"]["code"],
                )

                for _index in range(4):
                    local = client.post(
                        "/api/v1/auth/login",
                        json={"password": "incorrect-password"},
                        environ_overrides={"REMOTE_ADDR": "127.0.0.1"},
                    )
                    self.assertEqual(401, local.status_code)
                    self.assertNotIn("Retry-After", local.headers)
            finally:
                app.extensions["pandrator"]["database"].dispose()

    def test_auth_status_warns_when_remote_transport_is_not_secure(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            app = create_app(data_root=directory, testing=True)
            try:
                client = app.test_client()
                response = client.get(
                    "/api/v1/auth/status",
                    environ_overrides={"REMOTE_ADDR": "198.51.100.4"},
                )
                payload = response.get_json()
                self.assertTrue(payload["remote_access"])
                self.assertFalse(payload["secure_transport"])
                self.assertIn("plain HTTP", payload["security_warning"])

                local = client.get(
                    "/api/v1/auth/status",
                    environ_overrides={"REMOTE_ADDR": "::1"},
                ).get_json()
                self.assertFalse(local["remote_access"])
                self.assertEqual("", local["security_warning"])
            finally:
                app.extensions["pandrator"]["database"].dispose()


if __name__ == "__main__":
    unittest.main()
