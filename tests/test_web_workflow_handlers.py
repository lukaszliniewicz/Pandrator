import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pydub import AudioSegment
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.models import Artifact
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers


class WebWorkflowHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.temporary.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.session = self.sessions.create("Workflow fixture")
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    @staticmethod
    def progress(_value, _detail=None):
        return None

    def test_rerunning_an_upstream_role_marks_previous_descendants_stale(self):
        first_source = self.paths.uploads / "first.txt"
        first_source.write_text("First", encoding="utf-8")
        source = self.artifacts.register(first_source, kind="source", role="upload", session_id=self.session.id)
        first_output = self.session_dir / "cleaned-one.txt"
        first_output.write_text("One", encoding="utf-8")
        cleaned = self.artifacts.register(first_output, kind="text", role="clean_text", session_id=self.session.id, parent_ids=[source.id])
        prepared_path = self.session_dir / "prepared-one.json"
        prepared_path.write_text("[]", encoding="utf-8")
        prepared = self.artifacts.register(prepared_path, kind="json", role="prepared_text", session_id=self.session.id, parent_ids=[cleaned.id])

        second_output = self.session_dir / "cleaned-two.txt"
        second_output.write_text("Two", encoding="utf-8")
        self.artifacts.register(second_output, kind="text", role="clean_text", session_id=self.session.id, parent_ids=[source.id])

        with self.database.session() as session:
            self.assertEqual(session.get(Artifact, cleaned.id).state, "stale")
            self.assertEqual(session.get(Artifact, prepared.id).state, "stale")

    def test_audiobook_audio_uses_the_shared_tts_engine_and_registers_output(self):
        prepared_path = self.session_dir / "prepared.json"
        prepared_path.write_text(
            json.dumps([{"original_sentence": "First."}, {"original_sentence": "Second."}]),
            encoding="utf-8",
        )
        prepared = self.artifacts.register(prepared_path, kind="json", role="prepared_text", session_id=self.session.id)
        with mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=25)) as generate:
            result = self.handlers.generate_audiobook_audio(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": prepared.id,
                    "settings": {"service": "XTTS", "max_attempts": 1},
                },
                self.progress,
                threading.Event(),
            )
        self.assertEqual(generate.call_count, 2)
        artifact, output = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual(artifact.role, "audiobook_audio")
        self.assertTrue(output.is_file())

    def test_subtitle_only_export_does_not_require_tts(self):
        subtitle_session = self.sessions.create("Subtitle fixture", workflow_kind="subtitles")
        subtitle_session_dir = self.paths.sessions / subtitle_session.storage_key
        subtitle_session_dir.mkdir()
        srt_path = self.paths.uploads / "captions.srt"
        srt_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        uploaded = self.artifacts.register(
            srt_path,
            kind="source",
            role="upload",
            session_id=subtitle_session.id,
            metadata={"original_filename": "captions.srt"},
        )
        result = self.handlers.export(
            {
                "session_id": subtitle_session.id,
                "source_artifact_id": uploaded.id,
                "settings": {"subtitle_selection": "source", "subtitle_mode": "none"},
            },
            self.progress,
            threading.Event(),
        )
        self.assertEqual(len(result["artifact_ids"]), 1)
        with self.database.session() as session:
            exported = session.scalar(select(Artifact).where(Artifact.id == result["artifact_ids"][0]))
            self.assertEqual(exported.role, "export_upload")


if __name__ == "__main__":
    unittest.main()
