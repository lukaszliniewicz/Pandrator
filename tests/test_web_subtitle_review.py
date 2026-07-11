import tempfile
import unittest

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.sessions import SessionService
from pandrator.web.subtitle_review import SubtitleReviewService
from pandrator.web.workflow_handlers import WorkflowHandlers


class SubtitleReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.temporary.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.session = self.sessions.create("Review", workflow_kind="subtitles")
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()
        self.service = SubtitleReviewService(self.database, self.artifacts, lambda _session_id: self.session_dir)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _artifact(self, name, role, content, parent=None):
        path = self.session_dir / name
        path.write_text(content, encoding="utf-8")
        artifact = self.artifacts.register(
            path,
            kind="srt",
            role=role,
            session_id=self.session.id,
            parent_ids=[parent.id] if parent else [],
        )
        self.handlers._store_srt_document(self.session.id, artifact, role, parent_artifact=parent)
        return artifact

    def test_comparison_groups_splits_and_saving_creates_reviewed_revision(self):
        source = self._artifact(
            "source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n",
        )
        correction = self._artifact(
            "corrected.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:01,000\nHello,\n\n2\n00:00:01,000 --> 00:00:02,000\nworld.\n",
            parent=source,
        )
        payload = self.service.documents(self.session.id)
        self.assertEqual(len(payload["rows"]), 1)
        self.assertEqual(len(payload["rows"][0]["correction"]), 2)
        self.assertTrue(payload["rows"][0]["changed"])

        revision = payload["stages"]["correction"]["revision"]
        result = self.service.save_review(
            self.session.id,
            "correction",
            revision,
            [{"start_ms": 0, "end_ms": 2000, "text": "Hello, world.", "speaker": None}],
        )
        self.assertEqual(result["revision"], revision + 1)
        reviewed = self.service.documents(self.session.id)
        self.assertTrue(reviewed["stages"]["correction"]["reviewed"])
        self.assertEqual(len(reviewed["stages"]["correction"]["segments"]), 1)
        with self.assertRaises(RuntimeError):
            self.service.save_review(
                self.session.id,
                "correction",
                revision,
                [{"start_ms": 0, "end_ms": 2000, "text": "Stale write", "speaker": None}],
            )


if __name__ == "__main__":
    unittest.main()
