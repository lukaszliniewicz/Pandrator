import os
import re
import tempfile
import unittest
import uuid
from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
from io import StringIO
from pathlib import Path
from unittest import mock
from urllib.parse import parse_qs, urlsplit

from authlib.common.security import generate_token
from authlib.oauth2.rfc7636 import create_s256_code_challenge

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.cli import main as cli_main
from pandrator.web.models import (
    ApiIdempotency,
    ApiToken,
    AudioTake,
    AuditEvent,
    Document,
    DocumentRevision,
    GenerationRun,
    Job,
    OutputAssembly,
    SessionRecord,
    SessionSetting,
    SessionSource,
    SourceAsset,
    utcnow,
)
from pandrator_mcp.network_policy import TargetMode
from pandrator_mcp.targets import TargetProfile, TargetStore
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
                "code_challenge": create_s256_code_challenge(verifier),
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

    def test_owner_can_manage_local_mcp_paths_but_automation_clients_cannot(self):
        workspace = Path(self.temporary.name) / "manager-workspace"
        configuration = workspace / "Pandrator" / "state" / "mcp-targets.json"
        configuration.parent.mkdir(parents=True)
        TargetStore(configuration).put(
            TargetProfile(
                name="managed-local",
                mode=TargetMode.LOCAL_MANAGED,
                workspace=str(workspace),
            )
        )

        automation_headers = self._automation_headers("app.read", "app.write")
        with mock.patch.dict(os.environ, {"PANDRATOR_WORKSPACE": str(workspace)}):
            denied = self.client.get(
                "/api/v1/automation/local-paths",
                headers=automation_headers,
            )
            self.assertEqual(403, denied.status_code)

            owner = self.client.post(
                "/api/v1/auth/bootstrap",
                json={"token": self.owner_grant},
            )
            self.assertEqual(200, owner.status_code)
            csrf = owner.get_json()["csrf_token"]
            home = Path(self.temporary.name) / "home"
            home.mkdir()
            output = Path(self.temporary.name) / "outputs"
            updated = self.client.put(
                "/api/v1/automation/local-paths",
                json={
                    "source_roots": [{"name": "home", "path": str(home)}],
                    "output_root": str(output),
                },
                headers={"X-CSRF-Token": csrf},
            )
            self.assertEqual(200, updated.status_code)
            payload = updated.get_json()
            self.assertEqual("home", payload["source_roots"][0]["name"])
            self.assertEqual(str(home.resolve()), payload["source_roots"][0]["path"])
            self.assertEqual(str(output.resolve()), payload["output_root"])

            loaded = self.client.get("/api/v1/automation/local-paths")
            self.assertEqual(200, loaded.status_code)
            self.assertEqual(payload, loaded.get_json())

    def test_expired_and_origin_bound_tokens_are_rejected(self):
        identity = self.extension["identity"].snapshot(
            observed_origin="https://pandrator.example"
        )
        _expired, expired_raw = self.extension["auth"].create_api_token(
            "expired",
            scopes=["app.read"],
            expires_at=utcnow() - timedelta(seconds=1),
            target_instance_id=identity.instance_id,
            canonical_origin=identity.canonical_origin,
        )
        _wrong_origin, wrong_origin_raw = self.extension["auth"].create_api_token(
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
        wrong_redirect["redirect_uri"] = "http://127.0.0.1:43124/callback"
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
                "redirect_uri": ("http://127.0.0.1:43123/callback"),
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
        self.assertTrue(all(token.token_hash != raw for token in tokens))

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
        self.assertEqual("test-model", settings.get_json()["effective"]["model"])
        self.assertIn("builtin", settings.get_json())
        self.assertIn("context", settings.get_json())
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

    def test_generation_and_subtitle_writes_require_and_replay_idempotency(self):
        headers = self._automation_headers("app.read", "app.write", "app.run")
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Retryable generation", "workflow_kind": "audiobook"},
            headers={**headers, "Idempotency-Key": "retry-generation-session"},
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        self.extension["generation"].create_plan(
            session_id,
            source_revision_id=None,
            segments=[{"text": "A retryable segment."}],
        )
        segment = self.client.get(
            f"/api/v1/sessions/{session_id}/generation-segments",
            headers=headers,
        ).get_json()["items"][0]

        missing_calls = (
            (
                "patch",
                f"/api/v1/generation-segments/{segment['id']}",
                {"text": "Changed."},
                {"If-Match": f'"{segment["revision"]}"'},
            ),
            (
                "post",
                f"/api/v1/sessions/{session_id}/generation-runs",
                {},
                {},
            ),
            (
                "post",
                f"/api/v1/sessions/{session_id}/output-assemblies",
                {},
                {},
            ),
            (
                "post",
                f"/api/v1/sessions/{session_id}/subtitles/transcription/review",
                {
                    "expected_revision": 0,
                    "segments": [{"start_ms": 0, "end_ms": 1000, "text": "Hello"}],
                },
                {},
            ),
        )
        for method, path, body, extra_headers in missing_calls:
            response = getattr(self.client, method)(
                path, json=body, headers={**headers, **extra_headers}
            )
            self.assertEqual(400, response.status_code, response.get_json())
            self.assertEqual(
                "idempotency_key_required", response.get_json()["error"]["code"]
            )

        update_headers = {
            **headers,
            "Idempotency-Key": "retry-generation-segment",
            "If-Match": f'"{segment["revision"]}"',
        }
        updated = self.client.patch(
            f"/api/v1/generation-segments/{segment['id']}",
            json={"text": "Changed."},
            headers=update_headers,
        )
        replayed_update = self.client.patch(
            f"/api/v1/generation-segments/{segment['id']}",
            json={"text": "Changed."},
            headers=update_headers,
        )
        self.assertEqual(200, updated.status_code, updated.get_json())
        self.assertEqual(updated.get_json(), replayed_update.get_json())
        self.assertEqual("true", replayed_update.headers["Idempotency-Replayed"])
        self.assertEqual(updated.headers["ETag"], replayed_update.headers["ETag"])
        conflict = self.client.patch(
            f"/api/v1/generation-segments/{segment['id']}",
            json={"text": "Different."},
            headers=update_headers,
        )
        self.assertEqual(409, conflict.status_code)
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])

        run_headers = {**headers, "Idempotency-Key": "retry-generation-run"}
        started = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs",
            json={},
            headers=run_headers,
        )
        replayed_run = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs",
            json={},
            headers=run_headers,
        )
        self.assertEqual(202, started.status_code, started.get_json())
        self.assertEqual(started.get_json(), replayed_run.get_json())
        self.assertEqual("true", replayed_run.headers["Idempotency-Replayed"])

        assembly_headers = {**headers, "Idempotency-Key": "retry-output-assembly"}
        assembly = self.client.post(
            f"/api/v1/sessions/{session_id}/output-assemblies",
            json={},
            headers=assembly_headers,
        )
        replayed_assembly = self.client.post(
            f"/api/v1/sessions/{session_id}/output-assemblies",
            json={},
            headers=assembly_headers,
        )
        self.assertEqual(202, assembly.status_code, assembly.get_json())
        self.assertEqual(assembly.get_json(), replayed_assembly.get_json())
        self.assertEqual("true", replayed_assembly.headers["Idempotency-Replayed"])

        subtitle_headers = {**headers, "Idempotency-Key": "retry-subtitle-review"}
        subtitle_body = {
            "expected_revision": 0,
            "segments": [{"start_ms": 0, "end_ms": 1000, "text": "Hello"}],
        }
        reviewed = self.client.post(
            f"/api/v1/sessions/{session_id}/subtitles/transcription/review",
            json=subtitle_body,
            headers=subtitle_headers,
        )
        replayed_review = self.client.post(
            f"/api/v1/sessions/{session_id}/subtitles/transcription/review",
            json=subtitle_body,
            headers=subtitle_headers,
        )
        self.assertEqual(201, reviewed.status_code, reviewed.get_json())
        self.assertEqual(reviewed.get_json(), replayed_review.get_json())
        self.assertEqual("true", replayed_review.headers["Idempotency-Replayed"])

        with self.extension["database"].session() as db_session:
            record = db_session.get(SessionRecord, session_id)
        take_path = (
            self.extension["paths"].sessions / record.storage_key / "retry-take.wav"
        )
        take_path.write_bytes(b"RIFFretry")
        artifact = self.extension["artifacts"].register(
            take_path, kind="audio", role="generation_take", session_id=session_id
        )
        with self.extension["database"].session() as db_session:
            take = AudioTake(
                generation_segment_id=segment["id"],
                artifact_id=artifact.id,
                kind="tts",
                status="completed",
            )
            db_session.add(take)
            db_session.flush()
            take_id = take.id

        # The fifth target route also rejects a valid selection without a key.
        missing_select = self.client.post(
            f"/api/v1/generation-segments/{segment['id']}/takes/{take_id}/select",
            headers={**headers, "If-Match": f'"{updated.get_json()["revision"]}"'},
        )
        self.assertEqual(400, missing_select.status_code, missing_select.get_json())
        self.assertEqual(
            "idempotency_key_required", missing_select.get_json()["error"]["code"]
        )
        select_headers = {
            **headers,
            "Idempotency-Key": "retry-generation-take",
            "If-Match": f'"{updated.get_json()["revision"]}"',
        }
        selected = self.client.post(
            f"/api/v1/generation-segments/{segment['id']}/takes/{take_id}/select",
            headers=select_headers,
        )
        replayed_selection = self.client.post(
            f"/api/v1/generation-segments/{segment['id']}/takes/{take_id}/select",
            headers=select_headers,
        )
        self.assertEqual(200, selected.status_code, selected.get_json())
        self.assertEqual(selected.get_json(), replayed_selection.get_json())
        self.assertEqual("true", replayed_selection.headers["Idempotency-Replayed"])

        with self.extension["database"].session() as db_session:
            self.assertEqual(
                1,
                db_session.query(GenerationRun)
                .filter(GenerationRun.session_id == session_id)
                .count(),
            )
            self.assertEqual(
                1,
                db_session.query(OutputAssembly)
                .filter(OutputAssembly.session_id == session_id)
                .count(),
            )
            self.assertEqual(
                2, db_session.query(Job).filter(Job.session_id == session_id).count()
            )
            self.assertEqual(
                1,
                db_session.query(DocumentRevision)
                .join(Document, Document.id == DocumentRevision.document_id)
                .filter(Document.session_id == session_id)
                .count(),
            )

    def test_idempotency_completion_rollback_cleans_generation_and_subtitle(self):
        headers = self._automation_headers("app.read", "app.write", "app.run")
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Rollback retry", "workflow_kind": "audiobook"},
            headers={**headers, "Idempotency-Key": "rollback-retry-session"},
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        self.extension["generation"].create_plan(
            session_id, source_revision_id=None, segments=[{"text": "Retry me."}]
        )

        with (
            mock.patch.object(
                self.extension["idempotency"],
                "complete",
                side_effect=RuntimeError("complete failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "complete failed"),
        ):
            self.client.post(
                f"/api/v1/sessions/{session_id}/generation-runs",
                json={},
                headers={
                    **headers,
                    "Idempotency-Key": "rollback-generation-run",
                },
            )
        with self.extension["database"].session() as db_session:
            self.assertEqual(
                0,
                db_session.query(GenerationRun)
                .filter(GenerationRun.session_id == session_id)
                .count(),
            )
            self.assertEqual(
                0,
                db_session.query(Job).filter(Job.session_id == session_id).count(),
            )
        retried_run = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs",
            json={},
            headers={**headers, "Idempotency-Key": "rollback-generation-run"},
        )
        self.assertEqual(202, retried_run.status_code, retried_run.get_json())

        with self.extension["database"].session() as db_session:
            record = db_session.get(SessionRecord, session_id)
        subtitle_path = (
            self.extension["paths"].sessions
            / record.storage_key
            / "reviewed_transcription_r1.srt"
        )
        subtitle_body = {
            "expected_revision": 0,
            "segments": [{"start_ms": 0, "end_ms": 1000, "text": "Retry subtitle"}],
        }
        with mock.patch.object(
            self.extension["idempotency"],
            "complete",
            side_effect=RuntimeError("complete failed"),
        ):
            failed_subtitle = self.client.post(
                f"/api/v1/sessions/{session_id}/subtitles/transcription/review",
                json=subtitle_body,
                headers={
                    **headers,
                    "Idempotency-Key": "rollback-subtitle-review",
                },
            )
        self.assertEqual(409, failed_subtitle.status_code, failed_subtitle.get_json())
        self.assertFalse(subtitle_path.exists())
        retried_subtitle = self.client.post(
            f"/api/v1/sessions/{session_id}/subtitles/transcription/review",
            json=subtitle_body,
            headers={**headers, "Idempotency-Key": "rollback-subtitle-review"},
        )
        self.assertEqual(201, retried_subtitle.status_code, retried_subtitle.get_json())
        self.assertTrue(subtitle_path.exists())

    def test_generation_replay_reserves_before_preparation_and_abandons_failures(self):
        headers = self._automation_headers("app.read", "app.write", "app.run")
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Preparation ordering", "workflow_kind": "audiobook"},
            headers={**headers, "Idempotency-Key": "prepare-ordering-session"},
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        self.extension["generation"].create_plan(
            session_id, source_revision_id=None, segments=[{"text": "Generate once."}]
        )
        generation = self.extension["generation"]

        with (
            mock.patch.object(
                generation, "prepare_start", wraps=generation.prepare_start
            ) as prepare_start,
            mock.patch.object(
                generation,
                "plan_refresher",
                wraps=generation.plan_refresher,
            ) as plan_refresher,
        ):
            first = self.client.post(
                f"/api/v1/sessions/{session_id}/generation-runs",
                json={},
                headers={**headers, "Idempotency-Key": "prepare-ordering-run"},
            )
            replay = self.client.post(
                f"/api/v1/sessions/{session_id}/generation-runs",
                json={},
                headers={**headers, "Idempotency-Key": "prepare-ordering-run"},
            )
        self.assertEqual(202, first.status_code, first.get_json())
        self.assertEqual(first.get_json(), replay.get_json())
        self.assertEqual("true", replay.headers["Idempotency-Replayed"])
        self.assertEqual(1, prepare_start.call_count)
        self.assertEqual(1, plan_refresher.call_count)
        with self.extension["database"].session() as db_session:
            self.assertEqual(
                1,
                db_session.query(GenerationRun)
                .filter(GenerationRun.session_id == session_id)
                .count(),
            )
            self.assertEqual(
                1,
                db_session.query(Job).filter(Job.session_id == session_id).count(),
            )

        with mock.patch.object(
            generation,
            "prepare_start",
            side_effect=AssertionError("conflicting replay must not prepare"),
        ):
            conflict = self.client.post(
                f"/api/v1/sessions/{session_id}/generation-runs",
                json={"operation": "rvc"},
                headers={**headers, "Idempotency-Key": "prepare-ordering-run"},
            )
        self.assertEqual(409, conflict.status_code, conflict.get_json())
        self.assertEqual("idempotency_conflict", conflict.get_json()["error"]["code"])

        failed = self.client.post(
            "/api/v1/sessions",
            json={"name": "Preparation retry", "workflow_kind": "audiobook"},
            headers={**headers, "Idempotency-Key": "prepare-failure-session"},
        )
        self.assertEqual(201, failed.status_code, failed.get_json())
        failed_session_id = failed.get_json()["id"]
        generation.create_plan(
            failed_session_id,
            source_revision_id=None,
            segments=[{"text": "Prepare again."}],
        )
        with mock.patch.object(
            generation,
            "prepare_start",
            side_effect=ValueError("settings unavailable"),
        ):
            preparation_error = self.client.post(
                f"/api/v1/sessions/{failed_session_id}/generation-runs",
                json={},
                headers={**headers, "Idempotency-Key": "prepare-failure-run"},
            )
        self.assertEqual(409, preparation_error.status_code)
        retried = self.client.post(
            f"/api/v1/sessions/{failed_session_id}/generation-runs",
            json={},
            headers={**headers, "Idempotency-Key": "prepare-failure-run"},
        )
        self.assertEqual(202, retried.status_code, retried.get_json())

    def test_workflow_plan_and_execute_scopes_match_the_mcp_contract(self):
        read_headers = self._automation_headers("app.read")
        planned = self.client.post(
            "/api/v1/sessions/missing/workflow-plans",
            json={"target_stage": "generate_audio"},
            headers=read_headers,
        )
        self.assertEqual(404, planned.status_code)

        execute_path = (
            "/api/v1/workflow-plans/00000000-0000-0000-0000-000000000000/execute"
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
        settings = paths["/api/v1/sessions/{sessionId}/settings/{section}"]["put"]
        attach = paths["/api/v1/sessions/{sessionId}/sources"]["post"]
        for operation in (create, update, settings, attach):
            names = {item["name"] for item in operation.get("parameters", [])}
            self.assertIn("Idempotency-Key", names)
        for operation in (update, settings, attach):
            names = {item["name"] for item in operation.get("parameters", [])}
            self.assertIn("If-Match", names)
        optional_retry_operations = (
            paths["/api/v1/sessions/{sessionId}/subtitles/{stage}/review"]["post"],
            paths["/api/v1/generation-segments/{segmentId}"]["patch"],
            paths["/api/v1/generation-segments/{segmentId}/takes/{takeId}/select"][
                "post"
            ],
            paths["/api/v1/sessions/{sessionId}/generation-runs"]["post"],
            paths["/api/v1/sessions/{sessionId}/output-assemblies"]["post"],
        )
        for operation in optional_retry_operations:
            header = next(
                item
                for item in operation.get("parameters", [])
                if item["name"] == "Idempotency-Key"
            )
            self.assertFalse(header["required"])
            self.assertIn("Automation principals require it", header["description"])
        self.assertIn(
            {"nativeOAuth": ["app.read"]},
            paths["/api/v1/sessions/{sessionId}/workflow-plans"]["post"]["security"],
        )
        self.assertIn(
            {"nativeOAuth": ["app.run"]},
            paths["/api/v1/workflow-plans/{planId}/execute"]["post"]["security"],
        )


if __name__ == "__main__":
    unittest.main()
