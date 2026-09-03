import json
import tempfile
import unittest

from sqlalchemy import delete, event, select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, ArtifactEdge, SegmentLineage
from pandrator.web.schemas import SubtitleReviewRequest
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
        self.service = SubtitleReviewService(
            self.database, self.artifacts, lambda _session_id: self.session_dir
        )

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_initial_review_request_allows_revision_zero(self):
        payload = SubtitleReviewRequest.model_validate(
            {
                "expected_revision": 0,
                "segments": [
                    {
                        "start_ms": 0,
                        "end_ms": 2000,
                        "text": "Initial imported cue.",
                        "speaker": None,
                    }
                ],
            }
        )
        self.assertEqual(0, payload.expected_revision)

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
        self.handlers._store_srt_document(
            self.session.id, artifact, role, parent_artifact=parent
        )
        return artifact

    def test_first_import_can_create_a_subtitle_document_at_revision_zero(self):
        result = self.service.save_review(
            self.session.id,
            "transcription",
            0,
            [
                {
                    "start_ms": 0,
                    "end_ms": 1500,
                    "text": "Imported directly into a fresh subtitle session.",
                    "speaker": None,
                }
            ],
        )

        self.assertEqual(1, result["revision"])
        payload = self.service.documents(self.session.id)
        self.assertEqual(1, payload["stages"]["transcription"]["revision"])
        self.assertEqual(
            "Imported directly into a fresh subtitle session.",
            payload["stages"]["transcription"]["segments"][0]["text"],
        )

    def test_comparison_groups_splits_and_saving_creates_reviewed_revision(self):
        source = self._artifact(
            "source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n",
        )
        self._artifact(
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
                [
                    {
                        "start_ms": 0,
                        "end_ms": 2000,
                        "text": "Stale write",
                        "speaker": None,
                    }
                ],
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
            [
                item["speaker"]
                for item in payload["stages"]["transcription"]["segments"]
            ],
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
            revision_id = session.get(Artifact, artifact.id).metadata_json[
                "revision_id"
            ]
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
            [
                item["speaker"]
                for item in payload["stages"]["transcription"]["segments"]
            ],
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
            [
                {
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "Hello, beautiful world.",
                    "speaker": None,
                }
            ],
        )

        with self.database.session() as session:
            self.assertEqual("current", session.get(Artifact, source.id).state)
            self.assertEqual("stale", session.get(Artifact, correction.id).state)
            self.assertEqual("stale", session.get(Artifact, translation.id).state)
            reviewed = session.scalar(
                select(Artifact)
                .where(
                    Artifact.session_id == self.session.id,
                    Artifact.role == "correction",
                    Artifact.state == "current",
                )
                .order_by(Artifact.created_at.desc())
            )
            self.assertTrue(reviewed.metadata_json["reviewed"])

    def test_exact_review_loads_only_requested_historical_artifacts(self):
        source = self._artifact(
            "exact-source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
        )
        first = self._artifact(
            "first-correction.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nFirst correction.\n",
            parent=source,
        )
        second = self._artifact(
            "second-correction.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nSecond correction.\n",
            parent=source,
        )

        catalog = self.service.catalog(self.session.id)
        self.assertEqual(
            {source.id, first.id, second.id},
            {item["artifact_id"] for item in catalog["items"]},
        )
        payload = self.service.review(self.session.id, [first.id])

        self.assertEqual(first.id, payload["primary_artifact_id"])
        self.assertEqual(
            [first.id], [item["artifact_id"] for item in payload["columns"]]
        )
        self.assertEqual(
            "First correction.", payload["columns"][0]["segments"][0]["text"]
        )
        self.assertNotIn("Second correction.", json.dumps(payload))

    def test_exact_review_supports_same_stage_revisions_and_bounds_comparison(self):
        source = self._artifact(
            "same-stage-source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
        )
        first = self._artifact(
            "same-stage-one.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nOne.\n",
            parent=source,
        )
        second = self._artifact(
            "same-stage-two.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nTwo.\n",
            parent=source,
        )
        payload = self.service.review(self.session.id, [first.id, second.id])

        self.assertEqual(1, len(payload["rows"]))
        self.assertEqual("One.", payload["rows"][0]["cells"][first.id][0]["text"])
        self.assertEqual("Two.", payload["rows"][0]["cells"][second.id][0]["text"])
        with self.assertRaisesRegex(ValueError, "At most 4"):
            self.service.review(
                self.session.id, [source.id, first.id, second.id, "four", "five"]
            )

    def test_exact_review_compares_independent_transcription_revisions(self):
        first = self._artifact(
            "transcription-one.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nFirst engine result.\n",
        )
        second = self._artifact(
            "transcription-two.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nSecond engine result.\n",
        )

        catalog = self.service.catalog(self.session.id)
        transcription_ids = [
            item["artifact_id"]
            for item in catalog["items"]
            if item["stage"] == "transcription"
        ]
        payload = self.service.review(self.session.id, [first.id, second.id])

        self.assertEqual({first.id, second.id}, set(transcription_ids))
        self.assertEqual(
            ["transcription", "transcription"],
            [column["stage"] for column in payload["columns"]],
        )
        self.assertEqual(
            "First engine result.", payload["rows"][0]["cells"][first.id][0]["text"]
        )
        self.assertEqual(
            "Second engine result.",
            payload["rows"][0]["cells"][second.id][0]["text"],
        )

    def test_reviewing_historical_artifact_branches_from_that_exact_revision(self):
        source = self._artifact(
            "branch-source.srt",
            "transcription",
            "1\n00:00:00,000 --> 00:00:02,000\nOriginal.\n",
        )
        correction = self._artifact(
            "branch-correction.srt",
            "correction",
            "1\n00:00:00,000 --> 00:00:02,000\nCorrected.\n",
            parent=source,
        )
        exact = self.service.review(self.session.id, [correction.id])["columns"][0]

        result = self.service.save_review(
            self.session.id,
            "correction",
            exact["revision"],
            [
                {
                    "start_ms": 0,
                    "end_ms": 2000,
                    "text": "Reviewed branch.",
                    "speaker": None,
                }
            ],
            source_artifact_id=correction.id,
        )

        with self.database.session() as session:
            edge = session.get(ArtifactEdge, (correction.id, result["artifact_id"]))
            self.assertIsNotNone(edge)
            artifact = session.get(Artifact, result["artifact_id"])
            self.assertEqual(
                result["revision_id"], artifact.metadata_json["revision_id"]
            )

    def test_exact_review_uses_a_fixed_query_budget_independent_of_history(self):
        artifacts = [
            self._artifact(
                f"bounded-{index}.srt",
                "correction",
                f"1\n00:00:00,000 --> 00:00:02,000\nVersion {index}.\n",
            )
            for index in range(12)
        ]
        select_count = 0

        def count_selects(
            _connection, _cursor, statement, _parameters, _context, _executemany
        ):
            nonlocal select_count
            if statement.lstrip().upper().startswith("SELECT"):
                select_count += 1

        event.listen(self.database.engine, "before_cursor_execute", count_selects)
        try:
            payload = self.service.review(self.session.id, [artifacts[0].id])
        finally:
            event.remove(self.database.engine, "before_cursor_execute", count_selects)

        self.assertEqual("Version 0.", payload["columns"][0]["segments"][0]["text"])
        self.assertLessEqual(select_count, 4)


if __name__ == "__main__":
    unittest.main()
