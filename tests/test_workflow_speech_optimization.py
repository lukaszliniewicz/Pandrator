"""Regression witnesses for durable speech optimization workflow state."""

import inspect
import json
import tempfile
import threading
import unittest
from unittest import mock

from sqlalchemy import select

from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import GenerationSegment, UsageEvent
from pandrator.web.sessions import SessionService
from pandrator.web.tts_optimization import OptimizationUsage
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class WorkflowSpeechOptimizationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.session = SessionService(self.database).create(
            "Speech optimization regression", workflow_kind="voiceover"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()
        self.artifacts = ArtifactService(self.database, self.paths)
        self.handlers = WorkflowHandlers(self.database, self.paths)
        self.hydration = mock.patch.object(
            self.handlers, "_with_database_llm_settings", side_effect=self._hydrate
        )
        self.hydration.start()
        self.addCleanup(self.hydration.stop)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    @staticmethod
    def _hydrate(settings, _stage):
        return {
            **settings,
            "llm_provider_configs": [],
            "llm_default_model": "fixture/model",
            "tts_optimization_model": "fixture/model",
            "request_timeout_seconds": 30,
            "llm_concurrent_calls": 1,
            "llm_tts_batch_size": 1,
            "apply_reviewed_pronunciations": False,
            "combined_prompt": "Speak: ",
        }

    @staticmethod
    def _settings(**values):
        return {"llm_tts_optimization": True, "apply_reviewed_pronunciations": False, **values}

    @staticmethod
    def _response():
        return ChatCompletionResult(
            content=json.dumps({"items": [{"index": 0, "text": "Doctor Jones"}]}),
            usage={"prompt_tokens": 8, "completion_tokens": 5},
            cost=0.02,
            cost_source="fixture",
        )

    def _segments(self, count=1):
        _revision, ids = self.handlers._store_generation_plan(
            self.session.id,
            [{"text": "Dr. Jones", "language": "en"} for _ in range(count)],
            settings={},
        )
        return ids

    def _state(self, segment_id):
        with self.database.session() as session:
            segment = session.get(GenerationSegment, segment_id)
            return {
                "status": segment.optimization_status,
                "text": segment.optimized_text,
                "source_hash": segment.optimization_source_hash,
                "model": segment.optimization_model,
                "reviewed": segment.optimization_reviewed,
                "plan": dict(segment.speech_plan_json),
            }

    def _usage(self):
        with self.database.session() as session:
            return list(
                session.scalars(
                    select(UsageEvent).where(UsageEvent.session_id == self.session.id)
                ).all()
            )

    def _assert_usage(self, event):
        self.assertEqual(8, event.input_tokens)
        self.assertEqual(5, event.output_tokens)
        self.assertAlmostEqual(0.02, event.cost_usd)
        self.assertEqual(1, event.raw_usage_json["response_count"])

    def _inline(self, ids, settings, event=None):
        return self.handlers._optimize_generation_texts(
            self.session.id,
            ids,
            ["Dr. Jones" for _ in ids],
            settings,
            event if event is not None else threading.Event(),
            lambda *_: None,
        )

    def _source(self, name, content, kind):
        path = self.session_dir / name
        path.write_text(content, encoding="utf-8")
        return self.artifacts.register(
            path, kind=kind, role="source", session_id=self.session.id
        ), path

    def _standalone(self, source, settings):
        return self.handlers.optimize_tts(
            {"session_id": self.session.id, "source_artifact_id": source.id, "settings": settings},
            lambda *_: None,
            threading.Event(),
        )

    def test_pre_cancelled_inline_preserves_pending_segment(self):
        ids = self._segments()
        previous = self._state(ids[0])
        self.assertEqual("not_requested", previous["status"])
        event = threading.Event()
        event.set()
        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata",
            side_effect=AssertionError("Canceled optimization must not call a provider"),
        ) as provider:
            self._inline(ids, self._settings(speech_optimization_mode="legacy"), event)
        provider.assert_not_called()
        self.assertEqual(previous, self._state(ids[0]))
        self.assertEqual([], self._usage())

    def test_speech_facades_recapture_ports_and_forward_identity(self):
        from pandrator.web.workflow_speech_optimization import (
            optimize_generation_texts,
            optimize_tts,
        )

        payload = {"session_id": self.session.id}
        progress = mock.Mock()
        cancel_event = threading.Event()
        contexts = []
        with mock.patch(
            "pandrator.web.workflow_handlers._optimize_tts_impl",
            autospec=True,
            return_value=mock.sentinel.standalone_result,
        ) as owner:
            for marker in (mock.sentinel.first_id, mock.sentinel.second_id):
                with (
                    mock.patch.object(self.handlers, "_record_usage") as record_usage,
                    mock.patch("pandrator.web.workflow_handlers.new_id", return_value=marker),
                ):
                    result = self.handlers.optimize_tts(payload, progress, cancel_event)
                    arguments = (
                        inspect.signature(optimize_tts)
                        .bind(*owner.call_args.args, **owner.call_args.kwargs)
                        .arguments
                    )
                    context = arguments["context"]
                    contexts.append(context)
                    self.assertIs(context.database, self.database)
                    self.assertIs(context.artifacts, self.handlers.artifacts)
                    self.assertIs(context._record_usage, record_usage)
                    self.assertIs(context.new_id(), marker)
                    self.assertIs(arguments["payload"], payload)
                    self.assertIs(arguments["progress"], progress)
                    self.assertIs(arguments["cancel_event"], cancel_event)
                    self.assertIs(result, mock.sentinel.standalone_result)
        self.assertIsNot(contexts[0], contexts[1])

        ids, texts, settings, pronunciation = (
            ["fixture-segment"],
            ["Dr. Jones"],
            {},
            {"language": "pl"},
        )
        options = {
            "job_id": "fixture-job",
            "generation_run_id": "fixture-run",
            "source_artifact_id": "fixture-source",
            "pronunciation_settings": pronunciation,
            "pronunciation_language": "pl",
            "pronunciation_voice_language": "pl",
        }
        with mock.patch(
            "pandrator.web.workflow_handlers._optimize_generation_texts_impl",
            autospec=True,
            return_value=mock.sentinel.inline_result,
        ) as owner:
            result = self.handlers._optimize_generation_texts(
                self.session.id, ids, texts, settings, cancel_event, progress, **options
            )
        arguments = (
            inspect.signature(optimize_generation_texts)
            .bind(*owner.call_args.args, **owner.call_args.kwargs)
            .arguments
        )
        for name, value in {
            "segment_ids": ids,
            "texts": texts,
            "settings": settings,
            "cancel_event": cancel_event,
            "progress": progress,
            "pronunciation_settings": pronunciation,
        }.items():
            self.assertIs(arguments[name], value)
        for name, value in options.items():
            self.assertEqual(arguments[name], value)
        self.assertIs(result, mock.sentinel.inline_result)

    def test_cancelled_inline_batch_preserves_previous_state(self):
        ids = self._segments()
        with self.database.session() as session:
            segment = session.get(GenerationSegment, ids[0])
            segment.optimization_status = "failed"
            segment.optimization_model = "prior/model"
            segment.optimization_reviewed = True
            segment.speech_plan_json = {"prior": "retained"}
        previous = self._state(ids[0])

        def canceled(texts, _settings, _llm, _model, event, _progress, **callbacks):
            event.set()
            if callbacks.get("on_batch"):
                callbacks["on_batch"]([(0, texts[0])])
            if callbacks.get("on_plan_batch"):
                callbacks["on_plan_batch"]([(0, texts[0], {})])
            return list(texts), OptimizationUsage()

        with mock.patch("pandrator.web.tts_optimization.optimize_texts", side_effect=canceled):
            output, _model = self._inline(ids, self._settings(speech_optimization_mode="guarded"))
        self.assertEqual(["Dr. Jones"], output)
        self.assertEqual(previous, self._state(ids[0]))

    def test_missing_mode_uses_legacy_engine_and_reuses_result(self):
        ids = self._segments()
        with (
            mock.patch(
                "pandrator.web.tts_optimization.chat_completion_with_metadata",
                return_value=self._response(),
            ) as provider,
            mock.patch("pandrator.web.speech_planning.plan_speech_text") as single,
            mock.patch("pandrator.web.speech_planning.plan_speech_text_batch") as batch,
        ):
            first, _model = self._inline(ids, self._settings())
            second, _model = self._inline(ids, self._settings())
        self.assertEqual(["Doctor Jones"], first)
        self.assertEqual(["Doctor Jones"], second)
        self.assertEqual(1, provider.call_count)
        single.assert_not_called()
        batch.assert_not_called()
        self.assertEqual({}, self._state(ids[0])["plan"])
        usage = self._usage()
        self.assertEqual(1, len(usage))
        self._assert_usage(usage[0])

    def test_partial_inline_failure_retains_completed_usage(self):
        ids = self._segments(2)

        def fail_second(_texts, *_args, **callbacks):
            if callbacks.get("on_unit_completed"):
                callbacks["on_unit_completed"](
                    "unit0",
                    {
                        "cost": 0.02,
                        "response_count": 1,
                        "usage": {"prompt_tokens": 8, "completion_tokens": 5},
                        "cost_sources": ["fixture"],
                    },
                )
            callbacks["on_batch"]([(0, "Doctor Jones")])
            raise RuntimeError("second batch failed")

        with (
            mock.patch("pandrator.web.tts_optimization.optimize_texts", side_effect=fail_second),
            self.assertRaisesRegex(RuntimeError, "second batch failed"),
        ):
            self._inline(ids, self._settings(speech_optimization_mode="legacy"))
        self.assertEqual("optimized", self._state(ids[0])["status"])
        self.assertEqual("Doctor Jones", self._state(ids[0])["text"])
        self.assertEqual("failed", self._state(ids[1])["status"])
        usage = self._usage()
        self.assertEqual(1, len(usage))
        self._assert_usage(usage[0])

    def test_structured_plan_failure_does_not_publish_plain_result(self):
        ids = self._segments()
        previous = self._state(ids[0])

        def plain_then_plan(texts, *_args, **callbacks):
            callbacks["on_batch"]([(0, "Doctor Jones")])
            callbacks["on_plan_batch"]([(0, "Doctor Jones", {})])
            return list(texts), OptimizationUsage()

        with (
            mock.patch(
                "pandrator.web.tts_optimization.optimize_texts", side_effect=plain_then_plan
            ),
            mock.patch.object(
                self.handlers,
                "_save_speech_plan_proposals",
                side_effect=RuntimeError("plan persistence failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "plan persistence failed"),
        ):
            self._inline(ids, self._settings(speech_optimization_mode="guarded"))
        current = self._state(ids[0])
        self.assertEqual("failed", current["status"])
        self.assertEqual(previous["text"], current["text"])
        self.assertEqual(previous["source_hash"], current["source_hash"])

    def test_annotation_only_records_usage_with_artifact_lineage(self):
        source, _path = self._source("annotation.txt", "Dr. Jones", "text")

        def annotate(_database, _session_id, _texts, *, on_usage, **_kwargs):
            on_usage(self._response())
            return ["<speech>Dr. Jones</speech>"]

        with (
            mock.patch(
                "pandrator.web.speech_structure_analysis.annotate_speech_units",
                side_effect=annotate,
            ),
            mock.patch(
                "pandrator.web.tts_optimization.optimize_texts",
                side_effect=AssertionError("Annotation-only must not rewrite"),
            ) as optimizer,
        ):
            result = self._standalone(
                source,
                self._settings(
                    speech_optimization_mode="legacy",
                    llm_tts_annotation_only=True,
                    llm_tts_annotation_mode="dialogue",
                ),
            )
        optimizer.assert_not_called()
        _artifact, path = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual("Dr. Jones", path.read_text(encoding="utf-8"))
        self.assertAlmostEqual(0.02, result["cost"])
        self.assertEqual(8, result["usage"]["input_tokens"])
        self.assertEqual(5, result["usage"]["output_tokens"])
        usage = self._usage()
        self.assertEqual(1, len(usage))
        self._assert_usage(usage[0])
        self.assertEqual(result["agent_run_id"], usage[0].agent_run_id)
        self.assertEqual(result["artifact_id"], usage[0].artifact_id)

    def test_scalar_json_normalizes_display_and_spoken_fields(self):
        original = json.dumps(["Dr. Jones"])
        source, source_path = self._source("scalar.json", original, "json")
        with mock.patch(
            "pandrator.web.tts_optimization.optimize_texts",
            return_value=(["Doctor Jones"], OptimizationUsage()),
        ):
            result = self._standalone(source, self._settings(speech_optimization_mode="legacy"))
        _artifact, path = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual(
            [
                {
                    "text": "Dr. Jones",
                    "source_text": "Dr. Jones",
                    "tts_optimized_sentence": "Doctor Jones",
                }
            ],
            json.loads(path.read_text(encoding="utf-8")),
        )
        self.assertEqual(original, source_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
