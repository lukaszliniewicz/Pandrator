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
    OutputAssembly,
    SessionStageSelection,
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

    def artifact(self, name="assemblies/mix.wav", role="assembled_audio", parents=None):
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"independently materialized output fixture")
        artifact = self.artifacts.register(
            path, kind="audio", role=role, session_id=self.record.id,
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
            self.assertEqual("deleted", session.get(Artifact, assembly.id).state)
            self.assertEqual("current", session.get(Artifact, exported.id).state)
            self.assertIsNotNone(session.scalar(select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == assembly.id,
                ArtifactEdge.child_artifact_id == exported.id,
            )))
            row = session.get(OutputAssembly, assembly_row_id)
            self.assertEqual("stale", row.status)
            self.assertIsNone(row.artifact_id)
            selection = session.get(SessionStageSelection, (self.record.id, "audio"))
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
            self.assertEqual("current", session.get(Artifact, master.id).state)
            self.assertEqual("current", session.get(Artifact, exported.id).state)
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
        artifact, path = self.artifact()
        JobQueue(self.database).enqueue(
            "export.variant", {"session_id": self.record.id}, session_id=self.record.id
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

    def test_live_intermediate_child_blocks_removal(self):
        artifact, path = self.artifact()
        self.artifact("converted.wav", "rvc_audio", [artifact.id])
        with self.assertRaisesRegex(ValueError, "intermediate results"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())

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
            session.get(Artifact, artifact.id).relative_path = path.relative_to(self.paths.root).as_posix()
        with self.assertRaisesRegex(ValueError, "own session storage"):
            self.artifacts.remove_output(self.record.id, artifact.id)
        self.assertTrue(path.exists())
        self.assertTrue(original.exists())

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
            self.assertEqual("deleted", session.get(Artifact, artifact.id).state)

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
