"""Regression coverage for output cleanup and non-duplicating subtitle exports."""

import tempfile
import threading
import unittest
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    AudioTake,
    ExportRecord,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    OutputAssembly,
    SessionStageSelection,
    Voice,
    VoiceSample,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import SourceLibraryService
from tests.web_test_support import prepare_web_test_data_root


class SessionOutputStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.record = self.sessions.create("Output storage", workflow_kind="voiceover")
        self.directory = self.paths.sessions / self.record.storage_key
        self.directory.mkdir()
        self.artifacts = ArtifactService(self.database, self.paths)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def artifact(
        self,
        name="assemblies/mix.wav",
        role="assembled_audio",
        parents=None,
        kind="audio",
        record=None,
    ):
        target = record or self.record
        directory = self.paths.sessions / target.storage_key
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"independently materialized output fixture")
        artifact = self.artifacts.register(
            path,
            kind=kind,
            role=role,
            session_id=target.id,
            parent_ids=parents or [],
        )
        return artifact, path

    def test_deleting_assembly_preserves_finished_export_and_provenance(self):
        assembly, path = self.artifact()
        exported, final_path = self.artifact(
            "exports/video/final.mp4", "export_video", [assembly.id]
        )
        before = final_path.read_bytes()
        with self.database.session() as session:
            row = OutputAssembly(
                session_id=self.record.id, artifact_id=assembly.id, status="completed"
            )
            session.add(row)
            session.add(SessionStageSelection(
                session_id=self.record.id, stage_key="audio",
                artifact_id=assembly.id, revision=3,
            ))
            session.flush()
            assembly_row_id = row.id
        self.artifacts.remove_output(self.record.id, assembly.id)
        self.assertFalse(path.exists())
        self.assertEqual(before, final_path.read_bytes())
        with self.database.session() as session:
            assembly_artifact = session.get(Artifact, assembly.id)
            export_artifact = session.get(Artifact, exported.id)
            assert assembly_artifact is not None and export_artifact is not None
            self.assertEqual("deleted", assembly_artifact.state)
            self.assertEqual("current", export_artifact.state)
            self.assertIsNotNone(session.scalar(select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == assembly.id,
                ArtifactEdge.child_artifact_id == exported.id,
            )))
            row = session.get(OutputAssembly, assembly_row_id)
            assert row is not None
            self.assertEqual("stale", row.status)
            self.assertIsNone(row.artifact_id)
            selection = session.get(SessionStageSelection, (self.record.id, "audio"))
            assert selection is not None
            self.assertIsNone(selection.artifact_id)
            self.assertEqual(4, selection.revision)

    def test_deleting_assembly_preserves_cached_soundtrack_and_its_export(self):
        assembly, assembly_path = self.artifact()
        master, master_path = self.artifact(
            "soundtracks/cached.wav", "soundtrack_master", [assembly.id]
        )
        exported, export_path = self.artifact(
            "exports/audio/final.wav", "export_mixed_audio", [master.id]
        )
        master_bytes, export_bytes = master_path.read_bytes(), export_path.read_bytes()

        self.artifacts.remove_output(self.record.id, assembly.id)

        self.assertFalse(assembly_path.exists())
        self.assertEqual(master_bytes, master_path.read_bytes())
        self.assertEqual(export_bytes, export_path.read_bytes())
        with self.database.session() as session:
            master_artifact = session.get(Artifact, master.id)
            export_artifact = session.get(Artifact, exported.id)
            assert master_artifact is not None and export_artifact is not None
            self.assertEqual("current", master_artifact.state)
            self.assertEqual("current", export_artifact.state)
            self.assertIsNotNone(session.scalar(select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == assembly.id,
                ArtifactEdge.child_artifact_id == master.id,
            )))

    def test_legacy_audio_roles_and_root_paths_remain_removable(self):
        for role in ("assembled_audio", "audiobook_audio", "dubbing_audio"):
            with self.subTest(role=role):
                artifact, path = self.artifact(f"{role}.wav", role)
                self.artifacts.remove_output(self.record.id, artifact.id)
                self.assertFalse(path.exists())

    def test_queued_export_blocks_removal(self):
        JobQueue(self.database).enqueue(
            "export.variant", {"session_id": self.record.id}, session_id=self.record.id
        )
        for kind, role in (
            ("audio", "assembled_audio"),
            ("export", "delivery"),
            ("audio", "export"),
            ("audio", "export_video"),
        ):
            with self.subTest(kind=kind, role=role):
                artifact, path = self.artifact(
                    f"queued-job-guard/{kind}-{role}.wav", role, kind=kind
                )
                with self.assertRaisesRegex(ValueError, "assembly or export job"):
                    self.artifacts.remove_output(self.record.id, artifact.id)
                self.assertTrue(path.exists())

    def test_source_reference_blocks_removal(self):
        artifact, path = self.artifact()
        SourceLibraryService(self.database, self.artifacts).ensure_for_artifact(artifact.id)
        with self.assertRaisesRegex(ValueError, "session source"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())

    def test_trashed_sources_remain_restorable_for_each_output_classification(self):
        library = SourceLibraryService(self.database, self.artifacts)
        for index, (kind, role) in enumerate((
            ("audio", "assembled_audio"), ("export", "file"),
            ("audio", "export"), ("video", "export_video"),
        )):
            with self.subTest(kind=kind, role=role):
                artifact, path = self.artifact(f"trash-guard/{index}.bin", role, kind=kind)
                original = path.read_bytes()
                asset = library.ensure_for_artifact(artifact.id)
                trashed = library.set_state(asset.id, asset.revision, "trashed")
                with self.assertRaisesRegex(ValueError, "session source"):
                    self.artifacts.remove_output(self.record.id, artifact.id)
                restored = library.set_state(asset.id, trashed["revision"], "current")
                self.assertEqual("current", restored["state"])
                self.assertEqual(original, path.read_bytes())

    def test_live_intermediate_child_blocks_removal(self):
        for kind, role in (
            ("audio", "assembled_audio"),
            ("export", "delivery"),
            ("audio", "export"),
            ("audio", "export_video"),
        ):
            with self.subTest(kind=kind, role=role):
                artifact, path = self.artifact(
                    f"intermediate-guard/{kind}-{role}.wav", role, kind=kind
                )
                self.artifact(
                    f"intermediate-guard/{kind}-{role}-converted.wav",
                    "rvc_audio",
                    [artifact.id],
                )
                with self.assertRaisesRegex(ValueError, "intermediate results"):
                    self.artifacts.remove_output(self.record.id, artifact.id)
                self.assertTrue(path.exists())

    def test_each_output_classification_is_blocked_by_a_live_source_asset(self):
        classifications = (
            ("export", "file"),
            ("audio", "export"),
            ("audio", "export_video"),
        )
        for index, (kind, role) in enumerate(classifications):
            with self.subTest(kind=kind, role=role):
                artifact, path = self.artifact(f"source-guard/{index}.wav", role, kind=kind)
                SourceLibraryService(self.database, self.artifacts).ensure_for_artifact(artifact.id)
                with self.assertRaisesRegex(ValueError, "session source"):
                    self.artifacts.remove_output(self.record.id, artifact.id)
                self.assertTrue(path.exists())

    def test_export_take_and_voice_sample_references_block_removal(self):
        take_artifact, take_path = self.artifact(
            "referenced/take.wav", "export_audio", kind="export"
        )
        with self.database.session() as session:
            plan = GenerationPlan(session_id=self.record.id)
            session.add(plan)
            session.flush()
            revision = GenerationPlanRevision(
                plan_id=plan.id,
                revision_number=1,
                content_hash="fixture-generation-plan",
            )
            session.add(revision)
            session.flush()
            segment = GenerationSegment(
                plan_revision_id=revision.id,
                ordinal=0,
                text="fixture take reference",
            )
            session.add(segment)
            session.flush()
            session.add(
                AudioTake(
                    generation_segment_id=segment.id,
                    artifact_id=take_artifact.id,
                    status="completed",
                )
            )
        with self.assertRaisesRegex(ValueError, "generation take"):
            self.artifacts.remove_output(self.record.id, take_artifact.id)
        self.assertTrue(take_path.exists())

        sample_artifact, sample_path = self.artifact(
            "referenced/sample.wav", "export_audio", kind="export"
        )
        with self.database.session() as session:
            workspace_id = session.execute(
                select(Artifact.workspace_id).where(Artifact.id == sample_artifact.id)
            ).scalar_one()
            voice = Voice(workspace_id=workspace_id, name="fixture sample voice")
            session.add(voice)
            session.flush()
            session.add(
                VoiceSample(
                    workspace_id=workspace_id,
                    voice_id=voice.id,
                    artifact_id=sample_artifact.id,
                )
            )
        with self.assertRaisesRegex(ValueError, "voice sample"):
            self.artifacts.remove_output(self.record.id, sample_artifact.id)
        self.assertTrue(sample_path.exists())

    def test_cross_session_selection_blocks_removal(self):
        artifact, path = self.artifact()
        other = self.sessions.create("Other session")
        with self.database.session() as session:
            session.add(SessionStageSelection(
                session_id=other.id, stage_key="audio", artifact_id=artifact.id,
            ))
        with self.assertRaisesRegex(ValueError, "another session"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())

    def test_wrong_session_and_non_output_are_not_removable(self):
        artifact, path = self.artifact(role="upload")
        with self.assertRaises(ValueError):
            self.artifacts.remove_output(self.record.id, artifact.id)
        with self.assertRaises(KeyError):
            self.artifacts.remove_output("wrong-session", artifact.id)
        self.assertTrue(path.exists())

    def test_other_session_storage_is_not_removable(self):
        artifact, original = self.artifact()
        other = self.sessions.create("Other storage")
        path = self.paths.sessions / other.storage_key / "other.wav"
        path.parent.mkdir()
        path.write_bytes(b"do not delete")
        with self.database.session() as session:
            artifact_row = session.get(Artifact, artifact.id)
            assert artifact_row is not None
            artifact_row.relative_path = path.relative_to(self.paths.root).as_posix()
        with self.assertRaisesRegex(ValueError, "own session storage"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())
        self.assertTrue(original.exists())

    def test_current_cross_session_output_reference_blocks_removal(self):
        artifact, path = self.artifact()
        other = self.sessions.create("Other reference session")
        child, _child_path = self.artifact(
            "current-child.wav",
            "export_video",
            kind="export",
            record=other,
        )
        with self.database.session() as session:
            session.add(
                ArtifactEdge(
                    parent_artifact_id=artifact.id,
                    child_artifact_id=child.id,
                )
            )
        with self.assertRaisesRegex(ValueError, "another session"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())

    def test_active_assembly_blocks_export_removal(self):
        artifact, path = self.artifact("active-assembly/export.wav", "export_video", kind="export")
        with self.database.session() as session:
            session.add(
                OutputAssembly(
                    session_id=self.record.id,
                    artifact_id=artifact.id,
                    status="running",
                )
            )
        with self.assertRaisesRegex(ValueError, "assembly using this output"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())

    def test_current_cross_session_assembly_reference_blocks_export_removal(self):
        artifact, path = self.artifact(
            "cross-session-assembly/export.wav", "export_video", kind="export"
        )
        other = self.sessions.create("Other assembly reference")
        with self.database.session() as session:
            session.add(
                OutputAssembly(
                    session_id=other.id,
                    artifact_id=artifact.id,
                    status="completed",
                )
            )
        with self.assertRaisesRegex(ValueError, "another session"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())

    def test_historical_intermediate_edge_does_not_block_removal(self):
        artifact, path = self.artifact()
        child, child_path = self.artifact("historical-intermediate.wav", "rvc_audio", [artifact.id])
        with self.database.session() as session:
            child_row = session.get(Artifact, child.id)
            assert child_row is not None
            child_row.state = "stale"

        self.artifacts.remove_output(self.record.id, artifact.id)

        self.assertFalse(path.exists())
        self.assertTrue(child_path.exists())
        with self.database.session() as session:
            child_row = session.get(Artifact, child.id)
            assert child_row is not None
            self.assertEqual("stale", child_row.state)
            self.assertIsNotNone(
                session.scalar(
                    select(ArtifactEdge).where(
                        ArtifactEdge.parent_artifact_id == artifact.id,
                        ArtifactEdge.child_artifact_id == child.id,
                    )
                )
            )

    def test_export_in_foreign_session_storage_is_not_removable(self):
        artifact, original = self.artifact("exports/final.wav", "export_video", kind="export")
        other = self.sessions.create("Other export storage")
        foreign = self.paths.sessions / other.storage_key / "export.wav"
        foreign.parent.mkdir()
        foreign.write_bytes(b"do not delete")
        with self.database.session() as session:
            artifact_row = session.get(Artifact, artifact.id)
            assert artifact_row is not None
            artifact_row.relative_path = foreign.relative_to(self.paths.root).as_posix()
        with self.assertRaisesRegex(ValueError, "own session storage"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(foreign.exists())
        self.assertTrue(original.exists())

    def test_export_symlink_to_another_artifact_is_not_removable(self):
        artifact, path = self.artifact("exports/final.wav", "export_video", kind="export")
        _source, source_path = self.artifact("original.wav", "upload")
        path.unlink()
        path.symlink_to(source_path)
        with self.assertRaisesRegex(ValueError, "same file"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(source_path.exists())
        self.assertTrue(path.is_symlink())

    def test_unreferenced_export_deletion_retires_all_own_references(self):
        artifact, path = self.artifact("exports/final.wav", "export_video", kind="export")
        with self.database.session() as session:
            record = ExportRecord(
                session_id=self.record.id,
                artifact_id=artifact.id,
                options_json={},
                status="completed",
            )
            assembly = OutputAssembly(
                session_id=self.record.id, artifact_id=artifact.id, status="completed"
            )
            selection = SessionStageSelection(
                session_id=self.record.id,
                stage_key="export",
                artifact_id=artifact.id,
                revision=7,
            )
            session.add_all([record, assembly, selection])
            session.flush()
            record_id, assembly_id = record.id, assembly.id

        self.artifacts.remove_output(self.record.id, artifact.id)

        self.assertFalse(path.exists())
        with self.database.session() as session:
            retired = session.get(OutputAssembly, assembly_id)
            artifact_row = session.get(Artifact, artifact.id)
            export_row = session.get(ExportRecord, record_id)
            selection = session.get(SessionStageSelection, (self.record.id, "export"))
            assert artifact_row is not None
            assert export_row is not None
            assert retired is not None
            assert selection is not None
            self.assertEqual("deleted", artifact_row.state)
            self.assertEqual("deleted", export_row.status)
            self.assertEqual("stale", retired.status)
            self.assertIsNone(retired.artifact_id)
            self.assertIsNone(selection.artifact_id)
            self.assertEqual(8, selection.revision)

    def test_symlink_to_another_managed_artifact_is_not_deleted(self):
        artifact, path = self.artifact()
        _source, source_path = self.artifact("original.wav", "upload")
        path.unlink()
        path.symlink_to(source_path)
        with self.assertRaisesRegex(ValueError, "same file"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(source_path.exists())
        self.assertTrue(path.is_symlink())

    def test_missing_audio_can_be_retired(self):
        artifact, path = self.artifact()
        path.unlink()
        self.artifacts.remove_output(self.record.id, artifact.id)
        with self.database.session() as session:
            artifact_row = session.get(Artifact, artifact.id)
            assert artifact_row is not None
            self.assertEqual("deleted", artifact_row.state)

    def subtitle_source(self):
        record = self.sessions.create("Subtitle storage", workflow_kind="subtitles")
        directory = self.paths.sessions / record.storage_key
        directory.mkdir()
        path = directory / "source.srt"
        path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello world\n", encoding="utf-8")
        source = self.artifacts.register(
            path, kind="source", role="upload", session_id=record.id,
        )
        return record, directory, source, path

    def export(self, record, source, **settings):
        return WorkflowHandlers(self.database, self.paths).export(
            {"session_id": record.id, "source_artifact_id": source.id,
             "settings": {"subtitle_selection": "source", **settings}},
            lambda *_args: None, threading.Event(),
        )

    def test_each_explicit_subtitle_format_creates_only_one_deliverable(self):
        for mode, format_name, extension in (
            ("subtitles", "srt", ".srt"),
            ("subtitles", "vtt", ".vtt"),
            ("text", "srt", ".txt"),
        ):
            with self.subTest(mode=mode, format=format_name):
                record, directory, source, source_path = self.subtitle_source()
                original = source_path.read_bytes()
                result = self.export(record, source, export_mode=mode, subtitle_format=format_name)
                self.assertEqual(1, len(result["artifact_ids"]))
                artifact, path = self.artifacts.resolve(result["artifact_ids"][0])
                self.assertEqual(directory / "exports" / "subtitles", path.parent)
                self.assertEqual(extension, path.suffix)
                files = [p for p in (directory / "exports").rglob("*") if p.is_file()]
                self.assertEqual([path], files)
                self.assertEqual(original, source_path.read_bytes())
                with self.database.session() as session:
                    self.assertFalse(session.scalars(select(Artifact).where(
                        Artifact.session_id == record.id,
                        Artifact.role.startswith("final_subtitle_"),
                    )).all())
                    edge = session.scalar(select(ArtifactEdge).where(
                        ArtifactEdge.child_artifact_id == artifact.id
                    ))
                    assert edge is not None
                    self.assertEqual(source.id, edge.parent_artifact_id)

    def test_conversion_failure_cleans_its_scratch_subtitle(self):
        record, directory, source, source_path = self.subtitle_source()
        with patch("pandrator.logic.dubbing.srt_utils.srt_to_vtt", side_effect=ValueError("conversion failed")):
            with self.assertRaisesRegex(ValueError, "conversion failed"):
                self.export(record, source, subtitle_format="vtt")
        self.assertTrue(source_path.exists())
        self.assertFalse([p for p in (directory / "exports").rglob("*") if p.is_file()])


if __name__ == "__main__":
    unittest.main()
