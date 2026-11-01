"""Direct transaction-port coverage; no transport or provider execution."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.srt_utils import parse_srt
from pandrator.web import workflow_handlers as handler_module
from pandrator.web.agentic_runs import (
    AgenticRunStore,
    operation_settings_snapshot,
    stable_payload_hash,
)
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import (
    AgentRun,
    AgentStep,
    Artifact,
    Document,
    DocumentRevision,
    PronunciationEntry,
    Segment,
    SegmentLineage,
    UsageEvent,
)
from pandrator.web.pronunciations import PronunciationLibrary
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflow_srt_documents import store_srt_document_in_session
from tests.web_test_support import prepare_web_test_data_root

CUES = "1\n00:00:00,000 --> 00:00:01,000\nHello.\n"
PARENT_CUES = "1\n00:00:00,000 --> 00:00:01,000\nAlice: Hello.\n"


@pytest.fixture
def ports(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    sessions = SessionService(database)
    record = sessions.create("Port source", workflow_kind="audiobook")
    other = sessions.create("Other root", workflow_kind="audiobook")
    artifacts = ArtifactService(database, paths)

    def add_artifact(name: str, content: str, *, session_id: str = record.id):
        path = paths.sessions / record.storage_key / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return artifacts.register(path, kind="srt", role="transcription", session_id=session_id)

    source = add_artifact("source.srt", PARENT_CUES)
    output = add_artifact("output.srt", CUES)
    # Only the two called Handler methods need these direct data/service ports.
    handlers = object.__new__(WorkflowHandlers)
    handlers.database = database
    handlers.paths = paths
    handlers.artifacts = artifacts
    yield SimpleNamespace(
        database=database,
        record=record,
        other=other,
        source=source,
        output=output,
        artifacts=artifacts,
        handlers=handlers,
        add_artifact=add_artifact,
    )
    database.dispose()


def _start(store: AgenticRunStore, ports, session: Session | None = None):
    values: dict[str, Any] = dict(
        kind="correction",
        session_id=ports.record.id,
        source_artifact=ports.source,
        settings_hash="settings",
        settings={},
        job_id=None,
    )
    return store.start(**values) if session is None else store.start_in_session(session, **values)


def test_agent_ports_rollback_new_run_and_checkpoint(ports):
    store = AgenticRunStore(ports.database)
    with pytest.raises(RuntimeError, match="rollback"):
        with ports.database.immediate_session() as session:
            with patch.object(ports.database, "session", side_effect=AssertionError("nested unit")):
                run = _start(store, ports, session)
                store.checkpoint_in_session(
                    session,
                    run.id,
                    unit_key="one",
                    ordinal=0,
                    input_value={"text": "Hello"},
                    output={"text": "Hallo"},
                )
                session.flush()
                assert session.scalar(select(func.count()).select_from(AgentStep)) == 1
                raise RuntimeError("rollback")
    with ports.database.session() as session:
        assert session.scalar(select(func.count()).select_from(AgentRun)) == 0
        assert session.scalar(select(func.count()).select_from(AgentStep)) == 0


def test_agent_ports_finish_and_revision_rollback_usage_assignments(ports):
    store = AgenticRunStore(ports.database)
    run = _start(store, ports)
    with ports.database.session() as session:
        usage = UsageEvent(
            workspace_id=ports.source.workspace_id,
            project_id=ports.source.project_id,
            session_id=ports.record.id,
            agent_run_id=run.id,
            provider_key="test",
            model_id="test",
        )
        session.add(usage)
        session.flush()
        usage_id = usage.id
    with pytest.raises(RuntimeError):
        with ports.database.immediate_session() as session:
            with patch.object(
                ports.database, "immediate_session", side_effect=AssertionError("nested unit")
            ):
                store.checkpoint_in_session(
                    session,
                    run.id,
                    unit_key="one",
                    ordinal=0,
                    input_value="one",
                    output={"value": "done"},
                )
                store.finish_in_session(session, run.id, artifact_id=ports.output.id)
                session.flush()
                raise RuntimeError("rollback")
    with ports.database.session() as session:
        retained = session.get(AgentRun, run.id)
        usage = session.get(UsageEvent, usage_id)
        assert retained is not None and usage is not None
        assert retained.status == "running"
        assert retained.checkpoint_revision == 0
        assert retained.result_artifact_id is None
        assert usage.artifact_id is None
        assert session.scalar(select(func.count()).select_from(AgentStep)) == 0
    store.checkpoint(run.id, unit_key="one", ordinal=0, input_value="one", output={"done": True})
    store.fail(run.id, "retry")
    assert _start(store, ports).completed_units == {"one": {"done": True}}
    store.finish(run.id, artifact_id=ports.output.id)
    with ports.database.session() as session:
        usage = session.get(UsageEvent, usage_id)
        assert usage is not None and usage.artifact_id == ports.output.id


@pytest.mark.parametrize(
    "usage,expected",
    [
        ({}, {"model": "", "backend": "llm", "reasoning_effort": ""}),
        (
            {"llm_default_model": "default", "correction_reasoning_effort": "low"},
            {"model": "default", "backend": "llm", "reasoning_effort": "low"},
        ),
        (
            {"model_name": "named", "backend": "local", "reasoning_effort": "high"},
            {"model": "named", "backend": "local", "reasoning_effort": "high"},
        ),
        (
            {
                "correction_model": "specific",
                "model_name": "named",
                "translation_backend": "remote",
                "backend": "local",
                "reasoning_effort": "high",
                "correction_reasoning_effort": "low",
            },
            {"model": "specific", "backend": "remote", "reasoning_effort": "high"},
        ),
    ],
)
def test_operation_settings_snapshot_preserves_hash_fields_and_fallbacks(usage, expected):
    requested = {"style": "normal"}
    digest, stored = operation_settings_snapshot(
        kind="correction",
        requested_settings=requested,
        instructions="Keep this.",
        usage_settings=usage,
    )
    assert stored == {
        "settings": requested,
        "instructions": "Keep this.",
        "resolved_execution": expected,
    }
    assert digest == stable_payload_hash({"kind": "correction", **stored})


def test_handler_operation_keeps_legacy_checkpoint_ports_and_resume(ports):
    with patch.object(ports.handlers, "_record_usage") as usage:
        store, started, checkpoint = ports.handlers._begin_agentic_operation(
            payload={"session_id": ports.record.id},
            kind="correction",
            source_artifact=ports.source,
            requested_settings={"style": "normal"},
            instructions="Keep.",
            usage_settings={},
        )
        checkpoint("one", {"kind": "correction", "text": "Hello", "cost": 0})
        store.fail(started.id, "retry")
        _, resumed, _ = ports.handlers._begin_agentic_operation(
            payload={"session_id": ports.record.id, "_agent_run_id": started.id},
            kind="correction",
            source_artifact=ports.source,
            requested_settings={"style": "normal"},
            instructions="Keep.",
            usage_settings={},
        )
        assert resumed.resumed and "one" in resumed.completed_units
        usage.assert_called_once()


def test_propose_in_session_rollback_and_reviewed_duplicate(ports):
    library = PronunciationLibrary(ports.database)
    values: dict[str, Any] = dict(
        session_id=ports.record.id,
        source_form="  Imaoka  ",
        phonetic="ee-mah-oh-kah",
        language="EN_us",
        backend="Local Model",
        metadata={"origin": ["owned"]},
    )
    with pytest.raises(RuntimeError):
        with ports.database.immediate_session() as session:
            with patch.object(
                ports.database, "immediate_session", side_effect=AssertionError("nested unit")
            ):
                proposal = library.propose_in_session(session, **values)
                assert proposal["source_form"] == "Imaoka"
                assert proposal["language"] == "en-us"
                assert proposal["backend"] == "local_model"
                assert proposal["source"] == "speech_plan"
                raise RuntimeError("rollback")
    with ports.database.session() as session:
        assert session.scalar(select(func.count()).select_from(PronunciationEntry)) == 0
    proposed = library.propose(**values)
    library.update(proposed["id"], proposed["revision"], {"status": "reviewed"})
    reviewed = library.resolve(
        "Imaoka", session_id=ports.record.id, language="en-us", backend="local_model"
    )[0]
    with ports.database.immediate_session() as session:
        duplicate = library.propose_in_session(session, **values)
        row = session.get(PronunciationEntry, duplicate["id"])
        assert row is not None
        assert row.workspace_id == ports.source.workspace_id
        assert row.project_id == ports.source.project_id
        assert row.normalized_form == "imaoka"
        assert duplicate == reviewed
        assert duplicate["metadata"] == {"origin": ["owned"]}


def test_default_propose_validates_before_opening_writer(ports):
    with patch.object(
        ports.database, "immediate_session", side_effect=AssertionError("writer opened")
    ):
        with pytest.raises(ValueError, match="Written form"):
            PronunciationLibrary(ports.database).propose(
                session_id=ports.record.id,
                source_form="",
                phonetic="x",
                language="en",
            )


def _reviewed(
    library,
    *,
    source_form="Imaoka",
    scope="global",
    session_id=None,
    language="en",
    backend="*",
    metadata=None,
):
    return library.create(
        dict(
            source_form=source_form,
            phonetic="ee-mah-oh-kah",
            scope=scope,
            session_id=session_id,
            language=language,
            backend=backend,
            status="reviewed",
            metadata=metadata or {},
        )
    )


def test_candidate_predicate_prevents_poison_materialization_and_matches_detached(ports):
    library = PronunciationLibrary(ports.database)
    keep = _reviewed(
        library, scope="session", session_id=ports.record.id, metadata={"nested": ["original"]}
    )
    keep = library.resolve("Imaoka", session_id=ports.record.id, language="en")[0]
    poison = _reviewed(library, source_form="Poison", scope="session", session_id=ports.record.id)
    other = _reviewed(library, source_form="Other", scope="session", session_id=ports.other.id)
    with ports.database.immediate_session() as session:
        session.execute(
            text("UPDATE pronunciation_entries SET metadata_json = :bad WHERE id IN (:one, :two)"),
            {"bad": "{broken", "one": poison["id"], "two": other["id"]},
        )
    with ports.database.session() as session:
        candidates = library.reviewed_candidates_in_session(
            session,
            session_id=ports.record.id,
            language="en",
            predicate=PronunciationEntry.id == keep["id"],
        )
    with (
        patch.object(ports.database, "session", side_effect=AssertionError("DB opened")),
        patch.object(Session, "execute", side_effect=AssertionError("SQL")),
        patch.object(Path, "read_text", side_effect=AssertionError("file IO")),
    ):
        matches = library.match_reviewed_candidates(
            candidates, "Imaoka arrived.", session_id=ports.record.id, language="en"
        )
        assert matches == [keep]
        matches[0]["metadata"]["nested"].append("changed")
        assert candidates[0].payload["metadata"]["nested"] == ["original"]


def test_candidate_ranking_equals_legacy_and_legacy_only_payloads_matches(ports):
    library = PronunciationLibrary(ports.database)
    _reviewed(library, language="und")
    selected = _reviewed(library, scope="session", session_id=ports.record.id, backend="local")
    longer = _reviewed(library, source_form="Imaoka House")
    _reviewed(library, source_form="Absent")
    with ports.database.session() as session:
        candidates = library.reviewed_candidates_in_session(
            session, session_id=ports.record.id, language="en", backend="local"
        )
    statements = []

    def record_query(_connection, _cursor, statement, _parameters, _context, _many):
        if "FROM pronunciation_entries" in statement:
            statements.append(statement)

    event.listen(ports.database.engine, "before_cursor_execute", record_query)
    try:
        with (
            patch.object(
                library,
                "reviewed_candidates_in_session",
                side_effect=AssertionError("default snapshot"),
            ),
            patch.object(library, "payload", wraps=library.payload) as payload,
        ):
            legacy = library.resolve(
                "Imaoka House", session_id=ports.record.id, language="en", backend="local"
            )
            assert payload.call_count == 2
    finally:
        event.remove(ports.database.engine, "before_cursor_execute", record_query)
    assert len(statements) == 1
    assert [item["id"] for item in legacy] == [longer["id"], selected["id"]]
    assert (
        library.match_reviewed_candidates(
            candidates, "Imaoka House", session_id=ports.record.id, language="en", backend="local"
        )
        == legacy
    )


def _parent_document(ports, session_id: str, *, poison: bool = False):
    with ports.database.session() as session:
        document = Document(session_id=session_id, stage="transcription")
        session.add(document)
        session.flush()
        revision = DocumentRevision(
            document_id=document.id, revision_number=1, content_hash="parent"
        )
        session.add(revision)
        session.flush()
        parent = Segment(
            revision_id=revision.id,
            ordinal=0,
            start_ms=0,
            end_ms=1000,
            text="Hello.",
            speaker="SECRET" if poison else "Alice",
        )
        session.add(parent)
        session.flush()
        document.active_revision_id = revision.id
        managed = session.get(Artifact, ports.source.id)
        assert managed is not None
        managed.metadata_json = {"revision_id": revision.id}
        if poison:
            session.execute(
                text("UPDATE segments SET metadata_json = '{broken' WHERE id = :id"),
                {"id": parent.id},
            )
        return revision.id, parent.id


@pytest.mark.parametrize("rollback", [False, True])
def test_shared_srt_writer_is_caller_transaction_and_has_no_file_io(ports, rollback):
    parent_revision, parent_id = _parent_document(ports, ports.record.id)
    rows = [
        {
            "id": "p1",
            "start_ms": 0,
            "end_ms": 1000,
            "text": "Hello.",
            "speaker": "Alice",
            "review_state": "uncertain",
        }
    ]
    ports.output.metadata_json = {
        "speech_markup": {"1": '<segment id="1"><narrator>Hello.</narrator></segment>'}
    }
    ports.source.metadata_json = {"revision_id": parent_revision}

    def write():
        with ports.database.immediate_session() as session:
            managed = session.get(Artifact, ports.output.id)
            parent = session.get(Segment, parent_id)
            assert managed is not None and parent is not None
            with (
                patch.object(Path, "read_text", side_effect=AssertionError("file IO")),
                patch.object(ports.artifacts, "resolve", side_effect=AssertionError("artifact IO")),
                patch.object(ports.database, "session", side_effect=AssertionError("nested unit")),
            ):
                document_id, revision_id = store_srt_document_in_session(
                    session,
                    ports.record.id,
                    ports.output,
                    managed,
                    "tts_optimized",
                    parse_srt(CUES),
                    [parent],
                    [],
                    language="en",
                    logical_passages=rows,
                    parent_artifact=ports.source,
                )
                session.flush()
                child = session.scalar(select(Segment).where(Segment.revision_id == revision_id))
                assert child is not None and child.speaker == "Alice"
                assert child.metadata_json["review_state"] == "uncertain"
                assert "<narrator>Hello.</narrator>" in child.metadata_json["speech_xml"]
                assert (
                    managed.metadata_json["logical_passages"]["source_artifact_id"]
                    == ports.source.id
                )
                assert managed.metadata_json["uncertain_segment_count"] == 1
                assert session.get(SegmentLineage, (parent_id, child.id)) is not None
                if rollback:
                    raise RuntimeError("rollback")
                return document_id, revision_id

    if rollback:
        with pytest.raises(RuntimeError, match="rollback"):
            write()
    else:
        write()
    with ports.database.session() as session:
        managed = session.get(Artifact, ports.output.id)
        assert managed is not None
        assert ("revision_id" in managed.metadata_json) is not rollback
        assert session.scalar(select(func.count()).select_from(Document)) == (1 if rollback else 2)
        assert session.scalar(select(func.count()).select_from(DocumentRevision)) == (
            1 if rollback else 2
        )
        assert session.scalar(select(func.count()).select_from(Segment)) == (1 if rollback else 2)
        assert session.scalar(select(func.count()).select_from(SegmentLineage)) == (
            0 if rollback else 1
        )


@pytest.mark.parametrize("foreign", [False, True])
def test_handler_validates_actual_parent_namespace_before_segment_hydration(ports, foreign):
    parent_revision, parent_id = _parent_document(
        ports, ports.other.id if foreign else ports.record.id, poison=foreign
    )
    # Payload session/metadata are untrusted; the actual Artifact session remains source.
    ports.source.session_id = ports.other.id
    ports.source.metadata_json = {"revision_id": parent_revision}
    _, revision_id = ports.handlers._store_srt_document(
        ports.record.id,
        ports.output,
        "tts_optimized",
        parent_artifact=ports.source,
    )
    with ports.database.session() as session:
        child = session.scalar(select(Segment).where(Segment.revision_id == revision_id))
        assert child is not None and child.speaker == "Alice"
        lineage = session.get(SegmentLineage, (parent_id, child.id))
        assert (lineage is None) is foreign


def test_handler_srt_preserves_late_bound_hash_speaker_and_review_override(ports):
    with (
        patch.object(handler_module, "_hash_segments", return_value="patched-hash") as digest,
        patch.object(handler_module, "_dominant_speaker", return_value="Patched") as speaker,
    ):
        _, revision_id = ports.handlers._store_srt_document(
            ports.record.id, ports.output, "tts_optimized"
        )
        digest.assert_called_once()
        speaker.assert_called_once()
    with ports.database.session() as session:
        revision = session.get(DocumentRevision, revision_id)
        child = session.scalar(select(Segment).where(Segment.revision_id == revision_id))
        assert revision is not None and revision.content_hash == "patched-hash"
        assert child is not None and child.speaker == "Patched"
    _, reviewed_revision = ports.handlers._store_srt_document(
        ports.record.id,
        ports.output,
        "tts_optimized",
        speaker_overrides={1: "Reviewed"},
    )
    with ports.database.session() as session:
        child = session.scalar(select(Segment).where(Segment.revision_id == reviewed_revision))
        managed = session.get(Artifact, ports.output.id)
        assert child is not None and child.speaker == "Reviewed"
        assert child.metadata_json["speaker_source"] == "model_reviewed"
        assert managed is not None and managed.metadata_json["speaker_reviewed_by_model"] is True
