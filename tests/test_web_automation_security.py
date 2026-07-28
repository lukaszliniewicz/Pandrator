import re
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from io import StringIO
from urllib.parse import parse_qs, urlsplit

from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.cli import main as cli_main
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    AuditEvent,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    utcnow,
)
from tests.web_test_support import prepare_web_test_data_root


class AutomationSecurityTests(unittest.TestCase):
    password = "correct horse battery staple"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        prepare_web_test_data_root(self.temporary.name)
        self.bootstrap = BootstrapTokenStore()
        self.owner_grant = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
            public_origin="https://pandrator.example",
        )
        self.extension = self.app.extensions["pandrator"]
        self.extension["auth"].initialize_owner(self.password)
        self.client = self.app.test_client()

    def tearDown(self):
        self.extension["database"].dispose()
        self.temporary.cleanup()

    def _authorization(
        self,
        *,
        scopes=("app.read",),
        redirect_uri="http://127.0.0.1:43123/callback",
        client_id=None,
        verifier=None,
    ):
        client_id = client_id or str(uuid.uuid4())
        verifier = verifier or generate_token(64)
        state = generate_token(48)
        response = self.client.get(
            "/api/v1/auth/automation/authorize",
            query_string={
                "response_type": "code",
                "client_id": client_id,
                "client_name": "Test MCP",
                "redirect_uri": redirect_uri,
                "scope": " ".join(scopes),
                "state": state,
                "code_challenge": create_s256_code_challenge(
                    verifier
                ),
                "code_challenge_method": "S256",
                "expires_in_days": "7",
            },
        )
        self.assertEqual(200, response.status_code)
        nonce_match = re.search(
            r'name="authorization_nonce" value="([^"]+)"',
            response.get_data(as_text=True),
        )
        self.assertIsNotNone(nonce_match)
        approval = self.client.post(
            "/api/v1/auth/automation/authorize",
            data={
                "authorization_nonce": nonce_match.group(1),
                "decision": "approve",
                "password": self.password,
            },
        )
        self.assertEqual(302, approval.status_code)
        location = approval.headers["Location"]
        parameters = parse_qs(urlsplit(location).query)
        self.assertEqual([state], parameters["state"])
        return {
            "client_id": client_id,
            "verifier": verifier,
            "redirect_uri": redirect_uri,
            "code": parameters["code"][0],
        }

    def _exchange(self, authorization):
        return self.client.post(
            "/api/v1/auth/automation/token",
            data={
                "grant_type": "authorization_code",
                "client_id": authorization["client_id"],
                "redirect_uri": authorization["redirect_uri"],
                "code": authorization["code"],
                "code_verifier": authorization["verifier"],
            },
        )

    def _automation_headers(self, *scopes):
        authorization = self._authorization(scopes=scopes)
        exchanged = self._exchange(authorization)
        self.assertEqual(200, exchanged.status_code)
        raw = exchanged.get_json()["access_token"]
        return {"Authorization": f"Bearer {raw}"}

    def _cli(self, *arguments):
        stdout = StringIO()
        stderr = StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            status = cli_main(
                [
                    "--data-dir",
                    self.temporary.name,
                    "--json",
                    *arguments,
                ]
            )
        return status, stdout.getvalue(), stderr.getvalue()

    def test_scoped_tokens_fail_closed_for_writes_credentials_and_admin(
        self,
    ):
        identity = self.extension["identity"].snapshot(
            observed_origin="https://pandrator.example"
        )
        _record, raw = self.extension["auth"].create_api_token(
            "read only",
            scopes=["app.read"],
            target_instance_id=identity.instance_id,
            canonical_origin=identity.canonical_origin,
        )
        headers = {"Authorization": f"Bearer {raw}"}

        self.assertEqual(
            200,
            self.client.get("/api/v1/sessions", headers=headers).status_code,
        )
        write = self.client.post(
            "/api/v1/sessions",
            json={"name": "forbidden"},
            headers=headers,
        )
        credentials = self.client.get(
            "/api/v1/credentials",
            headers=headers,
        )
        raw_job = self.client.post(
            "/api/v1/jobs",
            json={"kind": "noop"},
            headers=headers,
        )
        self.assertEqual(403, write.status_code)
        self.assertEqual(403, credentials.status_code)
        self.assertEqual(403, raw_job.status_code)
        self.assertEqual("scope_denied", write.get_json()["error"]["code"])

    def test_expired_and_origin_bound_tokens_are_rejected(self):
        identity = self.extension["identity"].snapshot(
            observed_origin="https://pandrator.example"
        )
        _expired, expired_raw = self.extension[
            "auth"
        ].create_api_token(
            "expired",
            scopes=["app.read"],
            expires_at=utcnow() - timedelta(seconds=1),
            target_instance_id=identity.instance_id,
            canonical_origin=identity.canonical_origin,
        )
        _wrong_origin, wrong_origin_raw = self.extension[
            "auth"
        ].create_api_token(
            "wrong origin",
            scopes=["app.read"],
            target_instance_id=identity.instance_id,
            canonical_origin="https://other.example",
        )
        for raw in (expired_raw, wrong_origin_raw):
            response = self.client.get(
                "/api/v1/sessions",
                headers={"Authorization": f"Bearer {raw}"},
            )
            self.assertEqual(401, response.status_code)

    def test_pkce_exact_redirect_rotation_and_revocation(self):
        client_id = str(uuid.uuid4())
        first_authorization = self._authorization(
            scopes=("app.read", "app.cancel"),
            client_id=client_id,
        )
        wrong_redirect = dict(first_authorization)
        wrong_redirect["redirect_uri"] = (
            "http://127.0.0.1:43124/callback"
        )
        self.assertEqual(
            400,
            self._exchange(wrong_redirect).status_code,
        )
        first = self._exchange(first_authorization)
        self.assertEqual(200, first.status_code)
        first_payload = first.get_json()
        first_raw = first_payload["access_token"]
        self.assertEqual(
            400,
            self._exchange(first_authorization).status_code,
        )

        second = self._exchange(
            self._authorization(
                scopes=("app.read", "app.cancel"),
                client_id=client_id,
            )
        )
        self.assertEqual(200, second.status_code)
        second_raw = second.get_json()["access_token"]
        self.assertEqual(
            401,
            self.client.get(
                "/api/v1/sessions",
                headers={"Authorization": f"Bearer {first_raw}"},
            ).status_code,
        )
        self.assertEqual(
            200,
            self.client.get(
                "/api/v1/sessions",
                headers={"Authorization": f"Bearer {second_raw}"},
            ).status_code,
        )

        owner = self.client.post(
            "/api/v1/auth/bootstrap",
            json={"token": self.owner_grant},
        )
        csrf = owner.get_json()["csrf_token"]
        revoked = self.client.delete(
            f"/api/v1/auth/automation-clients/{client_id}",
            headers={"X-CSRF-Token": csrf},
        )
        self.assertEqual(204, revoked.status_code)
        self.assertEqual(
            401,
            self.client.get(
                "/api/v1/sessions",
                headers={"Authorization": f"Bearer {second_raw}"},
            ).status_code,
        )

    def test_invalid_redirect_and_admin_scope_are_refused(self):
        verifier = generate_token(64)
        base = {
            "response_type": "code",
            "client_id": str(uuid.uuid4()),
            "client_name": "Test MCP",
            "scope": "app.read",
            "state": generate_token(48),
            "code_challenge": create_s256_code_challenge(verifier),
            "code_challenge_method": "S256",
        }
        invalid_redirect = self.client.get(
            "/api/v1/auth/automation/authorize",
            query_string={
                **base,
                "redirect_uri": "https://attacker.example/callback",
            },
        )
        admin = self.client.get(
            "/api/v1/auth/automation/authorize",
            query_string={
                **base,
                "redirect_uri": (
                    "http://127.0.0.1:43123/callback"
                ),
                "scope": "app.admin",
            },
        )
        self.assertEqual(400, invalid_redirect.status_code)
        self.assertEqual(400, admin.status_code)

    def test_owner_cli_lists_and_revokes_automation_clients(self):
        authorization = self._authorization(scopes=("app.read",))
        exchanged = self._exchange(authorization)
        self.assertEqual(200, exchanged.status_code)
        raw = exchanged.get_json()["access_token"]
        client_id = authorization["client_id"]

        status, output, error = self._cli(
            "auth",
            "automation-client",
            "list",
        )
        self.assertEqual(0, status, error)
        self.assertIn(client_id, output)
        self.assertNotIn(raw, output)

        status, _output, error = self._cli(
            "auth",
            "automation-client",
            "revoke",
            client_id,
        )
        self.assertEqual(2, status)
        self.assertIn("--yes", error)

        status, output, error = self._cli(
            "auth",
            "automation-client",
            "revoke",
            client_id,
            "--yes",
        )
        self.assertEqual(0, status, error)
        self.assertIn(client_id, output)
        self.assertNotIn(raw, output)
        self.assertEqual(
            401,
            self.client.get(
                "/api/v1/sessions",
                headers={"Authorization": f"Bearer {raw}"},
            ).status_code,
        )

    def test_audit_is_content_free_and_never_stores_raw_credentials(self):
        authorization = self._authorization(scopes=("app.read",))
        token_response = self._exchange(authorization)
        raw = token_response.get_json()["access_token"]
        self.client.get(
            "/api/v1/sessions",
            headers={"Authorization": f"Bearer {raw}"},
        )

        with self.extension["database"].session() as db_session:
            events = list(db_session.query(AuditEvent).all())
            tokens = list(db_session.query(ApiToken).all())
        serialized = repr(
            [
                {
                    "subject": event.principal_subject,
                    "path": event.path,
                    "metadata": event.metadata_json,
                }
                for event in events
            ]
        )
        self.assertNotIn(raw, serialized)
        self.assertNotIn(authorization["code"], serialized)
        self.assertTrue(events)
        self.assertTrue(
            all(token.token_hash != raw for token in tokens)
        )

    def test_application_writes_are_atomic_revision_safe_and_replayed(self):
        headers = self._automation_headers("app.read", "app.write")

        missing_key = self.client.post(
            "/api/v1/sessions",
            json={"name": "Missing retry identity"},
            headers=headers,
        )
        self.assertEqual(400, missing_key.status_code)
        self.assertEqual(
            "idempotency_key_required",
            missing_key.get_json()["error"]["code"],
        )

        create_headers = {
            **headers,
            "Idempotency-Key": "session:create:one",
        }
        create_body = {
            "name": "Automation project",
            "workflow_kind": "audiobook",
            "source_language": "en",
            "included_stages": ["prepare_text", "generate_audio"],
        }
        created = self.client.post(
            "/api/v1/sessions",
            json=create_body,
            headers=create_headers,
        )
        replayed_create = self.client.post(
            "/api/v1/sessions",
            json=create_body,
            headers=create_headers,
        )
        self.assertEqual(201, created.status_code)
        self.assertEqual(created.get_json(), replayed_create.get_json())
        self.assertEqual(
            "true",
            replayed_create.headers["Idempotency-Replayed"],
        )
        session_id = created.get_json()["id"]

        changed_body = self.client.post(
            "/api/v1/sessions",
            json={**create_body, "name": "Different project"},
            headers=create_headers,
        )
        self.assertEqual(409, changed_body.status_code)
        self.assertEqual(
            "idempotency_conflict",
            changed_body.get_json()["error"]["code"],
        )

        update_headers = {
            **headers,
            "Idempotency-Key": "session:update:one",
            "If-Match": "1",
        }
        updated = self.client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"name": "Automation project revised"},
            headers=update_headers,
        )
        replayed_update = self.client.patch(
            f"/api/v1/sessions/{session_id}",
            json={"name": "Automation project revised"},
            headers=update_headers,
        )
        self.assertEqual(200, updated.status_code)
        self.assertEqual(2, updated.get_json()["revision"])
        self.assertEqual(updated.get_json(), replayed_update.get_json())
        self.assertEqual(
            "true",
            replayed_update.headers["Idempotency-Replayed"],
        )

        settings_headers = {
            **headers,
            "Idempotency-Key": "settings:tts:one",
            "If-Match": "0",
        }
        settings = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {"model": "test-model", "temperature": 0.4}},
            headers=settings_headers,
        )
        replayed_settings = self.client.put(
            f"/api/v1/sessions/{session_id}/settings/tts",
            json={"value": {"model": "test-model", "temperature": 0.4}},
            headers=settings_headers,
        )
        self.assertEqual(200, settings.status_code)
        self.assertEqual(1, settings.get_json()["revision"])
        self.assertEqual(settings.get_json(), replayed_settings.get_json())
        self.assertEqual(
            "true",
            replayed_settings.headers["Idempotency-Replayed"],
        )

        with self.extension["database"].session() as db_session:
            source = SourceAsset(
                display_name="input.txt",
                kind="txt",
                mime_type="text/plain",
                size_bytes=12,
                content_hash="source-hash",
            )
            db_session.add(source)
            db_session.flush()
            source_id = source.id

        attach_headers = {
            **headers,
            "Idempotency-Key": "source:attach:one",
            "If-Match": "2",
        }
        attached = self.client.post(
            f"/api/v1/sessions/{session_id}/sources",
            json={"source_asset_id": source_id, "role": "primary"},
            headers=attach_headers,
        )
        replayed_attach = self.client.post(
            f"/api/v1/sessions/{session_id}/sources",
            json={"source_asset_id": source_id, "role": "primary"},
            headers=attach_headers,
        )
        self.assertEqual(201, attached.status_code)
        self.assertEqual(3, attached.get_json()["session_revision"])
        self.assertEqual(attached.get_json(), replayed_attach.get_json())
        self.assertEqual(
            "true",
            replayed_attach.headers["Idempotency-Replayed"],
        )

        with self.extension["database"].session() as db_session:
            sessions = list(db_session.query(SessionRecord).all())
            settings_rows = list(db_session.query(SessionSetting).all())
            attachments = list(db_session.query(SessionSource).all())
            reservations = list(db_session.query(ApiIdempotency).all())
        self.assertEqual(1, len(sessions))
        self.assertEqual(3, sessions[0].revision)
        self.assertEqual(1, len(settings_rows))
        self.assertEqual(1, len(attachments))
        self.assertEqual(4, len(reservations))

    def test_workflow_plan_and_execute_scopes_match_the_mcp_contract(self):
        read_headers = self._automation_headers("app.read")
        planned = self.client.post(
            "/api/v1/sessions/missing/workflow-plans",
            json={"target_stage": "generate_audio"},
            headers=read_headers,
        )
        self.assertEqual(404, planned.status_code)

        execute_path = (
            "/api/v1/workflow-plans/"
            "00000000-0000-0000-0000-000000000000/execute"
        )
        execution_body = {
            "plan_digest": "a" * 64,
            "accepted_confirmations": [],
        }
        write_only = self.client.post(
            execute_path,
            json=execution_body,
            headers={
                **self._automation_headers("app.write"),
                "Idempotency-Key": "workflow:scope:write",
            },
        )
        self.assertEqual(403, write_only.status_code)
        self.assertEqual(
            "scope_denied",
            write_only.get_json()["error"]["code"],
        )
        run_scoped = self.client.post(
            execute_path,
            json=execution_body,
            headers={
                **self._automation_headers("app.run"),
                "Idempotency-Key": "workflow:scope:run",
            },
        )
        self.assertEqual(404, run_scoped.status_code)

    def test_openapi_declares_mcp_scopes_and_retry_headers(self):
        document = self.client.get("/api/v1/openapi.json").get_json()
        paths = document["paths"]
        create = paths["/api/v1/sessions"]["post"]
        update = paths["/api/v1/sessions/{sessionId}"]["patch"]
        settings = paths[
            "/api/v1/sessions/{sessionId}/settings/{section}"
        ]["put"]
        attach = paths[
            "/api/v1/sessions/{sessionId}/sources"
        ]["post"]
        for operation in (create, update, settings, attach):
            names = {
                item["name"]
                for item in operation.get("parameters", [])
            }
            self.assertIn("Idempotency-Key", names)
        for operation in (update, settings, attach):
            names = {
                item["name"]
                for item in operation.get("parameters", [])
            }
            self.assertIn("If-Match", names)
        self.assertIn(
            {"nativeOAuth": ["app.read"]},
            paths[
                "/api/v1/sessions/{sessionId}/workflow-plans"
            ]["post"]["security"],
        )
        self.assertIn(
            {"nativeOAuth": ["app.run"]},
            paths[
                "/api/v1/workflow-plans/{planId}/execute"
            ]["post"]["security"],
        )


if __name__ == "__main__":
    unittest.main()
