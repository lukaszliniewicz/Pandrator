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
from sqlalchemy import func, select

from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.credentials import (
    auxiliary_credential_key,
    database_reference,
    tts_service_credential_key,
    upsert_credential,
)
from pandrator.web.jobs import JobQueue
from pandrator.web.models import AppSetting, Artifact, ArtifactEdge, AudioTake, GenerationPlan, GenerationPlanRevision, GenerationRun, GenerationSegment, OutputAssembly, PronunciationEntry, SessionRecord, SessionSetting, SessionStageSelection, UsageEvent
from pandrator.web.pronunciations import PronunciationLibrary
from pandrator.web.sessions import SessionService
from pandrator.web.tts_providers import TtsBatchResult, TtsCapabilities
from pandrator.web.workflow_handlers import (
    WorkflowHandlers,
    _apply_selected_segment_tts_override,
    _fraction_message_callback,
    _source_cleaning_progress_callback,
)
from pandrator.web.tts_optimization import OptimizationUsage
from pandrator.web.workspace import GenerationService, OutcomePlanService, WorkspaceSettingsService, adapt_runtime_settings, mark_output_assemblies_stale
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

    def test_fraction_message_progress_uses_the_primary_work_counter(self):
        updates = []
        callback = _fraction_message_callback(
            lambda value, detail=None: updates.append((value, detail)),
            0.1,
            0.5,
        )

        callback("Web research turn 3/8 (1/3 searches, 0/2 pages)")

        self.assertAlmostEqual(0.2, updates[-1][0])

    def test_selected_tts_override_wins_after_persistent_segment_choices(self):
        persisted_segment_settings = {
            "service": "XTTS",
            "model": "base",
            "voice": "stored-voice",
            "speaker": "stored-voice",
            "language": "de",
            "target_language": "de",
        }
        effective = _apply_selected_segment_tts_override(
            persisted_segment_settings,
            {
                "service": "Chatterbox",
                "model": "alternate",
                "voice": "alternate-reference",
                "language": "fr",
                "generation_prompt": "Gentle and close.",
            },
        )

        self.assertEqual("Chatterbox", effective["service"])
        self.assertEqual("alternate", effective["model"])
        self.assertEqual("alternate-reference", effective["voice"])
        self.assertEqual("alternate-reference", effective["speaker"])
        self.assertEqual("fr", effective["language"])
        self.assertEqual("fr", effective["target_language"])
        self.assertEqual("Gentle and close.", effective["generation_prompt"])

    def test_selected_override_reaches_synthesis_and_rvc_without_touching_other_takes(
        self,
    ):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [
                {"text": "First alternate.", "voice": "stored-one", "language": "de"},
                {"text": "Second alternate.", "voice": "stored-two", "language": "it"},
                {"text": "Keep this take.", "voice": "stored-three", "language": "es"},
            ],
            settings={},
        )
        selected_override = {
            "tts": {
                "service": "Chatterbox",
                "model": "chatterbox-alternate",
                "voice": "alternate-reference",
                "language": "fr",
                "generation_prompt": "Warm, quiet delivery.",
            },
            "rvc": {"enabled": True, "model": "alternate-rvc", "pitch": 2},
        }
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={
                    "text": {"llm_tts_optimization": False},
                    "tts": {
                        "service": "XTTS",
                        "model": "base",
                        "voice": "base-voice",
                        "language": "en",
                    },
                    "rvc": {"enabled": True, "model": "global-rvc"},
                    "selected_segment_override": selected_override,
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[2],
                    kind="tts",
                    status="completed",
                    is_active=True,
                )
            )

        synthesis_settings = []
        rvc_settings = []

        def synthesize(_text, settings, **_kwargs):
            synthesis_settings.append(dict(settings))
            return AudioSegment.silent(duration=35)

        def apply_rvc(audio, settings):
            rvc_settings.append(dict(settings))
            return audio + AudioSegment.silent(duration=5)

        with (
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesize",
                side_effect=synthesize,
            ),
            mock.patch(
                "pandrator.logic.rvc_handler.process_with_rvc",
                side_effect=apply_rvc,
            ),
        ):
            result = self.handlers.run_generation(
                {
                    "generation_run_id": run_id,
                    "segment_ids": segment_ids[:2],
                    "operation": "regenerate",
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual("partial", result["status"])
        self.assertEqual(2, len(synthesis_settings))
        for settings in synthesis_settings:
            self.assertEqual("Chatterbox", settings["service"])
            self.assertEqual("chatterbox-alternate", settings["model"])
            self.assertEqual("alternate-reference", settings["voice"])
            self.assertEqual("alternate-reference", settings["speaker"])
            self.assertEqual("fr", settings["language"])
            self.assertEqual("fr", settings["target_language"])
            self.assertEqual("Warm, quiet delivery.", settings["generation_prompt"])
        self.assertEqual(2, len(rvc_settings))
        self.assertEqual(["alternate-rvc", "alternate-rvc"], [item["model"] for item in rvc_settings])
        with self.database.session() as session:
            takes = list(
                session.scalars(
                    select(AudioTake)
                    .where(AudioTake.generation_run_id == run_id)
                    .order_by(AudioTake.created_at)
                ).all()
            )
            self.assertEqual(segment_ids[:2], [item.generation_segment_id for item in takes])
            self.assertTrue(all(item.kind == "tts_rvc" for item in takes))
            untouched_take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_ids[2]
                )
            )
            self.assertTrue(untouched_take.is_active)
            untouched_segment = session.get(GenerationSegment, segment_ids[2])
            self.assertEqual("stored-three", untouched_segment.voice)
            self.assertEqual("es", untouched_segment.language)

    def test_selected_catalogue_provider_is_hydrated_before_synthesis(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [
                {"text": "Catalogue alternate.", "voice": "stored-one", "language": "de"},
                {"text": "Keep this take.", "voice": "stored-two", "language": "it"},
            ],
            settings={},
        )
        provider_configs = [
            {
                "id": "catalogue-provider",
                "name": "Catalogue Provider",
                "provider": "openai",
                "api_base": "https://catalogue.example/v1",
                "api_key": "catalogue-secret",
                "models": ["catalogue-model"],
                "voices": ["catalogue-voice"],
            },
            {
                "id": "explicit-catalogue-provider",
                "name": "Explicit Catalogue Provider",
                "provider": "openai",
                "api_base": "https://explicit.example/v1",
                "api_key": "explicit-secret",
                "models": ["catalogue-model"],
                "voices": ["catalogue-voice"],
            },
        ]
        selected_override = {
            "tts": {
                "service": "catalogue-provider",
                "model": "catalogue-model",
                "voice": "catalogue-voice",
                "language": "fr",
                "openai_audio_endpoint": "explicit-catalogue-provider",
            }
        }
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={
                    "text": {"llm_tts_optimization": False},
                    "tts": {
                        "service": "XTTS",
                        "model": "base",
                        "voice": "base-voice",
                        "language": "en",
                        "provider_configs": provider_configs,
                    },
                    "selected_segment_override": selected_override,
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id
            session.add(
                AudioTake(
                    generation_segment_id=segment_ids[1],
                    kind="tts",
                    status="completed",
                    is_active=True,
                )
            )

        synthesis_settings = []

        def synthesize(_text, settings, **_kwargs):
            synthesis_settings.append(dict(settings))
            return AudioSegment.silent(duration=35)

        with mock.patch.object(
            self.handlers.tts_providers,
            "synthesize",
            side_effect=synthesize,
        ):
            result = self.handlers.run_generation(
                {
                    "generation_run_id": run_id,
                    "segment_ids": [segment_ids[0]],
                    "operation": "regenerate",
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual("partial", result["status"])
        self.assertEqual(1, len(synthesis_settings))
        settings = synthesis_settings[0]
        self.assertEqual("Custom", settings["service"])
        self.assertEqual("catalogue-model", settings["model"])
        self.assertEqual("catalogue-voice", settings["voice"])
        self.assertEqual("catalogue-voice", settings["speaker"])
        self.assertEqual("fr", settings["language"])
        self.assertEqual("fr", settings["target_language"])
        self.assertEqual(
            "explicit-catalogue-provider", settings["openai_audio_endpoint"]
        )
        configured = next(
            item
            for item in settings["provider_configs"]
            if item["id"] == "explicit-catalogue-provider"
        )
        self.assertEqual("https://explicit.example/v1", configured["api_base"])
        self.assertEqual("explicit-secret", configured["api_key"])
        with self.database.session() as session:
            stored_run = session.get(GenerationRun, run_id)
            self.assertEqual("XTTS", stored_run.settings_snapshot_json["tts"]["service"])
            self.assertNotIn(
                "openai_audio_endpoint",
                stored_run.settings_snapshot_json["tts"],
            )
            untouched_segment = session.get(GenerationSegment, segment_ids[1])
            self.assertEqual("stored-two", untouched_segment.voice)
            self.assertEqual("it", untouched_segment.language)
            untouched_take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_segment_id == segment_ids[1]
                )
            )
            self.assertTrue(untouched_take.is_active)

    def test_start_binds_late_custom_provider_for_worker_hydration(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "Late provider alternate.", "voice": "stored", "language": "de"}],
            settings={},
        )
        source_snapshot = {
            "text": {"llm_tts_optimization": False},
            "tts": {
                "service": "XTTS",
                "model": "base",
                "voice": "base-voice",
                "language": "en",
            },
        }
        late_provider = {
            "id": "late-catalogue",
            "name": "Late Catalogue",
            "provider": "openai",
            "api_base": "https://late.example/v1",
            "secret_ref": database_reference(
                tts_service_credential_key("late-catalogue")
            ),
            "models": ["late-model"],
            "voices": ["late-voice"],
        }
        unrelated_provider = {
            "id": "unrelated-catalogue",
            "name": "Unrelated Catalogue",
            "provider": "openai",
            "api_base": "https://unrelated.example/v1",
            "secret_ref": database_reference(
                tts_service_credential_key("unrelated-catalogue")
            ),
            "models": ["unrelated-model"],
            "voices": ["unrelated-voice"],
        }
        with self.database.session() as session:
            source = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                sequence_number=1,
                status="completed",
                settings_snapshot_json=source_snapshot,
            )
            session.add(source)
            upsert_credential(
                session,
                tts_service_credential_key("late-catalogue"),
                "Late Catalogue API key",
                "late-secret",
            )
            session.add(
                AppSetting(
                    key="services.tts",
                    value_json={
                        "provider_configs": [late_provider, unrelated_provider]
                    },
                    revision=1,
                )
            )
            session.flush()
            source_run_id = source.id

        generation = GenerationService(
            self.database,
            JobQueue(self.database),
            WorkspaceSettingsService(self.database),
            self.artifacts,
        )
        selected_override = {
            "tts": {
                "service": "late-catalogue",
                "model": "late-model",
                "voice": "late-voice",
                "language": "fr",
            }
        }
        started = generation.start(
            self.session.id,
            segment_ids=[segment_ids[0]],
            generation_run_id=source_run_id,
            operation="regenerate",
            selected_segment_override=selected_override,
        )

        with self.database.session() as session:
            source = session.get(GenerationRun, source_run_id)
            run = session.get(GenerationRun, started["id"])
            snapshot = dict(run.settings_snapshot_json)
            bound_configs = snapshot["selected_segment_override"]["tts"][
                "provider_configs"
            ]
            self.assertEqual(["late-catalogue"], [item["id"] for item in bound_configs])
            self.assertNotIn("api_key", bound_configs[0])
            self.assertNotIn("late-secret", json.dumps(snapshot))
            self.assertEqual([], source.settings_snapshot_json["tts"].get("provider_configs", []))
            top_level_configs = snapshot["tts"]["provider_configs"]
            self.assertEqual(["late-catalogue"], [item["id"] for item in top_level_configs])

        synthesis_settings = []

        def synthesize(_text, settings, **_kwargs):
            synthesis_settings.append(dict(settings))
            return AudioSegment.silent(duration=35)

        with mock.patch.object(
            self.handlers.tts_providers,
            "synthesize",
            side_effect=synthesize,
        ):
            result = self.handlers.run_generation(
                {
                    "generation_run_id": started["id"],
                    "segment_ids": segment_ids[:1],
                    "operation": "regenerate",
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual("completed", result["status"])
        settings = synthesis_settings[0]
        self.assertEqual("Custom", settings["service"])
        self.assertEqual("late-model", settings["model"])
        self.assertEqual("late-voice", settings["voice"])
        self.assertEqual("fr", settings["language"])
        configured = next(
            item for item in settings["provider_configs"] if item["id"] == "late-catalogue"
        )
        self.assertEqual("https://late.example/v1", configured["api_base"])
        self.assertEqual("late-secret", configured["api_key"])
        self.assertNotIn(
            "unrelated-catalogue",
            [item["id"] for item in settings["provider_configs"]],
        )

    def test_source_cleaning_progress_spans_phase_turn_budgets(self):
        updates = []
        callback = _source_cleaning_progress_callback(
            lambda value, detail=None: updates.append((value, detail)),
            0.4,
            0.8,
            phase_names=["first", "second"],
            phase_budgets={"first": 2, "second": 2},
        )

        callback("Phase 1/2: First")
        callback("First: LLM turn 2/2")
        callback("Phase 2/2: Second")

        for expected, (value, _detail) in zip([0.4, 0.5, 0.6], updates):
            self.assertAlmostEqual(expected, value)

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

    def test_output_mix_preview_uses_managed_inputs_and_registers_lineage(self):
        source_path = self.session_dir / "source.wav"
        source_path.write_bytes(b"source audio")
        source = self.artifacts.register(
            source_path,
            kind="audio",
            role="upload",
            session_id=self.session.id,
        )
        dubbing_path = self.session_dir / "assembled.wav"
        dubbing_path.write_bytes(b"voiceover audio")
        dubbing = self.artifacts.register(
            dubbing_path,
            kind="audio",
            role="output_assembly",
            session_id=self.session.id,
        )
        next_source_path = self.session_dir / "source-next.wav"
        next_source_path.write_bytes(b"next source audio")
        next_source = self.artifacts.register(
            next_source_path,
            kind="audio",
            role="upload",
            session_id=self.session.id,
        )
        next_dubbing_path = self.session_dir / "assembled-next.wav"
        next_dubbing_path.write_bytes(b"next voiceover audio")
        next_dubbing = self.artifacts.register(
            next_dubbing_path,
            kind="audio",
            role="output_assembly",
            session_id=self.session.id,
            metadata={"takes": [{"target_start_ms": 6500}]},
        )
        captured: list[list[str]] = []

        def fake_run(command, **_kwargs):
            captured.append(list(command))
            Path(command[-1]).write_bytes(
                f"preview audio {len(captured)}".encode()
            )

        with (
            mock.patch(
                "pandrator.web.media_process.probe_audio_stream",
                side_effect=[
                    SimpleNamespace(duration_ms=60_000),
                    SimpleNamespace(duration_ms=50_000),
                    SimpleNamespace(duration_ms=60_000),
                    SimpleNamespace(duration_ms=50_000),
                ],
            ),
            mock.patch(
                "pandrator.web.media_process.run_media_process",
                side_effect=fake_run,
            ),
        ):
            result = self.handlers.preview_output_mix(
                {
                    "session_id": self.session.id,
                    "generation_run_id": "generation-run",
                    "source_artifact_id": source.id,
                    "dubbing_artifact_id": dubbing.id,
                    "start_seconds": 10,
                    "duration_seconds": 12,
                    "settings": {
                        "mix_ducking": "very_strong",
                        "mix_source_gain_db": -2,
                        "mix_voice_gain_db": 1,
                    },
                },
                self.progress,
                threading.Event(),
            )
            repeated = self.handlers.preview_output_mix(
                {
                    "session_id": self.session.id,
                    "generation_run_id": "next-generation-run",
                    "source_artifact_id": next_source.id,
                    "dubbing_artifact_id": next_dubbing.id,
                    "start_seconds": None,
                    "duration_seconds": 12,
                    "settings": {"mix_ducking": "very_strong"},
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual(10, result["start_seconds"])
        self.assertEqual(12, result["duration_seconds"])
        self.assertEqual(result["artifact_id"], repeated["artifact_id"])
        self.assertNotEqual(
            result["artifact"]["content_hash"],
            repeated["artifact"]["content_hash"],
        )
        self.assertEqual(5.5, repeated["start_seconds"])
        self.assertEqual("assembly_timeline", repeated["automatic_start_method"])
        self.assertEqual(
            "sessions/"
            + self.session.storage_key
            + "/previews/soundtrack-mix-preview.wav",
            repeated["artifact"]["relative_path"].replace("\\", "/"),
        )
        self.assertTrue(
            any("threshold=0.012589" in item for item in captured[0])
        )
        with self.database.session() as session:
            preview = session.get(Artifact, result["artifact_id"])
            self.assertEqual("mix_preview", preview.role)
            self.assertEqual("very_strong", preview.metadata_json["mix"]["ducking"])
            parent_ids = set(
                session.scalars(
                    select(ArtifactEdge.parent_artifact_id).where(
                        ArtifactEdge.child_artifact_id == preview.id
                    )
                ).all()
            )
        self.assertEqual({next_source.id, next_dubbing.id}, parent_ids)

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
            result = self.handlers.translate(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"translation_backend": "deepl", "target_language": "pl"},
                },
                self.progress,
                threading.Event(),
            )
        self.assertEqual("database-deepl-key", translate.call_args.kwargs["auth_key"])
        artifact, _output = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual("deepl", artifact.metadata_json["backend"])
        self.assertEqual("pl", artifact.metadata_json["language"])
        self.assertEqual("", artifact.metadata_json["model"])

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

    @staticmethod
    def _fake_llm_hydration(settings, stage):
        alias = "correction_model" if stage == "correction" else "translation_model"
        requested = str(settings.get("model_name") or "").strip()
        for key in (alias, "correct_model" if stage == "correction" else "translate_model"):
            requested = requested or str(settings.get(key) or "").strip()
        if requested == "default":
            requested = ""
        return {
            **settings,
            "llm_provider_configs": [],
            "llm_default_model": "mock/default-model",
            "request_timeout_seconds": 600,
            alias: requested or "mock/default-model",
        }

    def _voiceover_session_with_translation(self, fingerprint):
        with self.database.session() as session:
            session.get(SessionRecord, self.session.id).workflow_kind = "voiceover"
        source_path = self.paths.uploads / "fingerprint-source.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(
            source_path,
            kind="srt",
            role="upload",
            session_id=self.session.id,
            metadata={"original_filename": source_path.name},
        )
        translated_path = self.session_dir / "fingerprint-translation.srt"
        translated_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCześć\n", encoding="utf-8")
        translation_metadata = {
            "source_artifact_id": source.id,
            "source_content_hash": source.content_hash,
        }
        if fingerprint is not None:
            translation_metadata["settings_fingerprint"] = fingerprint
        translation = self.artifacts.register(
            translated_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            parent_ids=[source.id],
            metadata=translation_metadata,
        )
        outcome = OutcomePlanService(self.database)
        current = outcome.get(self.session.id)
        value = current["value"]
        value["transformations"] = {**value.get("transformations", {}), "translation": True, "generate_audio": True}
        value["inputs"] = {**value.get("inputs", {}), "translation": "source", "generation": "translation"}
        outcome.update(self.session.id, current["revision"], value)
        return source, translation

    def _continue_generation(self, translate_settings, *, reuse_stages=()):
        with mock.patch.object(self.handlers, "translate") as translate, mock.patch.object(
            self.handlers,
            "_run_reviewable_generation",
            return_value={"generation_run_id": "fixture", "status": "completed"},
        ), mock.patch.object(self.handlers, "_with_database_llm_settings", side_effect=self._fake_llm_hydration):
            result = self.handlers.continue_workflow(
                {
                    "session_id": self.session.id,
                    "target_stage": "generate_audio",
                    "stage_settings": {"translate": translate_settings},
                    "reuse_stages": list(reuse_stages),
                },
                self.progress,
                threading.Event(),
            )
        self.assertEqual("generate_audio", result["target_stage"])
        return translate

    def test_translation_fingerprint_reuse_ignores_submission_shape(self):
        self._voiceover_session_with_translation(
            {"backend": "llm", "target_language": "pl", "model": "mock/default-model", "instructions": ""}
        )
        # Same semantics as the stored run, but in the flat stage-dialog shape
        # that previously produced a different whole-dict hash and a rerun.
        translate = self._continue_generation(
            {"translation_backend": "llm", "target_language": "pl", "translate_model": "default", "instructions": ""}
        )
        translate.assert_not_called()

    def test_translation_reruns_only_when_fingerprint_changes(self):
        self._voiceover_session_with_translation(
            {"backend": "llm", "target_language": "pl", "model": "mock/default-model", "instructions": ""}
        )
        translate = self._continue_generation({"target_language": "de"})
        translate.assert_called_once()

    def test_translation_reasoning_override_is_part_of_the_fingerprint(self):
        self._voiceover_session_with_translation(
            {
                "backend": "llm",
                "target_language": "pl",
                "model": "mock/default-model",
                "instructions": "",
            }
        )

        translate = self._continue_generation(
            {"target_language": "pl", "reasoning_effort": "high"}
        )

        translate.assert_called_once()

    def test_parallel_translation_is_part_of_the_fingerprint_but_default_one_is_legacy_compatible(self):
        self._voiceover_session_with_translation(
            {
                "backend": "llm",
                "target_language": "pl",
                "model": "mock/default-model",
                "instructions": "",
            }
        )

        sequential = self._continue_generation(
            {"target_language": "pl", "llm_concurrent_calls": 1}
        )
        sequential.assert_not_called()
        parallel = self._continue_generation(
            {"target_language": "pl", "llm_concurrent_calls": 2}
        )
        parallel.assert_called_once()

    def test_reuse_stages_choice_keeps_translation_despite_change(self):
        self._voiceover_session_with_translation(
            {"backend": "llm", "target_language": "pl", "model": "mock/default-model", "instructions": ""}
        )
        translate = self._continue_generation({"target_language": "de"}, reuse_stages=("translate",))
        translate.assert_not_called()

    def test_reuse_translation_bypasses_selected_parent_lineage_change(self):
        with self.database.session() as session:
            session.get(SessionRecord, self.session.id).workflow_kind = "voiceover"
        source_path = self.paths.uploads / "lineage-source.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        source = self.artifacts.register(source_path, kind="srt", role="upload", session_id=self.session.id, metadata={"original_filename": source_path.name})
        parent_path = self.session_dir / "translation-parent.srt"
        parent_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nParent\n", encoding="utf-8")
        translation_parent = self.artifacts.register(parent_path, kind="srt", role="correction", session_id=self.session.id, parent_ids=[source.id])
        translation_path = self.session_dir / "lineage-translation.srt"
        translation_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nCześć\n", encoding="utf-8")
        translation = self.artifacts.register(translation_path, kind="srt", role="translation", session_id=self.session.id, parent_ids=[translation_parent.id], metadata={"source_artifact_id": translation_parent.id})
        selected_path = self.session_dir / "selected-correction.srt"
        selected_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nSelected\n", encoding="utf-8")
        selected_correction = self.artifacts.register(selected_path, kind="srt", role="correction", session_id=self.session.id, parent_ids=[source.id])
        outcome = OutcomePlanService(self.database)
        current = outcome.get(self.session.id)
        value = current["value"]
        value["transformations"] = {**value.get("transformations", {}), "translation": True, "generate_audio": True}
        value["inputs"] = {**value.get("inputs", {}), "translation": "correction", "generation": "translation"}
        outcome.update(self.session.id, current["revision"], value)
        with self.database.session() as session:
            session.get(SessionStageSelection, (self.session.id, "correct")).artifact_id = selected_correction.id
            session.get(SessionStageSelection, (self.session.id, "translate")).artifact_id = translation.id

        with mock.patch.object(self.handlers, "translate") as translate, mock.patch.object(
            self.handlers,
            "_run_reviewable_generation",
            return_value={"generation_run_id": "fixture", "status": "completed"},
        ):
            self.handlers.continue_workflow(
                {
                    "session_id": self.session.id,
                    "target_stage": "generate_audio",
                    "stage_settings": {"translate": {"source_artifact_id": selected_correction.id}},
                    "reuse_stages": ["correct", "translate"],
                },
                self.progress,
                threading.Event(),
            )

        translate.assert_not_called()

    def _resolved_translation_settings(self):
        resolved, _ = WorkspaceSettingsService(self.database).resolve(self.session.id, ["translation", "subtitles"])
        settings = {}
        for section in ("translation", "subtitles"):
            settings.update(adapt_runtime_settings(section, resolved.get(section, {})))
        return settings

    def test_settings_mismatches_detects_translation_target_language_change(self):
        self._voiceover_session_with_translation(
            {"backend": "llm", "target_language": "pl", "model": "mock/default-model", "instructions": ""}
        )
        with self.database.session() as session:
            session.get(SessionRecord, self.session.id).target_language = "de"
        with mock.patch.object(self.handlers, "_with_database_llm_settings", side_effect=self._fake_llm_hydration):
            mismatches = self.handlers.settings_mismatches(self.session.id, "generate_audio")
        self.assertEqual(1, len(mismatches))
        self.assertEqual("translate", mismatches[0]["stage"])
        self.assertIn("target_language", mismatches[0]["changed_fields"])

    def test_settings_mismatches_reports_legacy_rerun_and_keeps_matching_raw_hash_quiet(self):
        _source, translation = self._voiceover_session_with_translation(None)
        with mock.patch.object(self.handlers, "_with_database_llm_settings", side_effect=self._fake_llm_hydration):
            mismatches = self.handlers.settings_mismatches(self.session.id, "generate_audio")
        self.assertEqual(["translate"], [item["stage"] for item in mismatches])
        self.assertEqual(["settings_unverifiable"], mismatches[0]["reasons"])
        settings = self._resolved_translation_settings()
        with self.database.session() as session:
            current = session.get(Artifact, translation.id)
            current.settings_hash = hashlib.sha256(json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        with mock.patch.object(self.handlers, "_with_database_llm_settings", side_effect=self._fake_llm_hydration):
            self.assertEqual([], self.handlers.settings_mismatches(self.session.id, "generate_audio"))

    def test_settings_mismatches_reports_translation_source_lineage_change(self):
        source, translation = self._voiceover_session_with_translation(None)
        next_source_path = self.paths.uploads / "next-lineage-source.srt"
        next_source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        next_source = self.artifacts.register(next_source_path, kind="srt", role="upload", session_id=self.session.id, metadata={"original_filename": next_source_path.name})
        self.assertEqual(source.content_hash, next_source.content_hash)
        with self.database.session() as session:
            session.add(SessionSetting(session_id=self.session.id, section="translation", value_json={"source_artifact_id": next_source.id}))
            session.get(SessionStageSelection, (self.session.id, "translate")).artifact_id = translation.id
        settings = self._resolved_translation_settings()
        with self.database.session() as session:
            current = session.get(Artifact, translation.id)
            current.settings_hash = hashlib.sha256(json.dumps(settings, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        with mock.patch.object(self.handlers, "_with_database_llm_settings", side_effect=self._fake_llm_hydration):
            mismatches = self.handlers.settings_mismatches(self.session.id, "generate_audio")
        self.assertEqual("translate", mismatches[0]["stage"])
        self.assertEqual(["source_lineage_changed"], mismatches[0]["reasons"])
        self.assertEqual(source.id, translation.metadata_json["source_artifact_id"])
        translate = self._continue_generation({})
        translate.assert_called_once()

    def test_generation_plan_reuses_revision_for_identical_content(self):
        records = [{"text": "First sentence.", "paragraph": "yes"}, {"text": "Second sentence."}]
        settings = {"language": "en", "paragraph_silence_ms": 700, "sentence_silence_ms": 250, "voice": "alice"}
        first_id, first_segments = self.handlers._store_generation_plan(self.session.id, records, settings=settings)
        # Voice, service, and model choices do not alter segmentation, so the
        # plan revision (and with it takes, edits, and run history) is kept.
        second_id, second_segments = self.handlers._store_generation_plan(
            self.session.id,
            records,
            settings={**settings, "voice": "bob", "service": "Kokoro", "model": "other"},
        )
        self.assertEqual(first_id, second_id)
        self.assertEqual(first_segments, second_segments)
        changed_silence_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            records,
            settings={**settings, "sentence_silence_ms": 500},
        )
        self.assertNotEqual(first_id, changed_silence_id)
        changed_merge_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            records,
            settings={
                **settings,
                "sentence_silence_ms": 500,
                "speech_block_merge_threshold": 600,
            },
        )
        self.assertNotEqual(changed_silence_id, changed_merge_id)
        with self.database.session() as session:
            plan = session.scalar(select(GenerationPlan).where(GenerationPlan.session_id == self.session.id))
            self.assertEqual(changed_merge_id, plan.active_revision_id)
        third_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "Changed sentence."}],
            settings=settings,
        )
        self.assertNotEqual(changed_merge_id, third_id)

    def test_subtitle_plan_threshold_is_monotonic_and_preserves_speaker(self):
        source_path = self.session_dir / "speaker-safe.srt"
        source_path.write_text(
            """1
00:00:00,000 --> 00:00:01,000
[SPEAKER_1]: First sentence.

2
00:00:01,150 --> 00:00:02,000
[SPEAKER_1]: Second sentence.

3
00:00:02,050 --> 00:00:03,000
[SPEAKER_2]: A different speaker.
""",
            encoding="utf-8",
        )
        source = self.artifacts.register(
            source_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            metadata={"language": "en"},
        )

        first_revision = self.handlers._materialize_subtitle_generation_plan(
            self.session.id,
            source,
            source_path,
            {"speech_block_merge_threshold": 100},
            "en",
        )
        second_revision = self.handlers._materialize_subtitle_generation_plan(
            self.session.id,
            source,
            source_path,
            {"speech_block_merge_threshold": 200},
            "en",
        )

        self.assertNotEqual(first_revision, second_revision)
        with self.database.session() as session:
            first = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == first_revision)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            second = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == second_revision)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
        self.assertEqual(3, len(first))
        self.assertEqual(2, len(second))
        self.assertEqual("First sentence. Second sentence.", second[0].text)
        self.assertEqual([1, 2], second[0].source_segment_ids_json)
        self.assertEqual(["SPEAKER_1", "SPEAKER_2"], [
            segment.speaker for segment in second
        ])

    def test_reviewed_subtitle_speech_uses_the_same_merged_partition(self):
        display_path = self.session_dir / "display.srt"
        display_path.write_text(
            """1
00:00:00,000 --> 00:00:01,000
[SPEAKER_1]: Dr. Imaoka arrived.

2
00:00:01,100 --> 00:00:02,000
[SPEAKER_1]: He waved.

3
00:00:02,050 --> 00:00:03,000
[SPEAKER_2]: Hello.
""",
            encoding="utf-8",
        )
        display = self.artifacts.register(
            display_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            metadata={"language": "en"},
        )
        speech_path = self.session_dir / "speech.srt"
        speech_path.write_text(
            """1
00:00:00,000 --> 00:00:01,000
Doctor eemahohkah arrived.

2
00:00:01,100 --> 00:00:02,000
He waved.

3
00:00:02,050 --> 00:00:03,000
Hello.
""",
            encoding="utf-8",
        )
        speech = self.artifacts.register(
            speech_path,
            kind="srt",
            role="tts_optimized",
            session_id=self.session.id,
            parent_ids=[display.id],
            metadata={
                "language": "en",
                "source_artifact_id": display.id,
            },
        )

        revision_id = self.handlers._materialize_subtitle_generation_plan(
            self.session.id,
            speech,
            speech_path,
            {"speech_block_merge_threshold": 200},
            "en",
        )

        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
        self.assertEqual(2, len(segments))
        self.assertEqual("Dr. Imaoka arrived. He waved.", segments[0].text)
        self.assertEqual(
            "Doctor eemahohkah arrived. He waved.",
            segments[0].optimized_text,
        )
        self.assertEqual([1, 2], segments[0].source_segment_ids_json)
        self.assertEqual(["SPEAKER_1", "SPEAKER_2"], [
            segment.speaker for segment in segments
        ])
        generation = GenerationService(
            self.database,
            JobQueue(self.database),
            WorkspaceSettingsService(self.database),
            self.artifacts,
            plan_refresher=self.handlers.refresh_generation_plan,
        )
        started = generation.start(self.session.id)
        with self.database.session() as session:
            run = session.get(GenerationRun, started["id"])
        self.assertEqual(speech.id, run.settings_snapshot_json["source_artifact_id"])
        self.assertTrue(
            run.settings_snapshot_json["text"]["use_existing_speech_plans"]
        )

    def test_reviewed_subtitle_partition_caps_both_aligned_text_variants(self):
        display_path = self.session_dir / "short-display.srt"
        display_path.write_text(
            """1
00:00:00,000 --> 00:00:01,000
[SPEAKER_1]: One.

2
00:00:01,100 --> 00:00:02,000
[SPEAKER_1]: Two.
""",
            encoding="utf-8",
        )
        display = self.artifacts.register(
            display_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            metadata={"language": "en"},
        )
        speech_path = self.session_dir / "expanded-speech.srt"
        speech_path.write_text(
            """1
00:00:00,000 --> 00:00:01,000
Expanded pronunciation for cue one.

2
00:00:01,100 --> 00:00:02,000
Expanded pronunciation for cue two.
""",
            encoding="utf-8",
        )
        speech = self.artifacts.register(
            speech_path,
            kind="srt",
            role="tts_optimized",
            session_id=self.session.id,
            parent_ids=[display.id],
            metadata={"source_artifact_id": display.id, "language": "en"},
        )

        revision_id = self.handlers._materialize_subtitle_generation_plan(
            self.session.id,
            speech,
            speech_path,
            {
                "speech_block_max_chars": 50,
                "speech_block_merge_threshold": 200,
            },
            "en",
        )

        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
        self.assertEqual([[1], [2]], [
            segment.source_segment_ids_json for segment in segments
        ])
        self.assertEqual(
            [
                "Expanded pronunciation for cue one.",
                "Expanded pronunciation for cue two.",
            ],
            [segment.optimized_text for segment in segments],
        )

    def test_reviewed_long_cue_is_not_duplicated_by_display_splitting(self):
        display_path = self.session_dir / "long-display.srt"
        display_path.write_text(
            """1
00:00:00,000 --> 00:00:02,000
This display cue is deliberately longer than twenty characters.
""",
            encoding="utf-8",
        )
        display = self.artifacts.register(
            display_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            metadata={"language": "en"},
        )
        speech_path = self.session_dir / "single-reviewed-cue.srt"
        speech_path.write_text(
            """1
00:00:00,000 --> 00:00:02,000
A single reviewed cue.
""",
            encoding="utf-8",
        )
        speech = self.artifacts.register(
            speech_path,
            kind="srt",
            role="tts_optimized",
            session_id=self.session.id,
            parent_ids=[display.id],
            metadata={"source_artifact_id": display.id, "language": "en"},
        )

        revision_id = self.handlers._materialize_subtitle_generation_plan(
            self.session.id,
            speech,
            speech_path,
            {
                "speech_block_max_chars": 20,
                "speech_block_merge_threshold": 200,
            },
            "en",
        )

        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
        self.assertEqual(2, len(segments))
        self.assertTrue(
            all(segment.source_segment_ids_json == [1] for segment in segments)
        )
        self.assertEqual(1, len({segment.alignment_group for segment in segments}))
        self.assertEqual(
            "A single reviewed cue.",
            " ".join(segment.optimized_text for segment in segments),
        )
        self.assertTrue(all(len(segment.optimized_text) <= 20 for segment in segments))

    def test_full_new_run_rematerializes_plan_with_current_merge_threshold(self):
        with self.database.session() as session:
            session.get(SessionRecord, self.session.id).workflow_kind = "voiceover"
        source_path = self.session_dir / "rerun-threshold.srt"
        source_path.write_text(
            """1
00:00:00,000 --> 00:00:01,000
[SPEAKER_1]: First sentence.

2
00:00:01,150 --> 00:00:02,000
[SPEAKER_1]: Second sentence.
""",
            encoding="utf-8",
        )
        source = self.artifacts.register(
            source_path,
            kind="srt",
            role="translation",
            session_id=self.session.id,
            metadata={"language": "en"},
        )
        old_revision = self.handlers._materialize_subtitle_generation_plan(
            self.session.id,
            source,
            source_path,
            {"speech_block_merge_threshold": 100},
            "en",
        )
        settings = WorkspaceSettingsService(self.database)
        settings.update(
            self.session.id,
            "tts",
            0,
            {"speech_block_merge_threshold": 200},
        )
        generation = GenerationService(
            self.database,
            JobQueue(self.database),
            settings,
            self.artifacts,
            plan_refresher=self.handlers.refresh_generation_plan,
        )

        started = generation.start(self.session.id)

        with self.database.session() as session:
            run = session.get(GenerationRun, started["id"])
            plan = session.scalar(
                select(GenerationPlan).where(
                    GenerationPlan.session_id == self.session.id
                )
            )
            old_count = session.scalar(
                select(func.count())
                .select_from(GenerationSegment)
                .where(GenerationSegment.plan_revision_id == old_revision)
            )
            new_segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(
                        GenerationSegment.plan_revision_id
                        == run.plan_revision_id
                    )
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )

        self.assertNotEqual(old_revision, run.plan_revision_id)
        self.assertEqual(run.plan_revision_id, plan.active_revision_id)
        self.assertEqual(2, old_count)
        self.assertEqual(1, len(new_segments))
        self.assertEqual(
            "First sentence. Second sentence.",
            new_segments[0].text,
        )
        self.assertEqual(
            200,
            run.settings_snapshot_json["tts"][
                "speech_block_merge_threshold"
            ],
        )
        self.assertEqual(
            source.id,
            run.settings_snapshot_json["source_artifact_id"],
        )

    def test_mark_output_assemblies_stale_preserves_other_runs(self):
        plan_revision_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "One."}],
            settings={"language": "en"},
        )
        with self.database.session() as session:
            first_run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=plan_revision_id,
                sequence_number=1,
                operation="generate",
                status="completed",
            )
            second_run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=plan_revision_id,
                sequence_number=2,
                operation="generate",
                status="completed",
            )
            session.add_all([first_run, second_run])
            session.flush()
            first_assembly = OutputAssembly(session_id=self.session.id, generation_run_id=first_run.id, status="completed")
            second_assembly = OutputAssembly(session_id=self.session.id, generation_run_id=second_run.id, status="completed")
            selection_assembly = OutputAssembly(session_id=self.session.id, generation_run_id=None, status="completed")
            session.add_all([first_assembly, second_assembly, selection_assembly])
            session.flush()
            ids = (first_run.id, first_assembly.id, second_assembly.id, selection_assembly.id)

            mark_output_assemblies_stale(session, self.session.id, generation_run_id=second_run.id)
            self.assertEqual("completed", session.get(OutputAssembly, ids[1]).status)
            self.assertEqual("stale", session.get(OutputAssembly, ids[2]).status)
            self.assertEqual("stale", session.get(OutputAssembly, ids[3]).status)

            for assembly_id in ids[1:]:
                session.get(OutputAssembly, assembly_id).status = "completed"
            mark_output_assemblies_stale(session, self.session.id)
            self.assertEqual("completed", session.get(OutputAssembly, ids[1]).status)
            self.assertEqual("completed", session.get(OutputAssembly, ids[2]).status)
            self.assertEqual("stale", session.get(OutputAssembly, ids[3]).status)

    def test_url_download_uses_ytdlp_and_records_provenance(self):
        captured = {}
        progress_updates = []
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
                for hook in captured["progress_hooks"]:
                    hook({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100})
                    hook({"status": "downloading", "downloaded_bytes": 100, "total_bytes": 100})
                    hook({"status": "finished"})
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
                lambda value, detail=None: progress_updates.append((value, detail)),
                threading.Event(),
            )

        artifact, output = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual(output, destination.resolve())
        self.assertEqual(artifact.metadata_json["downloader"], "yt-dlp")
        self.assertTrue(captured["noplaylist"])
        self.assertTrue(captured["restrictfilenames"])
        self.assertTrue(
            any(detail == "Downloading source — 50%" for _value, detail in progress_updates)
        )
        self.assertTrue(
            any(
                detail == "Source download complete; processing media"
                for _value, detail in progress_updates
            )
        )
        self.assertEqual(1.0, progress_updates[-1][0])

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

    def test_automatic_audio_generation_uses_the_same_streaming_batch_contract(self):
        prepared_path = self.session_dir / "batch-ready.json"
        prepared_path.write_text(
            json.dumps(
                [
                    {"original_sentence": "First batch sentence."},
                    {"original_sentence": "Second batch sentence."},
                    {"original_sentence": "Third batch sentence."},
                ]
            ),
            encoding="utf-8",
        )
        prepared = self.artifacts.register(
            prepared_path,
            kind="json",
            role="prepared_text",
            session_id=self.session.id,
        )
        streamed_ids = []

        def stream(items, *, batch_size, **_options):
            self.assertEqual(2, batch_size)
            streamed_ids.extend(item.id for item in items)
            yield from (
                TtsBatchResult(
                    id=item.id,
                    audio=AudioSegment.silent(duration=25),
                )
                for item in items
            )

        with (
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesis_capabilities",
                return_value=TtsCapabilities(
                    batch_synthesis=True,
                    streaming_batch=True,
                    default_batch_size=10,
                    max_batch_size=32,
                ),
            ),
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesize_batch",
                side_effect=stream,
            ) as generate_batch,
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesize",
                side_effect=AssertionError("ordinary synthesis must not be used"),
            ),
        ):
            result = self.handlers.generate_audiobook_audio(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": prepared.id,
                    "settings": {
                        "service": "kobold_qwen",
                        "model": "Prebuilt Voices",
                        "voice": "Ryan",
                        "tts_batch_size": 2,
                    },
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual(3, result["segments"])
        self.assertEqual(3, len(streamed_ids))
        generate_batch.assert_called_once()
        with self.database.session() as session:
            self.assertEqual(3, session.scalar(select(func.count()).select_from(AudioTake)))

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

    def test_reviewed_pronunciation_applies_without_llm_and_reaches_tts(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        PronunciationLibrary(self.database).create(
            {
                "source_form": "existential threat",
                "phonetic": "egzistenszial fret",
                "language": "en",
                "status": "reviewed",
            }
        )
        with mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=AssertionError("the dictionary-only path must not call an LLM"),
        ):
            optimized, model = self.handlers._optimize_generation_texts(
                self.session.id,
                segment_ids,
                ["An existential threat."],
                {
                    "llm_tts_optimization": False,
                    "apply_reviewed_pronunciations": True,
                    "language": "en",
                    "service": "XTTS",
                },
                threading.Event(),
                self.progress,
            )
        self.assertEqual(["An egzistenszial fret."], optimized)
        self.assertEqual("", model)
        with self.database.session() as session:
            self.assertEqual(
                "An existential threat.",
                session.get(GenerationSegment, segment_ids[0]).text,
            )

        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={
                    "text": {
                        "llm_tts_optimization": False,
                        "apply_reviewed_pronunciations": True,
                    },
                    "tts": {"service": "XTTS", "language": "en"},
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id
        with mock.patch.object(
            self.handlers.tts_providers,
            "synthesize",
            return_value=AudioSegment.silent(duration=25),
        ) as synthesize:
            result = self.handlers.run_generation(
                {"generation_run_id": run_id, "operation": "generate"},
                self.progress,
                threading.Event(),
            )
        self.assertEqual("completed", result["status"])
        self.assertEqual("An egzistenszial fret.", synthesize.call_args.args[0])

    def test_reviewed_pronunciation_toggle_off_leaves_tts_text_untouched(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        PronunciationLibrary(self.database).create(
            {
                "source_form": "existential threat",
                "phonetic": "egzistenszial fret",
                "language": "en",
                "status": "reviewed",
            }
        )
        with mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=AssertionError("the dictionary-only path must not call an LLM"),
        ):
            optimized, _model = self.handlers._optimize_generation_texts(
                self.session.id,
                segment_ids,
                ["An existential threat."],
                {
                    "llm_tts_optimization": False,
                    "apply_reviewed_pronunciations": False,
                    "language": "en",
                    "service": "XTTS",
                },
                threading.Event(),
                self.progress,
            )
        self.assertEqual(["An existential threat."], optimized)

    def test_selected_alternate_uses_its_pronunciation_language_and_backend(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        library = PronunciationLibrary(self.database)
        library.create(
            {
                "source_form": "existential threat",
                "phonetic": "egzistenszial fret",
                "language": "en",
                "backend": "XTTS",
                "status": "reviewed",
            }
        )
        library.create(
            {
                "source_form": "existential threat",
                "phonetic": "egzystencjalny tret",
                "language": "pl",
                "backend": "ElevenLabs",
                "status": "reviewed",
            }
        )

        with self.database.session() as session:
            source_run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                sequence_number=1,
                status="queued",
                settings_snapshot_json={
                    "text": {
                        "llm_tts_optimization": False,
                        "apply_reviewed_pronunciations": True,
                    },
                    "tts": {"service": "XTTS", "language": "en"},
                },
            )
            alternate_run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                sequence_number=2,
                status="queued",
                settings_snapshot_json={
                    "text": {
                        "llm_tts_optimization": False,
                        "apply_reviewed_pronunciations": True,
                    },
                    "tts": {"service": "XTTS", "language": "en"},
                    "selected_segment_override": {
                        "tts": {
                            "service": "ElevenLabs",
                            "model": "eleven_multilingual_v2",
                            "voice": "alternate-voice",
                            "language": "pl",
                        }
                    },
                },
            )
            session.add_all([source_run, alternate_run])
            session.flush()
            source_run_id = source_run.id
            alternate_run_id = alternate_run.id

        synthesis = []

        def synthesize(text, settings, **_kwargs):
            synthesis.append((text, dict(settings)))
            return AudioSegment.silent(duration=25)

        with (
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesize",
                side_effect=synthesize,
            ),
            mock.patch.object(
                self.handlers,
                "_optimize_generation_texts",
                wraps=self.handlers._optimize_generation_texts,
            ) as optimize,
        ):
            source_result = self.handlers.run_generation(
                {"generation_run_id": source_run_id, "operation": "generate"},
                self.progress,
                threading.Event(),
            )
            alternate_result = self.handlers.run_generation(
                {
                    "generation_run_id": alternate_run_id,
                    "operation": "regenerate",
                },
                self.progress,
                threading.Event(),
            )

        self.assertEqual("completed", source_result["status"])
        self.assertEqual("completed", alternate_result["status"])
        self.assertEqual("An egzistenszial fret.", synthesis[0][0])
        self.assertEqual("An egzystencjalny tret.", synthesis[1][0])
        self.assertEqual("XTTS", synthesis[0][1]["service"])
        self.assertEqual("ElevenLabs", synthesis[1][1]["service"])
        alternate_context = optimize.call_args_list[1].kwargs[
            "pronunciation_settings"
        ]
        self.assertEqual("ElevenLabs", alternate_context["service"])
        self.assertLessEqual(
            set(alternate_context),
            {"service", "tts_service", "backend", "openai_audio_endpoint"},
        )
        self.assertNotIn("api_key", alternate_context)
        self.assertNotIn("provider_configs", alternate_context)
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            self.assertEqual("An existential threat.", segment.text)

    def test_custom_alternate_pronunciation_uses_catalogue_provider_id(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        PronunciationLibrary(self.database).create(
            {
                "source_form": "existential threat",
                "phonetic": "egzystencjalny tret",
                "language": "pl",
                "backend": "catalogue-provider",
                "status": "reviewed",
            }
        )
        optimized, _model = self.handlers._optimize_generation_texts(
            self.session.id,
            segment_ids,
            ["An existential threat."],
            {
                "llm_tts_optimization": False,
                "apply_reviewed_pronunciations": True,
                "language": "en",
                "service": "XTTS",
            },
            threading.Event(),
            self.progress,
            pronunciation_settings={
                "service": "Custom",
                "openai_audio_endpoint": "catalogue-provider",
            },
            pronunciation_language="pl",
        )
        self.assertEqual(["An egzystencjalny tret."], optimized)

    def test_structured_alternate_pronunciation_is_protected_once(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        library = PronunciationLibrary(self.database)
        entry = library.create(
            {
                "source_form": "existential threat",
                "phonetic": "egzystencjalny tret",
                "language": "pl",
                "backend": "ElevenLabs",
                "status": "reviewed",
            }
        )
        hydrated = {
            "llm_tts_optimization": True,
            "speech_optimization_mode": "guarded",
            "speech_plan_save_proposals": False,
            "llm_provider_configs": [],
            "llm_default_model": "local/test",
            "request_timeout_seconds": 30,
            "tts_optimization_model": "local/test",
            "service": "XTTS",
            "language": "en",
        }
        seen = {}

        def optimize(*_args, on_batch=None, on_plan_batch=None, **kwargs):
            known = kwargs["known_pronunciation_resolver"](
                "An existential threat.", "pl"
            )
            seen["known"] = known
            if on_batch:
                on_batch([(0, "An egzystencjalny tret.")])
            if on_plan_batch:
                on_plan_batch(
                    [
                        (
                            0,
                            "An egzystencjalny tret.",
                            {
                                "version": 1,
                                "source_hash": self.handlers._optimization_text_hash(
                                    "An existential threat."
                                ),
                                "mode_requested": "guarded",
                                "model": "local/test",
                            },
                        )
                    ]
                )
            return ["An egzystencjalny tret."], OptimizationUsage()

        with (
            mock.patch.object(
                self.handlers,
                "_with_database_llm_settings",
                return_value=hydrated,
            ),
            mock.patch(
                "pandrator.web.tts_optimization.optimize_texts",
                side_effect=optimize,
            ),
        ):
            optimized, _model = self.handlers._optimize_generation_texts(
                self.session.id,
                segment_ids,
                ["An existential threat."],
                hydrated,
                threading.Event(),
                self.progress,
                pronunciation_settings={"service": "ElevenLabs"},
                pronunciation_language="pl",
            )

        self.assertEqual(["An egzystencjalny tret."], optimized)
        self.assertEqual([entry["id"]], [item["id"] for item in seen["known"]])
        self.assertEqual(
            "egzystencjalny tret",
            seen["known"][0]["phonetic"].replace("-", ""),
        )

    def test_saved_structured_alternate_plan_reuses_matching_pronunciation_context(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        entry = PronunciationLibrary(self.database).create(
            {
                "source_form": "existential threat",
                "phonetic": "egzystencjalny tret",
                "language": "pl",
                "backend": "ElevenLabs",
                "status": "reviewed",
            }
        )
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            segment.optimized_text = "An egzystencjalny tret."
            segment.optimization_source_hash = self.handlers._optimization_text_hash(
                "An existential threat."
            )
            segment.optimization_status = "optimized"
            segment.optimization_model = "local/test"
            segment.speech_plan_json = {
                "source_hash": self.handlers._optimization_text_hash(
                    "An existential threat."
                ),
                "mode_requested": "guarded",
                "model": "local/test",
                "known_pronunciations": [
                    {
                        "entry_id": entry["id"],
                        "entry_revision": entry["revision"],
                    }
                ],
            }
        settings = {
            "llm_tts_optimization": True,
            "speech_optimization_mode": "guarded",
            "apply_reviewed_pronunciations": True,
            "use_existing_speech_plans": True,
            "llm_provider_configs": [],
            "llm_default_model": "local/test",
            "request_timeout_seconds": 30,
            "tts_optimization_model": "local/test",
            "service": "XTTS",
            "language": "en",
        }
        with mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=AssertionError("matching alternate plan must be reused"),
        ):
            optimized, _model = self.handlers._optimize_generation_texts(
                self.session.id,
                segment_ids,
                ["An existential threat."],
                settings,
                threading.Event(),
                self.progress,
                pronunciation_settings={"service": "ElevenLabs"},
                pronunciation_language="pl",
            )
        self.assertEqual(["An egzystencjalny tret."], optimized)

    def test_reviewed_pronunciation_reapplies_when_saved_speech_text_is_reused(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "An existential threat.", "language": "en"}],
            settings={},
        )
        PronunciationLibrary(self.database).create(
            {
                "source_form": "existential threat",
                "phonetic": "egzistenszial fret",
                "language": "en",
                "status": "reviewed",
            }
        )
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            segment.optimized_text = "An existential threat."
            segment.optimization_source_hash = self.handlers._optimization_text_hash(
                "An existential threat."
            )
            segment.optimization_status = "optimized"
            segment.optimization_model = "local/test"
        settings = {
            "llm_tts_optimization": False,
            "apply_reviewed_pronunciations": True,
            "use_existing_speech_plans": True,
            "language": "en",
            "service": "XTTS",
        }
        with mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=AssertionError("saved-plan reuse must not call an LLM"),
        ):
            optimized, _model = self.handlers._optimize_generation_texts(
                self.session.id,
                segment_ids,
                ["An existential threat."],
                settings,
                threading.Event(),
                self.progress,
            )
        self.assertEqual(["An egzistenszial fret."], optimized)

    def test_structured_speech_plan_is_persisted_and_pronunciation_stays_proposed(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "Imaoka arrived.", "language": "en"}],
            settings={},
        )
        hydrated = {
            "llm_tts_optimization": True,
            "speech_optimization_mode": "guarded",
            "speech_plan_save_proposals": True,
            "llm_provider_configs": [],
            "llm_default_model": "local/test",
            "request_timeout_seconds": 30,
            "tts_optimization_model": "local/test",
            "service": "XTTS",
            "language": "en",
        }
        plan = {
            "version": 1,
            "case_id": "case-1",
            "status": "valid",
            "mode_requested": "guarded",
            "mode_used": "guarded",
            "model": "local/test",
            "source_hash": self.handlers._optimization_text_hash(
                "Imaoka arrived."
            ),
            "language": "en",
            "voice_language": "en",
            "compiled_text": "eemahohkah arrived.",
            "known_pronunciations": [],
            "candidates": [
                {
                    "id": "P1",
                    "text": "Imaoka",
                    "task": "pronunciation",
                    "signals": ["foreign_name_shape"],
                }
            ],
            "decisions": [
                {
                    "span_id": "P1",
                    "action": "pronounce",
                    "spoken": "ee-mah-oh-kah",
                    "confidence": "high",
                }
            ],
            "discoveries": [],
            "validation": {"valid": True, "errors": [], "warnings": []},
        }

        def optimize(*_args, on_batch=None, on_plan_batch=None, **_kwargs):
            if on_batch:
                on_batch([(0, "eemahohkah arrived.")])
            if on_plan_batch:
                on_plan_batch([(0, "eemahohkah arrived.", plan)])
            return ["eemahohkah arrived."], OptimizationUsage()

        with mock.patch.object(
            self.handlers,
            "_with_database_llm_settings",
            return_value=hydrated,
        ), mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            side_effect=optimize,
        ):
            optimized, _model = self.handlers._optimize_generation_texts(
                self.session.id,
                segment_ids,
                ["Imaoka arrived."],
                hydrated,
                threading.Event(),
                self.progress,
            )

        self.assertEqual(["eemahohkah arrived."], optimized)
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            proposal = session.scalar(select(PronunciationEntry))
            self.assertEqual("Imaoka arrived.", segment.text)
            self.assertEqual("eemahohkah arrived.", segment.optimized_text)
            self.assertEqual("valid", segment.speech_plan_json["status"])
            self.assertEqual(proposal.id, segment.speech_plan_json["proposals"][0]["id"])
            self.assertEqual("proposed", proposal.status)

    def test_reviewed_document_optimization_keeps_display_and_speech_fields_separate(self):
        _revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [
                {
                    "text": "Dr. Imaoka arrived.",
                    "tts_optimized_sentence": "Doctor eemahohkah arrived.",
                    "speech_plan": {
                        "version": 1,
                        "status": "valid",
                        "model": "local/test",
                    },
                }
            ],
            settings={},
        )
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_ids[0])
            self.assertEqual("Dr. Imaoka arrived.", segment.text)
            self.assertEqual(
                "Doctor eemahohkah arrived.",
                segment.optimized_text,
            )
            self.assertEqual("valid", segment.speech_plan_json["status"])

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

    def test_generation_streams_negotiated_batches_and_commits_each_take_in_order(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": f"Sentence {index}."} for index in range(3)],
            settings={},
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={
                    "text": {"llm_tts_optimization": False},
                    "tts": {
                        "service": "kobold_qwen",
                        "model": "Prebuilt Voices",
                        "voice": "Ryan",
                        "tts_batch_size": 2,
                    },
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id

        observed_completed_counts = []

        def stream(items, *, batch_size, **_options):
            self.assertEqual(2, batch_size)
            self.assertEqual(segment_ids, [item.id for item in items])
            for index, item in enumerate(items):
                with self.database.session() as session:
                    observed_completed_counts.append(
                        session.scalar(
                            select(func.count())
                            .select_from(AudioTake)
                            .where(AudioTake.generation_run_id == run_id)
                        )
                    )
                yield TtsBatchResult(
                    id=item.id,
                    audio=AudioSegment.silent(duration=25 + index),
                )

        with (
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesis_capabilities",
                return_value=TtsCapabilities(
                    batch_synthesis=True,
                    streaming_batch=True,
                    default_batch_size=10,
                    max_batch_size=32,
                ),
            ),
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesize_batch",
                side_effect=stream,
            ) as generate_batch,
            mock.patch.object(
                self.handlers.tts_providers,
                "synthesize",
                side_effect=AssertionError("ordinary synthesis must not be used"),
            ),
        ):
            result = self.handlers.run_generation(
                {"generation_run_id": run_id, "operation": "generate"},
                self.progress,
                threading.Event(),
            )

        self.assertEqual("completed", result["status"])
        self.assertEqual([0, 1, 2], observed_completed_counts)
        generate_batch.assert_called_once()
        with self.database.session() as session:
            takes = list(
                session.scalars(
                    select(AudioTake)
                    .where(AudioTake.generation_run_id == run_id)
                    .order_by(AudioTake.created_at)
                ).all()
            )
        self.assertEqual(segment_ids, [take.generation_segment_id for take in takes])

    def test_generated_segment_database_writes_roll_back_as_one_unit(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "Atomic segment."}],
            settings={},
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={
                    "text": {"llm_tts_optimization": False},
                    "tts": {"service": "XTTS"},
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id

        with (
            mock.patch(
                "pandrator.logic.tts_handler.text_to_audio",
                return_value=AudioSegment.silent(duration=25),
            ),
            mock.patch(
                "pandrator.web.workspace.mark_output_assemblies_stale",
                side_effect=RuntimeError("forced unit-of-work rollback"),
            ),
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "forced unit-of-work rollback",
            ):
                self.handlers.run_generation(
                    {
                        "generation_run_id": run_id,
                        "operation": "generate",
                    },
                    self.progress,
                    threading.Event(),
                )

        with self.database.session() as session:
            self.assertEqual(
                0,
                session.scalar(
                    select(func.count())
                    .select_from(AudioTake)
                    .where(AudioTake.generation_run_id == run_id)
                ),
            )
            self.assertEqual(
                0,
                session.scalar(
                    select(func.count())
                    .select_from(Artifact)
                    .where(
                        Artifact.session_id == self.session.id,
                        Artifact.role == "generation_take",
                    )
                ),
            )
            self.assertEqual(
                0,
                session.scalar(
                    select(func.count())
                    .select_from(UsageEvent)
                    .where(UsageEvent.generation_run_id == run_id)
                ),
            )
            self.assertEqual(
                "failed",
                session.get(GenerationSegment, segment_ids[0]).status,
            )
            self.assertEqual("failed", session.get(GenerationRun, run_id).status)
        self.assertEqual(
            [],
            list(
                (
                    self.session_dir
                    / "generation"
                    / revision_id
                    / segment_ids[0]
                ).glob("*.wav")
            ),
        )

    def test_signal_verification_marks_a_run_relative_loud_outlier(self):
        revision_id, segment_ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": f"Sentence number {index}."} for index in range(8)],
            settings={},
        )
        with self.database.session() as session:
            run = GenerationRun(
                session_id=self.session.id,
                plan_revision_id=revision_id,
                status="queued",
                settings_snapshot_json={
                    "text": {"llm_tts_optimization": False},
                    "tts": {"service": "XTTS"},
                    "audio": {"audio_verification_mode": "signal"},
                },
            )
            session.add(run)
            session.flush()
            run_id = run.id
        clean = Sine(220).to_audio_segment(duration=950).apply_gain(-18) + AudioSegment.silent(duration=50)
        loud = clean.apply_gain(7)
        generated = [clean] * 7 + [loud]

        with mock.patch("pandrator.logic.tts_handler.text_to_audio", side_effect=generated):
            result = self.handlers.run_generation(
                {"generation_run_id": run_id, "operation": "generate"},
                self.progress,
                threading.Event(),
            )

        self.assertEqual(1, result["verification_warnings"])
        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.id.in_(segment_ids))
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )
            self.assertFalse(any(item.marked for item in segments[:-1]))
            self.assertTrue(segments[-1].marked)
            loud_segment_id = segments[-1].id
            take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == run_id,
                    AudioTake.generation_segment_id == loud_segment_id,
                )
            )
            artifact = session.get(Artifact, take.artifact_id)
            verification = artifact.metadata_json["audio_verification"]
            self.assertEqual("warning", verification["status"])
            self.assertIn("run_rms_outlier", {item["code"] for item in verification["issues"]})
        listed = GenerationService(
            self.database,
            JobQueue(self.database),
            WorkspaceSettingsService(self.database),
        ).list_segments(self.session.id)
        loud_take = next(
            item for item in listed["items"] if item["id"] == loud_segment_id
        )["takes"][0]
        self.assertEqual("warning", loud_take["audio_verification"]["status"])

    def test_targeted_generation_appends_a_new_take_without_overwriting_the_old_one(self):
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
            self.assertEqual(2, len(takes))
            original = next(item for item in takes if item.id == take_id)
            replacement = next(item for item in takes if item.id != take_id)
            self.assertEqual(25, original.duration_ms)
            self.assertEqual(original_artifact_id, original.artifact_id)
            self.assertFalse(original.is_active)
            self.assertEqual(40, replacement.duration_ms)
            self.assertNotEqual(original_artifact_id, replacement.artifact_id)
            self.assertTrue(replacement.is_active)
            self.assertEqual("current", session.get(Artifact, original_artifact_id).state)

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
        progress_updates = []
        result = self.handlers.export(
            {
                "session_id": subtitle_session.id,
                "source_artifact_id": uploaded.id,
                "settings": {"subtitle_selection": "source", "subtitle_mode": "none"},
            },
            lambda value, detail=None: progress_updates.append((value, detail)),
            threading.Event(),
        )
        self.assertEqual(len(result["artifact_ids"]), 1)
        self.assertEqual(1.0, progress_updates[-1][0])
        self.assertEqual(
            sorted(value for value, _detail in progress_updates),
            [value for value, _detail in progress_updates],
        )
        self.assertTrue(
            any(detail == "Prepared subtitle track 1 of 1" for _value, detail in progress_updates)
        )
        self.assertTrue(
            any(detail == "Exported track 1 of 1" for _value, detail in progress_updates)
        )
        with self.database.session() as session:
            exported = session.scalar(select(Artifact).where(Artifact.id == result["artifact_ids"][0]))
            self.assertEqual(exported.role, "export_subtitle_source")
            snapshot = exported.metadata_json["output_settings"]
            self.assertEqual("source", snapshot["sections"]["output"]["subtitle_selection"])
            self.assertEqual("none", snapshot["sections"]["output"]["subtitle_mode"])
            self.assertEqual(64, len(snapshot["settings_hash"]))
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

    def test_media_export_rejects_generated_audio_modes_without_an_assembly(self):
        voiceover = self.sessions.create("Missing generated audio", workflow_kind="voiceover")
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        media_path = session_dir / "source.mp4"
        media_path.write_bytes(b"media fixture")
        self.artifacts.register(
            media_path,
            kind="source",
            role="upload",
            session_id=voiceover.id,
        )

        with self.assertRaisesRegex(ValueError, "require an explicit audio mode"):
            self.handlers.export(
                {
                    "session_id": voiceover.id,
                    "settings": {"export_mode": "media"},
                },
                self.progress,
                threading.Event(),
            )

        for audio_mode in ("mixed", "dubbing_only"):
            with self.subTest(audio_mode=audio_mode):
                with self.assertRaisesRegex(ValueError, "requires assembled generated audio"):
                    self.handlers.export(
                        {
                            "session_id": voiceover.id,
                            "settings": {
                                "export_mode": "media",
                                "audio_mode": audio_mode,
                            },
                        },
                        self.progress,
                        threading.Event(),
                    )

        with self.database.session() as session:
            exported = session.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(
                    Artifact.session_id == voiceover.id,
                    Artifact.kind == "export",
                )
            )
        self.assertEqual(0, exported)

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

    def test_no_source_export_contract_cannot_pick_up_a_legacy_video(self):
        voiceover = self.sessions.create(
            "Pinned no-source voiceover",
            workflow_kind="voiceover",
        )
        session_dir = self.paths.sessions / voiceover.storage_key
        session_dir.mkdir()
        media_path = session_dir / "detached.mp4"
        media_path.write_bytes(b"legacy media must not be selected")
        self.artifacts.register(
            media_path,
            kind="source",
            role="upload",
            session_id=voiceover.id,
            metadata={"original_filename": media_path.name},
        )
        dubbing_path = session_dir / "voiceover.wav"
        AudioSegment.silent(duration=40).export(
            dubbing_path,
            format="wav",
        ).close()
        dubbing = self.artifacts.register(
            dubbing_path,
            kind="audio",
            role="assembled_audio",
            session_id=voiceover.id,
        )

        result = self.handlers.export(
            {
                "session_id": voiceover.id,
                "settings": {
                    "export_mode": "media",
                    "audio_mode": "dubbing_only",
                    "format": "wav",
                },
                "export_contract": {
                    "version": 1,
                    "workflow_kind": "voiceover",
                    "export_mode": "media",
                    "audio_mode": "dubbing_only",
                    "source_artifact_id": None,
                    "source_content_hash": None,
                    "source_profile": "none",
                    "source_resolution": "none",
                },
            },
            self.progress,
            threading.Event(),
        )

        exported, output = self.artifacts.resolve(result["artifact_ids"][-1])
        self.assertEqual(".wav", output.suffix)
        self.assertEqual(f"export_{dubbing.role}", exported.role)
        with self.database.session() as session:
            parent_ids = {
                edge.parent_artifact_id
                for edge in session.scalars(
                    select(ArtifactEdge).where(
                        ArtifactEdge.child_artifact_id == exported.id
                    )
                ).all()
            }
        self.assertEqual({dubbing.id}, parent_ids)

    @unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg qualification requires ffmpeg")
    def test_audio_source_voiceover_uses_an_explicit_duration_bounded_controlled_mix(self):
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
            {
                "session_id": voiceover.id,
                "settings": {
                    "export_mode": "media",
                    "audio_mode": "mixed",
                    "format": "wav",
                },
            },
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
                "continuation_threshold_ms": 3000,
                "max_internal_gap_ms": 1800,
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

    def test_generation_plan_uses_short_clause_pauses_and_inherits_language(self):
        revision_id, _ = self.handlers._store_generation_plan(
            self.session.id,
            [
                {"text": "An internal clause,", "sentence_continues_after": True},
                {"text": "the sentence ends."},
                {"text": "The paragraph ends.", "paragraph": "yes"},
                {"text": "A legacy internal clause,", "split_part": "0a"},
                {"text": "Explicit override.", "language": "pl", "voice": "alice"},
            ],
            settings={"language": "en", "sentence_silence_ms": 300, "paragraph_silence_ms": 900},
        )

        with self.database.session() as session:
            segments = list(
                session.scalars(
                    select(GenerationSegment)
                    .where(GenerationSegment.plan_revision_id == revision_id)
                    .order_by(GenerationSegment.ordinal)
                ).all()
            )

        self.assertEqual([100, 300, 900, 100, 300], [segment.silence_after_ms for segment in segments])
        self.assertEqual([None, None, None, None, "pl"], [segment.language for segment in segments])
        self.assertEqual("alice", segments[-1].voice)

    def test_segment_language_and_voice_override_reach_tts_runtime(self):
        prepared_path = self.session_dir / "segment-overrides.json"
        prepared_path.write_text(
            json.dumps([{"original_sentence": "Cześć.", "language": "pl", "voice": "alice"}]),
            encoding="utf-8",
        )
        prepared = self.artifacts.register(
            prepared_path,
            kind="json",
            role="prepared_text",
            session_id=self.session.id,
        )

        with mock.patch(
            "pandrator.logic.tts_handler.text_to_audio",
            return_value=AudioSegment.silent(duration=25),
        ) as generate:
            self.handlers.generate_audiobook_audio(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": prepared.id,
                    "settings": {"service": "XTTS", "language": "en", "voice": "default"},
                },
                self.progress,
                threading.Event(),
            )

        runtime_settings = generate.call_args.args[1]
        self.assertEqual("pl", runtime_settings["language"])
        self.assertEqual("pl", runtime_settings["target_language"])
        self.assertEqual("alice", runtime_settings["voice"])
        self.assertEqual("alice", runtime_settings["speaker"])

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
        voiceover = self.sessions.create(
            "Export Matrix",
            workflow_kind="voiceover",
            source_language="en",
            target_language="pl",
        )
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
        )
        translation_srt = session_dir / "translation.srt"
        translation_srt.write_text("1\n00:00:00,050 --> 00:00:00,650\nWiersz docelowy\n", encoding="utf-8")
        translation = self.artifacts.register(
            translation_srt,
            kind="srt",
            role="translation",
            session_id=voiceover.id,
            parent_ids=[correction.id],
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
                            "burn_video_resolution": "720p",
                            "burn_video_quality": 23,
                            "burn_video_speed": "fast",
                            "burn_audio_codec": "aac",
                            "burn_audio_bitrate": "128k",
                        }
                    )
                elif subtitle_mode == "soft":
                    export_settings.update(
                        {
                            "video_transcode": True,
                            "burn_video_encoder": "libx264",
                            "burn_video_resolution": "720p",
                        }
                    )
                elif audio_mode == "dubbing_only":
                    export_settings.update(
                        {
                            "video_transcode": True,
                            "burn_video_encoder": "libx264",
                            "burn_video_resolution": "480p",
                            "burn_audio_bitrate": "128k",
                        }
                    )
                elif audio_mode == "mixed":
                    export_settings["burn_audio_bitrate"] = "96k"
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
                    video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
                    self.assertEqual(720, video_stream["height"])
                    self.assertEqual(["eng", "pol"], [stream.get("tags", {}).get("language") for stream in subtitle_streams])
                    self.assertEqual(
                        ["English", "Polish"],
                        [stream.get("tags", {}).get("handler_name") for stream in subtitle_streams],
                    )
                    self.assertEqual(1, subtitle_streams[1].get("disposition", {}).get("default"))
                    self.assertTrue(output.name.endswith("_soft.mp4"))
                    self.assertEqual(2, len(exported.metadata_json.get("subtitle_tracks", [])))
                    self.assertTrue(all(item.get("artifact_id") for item in exported.metadata_json["subtitle_tracks"]))
                    self.assertEqual(
                        ["English", "Polish"],
                        [item["title"] for item in exported.metadata_json["subtitle_tracks"]],
                    )
                    self.assertTrue(exported.metadata_json.get("video_transcoded"))
                    self.assertEqual("720p", exported.metadata_json.get("video_resolution"))
                if subtitle_mode == "burned":
                    video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
                    self.assertEqual((1280, 720), (video_stream["width"], video_stream["height"]))
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
                    self.assertEqual("720p", exported.metadata_json.get("video_resolution"))
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
                    self.assertEqual("96k", exported.metadata_json.get("audio_bitrate"))
                if audio_mode == "dubbing_only" and subtitle_mode == "none":
                    video_stream = next(stream for stream in streams if stream["codec_type"] == "video")
                    audio_stream = next(stream for stream in streams if stream["codec_type"] == "audio")
                    self.assertEqual(480, video_stream["height"])
                    self.assertEqual("aac", audio_stream["codec_name"])
                    self.assertEqual("128k", exported.metadata_json.get("audio_bitrate"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg qualification requires ffmpeg and ffprobe")
    def test_mixed_video_without_source_soundtrack_fails_closed(self):
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

        with self.assertRaisesRegex(ValueError, "no audio stream to mix"):
            self.handlers.export(
                {
                    "session_id": voiceover.id,
                    "settings": {
                        "export_mode": "media",
                        "audio_mode": "mixed",
                    },
                },
                self.progress,
                threading.Event(),
            )

        with self.database.session() as session:
            exported = session.scalar(
                select(func.count())
                .select_from(Artifact)
                .where(
                    Artifact.session_id == voiceover.id,
                    Artifact.kind == "export",
                )
            )
        self.assertEqual(0, exported)


if __name__ == "__main__":
    unittest.main()
