import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from copy import deepcopy
from unittest.mock import patch

from pydub import AudioSegment
from sqlalchemy import select

from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.models import Artifact, AudioTake, GenerationRun, Job
from pandrator.web.tts_providers import (
    TtsBatchResult,
    TtsCapabilities,
    TtsProviderError,
)


class GenerationRegenerationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.bootstrap = BootstrapTokenStore()
        token = self.bootstrap.issue()
        self.app = create_app(
            data_root=self.temporary.name,
            testing=True,
            bootstrap_tokens=self.bootstrap,
        )
        self.client = self.app.test_client()
        self.headers = {
            "X-CSRF-Token": self.client.post(
                "/api/v1/auth/bootstrap", json={"token": token}
            ).get_json()["csrf_token"]
        }
        self.database = self.app.extensions["pandrator"]["database"]
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": "Regeneration", "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.session_id = created.get_json()["id"]
        plan = self.client.post(
            f"/api/v1/sessions/{self.session_id}/generation-plan",
            json={
                "segments": [
                    {"text": "One"},
                    {"text": "Two"},
                    {"text": "Three"},
                ]
            },
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        self.segment_ids = [
            item["id"]
            for item in self.client.get(
                f"/api/v1/sessions/{self.session_id}/generation-segments"
            ).get_json()["items"]
        ]

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def _create_session_with_plan(self, name="Regeneration extra"):
        created = self.client.post(
            "/api/v1/sessions",
            json={"name": name, "workflow_kind": "audiobook"},
            headers=self.headers,
        )
        self.assertEqual(201, created.status_code, created.get_json())
        session_id = created.get_json()["id"]
        plan = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-plan",
            json={
                "segments": [
                    {"text": "One"},
                    {"text": "Two"},
                    {"text": "Three"},
                ]
            },
            headers=self.headers,
        )
        self.assertEqual(201, plan.status_code, plan.get_json())
        segments = self.client.get(
            f"/api/v1/sessions/{session_id}/generation-segments"
        )
        return session_id, [item["id"] for item in segments.get_json()["items"]]

    def _start(self, session_id=None, **payload):
        session_id = session_id or self.session_id
        response = self.client.post(
            f"/api/v1/sessions/{session_id}/generation-runs",
            json=payload,
            headers=self.headers,
        )
        self.assertEqual(202, response.status_code, response.get_json())
        return response.get_json()

    @contextmanager
    def _fake_tts(self, calls, batch_calls, *, batch_size):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]

        def synthesize(text, _settings, **_kwargs):
            calls.append(text)
            return AudioSegment.silent(duration=20)

        def synthesize_batch(items, **_kwargs):
            batch_calls.append([item.id for item in items])
            for item in items:
                calls.append(item.text)
                yield TtsBatchResult(
                    id=item.id,
                    audio=AudioSegment.silent(duration=20),
                )

        capabilities = TtsCapabilities(
            batch_synthesis=batch_size > 1,
            streaming_batch=batch_size > 1,
            default_batch_size=batch_size,
            max_batch_size=max(1, batch_size),
        )
        with (
            patch.object(
                handlers.tts_providers,
                "synthesize",
                side_effect=synthesize,
            ),
            patch.object(
                handlers.tts_providers,
                "synthesize_batch",
                side_effect=synthesize_batch,
            ),
            patch.object(
                handlers.tts_providers,
                "synthesis_capabilities",
                return_value=capabilities,
            ),
        ):
            yield handlers

    @staticmethod
    def _progress(_value, _detail=None):
        return None

    def _run_job(self, handlers, payload):
        return handlers.run_generation(
            payload,
            self._progress,
            threading.Event(),
        )

    def test_cloud_pause_saves_current_wave_and_resume_keeps_its_takes(self):
        root = self._start(
            run_override={"tts": {"service": "openai", "tts_concurrent_requests": 2}}
        )
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        calls = []
        barrier = threading.Barrier(2)

        def synthesize(text, _settings, **_options):
            calls.append(text)
            barrier.wait(timeout=5)
            if text == "One":
                with self.database.session() as session:
                    session.get(GenerationRun, root["id"]).pause_requested = True
            return AudioSegment.silent(duration=20)

        with patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize):
            result = self._run_job(handlers, {"generation_run_id": root["id"]})
        self.assertEqual("paused", result["status"])
        self.assertCountEqual(["One", "Two"], calls)
        with self.database.session() as session:
            takes = list(
                session.scalars(
                    select(AudioTake).where(AudioTake.generation_run_id == root["id"])
                )
            )
            self.assertEqual(2, len(takes))
            run = session.get(GenerationRun, root["id"])
            run.pause_requested = False

        calls.clear()

        def resume_synthesis(text, _settings, **_options):
            calls.append(text)
            return AudioSegment.silent(duration=20)

        with patch.object(
            handlers.tts_providers, "synthesize", side_effect=resume_synthesis
        ):
            result = self._run_job(
                handlers, {"generation_run_id": root["id"], "operation": "resume"}
            )
        self.assertEqual(["Three"], calls)
        self.assertEqual("completed", result["status"])
        with self.database.session() as session:
            self.assertEqual(
                3,
                len(
                    list(
                        session.scalars(
                            select(AudioTake).where(
                                AudioTake.generation_run_id == root["id"]
                            )
                        )
                    )
                ),
            )

    def test_cloud_failure_saves_other_wave_results_without_extra_retry(self):
        root = self._start(
            run_override={"tts": {"service": "openai", "tts_concurrent_requests": 2}}
        )
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        calls = []
        error = TtsProviderError(
            "openai", "synthesize", "Rate limit exhausted", retryable=True
        )

        def synthesize(text, _settings, **_options):
            calls.append(text)
            if text == "One":
                raise error
            return AudioSegment.silent(duration=20)

        with patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize):
            with self.assertRaises(TtsProviderError) as raised:
                self._run_job(handlers, {"generation_run_id": root["id"]})
        self.assertIs(error, raised.exception)
        self.assertCountEqual(["One", "Two"], calls)
        with self.database.session() as session:
            takes = list(
                session.scalars(
                    select(AudioTake).where(AudioTake.generation_run_id == root["id"])
                )
            )
            self.assertEqual(
                [self.segment_ids[1]], [take.generation_segment_id for take in takes]
            )
            self.assertEqual("failed", session.get(GenerationRun, root["id"]).status)
        calls.clear()

        def resume_synthesis(text, _settings, **_options):
            calls.append(text)
            return AudioSegment.silent(duration=20)

        with patch.object(
            handlers.tts_providers, "synthesize", side_effect=resume_synthesis
        ):
            result = self._run_job(
                handlers, {"generation_run_id": root["id"], "operation": "resume"}
            )
        self.assertCountEqual(["One", "Three"], calls)
        self.assertEqual("completed", result["status"])

    def test_automatic_cloud_failure_preserves_other_results_in_final_wave(self):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        source_path = handlers._session_dir(self.session_id) / "prepared.json"
        source_path.write_text(
            json.dumps([{"text": "One"}, {"text": "Two"}, {"text": "Three"}]),
            encoding="utf-8",
        )
        source = handlers.artifacts.register(
            source_path, kind="json", role="prepared_text", session_id=self.session_id
        )
        calls = []
        error = TtsProviderError(
            "openai", "synthesize", "Invalid voice", retryable=False
        )

        def synthesize(text, _settings, **_options):
            calls.append(text)
            if text == "One":
                raise error
            return AudioSegment.silent(duration=20)

        with patch.object(handlers.tts_providers, "synthesize", side_effect=synthesize):
            with self.assertRaises(TtsProviderError) as raised:
                handlers._generate_audio(
                    self.session_id,
                    source,
                    source_path,
                    {"service": "openai", "tts_concurrent_requests": 3},
                    self._progress,
                    threading.Event(),
                    role="generated_audio",
                )
        self.assertIs(error, raised.exception)
        self.assertCountEqual(["One", "Two", "Three"], calls)
        with self.database.session() as session:
            takes = list(session.scalars(select(AudioTake)))
            self.assertCountEqual(
                self.segment_ids[1:], [take.generation_segment_id for take in takes]
            )

    def test_repeated_default_regeneration_replaces_single_root_baton(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])

        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            second_run = session.get(GenerationRun, second["id"])
            first_job = session.get(Job, first["job_id"])
            second_job = session.get(Job, second["job_id"])
            self.assertEqual(root["id"], first_run.source_generation_run_id)
            self.assertEqual(root["id"], second_run.source_generation_run_id)
            self.assertEqual(root["id"], first_run.output_generation_run_id)
            self.assertEqual(root["id"], second_run.output_generation_run_id)
            self.assertTrue(root_run.pause_requested)
            self.assertEqual("pausing", root_run.status)
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertNotIn(
                "auto_resume_source_generation_run_id", first_job.payload_json
            )
            self.assertTrue(second_run.resume_source_on_completion)
            self.assertEqual(
                root["id"],
                second_job.payload_json["auto_resume_source_generation_run_id"],
            )

            root_run.status = "paused"

        listed = self.client.get(
            f"/api/v1/sessions/{self.session_id}/generation-runs"
        ).get_json()["items"]
        self.assertEqual({root["id"]}, {
            item["output_generation_run_id"] or item["id"]
            for item in listed
        })
        self.assertTrue(
            next(item for item in listed if item["id"] == root["id"])["label"].startswith(
                "Run 1:"
            )
        )
        self.assertTrue(
            next(item for item in listed if item["id"] == first["id"])["label"].startswith(
                "Run 1:"
            )
        )

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        self.assertIsNone(
            handlers._resume_generation_after_regeneration(first["id"], root["id"])
        )
        resumed_job_id = handlers._resume_generation_after_regeneration(
            second["id"], root["id"]
        )
        self.assertIsNotNone(resumed_job_id)
        with self.database.session() as session:
            self.assertEqual("queued", session.get(GenerationRun, root["id"]).status)
            self.assertEqual(
                resumed_job_id, session.get(GenerationRun, root["id"]).job_id
            )

    def test_new_regeneration_pauses_root_again_after_previous_resume(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "paused"
            session.get(GenerationRun, first["id"]).status = "completed"

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        resumed_job_id = handlers._resume_generation_after_regeneration(
            first["id"], root["id"]
        )
        self.assertIsNotNone(resumed_job_id)
        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])

        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            second_run = session.get(GenerationRun, second["id"])
            self.assertEqual("pausing", root_run.status)
            self.assertTrue(root_run.pause_requested)
            self.assertEqual(root["id"], second_run.source_generation_run_id)
            self.assertTrue(second_run.resume_source_on_completion)

    def test_running_regeneration_replacement_keeps_one_baton_when_root_paused(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            first_job = session.get(Job, first["job_id"])
            root_run.status = "paused"
            first_run.status = "running"
            first_job.status = "running"

        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            second_run = session.get(GenerationRun, second["id"])
            first_job = session.get(Job, first["job_id"])
            second_job = session.get(Job, second["job_id"])
            self.assertEqual(root["id"], first_run.source_generation_run_id)
            self.assertEqual(root["id"], second_run.source_generation_run_id)
            self.assertTrue(root_run.pause_requested)
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertNotIn(
                "auto_resume_source_generation_run_id", first_job.payload_json
            )
            self.assertTrue(second_run.resume_source_on_completion)
            self.assertEqual(
                root["id"],
                second_job.payload_json["auto_resume_source_generation_run_id"],
            )

    def test_replacement_transfers_baton_after_child_terminal_status_commits(self):
        root = self._start()
        first = self._start(operation="regenerate", segment_ids=[self.segment_ids[0]])
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "paused"
            # run_generation commits this status immediately before opening
            # the transaction that consumes the resume baton.
            session.get(GenerationRun, first["id"]).status = "partial"

        second = self._start(operation="regenerate", segment_ids=[self.segment_ids[1]])
        with self.database.session() as session:
            first_run = session.get(GenerationRun, first["id"])
            second_run = session.get(GenerationRun, second["id"])
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertTrue(second_run.resume_source_on_completion)
            self.assertEqual(root["id"], second_run.source_generation_run_id)

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        self.assertIsNone(
            handlers._resume_generation_after_regeneration(first["id"], root["id"])
        )
        self.assertIsNotNone(
            handlers._resume_generation_after_regeneration(second["id"], root["id"])
        )

    def test_default_selection_flattens_legacy_nested_regeneration_chain(self):
        root = self._start()
        first = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[0]],
            generation_run_id=root["id"],
        )
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "failed"
            session.get(Job, root["job_id"]).status = "failed"
            session.get(GenerationRun, first["id"]).status = "failed"
            session.get(Job, first["job_id"]).status = "failed"
        nested = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[1]],
            generation_run_id=first["id"],
        )
        with self.database.session() as session:
            session.get(GenerationRun, nested["id"]).status = "failed"
            session.get(Job, nested["job_id"]).status = "failed"

        flattened = self._start(
            operation="regenerate", segment_ids=[self.segment_ids[0]]
        )
        self.assertEqual(root["id"], flattened["source_generation_run_id"])

    def test_explicit_pause_cancels_stale_terminal_baton(self):
        root = self._start()
        first = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[0]],
            generation_run_id=root["id"],
        )
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            first_run = session.get(GenerationRun, first["id"])
            root_run.status = "paused"
            first_run.status = "completed"

        pause = self.client.post(
            f"/api/v1/generation-runs/{root['id']}/pause", headers=self.headers
        )
        self.assertEqual(202, pause.status_code, pause.get_json())

        replacement = self._start(
            operation="regenerate", segment_ids=[self.segment_ids[0]]
        )
        with self.database.session() as session:
            replacement_run = session.get(GenerationRun, replacement["id"])
            self.assertEqual(root["id"], replacement_run.source_generation_run_id)
            self.assertFalse(replacement_run.resume_source_on_completion)
            first_run = session.get(GenerationRun, first["id"])
            first_job = session.get(Job, first["job_id"])
            self.assertFalse(first_run.resume_source_on_completion)
            self.assertNotIn(
                "auto_resume_source_generation_run_id", first_job.payload_json
            )

        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        self.assertIsNone(
            handlers._resume_generation_after_regeneration(first["id"], root["id"])
        )
        with self.database.session() as session:
            self.assertEqual("paused", session.get(GenerationRun, root["id"]).status)

    def _run_checkpoint_matrix(self, session_id, segment_ids, *, batch_size):
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        root_a = self._start(
            session_id,
            run_override={"tts": {"tts_batch_size": batch_size}},
        )
        calls: list[str] = []
        batch_calls: list[list[str]] = []
        with self._fake_tts(calls, batch_calls, batch_size=batch_size):
            self._run_job(
                handlers,
                {
                    "generation_run_id": root_a["id"],
                    "segment_ids": segment_ids[:2],
                    "operation": "generate",
                },
            )

        with self.database.session() as session:
            session.get(GenerationRun, root_a["id"]).status = "canceled"
            session.get(Job, root_a["job_id"]).status = "canceled"

        root_b = self._start(
            session_id,
            run_override={
                "tts": {
                    "tts_batch_size": batch_size,
                    "model": "root-model",
                    "voice": "root-voice",
                }
            },
        )
        calls.clear()
        batch_calls.clear()
        with self._fake_tts(calls, batch_calls, batch_size=batch_size):
            self._run_job(
                handlers,
                {
                    "generation_run_id": root_b["id"],
                    "segment_ids": [segment_ids[0]],
                    "operation": "generate",
                },
            )

        with self.database.session() as session:
            root_b_run = session.get(GenerationRun, root_b["id"])
            root_b_run.status = "running"
            original_snapshot = deepcopy(root_b_run.settings_snapshot_json)

        child = self._start(
            session_id,
            operation="regenerate",
            segment_ids=[segment_ids[0]],
            generation_run_id=root_b["id"],
            selected_segment_override={
                "tts": {"model": "child-model", "voice": "child-voice"}
            },
        )
        with self.database.session() as session:
            root_b_run = session.get(GenerationRun, root_b["id"])
            child_run = session.get(GenerationRun, child["id"])
            child_job = session.get(Job, child["job_id"])
            old_take = session.scalar(
                select(AudioTake)
                .where(
                    AudioTake.generation_run_id == root_b["id"],
                    AudioTake.generation_segment_id == segment_ids[0],
                )
                .order_by(AudioTake.created_at)
            )
            child_payload = dict(child_job.payload_json)
            self.assertEqual("pausing", root_b_run.status)
            self.assertTrue(child_run.resume_source_on_completion)
            self.assertEqual(root_b["id"], child_run.output_generation_run_id)
            self.assertIsNotNone(old_take)
            root_b_run.status = "paused"

        calls.clear()
        batch_calls.clear()
        with self._fake_tts(calls, batch_calls, batch_size=batch_size):
            child_result = self._run_job(handlers, child_payload)

        with self.database.session() as session:
            root_b_run = session.get(GenerationRun, root_b["id"])
            resume_job = session.get(Job, root_b_run.job_id)
            child_run = session.get(GenerationRun, child["id"])
            self.assertEqual("queued", root_b_run.status)
            self.assertEqual("resume", resume_job.payload_json["operation"])
            self.assertEqual(root_b["id"], child_run.output_generation_run_id)
            self.assertEqual("partial", child_result["status"])
            resume_payload = dict(resume_job.payload_json)

        calls.clear()
        batch_calls.clear()
        with self._fake_tts(calls, batch_calls, batch_size=batch_size):
            resume_result = self._run_job(handlers, resume_payload)

        with self.database.session() as session:
            root_a_run = session.get(GenerationRun, root_a["id"])
            root_b_run = session.get(GenerationRun, root_b["id"])
            child_run = session.get(GenerationRun, child["id"])
            takes = list(
                session.scalars(
                    select(AudioTake)
                    .where(AudioTake.generation_run_id == root_b["id"])
                    .order_by(AudioTake.created_at)
                ).all()
            )
            replacement = next(
                take
                for take in takes
                if take.generation_segment_id == segment_ids[0]
                and take.id != old_take.id
            )
            old_take = session.get(AudioTake, old_take.id)
            replacement_artifact = session.get(Artifact, replacement.artifact_id)
            self.assertEqual("canceled", root_a_run.status)
            self.assertEqual("completed", root_b_run.status)
            self.assertEqual(original_snapshot, root_b_run.settings_snapshot_json)
            self.assertEqual("completed", old_take.status)
            self.assertFalse(old_take.is_active)
            self.assertTrue(replacement.is_active)
            self.assertEqual(old_take.id, replacement.parent_take_id)
            self.assertEqual(root_b["id"], replacement.generation_run_id)
            self.assertEqual(child["id"], replacement_artifact.metadata_json["generation_task_run_id"])
            self.assertEqual(root_b["id"], replacement_artifact.metadata_json["generation_run_id"])
            self.assertEqual(
                "child-model",
                child_run.settings_snapshot_json["selected_segment_override"]["tts"]["model"],
            )
            self.assertEqual(1, resume_result["skipped"])
            self.assertEqual(2, resume_result["generated"])
            self.assertEqual("completed", resume_result["status"])
            self.assertEqual(1.0, self.app.extensions["pandrator"]["generation"]._run_payload(session, root_b_run)["progress"])

        self.assertEqual({"Two", "Three"}, set(calls))
        self.assertEqual(2, len(calls))
        return batch_calls

    def test_grouped_regeneration_checkpoint_uses_output_run_local_takes(self):
        session_id, segment_ids = self._create_session_with_plan("Checkpoint matrix")
        batch_calls = self._run_checkpoint_matrix(
            session_id,
            segment_ids,
            batch_size=1,
        )
        self.assertEqual([], batch_calls)

    def test_resume_checkpoint_filtering_uses_streaming_batch_path(self):
        session_id, segment_ids = self._create_session_with_plan("Batch checkpoint")
        batch_calls = self._run_checkpoint_matrix(
            session_id,
            segment_ids,
            batch_size=2,
        )
        self.assertTrue(batch_calls)
        self.assertEqual(2, len(batch_calls[-1]))
        self.assertNotIn(segment_ids[0], batch_calls[-1])

    def test_stale_or_missing_artifact_takes_do_not_satisfy_resume_checkpoint(self):
        root = self._start(run_override={"tts": {"tts_batch_size": 1}})
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        calls: list[str] = []
        with self._fake_tts(calls, [], batch_size=1):
            self._run_job(
                handlers,
                {
                    "generation_run_id": root["id"],
                    "segment_ids": [self.segment_ids[0]],
                    "operation": "generate",
                },
            )
        with self.database.session() as session:
            take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == root["id"],
                    AudioTake.generation_segment_id == self.segment_ids[0],
                )
            )
            take.status = "stale"
            session.add(
                AudioTake(
                    generation_segment_id=self.segment_ids[1],
                    generation_run_id=root["id"],
                    status="completed",
                    artifact_id=None,
                )
            )
            session.get(GenerationRun, root["id"]).status = "paused"

        calls.clear()
        with self._fake_tts(calls, [], batch_size=1):
            result = self._run_job(
                handlers,
                {
                    "generation_run_id": root["id"],
                    "segment_ids": [],
                    "operation": "resume",
                },
            )
        self.assertEqual("completed", result["status"])
        self.assertEqual(0, result["skipped"])
        self.assertEqual(3, result["generated"])
        self.assertEqual({"One", "Two", "Three"}, set(calls))

    def test_failed_replacement_keeps_previous_active_take_and_wav(self):
        root = self._start(run_override={"tts": {"tts_batch_size": 1}})
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        with self._fake_tts([], [], batch_size=1):
            self._run_job(
                handlers,
                {
                    "generation_run_id": root["id"],
                    "segment_ids": [self.segment_ids[0]],
                    "operation": "generate",
                },
            )
        with self.database.session() as session:
            root_run = session.get(GenerationRun, root["id"])
            root_run.status = "failed"
            session.get(Job, root["job_id"]).status = "failed"
            old_take = session.scalar(
                select(AudioTake).where(
                    AudioTake.generation_run_id == root["id"],
                    AudioTake.generation_segment_id == self.segment_ids[0],
                )
            )
            old_artifact = session.get(Artifact, old_take.artifact_id)
            old_path = self.app.extensions["pandrator"]["artifacts"].paths.managed_path(
                old_artifact.relative_path
            )

        child = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[0]],
            generation_run_id=root["id"],
        )
        with patch.object(
            handlers.tts_providers,
            "synthesize",
            side_effect=RuntimeError("replacement failed"),
        ), self.assertRaises(RuntimeError):
            self._run_job(
                handlers,
                {
                    "generation_run_id": child["id"],
                    "segment_ids": [self.segment_ids[0]],
                    "operation": "regenerate",
                },
            )

        with self.database.session() as session:
            takes = list(
                session.scalars(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == root["id"],
                        AudioTake.generation_segment_id == self.segment_ids[0],
                    )
                ).all()
            )
            self.assertEqual([old_take.id], [take.id for take in takes])
            self.assertTrue(takes[0].is_active)
            self.assertIsNone(takes[0].parent_take_id)
        self.assertTrue(old_path.is_file())

    def test_terminal_parent_filled_by_grouped_regeneration_becomes_completed(self):
        root = self._start(run_override={"tts": {"tts_batch_size": 1}})
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "canceled"
            session.get(Job, root["job_id"]).status = "canceled"
        child = self._start(
            operation="regenerate",
            segment_ids=list(self.segment_ids),
            generation_run_id=root["id"],
        )
        handlers = self.app.extensions["pandrator"]["workflow_handlers"]
        with self._fake_tts([], [], batch_size=1):
            result = self._run_job(
                handlers,
                {
                    "generation_run_id": child["id"],
                    "segment_ids": list(self.segment_ids),
                    "operation": "regenerate",
                },
            )
        self.assertEqual("completed", result["status"])
        latest = self.client.get(
            f"/api/v1/sessions/{self.session_id}/generation-runs/latest"
        ).get_json()["item"]
        self.assertEqual(root["id"], latest["id"])
        self.assertEqual("completed", latest["status"])
        self.assertEqual(1.0, latest["progress"])
        self.assertIsNone(latest["error_message"])

    def test_delete_root_rejects_active_child_then_removes_inactive_group(self):
        root = self._start()
        child = self._start(
            operation="regenerate",
            segment_ids=[self.segment_ids[0]],
            generation_run_id=root["id"],
        )
        other = self._start()
        generation = self.app.extensions["pandrator"]["generation"]
        with self.database.session() as session:
            session.get(GenerationRun, root["id"]).status = "completed"
            session.get(Job, root["job_id"]).status = "succeeded"
            session.get(GenerationRun, child["id"]).status = "queued"
            session.get(GenerationRun, other["id"]).status = "completed"
            session.add(
                AudioTake(
                    generation_segment_id=self.segment_ids[0],
                    generation_run_id=root["id"],
                    status="completed",
                )
            )

        with self.assertRaisesRegex(ValueError, "grouped regeneration"):
            generation.delete_run(root["id"])

        with self.database.session() as session:
            session.get(GenerationRun, child["id"]).status = "completed"
            session.get(Job, child["job_id"]).status = "running"
        with self.assertRaisesRegex(ValueError, "grouped regeneration"):
            generation.delete_run(root["id"])
        with self.database.session() as session:
            session.get(Job, child["job_id"]).status = "succeeded"
        self.assertEqual({"id": root["id"], "status": "deleted"}, generation.delete_run(root["id"]))
        with self.database.session() as session:
            self.assertIsNone(session.get(GenerationRun, root["id"]))
            self.assertIsNone(session.get(GenerationRun, child["id"]))
            self.assertIsNone(session.get(Job, child["job_id"]))
            self.assertIsNone(
                session.scalar(
                    select(AudioTake).where(
                        AudioTake.generation_run_id == root["id"]
                    )
                )
            )
            self.assertIsNotNone(session.get(GenerationRun, other["id"]))


if __name__ == "__main__":
    unittest.main()
