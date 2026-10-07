"""Compact generation reads stay bounded as history and takes grow."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event, insert
from sqlalchemy.orm import Session

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_summary import get_generation_summary
from pandrator.web.openapi import build_openapi_document
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def summary_case(tmp_path):
    prepare_web_test_data_root(tmp_path)
    tokens = BootstrapTokenStore()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=tokens)
    client = app.test_client()
    client.post("/api/v1/auth/bootstrap", json={"token": tokens.issue()})
    services = app.extensions["pandrator"]["services"]
    with services.database.session() as db:
        record = m.SessionRecord(name="Generation summary")
        db.add(record)
        db.flush()
        session_id = record.id
    yield services, client, session_id
    services.database.dispose()


def add_plan(case, *, session_id=None):
    services, _, own_session_id = case
    session_id = session_id or own_session_id
    with services.database.session() as db:
        plan = m.GenerationPlan(session_id=session_id)
        db.add(plan)
        db.flush()
        revision = m.GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="summary-plan"
        )
        db.add(revision)
        db.flush()
        plan.active_revision_id = revision.id
        segments = [
            m.GenerationSegment(plan_revision_id=revision.id, ordinal=0, text="Included"),
            m.GenerationSegment(
                plan_revision_id=revision.id, ordinal=1, text="Excluded", removed=True
            ),
        ]
        db.add_all(segments)
        db.flush()
        return revision.id, segments[0].id


def add_run(case, revision_id, *, status="running", sequence=1, session_id=None):
    services, _, own_session_id = case
    session_id = session_id or own_session_id
    with services.database.session() as db:
        job = m.Job(
            session_id=session_id, kind="generation.run", status=status,
            progress=0.4, progress_detail="Generating block 2",
        )
        db.add(job)
        db.flush()
        run = m.GenerationRun(
            session_id=session_id, plan_revision_id=revision_id, job_id=job.id,
            sequence_number=sequence, status=status,
        )
        db.add(run)
        db.flush()
        return run.id


def read(case):
    _, client, session_id = case
    response = client.get(f"/api/v1/sessions/{session_id}/generation/summary")
    assert response.status_code == 200
    return response.get_json()


def test_no_plan_and_empty_plan(summary_case):
    services, _, session_id = summary_case
    assert read(summary_case) == {
        "session_id": session_id, "plan_revision_id": None,
        "total": 0, "included_total": 0, "active_run": None, "assembly": None,
    }
    revision_id, _ = add_plan(summary_case)
    with services.database.session() as db:
        db.query(m.GenerationSegment).filter_by(plan_revision_id=revision_id).delete()
    result = read(summary_case)
    assert result["plan_revision_id"] == revision_id
    assert result["total"] == result["included_total"] == 0


def test_active_plan_counts_preserve_segment_total_semantics(summary_case):
    services, client, session_id = summary_case
    revision_id, _ = add_plan(summary_case)
    with services.database.session() as db:
        old_revision = m.GenerationPlanRevision(
            plan_id=db.get(m.GenerationPlanRevision, revision_id).plan_id,
            revision_number=2, content_hash="inactive",
        )
        db.add(old_revision)
        db.flush()
        db.add(m.GenerationSegment(plan_revision_id=old_revision.id, ordinal=0, text="Old"))
    result = read(summary_case)
    assert result["plan_revision_id"] == revision_id
    assert result["total"] == 2
    assert result["included_total"] == 1
    assert result["total"] == client.get(
        f"/api/v1/sessions/{session_id}/generation-segments"
    ).get_json()["total"]


@pytest.mark.parametrize("status", ["running", "pausing", "pause_requested", "cancel_requested"])
def test_worker_run_precedes_newer_queued_run(summary_case, status):
    revision_id, _ = add_plan(summary_case)
    worker_id = add_run(summary_case, revision_id, status=status)
    add_run(summary_case, revision_id, status="queued", sequence=2)
    assert read(summary_case)["active_run"] == {
        "id": worker_id, "status": status, "progress": 0.4,
        "progress_detail": "Generating block 2",
    }


def test_status_priority_then_newest_sequence_and_paused_fallback(summary_case):
    revision_id, _ = add_plan(summary_case)
    add_run(summary_case, revision_id, status="paused")
    queued_id = add_run(summary_case, revision_id, status="queued", sequence=2)
    assert read(summary_case)["active_run"]["id"] == queued_id
    newest_id = add_run(summary_case, revision_id, status="queued", sequence=3)
    assert read(summary_case)["active_run"]["id"] == newest_id
    services, _, _ = summary_case
    with services.database.session() as db:
        db.get(m.GenerationRun, queued_id).status = "completed"
        db.get(m.GenerationRun, newest_id).status = "failed"
    assert read(summary_case)["active_run"]["status"] == "paused"


def test_verified_repair_child_reports_actual_worker_progress(summary_case):
    services, _, _ = summary_case
    revision_id, _ = add_plan(summary_case)
    parent_id = add_run(summary_case, revision_id, status="completed")
    with services.database.session() as db:
        child_revision = m.GenerationPlanRevision(
            plan_id=db.get(m.GenerationPlanRevision, revision_id).plan_id,
            revision_number=2, content_hash="repair-child",
            operation_json={
                "reason": "early_timing_repair", "source_generation_run_id": parent_id,
                "repair_status": "pending",
            },
        )
        db.add(child_revision)
        db.flush()
        child_revision_id = child_revision.id
    child_id = add_run(summary_case, child_revision_id, sequence=2)
    with services.database.session() as db:
        child = db.get(m.GenerationRun, child_id)
        child.source_generation_run_id = parent_id
        child.settings_snapshot_json = {"early_repair_parent_run_id": parent_id}
    add_run(summary_case, revision_id, status="queued", sequence=3)
    full_runs = services.generation.list_runs(summary_case[2])
    assert next(run for run in full_runs if run["id"] == child_id)[
        "early_repair_parent_run_id"
    ] == parent_id
    assert read(summary_case)["active_run"] == {
        "id": child_id, "status": "running", "progress": 0.4,
        "progress_detail": "Generating block 2",
    }


def test_latest_assembly_and_cross_session_isolation(summary_case):
    services, _, session_id = summary_case
    revision_id, _ = add_plan(summary_case)
    run_id = add_run(summary_case, revision_id)
    with services.database.session() as db:
        foreign = m.SessionRecord(name="Foreign")
        db.add(foreign)
        db.flush()
        foreign_id = foreign.id
        foreign_job = m.Job(
            session_id=foreign_id, kind="generation.assemble", status="running",
            progress=0.9, progress_detail="Private progress",
        )
        db.add(foreign_job)
        db.flush()
        # Even a corrupted cross-session job reference cannot leak progress.
        db.get(m.GenerationRun, run_id).job_id = foreign_job.id
        now = m.utcnow()
        older = m.OutputAssembly(session_id=session_id, status="completed", created_at=now)
        newest = m.OutputAssembly(
            session_id=session_id, job_id=foreign_job.id, status="running",
            created_at=now + timedelta(seconds=1),
        )
        db.add_all([older, newest, m.OutputAssembly(
            session_id=foreign_id, status="completed", created_at=now + timedelta(days=1)
        )])
        db.flush()
        newest_id = newest.id
    foreign_revision, _ = add_plan(summary_case, session_id=foreign_id)
    add_run(summary_case, foreign_revision, sequence=100, session_id=foreign_id)
    result = read(summary_case)
    assert result["active_run"] == {
        "id": run_id, "status": "running", "progress": 0.0, "progress_detail": None,
    }
    assert result["assembly"] == {
        "id": newest_id, "status": "running", "progress": 0.0, "progress_detail": None,
    }
    with services.database.session() as db:
        job = m.Job(
            session_id=session_id, kind="generation.assemble", progress=0.65,
            progress_detail="Writing output",
        )
        db.add(job)
        db.flush()
        assembly = db.get(m.OutputAssembly, newest_id)
        assembly.job_id = job.id
    assert read(summary_case)["assembly"]["progress"] == 0.65
    assert read(summary_case)["assembly"]["progress_detail"] == "Writing output"
    with services.database.session() as db:
        db.get(m.OutputAssembly, newest_id).status = "stale"
    assert read(summary_case)["assembly"]["progress"] == 1.0


def test_missing_trashed_and_authentication(summary_case):
    services, client, session_id = summary_case
    assert client.get("/api/v1/sessions/missing/generation/summary").status_code == 404
    assert client.application.test_client().get(
        f"/api/v1/sessions/{session_id}/generation/summary"
    ).status_code == 401
    with services.database.session() as db:
        db.get(m.SessionRecord, session_id).trashed_at = m.utcnow()
    assert client.get(f"/api/v1/sessions/{session_id}/generation/summary").status_code == 404


def test_queries_and_model_hydration_are_constant_with_1000_runs_30000_takes(summary_case):
    services, _, session_id = summary_case
    revision_id, segment_id = add_plan(summary_case)
    run_id = add_run(summary_case, revision_id)
    with services.database.session() as db:
        db.add(m.OutputAssembly(session_id=session_id, status="completed"))

    def measured_read():
        statements, hydrated = [], []

        def statement(_conn, _cursor, sql, _parameters, _context, _many):
            statements.append(sql)

        def loaded(_db, instance):
            hydrated.append(type(instance))

        event.listen(services.database.engine, "before_cursor_execute", statement)
        event.listen(Session, "loaded_as_persistent", loaded)
        try:
            with patch.object(services.generation, "list_segments", side_effect=AssertionError), \
                 patch.object(services.generation, "list_runs", side_effect=AssertionError), \
                 patch.object(services.generation, "latest_run", side_effect=AssertionError), \
                 patch.object(services.generation, "latest_assembly", side_effect=AssertionError):
                result = get_generation_summary(services.database, session_id)
        finally:
            event.remove(services.database.engine, "before_cursor_execute", statement)
            event.remove(Session, "loaded_as_persistent", loaded)
        assert hydrated == []
        assert len(statements) == 5  # Explicit snapshot BEGIN and four SELECTs.
        assert all("audio_takes" not in sql and "_json" not in sql for sql in statements)
        assert sum("LIMIT" in sql for sql in statements) == 2
        return result, statements

    initial, small_queries = measured_read()
    with services.database.session() as db:
        db.execute(insert(m.GenerationRun), [
            {"id": f"history-{index}", "session_id": session_id,
             "plan_revision_id": revision_id, "sequence_number": index + 2,
             "status": "completed", "settings_snapshot_json": {"large": "x" * 1000}}
            for index in range(999)
        ])
        db.execute(insert(m.AudioTake), [
            {"id": f"take-{index}", "generation_segment_id": segment_id,
             "generation_run_id": run_id}
            for index in range(30000)
        ])
    expanded, large_queries = measured_read()
    assert initial == expanded
    assert small_queries == large_queries
    assert read(summary_case) == expanded


def test_openapi_declares_compact_response():
    document = build_openapi_document()
    operation = document["paths"]["/api/v1/sessions/{sessionId}/generation/summary"]["get"]
    assert operation["operationId"] == "getGenerationSummary"
    assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/GenerationSummary"
    }
    assert set(document["components"]["schemas"]["GenerationSummary"]["required"]) == {
        "session_id", "plan_revision_id", "total", "included_total", "active_run", "assembly"
    }
