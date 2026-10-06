import unittest
from types import SimpleNamespace

from pydantic import ValidationError

from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.generation import (
    AssembleGenerationRunInput,
    GenerateSpeechPlanInput,
    ListGenerationSegmentsInput,
    RegenerateSegmentsInput,
    ReviseSpeechBlockPlanBatchInput,
    ReviseSpeechBlockPlanInput,
    SelectTakeInput,
    UpdateGenerationSegmentInput,
    UpdateGenerationSegmentsInput,
)
from pandrator_mcp.schemas.sessions import (
    CuePatchInput,
    ImportSubtitlesInput,
    ListSessionsInput,
    PatchSubtitleCuesInput,
    PreviewSubtitlesInput,
    ReplaceSubtitleTextInput,
)
from pandrator_mcp.tools.generation import (
    assemble_generation_run,
    generate_speech_plan,
    list_generation_segments,
    regenerate_segments,
    revise_speech_block_plan,
    revise_speech_block_plan_batch,
    select_take,
    update_generation_segment,
    update_generation_segments,
)
from pandrator_mcp.tools.sessions import (
    import_subtitles,
    list_sessions,
    patch_subtitle_cues,
    preview_subtitles,
    replace_subtitle_text,
)


class _FakeApplication:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.topology_batch_error: PandratorMcpError | None = None
        self.generation_run_error: PandratorMcpError | None = None

    def list_sessions(self, limit=50, query=None, include_trashed=False):
        self.calls.append(
            (
                "list_sessions",
                {
                    "limit": limit,
                    "query": query,
                    "include_trashed": include_trashed,
                },
            )
        )
        return {
            "items": [
                {
                    "id": "session-1",
                    "name": "Pascal Polish Lecture",
                    "workflow_kind": "subtitles",
                    "status": "ready",
                    "source_language": "pl",
                    "target_language": "en",
                },
                {
                    "id": "session-2",
                    "name": "Chemistry Chapter 1",
                    "workflow_kind": "audiobook",
                    "status": "ready",
                    "source_language": "en",
                    "target_language": None,
                },
            ]
        }

    def get_subtitles(self, session_id):
        self.calls.append(("get_subtitles", {"session_id": session_id}))
        return {
            "session_id": session_id,
            "stages": {
                "transcribe": {
                    "language": "pl",
                    "revision": 1,
                    "segments": [
                        {
                            "ordinal": 0,
                            "start_ms": 0,
                            "end_ms": 2500,
                            "speaker": "SPEAKER_1",
                            "text": "Dzień dobry wszystkim.",
                            "review_state": "uncertain",
                            "review_note": "Source name unclear",
                            "evidence_ids": ["original-evidence"],
                            "uncertain_source_cue_ids": [143],
                        },
                        {
                            "ordinal": 1,
                            "start_ms": 2500,
                            "end_ms": 5000,
                            "speaker": "SPEAKER_1",
                            "text": "Dzisiaj omówimy filozofię Pascala.",
                            "review_state": "clear",
                            "review_note": "",
                            "evidence_ids": [],
                            "uncertain_source_cue_ids": [],
                        },
                    ],
                },
                "translate": {
                    "language": "en",
                    "revision": 1,
                    "segments": [
                        {
                            "ordinal": 0,
                            "start_ms": 0,
                            "end_ms": 2500,
                            "speaker": "SPEAKER_1",
                            "text": "Good morning everyone.",
                        },
                        {
                            "ordinal": 1,
                            "start_ms": 2500,
                            "end_ms": 5000,
                            "speaker": "SPEAKER_1",
                            "text": "Today we will discuss Pascal's philosophy.",
                        },
                    ],
                },
            },
        }

    def review_subtitles(self, session_id, *, artifact_ids):
        self.calls.append(
            (
                "review_subtitles",
                {"session_id": session_id, "artifact_ids": artifact_ids},
            )
        )
        return {
            "columns": [
                {
                    "artifact_id": artifact_ids[0],
                    "stage": "transcribe",
                    "language": "pl",
                    "revision": 2,
                    "segments": [
                        {
                            "ordinal": 0,
                            "start_ms": 0,
                            "end_ms": 3000,
                            "speaker": "Narrator",
                            "text": "Dokładny tekst recenzji.",
                        }
                    ],
                }
            ]
        }

    def save_subtitle_review(
        self,
        session_id: str,
        stage: str,
        *,
        expected_revision: int,
        segments: list[dict],
        source_artifact_id: str | None = None,
        idempotency_key: str | None = None,
    ):
        self.calls.append(
            (
                "save_subtitle_review",
                {
                    "session_id": session_id,
                    "stage": stage,
                    "expected_revision": expected_revision,
                    "segments": segments,
                    "source_artifact_id": source_artifact_id,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "artifact_id": f"art-reviewed-{expected_revision + 1}",
            "document_id": "doc-1",
            "revision_id": f"rev-{expected_revision + 1}",
            "revision": expected_revision + 1,
        }

    def list_generation_segments(
        self, session_id, *, cursor=0, limit=50, generation_run_id=None
    ):
        self.calls.append(
            (
                "list_generation_segments",
                {
                    "session_id": session_id,
                    "cursor": cursor,
                    "limit": limit,
                    "generation_run_id": generation_run_id,
                },
            )
        )
        return {
            "items": [
                {
                    "id": "segment-1",
                    "ordinal": 1,
                    "revision": 3,
                    "status": "ready",
                    "start_ms": 0,
                    "end_ms": 2000,
                    "speaker": "Narrator",
                    "node_kind": "paragraph",
                    "source_segment_ids": ["source-1"],
                    "alignment_group": "a0001",
                    "text": "Original cue text",
                    "optimized_text": "Optimized spoken text",
                    "speech_block_provenance": {
                        "schema_version": 1,
                        "origin": "automatic",
                        "source_reference_namespace": "document_segment_id",
                        "source_cues": [
                            {
                                "reference": "source-1",
                                "start_ms": 0,
                                "end_ms": 2000,
                                "display_spans": [[0, 17]],
                                "speech_spans": [[0, 21]],
                            }
                        ],
                        "formation_events": [],
                        "boundary_before": {
                            "action": "keep_boundary",
                            "reason_code": "document_start",
                            "summary": "Speech block starts the document.",
                            "measurements": {},
                            "source_references": ["source-1"],
                        },
                        "risk_flags": [],
                    },
                    "speech_plan": {"version": 1, "status": "reviewed"},
                    "voice_id": "voice-pl-1",
                    "voice": "Marek",
                    "language": "pl",
                    "selected_take_id": "take-1",
                    "takes": [
                        {
                            "id": "take-1",
                            "take_number": 1,
                            "status": "completed",
                            "duration_ms": 1950,
                            "artifact_id": "artifact-take-1",
                            "created_at": "2026-09-03T07:00:00Z",
                        }
                    ],
                }
            ],
            "next_cursor": None,
            "total": 1,
            "plan_revision_id": "plan-revision-3",
            "plan_revision_number": 3,
            "parent_revision_id": "plan-revision-2",
            "operation_json": {"action": "split"},
            "speech_block_settings": {"speech_block_max_chars": 220},
        }

    def revise_generation_plan_topology(
        self,
        session_id,
        *,
        expected_revision_id,
        action,
        segment_id=None,
        cursor=None,
        text_layer=None,
        left_segment_id=None,
        right_segment_id=None,
        target_revision_id=None,
        idempotency_key=None,
    ):
        payload = {
            "session_id": session_id,
            "expected_revision_id": expected_revision_id,
            "action": action,
            "segment_id": segment_id,
            "cursor": cursor,
            "text_layer": text_layer,
            "left_segment_id": left_segment_id,
            "right_segment_id": right_segment_id,
            "target_revision_id": target_revision_id,
            "idempotency_key": idempotency_key,
        }
        self.calls.append(("revise_generation_plan_topology", payload))
        return {
            "plan_revision_id": "plan-revision-4",
            "parent_revision_id": expected_revision_id,
            "revision_number": 4,
            "operation": {"action": action},
            "segment_ids": ["segment-left", "segment-right"],
            "affected_segment_ids": ["segment-1"],
        }

    def revise_generation_plan_topology_batch(
        self, session_id, *, expected_revision_id, operations, idempotency_key
    ):
        self.calls.append(
            (
                "revise_generation_plan_topology_batch",
                {
                    "session_id": session_id,
                    "expected_revision_id": expected_revision_id,
                    "operations": operations,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        if self.topology_batch_error is not None:
            raise self.topology_batch_error
        return {"plan_revision_id": "plan-revision-4", "revision_number": 4}

    def update_generation_segment(
        self, segment_id, *, changes, expected_revision, idempotency_key
    ):
        self.calls.append(
            (
                "update_generation_segment",
                {
                    "segment_id": segment_id,
                    "changes": changes,
                    "expected_revision": expected_revision,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "id": segment_id,
            "ordinal": 1,
            "revision": expected_revision + 1,
            "status": "ready",
            "start_ms": 0,
            "end_ms": 2000,
            "speaker": "Narrator",
            "text": changes.get("text", "Original cue text"),
            "removed": changes.get("removed", False),
            "optimized_text": changes.get("optimized_text", "Optimized spoken text"),
            "voice_id": changes.get("voice_id", "voice-pl-1"),
            "voice": changes.get("voice", "Marek"),
            "language": changes.get("language", "pl"),
            "selected_take_id": "take-1",
            "takes": [],
        }

    def update_generation_segments(self, session_id, *, updates, idempotency_key):
        self.calls.append(
            (
                "update_generation_segments",
                {
                    "session_id": session_id,
                    "updates": updates,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "items": [
                {
                    "id": item["id"],
                    "revision": item["revision"] + 1,
                    "ordinal": index,
                    "status": "ready",
                    "text": item["changes"].get("text", "Original cue text"),
                    "optimized_text": item["changes"].get(
                        "optimized_text", "Optimized spoken text"
                    ),
                    "removed": item["changes"].get("removed", False),
                    "voice_id": item["changes"].get("voice_id", "voice-pl-1"),
                    "voice": item["changes"].get("voice", "Marek"),
                    "language": item["changes"].get("language", "pl"),
                    "speech_block_provenance": {"large": "private details"},
                    "takes": [{"id": "take-private", "artifact_id": "artifact"}],
                }
                for index, item in enumerate(updates, start=1)
            ]
        }

    def select_generation_take(
        self, segment_id, take_id, *, expected_revision, idempotency_key
    ):
        self.calls.append(
            (
                "select_generation_take",
                {
                    "segment_id": segment_id,
                    "take_id": take_id,
                    "expected_revision": expected_revision,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "id": segment_id,
            "ordinal": 1,
            "revision": expected_revision + 1,
            "status": "ready",
            "selected_take_id": take_id,
            "takes": [],
        }

    def start_generation_run(
        self,
        session_id,
        *,
        segment_ids=None,
        operation="generate",
        idempotency_key="",
        speech_plan_revision_id=None,
        stale_only=False,
        view="full",
    ):
        self.calls.append(
            (
                "start_generation_run",
                {
                    "session_id": session_id,
                    "segment_ids": segment_ids,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                    "speech_plan_revision_id": speech_plan_revision_id,
                    "stale_only": stale_only,
                    "view": view,
                },
            )
        )
        if self.generation_run_error is not None:
            raise self.generation_run_error
        return {
            "id": "run-gen-1",
            "job_id": "job-gen-1",
            "run_id": "run-gen-1",
            "session_id": session_id,
            "state": "queued",
            "progress": 0.0,
        }

    def create_output_assembly(
        self, session_id, *, generation_run_id=None, idempotency_key=""
    ):
        self.calls.append(
            (
                "create_output_assembly",
                {
                    "session_id": session_id,
                    "generation_run_id": generation_run_id,
                    "idempotency_key": idempotency_key,
                },
            )
        )
        return {
            "id": "assembly-1",
            "job_id": "job-assembly-1",
            "session_id": session_id,
            "state": "queued",
            "progress": 0.0,
        }


class PreviewAndGenerationTests(unittest.TestCase):
    def setUp(self):
        self.application = _FakeApplication()
        self.runtime = SimpleNamespace(
            require_application=lambda: self.application,
        )

    def test_list_sessions_with_query_filters_results(self):
        outcome = list_sessions(
            self.runtime,
            ListSessionsInput(query="Pascal"),
        )
        items = outcome["items"]
        self.assertEqual(1, len(items))
        self.assertEqual("session-1", items[0]["id"])
        self.assertEqual("Pascal Polish Lecture", items[0]["name"])

    def test_preview_subtitles_defaults_to_highest_stage_and_supports_search(self):
        outcome = preview_subtitles(
            self.runtime,
            PreviewSubtitlesInput(session_id="session-1", query="filozofię"),
        )
        # Without explicit stage, translate is selected over transcribe
        self.assertEqual("translate", outcome["stage"])
        self.assertEqual(0, outcome["matched_cues"])

        # With transcribe stage explicitly specified
        outcome_pl = preview_subtitles(
            self.runtime,
            PreviewSubtitlesInput(
                session_id="session-1",
                stage="transcribe",
                query="filozofię",
            ),
        )
        self.assertEqual("transcribe", outcome_pl["stage"])
        self.assertEqual("pl", outcome_pl["language"])
        self.assertEqual(1, outcome_pl["matched_cues"])
        self.assertEqual(
            "Dzisiaj omówimy filozofię Pascala.",
            outcome_pl["cues"][0]["text"],
        )

    def test_preview_subtitles_via_artifact_id(self):
        outcome = preview_subtitles(
            self.runtime,
            PreviewSubtitlesInput(session_id="session-1", artifact_id="art-1"),
        )
        self.assertEqual("art-1", outcome["artifact_id"])
        self.assertEqual(1, len(outcome["cues"]))
        self.assertEqual("Dokładny tekst recenzji.", outcome["cues"][0]["text"])

    def test_generation_segments_inspection_and_update(self):
        listed = list_generation_segments(
            self.runtime,
            ListGenerationSegmentsInput(session_id="session-1"),
        )
        self.assertEqual(1, len(listed["items"]))
        segment = listed["items"][0]
        self.assertEqual("segment-1", segment["id"])
        self.assertEqual("Optimized spoken text", segment["optimized_text"])
        self.assertEqual(["source-1"], segment["source_segment_ids"])
        self.assertEqual("a0001", segment["alignment_group"])
        self.assertEqual(
            "document_start",
            segment["speech_block_provenance"]["boundary_before"]["reason_code"],
        )
        self.assertEqual(1, len(segment["takes"]))
        self.assertEqual("artifact-take-1", segment["takes"][0]["artifact_id"])
        self.assertEqual("plan-revision-3", listed["plan_revision_id"])
        self.assertEqual("plan-revision-2", listed["parent_revision_id"])
        self.assertEqual({"action": "split"}, listed["operation_json"])
        self.assertEqual(220, listed["speech_block_settings"]["speech_block_max_chars"])

        updated = update_generation_segment(
            self.runtime,
            UpdateGenerationSegmentInput(
                session_id="session-1",
                segment_id="segment-1",
                expected_revision=3,
                optimized_text="Better spoken text",
                idempotency_key="gen-update:1",
            ),
        )
        self.assertEqual(4, updated.result["revision"])
        self.assertEqual("Better spoken text", updated.result["optimized_text"])
        self.assertFalse(updated.result["removed"])
        single_call = next(
            payload
            for name, payload in self.application.calls
            if name == "update_generation_segment"
        )
        self.assertEqual({"optimized_text": "Better spoken text"}, single_call["changes"])

    def test_generation_segment_text_and_removed_can_be_updated_and_restored(self):
        edited = update_generation_segment(
            self.runtime,
            UpdateGenerationSegmentInput(
                session_id="session-1",
                segment_id="segment-1",
                expected_revision=3,
                idempotency_key="gen-update:text:1",
                text="Reviewed source text",
                removed=True,
            ),
        )
        restored = update_generation_segment(
            self.runtime,
            UpdateGenerationSegmentInput(
                session_id="session-1",
                segment_id="segment-1",
                expected_revision=4,
                idempotency_key="gen-update:restore:1",
                removed=False,
            ),
        )

        self.assertEqual("Reviewed source text", edited.result["text"])
        self.assertTrue(edited.result["removed"])
        self.assertFalse(restored.result["removed"])
        calls = [
            payload
            for name, payload in self.application.calls
            if name == "update_generation_segment"
        ]
        self.assertEqual(
            {"text": "Reviewed source text", "removed": True},
            calls[0]["changes"],
        )
        self.assertEqual({"removed": False}, calls[1]["changes"])
        with self.assertRaises(ValidationError):
            UpdateGenerationSegmentInput(
                session_id="session-1",
                segment_id="segment-1",
                expected_revision=5,
                idempotency_key="gen-update:blank:1",
                text=" \t ",
            )

    def test_single_explicit_none_keeps_legacy_no_change_semantics(self):
        update_generation_segment(
            self.runtime,
            UpdateGenerationSegmentInput(
                session_id="session-1",
                segment_id="segment-1",
                expected_revision=3,
                idempotency_key="gen-update:none:1",
                text=None,
                removed=None,
            ),
        )
        application_call = next(
            payload
            for name, payload in self.application.calls
            if name == "update_generation_segment"
        )
        self.assertEqual({}, application_call["changes"])

    def test_generation_segment_batch_validation_routing_and_compact_projection(self):
        arguments = UpdateGenerationSegmentsInput(
            session_id="session-1",
            idempotency_key="generation-batch:1",
            updates=[
                {
                    "id": "segment-1",
                    "revision": 4,
                    "changes": {"text": "Reviewed", "removed": False},
                },
                {
                    "id": "segment-2",
                    "revision": 2,
                    "changes": {"optimized_text": None, "voice_id": None},
                },
            ],
        )
        outcome = update_generation_segments(self.runtime, arguments)

        application_call = next(
            payload
            for name, payload in self.application.calls
            if name == "update_generation_segments"
        )
        self.assertEqual("session-1", application_call["session_id"])
        self.assertEqual("generation-batch:1", application_call["idempotency_key"])
        self.assertEqual(
            [
                {
                    "id": "segment-1",
                    "revision": 4,
                    "changes": {"text": "Reviewed", "removed": False},
                },
                {
                    "id": "segment-2",
                    "revision": 2,
                    "changes": {"optimized_text": None, "voice_id": None},
                },
            ],
            application_call["updates"],
        )
        self.assertEqual(5, outcome.result["items"][0]["revision"])
        self.assertFalse(outcome.result["items"][0]["removed"])
        self.assertNotIn("takes", outcome.result["items"][0])
        self.assertNotIn("speech_block_provenance", outcome.result["items"][0])

    def test_generation_segment_batch_rejects_empty_duplicate_and_unknown_changes(self):
        common = {
            "session_id": "session-1",
            "idempotency_key": "generation-batch:1",
        }
        for item in (
            {"id": "segment-1", "revision": 1, "changes": {}},
            {
                "id": "segment-1",
                "revision": 1,
                "changes": {"audio_source": "not supported"},
            },
        ):
            with self.subTest(item=item), self.assertRaises(ValidationError):
                UpdateGenerationSegmentsInput(**common, updates=[item])
        duplicate = {"id": "segment-1", "revision": 1, "changes": {"removed": False}}
        with self.assertRaises(ValidationError):
            UpdateGenerationSegmentsInput(**common, updates=[duplicate, duplicate])
        with self.assertRaises(ValidationError):
            UpdateGenerationSegmentsInput(
                **common,
                updates=[{"id": "segment-1", "revision": 0, "changes": {"text": "x"}}],
            )
        with self.assertRaises(ValidationError):
            UpdateGenerationSegmentsInput(
                **common,
                updates=[{"id": "segment-1", "revision": 1, "changes": {"removed": None}}],
            )

    def test_topology_batch_timeout_preserves_details_and_adds_safe_replay_actions(self):
        self.application.topology_batch_error = PandratorMcpError(
            "application_response_timeout",
            "The Pandrator mutation timed out before a response; its outcome is unknown.",
            details={
                "timeout_seconds": 120.0,
                "operation_outcome": "unknown",
                "retry_policy": "same_request_and_idempotency_key",
            },
            retryable=True,
        )
        arguments = ReviseSpeechBlockPlanBatchInput(
            session_id="session-1",
            expected_revision_id="plan-revision-3",
            idempotency_key="topology:batch:timeout-1",
            operations=[{"action": "split", "segment_id": "segment-1", "cursor": 2}],
        )

        with self.assertRaises(PandratorMcpError) as caught:
            revise_speech_block_plan_batch(self.runtime, arguments)

        self.assertEqual(
            {
                "timeout_seconds": 120.0,
                "operation_outcome": "unknown",
                "retry_policy": "same_request_and_idempotency_key",
            },
            caught.exception.details,
        )
        self.assertEqual(
            ["pandrator_get_speech_plan_status", "pandrator_revise_speech_block_plan_batch"],
            [action.tool for action in caught.exception.next_actions],
        )
        replay = caught.exception.next_actions[1].arguments
        self.assertEqual("topology:batch:timeout-1", replay["idempotency_key"])
        self.assertEqual("plan-revision-3", replay["expected_revision_id"])

    def test_generate_speech_plan_timeout_inspects_runs_and_replays_exact_arguments(self):
        self.application.generation_run_error = PandratorMcpError(
            "application_response_timeout",
            "The Pandrator mutation timed out before a response; its outcome is unknown.",
            details={
                "timeout_seconds": 120.0,
                "operation_outcome": "unknown",
                "retry_policy": "same_request_and_idempotency_key",
            },
            retryable=True,
        )
        arguments = GenerateSpeechPlanInput(
            session_id="session-1",
            speech_plan_revision_id="plan-revision-17",
            stale_only=True,
            idempotency_key="generation:timeout:exact-replay-1",
            view="full",
        )

        with self.assertRaises(PandratorMcpError) as caught:
            generate_speech_plan(self.runtime, arguments)

        self.assertEqual(self.application.generation_run_error.details, caught.exception.details)
        self.assertEqual(
            ["pandrator_list_generation_runs", "pandrator_generate_speech_plan"],
            [action.tool for action in caught.exception.next_actions],
        )
        self.assertEqual(
            {"session_id": "session-1", "limit": 5},
            caught.exception.next_actions[0].arguments,
        )
        self.assertEqual(
            arguments.model_dump(mode="json", exclude_unset=True),
            caught.exception.next_actions[1].arguments,
        )
        self.assertEqual("plan-revision-17", caught.exception.next_actions[1].arguments["speech_plan_revision_id"])
        self.assertEqual("generation:timeout:exact-replay-1", caught.exception.next_actions[1].arguments["idempotency_key"])
        self.assertEqual(
            ["start_generation_run"],
            [name for name, _payload in self.application.calls],
        )

    def test_revise_speech_block_plan_exposes_typed_split_and_follow_up(self):
        revised = revise_speech_block_plan(
            self.runtime,
            ReviseSpeechBlockPlanInput(
                session_id="session-1",
                expected_revision_id="plan-revision-3",
                action="split",
                segment_id="segment-1",
                cursor=7,
                text_layer="display",
                idempotency_key="topology:split:1",
            ),
        )

        self.assertEqual("plan-revision-4", revised.result["plan_revision_id"])
        self.assertEqual(
            "pandrator_list_generation_segments",
            revised.next_actions[0].tool,
        )
        call = next(
            payload
            for name, payload in self.application.calls
            if name == "revise_generation_plan_topology"
        )
        self.assertEqual("split", call["action"])
        self.assertEqual(7, call["cursor"])
        self.assertEqual("display", call["text_layer"])

    def test_select_take(self):
        selected = select_take(
            self.runtime,
            SelectTakeInput(
                segment_id="segment-1",
                take_id="take-2",
                expected_revision=4,
                idempotency_key="take-select:1",
            ),
        )
        self.assertEqual("take-2", selected.result["selected_take_id"])

    def test_regenerate_segments_and_assemble(self):
        regen = regenerate_segments(
            self.runtime,
            RegenerateSegmentsInput(
                session_id="session-1",
                segment_ids=["segment-1", "segment-2"],
                idempotency_key="regen:1",
            ),
        )
        self.assertEqual(2, regen.result["segment_count"])
        self.assertEqual("queued", regen.result["status"])
        self.assertIsNotNone(regen.work)
        self.assertEqual("job-gen-1", regen.work.id)
        self.assertEqual("run-gen-1", regen.result["run_id"])
        self.assertEqual(
            "regenerate",
            next(
                payload["operation"]
                for name, payload in self.application.calls
                if name == "start_generation_run"
            ),
        )

        assembled = assemble_generation_run(
            self.runtime,
            AssembleGenerationRunInput(
                session_id="session-1",
                idempotency_key="assemble:1",
            ),
        )
        self.assertEqual("queued", assembled.result["status"])
        self.assertEqual("assembly-1", assembled.result["assembly_id"])
        self.assertIsNotNone(assembled.work)
        self.assertEqual("job-assembly-1", assembled.work.id)

    def test_preview_subtitles_around_ordinal_and_context(self):
        outcome = preview_subtitles(
            self.runtime,
            PreviewSubtitlesInput(
                session_id="session-1",
                stage="transcribe",
                around_ordinal=2,
                context=1,
            ),
        )
        self.assertEqual("transcribe", outcome["stage"])
        self.assertEqual(2, len(outcome["cues"]))
        self.assertEqual(1, outcome["cues"][0]["ordinal"])
        self.assertEqual(2, outcome["cues"][1]["ordinal"])

    def test_preview_subtitles_start_and_end_ordinal(self):
        outcome = preview_subtitles(
            self.runtime,
            PreviewSubtitlesInput(
                session_id="session-1",
                stage="transcribe",
                start_ordinal=2,
                end_ordinal=2,
            ),
        )
        self.assertEqual(1, len(outcome["cues"]))
        self.assertEqual(2, outcome["cues"][0]["ordinal"])

    def test_replace_subtitle_text_dry_run_and_commit(self):
        # 1. Dry run
        dry_run_outcome = replace_subtitle_text(
            self.runtime,
            ReplaceSubtitleTextInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                search_text="Pascala",
                replacement_text="Blaise'a Pascala",
                dry_run=True,
                idempotency_key="rep:1",
            ),
        )
        self.assertTrue(dry_run_outcome.result["dry_run"])
        self.assertEqual(1, dry_run_outcome.result["modified_count"])
        self.assertEqual(
            "Dzisiaj omówimy filozofię Blaise'a Pascala.",
            dry_run_outcome.result["changes"][0]["after"],
        )
        save_calls = [
            c for c in self.application.calls if c[0] == "save_subtitle_review"
        ]
        self.assertEqual(len(save_calls), 0)

        # 2. Actual commit
        commit_outcome = replace_subtitle_text(
            self.runtime,
            ReplaceSubtitleTextInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                search_text="Pascala",
                replacement_text="Blaise'a Pascala",
                dry_run=False,
                idempotency_key="rep:2",
            ),
        )
        self.assertFalse(commit_outcome.result["dry_run"])
        self.assertEqual(1, commit_outcome.result["modified_count"])
        self.assertEqual(2, commit_outcome.result["revision"])
        self.assertEqual("art-reviewed-2", commit_outcome.result["artifact_id"])
        self.assertEqual([2], commit_outcome.result["changed_ordinals"])

    def test_replace_subtitle_text_whole_word_matching(self):
        # "Pas" should not match "Pascala" with whole_word=True
        no_match = replace_subtitle_text(
            self.runtime,
            ReplaceSubtitleTextInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                search_text="Pas",
                replacement_text="Blaise",
                whole_word=True,
                dry_run=False,
                idempotency_key="rep:3",
            ),
        )
        self.assertEqual(0, no_match.result["modified_count"])

        # But with whole_word=False, it matches substring
        match = replace_subtitle_text(
            self.runtime,
            ReplaceSubtitleTextInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                search_text="Pas",
                replacement_text="Blaise",
                whole_word=False,
                dry_run=True,
                idempotency_key="rep:4",
            ),
        )
        self.assertEqual(1, match.result["modified_count"])

    def test_patch_subtitle_cues(self):
        outcome = patch_subtitle_cues(
            self.runtime,
            PatchSubtitleCuesInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                cues=[
                    CuePatchInput(
                        ordinal=2,
                        text="Zupełnie nowy tekst odcinka drugiego.",
                        speaker="Profesor",
                    )
                ],
                idempotency_key="patch:1",
            ),
        )
        self.assertEqual(2, outcome.result["revision"])
        self.assertEqual(1, outcome.result["patched_count"])
        self.assertEqual([2], outcome.result["patched_ordinals"])
        change = outcome.result["changes"][0]
        self.assertEqual(2, change["ordinal"])
        self.assertEqual("Dzisiaj omówimy filozofię Pascala.", change["before"]["text"])
        self.assertEqual("Profesor", change["after"]["speaker"])
        self.assertEqual(
            "Zupełnie nowy tekst odcinka drugiego.", change["after"]["text"]
        )

    def test_patch_subtitle_cues_preserves_source_review_metadata(self):
        outcome = patch_subtitle_cues(
            self.runtime,
            PatchSubtitleCuesInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                cues=[CuePatchInput(ordinal=2, start_ms=2600)],
                idempotency_key="patch:metadata",
            ),
        )

        self.assertEqual(2, outcome.result["revision"])
        save_calls = [
            call
            for call in self.application.calls
            if call[0] == "save_subtitle_review"
        ]
        self.assertEqual(1, len(save_calls))
        self.assertEqual(
            [
                {
                    "start_ms": 0,
                    "end_ms": 2500,
                    "text": "Dzień dobry wszystkim.",
                    "speaker": "SPEAKER_1",
                    "review_state": "uncertain",
                    "review_note": "Source name unclear",
                    "evidence_ids": ["original-evidence"],
                    "uncertain_source_cue_ids": [143],
                },
                {
                    "start_ms": 2600,
                    "end_ms": 5000,
                    "text": "Dzisiaj omówimy filozofię Pascala.",
                    "speaker": "SPEAKER_1",
                    "review_state": "clear",
                    "review_note": "",
                    "evidence_ids": [],
                    "uncertain_source_cue_ids": [],
                },
            ],
            save_calls[0][1]["segments"],
        )

    def test_import_subtitles_can_create_first_revision(self):
        srt = "1\n00:00:00,000 --> 00:00:02,000\nFresh subtitle document.\n"
        outcome = import_subtitles(
            self.runtime,
            ImportSubtitlesInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=0,
                srt_content=srt,
                idempotency_key="import:fresh:1",
            ),
        )
        self.assertEqual(1, outcome.result["revision"])
        self.assertEqual(1, outcome.result["imported_cues"])

    def test_import_subtitles_from_srt_content(self):
        srt = (
            "1\n"
            "00:00:01,000 --> 00:00:03,500\n"
            "[Narrator]: Witamy serdecznie.\n\n"
            "2\n"
            "00:00:04,000 --> 00:00:07,000\n"
            "Rozpoczynamy wykład.\n"
        )
        outcome = import_subtitles(
            self.runtime,
            ImportSubtitlesInput(
                session_id="session-1",
                stage="transcribe",
                expected_revision=1,
                srt_content=srt,
                idempotency_key="import:1",
            ),
        )
        self.assertEqual(2, outcome.result["imported_cues"])
        self.assertEqual(2, outcome.result["revision"])
        self.assertEqual("art-reviewed-2", outcome.result["artifact_id"])


if __name__ == "__main__":
    unittest.main()
