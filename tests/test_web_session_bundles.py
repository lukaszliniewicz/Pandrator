import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from sqlalchemy import func, select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.bundles import SessionBundleService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, ArtifactEdge, SessionRecord
from pandrator.web.sessions import SessionService
from tests.web_test_support import prepare_web_test_data_root


class SessionBundleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.bundles = SessionBundleService(self.database, self.paths)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_round_trip_assigns_new_ids_and_preserves_files_and_dependencies(self):
        source_session = self.sessions.create("Portable", workflow_kind="voiceover", included_stages=["correct", "generate_audio"])
        directory = self.paths.sessions / source_session.storage_key
        directory.mkdir()
        source_path = directory / "source.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(source_path, kind="srt", role="transcription", session_id=source_session.id)
        corrected_path = directory / "corrected.srt"
        corrected_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello.\n", encoding="utf-8")
        corrected = self.artifacts.register(corrected_path, kind="srt", role="correction", session_id=source_session.id, parent_ids=[source.id])
        bundle_path = self.paths.root / "portable.pandrator-session"

        exported = self.bundles.export_bundle(source_session.id, bundle_path)
        imported = self.bundles.import_bundle(bundle_path, name="Portable copy")

        self.assertEqual(exported["artifacts"], 2)
        self.assertNotEqual(imported["session_id"], source_session.id)
        with self.database.session() as session:
            record = session.get(SessionRecord, imported["session_id"])
            self.assertEqual(record.name, "Portable copy")
            self.assertEqual(record.included_stages_json, ["correct", "generate_audio"])
            imported_artifacts = list(session.scalars(select(Artifact).where(Artifact.session_id == imported["session_id"])).all())
            self.assertEqual(len(imported_artifacts), 2)
            imported_ids = {item.id for item in imported_artifacts}
            edge = session.scalar(select(ArtifactEdge).where(ArtifactEdge.child_artifact_id.in_(imported_ids)))
            self.assertIn(edge.parent_artifact_id, imported_ids)
            self.assertIn(edge.child_artifact_id, imported_ids)
        for artifact in imported_artifacts:
            self.assertTrue(self.paths.managed_path(artifact.relative_path).is_file())

    def test_checksum_failure_does_not_create_a_session(self):
        malicious = self.paths.root / "bad.pandrator-session"
        manifest = {
            "format": "pandrator-session",
            "version": 1,
            "session": {"name": "Bad", "workflow_kind": "audiobook"},
            "artifacts": [{"source_id": "one", "kind": "text", "role": "upload", "size_bytes": 4, "sha256": "0" * 64, "archive_path": "files/one/source.txt"}],
            "artifact_edges": [],
        }
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("files/one/source.txt", b"text")
        with self.assertRaises(ValueError):
            self.bundles.import_bundle(malicious)
        with self.database.session() as session:
            self.assertEqual(session.scalar(select(func.count()).select_from(SessionRecord)), 0)

    def test_path_escape_is_rejected(self):
        malicious = self.paths.root / "escape.pandrator-session"
        manifest = {
            "format": "pandrator-session",
            "version": 1,
            "session": {"name": "Escape", "workflow_kind": "audiobook"},
            "artifacts": [{"source_id": "one", "kind": "text", "role": "upload", "size_bytes": 4, "sha256": "0" * 64, "archive_path": "../escape.txt"}],
            "artifact_edges": [],
        }
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("../escape.txt", b"text")
        with self.assertRaises(ValueError):
            self.bundles.import_bundle(malicious)
        self.assertFalse((self.paths.root.parent / "escape.txt").exists())


if __name__ == "__main__":
    unittest.main()
