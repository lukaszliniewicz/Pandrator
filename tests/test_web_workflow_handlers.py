import hashlib
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
from pydub.generators import Sine
from sqlalchemy import select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.credentials import auxiliary_credential_key, upsert_credential
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, ArtifactEdge, AudioTake, GenerationRun, GenerationSegment, OutputAssembly, SessionRecord
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.tts_optimization import OptimizationUsage
from pandrator.web.workspace import GenerationService, OutcomePlanService, WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root


class WebWorkflowHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
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
                    "settings": {
                        "service": "XTTS",
                        "max_attempts": 1,
                        "generation_prompt": "Read with quiet intensity.",
                    },
                },
                self.progress,
                threading.Event(),
            )
        self.assertEqual(generate.call_count, 2)
        self.assertTrue(
            all(
                call.args[1]["generation_prompt"] == "Read with quiet intensity."
                for call in generate.call_args_list
            )
        )
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

    def test_workflow_reuses_translation_when_settings_and_source_are_unchanged(self):
        with self.database.session() as session:
            session.get(SessionRecord, self.session.id).workflow_kind = "voiceover"
        source_path = self.paths.uploads / "reuse-source.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(
            source_path,
            kind="srt",
            role="upload",
            session_id=self.session.id,
            metadata={"original_filename": source_path.name},
        )
        requested_settings = {"target_language": "pl"}
        requested_hash = hashlib.sha256(
            json.dumps(requested_settings, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        translated_path = self.session_dir / "reused-translation.srt"
        translated_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCześć\n", encoding="utf-8")
        self.artifacts.register(
            translated_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            parent_ids=[source.id],
            settings={**requested_settings, "llm_default_model": "normalized/provider-model"},
            metadata={"source_artifact_id": source.id, "source_content_hash": source.content_hash, "requested_settings_hash": requested_hash},
        )
        outcome = OutcomePlanService(self.database)
        current = outcome.get(self.session.id)
        value = current["value"]
        value["transformations"] = {**value.get("transformations", {}), "translation": True, "generate_audio": True}
        value["inputs"] = {**value.get("inputs", {}), "translation": "source", "generation": "translation"}
        outcome.update(self.session.id, current["revision"], value)

        with mock.patch.object(self.handlers, "translate") as translate, mock.patch.object(
            self.handlers,
            "_run_reviewable_generation",
            return_value={"generation_run_id": "fixture", "status": "completed"},
        ):
            result = self.handlers.continue_workflow(
                {"session_id": self.session.id, "target_stage": "generate_audio", "stage_settings": {"translate": requested_settings}},
                self.progress,
                threading.Event(),
            )

        translate.assert_not_called()
        self.assertEqual("generate_audio", result["target_stage"])

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

    def test_individual_regeneration_overwrites_the_take_within_the_same_run(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "Regenerate this sentence."}],
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
        with mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=25)):
            self.handlers.run_generation(
                {"generation_run_id": run_id, "operation": "generate"},
                self.progress,
                threading.Event(),
            )
        with self.database.session() as session:
            original = session.scalar(select(AudioTake).where(AudioTake.generation_run_id == run_id))
            take_id = original.id
            original_artifact_id = original.artifact_id

        with mock.patch("pandrator.logic.tts_handler.text_to_audio", return_value=AudioSegment.silent(duration=40)):
            result = self.handlers.run_generation(
                {"generation_run_id": run_id, "segment_ids": segment_ids, "operation": "regenerate"},
                self.progress,
                threading.Event(),
            )

        self.assertEqual("completed", result["status"])
        with self.database.session() as session:
            takes = list(session.scalars(select(AudioTake).where(AudioTake.generation_run_id == run_id)).all())
            self.assertEqual(1, len(takes))
            self.assertEqual(take_id, takes[0].id)
            self.assertEqual(40, takes[0].duration_ms)
            self.assertNotEqual(original_artifact_id, takes[0].artifact_id)
            self.assertEqual("stale", session.get(Artifact, original_artifact_id).state)

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
            self.assertEqual(exported.role, "export_subtitle_source")
            edge = session.scalar(
                select(ArtifactEdge).where(ArtifactEdge.child_artifact_id == exported.id)
            )
            finalized = session.get(Artifact, edge.parent_artifact_id)
            self.assertEqual(finalized.role, "final_subtitle_source")
            final_path = self.paths.root / exported.relative_path
            self.assertIn("00:00:00,000 --> 00:00:01,000", final_path.read_text(encoding="utf-8"))

    def test_subtitle_workspace_with_video_defaults_to_a_subtitle_file(self):
        subtitle_session = self.sessions.create("Transcribed video", workflow_kind="subtitles")
        subtitle_session_dir = self.paths.sessions / subtitle_session.storage_key
        subtitle_session_dir.mkdir()
        video_path = self.paths.uploads / "source.mp4"
        video_path.write_bytes(b"not-needed-for-document-export")
        uploaded = self.artifacts.register(
            video_path,
            kind="source",
            role="upload",
            session_id=subtitle_session.id,
            metadata={"original_filename": "source.mp4"},
        )
        transcription_path = subtitle_session_dir / "transcription.srt"
        transcription_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        self.artifacts.register(
            transcription_path,
            kind="srt",
            role="transcription",
            session_id=subtitle_session.id,
            parent_ids=[uploaded.id],
        )

        result = self.handlers.export(
            {"session_id": subtitle_session.id, "settings": {"export_mode": "media"}},
            self.progress,
            threading.Event(),
        )

        exported, exported_path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual("export_subtitle_source", exported.role)
        self.assertEqual(".srt", exported_path.suffix)

    def test_subtitle_exports_support_vtt_and_concatenated_text(self):
        subtitle_session = self.sessions.create("Portable subtitles", workflow_kind="subtitles")
        session_dir = self.paths.sessions / subtitle_session.storage_key
        session_dir.mkdir()
        source_path = self.paths.uploads / "portable.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nHello\nworld\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\nAgain.\n",
            encoding="utf-8",
        )
        self.artifacts.register(
            source_path,
            kind="source",
            role="upload",
            session_id=subtitle_session.id,
            metadata={"original_filename": "portable.srt"},
        )

        vtt_result = self.handlers.export(
            {
                "session_id": subtitle_session.id,
                "settings": {"export_mode": "subtitles", "subtitle_format": "vtt", "subtitle_selection": "source"},
            },
            self.progress,
            threading.Event(),
        )
        text_result = self.handlers.export(
            {
                "session_id": subtitle_session.id,
                "settings": {"export_mode": "text", "subtitle_selection": "source"},
            },
            self.progress,
            threading.Event(),
        )

        vtt, vtt_path = self.artifacts.resolve(vtt_result["artifact_ids"][0])
        transcript, transcript_path = self.artifacts.resolve(text_result["artifact_ids"][0])
        self.assertEqual("export_subtitle_source", vtt.role)
        self.assertEqual("vtt", vtt.kind)
        self.assertIn("00:00:00.000 --> 00:00:01.000", vtt_path.read_text(encoding="utf-8"))
        self.assertEqual("export_text_source", transcript.role)
        self.assertEqual("Hello world Again.\n", transcript_path.read_text(encoding="utf-8"))

    def test_subtitle_export_falls_back_to_the_available_source_track(self):
        subtitle_session = self.sessions.create("Source-only subtitles", workflow_kind="subtitles")
        source_path = self.paths.uploads / "source-only.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nOnly source\n", encoding="utf-8")
        self.artifacts.register(
            source_path,
            kind="source",
            role="upload",
            session_id=subtitle_session.id,
            metadata={"original_filename": "source-only.srt"},
        )

        result = self.handlers.export(
            {
                "session_id": subtitle_session.id,
                "settings": {"export_mode": "subtitles", "subtitle_format": "srt"},
            },
            self.progress,
            threading.Event(),
        )

        exported, exported_path = self.artifacts.resolve(result["artifact_ids"][0])
        self.assertEqual("export_subtitle_source", exported.role)
        self.assertIn("Only source", exported_path.read_text(encoding="utf-8"))

    def test_repeated_subtitle_exports_create_immutable_output_versions(self):
        subtitle_session = self.sessions.create("Repeatable subtitles", workflow_kind="subtitles")
        source_path = self.paths.uploads / "repeatable.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nFirst version\n", encoding="utf-8")
        self.artifacts.register(
            source_path,
            kind="source",
            role="upload",
            session_id=subtitle_session.id,
            metadata={"original_filename": "repeatable.srt"},
        )
        payload = {
            "session_id": subtitle_session.id,
            "settings": {"export_mode": "subtitles", "subtitle_format": "srt", "subtitle_selection": "source"},
        }

        first = self.handlers.export(payload, self.progress, threading.Event())
        second = self.handlers.export(payload, self.progress, threading.Event())

        first_artifact, first_path = self.artifacts.resolve(first["artifact_ids"][0])
        second_artifact, second_path = self.artifacts.resolve(second["artifact_ids"][0])
        self.assertNotEqual(first_artifact.id, second_artifact.id)
        self.assertNotEqual(first_path, second_path)
        self.assertTrue(first_path.is_file())
        self.assertTrue(second_path.is_file())
        self.assertTrue(second_path.stem.endswith("-2"))

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

        with self.assertRaisesRegex(ValueError, "settings changed.*Reassemble"):
            self.handlers.export(
                {
                    "session_id": voiceover.id,
                    "settings": {"generation_run_id": run_id, "audio_mode": "dubbing_only"},
                    "resolved_settings_snapshot": {
                        "audio": {"synchronization_speed": 1.25},
                        "output": {"format": "wav", "audio_mode": "dubbing_only"},
                        "subtitles": {},
                    },
                },
                self.progress,
                threading.Event(),
            )

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
    def test_audio_source_voiceover_defaults_to_a_duration_bounded_controlled_mix(self):
        voiceover = self.sessions.create("Audio source mix", workflow_kind="voiceover")
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        source_path = session_dir / "source.wav"
        Sine(220).to_audio_segment(duration=1000).apply_gain(-6).export(source_path, format="wav").close()
        source = self.artifacts.register(source_path, kind="source", role="upload", session_id=voiceover.id)
        dubbed_path = session_dir / "dubbed.wav"
        (AudioSegment.silent(duration=200) + Sine(660).to_audio_segment(duration=900).apply_gain(-9) + AudioSegment.silent(duration=300)).export(dubbed_path, format="wav").close()
        dubbed = self.artifacts.register(dubbed_path, kind="audio", role="assembled_audio", session_id=voiceover.id)

        result = self.handlers.export(
            {"session_id": voiceover.id, "settings": {"export_mode": "media", "format": "wav"}},
            self.progress,
            threading.Event(),
        )
        exported, exported_path = self.artifacts.resolve(result["artifact_ids"][-1])

        self.assertEqual("export_mixed_audio", exported.role)
        self.assertLessEqual(abs(len(AudioSegment.from_wav(exported_path)) - 1000), 20)
        with self.database.session() as session:
            parents = {
                edge.parent_artifact_id
                for edge in session.scalars(
                    select(ArtifactEdge).where(ArtifactEdge.child_artifact_id == exported.id)
                ).all()
            }
        self.assertEqual({source.id, dubbed.id}, parents)

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

    def test_subtitle_speech_blocks_store_timing_references_without_narration_silence(self):
        revision_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            [{"number": "0001", "text": "Subtitle speech.", "subtitles": [3, 4]}],
            settings={"language": "en", "sentence_silence_ms": 900, "paragraph_silence_ms": 1400},
        )

        with self.database.session() as session:
            segment = session.scalar(
                select(GenerationSegment).where(GenerationSegment.plan_revision_id == revision_id)
            )
            self.assertEqual("subtitle_cue", segment.node_kind)
            self.assertEqual([3, 4], segment.source_segment_ids_json)
            self.assertFalse(segment.paragraph_break_after)
            self.assertEqual(0, segment.silence_after_ms)

    def test_generation_language_follows_selected_artifact(self):
        updated = self.sessions.update(
            self.session.id,
            self.session.revision,
            {"source_language": "de", "target_language": "pl"},
        )
        source_path = self.session_dir / "source-language.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHallo.\n", encoding="utf-8")
        source = self.artifacts.register(source_path, kind="srt", role="correction", session_id=updated.id)
        translation_path = self.session_dir / "translation-language.srt"
        translation_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCześć.\n", encoding="utf-8")
        translation = self.artifacts.register(translation_path, kind="srt", role="translation", session_id=updated.id)

        self.assertEqual("de", self.handlers._generation_language(updated.id, source, {"language": "pl"}))
        self.assertEqual("pl", self.handlers._generation_language(updated.id, translation, {"language": "de"}))

    def test_tts_settings_default_to_translation_language_when_translation_exists(self):
        updated = self.sessions.update(
            self.session.id,
            self.session.revision,
            {"source_language": "de", "target_language": "pl"},
        )
        translation_path = self.session_dir / "current-translation.srt"
        translation_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCześć.\n", encoding="utf-8")
        self.artifacts.register(translation_path, kind="srt", role="translation", session_id=updated.id)

        resolved = WorkspaceSettingsService(self.database).get(updated.id, "tts")

        self.assertEqual("pl", resolved["effective"]["language"])

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
                export_settings = {
                    "audio_mode": audio_mode,
                    "subtitle_mode": subtitle_mode,
                    "subtitle_selection": subtitle_selection,
                    "original_language": "en",
                    "target_language": "pl",
                }
                if subtitle_mode == "burned":
                    export_settings.update(
                        {
                            "burn_video_encoder": "libx264",
                            "burn_video_quality": 23,
                            "burn_video_speed": "fast",
                            "burn_audio_codec": "aac",
                            "burn_audio_bitrate": "128k",
                        }
                    )
                result = self.handlers.export(
                    {
                        "session_id": voiceover.id,
                        "settings": export_settings,
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
                    self.assertTrue(output.name.endswith("_soft.mp4"))
                    self.assertEqual(2, len(exported.metadata_json.get("subtitle_tracks", [])))
                    self.assertTrue(all(item.get("artifact_id") for item in exported.metadata_json["subtitle_tracks"]))
                if subtitle_mode == "burned":
                    self.assertTrue(output.name.endswith("_burned.mp4"))
                    source_frame = subprocess.run(
                        ["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(media_path), "-frames:v", "1", "-f", "md5", "-"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    burned_frame = subprocess.run(
                        ["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(output), "-frames:v", "1", "-f", "md5", "-"],
                        check=True,
                        capture_output=True,
                        text=True,
                    ).stdout
                    self.assertNotEqual(source_frame, burned_frame)
                    self.assertEqual("burned", exported.metadata_json.get("subtitle_mode"))
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
                if audio_mode == "mixed":
                    self.assertEqual("mixed", exported.metadata_json.get("audio_mode"))
                    self.assertEqual("strong", exported.metadata_json.get("mix", {}).get("ducking"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg qualification requires ffmpeg and ffprobe")
    def test_mixed_video_without_source_soundtrack_falls_back_to_voiceover_only(self):
        voiceover = self.sessions.create("Silent video", workflow_kind="voiceover")
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        media_path = session_dir / "silent-source.mp4"
        subprocess.run(
            [
                "ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=black:s=320x180:d=0.8",
                "-c:v", "libx264", "-pix_fmt", "yuv420p", str(media_path),
            ],
            check=True,
            capture_output=True,
        )
        self.artifacts.register(media_path, kind="source", role="upload", session_id=voiceover.id)
        dubbing_path = session_dir / "voiceover.wav"
        Sine(660).to_audio_segment(duration=800).apply_gain(-12).export(dubbing_path, format="wav").close()
        self.artifacts.register(dubbing_path, kind="audio", role="assembled_audio", session_id=voiceover.id)

        result = self.handlers.export(
            {"session_id": voiceover.id, "settings": {"export_mode": "media", "audio_mode": "mixed"}},
            self.progress,
            threading.Event(),
        )
        exported, output = self.artifacts.resolve(result["artifact_ids"][-1])

        self.assertEqual("dubbed", exported.metadata_json.get("audio_mode"))
        probe = json.loads(
            subprocess.run(
                ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
        )
        self.assertEqual(1, sum(stream["codec_type"] == "video" for stream in probe["streams"]))
        self.assertEqual(1, sum(stream["codec_type"] == "audio" for stream in probe["streams"]))


if __name__ == "__main__":
    unittest.main()
