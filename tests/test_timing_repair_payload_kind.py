"""Focused serializer regression: timing-repair payload kind/reason fields."""

from __future__ import annotations

import unittest
from datetime import datetime, timezone
from types import SimpleNamespace

from pandrator.web.generation_run_history import GenerationRunHistory
from pandrator.web.workspace import GenerationService


def _child(run_id: str, sequence_number: int, status: str = "completed", *, job_id: str | None = None):
    return SimpleNamespace(
        id=run_id,
        plan_revision_id=f"plan-{run_id}",
        source_generation_run_id="root",
        output_generation_run_id=None,
        operation="generate",
        status=status,
        sequence_number=sequence_number,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        settings_snapshot_json={},
        job_id=job_id,
    )


def _root(snapshot: dict | None = None):
    return SimpleNamespace(
        id="root",
        plan_revision_id="plan-root",
        source_generation_run_id=None,
        output_generation_run_id=None,
        operation="generate",
        status="completed",
        sequence_number=1,
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        settings_snapshot_json=snapshot or {},
    )


def _payload(root, children, operations, *, root_job=None, repair_job=None):
    history = GenerationRunHistory(
        root=root,
        repair_children=tuple(children),
        result=root,
        repair_operations=dict(operations),
        applied_children=tuple(
            child
            for child in children
            if operations[str(child.id)].get("repair_status") == "applied"
        ),
    )
    stub = SimpleNamespace(
        _timing_repair_status=GenerationService._timing_repair_status,
        _logical_usage_events=GenerationService._logical_usage_events,
    )
    context = {
        "runs": [root, *children],
        "usage_by_run_id": {},
        "workflow_kind": "voiceover",
    }
    summary, _active = GenerationService._timing_repair_payload(
        stub, context, history, root, root_job, repair_job=repair_job
    )
    return summary


def _operations(children, reason: str, repair_status: str = "applied"):
    return {
        str(child.id): {
            "reason": reason,
            "source_generation_run_id": "root",
            "repair_status": repair_status,
            "repair_reason": None,
        }
        for child in children
    }


class TimingRepairPayloadKindTests(unittest.TestCase):
    def test_regroup_versions_carry_reason_and_kind(self):
        root = _root()
        children = [_child("g1", 2), _child("g2", 3)]
        summary = _payload(root, children, _operations(children, "passage_regroup"))
        self.assertEqual("regroup", summary["kind"])
        self.assertEqual(
            ["passage_regroup", "passage_regroup"],
            [version["reason"] for version in summary["versions"]],
        )

    def test_repair_versions_carry_reason_and_kind(self):
        root = _root()
        children = [_child("r1", 2)]
        summary = _payload(
            root, children, _operations(children, "early_timing_repair")
        )
        self.assertEqual("repair", summary["kind"])
        self.assertEqual("early_timing_repair", summary["versions"][0]["reason"])

    def test_unverified_semantic_context_is_skipped_not_successful_check(self):
        root = _root({
            "tts": {
                "speech_block_generation_mode": "passage",
                "speech_block_early_repair_enabled": True,
            },
        })
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 0,
                "repair_rejection_reasons": {"missing_source_semantic_context": 1},
            },
        )
        summary = _payload(root, [], {}, root_job=job)
        self.assertEqual("skipped", summary["status"])
        self.assertEqual("missing_source_semantic_context", summary["reason"])
        self.assertEqual(0, summary["applied_count"])

    def test_no_eligible_passages_is_a_completed_check(self):
        root = _root({
            "tts": {
                "speech_block_generation_mode": "passage",
                "speech_block_early_repair_enabled": True,
            },
        })
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 4,
                "repair_candidate_blocks": 0,
                "repair_rejection_reasons": {},
                "repair_completed": True,
            },
        )
        summary = _payload(root, [], {}, root_job=job)
        self.assertEqual("no_effect", summary["status"])
        self.assertTrue(summary["no_effect"])
        self.assertEqual(4, summary["checked_count"])
        self.assertEqual(0, summary["candidate_count"])
        self.assertIsNone(summary["reason"])

    def test_rejected_attempt_is_not_reported_as_no_effect(self):
        root = _root()
        children = [_child("r1", 2), _child("r2", 3)]
        operations = {
            "r1": {
                "reason": "early_timing_repair",
                "repair_status": "not_applied",
                "repair_reason": "added_delay",
            },
            "r2": {
                "reason": "early_timing_repair",
                "repair_status": "not_applied",
                "repair_reason": "missed_repair_anchor",
            },
        }
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 5,
                "repair_candidate_blocks": 2,
                "repair_attempted_blocks": 2,
                "repair_completed": True,
                "repair_rejection_reasons": {
                    "added_delay": 1,
                    "missed_repair_anchor": 1,
                },
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("rejected", summary["status"])
        self.assertFalse(summary["no_effect"])
        self.assertEqual(2, summary["candidate_count"])
        self.assertEqual(2, summary["attempted_count"])

    def test_older_receipt_with_rejected_attempt_is_not_no_effect(self):
        root = _root()
        children = [_child("r1", 2)]
        operations = {
            "r1": {
                "reason": "early_timing_repair",
                "repair_status": "not_applied",
                "repair_reason": "added_delay",
            },
        }
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 4,
                "repair_rejection_reasons": {"added_delay": 1},
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("rejected", summary["status"])
        self.assertFalse(summary["no_effect"])
        self.assertEqual(1, summary["attempted_count"])

    def test_incomplete_timing_inspection_is_partial(self):
        root = _root({
            "tts": {
                "speech_block_generation_mode": "legacy",
                "speech_block_early_repair_enabled": True,
            },
        })
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 2,
                "repair_planned_blocks": 5,
                "repair_completed": False,
                "repair_inspection_failed": True,
                "repair_rejection_reasons": {"timing_inspection_failed": 1},
            },
        )
        summary = _payload(root, [], {}, root_job=job)
        self.assertEqual("partial", summary["status"])
        self.assertFalse(summary["pass_completed"])
        self.assertEqual(2, summary["checked_count"])

    def test_applied_and_rejected_attempts_report_partial(self):
        root = _root()
        children = [_child("r1", 2), _child("r2", 3, status="completed")]
        operations = {
            "r1": {"reason": "early_timing_repair", "repair_status": "applied"},
            "r2": {
                "reason": "early_timing_repair",
                "repair_status": "not_applied",
                "repair_reason": "added_delay",
            },
        }
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 1,
                "repair_checked_blocks": 3,
                "repair_candidate_blocks": 2,
                "repair_attempted_blocks": 2,
                "repair_completed": True,
                "repair_rejection_reasons": {"added_delay": 1},
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("partial", summary["status"])
        self.assertEqual(1, summary["applied_count"])
        self.assertEqual(2, summary["attempted_count"])

    def test_fully_applied_pass_is_completed(self):
        root = _root()
        children = [_child("r1", 2)]
        operations = _operations(children, "early_timing_repair")
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 1,
                "repair_checked_blocks": 2,
                "repair_candidate_blocks": 1,
                "repair_attempted_blocks": 1,
                "repair_completed": True,
                "repair_rejection_reasons": {},
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("completed", summary["status"])
        self.assertFalse(summary["no_effect"])
        self.assertTrue(summary["pass_completed"])

    def test_post_run_repair_job_is_running_before_any_child_exists(self):
        root = _root()
        job = SimpleNamespace(
            id="repair-job",
            status="queued",
            progress=0.0,
            result_json=None,
            error_code=None,
            error_message=None,
        )
        summary = _payload(root, [], {}, repair_job=job)
        self.assertEqual("running", summary["status"])

    def test_post_run_repair_failure_is_reported_with_diagnostics(self):
        root = _root()
        job = SimpleNamespace(
            id="repair-job",
            status="failed",
            progress=0.4,
            result_json=None,
            error_code="synthetic_repair_failure",
            error_message="Synthetic repair failure.",
        )
        summary = _payload(root, [], {}, repair_job=job)
        self.assertEqual("failed", summary["status"])
        self.assertEqual("synthetic_repair_failure", summary["error_code"])
        self.assertEqual("Synthetic repair failure.", summary["error_message"])

    def test_post_run_repair_uses_only_children_from_the_current_job(self):
        root = _root()
        old = _child("old", 2, status="failed", job_id="old-repair-job")
        operations = {
            "old": {"reason": "early_timing_repair", "repair_status": "failed"}
        }
        job = SimpleNamespace(
            id="new-repair-job",
            status="succeeded",
            progress=1.0,
            result_json={
                "repaired_blocks": 0,
                "repair_checked_blocks": 3,
                "repair_rejection_reasons": {},
            },
            error_code=None,
            error_message=None,
        )
        summary = _payload(
            root, [old], operations, repair_job=job
        )
        self.assertEqual("no_effect", summary["status"])
        # The older failed attempt remains visible in the version list.
        self.assertEqual("failed", summary["versions"][0]["repair_status"])


    def test_legacy_duplicate_split_rows_are_not_a_partial_pass(self):
        """Two staged rows for one applied split must count as one attempt."""

        root = _root()
        children = [_child("r1", 2), _child("r2", 3)]
        operations = {
            "r1": {
                "reason": "early_timing_repair",
                "repair_status": "applied",
                "source_block_ordinal": 4,
            },
            "r2": {
                "reason": "early_timing_repair",
                "repair_status": "applied",
                "source_block_ordinal": 4,
            },
        }
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 1,
                "repair_checked_blocks": 4,
                "repair_candidate_blocks": 1,
                "repair_completed": True,
                "repair_rejection_reasons": {},
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("completed", summary["status"])
        self.assertEqual(1, summary["attempted_count"])

    def test_legacy_duplicate_split_rows_without_ordinals_stay_completed(self):
        root = _root()
        children = [_child("r1", 2), _child("r2", 3)]
        operations = _operations(children, "early_timing_repair")
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "repaired_blocks": 1,
                "repair_checked_blocks": 4,
                "repair_candidate_blocks": 1,
                "repair_completed": True,
                "repair_rejection_reasons": {},
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("completed", summary["status"])
        self.assertEqual(1, summary["attempted_count"])

    def test_rejected_only_regroup_is_rejected_not_completed(self):
        root = _root()
        children = [_child("g1", 2), _child("g2", 3)]
        operations = _operations(children, "passage_regroup", "not_applied")
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "regrouped_groups": 0,
                "regroup_attempted_groups": 2,
                "regroup_rejected": 2,
                "regroup_status": "completed",
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("regroup", summary["kind"])
        self.assertEqual("rejected", summary["status"])
        self.assertFalse(summary["no_effect"])

    def test_regroup_with_an_applied_group_stays_completed(self):
        root = _root()
        children = [_child("g1", 2), _child("g2", 3)]
        operations = {
            "g1": {"reason": "passage_regroup", "repair_status": "applied"},
            "g2": {"reason": "passage_regroup", "repair_status": "not_applied"},
        }
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "regrouped_groups": 1,
                "regroup_attempted_groups": 2,
                "regroup_rejected": 1,
                "regroup_status": "completed",
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("completed", summary["status"])
        self.assertEqual(1, summary["applied_count"])

    def test_regroup_without_eligible_groups_is_no_effect(self):
        root = _root(
            {"tts": {"speech_block_generation_mode": "passage",
                     "speech_block_regroup_enabled": True}}
        )
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "regrouped_groups": 0,
                "regroup_attempted_groups": 0,
                "regroup_status": "no_eligible_groups",
            },
        )
        summary = _payload(root, [], {}, root_job=job)
        self.assertEqual("regroup", summary["kind"])
        self.assertEqual("no_effect", summary["status"])
        self.assertTrue(summary["no_effect"])

    def test_interrupted_regroup_with_no_merged_group_is_partial(self):
        root = _root(
            {"tts": {"speech_block_generation_mode": "passage",
                     "speech_block_regroup_enabled": True}}
        )
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "regrouped_groups": 0,
                "regroup_attempted_groups": 2,
                "regroup_rejected": 0,
                "regroup_status": "completed",
                "regroup_completed": False,
            },
        )
        summary = _payload(root, [], {}, root_job=job)
        self.assertEqual("regroup", summary["kind"])
        self.assertEqual("partial", summary["status"])
        self.assertFalse(summary["pass_completed"])
        self.assertFalse(summary["no_effect"])

    def test_interrupted_regroup_after_an_applied_group_is_partial(self):
        root = _root()
        children = [_child("g1", 2)]
        operations = _operations(children, "passage_regroup")
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "regrouped_groups": 1,
                "regroup_attempted_groups": 2,
                "regroup_rejected": 0,
                "regroup_status": "completed",
                "regroup_completed": False,
            },
        )
        summary = _payload(root, children, operations, root_job=job)
        self.assertEqual("partial", summary["status"])
        self.assertFalse(summary["pass_completed"])
        self.assertEqual(1, summary["applied_count"])

    def test_skipped_regroup_projects_skipped_with_its_reason(self):
        cases = {
            "skipped_active_plan_changed": "active_plan_changed",
            "skipped_first_pass_evidence_missing": "first_pass_evidence_missing",
            "skipped_not_passage_mode": "not_passage_mode",
        }
        for status, reason in cases.items():
            with self.subTest(status=status):
                job = SimpleNamespace(
                    status="succeeded",
                    result_json={
                        "regrouped_groups": 0,
                        "regroup_attempted_groups": 0,
                        "regroup_status": status,
                        "regroup_reason": reason,
                    },
                )
                summary = _payload(
                    _root(
                        {"tts": {"speech_block_generation_mode": "passage",
                                 "speech_block_regroup_enabled": True}}
                    ),
                    [],
                    {},
                    root_job=job,
                )
                self.assertEqual("regroup", summary["kind"])
                self.assertEqual("skipped", summary["status"])
                self.assertEqual(reason, summary["reason"])
                self.assertFalse(summary["no_effect"])

    def test_replayed_regroup_attempt_is_not_a_skipped_check(self):
        job = SimpleNamespace(
            status="succeeded",
            result_json={
                "regrouped_groups": 0,
                "regroup_attempted_groups": 0,
                "regroup_status": "already_attempted",
            },
        )
        summary = _payload(_root(), [], {}, root_job=job)
        self.assertEqual("completed", summary["status"])
        self.assertIsNone(summary["reason"])

    def test_mixed_reasons_yield_mixed_kind(self):
        root = _root()
        children = [_child("g1", 2), _child("r1", 3)]
        operations = {
            "g1": {"reason": "passage_regroup", "repair_status": "applied"},
            "r1": {"reason": "early_timing_repair", "repair_status": "applied"},
        }
        summary = _payload(root, children, operations)
        self.assertEqual("mixed", summary["kind"])

    def test_zero_children_falls_back_to_repair_snapshot(self):
        root = _root(
            {"tts": {"speech_block_generation_mode": "legacy",
                      "speech_block_early_repair_enabled": True}}
        )
        summary = _payload(root, [], {})
        self.assertEqual("repair", summary["kind"])
        self.assertEqual([], summary["versions"])

    def test_zero_children_falls_back_to_regroup_snapshot(self):
        root = _root(
            {"tts": {"speech_block_generation_mode": "passage",
                      "speech_block_regroup_enabled": True}}
        )
        summary = _payload(root, [], {})
        self.assertEqual("regroup", summary["kind"])

    def test_zero_children_without_second_pass_is_unknown(self):
        summary = _payload(_root(), [], {})
        self.assertEqual("unknown", summary["kind"])

    def test_children_with_missing_reasons_are_unknown_not_inferred(self):
        root = _root(
            {"tts": {"speech_block_generation_mode": "passage",
                      "speech_block_regroup_enabled": True}}
        )
        children = [_child("g1", 2)]
        operations = {"g1": {"repair_status": "applied"}}
        summary = _payload(root, children, operations)
        self.assertEqual("unknown", summary["kind"])


if __name__ == "__main__":
    unittest.main()
