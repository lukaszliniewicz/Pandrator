import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.schemas import (
    ClaimDispatchBatchInput,
    CreateDispatchRunInput,
    GetDispatchRunInput,
    ListDispatchRunsInput,
    RenewDispatchBatchInput,
    SubmitDispatchBatchInput,
)
from pandrator_mcp.tools.dispatch import (
    claim_dispatch_batch,
    create_dispatch_run,
    get_dispatch_run,
    list_dispatch_runs,
    submit_dispatch_batch,
)


class _Application:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    def create_dispatch_run(self, session_id, **kwargs):
        self.calls.append(("create", {"session_id": session_id, **kwargs}))
        return {"run_id": "run-1", "status": "queued", "instructions": "private"}

    def list_dispatch_runs(self, session_id, **kwargs):
        self.calls.append(("list", {"session_id": session_id, **kwargs}))
        return {
            "items": [{"run_id": "run-1", "status": "running", "prompt": "private"}],
            "total": 1,
        }

    def get_dispatch_run(self, run_id):
        self.calls.append(("get", {"run_id": run_id}))
        return {"run_id": run_id, "status": "completed", "source_batch": "private"}

    def claim_dispatch_batch(self, run_id, **kwargs):
        self.calls.append(("claim", {"run_id": run_id, **kwargs}))
        return {
            "schema_version": "1",
            "run_id": run_id,
            "status": "leased",
            "run_status": "running",
            "batch_status": "leased",
            "batch_id": "batch-1",
            "batch_ordinal": 1,
            "lease_token": "lease-capability",
            "lease_expires_at": "2030-01-01T00:00:00+00:00",
            "task": {
                "kind": "translation",
                "source_language": "en",
                "target_language": "pl",
                "instructions": "Translate exactly.",
                "result_contract": {"kind": "translation"},
                "no_remove_subtitles": False,
                "known_speakers": [],
                "glossary": {},
                "timing_context_mode": "full",
                "substantial_gap_ms": 2000,
                "unrelated": "must not leak",
            },
            "batch": {
                "id_namespace": "source_revision_cue",
                "source_revision_id": "revision-1",
                "cue_count": 1,
                "valid_cue_ids": [7],
                "cues": [
                    {
                        "cue_id": 7,
                        "text": "Hello",
                        "timing": {
                            "start_ms": 0,
                            "end_ms": 1000,
                            "unrelated": "must not leak",
                        },
                        "unrelated": "must not leak",
                    }
                ],
                "context": {
                    "previous_output": [
                        {"text": "Earlier", "unrelated": "must not leak"}
                    ],
                    "previous_source": [
                        {"text": "Earlier source", "unrelated": "must not leak"}
                    ],
                    "following_source": [{"text": "Later", "speaker": "S1"}],
                    "unrelated": "must not leak",
                },
                "unrelated": "must not leak",
            },
            "delegation": {
                "execution_mode": "parallel",
                "max_parallel_batches": 3,
                "wave_number": 1,
                "wave_batch_count": 3,
                "context_capsule": {
                    "overview": "Shared",
                    "notes": ["Keep this"],
                    "secret": "must not leak",
                },
            },
            "unrelated": "must not leak",
        }

    def submit_dispatch_batch(self, batch_id, **kwargs):
        self.calls.append(("submit", {"batch_id": batch_id, **kwargs}))
        return {"batch_id": batch_id, "run_id": "run-1", "status": "accepted"}


class DispatchHandlerTests(unittest.TestCase):
    def setUp(self):
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_strict_inputs_bound_lease_and_response_limits(self):
        with self.assertRaises(ValidationError):
            GetDispatchRunInput(run_id="run", extra="rejected")
        with self.assertRaises(ValidationError):
            ClaimDispatchBatchInput(
                run_id="run",
                lease_seconds=29,
                idempotency_key="claim:one",
            )
        with self.assertRaises(ValidationError):
            SubmitDispatchBatchInput(
                batch_id="batch",
                lease_token="lease",
                response_text="x" * 524_289,
                idempotency_key="submit:one",
            )
        self.assertNotIn("token", RenewDispatchBatchInput.model_fields)
        self.assertIn("lease_token", RenewDispatchBatchInput.model_fields)
        with self.assertRaises(ValidationError):
            SubmitDispatchBatchInput(
                batch_id="batch",
                lease_token="lease",
                idempotency_key="submit:empty",
            )
        with self.assertRaises(ValidationError):
            SubmitDispatchBatchInput(
                batch_id="batch",
                lease_token="lease",
                result={
                    "kind": "correction",
                    "operations": [{"action": "edit", "cue_ids": [1], "texts": []}],
                },
                idempotency_key="submit:shape",
            )
        with self.assertRaises(ValidationError):
            CreateDispatchRunInput(
                session_id="session-1",
                kind="translation",
                target_language="pl",
                glossary={"empty": "  "},
                idempotency_key="create:blank-glossary",
            )

    def test_submit_response_limit_is_measured_in_utf8_bytes(self):
        with self.assertRaises(ValueError):
            SubmitDispatchBatchInput(
                batch_id="batch",
                lease_token="lease",
                response_text="ą" * 300_000,
                idempotency_key="submit-key-123",
            )
        self.assertEqual(
            6_000,
            CreateDispatchRunInput(
                session_id="session",
                kind="correction",
                instructions="Keep cue boundaries.",
                idempotency_key="create:one",
            ).char_limit,
        )

    def test_create_and_submit_outcomes_point_to_next_loop_step(self):
        created = create_dispatch_run(
            self.runtime,
            CreateDispatchRunInput(
                session_id="session",
                kind="correction",
                instructions="Keep cue boundaries.",
                idempotency_key="create:one",
            ),
        )
        self.assertEqual("run-1", created.result["run_id"])
        create_call = self.application.calls[0][1]
        self.assertEqual("serial", create_call["execution_mode"])
        self.assertEqual(1, create_call["max_parallel_batches"])
        self.assertEqual(
            {
                "overview": "",
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            create_call["context_capsule"],
        )
        self.assertEqual(
            "pandrator_claim_dispatch_batch",
            created.next_actions[0].tool,
        )
        self.assertEqual("run-1", created.next_actions[0].arguments["run_id"])
        self.assertNotIn("instructions", created.result)

        submitted = submit_dispatch_batch(
            self.runtime,
            SubmitDispatchBatchInput(
                batch_id="batch-1",
                lease_token="lease-capability",
                result={"kind": "correction", "operations": []},
                idempotency_key="submit:one",
            ),
        )
        self.assertEqual("accepted", submitted.result["status"])
        submit_call = self.application.calls[-1][1]
        self.assertEqual(
            {
                "terminology": {},
                "entities": {},
                "style_rules": [],
                "decisions": [],
                "notes": [],
            },
            submit_call["context_delta"],
        )
        self.assertEqual(
            "pandrator_claim_dispatch_batch",
            submitted.next_actions[0].tool,
        )

    def test_claim_only_discloses_batch_content_and_completed_claim_gets_run(self):
        listed = list_dispatch_runs(
            self.runtime,
            ListDispatchRunsInput(session_id="session"),
        )
        self.assertNotIn("prompt", str(listed))
        inspected = get_dispatch_run(
            self.runtime,
            GetDispatchRunInput(run_id="run-1"),
        )
        self.assertNotIn("source_batch", str(inspected))

        claimed = claim_dispatch_batch(
            self.runtime,
            ClaimDispatchBatchInput(
                run_id="run-1",
                idempotency_key="claim:one",
            ),
        )
        self.assertEqual("lease-capability", claimed.result["lease_token"])
        self.assertEqual("Hello", claimed.result["batch"]["cues"][0]["text"])
        self.assertEqual(
            {"start_ms": 0, "end_ms": 1000},
            claimed.result["batch"]["cues"][0]["timing"],
        )
        self.assertNotIn("unrelated", claimed.result)
        self.assertNotIn("unrelated", claimed.result["batch"])
        self.assertEqual(
            {"text": "Earlier"},
            claimed.result["batch"]["context"]["previous_output"][0],
        )
        self.assertNotIn("unrelated", claimed.result["task"])
        self.assertEqual(
            [{"text": "Earlier source"}],
            claimed.result["batch"]["context"]["previous_source"],
        )
        self.assertEqual(
            {
                "execution_mode": "parallel",
                "max_parallel_batches": 3,
                "wave_number": 1,
                "wave_batch_count": 3,
                "context_capsule": {"overview": "Shared", "notes": ["Keep this"]},
            },
            claimed.result["delegation"],
        )
        self.assertNotIn("source_batch", claimed.result)
        self.assertNotIn("prompt", claimed.result)

        self.application.claim_dispatch_batch = lambda run_id, **kwargs: {
            "run_id": run_id,
            "batch_id": "batch-1",
            "status": "completed",
            "run_status": "running",
            "batch_status": "completed",
        }
        accepted_replay = claim_dispatch_batch(
            self.runtime,
            ClaimDispatchBatchInput(
                run_id="run-1",
                idempotency_key="claim:two",
            ),
        )
        self.assertEqual(
            "pandrator_claim_dispatch_batch",
            accepted_replay.next_actions[0].tool,
        )

        self.application.claim_dispatch_batch = lambda run_id, **kwargs: {
            "run_id": run_id,
            "status": "completed",
            "run_status": "completed",
            "batch_status": "completed",
            "final_artifact_id": "artifact-1",
        }
        completed = claim_dispatch_batch(
            self.runtime,
            ClaimDispatchBatchInput(
                run_id="run-1",
                idempotency_key="claim:three",
            ),
        )
        self.assertEqual(
            "pandrator_get_dispatch_run",
            completed.next_actions[0].tool,
        )


if __name__ == "__main__":
    unittest.main()
