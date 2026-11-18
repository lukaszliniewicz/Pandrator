"""Compact MCP receipts: media-edit plans, speech-plan revisions, plans, projects."""

import unittest
from types import SimpleNamespace

from pandrator_mcp.schemas.e2e import PlanExportVariantInput
from pandrator_mcp.schemas.generation import (
    ReviseSpeechBlockPlanBatchInput,
    ReviseSpeechBlockPlanInput,
)
from pandrator_mcp.schemas.media_edit import (
    MediaEditKeepRange,
    PrepareMediaEditArguments,
    UpdateMediaEditArguments,
)
from pandrator_mcp.schemas.session_branches import GetTranslationProjectInput
from pandrator_mcp.schemas.workflow import ExecuteWorkflowPlanInput, PlanWorkflowInput
from pandrator_mcp.tools.e2e import plan_export_variant
from pandrator_mcp.tools.generation import (
    revise_speech_block_plan,
    revise_speech_block_plan_batch,
)
from pandrator_mcp.tools.media_edit import get_media_edit, prepare_media_edit, update_media_edit
from pandrator_mcp.tools.media_edit_dispatch import _claim
from pandrator_mcp.tools.session_branches import get_translation_project
from pandrator_mcp.tools.workflow import execute_workflow_plan, plan_workflow
from pandrator_mcp.workflow_results import _plan_summary


def _cue(index: int) -> dict:
    return {
        "id": f"cue-{index:04d}",
        "start_ms": index * 1_000,
        "end_ms": index * 1_000 + 900,
        "text": f"Spoken line number {index}.",
        "words": [
            {"text": "Spoken", "start_ms": index * 1_000, "end_ms": index * 1_000 + 300},
            {"text": f"line{index}", "start_ms": index * 1_000 + 300, "end_ms": index * 1_000 + 900},
        ],
    }


def _media_edit_state(cue_count: int, range_count: int) -> dict:
    return {
        "session_id": "session-1",
        "workflow_kind": "media_edit",
        "readiness": {"ready": True},
        "plan": {
            "revision": 4,
            "cues": [_cue(index) for index in range(cue_count)],
            "evidence": {"alignment_coverage": 1.0},
            "instructions": "Remove housekeeping.",
            "keep_ranges": [
                {"id": f"keep-{index:06d}", "start_ms": index * 1_000, "end_ms": index * 1_000 + 400}
                for index in range(range_count)
            ],
            "timing_diagnostics": {"cue_count": cue_count},
            "content_hash": "hash",
        },
    }


class _Application:
    def __init__(self, *, state: dict, plan: dict | None = None) -> None:
        self.state = state
        self.plan = plan or {"plan_id": "plan-1", "plan_digest": "a" * 64}
        self.calls: list[tuple[str, object]] = []

    def get_media_edit(self, session_id):
        self.calls.append(("get", session_id))
        return self.state

    def prepare_media_edit(self, session_id, *, force=False, idempotency_key):
        self.calls.append(("prepare", (session_id, force, idempotency_key)))
        return self.state

    def update_media_edit(self, session_id, **kwargs):
        self.calls.append(("update", (session_id, kwargs)))
        return self.state

    def get_workflow(self, session_id):
        return {"stages": []}

    def create_workflow_plan(self, session_id, **kwargs):
        self.calls.append(("plan", kwargs))
        return self.plan

    def execute_workflow_plan(self, plan_id, *, plan_digest, accepted_confirmations, idempotency_key):
        self.calls.append(
            ("execute", (plan_id, plan_digest, list(accepted_confirmations), idempotency_key))
        )
        return {"job_id": "job-1", "status": "queued"}

    def revise_generation_plan_topology(self, session_id, **kwargs):
        self.calls.append(("revise", kwargs))
        return {
            "id": "plan-1",
            "plan_revision_id": "revision-4",
            "revision_number": 4,
            "parent_revision_id": "revision-3",
            "active_plan_revision_id": "revision-4",
            "lineage": {
                **{f"segment-{index}": [f"segment-{index}"] for index in range(1, 10)},
                "segment-10": ["segment-10-left", "segment-10-right"],
            },
            "unchanged_segment_ids": [f"segment-{index}" for index in range(1, 10)],
            "segment_ids": ["segment-10-left", "segment-10-right"],
            "affected_segment_ids": ["segment-10-left", "segment-10-right"],
            "is_draft": False,
            "preview": None,
            "review_required": {"performance": True, "changed_audio": 2},
            "carried_performance_plan_id": None,
            "operation_json": {"action": "split", "mapping": [{"new_segments": ["left", "right"]}]},
        }

    def revise_generation_plan_topology_batch(self, session_id, **kwargs):
        self.calls.append(("revise_batch", kwargs))
        return {
            "id": "plan-1",
            "plan_revision_id": "revision-5",
            "revision_number": 5,
            "parent_revision_id": "revision-4",
            "active_plan_revision_id": "revision-5",
            "lineage": {
                **{f"segment-{index}": [f"segment-{index}"] for index in range(1, 10)},
                "segment-10": ["segment-10-left", "segment-10-right"],
            },
            "unchanged_segment_ids": [f"segment-{index}" for index in range(1, 10)],
            "saved_ids": {f"segment-{index}": f"segment-{index}" for index in range(1, 11)},
            "affected_segment_ids": ["segment-10-left", "segment-10-right"],
            "segment_count": 10,
            "is_draft": False,
            "review_required": {"performance": True, "changed_audio": 2},
            "base_revision_id": "revision-4",
            "operation_count": 1,
            "revision_ids": ["revision-5"],
            "result_refs": {"r282.left": ["segment-10-left"], "r282.right": ["segment-10-right"]},
        }

    def get_translation_project(self, session_id):
        self.calls.append(("project", session_id))
        return {
            "project": {
                "id": "project-1",
                "name": "Khasi Hills",
                "source_session_id": "session-1",
                "source_language": "en",
                "checkpoint_artifact_id": "artifact-1",
                "revision": 3,
                "source_settings": {"tts": {"effective": {"voice": "A"}}},
                "source_status": {"source_changed": False, "reasons": []},
                "branches": [
                    {
                        "id": "branch-1",
                        "session_id": "session-de",
                        "name": "Khasi (de)",
                        "workflow_kind": "voiceover",
                        "target_language": "de",
                        "status": "active",
                        "translation_status": "completed",
                        "translation_artifact_id": "artifact-de",
                        "active_translation_run_id": None,
                        "settings": {"tts": {"effective": {"voice": "B"}}},
                        "readiness": {
                            "translation": {"status": "completed", "reasons": [], "progress": 1.0},
                            "review": {"status": "completed", "reasons": [], "artifact_id": "artifact-de"},
                            "speech_plan": {
                                "status": "completed",
                                "reasons": [],
                                "revision_id": "plan-revision-de",
                            },
                            "voice": {"status": "ready", "reasons": []},
                            "generation": {
                                "status": "running",
                                "reasons": ["A job is queued."],
                                "generation_run_id": "run-de",
                            },
                            "exports": {"subtitles": {"status": "ready", "reasons": []}},
                        },
                    }
                ],
            },
            "setup": {"large": "settings document"},
            "setup_state": "active",
        }


class MediaEditSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = _Application(state=_media_edit_state(300, 5))
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_prepare_summary_drops_cues_and_words(self):
        summary = prepare_media_edit(
            self.runtime,
            PrepareMediaEditArguments(
                session_id="session-1", idempotency_key="media:prepare:summary"
            ),
        )

        self.assertEqual("summary", summary["view"])
        self.assertEqual(300, summary["plan"]["cue_count"])
        self.assertEqual(5, summary["plan"]["keep_range_count"])
        self.assertNotIn("cues", summary["plan"])
        self.assertNotIn("evidence", summary["plan"])
        self.assertNotIn("keep_ranges", summary["plan"])
        self.assertIn("plan.cues", summary["omitted"])
        self.assertIn("plan.evidence", summary["omitted"])

    def test_full_view_is_todays_payload(self):
        full = prepare_media_edit(
            self.runtime,
            PrepareMediaEditArguments(
                session_id="session-1",
                idempotency_key="media:prepare:full",
                view="full",
            ),
        )

        self.assertIs(self.application.state, full)
        self.assertEqual(300, len(full["plan"]["cues"]))

    def test_get_summary_keeps_counts_and_says_what_it_left_out(self):
        from pandrator_mcp.schemas.media_edit import GetMediaEditArguments

        summary = get_media_edit(
            self.runtime, GetMediaEditArguments(session_id="session-1")
        )

        self.assertEqual("summary", summary["view"])
        self.assertEqual(300, summary["plan"]["cue_count"])
        self.assertEqual(5, summary["plan"]["keep_range_count"])
        self.assertEqual(2_000, summary["plan"]["kept_duration_ms"])
        self.assertNotIn("cues", summary["plan"])
        self.assertNotIn("words", summary["plan"])

    def test_update_summary_echoes_the_ranges_the_caller_just_set(self):
        application = _Application(state=_media_edit_state(300, 2))
        runtime = SimpleNamespace(require_application=lambda: application)

        summary = update_media_edit(
            runtime,
            UpdateMediaEditArguments(
                session_id="session-1",
                expected_revision=4,
                keep_ranges=[
                    MediaEditKeepRange(start_ms=0, end_ms=1_000),
                    MediaEditKeepRange(start_ms=2_000, end_ms=3_000),
                ],
                idempotency_key="media:update:summary",
            ),
        )

        self.assertEqual(2, len(summary["plan"]["keep_ranges"]))
        self.assertEqual(800, summary["plan"]["kept_duration_ms"])
        self.assertEqual("summary", summary["view"])
        self.assertNotIn("cues", summary["plan"])

    def test_update_summary_replaces_more_than_fifty_ranges_with_a_count(self):
        application = _Application(state=_media_edit_state(4, 80))
        runtime = SimpleNamespace(require_application=lambda: application)

        summary = update_media_edit(
            runtime,
            UpdateMediaEditArguments(
                session_id="session-1",
                expected_revision=4,
                keep_ranges=[MediaEditKeepRange(start_ms=0, end_ms=1_000)],
                idempotency_key="media:update:many",
            ),
        )

        self.assertNotIn("keep_ranges", summary["plan"])
        self.assertEqual(80, summary["plan"]["keep_range_count"])
        self.assertIn("plan.keep_ranges", summary["omitted"])


class MediaEditClaimTests(unittest.TestCase):
    def test_claim_projection_never_carries_word_arrays(self):
        projected = _claim(
            {
                "run_id": "run-1",
                "batch_id": "batch-1",
                "batch": {
                    "duration_ms": 5_000,
                    "cues": [{"id": "cue-1", "text": "Hello", "words": [{"text": "Hello"}]}],
                },
            }
        )
        self.assertEqual(1, len(projected["batch"]["cues"]))

    def test_backend_claim_evidence_omits_word_arrays(self):
        from pandrator.web.media_edit_dispatch import MediaEditDispatchRunService
        from pandrator.web.models import MediaEditPlanRevision

        row = MediaEditPlanRevision(
            plan_id="plan-1",
            revision_number=1,
            content_hash="hash",
            duration_ms=60_000,
            cues_json=[
                {
                    "id": "cue-1",
                    "start_ms": 0,
                    "end_ms": 1_000,
                    "text": "Hello",
                    "words": [{"text": "Hello", "start_ms": 0, "end_ms": 400}],
                }
            ],
            keep_ranges_json=[],
            evidence_json={},
            operation_json={},
        )

        cues = MediaEditDispatchRunService._cue_evidence(row)

        self.assertEqual(["cue-1"], [cue["id"] for cue in cues])
        self.assertNotIn("words", cues[0])


class RevisionReceiptSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = _Application(state=_media_edit_state(1, 1))
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_split_receipt_keeps_only_the_changed_block(self):
        outcome = revise_speech_block_plan(
            self.runtime,
            ReviseSpeechBlockPlanInput(
                session_id="session-1",
                expected_revision_id="revision-3",
                action="split",
                segment_id="segment-10",
                cursor=4,
                text_layer="display",
                idempotency_key="topology:summary:split",
            ),
        )

        receipt = outcome.result
        self.assertEqual(
            {"segment-10": ["segment-10-left", "segment-10-right"]},
            receipt["lineage"],
        )
        self.assertEqual(9, receipt["unchanged_count"])
        self.assertNotIn("segment_ids", receipt)
        self.assertEqual(2, receipt["segment_count"])
        self.assertEqual("revision-4", receipt["plan_revision_id"])
        self.assertEqual("plan-1", receipt["id"])
        self.assertEqual({"performance": True, "changed_audio": 2}, receipt["review_required"])
        self.assertEqual("summary", receipt["view"])
        self.assertIn("unchanged_segment_ids", receipt["omitted"])

    def test_full_receipt_is_todays_payload(self):
        outcome = revise_speech_block_plan(
            self.runtime,
            ReviseSpeechBlockPlanInput(
                session_id="session-1",
                expected_revision_id="revision-3",
                action="split",
                segment_id="segment-10",
                cursor=4,
                text_layer="display",
                idempotency_key="topology:full:split",
                view="full",
            ),
        )

        self.assertEqual(10, len(outcome.result["lineage"]))
        self.assertEqual(
            ["segment-10-left", "segment-10-right"], outcome.result["segment_ids"]
        )

    def test_batch_receipt_keeps_named_result_references(self):
        outcome = revise_speech_block_plan_batch(
            self.runtime,
            ReviseSpeechBlockPlanBatchInput(
                session_id="session-1",
                expected_revision_id="revision-4",
                operations=[
                    {"action": "split", "segment_id": "segment-10", "cursor": 3, "label": "r282"}
                ],
                idempotency_key="topology:summary:batch",
            ),
        )

        receipt = outcome.result
        self.assertEqual(
            {
                "r282.left": ["segment-10-left"],
                "r282.right": ["segment-10-right"],
            },
            receipt["result_refs"],
        )
        self.assertEqual(1, len(receipt["lineage"]))
        self.assertEqual(9, receipt["unchanged_count"])
        self.assertEqual(10, receipt["saved_id_count"])
        self.assertNotIn("saved_ids", receipt)
        self.assertEqual("revision-5", receipt["plan_revision_id"])


class WorkflowPlanSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        plan = {
            "plan_id": "plan-1",
            "plan_digest": "a" * 64,
            "settings": {
                "hash": "settings-hash",
                "effective": {"export_mode": {"effective": "subtitles"}},
                "resolved_snapshot": {"output": {"big": ["x"] * 500}},
            },
            "ordered_steps": [{"stage": "export", "decision": "run"}],
            "warnings": ["Reused stage."],
            "speech_timing": {
                "assembly_id": "assembly-1",
                "assembly_duration_ms": 2_000,
                "timing_drift": {"worst_block_lag_ms": 14_500, "blocks": 522},
                "tail": {"checked": True, "outcome": "fits"},
            },
            "required_confirmations": ["provider"],
            "expires_at": "2026-11-18T12:00:00+00:00",
        }
        self.application = _Application(state=_media_edit_state(1, 1), plan=plan)
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_summary_drops_the_resolved_snapshot_only(self):
        outcome = plan_workflow(
            self.runtime,
            PlanWorkflowInput(session_id="session-1", target_stage="export"),
        )

        plan = outcome.result
        self.assertEqual("a" * 64, plan["plan_digest"])
        self.assertEqual("plan-1", plan["plan_id"])
        self.assertNotIn("resolved_snapshot", plan["settings"])
        self.assertEqual(
            {"export_mode": {"effective": "subtitles"}}, plan["settings"]["effective"]
        )
        self.assertEqual(1, len(plan["ordered_steps"]))
        self.assertEqual(["provider"], plan["required_confirmations"])
        self.assertEqual("2026-11-18T12:00:00+00:00", plan["expires_at"])
        self.assertEqual("summary", plan["view"])
        self.assertEqual(["settings.resolved_snapshot"], plan["omitted"])
        # The timing figures are aggregates, so the compact view keeps them.
        self.assertEqual(522, plan["speech_timing"]["timing_drift"]["blocks"])
        self.assertEqual(
            "pandrator_execute_workflow_plan", outcome.next_actions[0].tool
        )

    def test_full_view_keeps_the_snapshot(self):
        outcome = plan_export_variant(
            self.runtime,
            PlanExportVariantInput(
                session_id="session-1", export_mode="subtitles", view="full"
            ),
        )
        self.assertIn("resolved_snapshot", outcome.result["settings"])
        self.assertEqual("a" * 64, outcome.next_actions[0].arguments["plan_digest"])

    def test_summary_plan_executes_with_the_same_digest(self):
        plan = plan_workflow(
            self.runtime,
            PlanWorkflowInput(session_id="session-1", target_stage="export"),
        )
        action = plan.next_actions[0]

        executed = execute_workflow_plan(
            self.runtime,
            ExecuteWorkflowPlanInput(
                plan_id=action.arguments["plan_id"],
                plan_digest=action.arguments["plan_digest"],
                accepted_confirmations=action.arguments["accepted_confirmations"],
                idempotency_key=action.arguments["idempotency_key"],
            ),
        )

        self.assertEqual("job-1", executed.work.id)
        self.assertEqual(
            ("plan-1", "a" * 64, ["provider"], "workflow-plan:plan-1"),
            self.application.calls[-1][1],
        )


class TranslationProjectSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.application = _Application(state=_media_edit_state(1, 1))
        self.runtime = SimpleNamespace(require_application=lambda: self.application)

    def test_summary_reduces_branches_to_identity_readiness_and_ids(self):
        outcome = get_translation_project(
            self.runtime, GetTranslationProjectInput(session_id="session-1")
        )

        project = outcome.result["project"]
        self.assertEqual("project-1", project["id"])
        self.assertEqual("en", project["source_language"])
        self.assertEqual(1, project["branch_count"])
        self.assertNotIn("source_settings", project)
        self.assertNotIn("setup", outcome.result)
        self.assertFalse(project["source_changed"])
        branch = project["branches"][0]
        self.assertEqual("branch-1", branch["id"])
        self.assertEqual("session-de", branch["session_id"])
        self.assertEqual("de", branch["target_language"])
        self.assertNotIn("settings", branch)
        self.assertEqual("completed", branch["readiness"]["review"]["status"])
        self.assertEqual(["A job is queued."], branch["readiness"]["generation"]["reasons"])
        self.assertEqual("ready", branch["readiness"]["exports"]["subtitles"]["status"])
        self.assertEqual("artifact-de", branch["subtitle_artifact_id"])
        self.assertEqual("plan-revision-de", branch["speech_plan_revision_id"])
        self.assertEqual("run-de", branch["generation_run_id"])
        self.assertEqual("summary", outcome.result["view"])
        self.assertIn("project.branches[].settings", outcome.result["omitted"])

    def test_full_view_is_todays_payload(self):
        outcome = get_translation_project(
            self.runtime,
            GetTranslationProjectInput(session_id="session-1", view="full"),
        )

        branch = outcome.result["project"]["branches"][0]
        self.assertIn("settings", branch)
        self.assertIn("setup", outcome.result)


class PlanSummaryProjectionTests(unittest.TestCase):
    def test_projection_is_a_value_copy(self):
        plan = {"settings": {"resolved_snapshot": {"a": 1}}}
        summary = _plan_summary(plan)
        self.assertEqual({"resolved_snapshot": {"a": 1}}, plan["settings"])
        self.assertEqual({}, summary["settings"])


if __name__ == "__main__":
    unittest.main()
