import json
import shutil
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pydub import AudioSegment
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.credentials import auxiliary_credential_key, upsert_credential
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, ArtifactEdge, AudioTake, GenerationRun, GenerationSegment, OutputAssembly
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.tts_optimization import OptimizationUsage
from pandrator.web.workspace import GenerationService, OutcomePlanService, WorkspaceSettingsService


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

    def test_deepl_translation_resolves_database_credential_at_runtime(self):
        source_path = self.paths.uploads / "source.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(source_path, kind="srt", role="transcription", session_id=self.session.id)
        with self.database.session() as session:
            upsert_credential(session, auxiliary_credential_key("deepl"), "DeepL API key", "database-deepl-key")
        translated_path = self.session_dir / "translated.srt"
        translated_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCześć\n", encoding="utf-8")
        with mock.patch(
            "pandrator.logic.dubbing.llm_translation.translate_srt_file_deepl_with_result",
            return_value=SimpleNamespace(output_path=str(translated_path), cost=0.0, response_count=1, usage={}),
        ) as translate:
            self.handlers.translate(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"translation_backend": "deepl", "target_language": "pl"},
                },
                self.progress,
                threading.Event(),
            )
        self.assertEqual("database-deepl-key", translate.call_args.kwargs["auth_key"])

    def test_url_download_uses_ytdlp_and_records_provenance(self):
        captured = {}
        destination = self.session_dir / "sources" / "example-video.mp4"

        class FakeYoutubeDL:
            def __init__(self, options):
                captured.update(options)

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def extract_info(self, _url, download):
                self.download = download
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(b"media")
                return {"title": "Example video", "id": "fixture", "ext": "mp4"}

            def prepare_filename(self, _information):
                return str(destination)

        with mock.patch.object(
            self.handlers,
            "_validate_download_url",
            return_value="https://example.com/watch?v=fixture",
        ), mock.patch("yt_dlp.YoutubeDL", FakeYoutubeDL):
            result = self.handlers.download_source_url(
                {"session_id": self.session.id, "url": "https://example.com/watch?v=fixture"},
                self.progress,
                threading.Event(),
            )

        artifact, output = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual(output, destination.resolve())
        self.assertEqual(artifact.metadata_json["downloader"], "yt-dlp")
        self.assertTrue(captured["noplaylist"])
        self.assertTrue(captured["restrictfilenames"])

    def test_audiobook_generation_rejects_unsegmented_text_with_actionable_error(self):
        raw_path = self.session_dir / "raw.txt"
        raw_path.write_text("Raw narration", encoding="utf-8")
        raw = self.artifacts.register(raw_path, kind="text", role="upload", session_id=self.session.id)
        with self.assertRaisesRegex(ValueError, "Segment narration"):
            self.handlers.generate_audiobook_audio(
                {"session_id": self.session.id, "source_artifact_id": raw.id, "settings": {}},
                self.progress,
                threading.Event(),
            )

    def test_audiobook_continuation_segments_raw_text_before_generation(self):
        raw_path = self.paths.uploads / "raw-book.txt"
        raw_path.write_text("Chapter One\n\nA short first paragraph.", encoding="utf-8")
        self.artifacts.register(
            raw_path,
            kind="source",
            role="upload",
            session_id=self.session.id,
            metadata={"original_filename": "raw-book.txt"},
        )
        with mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=25)):
            result = self.handlers.continue_workflow(
                {
                    "session_id": self.session.id,
                    "target_stage": "generate_audio",
                    "stage_settings": {
                        "clean_source": {"agentic": False},
                        "prepare_text": {"enable_sentence_splitting": True},
                        "generate_audio": {"service": "XTTS"},
                    },
                },
                self.progress,
                threading.Event(),
            )
        self.assertEqual([item["stage"] for item in result["artifacts"]], ["clean_source", "prepare_text", "generate_audio"])
        with self.database.session() as session:
            run = session.scalar(select(GenerationRun))
            segments = list(session.scalars(select(GenerationSegment).order_by(GenerationSegment.ordinal)).all())
            takes = list(session.scalars(select(AudioTake)).all())
            combined = list(session.scalars(select(Artifact).where(Artifact.role.in_(("audiobook_audio", "assembled_audio")))).all())
            self.assertEqual("completed", run.status)
            self.assertEqual(len(segments), len(takes))
            self.assertEqual([], combined)

    def test_automatic_generation_runs_document_optimization_but_stops_before_export(self):
        raw_path = self.paths.uploads / "automatic-book.txt"
        raw_path.write_text("Chapter One\n\nA short paragraph.", encoding="utf-8")
        self.artifacts.register(raw_path, kind="source", role="upload", session_id=self.session.id, metadata={"original_filename": raw_path.name})
        outcomes = OutcomePlanService(self.database)
        current = outcomes.get(self.session.id)
        value = current["value"]
        value["transformations"]["llm_tts_document_optimization"] = True
        outcomes.update(self.session.id, current["revision"], value)
        def fake_optimize(payload, _progress, _cancel):
            source, source_path = self.handlers._resolve_input(payload["source_artifact_id"])
            destination = self.session_dir / "reviewed-optimization.json"
            rows = json.loads(source_path.read_text(encoding="utf-8"))
            rows[0]["text"] = "Reviewed optimized narration."
            destination.write_text(json.dumps(rows), encoding="utf-8")
            artifact = self.artifacts.register(destination, kind="json", role="tts_optimized", session_id=self.session.id, parent_ids=[source.id], settings=payload["settings"])
            return {"artifact_id": artifact.id}

        with mock.patch.object(self.handlers, "optimize_tts", side_effect=fake_optimize), mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=25)), mock.patch.object(self.handlers, "export", side_effect=AssertionError("Export must remain manual")):
            result = self.handlers.continue_workflow(
                {"session_id": self.session.id, "target_stage": "generate_audio", "stage_settings": {"clean_source": {"agentic": False}, "prepare_text": {}, "optimize_document": {}, "generate_audio": {"service": "XTTS"}}},
                self.progress,
                threading.Event(),
            )
        self.assertEqual(["clean_source", "prepare_text", "optimize_document", "generate_audio"], [item["stage"] for item in result["artifacts"]])
        with self.database.session() as session:
            active_segment = session.scalar(select(GenerationSegment).order_by(GenerationSegment.created_at.desc()))
            self.assertEqual("Reviewed optimized narration.", active_segment.text)

    def test_llm_speech_optimization_runs_per_segment_without_mutating_plan_text(self):
        prepared_path = self.session_dir / "optimized-input.json"
        prepared_path.write_text(json.dumps([{"original_sentence": "Chapter 3."}]), encoding="utf-8")
        prepared = self.artifacts.register(prepared_path, kind="json", role="prepared_text", session_id=self.session.id)
        hydrated = {
            "llm_tts_optimization": True,
            "llm_provider_configs": [],
            "llm_default_model": "local/test",
            "request_timeout_seconds": 30,
            "tts_optimization_model": "local/test",
        }
        def optimize(*_args, on_batch=None, **_kwargs):
            if on_batch:
                on_batch([(0, "Chapter three.")])
            return ["Chapter three."], OptimizationUsage()

        with mock.patch.object(self.handlers, "_with_database_llm_settings", return_value=hydrated), mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=optimize,
        ), mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=25)) as generate:
            self.handlers.generate_audiobook_audio(
                {"session_id": self.session.id, "source_artifact_id": prepared.id, "settings": {"llm_tts_optimization": True, "service": "XTTS"}},
                self.progress,
                threading.Event(),
            )
        self.assertEqual(generate.call_args.args[0], "Chapter three.")
        with self.database.session() as session:
            segment = session.scalar(select(GenerationSegment))
            self.assertEqual(segment.text, "Chapter 3.")
            self.assertEqual(segment.optimized_text, "Chapter three.")
            self.assertEqual(segment.optimization_status, "optimized")

    def test_generation_progress_has_no_phantom_optimization_reserve(self):
        revision_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "First."}, {"text": "Second."}],
            settings={},
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={"text": {"llm_tts_optimization": False}, "tts": {"service": "XTTS"}},
            )
            session.add(run)
            session.flush()
            run_id = run.id
        updates = []

        with mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=25)):
            result = self.handlers.run_generation(
                {"generation_run_id": run_id, "operation": "generate"},
                lambda value, detail=None: updates.append((value, detail)),
                threading.Event(),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual(0.0, next(value for value, detail in updates if detail == "Generating segment 1 of 2"))
        self.assertEqual(0.5, next(value for value, detail in updates if detail == "Generated segment 1 of 2"))
        self.assertEqual(1.0, next(value for value, detail in updates if detail == "Generated segment 2 of 2"))

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
            self.assertEqual(exported.role, "final_subtitle_source")
            edge = session.scalar(
                select(ArtifactEdge).where(ArtifactEdge.child_artifact_id == exported.id)
            )
            self.assertEqual(edge.parent_artifact_id, uploaded.id)
            final_path = self.paths.root / exported.relative_path
            self.assertIn("00:00:00,000 --> 00:00:01,000", final_path.read_text(encoding="utf-8"))

    def test_audiobook_export_prefers_assembled_audio_and_preserves_container(self):
        audiobook = self.sessions.create("Finished Book", workflow_kind="audiobook")
        session_dir = self.paths.sessions / audiobook.storage_key
        session_dir.mkdir()
        legacy_path = session_dir / "legacy.wav"
        AudioSegment.silent(duration=20).export(legacy_path, format="wav").close()
        self.artifacts.register(
            legacy_path,
            kind="audio",
            role="audiobook_audio",
            session_id=audiobook.id,
        )
        assembled_path = session_dir / "assembly.mp3"
        AudioSegment.silent(duration=20).export(assembled_path, format="mp3", bitrate="128k").close()
        assembled = self.artifacts.register(
            assembled_path,
            kind="audio",
            role="assembled_audio",
            session_id=audiobook.id,
        )

        result = self.handlers.export(
            {"session_id": audiobook.id, "settings": {}},
            self.progress,
            threading.Event(),
        )

        self.assertEqual(1, len(result["artifact_ids"]))
        with self.database.session() as session:
            exported = session.get(Artifact, result["artifact_ids"][0])
            self.assertTrue(exported.relative_path.endswith("Finished_Book.mp3"))
            edge = session.scalar(select(ArtifactEdge).where(ArtifactEdge.child_artifact_id == exported.id))
            self.assertEqual(assembled.id, edge.parent_artifact_id)

    def test_voiceover_export_uses_the_requested_generation_run_assembly(self):
        voiceover = self.sessions.create("Versioned Voiceover", workflow_kind="voiceover")
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        generation = GenerationService(
            self.database,
            JobQueue(self.database),
            WorkspaceSettingsService(self.database),
        )
        plan = generation.create_plan(
            voiceover.id,
            source_revision_id=None,
            segments=[{"text": "Versioned speech."}],
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=voiceover.id,
                plan_revision_id=plan["active_revision_id"],
                sequence_number=1,
                status="completed",
                settings_snapshot_json={"tts": {"service": "Kokoro", "voice": "Ada"}},
            )
            session.add(run)
            session.flush()
            run_id = run.id
        selected_path = session_dir / "selected.wav"
        AudioSegment.silent(duration=20).export(selected_path, format="wav").close()
        selected = self.artifacts.register(
            selected_path,
            kind="audio",
            role="assembled_audio",
            session_id=voiceover.id,
        )
        newer_path = session_dir / "newer.wav"
        AudioSegment.silent(duration=80).export(newer_path, format="wav").close()
        self.artifacts.register(
            newer_path,
            kind="audio",
            role="assembled_audio",
            session_id=voiceover.id,
        )
        with self.database.session() as session:
            session.add(
                OutputAssembly(
                    session_id=voiceover.id,
                    generation_run_id=run_id,
                    artifact_id=selected.id,
                    status="completed",
                    settings_json={},
                )
            )

        result = self.handlers.export(
            {
                "session_id": voiceover.id,
                "settings": {"generation_run_id": run_id, "audio_mode": "dubbing_only"},
            },
            self.progress,
            threading.Event(),
        )

        with self.database.session() as session:
            exported = session.get(Artifact, result["artifact_ids"][0])
            edge = session.scalar(select(ArtifactEdge).where(ArtifactEdge.child_artifact_id == exported.id))
            self.assertEqual(selected.id, edge.parent_artifact_id)
            exported_path = self.paths.root / exported.relative_path
        self.assertEqual(20, len(AudioSegment.from_file(exported_path)))
        decoded = AudioSegment.from_file(self.paths.root / exported.relative_path)
        self.assertGreater(len(decoded), 0)

    def test_dubbing_audio_forwards_speech_block_settings_separately(self):
        voiceover = self.sessions.create("Speech block fixture", workflow_kind="voiceover")
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        source_path = self.paths.uploads / "speech-source.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(
            source_path,
            kind="srt",
            role="transcription",
            session_id=voiceover.id,
        )
        captured = {}

        def fake_generate(output_dir, _source, **kwargs):
            captured.update(kwargs)
            output = Path(output_dir) / "blocks.json"
            output.write_text("[]", encoding="utf-8")
            return str(output)

        with mock.patch(
            "pandrator.logic.dubbing.speech_blocks.generate_speech_blocks_file",
            side_effect=fake_generate,
        ), mock.patch.object(
            self.handlers,
            "_generate_audio",
            return_value={"artifact_id": "audio"},
        ):
            self.handlers.generate_dubbing_audio(
                {
                    "session_id": voiceover.id,
                    "source_artifact_id": source.id,
                    "settings": {
                        "target_language": "pl",
                        "speech_block_min_chars": 14,
                        "speech_block_max_chars": 120,
                        "speech_block_merge_threshold": 425,
                        "subtitle_max_chars_per_line": 48,
                    },
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual(
            captured,
            {
                "target_language": "pl",
                "min_chars": 14,
                "max_chars": 120,
                "merge_threshold": 425,
            },
        )

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg qualification requires ffmpeg and ffprobe")
    def test_video_export_matrix_preserves_or_replaces_audio_and_handles_dual_subtitles(self):
        voiceover = self.sessions.create("Export Matrix", workflow_kind="voiceover")
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        media_path = session_dir / "source.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.8",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8", "-shortest",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(media_path),
            ],
            check=True,
            capture_output=True,
        )
        upload = self.artifacts.register(
            media_path,
            kind="source",
            role="upload",
            session_id=voiceover.id,
            metadata={"original_filename": "source.mp4"},
        )
        source_srt = session_dir / "source.srt"
        source_srt.write_text("1\n00:00:00,050 --> 00:00:00,650\nSource line\n", encoding="utf-8")
        correction = self.artifacts.register(
            source_srt,
            kind="srt",
            role="correction",
            session_id=voiceover.id,
            parent_ids=[upload.id],
            metadata={"language": "en"},
        )
        translation_srt = session_dir / "translation.srt"
        translation_srt.write_text("1\n00:00:00,050 --> 00:00:00,650\nWiersz docelowy\n", encoding="utf-8")
        translation = self.artifacts.register(
            translation_srt,
            kind="srt",
            role="translation",
            session_id=voiceover.id,
            parent_ids=[correction.id],
            metadata={"language": "pl"},
        )
        dubbing_path = session_dir / "dub.wav"
        AudioSegment.silent(duration=800).overlay(AudioSegment.silent(duration=800)).export(dubbing_path, format="wav").close()
        dubbing = self.artifacts.register(
            dubbing_path,
            kind="audio",
            role="assembled_audio",
            session_id=voiceover.id,
        )

        cases = (
            ("preserve", "none", "source", 0),
            ("preserve", "soft", "dual", 2),
            ("preserve", "burned", "dual", 0),
            ("dubbing_only", "none", "source", 0),
            ("mixed", "none", "source", 0),
        )
        for audio_mode, subtitle_mode, subtitle_selection, expected_subtitles in cases:
            with self.subTest(audio_mode=audio_mode, subtitle_mode=subtitle_mode):
                result = self.handlers.export(
                    {
                        "session_id": voiceover.id,
                        "settings": {
                            "audio_mode": audio_mode,
                            "subtitle_mode": subtitle_mode,
                            "subtitle_selection": subtitle_selection,
                            "original_language": "en",
                            "target_language": "pl",
                        },
                    },
                    self.progress,
                    threading.Event(),
                )
                with self.database.session() as session:
                    exported = session.get(Artifact, result["artifact_ids"][-1])
                    output = self.paths.root / exported.relative_path
                probe = json.loads(
                    subprocess.run(
                        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                )
                streams = probe["streams"]
                self.assertEqual(1, sum(stream["codec_type"] == "video" for stream in streams))
                self.assertEqual(1, sum(stream["codec_type"] == "audio" for stream in streams))
                subtitle_streams = [stream for stream in streams if stream["codec_type"] == "subtitle"]
                self.assertEqual(expected_subtitles, len(subtitle_streams))
                if subtitle_mode == "soft":
                    self.assertEqual(["eng", "pol"], [stream.get("tags", {}).get("language") for stream in subtitle_streams])
                    self.assertEqual(1, subtitle_streams[1].get("disposition", {}).get("default"))
                if subtitle_mode == "burned":
                    with self.database.session() as session:
                        overlay = session.scalar(
                            select(Artifact).where(
                                Artifact.session_id == voiceover.id,
                                Artifact.role == "bilingual_subtitle_overlay",
                                Artifact.state == "current",
                            )
                        )
                        self.assertIsNotNone(overlay)
                        content = (self.paths.root / overlay.relative_path).read_text(encoding="utf-8-sig")
                        self.assertIn("Style: Source", content)
                        self.assertIn("Style: Translation", content)
                        self.assertIn("Dialogue: 0", content)
                        self.assertIn("Dialogue: 1", content)


if __name__ == "__main__":
    unittest.main()
