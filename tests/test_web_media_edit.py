import json
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from unittest.mock import patch

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import SCHEMA_HEAD, Database, upgrade_database
from pandrator.web.media_edit import (
    MediaEditInputsChanged,
    MediaEditRevisionConflict,
    MediaEditService,
)
from pandrator.web.models import (
    Artifact,
    MediaEditPlan,
    MediaEditPlanRevision,
    SessionRecord,
    SessionSource,
    SourceAsset,
)


class MediaEditServiceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.directory.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.session_id = "11111111-1111-4111-8111-111111111111"
        with self.database.session() as session:
            session.add(
                SessionRecord(
                    id=self.session_id,
                    name="Media edit",
                    storage_key="22222222-2222-4222-8222-222222222222",
                    workflow_kind="media_edit",
                )
            )

    def tearDown(self):
        self.database.dispose()
        self.directory.cleanup()

    def _register(
        self,
        name: str,
        role: str,
        content: str | bytes,
        kind: str,
        *,
        parent_ids: list[str] | None = None,
        metadata: dict | None = None,
    ):
        path = self.paths.root / name
        path.write_bytes(content if isinstance(content, bytes) else content.encode())
        return self.artifacts.register(
            path,
            kind=kind,
            role=role,
            session_id=self.session_id,
            parent_ids=parent_ids,
            metadata=metadata,
        )

    def _attach(self, artifact, role: str, kind: str):
        with self.database.session() as session:
            asset = SourceAsset(
                artifact_id=artifact.id,
                display_name=artifact.relative_path,
                kind=kind,
                state="current",
            )
            session.add(asset)
            session.flush()
            session.add(
                SessionSource(
                    session_id=self.session_id,
                    source_asset_id=asset.id,
                    role=role,
                    is_current=True,
                )
            )

    def _service(self):
        return MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.paths.sessions,
            duration_probe=lambda _path: 5000,
        )

    def _seed_external(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAlice: hello world\n\n00:00:01.800 --> 00:00:02.800\nBob: overlap\n",
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")
        timing = self._register(
            "timing.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "timed",
                            "start_ms": 2000,
                            "end_ms": 3800,
                            "text": "hello world overlap",
                            "words": [
                                {"text": "hello", "start_ms": 2000, "end_ms": 2400},
                                {"text": "world", "start_ms": 2500, "end_ms": 2900},
                                {"text": "overlap", "start_ms": 3000, "end_ms": 3800},
                            ],
                        }
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
        )
        return media, captions, timing

    def test_schema_head_and_media_edit_foreign_keys(self):
        with sqlite3.connect(self.paths.database) as connection:
            self.assertEqual(
                SCHEMA_HEAD,
                connection.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()[0],
            )
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            self.assertIn("media_edit_plans", tables)
            self.assertIn("media_edit_plan_revisions", tables)
            self.assertEqual(
                [], connection.execute("PRAGMA foreign_key_check").fetchall()
            )

    def test_external_prepare_aligns_constant_offset_and_preserves_speakers(self):
        self._seed_external()
        service = self._service()
        result = service.prepare(self.session_id)
        plan = result["plan"]
        self.assertEqual(1, plan["revision"])
        self.assertEqual(
            {"id": "keep-000001", "start_ms": 0, "end_ms": 5000, "label": None},
            plan["keep_ranges"][0],
        )
        self.assertEqual(2000, plan["cues"][0]["start_ms"])
        self.assertEqual("Alice", plan["cues"][0]["speaker"])
        self.assertEqual("Bob", plan["cues"][1]["speaker"])
        self.assertEqual("external_transcript", plan["evidence"]["source_kind"])
        self.assertEqual(
            {
                "ready",
                "source_media_artifact",
                "external_transcript_artifact",
                "transcription_artifact",
                "timing_artifact",
            },
            set(result["readiness"]),
        )
        self.assertEqual(set(result), set(service.state(self.session_id)))

    def test_asr_only_prepare_is_supported_and_repeated_prepare_is_idempotent(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        transcript = self._register(
            "transcription.srt",
            "transcription",
            "1\n00:00:01,000 --> 00:00:02,000\nhello\n",
            "srt",
            parent_ids=[media.id],
        )
        service = self._service()
        first = service.prepare(self.session_id)
        second = service.prepare(self.session_id)
        self.assertEqual(1, first["plan"]["revision"])
        self.assertEqual(first["plan"]["revision_id"], second["plan"]["revision_id"])
        self.assertEqual(
            transcript.id, first["plan"]["editorial_transcript_artifact"]["id"]
        )

    def test_prepare_ignores_timing_from_a_different_primary_source(self):
        old_media = self._register("old.mp4", "upload", b"old", "video")
        timing = self._register(
            "old-timing.json",
            "word_timestamps",
            json.dumps({"schema": "pandrator.transcript.v1", "segments": []}),
            "json",
            parent_ids=[old_media.id],
        )
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAlice: hello\n",
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")

        state = self._service().prepare(self.session_id)

        self.assertIsNone(state["plan"]["timing_artifact"])
        self.assertIsNone(state["readiness"]["timing_artifact"])
        self.assertNotEqual(timing.id, state["plan"].get("timing_artifact_id"))

    def test_prepare_ignores_low_coverage_pre_aligned_artifact(self):
        media, captions, _timing = self._seed_external()
        prealigned = self._register(
            "prealigned-low.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "metadata": {"alignment_coverage": 0.4},
                    "segments": [
                        {
                            "id": "cue-000001",
                            "start_ms": 2_000,
                            "end_ms": 2_400,
                            "text": "hello world",
                            "words": [
                                {"text": "hello", "start_ms": 2_000, "end_ms": 2_400}
                            ],
                        }
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
            metadata={
                "alignment_method": "asr_lexical_projection",
                "authoritative_transcript_artifact_id": captions.id,
                "alignment_coverage": 0.4,
            },
        )

        state = self._service().prepare(self.session_id)

        self.assertEqual(state["plan"]["timing_artifact"]["id"], prealigned.id)
        self.assertEqual(
            [cue["timing_source"] for cue in state["plan"]["cues"]],
            ["caption", "caption"],
        )
        self.assertEqual(
            [cue["start_ms"] for cue in state["plan"]["cues"]], [1000, 1800]
        )

    def test_prepare_consumes_valid_same_source_alignment_without_reprojection(self):
        media, captions, _timing = self._seed_external()
        prealigned = self._register(
            "prealigned-valid.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "metadata": {"alignment_coverage": 1.0},
                    "segments": [
                        {
                            "id": "cue-000001",
                            "start_ms": 1_900,
                            "end_ms": 3_000,
                            "text": "hello world",
                            "metadata": {"timing_confidence": 1.0},
                            "words": [
                                {"text": "hello", "start_ms": 2_000, "end_ms": 2_400},
                                {"text": "world", "start_ms": 2_500, "end_ms": 2_900},
                            ],
                        },
                        {
                            "id": "cue-000002",
                            "start_ms": 2_900,
                            "end_ms": 3_900,
                            "text": "overlap",
                            "metadata": {"timing_confidence": 1.0},
                            "words": [
                                {"text": "overlap", "start_ms": 3_000, "end_ms": 3_800}
                            ],
                        },
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
            metadata={
                "alignment_method": "asr_lexical_projection",
                "authoritative_transcript_artifact_id": captions.id,
                "alignment_coverage": 1.0,
            },
        )

        with patch(
            "pandrator.web.media_edit.align_cues_to_words",
            side_effect=AssertionError(
                "pre-aligned artifacts must not be projected again"
            ),
        ):
            state = self._service().prepare(self.session_id)

        self.assertEqual(state["plan"]["timing_artifact"]["id"], prealigned.id)
        self.assertEqual(
            [cue["start_ms"] for cue in state["plan"]["cues"]], [1900, 2900]
        )
        self.assertEqual([cue["end_ms"] for cue in state["plan"]["cues"]], [3000, 3900])
        self.assertEqual(
            [cue["speaker"] for cue in state["plan"]["cues"]], ["Alice", "Bob"]
        )
        self.assertFalse(
            any(
                "No ASR timing was available" in warning
                for warning in state["plan"]["evidence"]["warnings"]
            )
        )

    def test_prepare_preserves_ctc_coverage_separately_from_timing_quality(self):
        media, captions, _timing = self._seed_external()
        prealigned = self._register(
            "prealigned-ctc.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "cue-000001",
                            "start_ms": 1_900,
                            "end_ms": 3_000,
                            "text": "hello world",
                            "metadata": {
                                "timing_confidence": 0.5,
                                "timing_source": "ctc_alignment",
                            },
                            "words": [
                                {"text": "hello", "start_ms": 2_000, "end_ms": 2_400},
                                {"text": "world", "start_ms": 2_500, "end_ms": 2_900},
                            ],
                        },
                        {
                            "id": "cue-000002",
                            "start_ms": 2_900,
                            "end_ms": 3_900,
                            "text": "overlap",
                            "metadata": {
                                "timing_confidence": 0.5,
                                "timing_source": "ctc_alignment",
                            },
                            "words": [
                                {"text": "overlap", "start_ms": 3_000, "end_ms": 3_800}
                            ],
                        },
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
            metadata={
                "alignment_method": "ctc_cue_alignment",
                "authoritative_transcript_artifact_id": captions.id,
                "alignment_coverage": 1.0,
                "eligible_alignment_coverage": 0.99,
                "alignment_confidence": 0.5,
                "word_count": 3,
                "cue_count": 2,
                "accepted_cue_count": 2,
                "accepted_token_count": 3,
                "first_pass_batch_count": 1,
                "fallback_triggered": False,
            },
        )

        state = self._service().prepare(self.session_id)

        evidence = state["plan"]["evidence"]
        self.assertEqual(state["plan"]["timing_artifact"]["id"], prealigned.id)
        self.assertEqual(
            [cue["timing_source"] for cue in state["plan"]["cues"]],
            ["ctc_alignment", "ctc_alignment"],
        )
        self.assertEqual(1.0, evidence["alignment_coverage"])
        self.assertEqual(0.99, evidence["alignment_eligible_coverage"])
        self.assertEqual(0.5, evidence["alignment_quality"])
        self.assertEqual(3, evidence["word_count"])
        self.assertEqual(2, evidence["cue_count"])
        self.assertTrue(evidence["alignment_artifact_reused"])
        self.assertFalse(evidence["fallback_triggered"])

    def test_prepare_ignores_pre_aligned_artifact_from_different_transcript(self):
        media, _captions, _timing = self._seed_external()
        mismatched = self._register(
            "prealigned-mismatch.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "cue-000001",
                            "start_ms": 2_000,
                            "end_ms": 2_900,
                            "text": "hello world",
                            "words": [
                                {"text": "hello", "start_ms": 2_000, "end_ms": 2_400},
                                {"text": "world", "start_ms": 2_500, "end_ms": 2_900},
                            ],
                        },
                        {
                            "id": "cue-000002",
                            "start_ms": 3_000,
                            "end_ms": 3_800,
                            "text": "overlap",
                            "words": [
                                {"text": "overlap", "start_ms": 3_000, "end_ms": 3_800}
                            ],
                        },
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
            metadata={
                "alignment_method": "asr_lexical_projection",
                "authoritative_transcript_artifact_id": "different-caption-id",
                "alignment_coverage": 1.0,
            },
        )

        state = self._service().prepare(self.session_id)

        self.assertEqual(state["plan"]["timing_artifact"]["id"], mismatched.id)
        self.assertEqual(
            [cue["timing_source"] for cue in state["plan"]["cues"]],
            ["caption", "caption"],
        )
        self.assertEqual(
            [cue["start_ms"] for cue in state["plan"]["cues"]], [1000, 1800]
        )
        self.assertTrue(
            any(
                "does not match" in warning
                for warning in state["plan"]["evidence"]["warnings"]
            )
        )

    def test_prepare_rejects_pre_aligned_word_outside_stored_segment(self):
        media, captions, _timing = self._seed_external()
        invalid = self._register(
            "prealigned-outside-segment.json",
            "word_timestamps",
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "cue-000001",
                            "start_ms": 1_900,
                            "end_ms": 3_000,
                            "text": "hello world",
                            "words": [
                                {"text": "hello", "start_ms": 1_800, "end_ms": 2_400},
                                {"text": "world", "start_ms": 2_500, "end_ms": 2_900},
                            ],
                        },
                        {
                            "id": "cue-000002",
                            "start_ms": 2_900,
                            "end_ms": 3_900,
                            "text": "overlap",
                            "words": [
                                {"text": "overlap", "start_ms": 3_000, "end_ms": 3_800}
                            ],
                        },
                    ],
                }
            ),
            "json",
            parent_ids=[media.id],
            metadata={
                "alignment_method": "asr_lexical_projection",
                "authoritative_transcript_artifact_id": captions.id,
                "alignment_coverage": 1.0,
            },
        )

        state = self._service().prepare(self.session_id)

        self.assertEqual(state["plan"]["timing_artifact"]["id"], invalid.id)
        self.assertEqual(
            [cue["timing_source"] for cue in state["plan"]["cues"]],
            ["caption", "caption"],
        )
        self.assertEqual(
            [cue["start_ms"] for cue in state["plan"]["cues"]], [1000, 1800]
        )
        self.assertTrue(
            any(
                "failed its cue provenance or timing checks" in warning
                for warning in state["plan"]["evidence"]["warnings"]
            )
        )

    def test_update_is_immutable_and_normalizes_ranges(self):
        self._seed_external()
        service = self._service()
        first = service.prepare(self.session_id)
        second = service.update(
            self.session_id,
            1,
            keep_ranges=[
                {"start_ms": 100, "end_ms": 2000},
                {"start_ms": 1800, "end_ms": 5000},
            ],
            instructions="Keep the dialogue.",
            reviewed=True,
        )
        self.assertEqual(2, second["plan"]["revision"])
        self.assertEqual(
            first["plan"]["revision_id"], second["plan"]["parent_revision_id"]
        )
        self.assertEqual(100, second["plan"]["keep_ranges"][0]["start_ms"])
        self.assertEqual(5000, second["plan"]["keep_ranges"][0]["end_ms"])
        self.assertEqual(set(first), set(second))
        with self.assertRaises(MediaEditRevisionConflict):
            service.update(
                self.session_id, 1, keep_ranges=[{"start_ms": 0, "end_ms": 1}]
            )
        self.assertEqual(1, service.revision(self.session_id, 1)["revision"])

    def test_noop_update_reuses_the_active_revision(self):
        self._seed_external()
        service = self._service()
        first = service.prepare(self.session_id)

        second = service.update(
            self.session_id,
            first["plan"]["revision"],
            keep_ranges=first["plan"]["keep_ranges"],
            instructions=first["plan"]["instructions"],
            reviewed=first["plan"]["reviewed"],
        )

        self.assertEqual(first["plan"]["revision_id"], second["plan"]["revision_id"])

    def test_content_change_does_not_inherit_reviewed_state(self):
        self._seed_external()
        service = self._service()
        prepared = service.prepare(self.session_id)
        reviewed = service.update(
            self.session_id,
            prepared["plan"]["revision"],
            keep_ranges=prepared["plan"]["keep_ranges"],
            reviewed=True,
        )

        changed = service.update(
            self.session_id,
            reviewed["plan"]["revision"],
            keep_ranges=reviewed["plan"]["keep_ranges"],
            instructions="Remove the private discussion.",
        )

        self.assertFalse(changed["plan"]["reviewed"])

    def test_cut_listing_maps_internal_edges_to_their_actual_keep_ranges(self):
        self._seed_external()
        service = self._service()
        service.prepare(self.session_id)
        service.update(
            self.session_id,
            1,
            keep_ranges=[
                {"start_ms": 0, "end_ms": 2_000},
                {"start_ms": 3_000, "end_ms": 4_000},
                {"start_ms": 4_500, "end_ms": 5_000},
            ],
        )

        result = service.list_cuts(self.session_id)

        self.assertEqual(
            [(2_000, 3_000), (4_000, 4_500)],
            [(item["start_ms"], item["end_ms"]) for item in result["cuts"]],
        )
        self.assertEqual(
            "keep-000001", result["cuts"][0]["start"]["adjacent_keep_range_id"]
        )
        self.assertEqual(
            "keep-000002", result["cuts"][0]["end"]["adjacent_keep_range_id"]
        )
        self.assertTrue(result["cuts"][0]["start"]["editable"])
        self.assertTrue(result["cuts"][0]["end"]["editable"])

        inspection = service.list_cuts(
            self.session_id,
            cut_index=1,
            edge="start",
            context_ms=1_000,
            cue_limit=1,
        )
        self.assertEqual(2_000, inspection["boundary_ms"])
        self.assertEqual(1, len(inspection["cues"]))
        self.assertNotIn("plan", inspection)

    def test_boundary_inspection_caps_words_gaps_and_text(self):
        self._seed_external()
        service = self._service()
        service.prepare(self.session_id)
        service.update(
            self.session_id,
            1,
            keep_ranges=[
                {"start_ms": 0, "end_ms": 2_500},
                {"start_ms": 3_000, "end_ms": 5_000},
            ],
        )
        with self.database.session() as session:
            plan = (
                session.query(MediaEditPlan).filter_by(session_id=self.session_id).one()
            )
            revision = session.get(MediaEditPlanRevision, plan.active_revision_id)
            revision.cues_json = [
                {
                    "id": "dense-cue",
                    "start_ms": 1_000,
                    "end_ms": 4_000,
                    "text": "x" * 5_000,
                    "speaker": "s" * 600,
                    "timing_source": "ctc_alignment",
                    "timing_confidence": 0.9,
                    "words": [
                        {
                            "text": "w" * 300,
                            "start_ms": 1_500 + index * 2,
                            "end_ms": 1_501 + index * 2,
                            "confidence": 0.9,
                        }
                        for index in range(600)
                    ],
                }
            ]

        inspection = service.list_cuts(
            self.session_id,
            cut_index=1,
            edge="start",
            context_ms=2_000,
            cue_limit=1,
        )

        words = inspection["cues"][0]["words"]
        self.assertEqual(400, len(words))
        self.assertTrue(all(len(item["text"]) <= 256 for item in words))
        self.assertEqual(200, len(inspection["speech_gaps"]))
        self.assertEqual(4_000, len(inspection["cues"][0]["text"]))
        self.assertEqual(500, len(inspection["cues"][0]["speaker"]))
        self.assertEqual(
            {
                "cues": False,
                "cue_words": True,
                "cue_text": True,
                "speech_gaps": True,
            },
            inspection["truncated"],
        )

    def test_boundary_refinement_is_immutable_and_revision_guarded(self):
        self._seed_external()
        service = self._service()
        service.prepare(self.session_id)
        edited = service.update(
            self.session_id,
            1,
            keep_ranges=[
                {"start_ms": 0, "end_ms": 1_000, "label": "Opening"},
                {"start_ms": 2_000, "end_ms": 3_000, "label": "Middle"},
                {"start_ms": 4_000, "end_ms": 5_000, "label": "Closing"},
            ],
            reviewed=True,
        )

        result = service.refine_boundary(
            self.session_id,
            edited["plan"]["revision"],
            cut_index=1,
            edge="start",
            delta_ms=-100,
        )

        self.assertEqual(3, result["current_revision"]["revision"])
        self.assertFalse(result["current_revision"]["reviewed"])
        self.assertEqual(
            (1_000, 900), (result["change"]["from_ms"], result["change"]["to_ms"])
        )
        self.assertEqual(
            (900, 2_000),
            (result["affected_cut"]["start_ms"], result["affected_cut"]["end_ms"]),
        )
        self.assertEqual(
            ["Opening", "Middle", "Closing"],
            [
                item["label"]
                for item in service.revision(self.session_id, 3)["keep_ranges"]
            ],
        )
        self.assertEqual(2, service.revision(self.session_id, 2)["revision"])
        with self.assertRaises(MediaEditRevisionConflict):
            service.refine_boundary(
                self.session_id,
                2,
                cut_index=1,
                edge="end",
                delta_ms=100,
            )

    def test_boundary_refinement_can_collapse_a_keep_island(self):
        self._seed_external()
        service = self._service()
        service.prepare(self.session_id)
        service.update(
            self.session_id,
            1,
            keep_ranges=[
                {"start_ms": 1_000, "end_ms": 2_000},
                {"start_ms": 3_000, "end_ms": 5_000},
            ],
        )

        result = service.refine_boundary(
            self.session_id,
            2,
            cut_index=2,
            edge="start",
            position_ms=1_000,
        )

        self.assertEqual(3, result["current_revision"]["revision"])
        self.assertEqual(1, result["affected_cut"]["index"])
        self.assertEqual(
            (0, 3_000),
            (result["affected_cut"]["start_ms"], result["affected_cut"]["end_ms"]),
        )
        self.assertEqual(1, len(service.list_cuts(self.session_id)["cuts"]))

    def test_outer_boundary_is_fixed_and_noop_reuses_revision(self):
        self._seed_external()
        service = self._service()
        service.prepare(self.session_id)
        service.update(
            self.session_id,
            1,
            keep_ranges=[{"start_ms": 1_000, "end_ms": 5_000}],
        )

        current = service.list_cuts(self.session_id)
        self.assertFalse(current["cuts"][0]["start"]["editable"])
        with self.assertRaisesRegex(ValueError, "fixed"):
            service.refine_boundary(
                self.session_id,
                2,
                cut_index=1,
                edge="start",
                delta_ms=100,
            )
        noop = service.refine_boundary(
            self.session_id,
            2,
            cut_index=1,
            edge="end",
            position_ms=1_000,
        )
        self.assertTrue(noop["change"]["no_op"])
        self.assertEqual(2, noop["current_revision"]["revision"])

    def test_proposal_can_target_captionless_media_extents(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            (
                "WEBVTT\n\n"
                "00:00:01.000 --> 00:00:01.500\nAlice: opening\n\n"
                "00:00:03.500 --> 00:00:04.000\nBob: closing\n"
            ),
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")
        service = self._service()
        service.prepare(self.session_id)

        result = service.apply_proposal(
            self.session_id,
            1,
            [
                {
                    "start_at_media_start": True,
                    "end_cue_id": "cue-000001",
                    "reason": "Remove captionless setup.",
                },
                {
                    "start_cue_id": "cue-000002",
                    "end_at_media_end": True,
                    "reason": "Remove captionless tail.",
                },
            ],
        )

        cuts = result["plan"]["operation"]["cuts"]
        self.assertEqual((0, 1_500), (cuts[0]["start_ms"], cuts[0]["end_ms"]))
        self.assertEqual((3_500, 5_000), (cuts[1]["start_ms"], cuts[1]["end_ms"]))
        self.assertEqual("media_start", cuts[0]["start"]["method"])
        self.assertEqual("media_end", cuts[1]["end"]["method"])
        with self.assertRaisesRegex(ValueError, "Exactly one of start_cue_id"):
            service.apply_proposal(
                self.session_id,
                2,
                [
                    {
                        "start_cue_id": "cue-000001",
                        "start_at_media_start": True,
                        "end_cue_id": "cue-000002",
                        "reason": "Ambiguous start.",
                    }
                ],
            )

    def test_new_revision_invalidates_rendered_outputs_and_descendants(self):
        self._seed_external()
        service = self._service()
        prepared = service.prepare(self.session_id)
        revision = prepared["plan"]
        metadata = {
            "revision_id": revision["revision_id"],
            "content_hash": revision["content_hash"],
        }
        media = self._register(
            "edited.mp4",
            "media_edit_media",
            b"edited",
            "video",
            metadata=metadata,
        )
        subtitles = self._register(
            "edited.srt",
            "media_edit_subtitles",
            "1\n00:00:00,000 --> 00:00:01,000\nHello\n",
            "srt",
            parent_ids=[media.id],
            metadata=metadata,
        )
        correction = self._register(
            "corrected.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:01,000\nHello.\n",
            "srt",
            parent_ids=[subtitles.id],
        )

        service.update(
            self.session_id,
            revision["revision"],
            keep_ranges=revision["keep_ranges"],
            instructions="A changed edit decision.",
        )

        with self.database.session() as session:
            self.assertEqual("stale", session.get(Artifact, media.id).state)
            self.assertEqual("stale", session.get(Artifact, subtitles.id).state)
            self.assertEqual("stale", session.get(Artifact, correction.id).state)

    def test_proposal_preserves_caption_boundary_without_reliable_asr_alignment(self):
        media = self._register("media.mp4", "upload", b"media", "video")
        self._attach(media, "primary", "video")
        captions = self._register(
            "captions.vtt",
            "captions",
            "WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nAlice: remove this\n",
            "vtt",
        )
        self._attach(captions, "transcript", "vtt")
        service = self._service()
        service.prepare(self.session_id)

        result = service.apply_proposal(
            self.session_id,
            1,
            [
                {
                    "start_cue_id": "cue-000001",
                    "end_cue_id": "cue-000001",
                    "reason": "Remove setup chatter.",
                }
            ],
        )

        cut = result["plan"]["operation"]["cuts"][0]
        self.assertEqual((1000, 2000), (cut["start_ms"], cut["end_ms"]))
        self.assertEqual("caption_boundary", cut["start"]["method"])
        self.assertEqual("caption_boundary", cut["end"]["method"])
        self.assertTrue(cut["start"]["warnings"])
        self.assertIn(
            "No ASR timing was available",
            result["plan"]["evidence"]["warnings"][0],
        )

    def test_prepare_does_not_hold_immediate_write_session_during_probe(self):
        self._seed_external()
        immediate_entries: list[str] = []
        original_immediate_session = self.database.immediate_session

        @contextmanager
        def instrumented_immediate_session():
            immediate_entries.append("entered")
            with original_immediate_session() as session:
                yield session

        def probe(_path):
            self.assertEqual([], immediate_entries)
            return 5000

        service = MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.paths.sessions,
            duration_probe=probe,
        )
        with patch.object(
            self.database, "immediate_session", instrumented_immediate_session
        ):
            service.prepare(self.session_id)
        self.assertEqual(["entered"], immediate_entries)

    def test_prepare_rejects_inputs_changed_after_read_snapshot(self):
        media, _captions, _timing = self._seed_external()

        def probe(_path):
            with self.database.session() as session:
                current = session.get(Artifact, media.id)
                current.content_hash = "changed-during-prepare"
            return 5000

        service = MediaEditService(
            self.database,
            self.artifacts,
            lambda _session_id: self.paths.sessions,
            duration_probe=probe,
        )
        with self.assertRaises(MediaEditInputsChanged):
            service.prepare(self.session_id)


if __name__ == "__main__":
    unittest.main()
