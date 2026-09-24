"""Display text from the selected speech revision drives subtitle exports."""

from __future__ import annotations

import tempfile
import threading
import unittest

from sqlalchemy import select

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.generation_subtitles import (
    capture_display_subtitle_snapshot,
    project_display_srt,
)
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from tests.web_test_support import prepare_web_test_data_root

SOURCE_SRT = (
    "1\n00:00:01,000 --> 00:00:02,000\nFirst cue\n\n"
    "2\n00:00:03,000 --> 00:00:04,000\nSecond cue\n\n"
    "3\n00:00:05,000 --> 00:00:06,000\nUntouched cue\n"
)


def row(ordinal, text, refs, *, removed=False, provenance=None):
    return {
        "ordinal": ordinal,
        "text": text,
        "source_refs": refs,
        "removed": removed,
        "provenance": provenance or {},
    }


class ProjectionTests(unittest.TestCase):
    def project(self, *rows):
        return parse_srt(project_display_srt(SOURCE_SRT, {"segments": list(rows)}))

    def test_single_edited_cue_preserves_other_cues(self):
        cues = self.project(row(0, "Edited display", [1]))
        self.assertEqual([cue.text for cue in cues], [
            "Edited display", "Second cue", "Untouched cue",
        ])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 2000))

    def test_merged_edit_uses_union_window(self):
        cues = self.project(row(0, "Combined edit", [1, 2]))
        self.assertEqual([cue.text for cue in cues], ["Combined edit", "Untouched cue"])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 4000))

    def test_unchanged_merged_block_keeps_individual_cues(self):
        cues = self.project(row(0, "First cue Second cue", [1, 2]))
        self.assertEqual([cue.text for cue in cues], [
            "First cue", "Second cue", "Untouched cue",
        ])

    def test_valid_display_spans_use_actual_edited_slices(self):
        provenance = {"source_cues": [
            {"reference": 1, "display_text": "Newer cue", "display_spans": [[0, 9]]},
            {"reference": 2, "display_text": "Second cue", "display_spans": [[10, 20]]},
        ]}
        cues = self.project(row(
            0, "Newer cue Second cue", [1, 2], provenance=provenance
        ))
        self.assertEqual([cue.text for cue in cues], [
            "Newer cue", "Second cue", "Untouched cue",
        ])

    def test_stale_equal_length_display_evidence_uses_union_window(self):
        provenance = {"source_cues": [
            {"reference": 1, "display_text": "First cue", "display_spans": [[0, 9]]},
            {"reference": 2, "display_text": "Second cue", "display_spans": [[10, 20]]},
        ]}
        cues = self.project(row(
            0, "Newer cue Second cue", [1, 2], provenance=provenance
        ))
        self.assertEqual([cue.text for cue in cues], [
            "Newer cue Second cue", "Untouched cue",
        ])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 4000))

    def test_extra_text_outside_spans_uses_union_window(self):
        provenance = {"source_cues": [
            {"reference": 1, "display_spans": [[0, 9]]},
            {"reference": 2, "display_spans": [[10, 20]]},
        ]}
        cues = self.project(row(
            0, "First cue Second cue EXTRA", [1, 2], provenance=provenance
        ))
        self.assertEqual([cue.text for cue in cues], [
            "First cue Second cue EXTRA", "Untouched cue",
        ])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (1000, 4000))

    def test_split_shared_reference_is_emitted_once(self):
        cues = self.project(row(0, "Split", [1]), row(1, "display", [1]))
        self.assertEqual([cue.text for cue in cues], [
            "Split display", "Second cue", "Untouched cue",
        ])

    def test_removed_row_omits_covered_cue(self):
        cues = self.project(row(0, "First cue", [1], removed=True))
        self.assertEqual([cue.text for cue in cues], ["Second cue", "Untouched cue"])

    def test_bad_reference_fails_explicitly(self):
        with self.assertRaisesRegex(ValueError, "no usable source cue references"):
            self.project(row(0, "Edited display", []))

    def test_mixed_valid_and_malformed_references_fail(self):
        with self.assertRaisesRegex(ValueError, "malformed source cue reference"):
            self.project(row(0, "Edited display", [1, "bad"]))
        with self.assertRaisesRegex(ValueError, "malformed source cue reference"):
            self.project(row(0, "Edited display", [1.5]))
        with self.assertRaisesRegex(ValueError, "malformed source cue reference"):
            self.project(row(
                0, "Edited display", [1],
                provenance={"source_cues": [{"reference": 1.5}]},
            ))


class SnapshotAndExportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.paths = prepare_web_test_data_root(temporary.name)
        self.database = Database(self.paths.database)
        self.addCleanup(self.database.dispose)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.record = SessionService(self.database).create(
            "Display subtitle export", workflow_kind="subtitles"
        )
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir()
        self.display = self._artifact("display.srt", SOURCE_SRT, "transcription")

    def _artifact(self, filename, content, role, metadata=None):
        path = self.session_dir / filename
        path.write_text(content, encoding="utf-8")
        return self.artifacts.register(
            path, kind="srt", role=role, session_id=self.record.id,
            metadata=metadata or {},
        )

    def _revision(self, text, *, source=None, optimized="Speech only", active=True,
                  provenance=None):
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(
                GenerationPlan.session_id == self.record.id
            ))
            if plan is None:
                plan = GenerationPlan(session_id=self.record.id)
                session.add(plan)
                session.flush()
            count = len(list(session.scalars(select(GenerationPlanRevision).where(
                GenerationPlanRevision.plan_id == plan.id
            ))))
            revision = GenerationPlanRevision(
                plan_id=plan.id, revision_number=count + 1,
                settings_json={"_source_artifact_id": (source or self.display).id},
                content_hash=f"revision-{count + 1}",
            )
            session.add(revision)
            session.flush()
            session.add(GenerationSegment(
                plan_revision_id=revision.id, ordinal=0,
                source_segment_ids_json=[1], node_kind="subtitle_cue",
                text=text, optimized_text=optimized,
                speech_block_provenance_json=provenance or {},
            ))
            if active:
                plan.active_revision_id = revision.id
            return revision.id

    def _snapshot(self, settings=None):
        with self.database.session() as session:
            return capture_display_subtitle_snapshot(
                session, self.record.id, settings or {}
            )

    def test_optimized_source_uses_display_parent_and_excludes_speech_text(self):
        optimized = self._artifact(
            "optimized.srt", SOURCE_SRT.replace("First cue", "Wrong speech wording"),
            "tts_optimized", {"source_artifact_id": self.display.id},
        )
        revision_id = self._revision("Reviewed display", source=optimized)
        snapshot = self._snapshot()
        self.assertEqual(snapshot["revision_id"], revision_id)
        self.assertEqual(snapshot["display_artifact_id"], self.display.id)
        self.assertEqual(snapshot["segments"][0]["text"], "Reviewed display")
        self.assertNotIn("optimized_text", snapshot["segments"][0])

    def test_cross_session_source_and_run_are_rejected(self):
        foreign = SessionService(self.database).create(
            "Different session", workflow_kind="subtitles"
        )
        foreign_dir = self.paths.sessions / foreign.storage_key
        foreign_dir.mkdir()
        foreign_path = foreign_dir / "foreign.srt"
        foreign_path.write_text(SOURCE_SRT, encoding="utf-8")
        foreign_artifact = self.artifacts.register(
            foreign_path, kind="srt", role="transcription", session_id=foreign.id
        )
        self._revision("Foreign source", source=foreign_artifact)
        with self.assertRaisesRegex(ValueError, "unavailable in this session"):
            self._snapshot()

        with self.database.session() as session:
            foreign_plan = GenerationPlan(session_id=foreign.id)
            session.add(foreign_plan)
            session.flush()
            foreign_revision = GenerationPlanRevision(
                plan_id=foreign_plan.id, revision_number=1, settings_json={},
                content_hash="foreign",
            )
            session.add(foreign_revision)
            session.flush()
            foreign_run = GenerationRun(
                session_id=foreign.id, plan_revision_id=foreign_revision.id,
                sequence_number=1, status="completed", settings_snapshot_json={},
            )
            session.add(foreign_run)
            session.flush()
            foreign_run_id = foreign_run.id
        with self.assertRaisesRegex(ValueError, "unavailable in this session"):
            self._snapshot({"generation_run_id": foreign_run_id})

    def test_selected_old_run_and_queued_snapshot_ignore_later_edits(self):
        old_id = self._revision("Old reviewed")
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.record.id, plan_revision_id=old_id,
                sequence_number=1, status="completed", settings_snapshot_json={},
            )
            session.add(run)
            session.flush()
            run_id = run.id
        snapshot = self._snapshot({"generation_run_id": run_id})
        self._revision("New reviewed")
        self.assertEqual(snapshot["revision_id"], old_id)
        self.assertEqual(snapshot["segments"][0]["text"], "Old reviewed")
        self.assertEqual(self._snapshot({"generation_run_id": run_id})["revision_id"], old_id)
        self.assertEqual(self._snapshot()["segments"][0]["text"], "New reviewed")

    def test_queue_payload_freezes_display_revision(self):
        revision_id = self._revision("Queued display")
        resolved = WorkflowService(self.database, JobQueue(self.database)).resolve_stage(
            self.record.id, "export", {"export_mode": "subtitles"}
        )
        self.assertEqual(
            resolved.payload["display_subtitle_snapshot"]["revision_id"], revision_id
        )
        self._revision("Edited after queue")
        self.assertEqual(
            resolved.payload["display_subtitle_snapshot"]["segments"][0]["text"],
            "Queued display",
        )

    def test_heading_kind_with_timed_references_still_exports_display_edit(self):
        revision_id = self._revision("Edited heading")
        with self.database.session() as session:
            segment = session.scalar(select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == revision_id
            ))
            segment.node_kind = "heading"
        snapshot = self._snapshot()
        self.assertEqual(snapshot["segments"][0]["text"], "Edited heading")
        result = self.handlers.export(
            {"session_id": self.record.id,
             "settings": {"export_mode": "subtitles"},
             "display_subtitle_snapshot": snapshot},
            lambda *_: None, threading.Event(),
        )
        _artifact, output = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual(parse_srt(output.read_text())[0].text, "Edited heading")

    def test_untimed_paragraph_in_subtitle_plan_fails_explicitly(self):
        revision_id = self._revision("Untimed paragraph")
        with self.database.session() as session:
            segment = session.scalar(select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == revision_id
            ))
            segment.node_kind = "paragraph"
            segment.source_segment_ids_json = []
        snapshot = self._snapshot()
        self.assertEqual(len(snapshot["segments"]), 1)
        with self.assertRaisesRegex(ValueError, "no usable source cue references"):
            self.handlers.export(
                {"session_id": self.record.id,
                 "settings": {"export_mode": "subtitles"},
                 "display_subtitle_snapshot": snapshot},
                lambda *_: None, threading.Event(),
            )

    def test_logical_passage_number_collision_uses_verified_frozen_timing(self):
        with self.database.session() as session:
            artifact = session.get(Artifact, self.display.id)
            artifact.metadata_json = {
                "revision_id": "display-revision",
                "logical_passages": {
                    "schema_version": 1,
                    "display_revision_id": "display-revision",
                    "display_content_hash": artifact.content_hash,
                    "items": [{
                        "id": "logical-1", "start_ms": 12_000, "end_ms": 14_000,
                        "text": "Logical original", "speaker": "",
                    }],
                },
            }
        self._revision(
            "Logical edit", provenance={
                "source_reference_namespace": "logical_passage_ordinal"
            },
        )
        snapshot = self._snapshot()
        self.assertEqual(snapshot["source_reference_namespace"],
                         "logical_passage_ordinal")
        result = self.handlers.export(
            {"session_id": self.record.id,
             "settings": {"export_mode": "subtitles"},
             "display_subtitle_snapshot": snapshot},
            lambda *_: None, threading.Event(),
        )
        _artifact, output = self.artifacts.resolve(result["artifact_ids"][0])
        cues = parse_srt(output.read_text())
        self.assertEqual([cue.text for cue in cues], ["Logical edit"])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (12_000, 14_000))
        with self.database.session() as session:
            derived = session.scalar(select(Artifact).where(
                Artifact.role == "generation_display_subtitles"
            ))
            self.assertNotIn("logical_passages", derived.metadata_json)

    def test_real_export_uses_pinned_snapshot_and_isolates_dual_track(self):
        self._revision("Pinned display")
        snapshot = self._snapshot()
        self._revision("Later edit")
        translation = self._artifact(
            "translation.srt", SOURCE_SRT.replace("First cue", "Translated cue"),
            "translation", {"language": "de", "source_role": "translation"},
        )
        result = self.handlers.export(
            {
                "session_id": self.record.id,
                "settings": {"export_mode": "subtitles", "subtitle_selection": "dual"},
                "display_subtitle_snapshot": snapshot,
            },
            lambda *_: None, threading.Event(),
        )
        self.assertEqual(len(result["artifact_ids"]), 2)
        outputs = {}
        for artifact_id in result["artifact_ids"]:
            artifact, path = self.artifacts.resolve(artifact_id)
            outputs[artifact.role] = path.read_text(encoding="utf-8")
        self.assertIn("Pinned display", outputs["export_subtitle_source"])
        self.assertNotIn("Later edit", outputs["export_subtitle_source"])
        self.assertIn("Translated cue", outputs["export_subtitle_translation"])
        with self.database.session() as session:
            derived = session.scalar(select(Artifact).where(
                Artifact.role == "generation_display_subtitles"
            ))
            self.assertIsNotNone(derived)
            self.assertEqual(derived.metadata_json["original_parent_id"], self.display.id)
            parents = set(session.scalars(select(ArtifactEdge.parent_artifact_id).where(
                ArtifactEdge.child_artifact_id == derived.id
            )))
            self.assertEqual(parents, {self.display.id})
        self.assertEqual(self.artifacts.resolve(self.display.id)[1].read_text(), SOURCE_SRT)
        self.assertEqual(self.artifacts.resolve(translation.id)[1].read_text(),
                         SOURCE_SRT.replace("First cue", "Translated cue"))

    def test_explicit_null_does_not_resolve_current_plan(self):
        self._revision("Reviewed display")
        result = self.handlers.export(
            {"session_id": self.record.id,
             "settings": {"export_mode": "subtitles"},
             "display_subtitle_snapshot": None},
            lambda *_: None, threading.Event(),
        )
        _artifact, path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertIn("First cue", path.read_text())
        self.assertNotIn("Reviewed display", path.read_text())

    def test_vtt_and_text_exports_use_display_snapshot(self):
        self._revision("Reviewed display")
        snapshot = self._snapshot()
        for export_mode, subtitle_format in (
            ("subtitles", "vtt"), ("text", "srt")
        ):
            with self.subTest(export_mode=export_mode):
                result = self.handlers.export(
                    {
                        "session_id": self.record.id,
                        "settings": {
                            "export_mode": export_mode,
                            "subtitle_format": subtitle_format,
                        },
                        "display_subtitle_snapshot": snapshot,
                    },
                    lambda *_: None, threading.Event(),
                )
                _artifact, path = self.artifacts.resolve(result["artifact_ids"][0])
                self.assertIn("Reviewed display", path.read_text())
                self.assertNotIn("Speech only", path.read_text())


if __name__ == "__main__":
    unittest.main()
