import tempfile
import unittest
from pathlib import Path

from pandrator.web.auth import BootstrapTokenStore

from pandrator.web.api import create_app


class WebSecurityBoundaryTests(unittest.TestCase):
    def test_untrusted_host_is_rejected_before_route_handling(self):
        with tempfile.TemporaryDirectory() as directory:
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
            app = create_app(data_root=directory, testing=True, secure_cookies=True)
            try:
                self.assertTrue(app.config["SESSION_COOKIE_SECURE"])
                self.assertEqual(app.config["SESSION_COOKIE_SAMESITE"], "Lax")
                self.assertTrue(app.config["SESSION_COOKIE_HTTPONLY"])
            finally:
                app.extensions["pandrator"]["database"].dispose()


if __name__ == "__main__":
    unittest.main()
