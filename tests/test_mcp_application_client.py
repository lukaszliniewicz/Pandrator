import hashlib
import ipaddress
import json
import re
import ssl
import tempfile
import threading
import unittest
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.clients.manager_gateway import FallbackManagerGateway
from pandrator_mcp.clients.manager_recovery import (
    RecoveryManagerGateway,
)
from pandrator_mcp.credentials import (
    CredentialReference,
    CredentialResolver,
    SecretValue,
)
from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.network_policy import NetworkPolicy, TargetMode
from pandrator_mcp.targets import TargetProfile, TargetRegistry


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        payload: dict[str, Any] | None = None,
        *,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.status_code = status_code
        self.body = (
            body if body is not None else json.dumps(payload or {}).encode("utf-8")
        )
        self.headers = headers or {}
        self.closed = False

    def iter_content(self, chunk_size: int):
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        self.closed = True


class FakeSession(requests.Session):
    def __init__(self, responses: list[FakeResponse]) -> None:
        super().__init__()
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append({"url": url, **kwargs})
        return self.responses.pop(0)

    def request(
        self,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> FakeResponse:
        self.calls.append(
            {
                "method": method,
                "url": url,
                **kwargs,
            }
        )
        return self.responses.pop(0)


def local_registry(origin: str) -> TargetRegistry:
    profile = TargetProfile(
        name="local",
        mode=TargetMode.LOCAL_MANAGED,
        workspace="C:/Pandrator",
    )
    return TargetRegistry(
        [profile],
        network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
        local_discovery=lambda _profile: (origin, "manager-id"),
    )


def private_registry(origin: str, ca_bundle: str) -> TargetRegistry:
    profile = TargetProfile(
        name="private",
        mode=TargetMode.PRIVATE_NETWORK,
        application_origin=origin,
        allowed_private_cidrs=("127.0.0.0/8",),
        ca_bundle=ca_bundle,
    )
    return TargetRegistry(
        [profile],
        network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
    )


class MemoryCredentialBackend:
    name = "keyring"

    def __init__(self, values: dict[str, str]) -> None:
        self.values = values

    def resolve(
        self,
        reference: CredentialReference,
    ) -> SecretValue:
        return SecretValue(self.values[reference.reference])

    def store(
        self,
        reference: CredentialReference,
        value: SecretValue,
    ) -> None:
        self.values[reference.reference] = value.reveal()

    def delete(self, reference: CredentialReference) -> None:
        self.values.pop(reference.reference, None)


def create_test_ca(directory: Path) -> tuple[Path, Path, Path]:
    now = datetime.now(UTC)
    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Pandrator test CA")])
    ca_certificate = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_certificate = (
        x509.CertificateBuilder()
        .subject_name(server_name)
        .issuer_name(ca_name)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(ca_key, hashes.SHA256())
    )
    ca_path = directory / "ca.pem"
    certificate_path = directory / "server.pem"
    key_path = directory / "server-key.pem"
    ca_path.write_bytes(ca_certificate.public_bytes(serialization.Encoding.PEM))
    certificate_path.write_bytes(
        server_certificate.public_bytes(serialization.Encoding.PEM)
    )
    key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return ca_path, certificate_path, key_path


class ApplicationClientTests(unittest.TestCase):
    def test_upload_catalog_and_generation_helpers_use_bounded_routes(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(201, {"id": "upload-1", "chunk_count": 1}),
                FakeResponse(200, {"index": 0}),
                FakeResponse(201, {"artifact_id": "artifact-1"}),
                FakeResponse(200, {"services": []}),
                FakeResponse(200, {"items": []}),
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        client.initialize_upload(
            filename="course.mp4",
            size_bytes=4,
            mime_type="video/mp4",
            sha256="a" * 64,
            idempotency_key="upload:course:1",
        )
        client.upload_chunk("upload-1", 0, b"data", sha256="b" * 64)
        client.complete_upload("upload-1")
        client.tts_catalog(refresh=True)
        client.list_generation_runs("session-1")

        initialized, chunk, completed, catalog, runs = session.calls
        self.assertTrue(initialized["url"].endswith("/api/v1/uploads/init"))
        self.assertEqual("upload:course:1", initialized["headers"]["Idempotency-Key"])
        self.assertEqual("PUT", chunk["method"])
        self.assertEqual(b"data", chunk["data"])
        self.assertEqual("b" * 64, chunk["headers"]["X-Chunk-SHA256"])
        self.assertTrue(completed["url"].endswith("/api/v1/uploads/upload-1/complete"))
        self.assertEqual({"refresh": "true"}, catalog["params"])
        self.assertTrue(
            runs["url"].endswith("/api/v1/sessions/session-1/generation-runs")
        )

    def test_artifact_download_resumes_and_rejects_symlink_partials(self):
        origin = "http://127.0.0.1:8097"
        content = b"abcdef"
        session = FakeSession([FakeResponse(206, body=content[3:])])
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "final.bin"
            partial = Path(directory) / ".final.bin.part"
            partial.write_bytes(content[:3])
            result = client.download_artifact(
                "artifact-1",
                destination,
                expected_size=len(content),
                expected_hash=hashlib.sha256(content).hexdigest(),
            )
            self.assertTrue(result["resumed"])
            self.assertEqual(content, destination.read_bytes())
            self.assertEqual("bytes=3-", session.calls[0]["headers"]["Range"])

            other = Path(directory) / "other.bin"
            other.write_bytes(b"do not overwrite")
            malicious = Path(directory) / ".blocked.bin.part"
            malicious.symlink_to(other)
            with self.assertRaises(PandratorMcpError) as captured:
                client.download_artifact(
                    "artifact-2",
                    Path(directory) / "blocked.bin",
                    expected_size=4,
                    expected_hash=None,
                )
            self.assertEqual("network_policy_denied", captured.exception.code)
            self.assertEqual(b"do not overwrite", other.read_bytes())

    def test_session_write_methods_map_exact_bodies_and_revisions(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(201, {"id": "session-1", "revision": 1}),
                FakeResponse(200, {"id": "session-1", "revision": 2}),
                FakeResponse(
                    201,
                    {
                        "id": "attachment-1",
                        "session_revision": 3,
                    },
                ),
                FakeResponse(
                    200,
                    {"section": "tts", "revision": 1},
                ),
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        client.create_session(
            name="Project",
            workflow_kind="audiobook",
            source_language="en",
            target_language=None,
            workflow_preset="custom",
            included_stages=("prepare_text", "generate_audio"),
            idempotency_key="session:create:1",
        )
        client.update_session(
            "session-1",
            expected_revision=1,
            changes={"name": "Revised"},
            idempotency_key="session:update:1",
        )
        client.attach_existing_source(
            "session-1",
            source_asset_id="source-1",
            role="primary",
            expected_session_revision=2,
            idempotency_key="source:attach:1",
        )
        client.update_session_settings(
            "session-1",
            section="tts",
            value={"model": "model-1"},
            expected_revision=0,
            idempotency_key="settings:tts:1",
        )

        create, update, attach, settings = session.calls
        self.assertEqual("POST", create["method"])
        self.assertTrue(create["url"].endswith("/api/v1/sessions"))
        self.assertEqual(
            {
                "name": "Project",
                "workflow_kind": "audiobook",
                "source_language": "en",
                "target_language": None,
                "workflow_preset": "custom",
                "included_stages": [
                    "prepare_text",
                    "generate_audio",
                ],
            },
            json.loads(create["data"]),
        )
        self.assertEqual(
            "session:create:1",
            create["headers"]["Idempotency-Key"],
        )

        self.assertEqual("PATCH", update["method"])
        self.assertTrue(update["url"].endswith("/api/v1/sessions/session-1"))
        self.assertEqual({"name": "Revised"}, json.loads(update["data"]))
        self.assertEqual('"1"', update["headers"]["If-Match"])

        self.assertEqual("POST", attach["method"])
        self.assertTrue(attach["url"].endswith("/api/v1/sessions/session-1/sources"))
        self.assertEqual(
            {"source_asset_id": "source-1", "role": "primary"},
            json.loads(attach["data"]),
        )
        self.assertEqual('"2"', attach["headers"]["If-Match"])

        self.assertEqual("PUT", settings["method"])
        self.assertTrue(
            settings["url"].endswith("/api/v1/sessions/session-1/settings/tts")
        )
        self.assertEqual(
            {"value": {"model": "model-1"}},
            json.loads(settings["data"]),
        )
        self.assertEqual('"0"', settings["headers"]["If-Match"])

    def test_dispatch_methods_map_exact_routes_bodies_and_queries(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(201, {"run_id": "run-1", "status": "queued"}),
                FakeResponse(200, {"items": []}),
                FakeResponse(200, {"run_id": "run-1", "status": "completed"}),
                FakeResponse(
                    200,
                    {
                        "run_id": "run-1",
                        "batch_id": "batch-1",
                        "lease_token": "lease-capability",
                        "expiry": "2030-01-01T00:00:00Z",
                        "prompt": "prompt",
                        "task": "task",
                        "source_batch": [{"id": "cue-1", "text": "Hello"}],
                    },
                ),
                FakeResponse(
                    200,
                    {"batch_id": "batch-1", "lease_token": "lease-capability"},
                ),
                FakeResponse(200, {"batch_id": "batch-1", "status": "released"}),
                FakeResponse(
                    200,
                    {"batch_id": "batch-1", "run_id": "run-1", "status": "accepted"},
                ),
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        client.create_dispatch_run(
            "session-1",
            kind="translation",
            source_artifact_id="artifact-1",
            source_language="en",
            target_language="pl",
            instructions="Preserve cue boundaries.",
            char_limit=6000,
            max_segments_per_batch=40,
            no_remove_subtitles=False,
            timing_context_mode="full",
            substantial_gap_ms=2000,
            glossary={"Pandrator": "Pandrator"},
            idempotency_key="dispatch:create:1",
        )
        client.list_dispatch_runs("session-1", limit=20)
        client.get_dispatch_run("run-1")
        client.claim_dispatch_batch(
            "run-1",
            lease_seconds=900,
            idempotency_key="dispatch:claim:1",
        )
        client.renew_dispatch_batch(
            "batch-1",
            lease_token="lease-capability",
            lease_seconds=600,
            idempotency_key="dispatch:renew:1",
        )
        client.release_dispatch_batch(
            "batch-1",
            lease_token="lease-capability",
            idempotency_key="dispatch:release:1",
        )
        client.submit_dispatch_batch(
            "batch-1",
            lease_token="lease-capability",
            result={
                "kind": "translation",
                "translations": [{"cue_id": 1, "text": "Cześć"}],
            },
            idempotency_key="dispatch:submit:1",
        )

        create, listed, fetched, claim, renew, release, submit = session.calls
        self.assertEqual(
            {
                "kind": "translation",
                "source_artifact_id": "artifact-1",
                "source_language": "en",
                "target_language": "pl",
                "instructions": "Preserve cue boundaries.",
                "char_limit": 6000,
                "max_segments_per_batch": 40,
                "no_remove_subtitles": False,
                "context_before": 8,
                "context_after": 2,
                "timing_context_mode": "full",
                "substantial_gap_ms": 2000,
                "glossary": {"Pandrator": "Pandrator"},
                "execution_mode": "serial",
                "max_parallel_batches": 1,
                "context_capsule": {},
            },
            json.loads(create["data"]),
        )
        self.assertEqual("dispatch:create:1", create["headers"]["Idempotency-Key"])
        self.assertEqual("GET", listed.get("method", "GET"))
        self.assertTrue(
            listed["url"].endswith("/api/v1/sessions/session-1/dispatch-runs")
        )
        self.assertEqual({"limit": 20}, listed["params"])
        self.assertTrue(fetched["url"].endswith("/api/v1/dispatch-runs/run-1"))
        self.assertEqual("POST", claim["method"])
        self.assertTrue(claim["url"].endswith("/api/v1/dispatch-runs/run-1/claim"))
        self.assertEqual({"lease_seconds": 900}, json.loads(claim["data"]))
        self.assertEqual("dispatch:claim:1", claim["headers"]["Idempotency-Key"])
        self.assertTrue(renew["url"].endswith("/api/v1/dispatch-batches/batch-1/renew"))
        self.assertEqual(
            {"lease_token": "lease-capability", "lease_seconds": 600},
            json.loads(renew["data"]),
        )
        self.assertTrue(
            release["url"].endswith("/api/v1/dispatch-batches/batch-1/release")
        )
        self.assertEqual(
            {"lease_token": "lease-capability"}, json.loads(release["data"])
        )
        self.assertTrue(
            submit["url"].endswith("/api/v1/dispatch-batches/batch-1/submit")
        )
        self.assertEqual(
            {
                "lease_token": "lease-capability",
                "result": {
                    "kind": "translation",
                    "translations": [{"cue_id": 1, "text": "Cześć"}],
                },
                "context_delta": {},
            },
            json.loads(submit["data"]),
        )
        self.assertEqual("dispatch:renew:1", renew["headers"]["Idempotency-Key"])
        self.assertEqual("dispatch:release:1", release["headers"]["Idempotency-Key"])
        self.assertEqual("dispatch:submit:1", submit["headers"]["Idempotency-Key"])

    def test_speech_optimization_dispatch_maps_exact_routes_and_bodies(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(201, {"id": "run-1", "status": "ready"}),
                FakeResponse(200, {"items": []}),
                FakeResponse(200, {"id": "run-1", "status": "ready"}),
                FakeResponse(200, {"batch_id": "batch-1"}),
                FakeResponse(200, {"batch_id": "batch-1"}),
                FakeResponse(200, {"batch_id": "batch-1"}),
                FakeResponse(200, {"batch_id": "batch-1", "accepted": True}),
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        client.create_speech_optimization_dispatch_run(
            "session-1",
            source_artifact_id=None,
            language="en",
            voice_language="en-US",
            tts_service="xtts",
            instructions="Natural delivery.",
            char_limit=50_000,
            max_units_per_batch=200,
            context_before=5,
            context_after=3,
            include_timing=True,
            idempotency_key="speech:create:1",
        )
        client.list_speech_optimization_dispatch_runs("session-1", limit=30)
        client.get_speech_optimization_dispatch_run("run-1")
        client.claim_speech_optimization_dispatch_batch(
            "run-1",
            lease_seconds=900,
            idempotency_key="speech:claim:1",
        )
        client.renew_speech_optimization_dispatch_batch(
            "batch-1",
            lease_token="lease-capability",
            lease_seconds=600,
            idempotency_key="speech:renew:1",
        )
        client.release_speech_optimization_dispatch_batch(
            "batch-1",
            lease_token="lease-capability",
            idempotency_key="speech:release:1",
        )
        client.submit_speech_optimization_dispatch_batch(
            "batch-1",
            lease_token="lease-capability",
            result={
                "kind": "speech_optimization",
                "items": [{"unit_id": 1, "text": "Doctor Jones"}],
            },
            idempotency_key="speech:submit:1",
        )

        create, listed, fetched, claim, renew, release, submit = session.calls
        self.assertEqual(
            {
                "instructions": "Natural delivery.",
                "char_limit": 50_000,
                "max_units_per_batch": 200,
                "context_before": 5,
                "context_after": 3,
                "include_timing": True,
                "language": "en",
                "voice_language": "en-US",
                "tts_service": "xtts",
                "execution_mode": "serial",
                "max_parallel_batches": 1,
                "context_capsule": {},
            },
            json.loads(create["data"]),
        )
        self.assertTrue(
            create["url"].endswith(
                "/api/v1/sessions/session-1/speech-optimization-dispatch-runs"
            )
        )
        self.assertEqual({"limit": 30}, listed["params"])
        self.assertTrue(
            fetched["url"].endswith("/api/v1/speech-optimization-dispatch-runs/run-1")
        )
        self.assertEqual({"lease_seconds": 900}, json.loads(claim["data"]))
        self.assertEqual(
            {"lease_token": "lease-capability", "lease_seconds": 600},
            json.loads(renew["data"]),
        )
        self.assertEqual(
            {"lease_token": "lease-capability"}, json.loads(release["data"])
        )
        self.assertEqual(
            {
                "lease_token": "lease-capability",
                "result": {
                    "kind": "speech_optimization",
                    "items": [{"unit_id": 1, "text": "Doctor Jones"}],
                },
                "context_delta": {},
            },
            json.loads(submit["data"]),
        )

    def test_dispatch_error_codes_preserve_backend_message_and_details(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(
                    409,
                    {
                        "error": {
                            "code": "lease_expired",
                            "message": "Lease expired before submission.",
                            "details": {"batch_id": "batch-1", "retryable": True},
                        }
                    },
                )
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )
        with self.assertRaises(PandratorMcpError) as caught:
            client.submit_dispatch_batch(
                "batch-1",
                lease_token="lease-capability",
                response_text="{}",
                idempotency_key="dispatch:submit:2",
            )
        self.assertEqual("lease_expired", caught.exception.code)
        self.assertEqual("Lease expired before submission.", str(caught.exception))
        self.assertEqual("batch-1", caught.exception.details["batch_id"])
        self.assertTrue(caught.exception.retryable)

    def test_bounded_post_uses_csrf_idempotency_and_typed_errors(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "schema_version": "1",
                        "type": "job",
                        "id": "job-1",
                        "state": "cancelled",
                    },
                ),
                FakeResponse(
                    409,
                    {
                        "error": {
                            "code": "plan_stale",
                            "message": "Relevant workflow state changed.",
                            "details": {"retryable": False},
                        }
                    },
                ),
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )

        cancelled = client.cancel_work(
            "job-1",
            idempotency_key="cancel:job-1",
        )
        self.assertEqual("cancelled", cancelled["state"])
        call = session.calls[0]
        self.assertEqual("POST", call["method"])
        self.assertEqual(b"{}", call["data"])
        self.assertEqual(
            "cancel:job-1",
            call["headers"]["Idempotency-Key"],
        )
        self.assertEqual(
            "csrf-value",
            call["headers"]["X-CSRF-Token"],
        )
        self.assertFalse(call["allow_redirects"])

        with self.assertRaises(PandratorMcpError) as caught:
            client.execute_workflow_plan(
                "plan-1",
                plan_digest="a" * 64,
                accepted_confirmations=(),
                idempotency_key="workflow-plan:plan-1",
            )
        self.assertEqual("plan_stale", caught.exception.code)
        self.assertEqual(409, caught.exception.details["status"])

    def test_local_authentication_bootstraps_and_retries_only_once(self):
        origin = "http://127.0.0.1:8097"
        identity = {
            "schema_version": "1",
            "service": "pandrator",
            "instance_id": "application-id",
            "application_version": "0.6.0",
            "api_version": "v1",
            "protocol_version": "v1",
            "canonical_origin": origin,
            "managed": True,
            "manager_instance_id": "manager-id",
        }
        session = FakeSession(
            [
                FakeResponse(401, {"error": "expired"}),
                FakeResponse(200, identity),
            ]
        )
        bootstraps: list[str] = []
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda target, _session: bootstraps.append(
                target.application.origin
            ),
        )

        self.assertEqual(identity, client.identity())
        self.assertEqual([origin, origin], bootstraps)
        self.assertEqual(2, len(session.calls))
        for call in session.calls:
            self.assertFalse(call["allow_redirects"])
            self.assertEqual({}, call["proxies"])
            self.assertIn("X-Request-ID", call["headers"])
            self.assertRegex(
                call["headers"]["traceparent"],
                re.compile(r"^00-[a-f0-9]{32}-[a-f0-9]{16}-01$"),
            )

    def test_redirects_size_limits_and_scope_errors_are_typed(self):
        origin = "http://127.0.0.1:8097"
        cases = [
            (
                FakeResponse(302, {}),
                "network_policy_denied",
            ),
            (
                FakeResponse(403, {}),
                "scope_denied",
            ),
            (
                FakeResponse(200, body=b"x" * (64 * 1024 + 1)),
                "response_too_large",
            ),
        ]
        for response, code in cases:
            with self.subTest(code=code):
                client = ApplicationClient(
                    local_registry(origin).bind("local"),
                    CredentialResolver(()),
                    session=FakeSession([response]),
                    local_bootstrap=lambda _target, _session: None,
                    maximum_response_bytes=64 * 1024,
                )
                method = (
                    client.health if code != "scope_denied" else client.capabilities
                )
                with self.assertRaises(PandratorMcpError) as caught:
                    method()
                self.assertEqual(code, caught.exception.code)

    def test_identity_canonical_origin_must_equal_configured_origin(self):
        origin = "https://application.example"
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "schema_version": "1",
                        "service": "pandrator",
                        "instance_id": "application-id",
                        "application_version": "0.6.0",
                        "api_version": "v1",
                        "protocol_version": "v1",
                        "canonical_origin": "https://other.example",
                    },
                )
            ]
        )
        profile = TargetProfile(
            name="external",
            mode=TargetMode.EXTERNAL_HTTPS,
            application_origin=origin,
            application_credential=CredentialReference(
                backend="keyring",
                reference="token",
                audience="application",
            ),
        )
        registry = TargetRegistry(
            [profile],
            network_policy=NetworkPolicy(lambda _host, _port: ("8.8.8.8",)),
        )
        client = ApplicationClient(
            registry.bind("external"),
            CredentialResolver((MemoryCredentialBackend({"token": "secret"}),)),
            session=session,
        )
        with self.assertRaises(PandratorMcpError) as caught:
            client.identity()
        self.assertEqual("target_identity_mismatch", caught.exception.code)

    def test_identity_local_managed_allows_different_canonical_origin(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(
                    200,
                    {
                        "schema_version": "1",
                        "service": "pandrator",
                        "instance_id": "application-id",
                        "application_version": "0.6.0",
                        "api_version": "v1",
                        "protocol_version": "v1",
                        "canonical_origin": "http://192.168.1.164:8097",
                        "managed": True,
                        "manager_instance_id": "manager-id",
                    },
                )
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: None,
        )
        payload = client.identity()
        self.assertEqual("http://192.168.1.164:8097", payload["canonical_origin"])

    def test_save_subtitle_review(self):
        origin = "http://127.0.0.1:8097"
        session = FakeSession(
            [
                FakeResponse(
                    201,
                    {
                        "artifact_id": "art-1",
                        "document_id": "doc-1",
                        "revision_id": "rev-2",
                        "revision": 2,
                    },
                ),
            ]
        )
        client = ApplicationClient(
            local_registry(origin).bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: None,
        )
        saved = client.save_subtitle_review(
            "session-1",
            "transcribe",
            expected_revision=1,
            segments=[
                {"start_ms": 0, "end_ms": 2500, "text": "Hello world", "speaker": "A"}
            ],
            idempotency_key="sub-key-1",
        )
        self.assertEqual(2, saved["revision"])
        self.assertEqual("art-1", saved["artifact_id"])
        self.assertEqual(
            "http://127.0.0.1:8097/api/v1/sessions/session-1/subtitles/transcribe/review",
            session.calls[0]["url"],
        )
        self.assertEqual("sub-key-1", session.calls[0]["headers"]["Idempotency-Key"])

    def test_real_http_connection_keeps_host_header_while_using_pinned_ip(self):
        captured: dict[str, str] = {}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                captured["host"] = str(self.headers.get("Host") or "")
                body = json.dumps({"status": "ok", "service": "pandrator"}).encode(
                    "utf-8"
                )
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: Any) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = int(server.server_address[1])
            origin = f"http://localhost:{port}"
            client = ApplicationClient(
                local_registry(origin).bind("local"),
                CredentialResolver(()),
            )
            self.assertEqual("ok", client.health()["status"])
            self.assertEqual(f"localhost:{port}", captured["host"])
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_private_https_uses_explicit_ephemeral_ca_and_hostname(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            ca_path, certificate_path, key_path = create_test_ca(root)

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = json.dumps({"status": "ok", "service": "pandrator"}).encode(
                        "utf-8"
                    )
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, _format: str, *_args: Any) -> None:
                    return

            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(certificate_path, key_path)
            server.socket = context.wrap_socket(server.socket, server_side=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                port = int(server.server_address[1])
                origin = f"https://localhost:{port}"
                client = ApplicationClient(
                    private_registry(origin, str(ca_path)).bind("private"),
                    CredentialResolver(()),
                )
                self.assertEqual("ok", client.health()["status"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=5)


class RecoveryManagerGatewayTests(unittest.TestCase):
    @staticmethod
    def binding(*, with_credential: bool = True):
        reference = (
            CredentialReference(
                backend="keyring",
                reference="manager/recovery",
                audience="manager_recovery",
            )
            if with_credential
            else None
        )
        profile = TargetProfile(
            name="private",
            mode=TargetMode.PRIVATE_NETWORK,
            application_origin="https://application.home",
            manager_recovery_origin="https://manager.home",
            allowed_private_cidrs=("127.0.0.0/8",),
            manager_recovery_credential=reference,
            expected_identity={
                "manager_instance_id": "manager-id",
            },
        )
        registry = TargetRegistry(
            [profile],
            network_policy=NetworkPolicy(lambda _host, _port: ("127.0.0.1",)),
        )
        return registry.bind("private")

    def test_direct_recovery_uses_distinct_credential_and_pins_identity(self):
        identity = {
            "schema_version": "1",
            "service": "pandrator-manager",
            "manager_instance_id": "manager-id",
            "canonical_recovery_origin": "https://manager.home",
            "automation_enabled": True,
        }
        session = FakeSession(
            [
                FakeResponse(200, identity),
                FakeResponse(
                    200,
                    {"status": "ready"},
                    headers={"X-Pandrator-Manager-Instance": "manager-id"},
                ),
            ]
        )
        gateway = RecoveryManagerGateway(
            self.binding(),
            CredentialResolver(
                (MemoryCredentialBackend({"manager/recovery": "recovery-secret"}),)
            ),
            session=session,
        )

        result = gateway.status()

        self.assertEqual("direct_recovery", result["gateway"])
        self.assertNotIn("Authorization", session.calls[0]["headers"])
        self.assertEqual(
            "Bearer recovery-secret",
            session.calls[1]["headers"]["Authorization"],
        )
        self.assertEqual({}, session.calls[1]["proxies"])
        self.assertFalse(session.calls[1]["allow_redirects"])

    def test_recovery_redirect_identity_change_and_missing_enrollment_fail_closed(
        self,
    ):
        resolver = CredentialResolver(
            (MemoryCredentialBackend({"manager/recovery": "recovery-secret"}),)
        )
        redirect = RecoveryManagerGateway(
            self.binding(),
            resolver,
            session=FakeSession([FakeResponse(302, {})]),
        )
        with self.assertRaises(PandratorMcpError) as caught:
            redirect.identity()
        self.assertEqual("network_policy_denied", caught.exception.code)

        changed = RecoveryManagerGateway(
            self.binding(),
            resolver,
            session=FakeSession(
                [
                    FakeResponse(
                        200,
                        {
                            "schema_version": "1",
                            "service": "pandrator-manager",
                            "manager_instance_id": "other-manager",
                            "canonical_recovery_origin": ("https://manager.home"),
                            "automation_enabled": True,
                        },
                    )
                ]
            ),
        )
        with self.assertRaises(PandratorMcpError) as caught:
            changed.identity()
        self.assertEqual(
            "target_identity_mismatch",
            caught.exception.code,
        )

        missing = RecoveryManagerGateway(
            self.binding(with_credential=False),
            resolver,
            session=FakeSession([]),
        )
        with self.assertRaises(PandratorMcpError) as caught:
            missing.components()
        self.assertEqual(
            "recovery_enrollment_required",
            caught.exception.code,
        )

    def test_fallback_never_turns_authorization_failure_into_recovery(self):
        class Gateway:
            def __init__(self, outcome):
                self.outcome = outcome
                self.calls = 0

            def status(self):
                self.calls += 1
                if isinstance(self.outcome, Exception):
                    raise self.outcome
                return self.outcome

        primary = Gateway(
            PandratorMcpError(
                "scope_denied",
                "Primary authorization failed.",
            )
        )
        recovery = Gateway({"available": True})
        gateway = FallbackManagerGateway(primary, recovery)
        with self.assertRaises(PandratorMcpError) as caught:
            gateway.status()
        self.assertEqual("scope_denied", caught.exception.code)
        self.assertEqual(0, recovery.calls)

        primary.outcome = PandratorMcpError(
            "application_unavailable",
            "Application is down.",
        )
        self.assertEqual({"available": True}, gateway.status())
        self.assertEqual(1, recovery.calls)


if __name__ == "__main__":
    unittest.main()
