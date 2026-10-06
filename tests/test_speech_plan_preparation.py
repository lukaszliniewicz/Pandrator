"""Final-unit preparation must leave the selected plan intact until commit."""

import io
import tempfile
import unittest
import uuid
from threading import Event
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_plan_preparation import run_speech_preparation
from pandrator.web.tts_optimization import OptimizationUsage

SRT = (
    "1\n00:00:01,000 --> 00:00:03,000\nHello, world.\n\n"
    "2\n00:00:04,000 --> 00:00:06,000\nA second complete thought.\n"
)


class SpeechPlanPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        bootstrap = BootstrapTokenStore()
        self.app = create_app(data_root=self.temp.name, testing=True, bootstrap_tokens=bootstrap)
        self.services = self.app.extensions["pandrator"]["services"]
        self.addCleanup(self.services.database.dispose)
        self.client = self.app.test_client()
        token = bootstrap.issue()
        csrf = self.client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
        self.headers = {"X-CSRF-Token": csrf}
        self.sid = self.client.post(
            "/api/v1/sessions", json={"name": "Final speech preparation", "workflow_kind": "voiceover"},
            headers=self.headers,
        ).get_json()["id"]
        upload = self.client.post(
            "/api/v1/uploads",
            data={"file": (io.BytesIO(SRT.encode()), "original.srt"), "session_id": self.sid},
            headers=self.headers,
        )
        self.assertEqual(upload.status_code, 201, upload.get_json())
        with self.services.database.session() as session:
            outcome = session.get(m.OutcomePlan, self.sid)
            if outcome is None:
                outcome = m.OutcomePlan(session_id=self.sid, value_json={})
                session.add(outcome)
            outcome.value_json = {**dict(outcome.value_json or {}), "inputs": {"generation": "source"}}

    def _set_optimization(self, enabled):
        with self.services.database.session() as session:
            row = session.get(m.SessionSetting, (self.sid, "text"))
            if row is None:
                row = m.SessionSetting(session_id=self.sid, section="text", value_json={})
                session.add(row)
            row.value_json = {**dict(row.value_json or {}), "llm_tts_optimization": enabled}
            row.revision = int(row.revision or 0) + 1
            outcome = session.get(m.OutcomePlan, self.sid)
            value = dict(outcome.value_json or {})
            value["transformations"] = {
                **dict(value.get("transformations") or {}), "llm_tts_optimization": enabled,
            }
            outcome.value_json = value

    def _prepare(self, source_artifact_id=None):
        state = self.client.get(f"/api/v1/sessions/{self.sid}/generation-plan/status").get_json()
        return self.client.post(
            f"/api/v1/sessions/{self.sid}/generation-plan/prepare",
            json={
                "expected_revision": state["session_revision"],
                "expected_plan_revision_id": state["selected_revision_id"],
                "source_artifact_id": source_artifact_id or state["current_input"]["artifact_id"],
            },
            headers={**self.headers, "Idempotency-Key": uuid.uuid4().hex},
        )

    @staticmethod
    def _model(*args, **kwargs):
        return SimpleNamespace(provider_configs=[], default_model="fake/model", request_timeout_seconds=1), "fake/model"

    def _queue(self):
        self._set_optimization(True)
        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=self._model):
            response = self._prepare()
        self.assertEqual(response.status_code, 200, response.get_json())
        result = response.get_json()
        self.assertEqual(result["status"], "queued")
        self.assertFalse(result["synthesis_started"])
        with self.services.database.session() as session:
            job = session.get(m.Job, result["job_id"])
            payload = dict(job.payload_json)
            job.status = "running"
        return payload

    def test_optimizes_before_selecting_new_plan_and_keeps_source_fields(self):
        initial = self._prepare().get_json()
        self.assertIn("selected_revision_id", initial)
        payload = self._queue()
        seen = []

        def optimize(texts, settings, llm_settings, model, cancel, progress, **kwargs):
            seen.extend(texts)
            self.assertEqual(model, "fake/model")
            self.assertEqual(len(kwargs["languages"]), len(texts))
            self.assertEqual(len(kwargs["voice_languages"]), len(texts))
            self.assertIsNotNone(kwargs["known_pronunciation_resolver"])
            with self.services.database.session() as session:
                plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == self.sid))
                self.assertEqual(plan.active_revision_id, initial["selected_revision_id"])
            return [f"Spoken: {text}" for text in texts], OptimizationUsage(
                cost=0.01, response_count=1, usage={"prompt_tokens": 10}, cost_sources=["test"]
            )

        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=self._model), patch(
            "pandrator.web.tts_optimization.optimize_texts", side_effect=optimize
        ):
            result = self.services.workflow_handlers.handler_registry["speech.prepare"](
                payload, lambda *_: None, Event()
            )
        self.assertTrue(seen)
        with self.services.database.session() as session:
            plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == self.sid))
            self.assertEqual(plan.active_revision_id, result["selected_revision_id"])
            original = list(session.scalars(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == initial["selected_revision_id"]).order_by(m.GenerationSegment.ordinal)))
            revised = list(session.scalars(select(m.GenerationSegment).where(m.GenerationSegment.plan_revision_id == result["selected_revision_id"]).order_by(m.GenerationSegment.ordinal)))
            self.assertEqual(len(original), len(revised))
            for old, new in zip(original, revised, strict=True):
                self.assertEqual(new.text, old.text)
                self.assertEqual(new.source_segment_ids_json, old.source_segment_ids_json)
                self.assertEqual(new.speech_block_provenance_json, old.speech_block_provenance_json)
                self.assertEqual(new.speaker, old.speaker)
                self.assertEqual(new.optimized_text, f"Spoken: {old.text}")
            revision = session.get(m.GenerationPlanRevision, result["selected_revision_id"])
            self.assertFalse(revision.settings_json["llm_tts_optimization"])
            self.assertTrue(revision.settings_json["_prepared_for_review"])
            self.assertEqual(revision.settings_json["_final_unit_optimization_model"], "fake/model")
            usage = session.scalar(select(m.UsageEvent).where(m.UsageEvent.job_id == payload["job_id"]))
            self.assertIsNotNone(usage)
            self.assertEqual(usage.input_tokens, 10)
            session.get(m.Job, payload["job_id"]).status = "completed"
        generation = self.services.generation.start(self.sid, speech_plan_revision_id=result["selected_revision_id"])
        with self.services.database.session() as session:
            snapshot = session.get(m.GenerationRun, generation["id"]).settings_snapshot_json
            self.assertFalse(snapshot["text"]["llm_tts_optimization"])

    def test_failed_or_cancelled_optimizer_never_selects_partial_plan(self):
        initial = self._prepare().get_json()
        payload = self._queue()

        def fails_after_one_unit(_texts, *_args, **kwargs):
            kwargs["on_unit_completed"](
                "unit-1",
                {"cost": 0.02, "response_count": 1, "usage": {"prompt_tokens": 7}, "cost_sources": ["test"]},
            )
            raise RuntimeError("LLM failed")

        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=self._model), patch(
            "pandrator.web.tts_optimization.optimize_texts", side_effect=fails_after_one_unit
        ):
            with self.assertRaisesRegex(RuntimeError, "LLM failed"):
                run_speech_preparation(self.services.workflow_handlers, payload, lambda *_: None, Event())
        with self.services.database.session() as session:
            plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == self.sid))
            self.assertEqual(plan.active_revision_id, initial["selected_revision_id"])
            usage = session.scalar(select(m.UsageEvent).where(m.UsageEvent.job_id == payload["job_id"]))
            self.assertEqual(usage.input_tokens, 7)
        cancelled = Event()
        cancelled.set()
        self.assertEqual(run_speech_preparation(self.services.workflow_handlers, payload, lambda *_: None, cancelled), {})

    def test_changed_settings_rejected_before_model_call(self):
        payload = self._queue()
        self._set_optimization(False)
        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=AssertionError("model selected")):
            with self.assertRaises(RevisionConflict):
                run_speech_preparation(self.services.workflow_handlers, payload, lambda *_: None, Event())

    def test_settings_changed_during_optimizer_never_selects_result(self):
        initial = self._prepare().get_json()
        payload = self._queue()

        def optimize(texts, *_args, **_kwargs):
            self._set_optimization(False)
            return [f"Spoken: {text}" for text in texts], SimpleNamespace()

        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=self._model), patch(
            "pandrator.web.tts_optimization.optimize_texts", side_effect=optimize
        ):
            with self.assertRaises(RevisionConflict):
                run_speech_preparation(self.services.workflow_handlers, payload, lambda *_: None, Event())
        with self.services.database.session() as session:
            plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == self.sid))
            self.assertEqual(plan.active_revision_id, initial["selected_revision_id"])

    def test_document_and_final_optimization_cannot_queue_together(self):
        source_artifact_id = self.client.get(
            f"/api/v1/sessions/{self.sid}/generation-plan/status"
        ).get_json()["current_input"]["artifact_id"]
        self._set_optimization(True)
        with self.services.database.session() as session:
            row = session.get(m.SessionSetting, (self.sid, "text"))
            row.value_json = {**row.value_json, "llm_tts_document_optimization": True}
            row.revision += 1
            outcome = session.get(m.OutcomePlan, self.sid)
            outcome.value_json = {
                **outcome.value_json,
                "transformations": {**outcome.value_json["transformations"], "llm_tts_document_optimization": True},
            }
        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=AssertionError("model selected")):
            response = self._prepare(source_artifact_id)
        self.assertEqual(response.status_code, 422, response.get_json())
        self.assertIn("Document and final-unit", str(response.get_json()))

    def test_saved_false_overrides_enabled_text_before_preparation(self):
        self._set_optimization(True)
        with self.services.database.session() as session:
            outcome = session.get(m.OutcomePlan, self.sid)
            outcome.value_json = {
                **outcome.value_json,
                "transformations": {"llm_tts_optimization": False, "llm_tts_document_optimization": False},
            }
        with patch("pandrator.web.speech_plan_preparation.build_llm_settings", side_effect=AssertionError("No LLM should be selected")):
            response = self._prepare()
        self.assertEqual(200, response.status_code, response.get_json())
        self.assertIn("selected_revision_id", response.get_json())
        self.assertNotIn("job_id", response.get_json())

    def test_legacy_inline_rejects_xml_before_provider_and_preserves_it(self):
        revision_id = self._prepare().get_json()["selected_revision_id"]
        with self.services.database.session() as session:
            segments = list(session.scalars(select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id == revision_id
            ).order_by(m.GenerationSegment.ordinal)))
            original = dict(segments[0].speech_plan_json or {})
            xml = '<segment id="1"><span>Hello, world.</span></segment>'
            segments[0].speech_plan_json = {**original, "speech_xml": xml}
            ids = [segment.id for segment in segments]
            texts = [segment.text for segment in segments]
        with patch.object(
            self.services.workflow_handlers, "_with_database_llm_settings",
            side_effect=AssertionError("provider must not be resolved"),
        ):
            with self.assertRaisesRegex(ValueError, "Speech XML"):
                self.services.workflow_handlers._optimize_generation_texts(
                    self.sid, ids, texts,
                    {"llm_tts_optimization": True, "service": "XTTS"},
                    Event(), lambda *_: None,
                )
        with self.services.database.session() as session:
            segment = session.get(m.GenerationSegment, ids[0])
            self.assertEqual(segment.speech_plan_json["speech_xml"], xml)
            self.assertIsNone(segment.optimized_text)
            self.assertEqual(segment.optimization_status, "not_requested")

    def test_unannotated_legacy_inline_still_optimizes(self):
        revision_id = self._prepare().get_json()["selected_revision_id"]
        with self.services.database.session() as session:
            segment = session.scalar(select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id == revision_id
            ).order_by(m.GenerationSegment.ordinal))
            segment_id, source_text = segment.id, segment.text
        settings = {
            "llm_tts_optimization": True,
            "speech_optimization_mode": "legacy",
            "tts_optimization_model": "fake/model",
            "llm_default_model": "fake/model",
            "llm_provider_configs": [],
            "request_timeout_seconds": 1,
            "service": "XTTS",
            "apply_reviewed_pronunciations": False,
        }
        with patch.object(
            self.services.workflow_handlers, "_with_database_llm_settings", return_value=settings
        ), patch("pandrator.web.tts_optimization.optimize_texts", return_value=(
            [f"Spoken: {source_text}"], OptimizationUsage()
        )):
            revised, model = self.services.workflow_handlers._optimize_generation_texts(
                self.sid, [segment_id], [source_text], settings, Event(), lambda *_: None,
            )
        self.assertEqual(revised, [f"Spoken: {source_text}"])
        self.assertEqual(model, "fake/model")

    def test_legacy_inline_rejects_document_optimized_source_and_setting(self):
        revision_id = self._prepare().get_json()["selected_revision_id"]
        with self.services.database.session() as session:
            segment = session.scalar(select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id == revision_id
            ))
            revision = session.get(m.GenerationPlanRevision, revision_id)
            source_id = revision.settings_json["_source_artifact_id"]
            segment_id, source_text = segment.id, segment.text
            session.get(m.Artifact, source_id).role = "tts_optimized"
        with patch.object(
            self.services.workflow_handlers, "_with_database_llm_settings",
            side_effect=AssertionError("provider must not be resolved"),
        ):
            with self.assertRaisesRegex(ValueError, "document-level optimization"):
                self.services.workflow_handlers._optimize_generation_texts(
                    self.sid, [segment_id], [source_text],
                    {"llm_tts_optimization": True}, Event(), lambda *_: None,
                    source_artifact_id=source_id,
                )
            with self.assertRaisesRegex(ValueError, "Document-level speech optimization"):
                self.services.workflow_handlers._optimize_generation_texts(
                    self.sid, [segment_id], [source_text],
                    {"llm_tts_optimization": True, "llm_tts_document_optimization": True},
                    Event(), lambda *_: None,
                )


if __name__ == "__main__":
    unittest.main()
