import json
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import AgentRun, AgentStep, ArtifactEdge, Document, UsageEvent
from pandrator.web.sessions import SessionService
from pandrator.web.speech_planning import SPEECH_PROMPT_REVISION
from pandrator.web.tts_optimization import (
    optimization_unit_key,
    optimize_texts,
    prompt_sequence,
)
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root


class TtsOptimizationUnitTests(unittest.TestCase):
    def test_prompt_sequence_uses_explicit_multi_stage_prompts(self):
        self.assertEqual(
            ["First:", "Second:"],
            prompt_sequence(
                {
                    "llm_multi_stage": True,
                    "first_prompt": "First: ",
                    "second_prompt": "Second: ",
                }
            ),
        )

    def test_optimization_preserves_order_and_aggregates_usage(self):
        calls = []

        def complete(*, messages, **_kwargs):
            source = messages[-1]["content"]
            calls.append(source)
            input_payload = json.loads(source.split("Input JSON:\n", 1)[1])
            return ChatCompletionResult(
                content=json.dumps(
                    {
                        "items": [
                            {"index": item["index"], "text": f"{item['text']} spoken"}
                            for item in input_payload["items"]
                        ]
                    }
                ),
                usage={"prompt_tokens": 3, "completion_tokens": 2},
                cost=0.01,
                cost_source="provider",
            )

        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata",
            side_effect=complete,
        ):
            output, usage = optimize_texts(
                ["one", "two"],
                {"combined_prompt": "Optimize: ", "llm_concurrent_calls": 2},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
            )
        self.assertEqual(["one spoken", "two spoken"], output)
        self.assertEqual(1, len(calls))
        self.assertEqual(1, usage.response_count)
        self.assertAlmostEqual(0.01, usage.cost)
        self.assertEqual(3, usage.usage["prompt_tokens"])

    def test_json_batching_preserves_indexes_and_publishes_completed_batches(self):
        calls = []
        published = []

        def complete(*, messages, **_kwargs):
            request_payload = json.loads(
                messages[-1]["content"].split("Input JSON:\n", 1)[1]
            )
            calls.append(request_payload)
            return ChatCompletionResult(
                content=json.dumps(
                    {
                        "items": [
                            {"index": item["index"], "text": item["text"].upper()}
                            for item in request_payload["items"]
                        ]
                    }
                ),
                usage={},
            )

        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata",
            side_effect=complete,
        ):
            output, _usage = optimize_texts(
                ["one", "two", "three", "four", "five"],
                {"llm_tts_batch_size": 2, "llm_concurrent_calls": 2},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
                on_batch=lambda items: published.append(items),
            )
        self.assertEqual(["ONE", "TWO", "THREE", "FOUR", "FIVE"], output)
        self.assertEqual(3, len(calls))
        self.assertEqual(
            {0, 1, 2, 3, 4}, {index for batch in published for index, _text in batch}
        )

    def test_multi_stage_optimization_reports_each_planned_request(self):
        updates = []

        def complete(*, messages, **_kwargs):
            request_payload = json.loads(
                messages[-1]["content"].split("Input JSON:\n", 1)[1]
            )
            return ChatCompletionResult(
                content=json.dumps(
                    {
                        "items": [
                            {"index": item["index"], "text": item["text"].upper()}
                            for item in request_payload["items"]
                        ]
                    }
                ),
                usage={},
            )

        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata",
            side_effect=complete,
        ):
            output, _usage = optimize_texts(
                ["one", "two", "three", "four"],
                {
                    "llm_multi_stage": True,
                    "first_prompt": "First pass",
                    "second_prompt": "Second pass",
                    "llm_tts_batch_size": 2,
                    "llm_concurrent_calls": 2,
                },
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda value, detail=None: updates.append((value, detail)),
            )

        self.assertEqual(["ONE", "TWO", "THREE", "FOUR"], output)
        requests = [
            (value, detail)
            for value, detail in updates
            if str(detail).startswith("Completed speech optimization request")
        ]
        self.assertEqual([0.25, 0.5, 0.75, 1.0], [value for value, _detail in requests])
        self.assertEqual(
            "Completed speech optimization request 4 of 4 for 2 text units",
            requests[-1][1],
        )

    def test_structured_response_is_retried_without_losing_rejected_usage(self):
        calls = []

        def complete(*, messages, **_kwargs):
            calls.append(messages)
            payload = json.loads(messages[-1]["content"].split("Input JSON:\n", 1)[1])
            if len(calls) == 1:
                return ChatCompletionResult(
                    content=json.dumps({"items": [payload["items"][0]]}),
                    cost=0.01,
                )
            return ChatCompletionResult(
                content=json.dumps(
                    {
                        "items": [
                            {
                                "index": item["index"],
                                "text": item["text"].upper(),
                            }
                            for item in payload["items"]
                        ]
                    }
                ),
                cost=0.02,
            )

        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata",
            side_effect=complete,
        ):
            output, usage = optimize_texts(
                ["one", "two"],
                {"llm_tts_batch_size": 2, "llm_concurrent_calls": 1},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
            )

        self.assertEqual(["ONE", "TWO"], output)
        self.assertEqual(2, usage.response_count)
        self.assertAlmostEqual(0.03, usage.cost)
        self.assertIn("previous response was rejected", calls[1][0]["content"])

    def test_completed_optimization_batch_is_restored_without_model_call(self):
        checkpoints = {}
        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata",
            return_value=ChatCompletionResult(
                content='{"items":[{"index":0,"text":"Doctor Jones"}]}',
                cost=0.02,
                usage={"prompt_tokens": 8, "completion_tokens": 3},
            ),
        ) as first_completion:
            first_output, first_usage = optimize_texts(
                ["Dr. Jones"],
                {"llm_tts_batch_size": 1, "llm_concurrent_calls": 1},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
                on_unit_completed=lambda key, payload: checkpoints.__setitem__(
                    key, payload
                ),
            )

        with mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata"
        ) as resumed_completion:
            resumed_output, resumed_usage = optimize_texts(
                ["Dr. Jones"],
                {"llm_tts_batch_size": 1, "llm_concurrent_calls": 1},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
                completed_units=checkpoints,
            )

        self.assertEqual(["Doctor Jones"], first_output)
        self.assertEqual(first_output, resumed_output)
        self.assertEqual(first_usage.response_count, resumed_usage.response_count)
        first_completion.assert_called_once()
        resumed_completion.assert_not_called()

    def test_structured_checkpoint_with_old_prompt_revision_is_recomputed(self):
        checkpoints = {
            optimization_unit_key([0], stage=0): {
                "version": 1,
                "kind": "tts_optimization_plan",
                "stage": 0,
                "original_indices": [0],
                "items": [{"index": 0, "text": "Old pronunciation"}],
                "plan": {"prompt_revision": SPEECH_PROMPT_REVISION - 1},
                "cost": 0.01,
                "response_count": 1,
                "usage": {},
                "cost_sources": [],
            }
        }
        updated_checkpoints = {}
        planned = SimpleNamespace(
            text="New pronunciation",
            plan={"prompt_revision": SPEECH_PROMPT_REVISION},
            responses=[],
        )

        with mock.patch(
            "pandrator.web.speech_planning.plan_speech_text",
            return_value=planned,
        ) as plan:
            output, _usage = optimize_texts(
                ["Wisconsin"],
                {
                    "speech_optimization_mode": "guarded",
                    "llm_concurrent_calls": 1,
                },
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
                completed_units=checkpoints,
                on_unit_completed=lambda key, payload: updated_checkpoints.__setitem__(
                    key, payload
                ),
            )

        self.assertEqual(["New pronunciation"], output)
        plan.assert_called_once()
        self.assertEqual(
            SPEECH_PROMPT_REVISION,
            next(iter(updated_checkpoints.values()))["plan"]["prompt_revision"],
        )


class TtsOptimizationHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = prepare_web_test_data_root(self.temporary.name)
        self.database = Database(self.paths.database)
        self.session = SessionService(self.database).create(
            "Optimization", workflow_kind="voiceover"
        )
        self.session_dir = self.paths.sessions / self.session.storage_key
        self.session_dir.mkdir()
        self.artifacts = ArtifactService(self.database, self.paths)

    def tearDown(self):
        self.database.dispose()
        self.temporary.cleanup()

    def test_srt_optimization_creates_previewable_revision_with_lineage_and_cost(self):
        source_path = self.session_dir / "source.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nRoom 101\n\n2\n00:00:01,100 --> 00:00:02,000\nDr. Jones\n",
            encoding="utf-8",
        )
        source = self.artifacts.register(
            source_path, kind="srt", role="transcription", session_id=self.session.id
        )
        handlers = WorkflowHandlers(self.database, self.paths)
        handlers._store_srt_document(
            self.session.id, source, "transcription", language="en"
        )
        responses = iter(
            [
                ChatCompletionResult(
                    content=json.dumps(
                        {
                            "items": [
                                {"index": 0, "text": "Room one oh one"},
                                {"index": 1, "text": "Doctor Jones"},
                            ]
                        }
                    ),
                    usage={"prompt_tokens": 8, "completion_tokens": 5},
                    cost=0.02,
                    cost_source="provider",
                )
            ]
        )
        hydrated = {
            "combined_prompt": "Speak: ",
            "llm_concurrent_calls": 1,
            "llm_provider_configs": [],
            "llm_default_model": "provider/model",
            "tts_optimization_model": "provider/model",
            "request_timeout_seconds": 30,
        }
        progress_updates = []
        with (
            mock.patch.object(
                handlers, "_with_database_llm_settings", return_value=hydrated
            ),
            mock.patch(
                "pandrator.web.tts_optimization.chat_completion_with_metadata",
                side_effect=lambda **_kwargs: next(responses),
            ),
        ):
            result = handlers.optimize_tts(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {},
                },
                lambda value, detail=None: progress_updates.append((value, detail)),
                threading.Event(),
            )

        optimized, output_path = self.artifacts.resolve(result["artifact_id"])
        segments = parse_srt(output_path.read_text(encoding="utf-8"))
        self.assertEqual(
            ["Room one oh one", "Doctor Jones"], [segment.text for segment in segments]
        )
        self.assertEqual(
            [(0, 1000), (1100, 2000)],
            [(segment.start_ms, segment.end_ms) for segment in segments],
        )
        self.assertEqual("tts_optimized", optimized.role)
        self.assertAlmostEqual(0.02, result["cost"])
        self.assertEqual(13, result["usage"]["total_tokens"])
        self.assertEqual(8, result["usage"]["input_tokens"])
        self.assertEqual(5, result["usage"]["output_tokens"])
        with self.database.session() as session:
            self.assertIsNotNone(session.get(ArtifactEdge, (source.id, optimized.id)))
            document = session.scalar(
                select(Document).where(
                    Document.session_id == self.session.id,
                    Document.stage == "tts_optimization",
                )
            )
            self.assertIsNotNone(document)
            usage = session.scalar(
                select(UsageEvent).where(
                    UsageEvent.session_id == self.session.id,
                    UsageEvent.stage == "tts_optimization",
                )
            )
            self.assertAlmostEqual(0.02, usage.cost_usd)
            self.assertEqual(8, usage.input_tokens)
            self.assertEqual(5, usage.output_tokens)
        request_update = next(
            value
            for value, detail in progress_updates
            if str(detail).startswith("Completed speech optimization request")
        )
        self.assertEqual(0.9, request_update)
        self.assertEqual(
            1.0,
            next(
                value
                for value, detail in progress_updates
                if detail == "Speech optimization preview ready"
            ),
        )

    def test_failed_optimization_resumes_from_persisted_batch(self):
        source_path = self.session_dir / "resume-source.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nOne\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\nTwo\n",
            encoding="utf-8",
        )
        source = self.artifacts.register(
            source_path,
            kind="srt",
            role="transcription",
            session_id=self.session.id,
        )
        handlers = WorkflowHandlers(self.database, self.paths)
        hydrated = {
            "combined_prompt": "Speak: ",
            "llm_concurrent_calls": 1,
            "llm_tts_document_batch_size": 1,
            "llm_provider_configs": [],
            "llm_default_model": "provider/model",
            "tts_optimization_model": "provider/model",
            "request_timeout_seconds": 30,
        }
        first_calls = 0

        def fail_second(*, messages, **_kwargs):
            nonlocal first_calls
            first_calls += 1
            if first_calls == 2:
                raise RuntimeError("provider unavailable")
            item = json.loads(messages[-1]["content"].split("Input JSON:\n", 1)[1])[
                "items"
            ][0]
            return ChatCompletionResult(
                content=json.dumps(
                    {"items": [{"index": item["index"], "text": "One spoken"}]}
                ),
                usage={"prompt_tokens": 3, "completion_tokens": 2},
                cost=0.01,
                cost_source="provider",
            )

        payload = {
            "session_id": self.session.id,
            "source_artifact_id": source.id,
            "settings": {"llm_tts_document_batch_size": 1},
        }
        with (
            mock.patch.object(
                handlers, "_with_database_llm_settings", return_value=hydrated
            ),
            mock.patch(
                "pandrator.web.tts_optimization.chat_completion_with_metadata",
                side_effect=fail_second,
            ),
            self.assertRaisesRegex(RuntimeError, "provider unavailable"),
        ):
            handlers.optimize_tts(
                payload,
                lambda *_args: None,
                threading.Event(),
            )

        with self.database.session() as session:
            run = session.scalar(
                select(AgentRun).where(AgentRun.session_id == self.session.id)
            )
            self.assertEqual("failed", run.status)
            self.assertEqual(
                1,
                len(
                    list(
                        session.scalars(
                            select(AgentStep).where(AgentStep.agent_run_id == run.id)
                        ).all()
                    )
                ),
            )
            run_id = run.id

        resume_calls = 0

        def finish_second(*, messages, **_kwargs):
            nonlocal resume_calls
            resume_calls += 1
            item = json.loads(messages[-1]["content"].split("Input JSON:\n", 1)[1])[
                "items"
            ][0]
            return ChatCompletionResult(
                content=json.dumps(
                    {"items": [{"index": item["index"], "text": "Two spoken"}]}
                ),
                usage={"prompt_tokens": 4, "completion_tokens": 2},
                cost=0.02,
                cost_source="provider",
            )

        with (
            mock.patch.object(
                handlers, "_with_database_llm_settings", return_value=hydrated
            ),
            mock.patch(
                "pandrator.web.tts_optimization.chat_completion_with_metadata",
                side_effect=finish_second,
            ),
        ):
            result = handlers.optimize_tts(
                {**payload, "_agent_run_id": run_id},
                lambda *_args: None,
                threading.Event(),
            )

        self.assertTrue(result["resumed"])
        self.assertEqual(1, resume_calls)
        _artifact, destination = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual(
            ["One spoken", "Two spoken"],
            [segment.text for segment in parse_srt(destination.read_text())],
        )
        with self.database.session() as session:
            run = session.get(AgentRun, run_id)
            self.assertEqual("completed", run.status)
            self.assertEqual(result["artifact_id"], run.result_artifact_id)
            self.assertEqual(
                2,
                len(
                    list(
                        session.scalars(
                            select(AgentStep).where(AgentStep.agent_run_id == run_id)
                        ).all()
                    )
                ),
            )


if __name__ == "__main__":
    unittest.main()
