"""Compact generation reads stay bounded as history and takes grow."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import event, insert, select
from sqlalchemy.orm import Session

from pandrator.web import models as m
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_summary import get_generation_summary
from pandrator.web.openapi import build_openapi_document
from pandrator.web.usage import usage_summary
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


def add_source_artifact(case, session_id, *, role, artifact_id=None):
    services, _, _ = case
    with services.database.session() as db:
        artifact = m.Artifact(
            id=artifact_id,
            session_id=session_id,
            kind="text",
            role=role,
            relative_path=f"test/generation-summary/{session_id}/{role}",
        )
        db.add(artifact)
        db.flush()
        return artifact.id


def set_source_reference(case, revision_id, *, value=None, include=True):
    services, _, _ = case
    with services.database.session() as db:
        revision = db.get(m.GenerationPlanRevision, revision_id)
        revision.settings_json = (
            {"_source_artifact_id": value} if include else {}
        )


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


def run_job_id(case, run_id):
    services, _, _ = case
    with services.database.session() as db:
        return db.scalar(
            select(m.GenerationRun.job_id).where(m.GenerationRun.id == run_id)
        )


def add_usage_event(
    case,
    *,
    session_id,
    generation_run_id,
    stage,
    model_id,
    cost_usd,
    input_tokens,
    cached_input_tokens,
    output_tokens,
    commercial,
    estimated,
):
    services, _, _ = case
    with services.database.session() as db:
        event = m.UsageEvent(
            session_id=session_id,
            generation_run_id=generation_run_id,
            stage=stage,
            provider_key="test-provider",
            model_id=model_id,
            cost_usd=cost_usd,
            input_tokens=input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            raw_usage_json={"commercial": commercial, "estimated": estimated},
        )
        db.add(event)
        db.flush()
        return event.id


def read(case):
    _, client, session_id = case
    response = client.get(f"/api/v1/sessions/{session_id}/generation/summary")
    assert response.status_code == 200
    return response.get_json()


def test_no_plan_and_empty_plan(summary_case):
    services, _, session_id = summary_case
    assert read(summary_case) == {
        "session_id": session_id, "plan_revision_id": None,
        "plan_source_role": None, "total": 0, "included_total": 0,
        "active_run": None, "assembly": None,
    }
    revision_id, _ = add_plan(summary_case)
    with services.database.session() as db:
        db.query(m.GenerationSegment).filter_by(plan_revision_id=revision_id).delete()
    result = read(summary_case)
    assert result["plan_revision_id"] == revision_id
    assert result["total"] == result["included_total"] == 0


@pytest.mark.parametrize("role", ["tts_optimized", "prepared_text"])
def test_active_plan_source_role_is_resolved_from_same_session_artifact(summary_case, role):
    services, _, session_id = summary_case
    revision_id, _ = add_plan(summary_case)
    artifact_id = add_source_artifact(
        summary_case, session_id, role=role
    )
    set_source_reference(summary_case, revision_id, value=artifact_id)

    result = read(summary_case)

    assert result["plan_revision_id"] == revision_id
    assert result["plan_source_role"] == role
    with services.database.session() as db:
        artifact = db.get(m.Artifact, artifact_id)
        assert artifact.role == role


@pytest.mark.parametrize(
    "reference_kind", ["missing", "malformed", "unmatched", "cross_session"]
)
def test_invalid_or_foreign_active_plan_source_has_no_role(summary_case, reference_kind):
    services, _, session_id = summary_case
    revision_id, _ = add_plan(summary_case)

    if reference_kind == "missing":
        set_source_reference(summary_case, revision_id, include=False)
    elif reference_kind == "malformed":
        # A malformed JSON number must not match even a same-session artifact ID.
        add_source_artifact(
            summary_case, session_id, role="invalid_numeric_match", artifact_id="123"
        )
        set_source_reference(summary_case, revision_id, value=123)
    elif reference_kind == "unmatched":
        set_source_reference(
            summary_case,
            revision_id,
            value="00000000-0000-0000-0000-000000000099",
        )
    else:
        with services.database.session() as db:
            foreign = m.SessionRecord(name="Foreign source")
            db.add(foreign)
            db.flush()
            foreign_session_id = foreign.id
        artifact_id = add_source_artifact(
            summary_case,
            foreign_session_id,
            role="tts_optimized",
            artifact_id="00000000-0000-0000-0000-000000000099",
        )
        set_source_reference(summary_case, revision_id, value=artifact_id)

    assert read(summary_case)["plan_source_role"] is None


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
        "job_id": run_job_id(summary_case, worker_id),
        "usage": None,
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


def test_zero_event_active_run_usage_is_none(summary_case):
    revision_id, _ = add_plan(summary_case)
    run_id = add_run(summary_case, revision_id)

    active_run = read(summary_case)["active_run"]

    assert active_run["id"] == run_id
    assert active_run["job_id"] == run_job_id(summary_case, run_id)
    assert active_run["usage"] is None


def test_active_run_usage_matches_usage_summary_and_refreshes(summary_case):
    services, _, session_id = summary_case
    revision_id, _ = add_plan(summary_case)
    run_id = add_run(summary_case, revision_id)
    active_job_id = run_job_id(summary_case, run_id)
    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=run_id,
        stage="tts",
        model_id="model-z",
        cost_usd=0.12,
        input_tokens=100,
        cached_input_tokens=20,
        output_tokens=25,
        commercial=True,
        estimated=False,
    )
    first = read(summary_case)["active_run"]
    with services.database.session() as db:
        first_events = list(
            db.scalars(
                select(m.UsageEvent).where(
                    m.UsageEvent.session_id == session_id,
                    m.UsageEvent.generation_run_id == run_id,
                )
            )
        )
    assert first["job_id"] == active_job_id
    assert first["usage"] == usage_summary(first_events)
    assert first["usage"]["total_cost_usd"] == pytest.approx(0.12)
    assert first["usage"]["event_count"] == 1

    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=run_id,
        stage="tts",
        model_id="model-a",
        cost_usd=0.0,
        input_tokens=50,
        cached_input_tokens=10,
        output_tokens=10,
        commercial=True,
        estimated=True,
    )
    second = read(summary_case)["active_run"]
    with services.database.session() as db:
        second_events = list(
            db.scalars(
                select(m.UsageEvent).where(
                    m.UsageEvent.session_id == session_id,
                    m.UsageEvent.generation_run_id == run_id,
                )
            )
        )
    assert second["usage"] == usage_summary(second_events)
    assert second["usage"] != first["usage"]
    assert second["usage"]["priced_event_count"] == 2
    assert second["usage"]["total_cost_usd"] == pytest.approx(0.12)

    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=run_id,
        stage="optimization",
        model_id="model-z",
        cost_usd=None,
        input_tokens=7,
        cached_input_tokens=0,
        output_tokens=3,
        commercial=True,
        estimated=True,
    )
    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=run_id,
        stage=None,
        model_id="",
        cost_usd=0.03,
        input_tokens=10,
        cached_input_tokens=2,
        output_tokens=2,
        commercial=False,
        estimated=False,
    )
    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=run_id,
        stage="",
        model_id="   ",
        cost_usd=None,
        input_tokens=0,
        cached_input_tokens=0,
        output_tokens=0,
        commercial=False,
        estimated=False,
    )
    unrelated_run_id = add_run(
        summary_case, revision_id, status="completed", sequence=2
    )
    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=unrelated_run_id,
        stage="private",
        model_id="foreign-model",
        cost_usd=99.0,
        input_tokens=999,
        cached_input_tokens=0,
        output_tokens=999,
        commercial=True,
        estimated=True,
    )
    with services.database.session() as db:
        foreign_session = m.SessionRecord(name="Foreign usage session")
        db.add(foreign_session)
        db.flush()
        foreign_session_id = foreign_session.id
    foreign_revision_id, _ = add_plan(
        summary_case, session_id=foreign_session_id
    )
    foreign_run_id = add_run(
        summary_case,
        foreign_revision_id,
        status="completed",
        session_id=foreign_session_id,
    )
    add_usage_event(
        summary_case,
        session_id=foreign_session_id,
        generation_run_id=run_id,
        stage="foreign-session",
        model_id="foreign-model",
        cost_usd=100.0,
        input_tokens=1000,
        cached_input_tokens=0,
        output_tokens=1000,
        commercial=True,
        estimated=True,
    )
    add_usage_event(
        summary_case,
        session_id=session_id,
        generation_run_id=foreign_run_id,
        stage="foreign-run",
        model_id="foreign-model",
        cost_usd=200.0,
        input_tokens=2000,
        cached_input_tokens=0,
        output_tokens=2000,
        commercial=True,
        estimated=True,
    )

    result = read(summary_case)["active_run"]
    with services.database.session() as db:
        matching_events = list(
            db.scalars(
                select(m.UsageEvent).where(
                    m.UsageEvent.session_id == session_id,
                    m.UsageEvent.generation_run_id == run_id,
                )
            )
        )
    usage = result["usage"]
    assert result["job_id"] == active_job_id
    assert usage == usage_summary(matching_events)
    assert usage == {
        "total_cost_usd": pytest.approx(0.15),
        "event_count": 5,
        "priced_event_count": 3,
        "has_unpriced_usage": True,
        "commercial": True,
        "estimated": True,
        "input_tokens": 167,
        "cached_input_tokens": 32,
        "output_tokens": 40,
        "total_tokens": 207,
        "model_ids": ["model-a", "model-z"],
        "stages": [
            {
                "stage": "optimization",
                "cost_usd": 0.0,
                "event_count": 1,
                "priced_event_count": 0,
                "input_tokens": 7,
                "cached_input_tokens": 0,
                "output_tokens": 3,
            },
            {
                "stage": "other",
                "cost_usd": pytest.approx(0.03),
                "event_count": 2,
                "priced_event_count": 1,
                "input_tokens": 10,
                "cached_input_tokens": 2,
                "output_tokens": 2,
            },
            {
                "stage": "tts",
                "cost_usd": pytest.approx(0.12),
                "event_count": 2,
                "priced_event_count": 2,
                "input_tokens": 150,
                "cached_input_tokens": 30,
                "output_tokens": 35,
            },
        ],
    }


def test_usage_aggregate_query_runs_only_for_an_active_run(summary_case):
    services, _, session_id = summary_case
    revision_id, _ = add_plan(summary_case)

    def captured_read():
        statements = []
        def statement(_conn, _cursor, sql, _parameters, _context, _many):
            statements.append(sql)

        event.listen(services.database.engine, "before_cursor_execute", statement)
        try:
            get_generation_summary(services.database, session_id)
        finally:
            event.remove(services.database.engine, "before_cursor_execute", statement)
        return statements

    without_active_run = captured_read()
    assert not any("usage_events" in sql.lower() for sql in without_active_run)

    add_run(summary_case, revision_id)
    with_active_run = captured_read()
    usage_queries = [
        sql for sql in with_active_run if "usage_events" in sql.lower()
    ]
    assert len(usage_queries) == 1


def test_workflow_keeps_usage_flags_on_reload_and_completion(summary_case):
    services, client, session_id = summary_case
    revision_id, _ = add_plan(summary_case)
    run_id = add_run(summary_case, revision_id)
    for cost in (0.01, None):
        add_usage_event(
            summary_case, session_id=session_id, generation_run_id=run_id,
            stage="tts", model_id="gemini-3.8-flash-tts", cost_usd=cost,
            input_tokens=10, cached_input_tokens=0, output_tokens=25,
            commercial=True, estimated=True,
        )
    for status in ("running", "completed"):
        with services.database.session() as db:
            db.get(m.GenerationRun, run_id).status = status
        response = client.get(f"/api/v1/sessions/{session_id}/workflow")
        assert response.status_code == 200
        stage = next(item for item in response.get_json()["stages"] if item["key"] == "generate_audio")
        assert stage["usage"]["estimated"] is True
        assert stage["usage"]["has_unpriced_usage"] is True
        assert stage["usage"]["cost_usd"] == pytest.approx(0.01)


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
        "job_id": run_job_id(summary_case, child_id),
        "usage": None,
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
        foreign_job_id = foreign_job.id
        # Even a corrupted cross-session job reference cannot leak progress.
        db.get(m.GenerationRun, run_id).job_id = foreign_job_id
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
        "job_id": None, "usage": None,
    }
    assert result["assembly"] == {
        "id": newest_id, "status": "running", "progress": 0.0, "progress_detail": None,
        "job_id": None, "usage": None,
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
        assert len(statements) == 6  # Explicit snapshot BEGIN and five SELECTs.
        assert all("audio_takes" not in sql for sql in statements)
        settings_queries = [sql for sql in statements if "settings_json" in sql.lower()]
        assert len(settings_queries) == 1
        selected_columns = settings_queries[0].partition("FROM")[0].lower()
        assert "settings_json" not in selected_columns
        assert "artifacts.role" in selected_columns
        usage_queries = [sql for sql in statements if "usage_events" in sql.lower()]
        assert len(usage_queries) == 1
        usage_projection = usage_queries[0].partition("FROM")[0].lower()
        assert "raw_usage_json" in usage_projection
        assert "json_extract" in usage_projection
        assert "select usage_events.raw_usage_json" not in usage_projection
        assert all(
            "_json" not in sql or "settings_json" in sql.lower()
            or sql in usage_queries
            for sql in statements
        )
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
