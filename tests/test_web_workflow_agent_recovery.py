"""Document optimization recovery stays visible through its canonical stage."""

from pathlib import Path

import pytest
from sqlalchemy import select

from pandrator.web.agentic_runs import AgenticRunStore
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import AgentRun, AgentStep, Job
from pandrator.web.sessions import SessionService
from pandrator.web.subtitle_sources import adopt_subtitle_source_in_session
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace import OutcomePlanService
from tests.web_test_support import prepare_web_test_data_root


@pytest.mark.parametrize("status", ["failed", "interrupted"])
def test_document_optimization_checkpoint_is_visible_for_resume(
    tmp_path: Path, status: str
) -> None:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        record = SessionService(database).create("Resume optimization", workflow_kind="voiceover")
        source_path = paths.uploads / "captions.srt"
        source_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        artifacts = ArtifactService(database, paths)
        source = artifacts.register(
            source_path,
            kind="srt",
            role="upload",
            session_id=record.id,
            metadata={"original_filename": "captions.srt"},
        )
        with database.session() as session:
            adopt_subtitle_source_in_session(session, artifacts, record.id, source.id)
        outcomes = OutcomePlanService(database)
        current = outcomes.get(record.id)
        value = current["value"]
        value["transformations"]["llm_tts_document_optimization"] = True
        value["inputs"]["generation"] = "source"
        outcomes.update(record.id, current["revision"], value)
        queue = JobQueue(database)
        job = queue.enqueue("text.optimize_tts", {"session_id": record.id}, session_id=record.id)
        store = AgenticRunStore(database)
        started = store.start(
            kind="tts_optimization",
            session_id=record.id,
            source_artifact=source,
            settings_hash="native-test-settings",
            settings={},
            job_id=job.id,
        )
        store.checkpoint(
            started.id,
            unit_key="block:0",
            ordinal=0,
            input_value={"text": "Hello"},
            output={"text": "Hello."},
        )
        store.fail(started.id, "Interrupted test operation")
        with database.session() as session:
            managed_job = session.get(Job, job.id)
            run = session.get(AgentRun, started.id)
            assert managed_job is not None and run is not None
            managed_job.status = "failed"
            run.status = status

        def stored_rows() -> list[list[tuple[object, ...]]]:
            with database.session() as session:
                return [
                    [
                        tuple(row)
                        for row in session.execute(select(model.__table__).order_by(model.id))
                    ]
                    for model in (Job, AgentRun, AgentStep)
                ]

        before = stored_rows()
        stages = WorkflowService(database, queue).snapshot(record.id)["stages"]
        assert stored_rows() == before
        stage = next(item for item in stages if item["key"] == "optimize_tts")
        assert stage["status"] == "failed"
        assert stage["optimization_timing"] == "document"
        assert stage["agent_run_id"] == started.id
        assert stage["resumable"] is True
        resumed, previous_job = store.prepare_resume(started.id)
        assert resumed.status == "retrying"
        assert previous_job.id == job.id
    finally:
        database.dispose()
