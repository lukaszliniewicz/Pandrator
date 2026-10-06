"""Disposable native witnesses for subtitle publication and research cancellation."""

import hashlib
import json
import threading
from contextlib import ExitStack
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from sqlalchemy import inspect, select

from pandrator.logic.dubbing.llm_correction import CorrectionResult
from pandrator.logic.dubbing.llm_translation import TranslationResult
from pandrator.web.agentic_runs import AgenticRunStore
from pandrator.web.artifact_selection import selected_artifacts
from pandrator.web.artifacts import ArtifactService
from pandrator.web.context_budget import ContextBudget
from pandrator.web.database import Database
from pandrator.web.knowledge import KnowledgeLedgerStore
from pandrator.web.models import (
    AgentRun,
    AgentStep,
    Artifact,
    ArtifactEdge,
    Document,
    DocumentRevision,
    KnowledgeLedger,
    Segment,
    SessionStageSelection,
    TimedWord,
    UsageEvent,
)
from pandrator.web.sessions import SessionService
from pandrator.web.web_research import WebResearchResult
from pandrator.web.workflow_handlers import WorkflowHandlers
from tests.web_test_support import prepare_web_test_data_root

MODES = ["correction", "llm-translation", "deepl-translation"]
LLM_MODES = MODES[:2]


def _row(row):
    return {
        column.key: deepcopy(getattr(row, column.key))
        for column in inspect(row).mapper.column_attrs
    }


def _snapshot(case):
    with case.database.session() as session:
        rows = [
            (model.__tablename__, identity, _row(session.get(model, identity)))
            for model, identity in case.protected_rows
        ]
        selections = [
            _row(row)
            for row in session.scalars(
                select(SessionStageSelection)
                .where(SessionStageSelection.session_id == case.session.id)
                .order_by(SessionStageSelection.stage_key)
            )
        ]
        selected = {
            key: artifact.id
            for key, artifact in selected_artifacts(session, case.session.id).items()
        }
    return {
        "rows": rows,
        "selections": selections,
        "selected": selected,
        "files": {str(path): path.read_bytes() for path in case.protected_files},
    }


def _native_subtitle(case, name, text, role, *, parent=None):
    path = case.paths.root / name
    path.write_text(f"1\n00:00:01,100 --> 00:00:01,900\n{text}\n", encoding="utf-8")
    artifact = case.artifacts.register(
        path,
        kind="srt",
        role=role,
        session_id=case.session.id,
        parent_ids=[parent.id] if parent else [],
    )
    _document, revision = case.handlers._store_srt_document(
        case.session.id,
        artifact,
        role,
        language=case.language if role != "transcription" else "en",
        parent_artifact=parent,
        speaker_overrides={1: "Alice"},
    )
    words_path = path.with_suffix(".words.json")
    words_path.write_text(
        json.dumps(
            {
                "schema": "pandrator.transcript.v1",
                "segments": [
                    {
                        "text": text,
                        "start_ms": 1100,
                        "end_ms": 1900,
                        "speaker": "Alice",
                        "words": [{"text": text, "start_ms": 1100, "end_ms": 1900}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    case.handlers._store_timed_words(revision, words_path)
    case.protected_files.extend([path, words_path])
    return case.artifacts.resolve(artifact.id)[0], path


@pytest.fixture
def case(tmp_path, request):
    mode = request.param
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    value = SimpleNamespace(
        paths=paths,
        database=database,
        mode=mode,
        stage="correction" if mode == "correction" else "translation",
        stage_key="correct" if mode == "correction" else "translate",
        language="en" if mode == "correction" else "de",
        session=SessionService(database).create("Subtitle publication witness"),
        artifacts=ArtifactService(database, paths),
        handlers=WorkflowHandlers(database, paths),
        protected_files=[],
        expected_units={},
    )
    try:
        value.source, value.source_path = _native_subtitle(
            value, "source.srt", "Hello world.", "transcription"
        )
        value.previous, _ = _native_subtitle(
            value, "previous.srt", "Previous review.", value.stage, parent=value.source
        )
        child_role = "translation" if mode == "correction" else "tts_optimized"
        value.child, _ = _native_subtitle(
            value, "dependent.srt", "Dependent reviewed output.", child_role, parent=value.previous
        )
        value.settings = {
            "original_language": "en",
            "target_language": "de",
            "translation_backend": "deepl" if mode == "deepl-translation" else "llm",
            "correction_style": "publishable",
            "glossary_enabled": True,
        }
        effective, revision = value.handlers._resolve_run_passage_settings(
            value.session.id, value.settings, database=database
        )
        prime = paths.root / "primed-passages"
        prime.mkdir()
        value.handlers._prepare_passage_input(
            value.source,
            value.source_path,
            prime,
            source_passage_settings=effective,
            source_passage_settings_revision=revision,
        )
        value.handlers._passage_display_settings(value.session.id)
        value.source, value.source_path = value.artifacts.resolve(value.source.id)
        value.knowledge = KnowledgeLedgerStore(database)
        value.knowledge_languages = {
            "source_language": "en",
            "target_language": "" if mode == "correction" else "de",
        }
        value.knowledge.merge_research(
            value.session.id,
            **value.knowledge_languages,
            evidence=[
                {"recommendation": "Prior knowledge", "source_url": "https://example.invalid/prior"}
            ],
            summary="Previously accepted summary",
        )
        value.knowledge.merge_glossary(
            value.session.id,
            **value.knowledge_languages,
            entries=[{"source": "Locked", "target": "Manual seed"}],
            origin="manual",
            locked=True,
        )
        value.protected_rows = []
        with database.session() as session:
            for model in (
                Artifact,
                Document,
                DocumentRevision,
                Segment,
                TimedWord,
                ArtifactEdge,
                KnowledgeLedger,
            ):
                value.protected_rows.extend(
                    (model, inspect(row).identity) for row in session.scalars(select(model))
                )
        value.before = _snapshot(value)
        value.operation_dirs = list(paths.sessions.glob("*/stage-runs/*"))
        assert value.before["selected"][value.stage_key] == value.previous.id
        yield value
    finally:
        database.dispose()


def _unit(case, path):
    return {
        "kind": case.stage,
        "stage": case.stage,
        "source_hash": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        "original_indices": [1],
        "segments": [{"id": 1, "text": "Accepted reviewed unit", "speaker": "Alice"}],
        "cost": 0.02,
        "response_count": 1,
        "cost_sources": ["fixture"],
        "usage": {"prompt_tokens": 8, "completion_tokens": 5},
    }


def _engine(case, *, event=None, cancel=False, failure=None, glossary=None):
    def produce(directory, source_path, _settings, **kwargs):
        path = Path(directory) / "fixture-reviewed.srt"
        text = "Hello reviewed." if case.stage == "correction" else "Hallo geprüft."
        content = f"1\n00:00:01,100 --> 00:00:01,900\n{text}\n"
        path.write_text(content, encoding="utf-8")
        callback = kwargs.get("on_unit_completed")
        if callback is not None:
            payload = _unit(case, source_path)
            callback("fixture-unit", payload)
            case.expected_units["fixture-unit"] = deepcopy(payload)
        if failure is not None:
            raise failure
        if cancel:
            event.set()
        shared = {
            "srt_content": content,
            "output_path": str(path),
            "cost": 0.02,
            "response_count": 1,
            "cost_sources": ("fixture",),
            "usage": {"prompt_tokens": 8, "completion_tokens": 5},
            "speaker_by_subtitle": {1: "Alice"},
            "logical_passages": None,
        }
        if case.stage == "correction":
            return CorrectionResult(**shared)
        return TranslationResult(**shared, block_responses=[], glossary=glossary or {})

    return produce


def _research_runner(case, *, event=None, cancel=False):
    def research(source_text, **kwargs):
        result = WebResearchResult(
            evidence=[
                {
                    "term": "Nautilus",
                    "recommendation": "Nautilus verified",
                    "source_url": "https://example.invalid/new",
                    "source_title": "Fixture evidence",
                }
            ],
            glossary=[{"source": "Nautilus", "target": "Verified Nautilus"}],
            summary="New accepted research summary",
            response_count=1,
            cost=0.01,
            cost_sources=("fixture",),
            usage={"prompt_tokens": 4, "completion_tokens": 3},
        )
        state = {
            "version": 1,
            "kind": "web_research",
            "stage": kwargs["config"].stage,
            "source_hash": hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
            "iteration": 1,
            "conversation": [{"role": "assistant", "content": "Finished fixture research"}],
            "search_count": 1,
            "extraction_count": 0,
            "allowed_urls": ["https://example.invalid/new"],
            "result": result.to_dict(),
            "completed": True,
        }
        state = json.loads(json.dumps(state))
        kwargs["on_checkpoint"](state)
        case.expected_units["research:global:0"] = {
            **deepcopy(state),
            "cost": result.cost,
            "response_count": result.response_count,
            "cost_sources": list(result.cost_sources),
            "usage": dict(result.usage),
        }
        if cancel:
            event.set()
        return result

    return research


def _run(case, *, event=None, progress=None, engine=None, research=False, research_runner=None):
    event = event if event is not None else threading.Event()
    settings = {**case.settings, "web_research_enabled": research}
    credential = SimpleNamespace(resolved_value=lambda: "fixture-credential")
    fixture_settings = {
        "llm_default_model": "fixture/model",
        "correction_model": "fixture/model",
        "translation_model": "fixture/model",
        "request_timeout_seconds": 30,
        "max_segments_per_batch": 1,
    }
    case.engine = Mock(side_effect=engine or _engine(case))
    case.research_runner = Mock(side_effect=research_runner or _research_runner(case))
    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                case.handlers,
                "_with_database_llm_settings",
                side_effect=lambda raw, _stage: {**fixture_settings, **raw},
            )
        )
        stack.enter_context(
            patch(
                "pandrator.web.workflow_handlers.resolve_secret_reference", return_value=credential
            )
        )
        stack.enter_context(
            patch(
                "pandrator.logic.dubbing.llm_correction.correct_srt_file_with_result",
                side_effect=case.engine,
            )
        )
        stack.enter_context(
            patch(
                "pandrator.logic.dubbing.llm_translation.translate_srt_file_with_result",
                side_effect=case.engine,
            )
        )
        stack.enter_context(
            patch(
                "pandrator.logic.dubbing.llm_translation.translate_srt_file_deepl_with_result",
                side_effect=case.engine,
            )
        )
        stack.enter_context(
            patch(
                "pandrator.web.provider_settings.build_llm_settings",
                return_value=(SimpleNamespace(), "fixture/model"),
            )
        )
        stack.enter_context(
            patch("pandrator.web.web_research.JinaResearchProvider", return_value=SimpleNamespace())
        )
        stack.enter_context(
            patch(
                "pandrator.web.context_budget.ContextBudgetService.resolve",
                return_value=ContextBudget("fixture/model", 8192, 1024, 0.8, 4096),
            )
        )
        stack.enter_context(
            patch(
                "pandrator.web.context_budget.ContextBudgetService.partition",
                side_effect=lambda records, **_kwargs: [[dict(record) for record in records]],
            )
        )
        stack.enter_context(
            patch(
                "pandrator.web.web_research.run_web_research_agent",
                side_effect=case.research_runner,
            )
        )
        method = case.handlers.correct if case.stage == "correction" else case.handlers.translate
        return method(
            {
                "session_id": case.session.id,
                "source_artifact_id": case.source.id,
                "settings": settings,
            },
            progress or (lambda *_: None),
            event,
        )


def _run_state(case, expected):
    with case.database.session() as session:
        runs = list(session.scalars(select(AgentRun).where(AgentRun.session_id == case.session.id)))
        assert len(runs) == 1
        run = runs[0]
        assert run.status == expected
        steps = list(session.scalars(select(AgentStep).where(AgentStep.agent_run_id == run.id)))
        actual = {step.unit_key: deepcopy(step.output_json) for step in steps}
        assert all(step.status == "completed" for step in steps)
        assert actual == case.expected_units
        usage = list(session.scalars(select(UsageEvent).where(UsageEvent.agent_run_id == run.id)))
        assert len(usage) == len(case.expected_units)
        assert {event.request_key for event in usage} == set(case.expected_units)
        if expected != "completed":
            assert run.result_artifact_id is None
        session.expunge(run)
    return run


def _resume(case, run):
    resumed = AgenticRunStore(case.database).start(
        kind=run.kind,
        session_id=case.session.id,
        source_artifact=case.artifacts.resolve(case.source.id)[0],
        settings_hash=run.settings_hash,
        settings=run.settings_json,
        job_id=None,
        requested_run_id=run.id,
    )
    assert resumed.id == run.id and resumed.resumed is True
    assert resumed.completed_units == case.expected_units


def _assert_preserved(case):
    assert _snapshot(case) == case.before


def _receipt(session, artifact):
    metadata = deepcopy(artifact.metadata_json)
    document = (
        session.get(Document, metadata["document_id"]) if metadata.get("document_id") else None
    )
    revision = (
        session.get(DocumentRevision, metadata["revision_id"])
        if metadata.get("revision_id")
        else None
    )
    return {
        "metadata": metadata,
        "document": _row(document) if document else None,
        "revision": _row(revision) if revision else None,
    }


def _assert_success(case, result):
    with case.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        receipt = _receipt(session, artifact)
        assert receipt["document"] is not None and receipt["revision"] is not None
        metadata = receipt["metadata"]
        assert metadata["stage"] == case.stage
        assert metadata["has_speaker_metadata"] is True and metadata["speaker_count"] == 1
        assert metadata["language"] == case.language
        assert receipt["revision"]["document_id"] == receipt["document"]["id"]
        assert receipt["document"]["active_revision_id"] == receipt["revision"]["id"]
        segments = list(
            session.scalars(select(Segment).where(Segment.revision_id == metadata["revision_id"]))
        )
        assert len(segments) == 1 and segments[0].speaker == "Alice"
        assert segments[0].metadata_json["speaker_source"] == "model_reviewed"
        assert "logical_passages" not in metadata or metadata["logical_passages"] is None
        assert result["path"] == artifact.relative_path and result["cost"] == 0.02
        assert selected_artifacts(session, case.session.id)[case.stage_key].id == artifact.id
        assert session.get(Artifact, case.previous.id).state == "stale"
        assert session.get(Artifact, case.child.id).state == "stale"
        assert session.get(ArtifactEdge, (case.source.id, artifact.id)) is not None
        if case.stage == "translation":
            assert metadata["backend"] == ("deepl" if case.mode == "deepl-translation" else "llm")
        if case.mode == "deepl-translation":
            assert "agent_run_id" not in result
        else:
            assert metadata["agent_run_id"] == result["agent_run_id"]
    assert (case.paths.root / result["path"]).is_file()
    assert {str(path): path.read_bytes() for path in case.protected_files} == case.before["files"]
    if case.mode != "deepl-translation":
        run = _run_state(case, "completed")
        assert run.result_artifact_id == result["artifact_id"]


@pytest.mark.parametrize("case", MODES, indirect=True)
def test_pre_cancelled_stage_has_no_operation_or_engine_side_effects(case):
    event = threading.Event()
    event.set()
    with pytest.raises(RuntimeError, match=f"Subtitle {case.stage} was canceled\\."):
        _run(case, event=event)
    case.engine.assert_not_called()
    case.research_runner.assert_not_called()
    assert list(case.paths.sessions.glob("*/stage-runs/*")) == case.operation_dirs
    _assert_preserved(case)


@pytest.mark.parametrize("case", MODES, indirect=True)
@pytest.mark.parametrize("value", [0.92, 0.97])
def test_progress_cancellation_preserves_previous_output_and_interrupts_run(case, value):
    event = threading.Event()

    def progress(current, *_):
        if current == value:
            event.set()

    with pytest.raises(RuntimeError, match=f"Subtitle {case.stage} was canceled\\."):
        _run(case, event=event, progress=progress)
    _assert_preserved(case)
    if case.mode != "deepl-translation":
        run = _run_state(case, "interrupted")
        if case.mode == "llm-translation" and value == 0.92:
            _resume(case, run)


@pytest.mark.parametrize("case", MODES, indirect=True)
def test_native_storage_failure_preserves_previous_output(case):
    failure = RuntimeError("fixture native document failure")
    with (
        patch.object(case.handlers, "_store_srt_document", side_effect=failure),
        pytest.raises(RuntimeError) as caught,
    ):
        _run(case)
    assert caught.value is failure
    _assert_preserved(case)
    if case.mode != "deepl-translation":
        _run_state(case, "failed")


@pytest.mark.parametrize("case", MODES, indirect=True)
def test_cancel_after_native_storage_preserves_previous_output(case):
    event = threading.Event()
    native = case.handlers._store_srt_document

    def store(*args, **kwargs):
        result = native(*args, **kwargs)
        event.set()
        return result

    with (
        patch.object(case.handlers, "_store_srt_document", side_effect=store),
        pytest.raises(RuntimeError, match=f"Subtitle {case.stage} was canceled\\."),
    ):
        _run(case, event=event)
    _assert_preserved(case)
    if case.mode != "deepl-translation":
        _run_state(case, "interrupted")


@pytest.mark.parametrize("case", MODES, indirect=True)
def test_promotion_failure_preserves_previous_output_and_has_native_candidate(case):
    native = case.handlers.artifacts.register_in_session
    failure = RuntimeError("fixture final promotion failure")
    receipts = []

    def register(session, *args, **kwargs):
        artifact = native(session, *args, **kwargs)
        if kwargs.get("role") == case.stage:
            receipts.append(_receipt(session, artifact))
            raise failure
        return artifact

    with (
        patch.object(case.handlers.artifacts, "register_in_session", side_effect=register),
        pytest.raises(RuntimeError) as caught,
    ):
        _run(case)
    assert caught.value is failure
    _assert_preserved(case)
    assert len(receipts) == 1
    assert receipts[0]["document"] is not None and receipts[0]["revision"] is not None
    if case.mode != "deepl-translation":
        _run_state(case, "failed")


@pytest.mark.parametrize("case", MODES, indirect=True)
def test_cancel_during_promotion_rolls_back_previous_output(case):
    native = case.handlers.artifacts.register_in_session
    event = threading.Event()

    def register(session, *args, **kwargs):
        artifact = native(session, *args, **kwargs)
        if kwargs.get("role") == case.stage:
            event.set()
        return artifact

    with (
        patch.object(case.handlers.artifacts, "register_in_session", side_effect=register),
        pytest.raises(RuntimeError, match=f"Subtitle {case.stage} was canceled\\."),
    ):
        _run(case, event=event)
    _assert_preserved(case)
    if case.mode != "deepl-translation":
        _run_state(case, "interrupted")


@pytest.mark.parametrize("case", MODES, indirect=True)
def test_success_publishes_native_reviewed_output(case):
    result = _run(case)
    _assert_success(case, result)


@pytest.mark.parametrize("case", LLM_MODES, indirect=True)
def test_engine_failure_retains_accepted_checkpoint_and_native_resume(case):
    failure = RuntimeError("fixture engine failed after accepted unit")
    with pytest.raises(RuntimeError) as caught:
        _run(case, engine=_engine(case, failure=failure))
    assert caught.value is failure
    _assert_preserved(case)
    run = _run_state(case, "failed")
    _resume(case, run)
    case.engine.assert_called_once()


@pytest.mark.parametrize("case", ["llm-translation"], indirect=True)
def test_cancelled_translation_result_does_not_merge_final_glossary(case):
    event = threading.Event()
    with pytest.raises(RuntimeError, match="Subtitle translation was canceled\\."):
        _run(
            case,
            event=event,
            engine=_engine(case, event=event, cancel=True, glossary={"Canceled": "Must stay out"}),
        )
    _assert_preserved(case)
    _run_state(case, "interrupted")


@pytest.mark.parametrize("case", LLM_MODES, indirect=True)
@pytest.mark.parametrize("when", ["completion-progress", "runner-return"])
def test_cancelled_research_retains_checkpoint_without_final_knowledge(case, when):
    event = threading.Event()

    def progress(value, *_):
        if when == "completion-progress" and value == 0.2:
            event.set()

    with pytest.raises(RuntimeError):
        _run(
            case,
            event=event,
            progress=progress,
            research=True,
            research_runner=_research_runner(case, event=event, cancel=when == "runner-return"),
        )
    case.engine.assert_not_called()
    _assert_preserved(case)
    _run_state(case, "interrupted")


@pytest.mark.parametrize("case", LLM_MODES, indirect=True)
def test_successful_research_merges_final_knowledge_and_completes_stage(case):
    result = _run(case, research=True)
    _assert_success(case, result)
    research = case.knowledge.get(case.session.id, "research", **case.knowledge_languages)
    glossary = case.knowledge.get(case.session.id, "glossary", **case.knowledge_languages)
    assert (
        research["payload"]["summary"]
        == "Previously accepted summary New accepted research summary"
    )
    assert any(
        item["recommendation"] == "Nautilus verified" for item in research["payload"]["evidence"]
    )
    entries = {item["source"]: item for item in glossary["payload"]["entries"]}
    assert entries["Nautilus"]["target"] == "Verified Nautilus"
    assert entries["Locked"]["target"] == "Manual seed" and entries["Locked"]["locked"] is True
    artifact, _ = case.artifacts.resolve(result["artifact_id"])
    assert (
        artifact.metadata_json["research"]["summary"]
        == "Previously accepted summary New accepted research summary"
    )
    assert artifact.metadata_json["research"]["agent_run_id"] == result["agent_run_id"]
