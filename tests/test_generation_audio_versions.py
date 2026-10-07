"""Disposable backend evidence for exact recast manifests and selection undo."""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import inspect, select

from pandrator.web.database import Database, upgrade_database
from pandrator.web.generation_audio_versions import (
    audio_version_summary,
    audio_version_take_ids,
    audio_version_undo_updates,
    initialize_audio_version,
    mark_audio_version_undone,
    preview_audio_version_undo,
    record_audio_version_take,
)
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.generation_edit_audio import selection_guard
from pandrator.web.models import (
    Artifact,
    AudioTake,
    Base,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
)
from pandrator.web.output_assembly_lifecycle import mark_output_assemblies_stale
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.workflow_generation_finalization import finalize_generation_run
from pandrator.web.workflow_generation_publication import (
    GenerationTakePublication,
    publish_generation_take,
)
from pandrator.web.workspace import GenerationService


@pytest.fixture
def rig(tmp_path):
    path = tmp_path / "test.sqlite3"
    database = Database(path)
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        record = SessionRecord(name="Recast fixture")
        session.add(record)
        session.flush()
        plan = GenerationPlan(session_id=record.id)
        session.add(plan)
        session.flush()
        revision = GenerationPlanRevision(plan_id=plan.id, revision_number=1, content_hash="plan")
        session.add(revision)
        session.flush()
        plan.active_revision_id = revision.id
        root = GenerationRun(session_id=record.id, plan_revision_id=revision.id, status="completed")
        job = Job(kind="generation", session_id=record.id, status="running")
        session.add_all([root, job])
        session.flush()
        run = GenerationRun(
            session_id=record.id, plan_revision_id=revision.id, sequence_number=2,
            operation="regenerate", status="running", job_id=job.id,
            source_generation_run_id=root.id,
        )
        segments = [GenerationSegment(plan_revision_id=revision.id, ordinal=i, text=str(i),
                                      voice="Original voice", speaker="Original speaker") for i in range(2)]
        session.add_all([run, *segments])
        session.flush()
        takes = []
        for i, segment in enumerate(segments):
            audio = tmp_path / f"baseline-{i}.wav"
            audio.write_bytes(b"retained audio bytes")
            artifact = Artifact(session_id=record.id, kind="audio", relative_path=audio.name)
            session.add(artifact)
            session.flush()
            take = AudioTake(generation_segment_id=segment.id, generation_run_id=root.id,
                             artifact_id=artifact.id, status="completed", is_active=True,
                             kind="tts", settings_hash="original", duration_ms=1234)
            session.add(take)
            takes.append(take)
        session.flush()
        ids = SimpleNamespace(database=database, session_id=record.id, run_id=run.id,
                              root_id=root.id, job_id=job.id, plan_id=plan.id,
                              revision_id=revision.id, segments=[item.id for item in segments],
                              takes=[item.id for item in takes], directory=tmp_path)
    yield ids
    database.dispose()


def _initialize(session, rig):
    run = session.get(GenerationRun, rig.run_id)
    initialize_audio_version(session, run, dict(zip(rig.segments, rig.takes, strict=True)),
                             rig.segments, [{"key": "speaker:a", "label": "Speaker A"}])
    return run


def _produced(session, rig, segment_id=None, applied=True):
    segment_id = segment_id or rig.segments[0]
    artifact = Artifact(session_id=rig.session_id, kind="audio", relative_path=f"generated-{segment_id}.wav")
    session.add(artifact)
    session.flush()
    if applied:
        for old in session.scalars(select(AudioTake).where(AudioTake.generation_segment_id == segment_id)):
            old.is_active = False
            old.revision += 1
    take = AudioTake(generation_segment_id=segment_id, generation_run_id=rig.run_id,
                     artifact_id=artifact.id, status="completed", is_active=applied)
    session.add(take)
    session.flush()
    record_audio_version_take(session, rig.run_id, take, applied)
    return take


def _finish(session, rig):
    session.get(GenerationRun, rig.run_id).status = "partial"
    session.get(Job, rig.job_id).status = "succeeded"


def test_migration_adds_nullable_manifest_and_legacy_read_is_none(rig):
    column = next(item for item in inspect(rig.database.engine).get_columns("generation_runs")
                  if item["name"] == "audio_version_json")
    assert column["nullable"] is True
    with rig.database.session() as session:
        assert audio_version_take_ids(session.get(GenerationRun, rig.root_id)) is None


def test_migration_upgrades_fresh_and_existing_0052_database(tmp_path):
    path = tmp_path / "migrated.sqlite3"
    upgrade_database(path)
    database = Database(path)
    try:
        with database.engine.begin() as connection:
            connection.exec_driver_sql("ALTER TABLE generation_runs DROP COLUMN audio_version_json")
            connection.exec_driver_sql(
                "UPDATE alembic_version SET version_num = '0052_translation_project_operations'"
            )
        upgrade_database(path)
        columns = {item["name"] for item in inspect(database.engine).get_columns("generation_runs")}
        assert "audio_version_json" in columns
    finally:
        database.dispose()


def test_exact_baseline_clones_preserve_manual_stale_take_without_file_copy(rig):
    files_before = {item.name: item.read_bytes() for item in rig.directory.glob("*.wav")}
    with rig.database.session() as session:
        source = session.get(AudioTake, rig.takes[0])
        source.status = "stale"
        run = _initialize(session, rig)
        before = audio_version_take_ids(run, "before")
        assert before == audio_version_take_ids(run)
        clone = session.get(AudioTake, before[rig.segments[0]])
        assert clone.id != source.id and clone.parent_take_id == source.id
        assert clone.generation_run_id == run.id and clone.is_active is False
        assert (clone.status, clone.artifact_id, clone.kind, clone.duration_ms, clone.settings_hash) == (
            "stale", source.artifact_id, source.kind, 1234, "original")
        assert source.is_active is True
        session.delete(session.get(GenerationRun, rig.root_id))
        session.flush()
        assert session.get(AudioTake, clone.id).artifact_id == source.artifact_id
    assert files_before == {item.name: item.read_bytes() for item in rig.directory.glob("*.wav")}


def test_generated_after_differs_from_before_even_when_manual_selection_wins(rig):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        before = audio_version_take_ids(run, "before")
        produced = _produced(session, rig, applied=False)
        assert audio_version_take_ids(run)[rig.segments[0]] == produced.id
        assert audio_version_take_ids(run, "before") == before
        assert run.audio_version_json["applied_take_ids"] == {}
        assert session.get(AudioTake, rig.takes[0]).is_active is True
        frozen = deepcopy(run.audio_version_json)
        foreign = GenerationRun(session_id=rig.session_id, plan_revision_id=rig.revision_id,
                                sequence_number=3, output_generation_run_id=rig.run_id)
        session.add(foreign)
        session.flush()
        record_audio_version_take(session, foreign.id, produced, True)
        _finish(session, rig)
        record_audio_version_take(session, rig.run_id, produced, True)
        assert run.audio_version_json == frozen


def test_explicit_missing_target_has_no_invented_baseline_and_undo_clears_selection(rig):
    with rig.database.session() as session:
        segment_id = rig.segments[1]
        session.get(AudioTake, rig.takes[1]).is_active = False
        run = session.get(GenerationRun, rig.run_id)
        initialize_audio_version(session, run, {rig.segments[0]: rig.takes[0]}, rig.segments,
                                 [{"key": "speaker:a", "label": "Speaker A"}])
        assert segment_id not in audio_version_take_ids(run, "before")
        _produced(session, rig, segment_id)
        _finish(session, rig)
        preview = preview_audio_version_undo(session, run.id)
        assert preview["blocked_reason"] is None and preview["restore_count"] == 1
        segment = session.get(GenerationSegment, segment_id)
        assert audio_version_undo_updates(session, run.id, preview["selection_hash"]) == [
            (segment_id, None, segment.revision),
        ]


def test_failure_before_success_records_later_success_while_job_is_active(rig):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        run.status = "failed"
        produced = _produced(session, rig, rig.segments[1])
        assert audio_version_take_ids(run)[rig.segments[1]] == produced.id
        assert audio_version_summary(run)["generated_count"] == 1
        assert audio_version_summary(run)["remaining_count"] == 1
    actual, remaining = finalize_generation_run(
        rig.database, run_id=rig.run_id, output_run_id=rig.run_id,
        plan_revision_id=rig.revision_id, operation="regenerate", repair_requested=True,
    )
    assert (actual, remaining) == ("partial", 1)


@pytest.mark.parametrize("status,expected", [("running", "partial"), ("failed", "failed"), ("canceled", "canceled")])
def test_fallback_rows_do_not_count_as_success_or_hide_failed_cancel_status(rig, status, expected):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        run.status = status
    actual, remaining = finalize_generation_run(
        rig.database, run_id=rig.run_id, output_run_id=rig.run_id,
        plan_revision_id=rig.revision_id, operation="regenerate", repair_requested=True,
    )
    assert (actual, remaining) == (expected, 2)
    with rig.database.session() as session:
        assert session.get(GenerationRun, rig.run_id).status == expected


def test_undo_restores_only_applied_audio_and_preserves_cast_and_frozen_maps(rig):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        _produced(session, rig)
        _finish(session, rig)
        maps_before = deepcopy(run.audio_version_json)
        segment = session.get(GenerationSegment, rig.segments[0])
        segment.voice = "New voice kept by undo"
        preview = preview_audio_version_undo(session, run.id)
        assert preview["blocked_reason"] is None and preview["restore_count"] == 1
        service = GenerationService(rig.database, None, None)
        for segment_id, take_id, revision in audio_version_undo_updates(session, run.id, preview["selection_hash"]):
            service.select_take_in_session(session, segment_id, take_id, revision)
        mark_audio_version_undone(session, run.id)
        assert segment.voice == "New voice kept by undo" and segment.speaker == "Original speaker"
        assert run.audio_version_json["before_take_ids"] == maps_before["before_take_ids"]
        assert run.audio_version_json["after_take_ids"] == maps_before["after_take_ids"]
        assert preview_audio_version_undo(session, run.id)["restore_count"] == 0
        assert audio_version_summary(run)["undone"] is True


def test_undo_blocks_manual_reselection_even_with_identical_artifact(rig):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        produced = _produced(session, rig)
        _finish(session, rig)
        preview = preview_audio_version_undo(session, run.id)
        produced.revision += 1  # Manual selection of the exact same take still wins.
        blocked = preview_audio_version_undo(session, run.id)
        assert blocked["blocked_reason"] and blocked["restore_count"] == 0
        with pytest.raises(RevisionConflict):
            audio_version_undo_updates(session, run.id, preview["selection_hash"])


@pytest.mark.parametrize("drift", ["segment", "plan", "artifact", "job"])
def test_undo_rechecks_revision_plan_artifact_and_job(rig, drift):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        _produced(session, rig)
        _finish(session, rig)
        preview = preview_audio_version_undo(session, run.id)
        if drift == "segment":
            session.get(GenerationSegment, rig.segments[0]).revision += 1
        elif drift == "plan":
            session.get(GenerationPlan, rig.plan_id).active_revision_id = "another revision"
        elif drift == "artifact":
            baseline = session.get(AudioTake, audio_version_take_ids(run, "before")[rig.segments[0]])
            session.get(Artifact, baseline.artifact_id).state = "deleted"
        else:
            session.get(Job, rig.job_id).status = "running"
        with pytest.raises(RevisionConflict):
            audio_version_undo_updates(session, run.id, preview["selection_hash"])


def test_lifecycle_preserves_before_and_independent_later_pinned_mix(rig):
    with rig.database.session() as session:
        _initialize(session, rig)
        before = OutputAssembly(session_id=rig.session_id, generation_run_id=rig.run_id,
                                status="completed", settings_json={"audio_version_side": "before"})
        after = OutputAssembly(session_id=rig.session_id, generation_run_id=rig.run_id,
                               status="completed", settings_json={"audio_version_side": "after"})
        current = OutputAssembly(session_id=rig.session_id, status="completed")
        root = OutputAssembly(session_id=rig.session_id, generation_run_id=rig.root_id, status="completed")
        session.add_all([before, after, current, root])
        session.flush()
        mark_output_assemblies_stale(session, rig.session_id, generation_run_id=rig.root_id,
                                     include_later_runs=True)
        assert root.status == "stale" and current.status == "stale"
        assert before.status == after.status == "completed"
        current.status = "completed"
        mark_output_assemblies_stale(session, rig.session_id, generation_run_id=rig.run_id)
        assert before.status == "completed" and after.status == current.status == "stale"


def test_publication_records_produced_manifest_without_overwriting_manual_selection(rig):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        selected = session.get(AudioTake, rig.takes[0])
        expected = selection_guard(selected)
        selected.revision += 1
        artifact = Artifact(session_id=rig.session_id, kind="audio", relative_path="published.wav")

        class Registry:
            def register_in_session(self, database_session, *_args, **_kwargs):
                database_session.add(artifact)
                database_session.flush()
                return artifact

        publication = GenerationTakePublication(
            segment=session.get(GenerationSegment, rig.segments[0]), session_id=rig.session_id,
            run_id=run.id, output_run_id=run.id, path=Path("published.wav"), kind="tts",
            duration_ms=1200, parent_ids=[], parent_take_id=selected.id, settings={}, metadata={},
            prepared=None, expected_selection=expected, verification=None,
        )
        publish_generation_take(session, Registry(), publication, usage_event_factory=None,
                                mark_stale=mark_output_assemblies_stale)
        take = session.get(AudioTake, run.audio_version_json["generated_take_ids"][rig.segments[0]])
        assert take.is_active is False and selected.is_active is True
        assert run.audio_version_json["applied_take_ids"] == {}


@pytest.mark.parametrize("race", ["cast", "selection", "none"])
def test_recast_copy_publication_retains_alternative_and_respects_adoption_races(rig, race):
    with rig.database.session() as session:
        run = _initialize(session, rig)
        run.settings_snapshot_json = {"generation_recast_controls_revision": 0}
        source = session.get(GenerationSegment, rig.segments[0])
        original = session.get(AudioTake, rig.takes[0])
        expected = selection_guard(original)
        parent = session.get(GenerationPlanRevision, rig.revision_id)
        descendant = GenerationPlanRevision(
            plan_id=parent.plan_id, parent_revision_id=parent.id, revision_number=2,
            content_hash="unchanged copy", operation_json={
                "reason": "edit_copy", "action": "restore", "target_revision_id": parent.id,
            },
        )
        session.add(descendant)
        session.flush()
        session.get(GenerationPlan, rig.plan_id).active_revision_id = descendant.id
        attributes = {
            column.name: getattr(source, column.name) for column in source.__table__.columns
            if column.name not in {"id", "plan_revision_id", "created_at", "updated_at"}
        }
        target = GenerationSegment(plan_revision_id=descendant.id, **attributes)
        session.add(target)
        session.flush()
        inherited = AudioTake(
            generation_segment_id=target.id, artifact_id=original.artifact_id,
            parent_take_id=original.id, status="completed", is_active=True,
            revision=original.revision,
        )
        session.add(inherited)
        session.flush()
        if race == "cast":
            save_generation_controls(session, rig.session_id, expected_revision=0,
                                     cast={"narrator": {"voice": "Later casting"}})
        elif race == "selection":
            original.revision += 1
        artifact = Artifact(
            session_id=rig.session_id, kind="audio", relative_path="copy-replacement.wav",
            metadata_json={"synthesized_text": source.text},
        )

        class Registry:
            def register_in_session(self, database_session, *_args, **_kwargs):
                database_session.add(artifact)
                database_session.flush()
                return artifact

        publication = GenerationTakePublication(
            segment=source, session_id=rig.session_id, run_id=run.id, output_run_id=run.id,
            path=Path("copy-replacement.wav"), kind="tts", duration_ms=1200,
            parent_ids=[], parent_take_id=original.id, settings={}, metadata={},
            prepared=None, expected_selection=expected, verification=None,
        )
        publish_generation_take(session, Registry(), publication, usage_event_factory=None,
                                mark_stale=mark_output_assemblies_stale)
        session.flush()
        produced_id = run.audio_version_json["generated_take_ids"][source.id]
        produced = session.get(AudioTake, produced_id)
        copied = session.scalar(select(AudioTake).where(
            AudioTake.generation_segment_id == target.id, AudioTake.parent_take_id == produced.id,
        ))
        assert copied is not None and copied.artifact_id == produced.artifact_id
        assert produced.is_active == copied.is_active == (race == "none")
        if race != "none":
            session.refresh(original)
            assert original.is_active and inherited.is_active
            assert run.audio_version_json["applied_take_ids"] == {}
