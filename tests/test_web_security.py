import tempfile
import unittest

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
