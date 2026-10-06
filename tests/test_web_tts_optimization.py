import json
import tempfile
import threading
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest import mock

from sqlalchemy import inspect, select

from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.web.artifact_selection import selected_artifacts
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import (
    AgentRun,
    AgentStep,
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    Segment,
    SegmentLineage,
    SessionStageSelection,
    TimedWord,
    UsageEvent,
)
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
    def test_current_structured_checkpoint_restores_plan_and_usage_without_model_call(self):
        key = optimization_unit_key([0], stage=0)
        plan = {"prompt_revision": SPEECH_PROMPT_REVISION, "reviewed_reading": "retained"}
        checkpoints = {
            key: {
                "original_indices": [0],
                "items": [{"index": 0, "text": "Doctor Jones"}],
                "plan": plan,
                "cost": 0.02,
                "response_count": 1,
                "usage": {"prompt_tokens": 8},
                "cost_sources": ["fixture"],
            }
        }
        published = []
        with (
            mock.patch("pandrator.web.speech_planning.plan_speech_text") as single_plan,
            mock.patch("pandrator.web.speech_planning.plan_speech_text_batch") as batch_plan,
        ):
            output, usage = optimize_texts(
                ["Dr. Jones"],
                {"speech_optimization_mode": "guarded", "llm_concurrent_calls": 1},
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
                completed_units=checkpoints,
                on_plan_batch=published.extend,
            )
        single_plan.assert_not_called()
        batch_plan.assert_not_called()
        self.assertEqual(["Doctor Jones"], output)
        self.assertEqual((0, "Doctor Jones", plan), published[0])
        self.assertIsNot(plan, published[0][2])
        self.assertEqual(0.02, usage.cost)
        self.assertEqual(1, usage.response_count)
        self.assertEqual({"prompt_tokens": 8}, usage.usage)
        self.assertEqual(["fixture"], usage.cost_sources)

    def test_structured_mode_batches_multiple_units_when_configured(self):
        planned = SimpleNamespace(
            results=[
                SimpleNamespace(
                    text="Doctor Jones",
                    plan={"prompt_revision": SPEECH_PROMPT_REVISION},
                    responses=[],
                ),
                SimpleNamespace(
                    text="Room one oh one",
                    plan={"prompt_revision": SPEECH_PROMPT_REVISION},
                    responses=[],
                ),
            ],
            responses=[
                ChatCompletionResult(
                    content="{}",
                    cost=0.03,
                    usage={"prompt_tokens": 12, "completion_tokens": 5},
                )
            ],
        )
        with (
            mock.patch(
                "pandrator.web.speech_planning.plan_speech_text_batch",
                return_value=planned,
            ) as batch_plan,
            mock.patch("pandrator.web.speech_planning.plan_speech_text") as single_plan,
        ):
            output, usage = optimize_texts(
                ["Dr. Jones", "Room 101"],
                {
                    "speech_optimization_mode": "guarded",
                    "llm_tts_batch_size": 2,
                    "llm_concurrent_calls": 1,
                },
                SimpleNamespace(),
                "provider/model",
                threading.Event(),
                lambda *_args: None,
            )

        self.assertEqual(["Doctor Jones", "Room one oh one"], output)
        batch_plan.assert_called_once()
        single_plan.assert_not_called()
        self.assertEqual(1, usage.response_count)
        self.assertAlmostEqual(0.03, usage.cost)

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

    def _publication_case(self):
        handlers = WorkflowHandlers(self.database, self.paths)
        protected_files = []

        def subtitle(name, text, role, parent=None):
            path = self.session_dir / name
            path.write_text(f"1\n00:00:00,000 --> 00:00:01,000\n{text}\n", encoding="utf-8")
            protected_files.append(path)
            artifact = self.artifacts.register(
                path,
                kind="srt",
                role=role,
                session_id=self.session.id,
                parent_ids=[parent.id] if parent else [],
            )
            handlers._store_srt_document(
                self.session.id,
                artifact,
                "tts_optimization" if parent else "transcription",
                language="en",
                parent_artifact=parent,
            )
            return self.artifacts.resolve(artifact.id)[0]

        source = subtitle("publication-source.srt", "Room 101", "transcription")
        previous = subtitle(
            "publication-previous.srt", "Previous accepted speech", "tts_optimized", source
        )
        derived_path = self.session_dir / "publication-derived.wav"
        derived_path.write_bytes(b"previous derived audio")
        protected_files.append(derived_path)
        descendant = self.artifacts.register(
            derived_path,
            kind="audio",
            role="dubbing_audio",
            session_id=self.session.id,
            parent_ids=[previous.id],
        )
        protected_rows = []
        with self.database.session() as session:
            for model in (
                Artifact,
                ArtifactEdge,
                Document,
                DocumentRevision,
                Segment,
                SegmentLineage,
                TimedWord,
            ):
                protected_rows.extend(
                    (model, inspect(row).identity) for row in session.scalars(select(model))
                )
        case = SimpleNamespace(
            handlers=handlers,
            source=source,
            previous=previous,
            descendant=descendant,
            protected_rows=protected_rows,
            protected_files=protected_files,
        )
        case.before = self._publication_snapshot(case)
        self.assertEqual(case.before["selected"]["optimize_tts"], previous.id)
        self.assertEqual("current", self.artifacts.resolve(descendant.id)[0].state)
        return case

    def _publication_snapshot(self, case):
        def row_value(row):
            return {
                column.key: deepcopy(getattr(row, column.key))
                for column in inspect(row).mapper.column_attrs
            }

        with self.database.session() as session:
            rows = [
                (model.__tablename__, identity, row_value(session.get(model, identity)))
                for model, identity in case.protected_rows
            ]
            selections = [
                row_value(row)
                for row in session.scalars(
                    select(SessionStageSelection)
                    .where(SessionStageSelection.session_id == self.session.id)
                    .order_by(SessionStageSelection.stage_key)
                )
            ]
            selected = {
                stage: artifact.id
                for stage, artifact in selected_artifacts(session, self.session.id).items()
            }
        return {
            "rows": rows,
            "selections": selections,
            "selected": selected,
            "files": {str(path): path.read_bytes() for path in case.protected_files},
        }

    def _run_publication(self, case, *, event=None, progress=None):
        settings = {
            "combined_prompt": "Speak: ",
            "llm_concurrent_calls": 1,
            "llm_provider_configs": [],
            "llm_default_model": "provider/model",
            "tts_optimization_model": "provider/model",
            "request_timeout_seconds": 30,
        }
        with (
            mock.patch.object(case.handlers, "_with_database_llm_settings", return_value=settings),
            mock.patch(
                "pandrator.web.tts_optimization.chat_completion_with_metadata",
                return_value=ChatCompletionResult(
                    content='{"items":[{"index":0,"text":"Room one oh one"}]}',
                    usage={"prompt_tokens": 8, "completion_tokens": 5},
                    cost=0.02,
                ),
            ),
        ):
            return case.handlers.optimize_tts(
                {"session_id": self.session.id, "source_artifact_id": case.source.id},
                progress or (lambda *_args: None),
                event or threading.Event(),
            )

    def _assert_publication_preserved(self, case):
        self.assertEqual(
            self._publication_snapshot(case),
            case.before,
            "Previous optimized output, descendants, native receipt, selection and files changed.",
        )

    def _assert_publication_run(self, status):
        with self.database.session() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.session_id == self.session.id))
            self.assertIsNotNone(run)
            self.assertEqual(status, run.status)

    def _assert_native_candidate(self, case):
        with self.database.session() as session:
            candidates = list(
                session.scalars(select(Artifact).where(Artifact.role == "tts_optimization_candidate"))
            )
            self.assertEqual(1, len(candidates))
            candidate = candidates[0]
            self.assertNotIn(
                candidate.id,
                [artifact.id for artifact in selected_artifacts(session, self.session.id).values()],
            )
            metadata = candidate.metadata_json
            document = session.get(Document, metadata["document_id"])
            revision = session.get(DocumentRevision, metadata["revision_id"])
            self.assertEqual("tts_optimization", document.stage)
            self.assertEqual("en", document.language)
            self.assertEqual(revision.id, document.active_revision_id)
            self.assertEqual(document.id, revision.document_id)
            self.assertIsNotNone(session.get(ArtifactEdge, (case.source.id, candidate.id)))
            self.assertTrue((self.paths.root / candidate.relative_path).is_file())

    def test_srt_native_storage_failure_preserves_previous_publication(self):
        case = self._publication_case()
        failure = RuntimeError("fixture native document failure")
        with (
            mock.patch.object(case.handlers, "_store_srt_document", side_effect=failure),
            self.assertRaises(RuntimeError) as caught,
        ):
            self._run_publication(case)
        self.assertIs(caught.exception, failure)
        self._assert_publication_preserved(case)
        self._assert_publication_run("failed")

    def test_srt_cancellation_at_publication_progress_preserves_previous_output(self):
        case = self._publication_case()
        event = threading.Event()

        def progress(value, _detail=None):
            if value == 0.97:
                event.set()

        with (
            mock.patch.object(case.handlers, "_store_srt_document") as store,
            self.assertRaisesRegex(ProcessCancelled, "Speech optimization was canceled\\."),
        ):
            self._run_publication(case, event=event, progress=progress)
        store.assert_not_called()
        self._assert_publication_preserved(case)
        self._assert_publication_run("interrupted")

    def test_srt_cancellation_after_native_storage_preserves_previous_output(self):
        case = self._publication_case()
        event = threading.Event()
        native = case.handlers._store_srt_document

        def store(*args, **kwargs):
            self.assertEqual("tts_optimization_candidate", args[1].role)
            self.assertEqual("tts_optimization", args[2])
            self.assertEqual("en", kwargs["language"])
            self.assertEqual(case.source.id, kwargs["parent_artifact"].id)
            receipt = native(*args, **kwargs)
            event.set()
            return receipt

        with (
            mock.patch.object(case.handlers, "_store_srt_document", side_effect=store),
            self.assertRaisesRegex(ProcessCancelled, "Speech optimization was canceled\\."),
        ):
            self._run_publication(case, event=event)
        self._assert_publication_preserved(case)
        self._assert_native_candidate(case)
        self._assert_publication_run("interrupted")

    def test_srt_promotion_failure_rolls_back_previous_publication(self):
        case = self._publication_case()
        native = case.handlers.artifacts.register_in_session
        failure = RuntimeError("fixture failure after native promotion")

        def register(*args, **kwargs):
            artifact = native(*args, **kwargs)
            if kwargs.get("role") == "tts_optimized":
                self.assertEqual("stale", args[0].get(Artifact, case.previous.id).state)
                self.assertEqual(
                    artifact.id, selected_artifacts(args[0], self.session.id)["optimize_tts"].id
                )
                raise failure
            return artifact

        with (
            mock.patch.object(case.handlers.artifacts, "register_in_session", side_effect=register),
            self.assertRaises(RuntimeError) as caught,
        ):
            self._run_publication(case)
        self.assertIs(caught.exception, failure)
        self._assert_publication_preserved(case)
        self._assert_native_candidate(case)
        self._assert_publication_run("failed")

    def test_srt_cancellation_during_promotion_rolls_back_previous_publication(self):
        case = self._publication_case()
        event = threading.Event()
        native = case.handlers.artifacts.register_in_session

        def register(*args, **kwargs):
            artifact = native(*args, **kwargs)
            if kwargs.get("role") == "tts_optimized":
                self.assertEqual("stale", args[0].get(Artifact, case.previous.id).state)
                self.assertEqual(
                    artifact.id, selected_artifacts(args[0], self.session.id)["optimize_tts"].id
                )
                event.set()
            return artifact

        with (
            mock.patch.object(case.handlers.artifacts, "register_in_session", side_effect=register),
            self.assertRaisesRegex(ProcessCancelled, "Speech optimization was canceled\\."),
        ):
            self._run_publication(case, event=event)
        self._assert_publication_preserved(case)
        self._assert_native_candidate(case)
        self._assert_publication_run("interrupted")

    def test_guarded_plain_text_publishes_optimized_text_and_speech_plan(self):
        source_path = self.session_dir / "guarded-source.txt"
        source_path.write_text("Dr. Jones", encoding="utf-8")
        source = self.artifacts.register(
            source_path, kind="text", role="source", session_id=self.session.id
        )
        handlers = WorkflowHandlers(self.database, self.paths)
        settings = {
            "speech_optimization_mode": "guarded",
            "speech_plan_save_proposals": False,
            "llm_concurrent_calls": 1,
            "llm_provider_configs": [],
            "llm_default_model": "provider/model",
            "tts_optimization_model": "provider/model",
            "request_timeout_seconds": 30,
            "language": "en",
        }
        planned = SimpleNamespace(
            text="Doctor Jones",
            plan={"prompt_revision": SPEECH_PROMPT_REVISION},
            responses=[],
        )
        with (
            mock.patch.object(
                handlers, "_with_database_llm_settings", return_value=settings
            ),
            mock.patch(
                "pandrator.web.speech_planning.plan_speech_text", return_value=planned
            ),
        ):
            result = handlers.optimize_tts(
                {"session_id": self.session.id, "source_artifact_id": source.id},
                lambda *_args: None,
                threading.Event(),
            )
        output, path = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual("Doctor Jones", path.read_text(encoding="utf-8"))
        plan_id = output.metadata_json["speech_plan_artifact_id"]
        _plan, plan_path = self.artifacts.resolve(plan_id)
        self.assertEqual("Dr. Jones", json.loads(plan_path.read_text())[0]["source_text"])
        with self.database.session() as session:
            run = session.scalar(select(AgentRun).where(AgentRun.session_id == self.session.id))
            self.assertIsNotNone(run)
            self.assertEqual("completed", run.status)

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
        handlers._store_srt_document(self.session.id, source, "transcription", language="en")
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
            mock.patch.object(handlers, "_with_database_llm_settings", return_value=hydrated),
            mock.patch.object(
                handlers, "_store_srt_document", wraps=handlers._store_srt_document
            ) as store_document,
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
        self.assertEqual(["Room one oh one", "Doctor Jones"], [segment.text for segment in segments])
        self.assertEqual(
            [(0, 1000), (1100, 2000)],
            [(segment.start_ms, segment.end_ms) for segment in segments],
        )
        self.assertEqual("tts_optimized", optimized.role)
        self.assertEqual("current", optimized.state)
        self.assertEqual(result["artifact_id"], store_document.call_args.args[1].id)
        self.assertEqual("tts_optimization_candidate", store_document.call_args.args[1].role)
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
            self.assertEqual(document.id, optimized.metadata_json["document_id"])
            self.assertEqual(document.active_revision_id, optimized.metadata_json["revision_id"])
            revision = session.get(DocumentRevision, optimized.metadata_json["revision_id"])
            self.assertEqual(document.id, revision.document_id)
            child_ids = list(
                session.scalars(select(Segment.id).where(Segment.revision_id == revision.id))
            )
            lineage = list(
                session.scalars(
                    select(SegmentLineage).where(SegmentLineage.child_segment_id.in_(child_ids))
                )
            )
            self.assertEqual(2, len(lineage))
            self.assertEqual(
                optimized.id, selected_artifacts(session, self.session.id)["optimize_tts"].id
            )
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

    def test_standalone_guarded_mode_uses_document_request_batch_size(self):
        source_path = self.session_dir / "guarded-source.srt"
        source_path.write_text(
            "1\n00:00:00,000 --> 00:00:01,000\nDr. Jones\n\n"
            "2\n00:00:01,100 --> 00:00:02,000\nRoom 101\n",
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
            "speech_optimization_mode": "guarded",
            "speech_plan_save_proposals": False,
            "llm_tts_batch_size": 1,
            "llm_tts_document_batch_size": 2,
            "llm_concurrent_calls": 1,
            "llm_provider_configs": [],
            "llm_default_model": "provider/model",
            "tts_optimization_model": "provider/model",
            "request_timeout_seconds": 30,
            "language": "en",
        }
        planned = SimpleNamespace(
            results=[
                SimpleNamespace(
                    text="Doctor Jones",
                    plan={"prompt_revision": SPEECH_PROMPT_REVISION},
                    responses=[],
                ),
                SimpleNamespace(
                    text="Room one oh one",
                    plan={"prompt_revision": SPEECH_PROMPT_REVISION},
                    responses=[],
                ),
            ],
            responses=[],
        )

        with (
            mock.patch.object(
                handlers, "_with_database_llm_settings", return_value=hydrated
            ),
            mock.patch(
                "pandrator.web.speech_planning.plan_speech_text_batch",
                return_value=planned,
            ) as batch_plan,
            mock.patch("pandrator.web.speech_planning.plan_speech_text") as single_plan,
        ):
            result = handlers.optimize_tts(
                {
                    "session_id": self.session.id,
                    "source_artifact_id": source.id,
                    "settings": {"llm_tts_document_batch_size": 2},
                },
                lambda *_args: None,
                threading.Event(),
            )

        batch_plan.assert_called_once()
        single_plan.assert_not_called()
        optimized, output_path = self.artifacts.resolve(result["artifact_id"])
        self.assertEqual(2, optimized.metadata_json["batch_size"])
        self.assertEqual(
            ["Doctor Jones", "Room one oh one"],
            [item.text for item in parse_srt(output_path.read_text(encoding="utf-8"))],
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
