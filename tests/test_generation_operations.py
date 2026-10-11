"""Focused backend tests for generation operation linkage and audio coverage."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import event

from pandrator.runtime import DataPaths
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.generation_audio_versions import audio_version_take_ids
from pandrator.web.generation_operations import (
    get_generation_operation,
    get_generation_run_coverage,
)
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Base,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    SessionRecord,
)
from pandrator.web.work import WorkService


@pytest.fixture
def rig(tmp_path):
    paths = DataPaths(tmp_path).ensure()
    database = Database(paths.database)
    Base.metadata.create_all(database.engine)
    redactor = SecretRedactor(database, paths)
    work = WorkService(JobQueue(database, redactor), redactor)
    with database.session() as session:
        record = SessionRecord(name="Generation operation test")
        session.add(record)
        session.flush()
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(
            plan_id=plan.id,
            revision_number=1,
            content_hash="generation-operation-plan",
        )
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        segments = [
            GenerationSegment(
                plan_revision_id=revision.id,
                ordinal=index,
                text=f"Private source text {index}",
            )
            for index in range(5)
        ]
        session.add_all(segments)
        session.flush()
        identity = SimpleNamespace(
            session_id=record.id,
            plan_id=plan.id,
            revision_id=revision.id,
            segment_ids=[item.id for item in segments],
        )

    value = SimpleNamespace(
        paths=paths,
        database=database,
        work=work,
        **vars(identity),
    )

    def create_job(
        *,
        kind: str,
        status: str = "queued",
        session_id: str | None = value.session_id,
        payload: dict | None = None,
        result: dict | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> str:
        with database.session() as session:
            job = Job(
                kind=kind,
                status=status,
                session_id=session_id,
                payload_json=payload or {},
                result_json=result,
                error_code=error_code,
                error_message=error_message,
            )
            session.add(job)
            session.flush()
            return job.id

    def create_run(
        *,
        sequence: int = 1,
        session_id: str = value.session_id,
        revision_id: str = value.revision_id,
        job_id: str | None = None,
        operation: str = "generate",
        status: str = "completed",
        audio_version_json: dict | None = None,
    ) -> str:
        with database.session() as session:
            run = GenerationRun(
                session_id=session_id,
                plan_revision_id=revision_id,
                sequence_number=sequence,
                job_id=job_id,
                operation=operation,
                status=status,
                audio_version_json=audio_version_json,
            )
            session.add(run)
            session.flush()
            return run.id

    def add_take(
        segment_id: str,
        run_id: str | None,
        *,
        artifact_state: str | None = "current",
        file_exists: bool = True,
        status: str = "completed",
        relative_path: str | None = None,
        artifact_id: str | None = None,
        parent_take_id: str | None = None,
        session_id: str = value.session_id,
    ) -> tuple[str, str | None]:
        if artifact_state is not None and artifact_id is None:
            relative_path = relative_path or (f"artifacts/{segment_id}-{run_id}-{uuid4().hex}.wav")
            if file_exists:
                artifact_path = paths.managed_path(relative_path)
                artifact_path.parent.mkdir(parents=True, exist_ok=True)
                artifact_path.write_bytes(b"audio")
            with database.session() as session:
                artifact = Artifact(
                    session_id=session_id,
                    kind="audio",
                    relative_path=relative_path,
                    state=artifact_state,
                )
                session.add(artifact)
                session.flush()
                artifact_id = artifact.id
        with database.session() as session:
            take = AudioTake(
                generation_segment_id=segment_id,
                generation_run_id=run_id,
                artifact_id=artifact_id,
                parent_take_id=parent_take_id,
                status=status,
            )
            session.add(take)
            session.flush()
            return take.id, artifact_id

    value.create_job = create_job
    value.create_run = create_run
    value.add_take = add_take
    try:
        yield value
    finally:
        database.dispose()


def link_admission(
    rig, *, root_status="succeeded", child_status="queued", child_kind="generation.run"
):
    child_id = rig.create_job(
        kind=child_kind,
        status=child_status,
        payload={},
    )
    run_id = rig.create_run(sequence=1, job_id=child_id, status="running")
    with rig.database.session() as session:
        child = session.get(Job, child_id)
        child.payload_json = {"generation_run_id": run_id}
        root = session.get(Job, rig.root_id)
        root.status = root_status
        root.result_json = {"job_id": child_id, "generation_run_id": run_id}
    return run_id, child_id


def test_admission_polls_same_root_through_preparation_and_synthesis(rig):
    rig.root_id = rig.create_job(kind="generation.admit", status="queued")
    initial = get_generation_operation(rig.work, rig.root_id)
    assert initial["id"] == rig.root_id
    assert initial["phase"] == "preparation"
    assert initial["state"] == "queued"
    assert initial["preparation_work_id"] == rig.root_id
    assert initial["synthesis_work_id"] is None

    run_id, child_id = link_admission(rig, child_status="queued")
    queued = get_generation_operation(rig.work, rig.root_id)
    assert queued["id"] == rig.root_id
    assert queued["phase"] == "synthesis"
    assert queued["state"] == "queued"
    assert queued["generation_run_id"] == run_id
    assert queued["synthesis_work_id"] == child_id
    assert queued["linkage_pending"] is False

    with rig.database.session() as session:
        session.get(Job, child_id).status = "running"
    running = get_generation_operation(rig.work, rig.root_id)
    assert running["id"] == rig.root_id
    assert running["phase"] == "synthesis"
    assert running["state"] == "running"

    with rig.database.session() as session:
        session.get(Job, child_id).status = "succeeded"
    terminal = get_generation_operation(rig.work, rig.root_id)
    assert terminal["id"] == rig.root_id
    assert terminal["phase"] == "complete"
    assert terminal["state"] == "succeeded"
    assert terminal["coverage"]["included_count"] == 5
    assert terminal["coverage"]["export_ready"] is False
    assert set(terminal["work"]) == {
        "id",
        "kind",
        "state",
        "progress",
        "detail",
        "error",
        "poll_after_ms",
    }
    assert "Private source text" not in repr(terminal)
    assert not any(
        key in terminal
        for key in ("payload_json", "settings_snapshot_json", "generation_selection_guards")
    )


def test_root_success_without_valid_linkage_waits_and_never_reports_success(rig):
    rig.root_id = rig.create_job(
        kind="generation.admit",
        status="succeeded",
        result={"job_id": "untrusted-child", "generation_run_id": "untrusted-run"},
    )
    result = get_generation_operation(rig.work, rig.root_id)
    assert result["id"] == rig.root_id
    assert result["phase"] == "preparation"
    assert result["state"] == "waiting"
    assert result["linkage_pending"] is True
    assert result["synthesis_work_id"] is None
    assert result["generation_run_id"] is None
    assert result["linkage_diagnostic"]


def test_mismatched_child_payload_never_becomes_the_current_phase(rig):
    rig.root_id = rig.create_job(
        kind="generation.admit",
        status="succeeded",
        result={"job_id": "pending", "generation_run_id": "pending"},
    )
    child_id = rig.create_job(
        kind="generation.run",
        status="running",
        payload={"generation_run_id": "different-run"},
    )
    run_id = rig.create_run(job_id=child_id, status="running")
    with rig.database.session() as session:
        session.get(Job, rig.root_id).result_json = {
            "job_id": child_id,
            "generation_run_id": run_id,
        }
    result = get_generation_operation(rig.work, rig.root_id)
    assert result["state"] == "waiting"
    assert result["linkage_pending"] is True
    assert result["synthesis_work_id"] is None
    assert child_id not in repr(result)


@pytest.mark.parametrize("status", ["failed", "cancelled"])
def test_failed_or_cancelled_admission_without_child_is_terminal(rig, status):
    rig.root_id = rig.create_job(kind="generation.admit", status=status)
    result = get_generation_operation(rig.work, rig.root_id)
    assert result["state"] == status
    assert result["phase"] == "complete"
    assert result["linkage_pending"] is False
    assert result["synthesis_work_id"] is None


def test_cross_session_child_and_missing_child_are_never_exposed(rig):
    rig.root_id = rig.create_job(
        kind="generation.admit",
        status="succeeded",
        result={"job_id": "pending", "generation_run_id": "pending"},
    )
    with rig.database.session() as session:
        foreign_session = SessionRecord(name="Other session")
        session.add(foreign_session)
        session.flush()
        foreign_session_id = foreign_session.id
        foreign_plan = GenerationPlan(session_id=foreign_session.id)
        session.add(foreign_plan)
        session.flush()
        foreign_revision = GenerationPlanRevision(
            plan_id=foreign_plan.id,
            revision_number=1,
            content_hash="foreign-plan",
        )
        session.add(foreign_revision)
        session.flush()
    child_id = rig.create_job(
        kind="generation.run",
        session_id=foreign_session_id,
        payload={"generation_run_id": "pending"},
    )
    run_id = rig.create_run(job_id=child_id)
    with rig.database.session() as session:
        run = session.get(GenerationRun, run_id)
        run.session_id = rig.session_id
        run.plan_revision_id = rig.revision_id
        session.get(Job, child_id).payload_json = {"generation_run_id": run_id}
        session.get(Job, rig.root_id).result_json = {
            "job_id": child_id,
            "generation_run_id": run_id,
        }
    result = get_generation_operation(rig.work, rig.root_id)
    assert result["state"] == "waiting"
    assert result["linkage_pending"] is True
    assert result["generation_run_id"] is None
    assert result["synthesis_work_id"] is None
    assert child_id not in repr(result)

    with rig.database.session() as session:
        session.get(Job, rig.root_id).result_json = {
            "job_id": child_id,
            "generation_run_id": "missing-run",
        }
    missing = get_generation_operation(rig.work, rig.root_id)
    assert missing["linkage_pending"] is True
    assert missing["synthesis_work_id"] is None
    assert child_id not in repr(missing)


def test_failed_preparation_with_valid_child_reports_child_state_and_safe_error(rig):
    rig.root_id = rig.create_job(
        kind="generation.admit",
        status="queued",
        error_code="preparation_retry",
        error_message="Preparation had a retryable issue.",
    )
    run_id, child_id = link_admission(rig, root_status="failed", child_status="running")
    with rig.database.session() as session:
        root = session.get(Job, rig.root_id)
        root.error_code = "preparation_failed"
        root.error_message = "Preparation stopped after the run was linked."
    result = get_generation_operation(rig.work, rig.root_id)
    assert result["phase"] == "synthesis"
    assert result["state"] == "running"
    assert result["generation_run_id"] == run_id
    assert result["synthesis_work_id"] == child_id
    assert result["preparation_error"] == {
        "code": "preparation_failed",
        "message": "Preparation stopped after the run was linked.",
    }


def test_cancellation_hint_uses_either_root_or_validated_child(rig):
    rig.root_id = rig.create_job(kind="generation.admit", status="succeeded")
    _, child_id = link_admission(rig, child_status="running")
    child_running = get_generation_operation(rig.work, rig.root_id)
    assert child_running["cancellable"] is True
    assert child_running["work"]["id"] == child_id

    with rig.database.session() as session:
        session.get(Job, rig.root_id).status = "running"
        session.get(Job, child_id).status = "cancelled"
    root_cancellable = get_generation_operation(rig.work, rig.root_id)
    assert root_cancellable["state"] == "cancelled"
    assert root_cancellable["cancellable"] is True


def test_direct_generation_run_tracks_its_own_job(rig):
    child_id = rig.create_job(kind="generation.run", status="running")
    run_id = rig.create_run(job_id=child_id, status="running")
    with rig.database.session() as session:
        session.get(Job, child_id).payload_json = {"generation_run_id": run_id}
    result = get_generation_operation(rig.work, child_id, include_coverage=False)
    assert result["id"] == child_id
    assert result["phase"] == "synthesis"
    assert result["state"] == "running"
    assert result["preparation_work_id"] is None
    assert result["synthesis_work_id"] == child_id
    assert result["generation_run_id"] == run_id
    assert result["coverage"] is None


def test_reading_operation_does_not_duplicate_jobs_or_runs(rig):
    rig.root_id = rig.create_job(kind="generation.admit", status="queued")
    run_id, child_id = link_admission(rig)
    with rig.database.engine.connect() as connection:
        before = connection.exec_driver_sql(
            "SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM generation_runs)"
        ).one()
    first = get_generation_operation(rig.work, rig.root_id)
    second = get_generation_operation(rig.work, rig.root_id)
    with rig.database.snapshot_session() as session:
        after = (
            session.connection()
            .exec_driver_sql(
                "SELECT (SELECT COUNT(*) FROM jobs), (SELECT COUNT(*) FROM generation_runs)"
            )
            .one()
        )
    assert first["id"] == second["id"] == rig.root_id
    assert first["generation_run_id"] == second["generation_run_id"] == run_id
    assert first["synthesis_work_id"] == second["synthesis_work_id"] == child_id
    assert tuple(before) == tuple(after)


def test_successful_direct_job_without_run_link_is_still_waiting(rig):
    job_id = rig.create_job(kind="generation.run", status="succeeded")
    result = get_generation_operation(rig.work, job_id)
    assert result["phase"] == "synthesis"
    assert result["state"] == "waiting"
    assert result["linkage_pending"] is True
    assert result["coverage"] is None


def test_coverage_counts_selected_run_inherited_missing_and_unavailable_audio(rig):
    selected_run_id = rig.create_run(sequence=2)
    previous_run_id = rig.create_run(sequence=1)
    later_run_id = rig.create_run(sequence=3)
    rig.add_take(rig.segment_ids[0], selected_run_id)
    rig.add_take(rig.segment_ids[1], previous_run_id, status="stale")
    rig.add_take(rig.segment_ids[2], selected_run_id, artifact_state=None)
    rig.add_take(
        rig.segment_ids[3],
        selected_run_id,
        file_exists=False,
        relative_path="artifacts/deleted-audio.wav",
    )
    rig.add_take(rig.segment_ids[4], later_run_id)
    rig.add_take(
        rig.segment_ids[4],
        selected_run_id,
        artifact_state="deleted",
        file_exists=False,
    )
    with rig.database.session() as session:
        # This newer, active take must not be consulted for the historical run.
        session.add(
            AudioTake(
                generation_segment_id=rig.segment_ids[0],
                generation_run_id=later_run_id,
                status="completed",
                is_active=True,
            )
        )

    result = get_generation_run_coverage(rig.database, selected_run_id)
    assert result == {
        "run_id": selected_run_id,
        "plan_revision_id": rig.revision_id,
        "audio_version_side": "after",
        "included_count": 5,
        "effective_audio_count": 5,
        "run_audio_count": 4,
        "inherited_audio_count": 1,
        "generated_audio_count": 4,
        "missing_audio_count": 0,
        "unavailable_artifact_count": 2,
        "missing_file_count": 1,
        "missing_segment_ordinals": [],
        "export_ready": False,
    }


def test_coverage_keeps_historical_selection_and_ready_state_independent_of_active_take(rig):
    run_id = rig.create_run(sequence=2)
    previous_id = rig.create_run(sequence=1)
    for segment_id in rig.segment_ids[:2]:
        rig.add_take(segment_id, previous_id)
    rig.add_take(rig.segment_ids[0], run_id)
    later_id = rig.create_run(sequence=3)
    rig.add_take(
        rig.segment_ids[1],
        later_id,
        artifact_state="deleted",
        file_exists=False,
    )
    with rig.database.session() as session:
        session.get(GenerationSegment, rig.segment_ids[2]).removed = True
        session.get(GenerationSegment, rig.segment_ids[3]).removed = True
        session.get(GenerationSegment, rig.segment_ids[4]).removed = True
        session.add(
            AudioTake(
                generation_segment_id=rig.segment_ids[0],
                generation_run_id=later_id,
                status="completed",
                is_active=True,
            )
        )
    result = get_generation_run_coverage(rig.database, run_id)
    assert result["included_count"] == 2
    assert result["effective_audio_count"] == 2
    assert result["run_audio_count"] == 1
    assert result["inherited_audio_count"] == 1
    assert result["generated_audio_count"] == 1
    assert result["unavailable_artifact_count"] == 0
    assert result["missing_file_count"] == 0
    assert result["export_ready"] is True


def test_coverage_counts_run_owned_shared_artifact_as_inherited_and_new_artifact_as_generated(
    rig,
):
    run_id = rig.create_run(sequence=2)
    previous_id = rig.create_run(sequence=1)
    artifact_ids = []
    for index in range(3):
        relative_path = f"artifacts/lineage-{index}.wav"
        artifact_path = rig.paths.managed_path(relative_path)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        artifact_path.write_bytes(b"audio")
        with rig.database.session() as session:
            artifact = Artifact(
                session_id=rig.session_id,
                kind="audio",
                relative_path=relative_path,
                state="current",
            )
            session.add(artifact)
            session.flush()
            artifact_ids.append(artifact.id)

    with rig.database.session() as session:
        for segment_id in rig.segment_ids[2:]:
            session.get(GenerationSegment, segment_id).removed = True

        shared_parent = AudioTake(
            generation_segment_id=rig.segment_ids[0],
            generation_run_id=previous_id,
            artifact_id=artifact_ids[0],
            status="completed",
        )
        new_parent = AudioTake(
            generation_segment_id=rig.segment_ids[1],
            generation_run_id=previous_id,
            artifact_id=artifact_ids[1],
            status="completed",
        )
        session.add_all([shared_parent, new_parent])
        session.flush()
        session.add_all(
            [
                AudioTake(
                    generation_segment_id=rig.segment_ids[0],
                    generation_run_id=run_id,
                    artifact_id=artifact_ids[0],
                    parent_take_id=shared_parent.id,
                    status="completed",
                ),
                AudioTake(
                    generation_segment_id=rig.segment_ids[1],
                    generation_run_id=run_id,
                    artifact_id=artifact_ids[2],
                    parent_take_id=new_parent.id,
                    status="completed",
                ),
            ]
        )

    result = get_generation_run_coverage(rig.database, run_id)
    assert result["effective_audio_count"] == 2
    assert result["run_audio_count"] == 2
    assert result["inherited_audio_count"] == 1
    assert result["generated_audio_count"] == 1
    assert result["export_ready"] is True


def test_recast_coverage_uses_frozen_before_and_after_take_ids(rig):
    version_run_id = rig.create_run(sequence=2)
    previous_id = rig.create_run(sequence=1)
    before_ids = {}
    after_ids = {}
    for segment_id in rig.segment_ids[:2]:
        parent_take_id, artifact_id = rig.add_take(segment_id, previous_id)
        before_ids[segment_id] = rig.add_take(
            segment_id,
            version_run_id,
            artifact_id=artifact_id,
            parent_take_id=parent_take_id,
        )[0]
        after_ids[segment_id] = rig.add_take(
            segment_id,
            version_run_id,
            artifact_id=artifact_id,
            parent_take_id=parent_take_id,
        )[0]
    manifest = {
        "version": 1,
        "kind": "recast",
        "target_segment_ids": rig.segment_ids[:1],
        "before_take_ids": before_ids,
        "after_take_ids": after_ids,
        "generated_take_ids": {},
        "applied_take_ids": {},
        "applied_take_revisions": {},
        "undone": False,
    }
    with rig.database.session() as session:
        run = session.get(GenerationRun, version_run_id)
        run.audio_version_json = manifest
        session.get(GenerationSegment, rig.segment_ids[2]).removed = True
        session.get(GenerationSegment, rig.segment_ids[3]).removed = True
        session.get(GenerationSegment, rig.segment_ids[4]).removed = True
        # A later active take cannot replace either pinned side.
        session.add(
            AudioTake(
                generation_segment_id=rig.segment_ids[0],
                generation_run_id=previous_id,
                status="completed",
                is_active=True,
            )
        )
    with rig.database.snapshot_session() as session:
        assert (
            audio_version_take_ids(session.get(GenerationRun, version_run_id), "before")
            == before_ids
        )
    before = get_generation_run_coverage(rig.database, version_run_id, audio_version_side="before")
    after = get_generation_run_coverage(rig.database, version_run_id, audio_version_side="after")
    assert before["audio_version_side"] == "before"
    assert after["audio_version_side"] == "after"
    assert before["effective_audio_count"] == after["effective_audio_count"] == 2
    assert before["run_audio_count"] == after["run_audio_count"] == 2
    assert before["inherited_audio_count"] == after["inherited_audio_count"] == 2
    assert before["generated_audio_count"] == after["generated_audio_count"] == 0
    assert before["export_ready"] is True
    assert after["export_ready"] is True


def test_coverage_rejects_audio_version_from_another_session(rig):
    with rig.database.session() as session:
        foreign = SessionRecord(name="Other plan")
        session.add(foreign)
        session.flush()
        foreign_plan = GenerationPlan(session_id=foreign.id)
        session.add(foreign_plan)
        session.flush()
        foreign_revision = GenerationPlanRevision(
            plan_id=foreign_plan.id,
            revision_number=1,
            content_hash="foreign-revision",
        )
        session.add(foreign_revision)
        session.flush()
        run = GenerationRun(
            session_id=rig.session_id,
            plan_revision_id=foreign_revision.id,
            sequence_number=1,
            audio_version_json={
                "version": 1,
                "kind": "recast",
                "before_take_ids": {},
                "after_take_ids": {},
            },
        )
        session.add(run)
        session.flush()
        run_id = run.id
    with pytest.raises(ValueError, match="does not belong to this session"):
        get_generation_run_coverage(rig.database, run_id)


def test_coverage_rejects_artifact_paths_outside_the_data_root(rig):
    run_id = rig.create_run(sequence=1)
    rig.add_take(
        rig.segment_ids[0],
        run_id,
        file_exists=False,
        relative_path="../outside-audio.wav",
    )
    result = get_generation_run_coverage(rig.database, run_id)
    assert result["unavailable_artifact_count"] == 1
    assert result["missing_file_count"] == 0
    assert result["export_ready"] is False
    assert "outside-audio" not in repr(result)


def test_selected_take_lookups_are_batched_at_500(rig):
    run_id = rig.create_run(sequence=1)
    previous_run_id = rig.create_run(sequence=0)
    artifact_relative_path = "artifacts/batch-audio.wav"
    artifact_path = rig.paths.managed_path(artifact_relative_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_bytes(b"audio")
    with rig.database.session() as session:
        artifact = Artifact(
            session_id=rig.session_id,
            kind="audio",
            relative_path=artifact_relative_path,
            state="current",
        )
        session.add(artifact)
        session.flush()
        revision_id = rig.revision_id
        created_segments = []
        for index in range(501):
            segment = GenerationSegment(
                plan_revision_id=revision_id,
                ordinal=index + 10,
                text=f"Batch test {index}",
            )
            session.add(segment)
            created_segments.append(segment)
        session.flush()
        source_takes = [
            AudioTake(
                generation_segment_id=segment.id,
                generation_run_id=previous_run_id,
                artifact_id=artifact.id,
                status="completed",
            )
            for segment in created_segments
        ]
        session.add_all(source_takes)
        session.flush()
        session.add_all(
            [
                AudioTake(
                    generation_segment_id=segment.id,
                    generation_run_id=run_id,
                    artifact_id=artifact.id,
                    parent_take_id=source_take.id,
                    status="completed",
                )
                for segment, source_take in zip(
                    created_segments,
                    source_takes,
                    strict=True,
                )
            ]
        )
        for index in range(25):
            session.add(
                GenerationSegment(
                    plan_revision_id=revision_id,
                    ordinal=1_000 + index,
                    text=f"Unselected batch test {index}",
                )
            )
    batch_sizes = []
    parent_batch_sizes = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        if "audio_takes.id IN" in statement:
            batch_sizes.append(len(parameters))
        if "(audio_takes.id, audio_takes.generation_segment_id) IN" in statement:
            parent_batch_sizes.append(len(parameters) // 2)

    event.listen(rig.database.engine, "before_cursor_execute", capture)
    try:
        result = get_generation_run_coverage(rig.database, run_id)
    finally:
        event.remove(rig.database.engine, "before_cursor_execute", capture)
    assert result["effective_audio_count"] == 501
    assert result["run_audio_count"] == 501
    assert result["inherited_audio_count"] == 501
    assert result["generated_audio_count"] == 0
    assert result["missing_audio_count"] == len(rig.segment_ids) + 25
    assert result["missing_segment_ordinals"] == [
        *range(len(rig.segment_ids)),
        *range(1_000, 1_015),
    ]
    assert batch_sizes == [500, 1]
    assert parent_batch_sizes == [500, 1]


def test_unknown_operation_kind_and_unknown_run_are_not_accepted(rig):
    job_id = rig.create_job(kind="other.job")
    with pytest.raises(KeyError):
        get_generation_operation(rig.work, job_id)
    with pytest.raises(KeyError):
        get_generation_run_coverage(rig.database, "missing-run")
