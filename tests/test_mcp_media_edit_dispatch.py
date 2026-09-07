import json
import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.clients.application import ApplicationClient
from pandrator_mcp.credentials import CredentialResolver
from pandrator_mcp.schemas import (
    ClaimMediaEditDispatchBatchInput,
    CreateMediaEditDispatchRunInput,
    GetMediaEditDispatchRunInput,
    MediaEditDispatchResultInput,
    SubmitMediaEditDispatchBatchInput,
)
from pandrator_mcp.tools.media_edit_dispatch import (
    claim_media_edit_dispatch_batch,
    create_media_edit_dispatch_run,
    get_media_edit_dispatch_run,
    submit_media_edit_dispatch_batch,
)
from tests import test_mcp_application_client as _client_tests


class _Application:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.status = "ready"
        self.submit_status = "completed"

    def create_media_edit_dispatch_run(self, session_id, **kwargs):
        self.calls.append(("create", {"session_id": session_id, **kwargs}))
        return {
            "id": "run-1",
            "session_id": session_id,
            "kind": "media_edit",
            "source_revision": 2,
            "source_revision_id": "revision-2",
            "source_content_hash": "hash",
            "status": "ready",
            "batch_count": 1,
            "completed_batch_count": 0,
            "instructions": "trim silence",
        }

    def get_media_edit_dispatch_run(self, run_id):
        self.calls.append(("get", {"run_id": run_id}))
        return {
            "id": run_id,
            "session_id": "session-1",
            "status": self.status,
            "source_revision": 2,
            "source_revision_id": "revision-2",
            "source_content_hash": "hash",
            "batch_count": 1,
            "completed_batch_count": 1 if self.status == "completed" else 0,
            "result_revision_id": "revision-3" if self.status == "completed" else None,
            "batches": [{"input_json": {"private": True}}],
        }

    def claim_media_edit_dispatch_batch(self, run_id, **kwargs):
        self.calls.append(("claim", {"run_id": run_id, **kwargs}))
        return {
            "run_id": run_id,
            "batch_id": "batch-1",
            "batch_ordinal": 1,
            "status": "leased",
            "run_status": "running",
            "batch_status": "leased",
            "lease_token": "lease-capability",
            "lease_expires_at": "2030-01-01T00:00:00+00:00",
            "source_revision": {
                "id": "revision-2",
                "revision": 2,
                "content_hash": "hash",
            },
            "task": {"kind": "media_edit", "instructions": "Reason globally."},
            "batch": {
                "duration_ms": 5000,
                "keep_ranges": [{"start_ms": 0, "end_ms": 5000}],
                "cues": [
                    {"id": "cue-1", "start_ms": 0, "end_ms": 500, "text": "hello"}
                ],
                "evidence": {},
                "artifact_ids": {},
                "private": True,
            },
            "private": True,
        }

    def submit_media_edit_dispatch_batch(self, batch_id, **kwargs):
        self.calls.append(("submit", {"batch_id": batch_id, **kwargs}))
        finalizing = self.submit_status == "finalizing"
        return {
            "run_id": "run-1",
            "batch_id": batch_id,
            "session_id": "session-1",
            "kind": "media_edit",
            "status": self.submit_status,
            "run_status": self.submit_status,
            "batch_status": "completed",
            "accepted": True,
            "batch_count": 1,
            "total_batches": 1,
            "completed_batch_count": 1,
            "completed_batches": 1,
            "remaining_batches": 0,
            "finalized": not finalizing,
            "result_revision_id": None if finalizing else "revision-3",
        }


class MediaEditDispatchMcpToolTests(unittest.TestCase):
    def setUp(self):
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_schema_is_strict_and_empty_result_is_valid(self):
        with self.assertRaises(ValidationError):
            CreateMediaEditDispatchRunInput(
                session_id="session-1",
                revision=2,
                idempotency_key="media:create:1",
                unexpected=True,
            )
        empty = MediaEditDispatchResultInput(kind="media_edit", cuts=[])
        self.assertEqual([], empty.cuts)
        with self.assertRaises(ValidationError):
            MediaEditDispatchResultInput(kind="translation", cuts=[])
        with self.assertRaises(ValidationError):
            CreateMediaEditDispatchRunInput(
                session_id="session-1",
                revision=2,
                instructions="  ",
                idempotency_key="media:create:blank",
            )

    def test_create_claim_submit_and_completed_next_actions(self):
        created = create_media_edit_dispatch_run(
            self.runtime,
            CreateMediaEditDispatchRunInput(
                session_id="session-1",
                revision=2,
                instructions=" trim silence ",
                idempotency_key="media:create:1",
            ),
        )
        self.assertEqual(
            "pandrator_claim_media_edit_dispatch_batch",
            created.next_actions[0].tool,
        )
        self.assertNotIn("settings_json", created.result)
        claimed = claim_media_edit_dispatch_batch(
            self.runtime,
            ClaimMediaEditDispatchBatchInput(
                run_id="run-1", idempotency_key="media:claim:1"
            ),
        )
        self.assertEqual("lease-capability", claimed.result["lease_token"])
        self.assertNotIn("words", json.dumps(claimed.result))
        self.assertEqual(
            {
                "pandrator_renew_media_edit_dispatch_batch",
                "pandrator_release_media_edit_dispatch_batch",
            },
            {action.tool for action in claimed.next_actions},
        )
        submitted = submit_media_edit_dispatch_batch(
            self.runtime,
            SubmitMediaEditDispatchBatchInput(
                batch_id="batch-1",
                lease_token="lease-capability",
                result={"kind": "media_edit", "cuts": []},
                idempotency_key="media:submit:1",
            ),
        )
        self.assertEqual("completed", submitted.result["status"])
        self.assertEqual(
            "pandrator_get_media_edit",
            submitted.next_actions[0].tool,
        )
        self.application.status = "completed"
        fetched = get_media_edit_dispatch_run(
            self.runtime,
            GetMediaEditDispatchRunInput(run_id="run-1"),
        )
        self.assertEqual("pandrator_get_media_edit", fetched.next_actions[0].tool)
        self.assertEqual("session-1", fetched.next_actions[0].arguments["session_id"])

    def test_finalizing_submission_can_be_retried_exactly(self):
        self.application.submit_status = "finalizing"
        arguments = SubmitMediaEditDispatchBatchInput(
            batch_id="batch-1",
            lease_token="lease-capability",
            result={"kind": "media_edit", "cuts": []},
            idempotency_key="media:submit:retry",
        )
        submitted = submit_media_edit_dispatch_batch(self.runtime, arguments)
        retry = submitted.next_actions[0]
        self.assertEqual("pandrator_submit_media_edit_dispatch_batch", retry.tool)
        self.assertEqual("media:submit:retry", retry.arguments["idempotency_key"])
        self.assertEqual({"kind": "media_edit", "cuts": []}, retry.arguments["result"])


class MediaEditDispatchApplicationClientTests(unittest.TestCase):
    def test_methods_use_exact_paths_and_idempotency_headers(self):
        session = _client_tests.FakeSession(
            [
                _client_tests.FakeResponse(201, {"id": "run-1"}),
                _client_tests.FakeResponse(200, {"items": []}),
                _client_tests.FakeResponse(200, {"id": "run-1"}),
                _client_tests.FakeResponse(200, {"batch_id": "batch-1"}),
                _client_tests.FakeResponse(200, {"batch_id": "batch-1"}),
                _client_tests.FakeResponse(200, {"batch_id": "batch-1"}),
                _client_tests.FakeResponse(200, {"batch_id": "batch-1"}),
            ]
        )
        client = ApplicationClient(
            _client_tests.local_registry("http://127.0.0.1:8097").bind("local"),
            CredentialResolver(()),
            session=session,
            local_bootstrap=lambda _target, _session: "csrf-value",
        )
        client.create_media_edit_dispatch_run(
            "session-1",
            revision=2,
            instructions="trim",
            idempotency_key="media:create:1",
        )
        client.list_media_edit_dispatch_runs("session-1", limit=20)
        client.get_media_edit_dispatch_run("run-1")
        client.claim_media_edit_dispatch_batch(
            "run-1", lease_seconds=900, idempotency_key="media:claim:1"
        )
        client.renew_media_edit_dispatch_batch(
            "batch-1",
            lease_token="lease",
            lease_seconds=600,
            idempotency_key="media:renew:1",
        )
        client.release_media_edit_dispatch_batch(
            "batch-1", lease_token="lease", idempotency_key="media:release:1"
        )
        client.submit_media_edit_dispatch_batch(
            "batch-1",
            lease_token="lease",
            result={"kind": "media_edit", "cuts": []},
            idempotency_key="media:submit:1",
        )
        create, listed, fetched, claim, renew, release, submit = session.calls
        self.assertTrue(
            create["url"].endswith(
                "/api/v1/sessions/session-1/media-edit-dispatch-runs"
            )
        )
        self.assertEqual(
            {"revision": 2, "instructions": "trim"}, json.loads(create["data"])
        )
        self.assertEqual({"limit": 20}, listed["params"])
        self.assertTrue(
            fetched["url"].endswith("/api/v1/media-edit-dispatch-runs/run-1")
        )
        self.assertEqual("media:claim:1", claim["headers"]["Idempotency-Key"])
        self.assertEqual("media:renew:1", renew["headers"]["Idempotency-Key"])
        self.assertEqual("media:release:1", release["headers"]["Idempotency-Key"])
        self.assertEqual("media:submit:1", submit["headers"]["Idempotency-Key"])


if __name__ == "__main__":
    unittest.main()
