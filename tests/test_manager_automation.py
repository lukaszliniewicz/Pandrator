import base64
import hashlib
import re
import tempfile
import unittest
import uuid
from urllib.parse import parse_qs, urlsplit

from pandrator_manager.api import create_api
from pandrator_manager.api.app import RECOVERY_COOKIE
from pandrator_manager.application import create_application
from pandrator_manager.auth import (
    ManagerAutomationRateLimiter,
    RecoverySessionManager,
)
from pandrator_manager.network import AccessMode, EndpointExposure
from pandrator_manager.supervisor import ProcessSupervisor


class ManagerAutomationApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.application = create_application(self.temporary.name)
        self.application.instance_id = str(uuid.uuid4())
        self.supervisor = ProcessSupervisor(
            self.application.context,
            self.application.store,
            manager_instance_id=self.application.instance_id,
        )
        self.sessions = RecoverySessionManager(
            store=self.application.store,
            security_context="automation-test",
        )
        self.permanent_secret = "p" * 43
        self.exposure = EndpointExposure(
            mode=AccessMode.HTTPS_PROXY,
            bind_host="127.0.0.1",
            port=8098,
            public_url="https://setup.example",
            proxy_hops=1,
        )
        self.api = create_api(
            self.application,
            self.supervisor,
            client_secret=self.permanent_secret,
            recovery_sessions=self.sessions,
            manager_exposure=self.exposure,
        )
        self.client = self.api.test_client()
        self.forwarded = {
            "Host": "setup.example",
            "X-Forwarded-For": "198.51.100.44",
            "X-Forwarded-Proto": "https",
        }
        launched = self.sessions.exchange(
            self.sessions.mint_launch_token(),
            remember=False,
            user_agent="automation test",
        )
        self.assertIsNotNone(launched)
        self.browser_session = launched
        self.client.set_cookie(
            RECOVERY_COOKIE,
            launched.session_id,
            domain="setup.example",
            secure=True,
        )

    def tearDown(self):
        self.supervisor.shutdown(stop_children=True)
        self.temporary.cleanup()

    @staticmethod
    def _pkce():
        verifier = base64.urlsafe_b64encode(
            hashlib.sha256(uuid.uuid4().bytes).digest()
            + hashlib.sha256(uuid.uuid4().bytes).digest()
        ).rstrip(b"=").decode("ascii")
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        return verifier, challenge

    def _authorize(self, *, scopes=("manager.read",)):
        verifier, challenge = self._pkce()
        client_id = str(uuid.uuid4())
        redirect_uri = "http://127.0.0.1:43123/callback"
        state = uuid.uuid4().hex
        response = self.client.get(
            "/v1/automation/authorize",
            query_string={
                "client_id": client_id,
                "client_name": "Pandrator MCP test",
                "subject": "owner",
                "application_instance_id": str(uuid.uuid4()),
                "canonical_application_origin": (
                    "https://pandrator.example"
                ),
                "canonical_recovery_origin": "https://setup.example",
                "scope": " ".join(scopes),
                "expires_in_seconds": "3600",
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "redirect_uri": redirect_uri,
                "state": state,
            },
            headers=self.forwarded,
        )
        self.assertEqual(200, response.status_code, response.get_json())
        nonce = re.search(
            r'name="authorization_nonce" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(nonce)
        approved = self.client.post(
            "/v1/automation/authorize",
            data={
                "authorization_nonce": nonce.group(1),
                "csrf_token": self.browser_session.csrf_token,
                "decision": "approve",
            },
            headers={
                **self.forwarded,
                "Origin": "https://setup.example",
            },
        )
        self.assertEqual(302, approved.status_code, approved.get_json())
        callback = urlsplit(approved.headers["Location"])
        parameters = parse_qs(callback.query)
        self.assertEqual([state], parameters["state"])
        exchanged = self.client.post(
            "/v1/automation/token",
            json={
                "client_id": client_id,
                "grant_code": parameters["code"][0],
                "code_verifier": verifier,
                "manager_instance_id": self.application.instance_id,
            },
            headers=self.forwarded,
        )
        self.assertEqual(200, exchanged.status_code, exchanged.get_json())
        return exchanged.get_json()

    def test_remote_recovery_enrollment_scope_and_revocation(self):
        identity = self.client.get(
            "/v1/automation/identity",
            headers=self.forwarded,
        )
        self.assertEqual(200, identity.status_code)
        self.assertTrue(identity.get_json()["automation_enabled"])
        self.assertEqual(
            "https://setup.example",
            identity.get_json()["canonical_recovery_origin"],
        )

        remote_permanent = self.api.test_client().get(
            "/v1/status",
            headers={
                **self.forwarded,
                "Authorization": f"Bearer {self.permanent_secret}",
            },
        )
        self.assertEqual(401, remote_permanent.status_code)

        enrollment = self._authorize(scopes=("manager.read",))
        access_token = enrollment["access_token"]
        principal = enrollment["principal"]
        authorization = {
            **self.forwarded,
            "Authorization": f"Bearer {access_token}",
        }

        self.assertEqual(
            200,
            self.client.get(
                "/v1/status",
                headers=authorization,
            ).status_code,
        )
        inspected = self.client.get(
            "/v1/automation/principal",
            headers=authorization,
        )
        self.assertEqual(200, inspected.status_code)
        self.assertNotIn(
            access_token,
            inspected.get_data(as_text=True),
        )
        denied_scope = self.client.post(
            "/v1/application/start",
            json={},
            headers={
                **authorization,
                "Idempotency-Key": "automation-runtime-denied",
            },
        )
        self.assertEqual(403, denied_scope.status_code)
        self.assertEqual(
            "scope_denied",
            denied_scope.get_json()["error"]["code"],
        )
        denied_route = self.client.get(
            "/v1/network",
            headers=authorization,
        )
        self.assertEqual(403, denied_route.status_code)
        self.assertEqual(
            "automation_route_denied",
            denied_route.get_json()["error"]["code"],
        )

        listed = self.client.get(
            "/v1/automation/clients",
            headers=self.forwarded,
        )
        self.assertEqual(200, listed.status_code)
        self.assertEqual(
            principal["client_id"],
            listed.get_json()["items"][0]["client_id"],
        )
        revoked = self.client.delete(
            f"/v1/automation/clients/{principal['client_id']}",
            headers={
                **self.forwarded,
                "X-CSRF-Token": self.browser_session.csrf_token,
                "Idempotency-Key": "revoke-manager-automation-client",
            },
        )
        self.assertEqual(200, revoked.status_code)
        self.assertEqual(
            401,
            self.client.get(
                "/v1/automation/principal",
                headers=authorization,
            ).status_code,
        )

        database_files = list(
            self.application.context.layout.state.glob(
                "manager.sqlite3*"
            )
        )
        persisted = b"".join(
            path.read_bytes()
            for path in database_files
            if path.is_file()
        )
        self.assertNotIn(access_token.encode("utf-8"), persisted)

    def test_openapi_distinguishes_local_browser_and_automation_authority(
        self,
    ):
        response = self.client.get(
            "/v1/openapi.json",
            headers=self.forwarded,
        )
        self.assertEqual(200, response.status_code)
        document = response.get_json()
        schemes = document["components"]["securitySchemes"]
        self.assertEqual(
            {
                "managerAutomationBearer",
                "managerBrowserSession",
                "managerLocalBearer",
            },
            set(schemes),
        )
        self.assertEqual(
            "/v1/automation/authorize",
            schemes["managerAutomationBearer"]["flows"][
                "authorizationCode"
            ]["authorizationUrl"],
        )
        self.assertNotIn(
            "security",
            document["paths"]["/v1/automation/identity"]["get"],
        )
        self.assertIn(
            {"managerAutomationBearer": ["manager.read"]},
            document["paths"]["/v1/status"]["get"]["security"],
        )
        self.assertIn(
            "429",
            document["paths"]["/v1/status"]["get"]["responses"],
        )
        self.assertNotIn(
            {"managerAutomationBearer": ["manager.read"]},
            document["paths"]["/v1/network"]["get"]["security"],
        )
        for path, method in (
            ("/v1/automation/authorize", "get"),
            ("/v1/automation/authorize", "post"),
            ("/v1/session", "delete"),
            ("/v1/browser-sessions", "delete"),
        ):
            self.assertEqual(
                [{"managerBrowserSession": []}],
                document["paths"][path][method]["security"],
            )
        for path, method in (
            ("/v1/automation/clients", "get"),
            ("/v1/automation/clients/{client_id}", "delete"),
        ):
            self.assertEqual(
                [
                    {"managerLocalBearer": []},
                    {"managerBrowserSession": []},
                ],
                document["paths"][path][method]["security"],
            )

    def test_local_manager_client_can_revoke_recovery_client(self):
        enrollment = self._authorize(scopes=("manager.read",))
        principal = enrollment["principal"]
        access_token = enrollment["access_token"]
        local_client = self.api.test_client()
        owner_headers = {
            "Host": "setup.example",
            "Authorization": f"Bearer {self.permanent_secret}",
        }

        listed = local_client.get(
            "/v1/automation/clients",
            headers=owner_headers,
        )
        self.assertEqual(200, listed.status_code, listed.get_json())
        self.assertEqual(
            principal["client_id"],
            listed.get_json()["items"][0]["client_id"],
        )
        revoked = local_client.delete(
            f"/v1/automation/clients/{principal['client_id']}",
            headers={
                **owner_headers,
                "Idempotency-Key": "local-owner-revoke-client",
            },
        )
        self.assertEqual(200, revoked.status_code, revoked.get_json())
        self.assertEqual(
            401,
            self.client.get(
                "/v1/automation/principal",
                headers={
                    **self.forwarded,
                    "Authorization": f"Bearer {access_token}",
                },
            ).status_code,
        )

    def test_authenticated_recovery_clients_are_rate_limited(self):
        enrollment = self._authorize(scopes=("manager.read",))
        limited_api = create_api(
            self.application,
            self.supervisor,
            client_secret=self.permanent_secret,
            recovery_sessions=self.sessions,
            manager_exposure=self.exposure,
            automation_rate_limiter=ManagerAutomationRateLimiter(
                maximum_requests=1,
                window_seconds=30,
            ),
        )
        authorization = {
            **self.forwarded,
            "Authorization": f"Bearer {enrollment['access_token']}",
        }
        client = limited_api.test_client()

        self.assertEqual(
            200,
            client.get("/v1/status", headers=authorization).status_code,
        )
        limited = client.get("/v1/status", headers=authorization)

        self.assertEqual(429, limited.status_code)
        self.assertEqual(
            "automation_rate_limited",
            limited.get_json()["error"]["code"],
        )
        self.assertEqual("30", limited.headers["Retry-After"])


if __name__ == "__main__":
    unittest.main()
