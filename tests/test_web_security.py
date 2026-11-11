import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from flask import Flask, session
from PIL import Image
from sqlalchemy import func, select

from pandrator.web.accounts.execution_actor import (
    UserActor,
    actor_from_principal,
    current_execution_actor,
)
from pandrator.web.api import create_app
from pandrator.web.auth import PRINCIPAL_KINDS, BootstrapTokenStore
from pandrator.web.http_lifecycle import ApiGuards
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
                for filename, content in (
                    ("broken.webp", b"not a readable image"),
                    ("truncated.png", cover.getvalue()[:16]),
                ):
                    with self.subTest(filename=filename):
                        rejected = client.post(
                            "/api/v1/uploads",
                            data={
                                "session_id": session_id,
                                "purpose": "cover",
                                "file": (io.BytesIO(content), filename),
                            },
                            content_type="multipart/form-data",
                            headers={"X-CSRF-Token": csrf},
                        )
                        self.assertEqual(rejected.status_code, 422)
                        self.assertEqual(
                            rejected.get_json()["error"]["code"], "invalid_cover"
                        )
                        with app.extensions["pandrator"]["database"].session() as session:
                            self.assertEqual(
                                session.scalar(select(func.count()).select_from(Artifact)), 0
                            )
                            self.assertEqual(
                                session.scalar(select(func.count()).select_from(SourceAsset)), 0
                            )
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

    def test_generic_local_cookie_does_not_replace_owner_session(self):
        with tempfile.TemporaryDirectory() as directory:
            prepare_web_test_data_root(directory)
            bootstrap = BootstrapTokenStore()
            app = create_app(data_root=directory, testing=True, bootstrap_tokens=bootstrap)
            try:
                client = app.test_client()
                client.set_cookie("session", "another-local-app", secure=True)
                response = client.post(
                    "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()},
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.headers["Set-Cookie"].startswith("pandrator_session="))
                self.assertEqual(client.get_cookie("session").value, "another-local-app")
                client.set_cookie("session", "another-app-refreshed", secure=True)
                self.assertTrue(client.get("/api/v1/auth/status").get_json()["authenticated"])
                csrf = response.get_json()["csrf_token"]
                logout = client.post("/api/v1/auth/logout", headers={"X-CSRF-Token": csrf})
                self.assertEqual(logout.status_code, 200)
                self.assertFalse(client.get("/api/v1/auth/status").get_json()["authenticated"])
                self.assertEqual(client.get_cookie("session").value, "another-app-refreshed")
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


class PrincipalContractTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.secret_key = "principal-contract-fixture"
        self.session_store = SimpleNamespace(resolve=Mock(return_value=None))
        self.auth = SimpleNamespace(
            browser_sessions=self.session_store, solo_access_allowed=Mock(return_value=True)
        )
        self.guards = ApiGuards(
            self.app,
            SimpleNamespace(
                identity=SimpleNamespace(instance_id="fixture-instance"),
                auth=self.auth,
                # Only consulted by dispatched requests; the contract tests
                # below push bare request contexts and never dispatch one.
                paths=SimpleNamespace(root=Path(tempfile.gettempdir())),
            ),
            testing=True,
            script_policy="'self'",
        )
        # Teardown restores the execution-actor ContextVar, so the fixture must
        # install the real lifecycle hooks instead of hand-rolling the reset.
        self.guards.register()

    def tearDown(self):
        self.assertIsNone(
            current_execution_actor.get(),
            "The execution actor leaked out of a request context.",
        )

    @staticmethod
    def _resolved_session(
        kind: str,
        *,
        user_id: str | None = "server-user-id",
        auth_revision: int | None = 7,
        session_id: str = "server-session-id",
    ) -> SimpleNamespace:
        return SimpleNamespace(
            subject="server-actor",
            kind=kind,
            scopes=frozenset({"app.read"}),
            client_id=None,
            user_id=user_id,
            auth_revision=auth_revision,
            session_id=session_id,
        )

    def test_network_zone_handles_ipv4_ipv6_and_invalid_addresses(self):
        for address, expected in (
            ("127.0.0.1", "loopback"), ("::1", "loopback"),
            ("192.168.1.2", "private"), ("fd00::1", "private"),
            ("8.8.8.8", "public"), ("2001:4860:4860::8888", "public"),
            ("invalid", "public"), ("", "public"),
        ):
            with self.subTest(address=address), self.app.test_request_context(
                environ_overrides={"REMOTE_ADDR": address}
            ):
                self.assertEqual(expected, self.guards._network_zone())

    def test_session_principal_kind_scope_and_request_cache_contract(self):
        # The opaque browser-session record is authoritative. Cookie-stored
        # kind/scope claims are client-controlled, so they must never be read.
        for kind in sorted(PRINCIPAL_KINDS - {"project_link"}):
            with self.subTest(kind=kind), self.app.test_request_context():
                self.session_store.resolve.reset_mock()
                self.auth.solo_access_allowed.reset_mock()
                self.session_store.resolve.return_value = self._resolved_session(kind)
                session.update(
                    authenticated=True, auth_session="synthetic-opaque-session",
                    principal_kind="unknown-kind", principal_scopes=["invalid-scope"],
                )
                principal = self.guards.principal()
                self.assertIsNotNone(principal)
                self.assertEqual(kind, principal.kind)
                self.assertEqual(frozenset({"app.read"}), principal.scopes)
                self.assertEqual("server-actor", principal.subject)
                self.assertEqual("server-user-id", principal.user_id)
                self.assertEqual(7, principal.auth_revision)
                self.assertEqual("server-session-id", principal.browser_session_id)
                self.assertIsNone(principal.token_id)
                self.assertEqual("fixture-instance", principal.target_instance_id)
                self.assertEqual(
                    actor_from_principal(principal), current_execution_actor.get()
                )
                self.session_store.resolve.assert_called_once_with(
                    "synthetic-opaque-session"
                )
                self.auth.solo_access_allowed.assert_called_once_with("server-user-id", 7)
                # The request cache outlives later cookie mutation and reuses
                # the single server resolution.
                session.update(
                    authenticated=False, principal_kind="service",
                    principal_scopes=["app.admin"],
                )
                self.assertIs(principal, self.guards.principal())
                self.assertEqual(
                    actor_from_principal(principal), current_execution_actor.get()
                )
                self.session_store.resolve.assert_called_once_with(
                    "synthetic-opaque-session"
                )

    def test_session_principal_rejects_project_link_kind_from_login_cookie(self):
        # Shared-link guests authenticate through their own cookie path; a
        # resolved record with that kind must never ride the login session.
        with self.app.test_request_context():
            self.session_store.resolve.return_value = self._resolved_session(
                "project_link"
            )
            session.update(
                authenticated=True, auth_session="link-kind-session",
                principal_kind="owner_session", principal_scopes=["app.read"],
            )
            self.assertIsNone(self.guards.principal())
            self.assertIsNone(self.guards.principal())
            self.session_store.resolve.assert_called_once_with("link-kind-session")
            self.assertIsNone(current_execution_actor.get())

    def test_session_cookie_claims_cannot_synthesize_a_principal(self):
        with self.app.test_request_context():
            self.session_store.resolve.reset_mock()
            self.session_store.resolve.return_value = self._resolved_session(
                "owner_session"
            )
            session.update(
                authenticated=True, principal_kind="service",
                principal_scopes=["app.admin"],
            )
            self.assertIsNone(self.guards.principal())
            self.session_store.resolve.assert_not_called()
            self.assertIsNone(current_execution_actor.get())
        with self.app.test_request_context():
            self.session_store.resolve.reset_mock()
            self.session_store.resolve.return_value = None
            session.update(
                authenticated=True, auth_session="revoked-opaque-session",
                principal_scopes=["invalid-scope"],
            )
            self.assertIsNone(self.guards.principal())
            self.assertIsNone(self.guards.principal())
            self.session_store.resolve.assert_called_once_with(
                "revoked-opaque-session"
            )
        with self.app.test_request_context():
            self.session_store.resolve.reset_mock()
            session.update(authenticated=True, auth_session=42)
            self.assertIsNone(self.guards.principal())
            self.session_store.resolve.assert_not_called()
        with self.app.test_request_context():
            self.session_store.resolve.reset_mock()
            session.update(authenticated=False, auth_session="opaque-session")
            self.assertIsNone(self.guards.principal())
            self.session_store.resolve.assert_not_called()
        with self.app.test_request_context():
            self.session_store.resolve.return_value = self._resolved_session(
                "owner_session"
            )
            self.auth.solo_access_allowed.return_value = False
            session.update(authenticated=True, auth_session="revoked-account")
            self.assertIsNone(self.guards.principal())
            self.auth.solo_access_allowed.assert_called_once_with("server-user-id", 7)
            self.assertIsNone(current_execution_actor.get())
        with self.app.test_request_context():
            # An internally inconsistent record must fail closed, not bind
            # a partially identified actor.
            self.session_store.resolve.return_value = self._resolved_session(
                "owner_session", user_id=None
            )
            session.update(authenticated=True, auth_session="inconsistent-record")
            self.assertIsNone(self.guards.principal())
            self.assertIsNone(current_execution_actor.get())

    def test_session_principal_restores_execution_actor_context(self):
        prior = UserActor(
            user_id="prior-user",
            auth_revision=3,
            subject="prior-actor",
            principal_kind="owner_session",
            scopes=frozenset({"app.read"}),
            token_id=None,
            browser_session_id="prior-session",
            client_id=None,
        )
        token = current_execution_actor.set(prior)
        try:
            with self.app.test_request_context():
                self.session_store.resolve.return_value = self._resolved_session(
                    "owner_session"
                )
                session.update(authenticated=True, auth_session="outer-session")
                outer = self.guards.principal()
                self.assertEqual(
                    actor_from_principal(outer), current_execution_actor.get()
                )
                # A second app gets its own app context and ``g``, so this
                # nests a real second principal binding over the first.
                inner_app = Flask("principal-contract-inner")
                inner_app.secret_key = "principal-contract-inner"
                inner_store = SimpleNamespace(
                    resolve=Mock(
                        return_value=self._resolved_session(
                            "owner_session",
                            user_id="inner-user",
                            auth_revision=9,
                            session_id="inner-session",
                        )
                    )
                )
                inner_guards = ApiGuards(
                    inner_app,
                    SimpleNamespace(
                        identity=SimpleNamespace(instance_id="fixture-instance"),
                        auth=SimpleNamespace(
                            browser_sessions=inner_store,
                            solo_access_allowed=Mock(return_value=True),
                        ),
                        paths=SimpleNamespace(root=Path(tempfile.gettempdir())),
                    ),
                    testing=True,
                    script_policy="'self'",
                )
                inner_guards.register()
                with inner_app.test_request_context():
                    session.update(authenticated=True, auth_session="inner-session")
                    inner = inner_guards.principal()
                    self.assertEqual(
                        actor_from_principal(inner), current_execution_actor.get()
                    )
                    self.assertNotEqual(
                        actor_from_principal(outer), current_execution_actor.get()
                    )
                self.assertEqual(
                    actor_from_principal(outer), current_execution_actor.get()
                )
                self.assertIs(outer, self.guards.principal())
            self.assertIs(prior, current_execution_actor.get())
        finally:
            current_execution_actor.reset(token)


if __name__ == "__main__":
    unittest.main()
