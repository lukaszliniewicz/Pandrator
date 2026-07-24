import json
import tempfile
import unittest

from sqlalchemy import delete, select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, SegmentLineage
from pandrator.web.sessions import SessionService
from pandrator.web.subtitle_review import SubtitleReviewService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class SubtitleReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
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

    def test_legacy_temporal_alignment_groups_many_to_one_without_lineage(self):
        source = self._artifact(
            "legacy-source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:01,000\nGood\n\n2\n00:00:01,000 --> 00:00:02,000\nmorning.\n",
        )
        self._artifact(
            "legacy-correction.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nGood morning.\n",
            parent=source,
        )
        with self.database.session() as session:
            session.execute(delete(SegmentLineage))

        payload = self.service.documents(self.session.id)

        self.assertEqual(1, len(payload["rows"]))
        self.assertEqual(2, len(payload["rows"][0]["transcription"]))
        self.assertEqual(1, len(payload["rows"][0]["correction"]))

    def test_legacy_labels_become_structured_metadata_and_propagate(self):
        source = self._artifact(
            "diarized-source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:01,000\n[SPEAKER_0]: Hello.\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\n[Speaker 1] Welcome.\n",
        )
        self._artifact(
            "diarized-correction.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\nWelcome.\n",
            parent=source,
        )

        payload = self.service.documents(self.session.id)

        self.assertEqual(
            [item["text"] for item in payload["stages"]["transcription"]["segments"]],
            ["Hello.", "Welcome."],
        )
        self.assertEqual(
            [item["speaker"] for item in payload["stages"]["transcription"]["segments"]],
            ["SPEAKER_0", "Speaker 1"],
        )
        self.assertEqual(
            [item["speaker"] for item in payload["stages"]["correction"]["segments"]],
            ["SPEAKER_0", "Speaker 1"],
        )

    def test_timed_transcript_populates_plain_cue_speakers(self):
        artifact = self._artifact(
            "plain-transcription.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:01,000\nHello.\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\nWelcome.\n",
        )
        with self.database.session() as session:
            revision_id = session.get(Artifact, artifact.id).metadata_json["revision_id"]
        metadata_path = self.session_dir / "plain-transcription-words.json"
        metadata_path.write_text(
            json.dumps(
                {
                    "transcription": [
                        {
                            "speaker": "Speaker 0",
                            "offsets": {"from": 0, "to": 1000},
                            "text": "Hello.",
                            "words": [
                                {
                                    "speaker": "Speaker 0",
                                    "text": "Hello.",
                                    "offsets": {"from": 0, "to": 700},
                                }
                            ],
                        },
                        {
                            "speaker": "Speaker 1",
                            "offsets": {"from": 1100, "to": 2000},
                            "text": "Welcome.",
                            "words": [
                                {
                                    "speaker": "Speaker 1",
                                    "text": "Welcome.",
                                    "offsets": {"from": 1100, "to": 1800},
                                }
                            ],
                        },
                    ]
                }
            ),
            encoding="utf-8",
        )

        self.handlers._store_timed_words(revision_id, metadata_path)
        payload = self.service.documents(self.session.id)

        self.assertEqual(
            [item["speaker"] for item in payload["stages"]["transcription"]["segments"]],
            ["Speaker 0", "Speaker 1"],
        )

    def test_review_rejects_a_cue_that_crosses_speakers(self):
        self._artifact(
            "speaker-boundaries.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:01,000\n[SPEAKER_0]: Hello.\n\n"
            "2\n00:00:01,000 --> 00:00:02,000\n[SPEAKER_1]: Welcome.\n",
        )
        payload = self.service.documents(self.session.id)

        with self.assertRaisesRegex(ValueError, "crosses a speaker boundary"):
            self.service.save_review(
                self.session.id,
                "transcription",
                payload["stages"]["transcription"]["revision"],
                [
                    {
                        "start_ms": 0,
                        "end_ms": 2000,
                        "text": "Hello. Welcome.",
                        "speaker": "SPEAKER_0",
                    }
                ],
            )

    def test_reviewed_upstream_revision_stales_only_derived_artifacts(self):
        source = self._artifact(
            "source-for-descendants.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n",
        )
        correction = self._artifact(
            "correction-for-descendants.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nHello, world.\n",
            parent=source,
        )
        translation = self._artifact(
            "translation-child.srt",
            "translation",
            "1\n00:00:00,000 --> 00:00:02,000\nWitaj, świecie.\n",
            parent=correction,
        )
        payload = self.service.documents(self.session.id)

        self.service.save_review(
            self.session.id,
            "correction",
            payload["stages"]["correction"]["revision"],
            [{"start_ms": 0, "end_ms": 2000, "text": "Hello, beautiful world.", "speaker": None}],
        )

        with self.database.session() as session:
            self.assertEqual("current", session.get(Artifact, source.id).state)
            self.assertEqual("stale", session.get(Artifact, correction.id).state)
            self.assertEqual("stale", session.get(Artifact, translation.id).state)
            reviewed = session.scalar(
                select(Artifact)
                .where(Artifact.session_id == self.session.id, Artifact.role == "correction", Artifact.state == "current")
                .order_by(Artifact.created_at.desc())
            )
            self.assertTrue(reviewed.metadata_json["reviewed"])


if __name__ == "__main__":
    unittest.main()
