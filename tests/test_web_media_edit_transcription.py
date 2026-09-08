import json
import tempfile
import threading
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sqlalchemy import select

from pandrator.web.artifact_selection import selected_artifacts
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.media_process import MediaProcessError
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


def _write_normalized_wav(path, duration_seconds=3):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * duration_seconds)
    return path


def _fake_extract_audio(_source, session_dir, _source_name, **_kwargs):
    return str(_write_normalized_wav(Path(session_dir) / "normalized.wav"))


def _fake_vad_export(_audio, output_path, *_args, **_kwargs):
    output_path = Path(output_path)
    output_path.write_text(
        json.dumps(
            {
                "crispasr_vad": {
                    "version": 1,
                    "kind": "vad_segments",
                    "sample_rate": 16000,
                    "num_slices": 1,
                    "slices": [{"start": 0, "end": 48000}],
                }
            }
        ),
        encoding="utf-8",
    )
    return output_path


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
                    "settings": {"caption_alignment_method": "asr"},
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
        self.assertEqual(
            result["alignment_coverage"],
            transcription.metadata_json["alignment_coverage"],
        )
        self.assertEqual(
            result["alignment_confidence"],
            transcription.metadata_json["alignment_confidence"],
        )
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
        ), self.assertRaisesRegex(
            ValueError,
            r"coverage is 0\.000000.*no aligned transcription was promoted",
        ):
            self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"caption_alignment_method": "asr"},
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

    def test_ctc_fallback_artifact_settings_redact_runtime_api_key(self):
        source, _caption = self._source_and_caption()
        normalized = self.paths.root / "normalized.wav"

        def fake_extract(_source, session_dir, _source_name, **_kwargs):
            path = Path(session_dir) / normalized.name
            with wave.open(str(path), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b"\0\0" * 16000 * 3)
            return str(path)

        fake_result = self._mock_transcription(self.paths.root / "source.mp4")
        observed_settings = []
        original_register = self.handlers.artifacts.register

        def register_spy(*args, **kwargs):
            observed_settings.append(kwargs.get("settings"))
            return original_register(*args, **kwargs)

        with (
            patch(
                "pandrator.logic.dubbing.transcription.extract_audio",
                side_effect=fake_extract,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
                return_value=[{"word": "wrong", "start": 1.1, "end": 1.4}],
            ),
            patch(
                "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
                return_value=fake_result,
            ),
            patch.object(
                self.handlers.artifacts,
                "register",
                side_effect=register_spy,
            ),
        ):
            result = self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {
                        "caption_alignment_method": "ctc_asr_fallback",
                        "caption_alignment_fallback_coverage": 0.9,
                        "crispasr_vad_enabled": False,
                        "provider_configs": [
                            {
                                "id": "fallback-provider",
                                "api_key": "injected-fallback-secret",
                            }
                        ],
                    },
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual("ctc_with_asr_fallback", result["alignment_method"])
        self.assertTrue(observed_settings)
        serialized = json.dumps(observed_settings, sort_keys=True)
        self.assertNotIn("injected-fallback-secret", serialized)
        self.assertNotIn('"api_key"', serialized)

    def test_pure_ctc_promotes_native_words_without_whole_recording_asr(self):
        source, _caption = self._source_and_caption()
        asr = Mock(side_effect=AssertionError("whole-recording ASR must not run"))
        with (
            patch(
                "pandrator.logic.dubbing.transcription.extract_audio",
                side_effect=_fake_extract_audio,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_vad_export",
                side_effect=_fake_vad_export,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
                return_value=[
                    {"word": "Hello,", "start": 1.1, "end": 1.3},
                    {"word": "world!", "start": 1.5, "end": 1.9},
                ],
            ),
            patch(
                "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
                asr,
            ),
        ):
            result = self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"caption_alignment_method": "ctc"},
                },
                self.progress,
                threading.Event(),
            )

        asr.assert_not_called()
        self.assertEqual("ctc_cue_alignment", result["alignment_method"])
        self.assertEqual(1.0, result["alignment_coverage"])
        self.assertEqual(2, result["word_count"])
        with self.database.session() as session:
            transcription = session.get(Artifact, result["artifact_id"])
            words = list(
                session.scalars(
                    select(TimedWord).where(
                        TimedWord.revision_id == result["revision_id"]
                    )
                )
            )
        self.assertEqual("transcription", transcription.role)
        self.assertEqual(["Hello,", "world!"], [word.text for word in words])
        self.assertEqual(
            "ctc_cue_alignment", transcription.metadata_json["alignment_method"]
        )

    def test_pure_ctc_low_coverage_keeps_evidence_but_does_not_promote(self):
        source, _caption = self._source_and_caption()
        asr = Mock(side_effect=AssertionError("pure CTC must not fall back"))
        with (
            patch(
                "pandrator.logic.dubbing.transcription.extract_audio",
                side_effect=_fake_extract_audio,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_vad_export",
                side_effect=_fake_vad_export,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
                return_value=[
                    {"word": "wrong", "start": 1.1, "end": 1.3},
                    {"word": "also-wrong", "start": 1.5, "end": 1.9},
                ],
            ),
            patch(
                "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
                asr,
            ),
            self.assertRaisesRegex(
                ValueError,
                r"coverage is 0\.000000.*no aligned transcription was promoted",
            ),
        ):
            self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"caption_alignment_method": "ctc"},
                },
                self.progress,
                threading.Event(),
            )

        asr.assert_not_called()
        with self.database.session() as session:
            roles = {
                item.role
                for item in session.scalars(
                    select(Artifact).where(Artifact.session_id == self.session.id)
                )
            }
        self.assertIn("transcription_vad_evidence", roles)
        self.assertIn("transcription_alignment_diagnostics", roles)
        self.assertNotIn("transcription", roles)

    def test_hybrid_threshold_not_crossed_does_not_call_asr(self):
        source, _caption = self._source_and_caption()
        asr = Mock(side_effect=AssertionError("fallback should not run"))
        with (
            patch(
                "pandrator.logic.dubbing.transcription.extract_audio",
                side_effect=_fake_extract_audio,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_vad_export",
                side_effect=_fake_vad_export,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
                return_value=[
                    {"word": "Hello,", "start": 1.1, "end": 1.3},
                    {"word": "world!", "start": 1.5, "end": 1.9},
                ],
            ),
            patch(
                "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
                asr,
            ),
        ):
            result = self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {
                        "caption_alignment_method": "ctc_asr_fallback",
                        "caption_alignment_fallback_coverage": 0.9,
                    },
                },
                self.progress,
                threading.Event(),
            )

        asr.assert_not_called()
        self.assertEqual("ctc_cue_alignment", result["alignment_method"])
        self.assertFalse(result["fallback_triggered"])

    def test_hybrid_fallback_runs_once_and_only_fills_ctc_rejections(self):
        source = self._artifact("hybrid-source.mp4", "upload", "media", "video")
        self._attach(source, "primary", "video")
        caption = self._artifact(
            "hybrid-captions.srt",
            "captions",
            (
                "1\n00:00:01,000 --> 00:00:01,500\nAlice: Hello,\n\n"
                "2\n00:00:01,500 --> 00:00:02,500\nBob: Missing\n\n"
                "3\n00:00:04,000 --> 00:00:05,000\nEve: Outside\n"
            ),
            "srt",
        )
        self._attach(caption, "transcript", "srt")
        raw_srt = self.paths.root / "fallback.srt"
        raw_srt.write_text(
            "1\n00:00:01,250 --> 00:00:01,900\nHello, Missing\n",
            encoding="utf-8",
        )
        raw_json = self.paths.root / "fallback.json"
        raw_json.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "segments": [
                        {
                            "id": "fallback",
                            "start_ms": 1250,
                            "end_ms": 1900,
                            "text": "Hello, Missing",
                            "words": [
                                {"text": "Hello,", "start_ms": 1250, "end_ms": 1400},
                                {"text": "Missing", "start_ms": 1600, "end_ms": 1900},
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fallback_result = SimpleNamespace(
            srt_path=str(raw_srt),
            word_timestamps_path=str(raw_json),
            engine="test-asr",
            compute_backend="cpu",
        )
        asr = Mock(return_value=fallback_result)

        def ctc_words(_clip, text_path, *_args, **_kwargs):
            if "Hello," in text_path.read_text(encoding="utf-8"):
                return [
                    {"word": "Hello,", "start": 1.1, "end": 1.3},
                    {"word": "wrong", "start": 1.6, "end": 1.9},
                ]
            return [{"word": "wrong", "start": 1.6, "end": 1.9}]

        with (
            patch(
                "pandrator.logic.dubbing.transcription.extract_audio",
                side_effect=_fake_extract_audio,
            ),
            patch(
                "pandrator.logic.dubbing.crispasr.run_ctc_alignment",
                side_effect=ctc_words,
            ),
            patch(
                "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
                asr,
            ),
        ):
            result = self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {
                        "caption_alignment_method": "ctc_asr_fallback",
                        "caption_alignment_fallback_coverage": 0.9,
                        "crispasr_vad_enabled": False,
                    },
                },
                self.progress,
                threading.Event(),
            )

        asr.assert_called_once()
        self.assertTrue(asr.call_args.kwargs["source_is_normalized"])
        self.assertEqual("ctc_with_asr_fallback", result["alignment_method"])
        self.assertEqual(1, result["fallback_filled_cue_count"])
        self.assertEqual(1, result["fallback_filled_token_count"])
        self.assertEqual(1, result["outside_media_count"])
        with self.database.session() as session:
            words = list(
                session.scalars(
                    select(TimedWord).where(
                        TimedWord.revision_id == result["revision_id"]
                    )
                )
            )
        self.assertEqual(["Hello,", "Missing"], [word.text for word in words])
        self.assertEqual(1100, words[0].start_ms)
        self.assertEqual(1600, words[1].start_ms)

    def test_media_edit_transcription_rejects_exact_half_matches_without_words(self):
        source, caption = self._source_and_caption()
        caption_path = self.artifacts.resolve(caption.id)[1]
        caption_path.write_text(
            (
                "1\n00:00:01,000 --> 00:00:02,000\n"
                "Alice: Hello missing\n\n"
                "2\n00:00:01,800 --> 00:00:02,800\n"
                "Bob: World missing\n"
            ),
            encoding="utf-8",
        )
        fake_result = self._mock_transcription(self.paths.root / "source.mp4")
        with patch(
            "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
            return_value=fake_result,
        ), self.assertRaisesRegex(
            ValueError,
            r"coverage is 0\.000000.*no aligned transcription was promoted",
        ):
            self.handlers.transcribe(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"caption_alignment_method": "asr"},
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
            with self.database.session() as session:
                subtitle_artifact = session.scalar(
                    select(Artifact).where(
                        Artifact.session_id == self.session.id,
                        Artifact.role == "media_edit_subtitles",
                    )
                )
                word_artifact = session.scalar(
                    select(Artifact).where(
                        Artifact.session_id == self.session.id,
                        Artifact.role == "media_edit_word_timestamps",
                    )
                )
                self.assertIsNotNone(subtitle_artifact)
                self.assertIsNotNone(word_artifact)
                self.assertIsNone(
                    session.scalar(
                        select(Artifact).where(
                            Artifact.session_id == self.session.id,
                            Artifact.role == "media_edit_media",
                            Artifact.state == "current",
                        )
                    )
                )
                document_revision = session.get(
                    DocumentRevision,
                    subtitle_artifact.metadata_json.get("revision_id"),
                )
                self.assertIsNotNone(document_revision)
                self.assertEqual(
                    4,
                    len(
                        list(
                            session.scalars(
                                select(TimedWord).where(
                                    TimedWord.revision_id == document_revision.id
                                )
                            )
                        )
                    ),
                )
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
        self.assertEqual(
            "revision-1",
            subtitle.metadata_json["media_edit_revision_id"],
        )
        self.assertEqual(
            result["document_revision_id"],
            subtitle.metadata_json["revision_id"],
        )
        self.assertIn((subtitle.id, word_artifact.id), edges)
        self.assertNotIn((word_artifact.id, subtitle.id), edges)
        self.assertNotIn(
            (
                result["media_artifact_id"],
                subtitle.id,
            ),
            edges,
        )
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

    def test_media_edit_render_failure_keeps_materialized_subtitles_and_words(self):
        source = self._artifact("failed-render-source.mp4", "upload", "media", "video")
        revision = {
            "reviewed": True,
            "plan_id": "plan-failed",
            "revision_id": "revision-failed",
            "source_media_artifact": {"id": source.id},
            "keep_ranges": [{"id": "keep", "start_ms": 500, "end_ms": 3500}],
            "cues": [
                {
                    "id": "cue-failed",
                    "start_ms": 1000,
                    "end_ms": 2200,
                    "text": "Hello world",
                    "speaker": "Alice",
                    "words": [
                        {"text": "Hello", "start_ms": 1100, "end_ms": 1300},
                        {"text": "world", "start_ms": 1400, "end_ms": 1600},
                    ],
                }
            ],
        }
        partial_output = []

        def fake_run(command, *, cancel_event):
            del cancel_event
            partial_output.append(Path(command[-1]))
            partial_output[0].write_bytes(b"partial video")
            raise MediaProcessError("ffmpeg failed")

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
            self.assertRaisesRegex(
                ValueError,
                "FFmpeg could not produce the MP4",
            ),
        ):
            self.handlers.media_edit_render(
                {
                    "session_id": self.session.id,
                    "revision": 1,
                    "settings": {"burn_video_encoder": "libx264"},
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual(1, len(partial_output))
        self.assertFalse(partial_output[0].exists())
        with self.database.session() as session:
            subtitle = session.scalar(
                select(Artifact).where(
                    Artifact.session_id == self.session.id,
                    Artifact.role == "media_edit_subtitles",
                )
            )
            words = session.scalar(
                select(Artifact).where(
                    Artifact.session_id == self.session.id,
                    Artifact.role == "media_edit_word_timestamps",
                )
            )
            self.assertIsNotNone(subtitle)
            self.assertIsNotNone(words)
            self.assertEqual("current", subtitle.state)
            self.assertEqual("current", words.state)
            document_revision = session.get(
                DocumentRevision,
                subtitle.metadata_json["revision_id"],
            )
            self.assertIsNotNone(document_revision)
            timed_words = list(
                session.scalars(
                    select(TimedWord).where(
                        TimedWord.revision_id == document_revision.id
                    )
                )
            )
            self.assertEqual(2, len(timed_words))
            self.assertIsNone(
                session.scalar(
                    select(Artifact).where(
                        Artifact.session_id == self.session.id,
                        Artifact.role == "media_edit_media",
                        Artifact.state == "current",
                    )
                )
            )


if __name__ == "__main__":
    unittest.main()
