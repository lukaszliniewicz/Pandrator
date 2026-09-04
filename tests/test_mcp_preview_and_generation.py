import unittest
from types import SimpleNamespace

from pandrator_mcp.schemas.generation import (
    AssembleGenerationRunInput,
    ListGenerationSegmentsInput,
    RegenerateSegmentsInput,
    SelectTakeInput,
    UpdateGenerationSegmentInput,
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
    list_generation_segments,
    regenerate_segments,
    select_take,
    update_generation_segment,
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

    def list_sessions(self, limit=50, query=None):
        self.calls.append(("list_sessions", {"limit": limit, "query": query}))
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
                        },
                        {
                            "ordinal": 1,
                            "start_ms": 2500,
                            "end_ms": 5000,
                            "speaker": "SPEAKER_1",
                            "text": "Dzisiaj omówimy filozofię Pascala.",
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
                    "text": "Original cue text",
                    "optimized_text": "Optimized spoken text",
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
        }

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
            "text": "Original cue text",
            "optimized_text": changes.get("optimized_text", "Optimized spoken text"),
            "voice_id": changes.get("voice_id", "voice-pl-1"),
            "voice": changes.get("voice", "Marek"),
            "language": changes.get("language", "pl"),
            "selected_take_id": "take-1",
            "takes": [],
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
        self, session_id, *, segment_ids=None, operation="generate", idempotency_key=""
    ):
        self.calls.append(
            (
                "start_generation_run",
                {
                    "session_id": session_id,
                    "segment_ids": segment_ids,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                },
            )
        )
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
        self.assertEqual(1, len(segment["takes"]))
        self.assertEqual("artifact-take-1", segment["takes"][0]["artifact_id"])

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
