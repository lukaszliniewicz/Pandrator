import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.schemas import (
    ClaimSpeechOptimizationDispatchBatchInput,
    CreateSpeechOptimizationDispatchRunInput,
    GetSpeechOptimizationDispatchRunInput,
    SubmitSpeechOptimizationDispatchBatchInput,
)
from pandrator_mcp.tools.speech_optimization_dispatch import (
    claim_speech_optimization_dispatch_batch,
    create_speech_optimization_dispatch_run,
    get_speech_optimization_dispatch_run,
    submit_speech_optimization_dispatch_batch,
)


class _Application:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []
        self.run_status = "ready"
        self.submit_status = "running"

    def create_speech_optimization_dispatch_run(self, session_id, **kwargs):
        self.calls.append(("create", {"session_id": session_id, **kwargs}))
        return {
            "id": "run-1",
            "session_id": session_id,
            "kind": "speech_optimization",
            "output_role": "tts_optimized",
            "source_format": "json",
            "language": "en",
            "status": "ready",
            "batch_count": 2,
            "completed_batch_count": 0,
            "execution_mode": "parallel",
            "max_parallel_batches": 3,
            "settings_json": {"private": True},
        }

    def get_speech_optimization_dispatch_run(self, run_id):
        self.calls.append(("get", {"run_id": run_id}))
        return {
            "id": run_id,
            "session_id": "session-1",
            "status": self.run_status,
            "batch_count": 2,
            "completed_batch_count": 0,
            "batches": [
                {
                    "id": "batch-1",
                    "batch_ordinal": 1,
                    "status": "ready",
                    "input_json": {"private": True},
                }
            ],
            "private_text": "must not leak",
        }

    def claim_speech_optimization_dispatch_batch(self, run_id, **kwargs):
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
                "kind": "speech_optimization",
                "output_role": "tts_optimized",
                "instructions": "Optimize every unit exactly once.",
            },
            "batch": {
                "valid_unit_ids": [1, 2],
                "units": [
                    {"unit_id": 1, "text": "Dr. Jones", "language": "en"},
                    {"unit_id": 2, "text": "Room 101", "language": "en"},
                ],
                "context": {
                    "previous_output": [],
                    "previous_source": [
                        {"text": "Before", "language": "en", "private": True}
                    ],
                    "following_source": [],
                },
            },
            "delegation": {
                "execution_mode": "parallel",
                "max_parallel_batches": 3,
                "wave_number": 1,
                "wave_batch_count": 2,
                "context_capsule": {
                    "overview": "Shared",
                    "entities": {"Alice": "narrator"},
                    "private": True,
                },
            },
            "unrelated": "must not leak",
        }

    def submit_speech_optimization_dispatch_batch(self, batch_id, **kwargs):
        self.calls.append(("submit", {"batch_id": batch_id, **kwargs}))
        return {
            "run_id": "run-1",
            "batch_id": batch_id,
            "output_role": "tts_optimized",
            "status": self.submit_status,
            "run_status": self.submit_status,
            "batch_status": "completed",
            "accepted": True,
            "completed_batch_count": 1,
            "completed_batches": 1,
            "batch_count": 2,
            "total_batches": 2,
            "remaining_batches": 1,
            "finalized": False,
            "error_code": (
                "materialization_failed" if self.submit_status == "finalizing" else None
            ),
            "normalized_output_json": {"private": True},
        }


class SpeechOptimizationDispatchHandlerTests(unittest.TestCase):
    def setUp(self):
        self.application = _Application()
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_inputs_are_strict_and_transport_bounds_are_not_model_budgets(self):
        with self.assertRaises(ValidationError):
            GetSpeechOptimizationDispatchRunInput(run_id="run", extra="rejected")
        with self.assertRaises(ValidationError):
            CreateSpeechOptimizationDispatchRunInput(
                session_id="session-1",
                max_units_per_batch=501,
                idempotency_key="speech:create-1",
            )
        with self.assertRaises(ValidationError):
            CreateSpeechOptimizationDispatchRunInput(
                session_id="session-1",
                execution_mode="serial",
                max_parallel_batches=2,
                idempotency_key="speech:create-width",
            )
        configured = CreateSpeechOptimizationDispatchRunInput(
            session_id="session-1",
            char_limit=1_000_000,
            max_units_per_batch=500,
            execution_mode="parallel",
            max_parallel_batches=3,
            context_capsule={"overview": "Shared"},
            idempotency_key="speech:create-2",
        )
        self.assertEqual(1_000_000, configured.char_limit)
        self.assertEqual("parallel", configured.execution_mode)
        self.assertEqual(3, configured.max_parallel_batches)
        self.assertNotIn(
            "max_tokens", CreateSpeechOptimizationDispatchRunInput.model_fields
        )
        self.assertNotIn(
            "max_iterations", CreateSpeechOptimizationDispatchRunInput.model_fields
        )

    def test_create_get_and_claim_form_a_provider_free_sequential_loop(self):
        created = create_speech_optimization_dispatch_run(
            self.runtime,
            CreateSpeechOptimizationDispatchRunInput(
                session_id="session-1",
                tts_service="xtts",
                execution_mode="parallel",
                max_parallel_batches=3,
                context_capsule={"overview": "Shared"},
                idempotency_key="speech:create-3",
            ),
        )
        self.assertEqual("run-1", created.result["id"])
        self.assertNotIn("settings_json", created.result)
        create_call = self.application.calls[0]
        self.assertEqual("parallel", create_call[1]["execution_mode"])
        self.assertEqual(3, create_call[1]["max_parallel_batches"])
        self.assertEqual(
            "Shared",
            create_call[1]["context_capsule"]["overview"],
        )
        self.assertEqual(
            "pandrator_claim_speech_optimization_dispatch_batch",
            created.next_actions[0].tool,
        )

        fetched = get_speech_optimization_dispatch_run(
            self.runtime,
            GetSpeechOptimizationDispatchRunInput(run_id="run-1"),
        )
        self.assertNotIn("private_text", fetched.result)
        self.assertNotIn("input_json", fetched.result["batches"][0])
        self.assertEqual(
            "pandrator_claim_speech_optimization_dispatch_batch",
            fetched.next_actions[0].tool,
        )

        claimed = claim_speech_optimization_dispatch_batch(
            self.runtime,
            ClaimSpeechOptimizationDispatchBatchInput(
                run_id="run-1",
                idempotency_key="speech:claim-1",
            ),
        )
        self.assertEqual("lease-capability", claimed["lease_token"])
        self.assertEqual([1, 2], claimed["batch"]["valid_unit_ids"])
        self.assertEqual("parallel", claimed["delegation"]["execution_mode"])
        self.assertEqual(
            [{"text": "Before", "language": "en"}],
            claimed["batch"]["context"]["previous_source"],
        )
        self.assertNotIn("unrelated", claimed)

    def test_submit_preserves_typed_units_and_points_to_next_claim(self):
        submitted = submit_speech_optimization_dispatch_batch(
            self.runtime,
            SubmitSpeechOptimizationDispatchBatchInput(
                batch_id="batch-1",
                lease_token="lease-capability",
                result={
                    "kind": "speech_optimization",
                    "items": [
                        {"unit_id": 1, "text": "Doctor Jones"},
                        {"unit_id": 2, "text": "Room one oh one"},
                    ],
                },
                context_delta={"entities": {"Alice": "narrator"}},
                idempotency_key="speech:submit-1",
            ),
        )
        self.assertNotIn("normalized_output_json", submitted.result)
        self.assertEqual(
            "pandrator_claim_speech_optimization_dispatch_batch",
            submitted.next_actions[0].tool,
        )
        call = self.application.calls[-1]
        self.assertEqual(2, call[1]["result"]["items"][1]["unit_id"])
        self.assertEqual(
            {"Alice": "narrator"},
            call[1]["context_delta"]["entities"],
        )

        self.application.submit_status = "finalizing"
        finalizing = submit_speech_optimization_dispatch_batch(
            self.runtime,
            SubmitSpeechOptimizationDispatchBatchInput(
                batch_id="batch-2",
                lease_token="lease-capability",
                result={
                    "kind": "speech_optimization",
                    "items": [{"unit_id": 3, "text": "Chapter Four"}],
                },
                idempotency_key="speech:submit-2",
            ),
        )
        self.assertEqual(
            "pandrator_get_speech_optimization_dispatch_run",
            finalizing.next_actions[0].tool,
        )

        self.application.run_status = "completed"
        completed = get_speech_optimization_dispatch_run(
            self.runtime,
            GetSpeechOptimizationDispatchRunInput(run_id="run-1"),
        )
        self.assertEqual("pandrator_get_workflow", completed.next_actions[0].tool)


if __name__ == "__main__":
    unittest.main()
