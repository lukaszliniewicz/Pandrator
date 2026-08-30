import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.schemas import (
    ClaimSourceCleaningDispatchBatchInput,
    CreateSourceCleaningDispatchRunInput,
    GetSourceCleaningDispatchRunInput,
    InspectSourceCleaningDispatchExtractionInput,
    SourceCleaningDispatchOperationInput,
    SubmitSourceCleaningDispatchBatchInput,
)
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools.source_cleaning_dispatch import (
    claim_source_cleaning_dispatch_batch,
    create_source_cleaning_dispatch_run,
    get_source_cleaning_dispatch_run,
    inspect_source_cleaning_dispatch_extraction,
    submit_source_cleaning_dispatch_batch,
)


class _Application:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.run_status = "ready"

    def create_source_cleaning_dispatch_run(self, session_id, **kwargs):
        self.calls.append(("create", {"session_id": session_id, **kwargs}))
        return {
            "id": "run-1",
            "run_id": "run-1",
            "session_id": session_id,
            "job_id": "job-1",
            "status": "preparing",
            "settings_json": {"private": True},
        }

    def get_source_cleaning_dispatch_run(self, run_id):
        self.calls.append(("get", {"run_id": run_id}))
        return {
            "id": run_id,
            "run_id": run_id,
            "session_id": "session-1",
            "status": self.run_status,
            "completed_batch_count": 0,
            "batches": [
                {
                    "id": "batch-1",
                    "batch_ordinal": 1,
                    "phase": "metadata",
                    "status": "ready",
                    "input_json": {"private": True},
                }
            ],
            "private_text": "must not leak",
        }

    def claim_source_cleaning_dispatch_batch(self, run_id, **kwargs):
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
            "task": {
                "kind": "source_cleaning",
                "phase": "metadata",
                "allowed_operation_types": ["set_metadata"],
            },
            "batch": {
                "phase": "metadata",
                "evidence": {
                    "candidate_blocks": [{"block_id": "block-1", "text": "A Test Book"}]
                },
                "proposals": [],
                "valid_block_ids": ["block-1"],
                "valid_metadata_keys": ["title"],
            },
            "unrelated": "must not leak",
        }

    def submit_source_cleaning_dispatch_batch(self, batch_id, **kwargs):
        self.calls.append(("submit", {"batch_id": batch_id, **kwargs}))
        return {
            "run_id": "run-1",
            "batch_id": batch_id,
            "output_role": "clean_text",
            "status": "running",
            "run_status": "running",
            "batch_status": "completed",
            "accepted": True,
            "completed_batch_count": 1,
            "completed_batches": 1,
            "batch_count": 6,
            "total_batches": 6,
            "remaining_batches": 5,
            "accepted_operation_count": 1,
            "rejected_proposal_count": 0,
            "finalized": False,
            "requires_review": False,
            "validation": {},
            "normalized_output_json": {"private": True},
        }

    def inspect_source_cleaning_dispatch_extraction(self, batch_id, **kwargs):
        self.calls.append(("inspect", {"batch_id": batch_id, **kwargs}))
        return {
            "run_id": "run-1",
            "batch_id": batch_id,
            "phase": "text_repair",
            "inspection_id": "inspection-1",
            "view": kwargs["view"],
            "action": kwargs["action"],
            "observation": [
                {"block_id": "block-9", "text": "A bro ken word."}
            ],
            "promoted_block_ids": ["block-9"],
            "baseline_only_block_ids": [],
            "valid_block_id_count": 9,
            "lease_expires_at": "2030-01-01T00:00:00+00:00",
        }


class SourceCleaningDispatchHandlerTests(unittest.TestCase):
    def setUp(self):
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_inputs_are_strict_and_use_evidence_not_token_budgets(self):
        with self.assertRaises(ValidationError):
            GetSourceCleaningDispatchRunInput(run_id="run", extra="rejected")
        with self.assertRaises(ValidationError):
            CreateSourceCleaningDispatchRunInput(
                session_id="session-1",
                evidence_limit=2_001,
                idempotency_key="source:create-1",
            )
        configured = CreateSourceCleaningDispatchRunInput(
            session_id="session-1",
            evidence_limit=2_000,
            idempotency_key="source:create-2",
        )
        self.assertEqual(2_000, configured.evidence_limit)
        self.assertNotIn(
            "max_tokens", CreateSourceCleaningDispatchRunInput.model_fields
        )
        self.assertNotIn(
            "max_iterations", CreateSourceCleaningDispatchRunInput.model_fields
        )
        self.assertEqual(
            8 * 1024 * 1024,
            McpSettings(target_name="local").maximum_response_bytes,
        )
        with self.assertRaises(ValidationError):
            SourceCleaningDispatchOperationInput(
                op="delete_blocks",
                block_ids=["block-1", "block-1"],
            )
        with self.assertRaises(ValidationError):
            ClaimSourceCleaningDispatchBatchInput(
                run_id="run-1",
                lease_seconds=29,
                idempotency_key="source:claim-1",
            )

    def test_create_get_and_claim_form_a_bounded_editorial_loop(self):
        created = create_source_cleaning_dispatch_run(
            self.runtime,
            CreateSourceCleaningDispatchRunInput(
                session_id="session-1",
                evidence_limit=2_000,
                idempotency_key="source:create-3",
            ),
        )
        self.assertEqual("run-1", created.result["run_id"])
        self.assertNotIn("settings_json", created.result)
        self.assertEqual(
            "pandrator_get_source_cleaning_dispatch_run",
            created.next_actions[0].tool,
        )

        fetched = get_source_cleaning_dispatch_run(
            self.runtime,
            GetSourceCleaningDispatchRunInput(run_id="run-1"),
        )
        self.assertNotIn("private_text", fetched.result)
        self.assertNotIn("input_json", fetched.result["batches"][0])
        self.assertEqual(
            "pandrator_claim_source_cleaning_dispatch_batch",
            fetched.next_actions[0].tool,
        )

        claimed = claim_source_cleaning_dispatch_batch(
            self.runtime,
            ClaimSourceCleaningDispatchBatchInput(
                run_id="run-1",
                idempotency_key="source:claim-2",
            ),
        )
        self.assertEqual("lease-capability", claimed["lease_token"])
        self.assertEqual("metadata", claimed["task"]["phase"])
        self.assertEqual(
            "A Test Book",
            claimed["batch"]["evidence"]["candidate_blocks"][0]["text"],
        )
        self.assertNotIn("unrelated", claimed)

    def test_submit_preserves_typed_operations_and_points_to_next_claim(self):
        submitted = submit_source_cleaning_dispatch_batch(
            self.runtime,
            SubmitSourceCleaningDispatchBatchInput(
                batch_id="batch-1",
                lease_token="lease-capability",
                result={
                    "kind": "source_cleaning",
                    "phase": "metadata",
                    "decisions": [],
                    "operations": [
                        {
                            "op": "set_metadata",
                            "metadata": {"title": "A Test Book"},
                            "reason": "Confirmed from evidence.",
                        }
                    ],
                    "summary": "Metadata reviewed.",
                    "confidence": 0.9,
                },
                idempotency_key="source:submit-1",
            ),
        )
        self.assertNotIn("normalized_output_json", submitted.result)
        self.assertEqual(
            "pandrator_claim_source_cleaning_dispatch_batch",
            submitted.next_actions[0].tool,
        )
        call = self.application.calls[-1]
        self.assertEqual("set_metadata", call[1]["result"]["operations"][0]["op"])

        self.application.run_status = "completed"
        completed = get_source_cleaning_dispatch_run(
            self.runtime,
            GetSourceCleaningDispatchRunInput(run_id="run-1"),
        )
        self.assertEqual("pandrator_get_workflow", completed.next_actions[0].tool)
        self.assertEqual("session-1", completed.next_actions[0].arguments["session_id"])

    def test_inspection_preserves_flexible_arguments_and_promoted_scope(self):
        inspected = inspect_source_cleaning_dispatch_extraction(
            self.runtime,
            InspectSourceCleaningDispatchExtractionInput(
                batch_id="batch-1",
                lease_token="lease-capability",
                action="batch",
                arguments={
                    "commands": [
                        {
                            "action": "search",
                            "arguments": {"query": "bro ken", "max_hits": 20},
                        },
                        {
                            "action": "preview",
                            "arguments": {"start_line": 1, "end_line": 80},
                        },
                    ]
                },
                view="baseline",
                idempotency_key="source:inspect-1",
            ),
        )

        self.assertEqual(["block-9"], inspected["promoted_block_ids"])
        self.assertEqual("batch", inspected["action"])
        call = self.application.calls[-1]
        self.assertEqual("baseline", call[1]["view"])
        self.assertEqual("search", call[1]["arguments"]["commands"][0]["action"])
