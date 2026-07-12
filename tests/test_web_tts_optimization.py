import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import select

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.runtime import DataPaths
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, upgrade_database
from pandrator.web.models import ArtifactEdge, Document, UsageEvent
from pandrator.web.sessions import SessionService
from pandrator.web.tts_optimization import optimize_texts, prompt_sequence
from pandrator.web.workflow_handlers import WorkflowHandlers


class TtsOptimizationUnitTests(unittest.TestCase):
    def test_prompt_sequence_uses_explicit_multi_stage_prompts(self):
        self.assertEqual(
            ["First:", "Second:"],
            prompt_sequence({"llm_multi_stage": True, "first_prompt": "First: ", "second_prompt": "Second: "}),
        )

    def test_optimization_preserves_order_and_aggregates_usage(self):
        calls = []

        def complete(*, messages, **_kwargs):
            source = messages[-1]["content"]
            calls.append(source)
            return ChatCompletionResult(
                content=f'"{source.rsplit(":", 1)[-1].strip()} spoken"',
                usage={"prompt_tokens": 3, "completion_tokens": 2},
                cost=0.01,
                cost_source="provider",
            )

        with mock.patch("pandrator.web.tts_optimization.chat_completion_with_metadata", side_effect=complete):
            output, usage = optimize_texts(
                ["one", "two"],
                {"combined_prompt": "Optimize: ", "llm_concurrent_calls": 2},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
            )
        self.assertEqual(["one spoken", "two spoken"], output)
        self.assertEqual(2, len(calls))
        self.assertEqual(2, usage.response_count)
        self.assertAlmostEqual(0.02, usage.cost)
        self.assertEqual(6, usage.usage["prompt_tokens"])


class TtsOptimizationHandlerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.paths = DataPaths.from_value(self.temporary.name).ensure()
        upgrade_database(self.paths.database)
        self.database = Database(self.paths.database)
        self.session = SessionService(self.database).create("Optimization", workflow_kind="voiceover")
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
        source = self.artifacts.register(source_path, kind="srt", role="transcription", session_id=self.session.id)
        handlers = WorkflowHandlers(self.database, self.paths)
        handlers._store_srt_document(self.session.id, source, "transcription", language="en")
        responses = iter(
            [
                ChatCompletionResult(content="Room one oh one", usage={"prompt_tokens": 4, "completion_tokens": 3}, cost=0.01, cost_source="provider"),
                ChatCompletionResult(content="Doctor Jones", usage={"prompt_tokens": 4, "completion_tokens": 2}, cost=0.01, cost_source="provider"),
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
        with mock.patch.object(handlers, "_with_database_llm_settings", return_value=hydrated), mock.patch(
            "pandrator.web.tts_optimization.chat_completion_with_metadata", side_effect=lambda **_kwargs: next(responses)
        ):
            result = handlers.optimize_tts(
                {"session_id": self.session.id, "source_artifact_id": source.id, "settings": {}},
                lambda *_args: None,
                threading.Event(),
            )

        optimized, output_path = self.artifacts.resolve(result["artifact_id"])
        segments = parse_srt(output_path.read_text(encoding="utf-8"))
        self.assertEqual(["Room one oh one", "Doctor Jones"], [segment.text for segment in segments])
        self.assertEqual([(0, 1000), (1100, 2000)], [(segment.start_ms, segment.end_ms) for segment in segments])
        self.assertEqual("tts_optimized", optimized.role)
        with self.database.session() as session:
            self.assertIsNotNone(session.get(ArtifactEdge, (source.id, optimized.id)))
            document = session.scalar(select(Document).where(Document.session_id == self.session.id, Document.stage == "tts_optimization"))
            self.assertIsNotNone(document)
            usage = session.scalar(select(UsageEvent).where(UsageEvent.session_id == self.session.id, UsageEvent.stage == "tts_optimization"))
            self.assertAlmostEqual(0.02, usage.cost_usd)
            self.assertEqual(8, usage.input_tokens)
            self.assertEqual(5, usage.output_tokens)


if __name__ == "__main__":
    unittest.main()
