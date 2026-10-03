"""Native agent link selection and bounded workflow snapshot read contracts."""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from pandrator.web import workflows
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    AgentRun,
    Artifact,
    Job,
    OutcomePlan,
    SessionRecord,
    SessionSetting,
    SessionStageSelection,
    new_id,
)
from pandrator.web.sessions import SessionService
from pandrator.web.workflows import WorkflowService
from scripts.phase0_baseline import QueryCounter
from tests.web_test_support import prepare_web_test_data_root

BASE = datetime(2026, 10, 1, tzinfo=UTC)
CURRENT = BASE + timedelta(days=2)
METRIC_NOW = CURRENT + timedelta(days=1)
METADATA_RUN_ID = "00000000-0000-0000-0000-00000000b001"
ARTIFACT_RUN_ID = "00000000-0000-0000-0000-00000000a001"
JOB_RUN_ID = "00000000-0000-0000-0000-00000000a002"


@dataclass
class ProjectionCase:
    database: Database
    workflow: WorkflowService
    session_id: str
    job_id: str


@dataclass
class Measurement:
    payload: dict[str, object]
    counter: QueryCounter


@pytest.fixture
def projection_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ProjectionCase]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        record = SessionService(database).create("Agent projection", workflow_kind="voiceover")
        job_id = new_id()
        with database.session() as session:
            session.add(
                Job(
                    id=job_id,
                    session_id=record.id,
                    kind="dubbing.translate",
                    status="running",
                    created_at=CURRENT,
                    updated_at=CURRENT,
                    started_at=CURRENT,
                    progress=0.4,
                    progress_detail="Current translation",
                )
            )
        monkeypatch.setattr(workflows, "utcnow", lambda: METRIC_NOW)
        yield ProjectionCase(
            database, WorkflowService(database, JobQueue(database)), record.id, job_id
        )
    finally:
        database.dispose()


def _rows(database: Database) -> dict[str, list[tuple[object, ...]]]:
    result = {}
    with database.session() as session:
        for model in (
            SessionRecord,
            SessionSetting,
            OutcomePlan,
            SessionStageSelection,
            Artifact,
            Job,
            AgentRun,
        ):
            table = model.__table__
            order = list(table.primary_key)
            result[model.__tablename__] = [
                tuple(row) for row in session.execute(select(table).order_by(*order))
            ]
    return result


def _measure(case: ProjectionCase) -> Measurement:
    before = _rows(case.database)
    with QueryCounter(case.database) as counter:
        payload = case.workflow.snapshot(case.session_id)
    assert _rows(case.database) == before
    return Measurement(payload, counter)


def _translation(measurement: Measurement) -> dict[str, object]:
    stages = measurement.payload["stages"]
    assert isinstance(stages, list)
    for stage in stages:
        assert isinstance(stage, dict)
        if stage["key"] == "translate":
            return stage
    raise AssertionError("Translation stage missing")


def _set_job_status(case: ProjectionCase, status: str) -> None:
    with case.database.session() as session:
        job = session.get(Job, case.job_id)
        assert job is not None
        job.status = status
        job.finished_at = CURRENT + timedelta(minutes=1)


def _add_job(
    case: ProjectionCase,
    *,
    kind: str = "dubbing.translate",
    status: str = "succeeded",
    created_at: datetime = BASE,
) -> str:
    job_id = new_id()
    with case.database.session() as session:
        session.add(
            Job(
                id=job_id,
                session_id=case.session_id,
                kind=kind,
                status=status,
                created_at=created_at,
                updated_at=created_at,
                started_at=created_at,
            )
        )
    return job_id


def _add_run(
    case: ProjectionCase,
    *,
    status: str,
    updated_at: datetime = CURRENT,
    job_id: str | None = None,
    artifact_id: str | None = None,
    kind: str = "translation",
    session_id: str | None = None,
    run_id: str | None = None,
) -> str:
    identifier = run_id or new_id()
    with case.database.session() as session:
        session.add(
            AgentRun(
                id=identifier,
                kind=kind,
                session_id=session_id or case.session_id,
                job_id=job_id or case.job_id,
                result_artifact_id=artifact_id,
                status=status,
                created_at=updated_at,
                updated_at=updated_at,
            )
        )
    return identifier


def _selected_artifact(case: ProjectionCase, *, metadata_run_id: str | None = None) -> str:
    artifact_id = new_id()
    timestamp = BASE + timedelta(hours=12)
    with case.database.session() as session:
        session.add(
            Artifact(
                id=artifact_id,
                session_id=case.session_id,
                role="translation",
                kind="srt",
                state="current",
                relative_path=f"artifacts/{artifact_id}.srt",
                metadata_json={"agent_run_id": metadata_run_id} if metadata_run_id else {},
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        session.flush()
        session.add(
            SessionStageSelection(
                session_id=case.session_id,
                stage_key="translate",
                artifact_id=artifact_id,
                revision=1,
                updated_at=timestamp,
            )
        )
    return artifact_id


def _seed_history(
    case: ProjectionCase,
    first: int,
    stop: int,
    *,
    shared_job: bool = False,
    artifact_id: str | None = None,
) -> None:
    jobs = []
    runs = []
    for index in range(first, stop):
        timestamp = BASE + timedelta(seconds=index + 1)
        job_id = case.job_id if shared_job else new_id()
        if not shared_job:
            jobs.append(
                Job(
                    id=job_id,
                    session_id=case.session_id,
                    kind="dubbing.translate",
                    status="succeeded",
                    created_at=timestamp,
                    updated_at=timestamp,
                )
            )
        runs.append(
            AgentRun(
                id=new_id(),
                session_id=case.session_id,
                kind="translation",
                job_id=job_id,
                status="completed",
                result_artifact_id=artifact_id,
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
    with case.database.session() as session:
        session.add_all(jobs)
        session.flush()
        session.add_all(runs)


def _assert_stage(
    measurement: Measurement, case: ProjectionCase, *, run_id: str, status: str, resumable: bool
) -> None:
    stage = _translation(measurement)
    assert stage["job_id"] == case.job_id
    assert stage["agent_run_id"] == run_id
    assert stage["status"] == status
    assert stage["resumable"] is resumable


def _assert_budget(small: Measurement, large: Measurement, agent_limit: int) -> None:
    assert len(small.counter.statements) == len(large.counter.statements)
    small_agents = small.counter.orm_loads["AgentRun"]
    large_agents = large.counter.orm_loads["AgentRun"]
    small_total = sum(small.counter.orm_loads.values())
    large_total = sum(large.counter.orm_loads.values())
    assert small_agents <= agent_limit and large_agents <= agent_limit, (
        f"AgentRun loads: {small_agents} -> {large_agents}; budget <= {agent_limit}; "
        f"total ORM loads: {small_total} -> {large_total}"
    )
    assert small_total == large_total


def test_agent_snapshot_history_does_not_scale(projection_case: ProjectionCase) -> None:
    case = projection_case
    current_id = _add_run(case, status="running")
    _seed_history(case, 0, 10)
    small = _measure(case)
    _seed_history(case, 10, 1000)
    large = _measure(case)
    assert small.payload == large.payload
    for result in (small, large):
        _assert_stage(result, case, run_id=current_id, status="running", resumable=False)
    _assert_budget(small, large, agent_limit=1)


def test_agent_snapshot_duplicate_job_candidates_are_bounded(
    projection_case: ProjectionCase,
) -> None:
    case = projection_case
    _set_job_status(case, "failed")
    current_id = _add_run(case, status="failed")
    _seed_history(case, 0, 10, shared_job=True)
    small = _measure(case)
    _seed_history(case, 10, 1000, shared_job=True)
    large = _measure(case)
    assert small.payload == large.payload
    for result in (small, large):
        _assert_stage(result, case, run_id=current_id, status="failed", resumable=True)
    _assert_budget(small, large, agent_limit=1)


def test_agent_snapshot_duplicate_artifact_candidates_are_bounded(
    projection_case: ProjectionCase,
) -> None:
    case = projection_case
    _set_job_status(case, "failed")
    _add_run(case, status="failed")
    artifact_id = _selected_artifact(case)
    artifact_job_id = _add_job(case, created_at=BASE + timedelta(hours=1))
    artifact_run_id = _add_run(
        case,
        status="completed",
        artifact_id=artifact_id,
        job_id=artifact_job_id,
        updated_at=CURRENT + timedelta(hours=1),
    )
    _seed_history(case, 0, 10, artifact_id=artifact_id)
    small = _measure(case)
    _seed_history(case, 10, 1000, artifact_id=artifact_id)
    large = _measure(case)
    assert small.payload == large.payload
    for result in (small, large):
        _assert_stage(result, case, run_id=artifact_run_id, status="failed", resumable=False)
    _assert_budget(small, large, agent_limit=2)


@pytest.mark.parametrize("newer_link", ["artifact", "job"])
def test_latest_link_wins_regardless_of_link_type(
    projection_case: ProjectionCase,
    newer_link: str,
) -> None:
    case = projection_case
    _set_job_status(case, "failed")
    artifact_id = _selected_artifact(case)
    artifact_job_id = _add_job(case)
    later = CURRENT + timedelta(hours=1)
    artifact_run_id = _add_run(
        case,
        status="completed",
        artifact_id=artifact_id,
        job_id=artifact_job_id,
        updated_at=later if newer_link == "artifact" else CURRENT,
    )
    job_run_id = _add_run(
        case, status="failed", updated_at=later if newer_link == "job" else CURRENT
    )
    _assert_stage(
        _measure(case),
        case,
        run_id=artifact_run_id if newer_link == "artifact" else job_run_id,
        status="failed",
        resumable=newer_link == "job",
    )


@pytest.mark.parametrize("excluded", ["wrong-kind", "foreign-session"])
def test_agent_snapshot_combines_kind_and_same_session(
    projection_case: ProjectionCase,
    excluded: str,
) -> None:
    case = projection_case
    _set_job_status(case, "failed")
    expected_id = _add_run(case, status="failed")
    other_session = SessionService(case.database).create("Foreign agent", workflow_kind="voiceover")
    _add_run(
        case,
        status="failed",
        updated_at=CURRENT + timedelta(hours=1),
        kind="correction" if excluded == "wrong-kind" else "translation",
        session_id=other_session.id if excluded == "foreign-session" else case.session_id,
    )
    _assert_stage(_measure(case), case, run_id=expected_id, status="failed", resumable=True)


def test_agent_snapshot_metadata_fallback_is_not_resumable(projection_case: ProjectionCase) -> None:
    case = projection_case
    _set_job_status(case, "failed")
    _selected_artifact(case, metadata_run_id=METADATA_RUN_ID)
    _assert_stage(_measure(case), case, run_id=METADATA_RUN_ID, status="failed", resumable=False)


def test_agent_snapshot_completed_only_history_skips_mutable_status_reads(
    projection_case: ProjectionCase,
) -> None:
    case = projection_case
    _set_job_status(case, "succeeded")
    _selected_artifact(case, metadata_run_id=METADATA_RUN_ID)
    _seed_history(case, 0, 10)
    result = _measure(case)
    _assert_stage(result, case, run_id=METADATA_RUN_ID, status="completed", resumable=False)
    assert result.counter.orm_loads["AgentRun"] == 0
    assert not any("agent_runs" in statement.lower() for statement in result.counter.statements)


def test_agent_snapshot_selected_completed_run_is_kept_during_continue(
    projection_case: ProjectionCase,
) -> None:
    case = projection_case
    _set_job_status(case, "succeeded")
    artifact_id = _selected_artifact(case)
    expected_id = _add_run(case, status="completed", artifact_id=artifact_id)
    _add_job(
        case, kind="workflow.continue", status="running", created_at=CURRENT + timedelta(hours=1)
    )
    _assert_stage(_measure(case), case, run_id=expected_id, status="completed", resumable=False)


@pytest.mark.parametrize("insertion_order", ["artifact-first", "job-first"])
def test_agent_snapshot_tied_links_return_a_valid_latest_match(
    projection_case: ProjectionCase,
    insertion_order: str,
) -> None:
    case = projection_case
    _set_job_status(case, "failed")
    artifact_id = _selected_artifact(case)
    artifact_job_id = _add_job(case)
    order = ("artifact", "job") if insertion_order == "artifact-first" else ("job", "artifact")
    for link in order:
        _add_run(
            case,
            run_id=ARTIFACT_RUN_ID if link == "artifact" else JOB_RUN_ID,
            status="completed" if link == "artifact" else "failed",
            artifact_id=artifact_id if link == "artifact" else None,
            job_id=artifact_job_id if link == "artifact" else case.job_id,
            updated_at=CURRENT + timedelta(hours=1),
        )
    result = _measure(case)
    stage = _translation(result)
    assert stage["agent_run_id"] in {ARTIFACT_RUN_ID, JOB_RUN_ID}
    assert stage["job_id"] == case.job_id
    assert stage["status"] == "failed"
    assert stage["resumable"] is (stage["agent_run_id"] == JOB_RUN_ID)
