import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web.artifact_selection import selected_artifacts
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    DocumentRevision,
    Segment,
    SessionSource,
    SourceAsset,
    TimedWord,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class MediaEditTranscriptionHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.sessions = SessionService(self.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.session = self.sessions.create(
            "Media edit transcription",
            workflow_kind="media_edit",
        )

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    @staticmethod
    def progress(_value, _detail=None):
        return None

    def _artifact(self, name, role, content, kind):
        path = self.paths.root / name
        path.write_text(content, encoding="utf-8")
        return self.artifacts.register(
            path,
            kind=kind,
            role=role,
            session_id=self.session.id,
        )

    def _attach(self, artifact, role, kind):
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
                    session_id=self.session.id,
                    source_asset_id=asset.id,
                    role=role,
                    is_current=True,
                )
            )

    def _source_and_caption(self, *, attach_caption=True):
        source = self._artifact("source.mp4", "upload", "media", "video")
        self._attach(source, "primary", "video")
        caption = self._artifact(
            "captions.srt",
            "captions",
            (
                "1\n00:00:01,000 --> 00:00:02,000\n"
                "Alice: Hello, world!\n"
            ),
            "srt",
        )
        if attach_caption:
            self._attach(caption, "transcript", "srt")
        return source, caption

    def _mock_transcription(self, source_path):
        raw_srt = source_path.parent / "raw.srt"
        raw_srt.write_text(
            "1\n00:00:01,100 --> 00:00:01,900\nHELLO WORLD\n",
            encoding="utf-8",
        )
        raw_json = source_path.parent / "raw.json"
        raw_json.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "raw",
                            "start_ms": 1100,
                            "end_ms": 1900,
                            "text": "HELLO WORLD",
                            "words": [
                                {"text": "HELLO", "start_ms": 1100, "end_ms": 1400},
                                {"text": "WORLD", "start_ms": 1500, "end_ms": 1900},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            srt_path=str(raw_srt),
            word_timestamps_path=str(raw_json),
            engine="test-asr",
            compute_backend="cpu",
        )

    def test_attached_caption_is_authoritative_and_raw_asr_is_evidence(self):
        source, _caption = self._source_and_caption()
        fake_result = self._mock_transcription(self.paths.root / "source.mp4")
        with patch(
            "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
            return_value=fake_result,
        ):
            result = self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {},
                },
                self.progress,
                threading.Event(),
            )

        with self.database.session() as session:
            transcription = session.get(Artifact, result["artifact_id"])
            word_artifact = session.get(
                Artifact, result["word_timestamps_artifact_id"]
            )
            roles = {
                item.role: item
                for item in session.scalars(
                    select(Artifact).where(Artifact.session_id == self.session.id)
                )
            }
            segments = list(
                session.scalars(
                    select(Segment)
                    .join(DocumentRevision, Segment.revision_id == DocumentRevision.id)
                    .where(DocumentRevision.id == result["revision_id"])
                    .order_by(Segment.ordinal)
                )
            )
            timed_words = list(
                session.scalars(
                    select(TimedWord).where(
                        TimedWord.revision_id == result["revision_id"]
                    )
                )
            )
            edges = set(
                session.execute(
                    select(
                        ArtifactEdge.parent_artifact_id,
                        ArtifactEdge.child_artifact_id,
                    )
                )
            )
            selected = selected_artifacts(session, self.session.id)

        self.assertEqual("transcription", transcription.role)
        self.assertEqual(
            "1\n00:00:01,100 --> 00:00:01,900\nHello, world!\n",
            self.artifacts.resolve(transcription.id)[1].read_text(encoding="utf-8"),
        )
        self.assertEqual("asr_lexical_projection", result["alignment_method"])
        self.assertEqual(2, result["word_count"])
        self.assertEqual("asr_lexical_projection", transcription.metadata_json["alignment_method"])
        payload = json.loads(
            self.artifacts.resolve(word_artifact.id)[1].read_text(encoding="utf-8")
        )
        self.assertEqual("Hello,", payload["segments"][0]["words"][0]["text"])
        self.assertEqual("Alice", payload["segments"][0]["words"][0]["speaker"])
        self.assertEqual("Alice", segments[0].speaker)
        self.assertEqual(["Hello,", "world!"], [word.text for word in timed_words])
        self.assertEqual(["Alice", "Alice"], [word.speaker for word in timed_words])
        self.assertIn("transcription", roles)
        self.assertIn("transcription_evidence", roles)
        self.assertIn("recognition_word_timestamps", roles)
        self.assertEqual(transcription.id, selected["transcribe"].id)
        self.assertNotIn(roles["transcription_evidence"].id, selected.values())
        self.assertIn((source.id, roles["transcription_evidence"].id), edges)
        self.assertIn((source.id, word_artifact.id), edges)
        self.assertIn((roles["transcription_evidence"].id, word_artifact.id), edges)

    def test_media_edit_transcription_failure_does_not_select_raw_asr(self):
        source, caption = self._source_and_caption()
        caption_path = self.artifacts.resolve(caption.id)[1]
        caption_path.write_text(
            "1\n00:00:01,000 --> 00:00:02,000\nNo lexical match\n",
            encoding="utf-8",
        )
        fake_result = self._mock_transcription(self.paths.root / "source.mp4")
        with patch(
            "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
            return_value=fake_result,
        ), self.assertRaises(ValueError):
            self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {},
                },
                self.progress,
                threading.Event(),
            )

        with self.database.session() as session:
            roles = {
                item.role
                for item in session.scalars(
                    select(Artifact).where(Artifact.session_id == self.session.id)
                )
            }
        self.assertIn("transcription_evidence", roles)
        self.assertIn("recognition_word_timestamps", roles)
        self.assertNotIn("transcription", roles)

    def test_media_edit_without_caption_keeps_existing_asr_path(self):
        source, _caption = self._source_and_caption(attach_caption=False)
        fake_result = self._mock_transcription(self.paths.root / "source.mp4")
        with patch(
            "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
            return_value=fake_result,
        ):
            result = self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {},
                },
                self.progress,
                threading.Event(),
            )

        with self.database.session() as session:
            artifact = session.get(Artifact, result["artifact_id"])
        self.assertEqual("transcription", artifact.role)
        self.assertEqual(2, result["word_count"])

    def test_media_edit_render_registers_canonical_words_and_cue_owners(self):
        source = self._artifact("render-source.mp4", "upload", "media", "video")
        revision = {
            "reviewed": True,
            "plan_id": "plan-1",
            "revision_id": "revision-1",
            "source_media_artifact": {"id": source.id},
            "keep_ranges": [
                {"id": "keep", "start_ms": 500, "end_ms": 3500}
            ],
            "cues": [
                {
                    "id": "cue-1",
                    "start_ms": 1000,
                    "end_ms": 2200,
                    "text": "Hello world",
                    "speaker": "Alice",
                    "words": [
                        {"text": "Hello", "start_ms": 1100, "end_ms": 1300},
                        {"text": "world", "start_ms": 1400, "end_ms": 1600},
                    ],
                },
                {
                    "id": "cue-2",
                    "start_ms": 1800,
                    "end_ms": 3000,
                    "text": "Overlap again",
                    "speaker": "Bob",
                    "words": [
                        {"text": "Overlap", "start_ms": 1900, "end_ms": 2100},
                        {"text": "again", "start_ms": 2400, "end_ms": 2600},
                    ],
                },
            ],
        }

        def fake_run(command, *, cancel_event):
            del cancel_event
            Path(command[-1]).write_bytes(b"rendered video")

        def fake_build(source_path, output_path, *_args, **_kwargs):
            del source_path
            return ["ffmpeg", output_path]

        with (
            patch.object(self.handlers.media_edit, "revision", return_value=revision),
            patch(
                "pandrator.logic.dubbing.audio_sync.media_has_audio_stream",
                return_value=True,
            ),
            patch(
                "pandrator.logic.dubbing.video_muxing.build_removal_only_video_command",
                side_effect=fake_build,
            ),
            patch(
                "pandrator.logic.dubbing.video_muxing.normalize_video_resolution",
                return_value="source",
            ),
            patch(
                "pandrator.web.capabilities.ffmpeg_video_encoder_ids",
                return_value={"libx264"},
            ),
            patch(
                "pandrator.web.media_process.resolve_ffmpeg_executable",
                return_value="ffmpeg",
            ),
            patch(
                "pandrator.web.media_process.resolve_ffprobe_executable",
                return_value="ffprobe",
            ),
            patch(
                "pandrator.web.media_process.run_media_process",
                side_effect=fake_run,
            ),
        ):
            result = self.handlers.media_edit_render(
                {
                    "session_id": self.session.id,
                    "revision": 1,
                    "settings": {"burn_video_encoder": "libx264"},
                    "settings_hash": "settings-hash",
                },
                self.progress,
                threading.Event(),
            )

        with self.database.session() as session:
            word_artifact = session.get(
                Artifact, result["media_edit_word_timestamps_artifact_id"]
            )
            subtitle = session.get(Artifact, result["subtitle_artifact_id"])
            edges = set(
                session.execute(
                    select(
                        ArtifactEdge.parent_artifact_id,
                        ArtifactEdge.child_artifact_id,
                    )
                )
            )
            timed_words = list(
                session.scalars(
                    select(TimedWord)
                    .where(TimedWord.revision_id == result["document_revision_id"])
                    .order_by(TimedWord.ordinal)
                )
            )
            segments = list(
                session.scalars(
                    select(Segment)
                    .where(Segment.revision_id == result["document_revision_id"])
                    .order_by(Segment.ordinal)
                )
            )

        self.assertEqual(4, result["word_count"])
        self.assertEqual("media_edit_word_timestamps", word_artifact.role)
        self.assertIn((subtitle.id, word_artifact.id), edges)
        self.assertEqual(["Alice", "Alice", "Bob", "Bob"], [
            word.speaker for word in timed_words
        ])
        self.assertEqual([600, 900, 1400, 1900], [
            word.start_ms for word in timed_words
        ])
        self.assertEqual([segments[0].id, segments[0].id, segments[1].id, segments[1].id], [
            word.segment_id for word in timed_words
        ])
        payload = json.loads(
            self.artifacts.resolve(word_artifact.id)[1].read_text(encoding="utf-8")
        )
        self.assertEqual("plan-1", payload["metadata"]["plan_id"])
        self.assertEqual(4, payload["metadata"]["word_count"])


if __name__ == "__main__":
    unittest.main()
