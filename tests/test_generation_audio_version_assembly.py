"""Pinned recast assembly acceptance using disposable databases and real WAVs."""

import threading
from copy import deepcopy
from unittest import mock

import pytest
from sqlalchemy import event, select

import tests.test_web_audio_assembly as support
from pandrator.web.export_inputs import resolve_export_inputs
from pandrator.web.generation_audio_versions import (
    audio_version_take_ids,
    initialize_audio_version,
    record_audio_version_take,
)
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationRun,
    GenerationSegment,
    Job,
    OutputAssembly,
)
from pandrator.web.sessions import SessionService
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workspace import find_matching_output_assembly


@pytest.fixture
def rig():
    fixture = support.DurableOutputAssemblyTests()
    fixture.setUp()
    try:
        fixture.segment_ids = fixture._plan_with_takes()
        with fixture.database.session() as session:
            segments = [session.get(GenerationSegment, key) for key in fixture.segment_ids]
            root = GenerationRun(session_id=fixture.record.id,
                                 plan_revision_id=segments[0].plan_revision_id,
                                 sequence_number=1, status="completed")
            job = Job(session_id=fixture.record.id, kind="generation", status="running")
            session.add_all([root, job])
            session.flush()
            run = GenerationRun(session_id=fixture.record.id, plan_revision_id=root.plan_revision_id,
                                sequence_number=2, status="running", job_id=job.id,
                                operation="regenerate", source_generation_run_id=root.id)
            session.add(run)
            session.flush()
            baseline = {}
            for segment in segments:
                take = session.scalar(select(AudioTake).where(
                    AudioTake.generation_segment_id == segment.id, AudioTake.is_active.is_(True),
                ))
                take.generation_run_id = root.id
                baseline[segment.id] = take.id
                session.add(AudioTake(generation_run_id=root.id, generation_segment_id=segment.id,
                                      artifact_id=take.artifact_id, status="completed", is_active=False))
            initialize_audio_version(session, run, baseline, fixture.segment_ids[:1],
                                     [{"key": "speaker:a", "label": "A"}])
            fixture.before = audio_version_take_ids(run, "before")
            fixture.run_id, fixture.root_id, fixture.job_id = run.id, root.id, job.id
        yield fixture
    finally:
        fixture.tearDown()


def finish(rig, status="partial", generated=True):
    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        if generated:
            baseline = session.get(AudioTake, rig.before[rig.segment_ids[0]])
            take = AudioTake(generation_run_id=run.id, generation_segment_id=rig.segment_ids[0],
                             artifact_id=baseline.artifact_id, status="completed", is_active=False)
            session.add(take)
            session.flush()
            record_audio_version_take(session, run.id, take, applied=False)
        run.status = status
        session.get(Job, rig.job_id).status = "succeeded" if status != "failed" else "failed"
        return audio_version_take_ids(run)


def queue(rig, side="after"):
    return rig.generation.create_assembly(
        rig.record.id, generation_run_id=rig.run_id, audio_version_side=side,
        run_override={"output": {"format": "wav"}},
    )


def render(rig, queued):
    return WorkflowHandlers(rig.database, rig.paths).assemble_generation_output(
        {"output_assembly_id": queued["id"]}, lambda *_: None, threading.Event(),
    )


def test_before_runs_with_exact_older_manual_baseline_and_after_is_blocked(rig):
    with pytest.raises(ValueError, match="finish"):
        queue(rig)
    queued = queue(rig, "before")
    result = render(rig, queued)
    with rig.database.session() as session:
        assembly = session.get(OutputAssembly, queued["id"])
        manifest = assembly.settings_json["takes"]
        assert [row["take_id"] for row in manifest] == list(rig.before.values())
        artifact = session.get(Artifact, result["artifact_id"])
        assert artifact.metadata_json["audio_version_side"] == "before"
        assert artifact.metadata_json["audio_version_take_ids"] == rig.before
        assert assembly.settings_json["audio_version_selection_hash"]


@pytest.mark.parametrize("status", ["completed", "partial", "failed", "canceled"])
def test_terminal_after_contains_replacement_and_retained_baseline(rig, status):
    after = finish(rig, status)
    assert after[rig.segment_ids[0]] != rig.before[rig.segment_ids[0]]
    assert after[rig.segment_ids[1]] == rig.before[rig.segment_ids[1]]
    queued = queue(rig)
    # A later active take, and a baseline selection revision change, cannot
    # alter either historical side of this version.
    with rig.database.session() as session:
        baseline = session.get(AudioTake, rig.before[rig.segment_ids[0]])
        baseline.revision += 1
        baseline.is_active = True
        session.add(AudioTake(generation_segment_id=baseline.generation_segment_id,
                              artifact_id=baseline.artifact_id, status="completed", is_active=True))
        pin = deepcopy(session.get(OutputAssembly, queued["id"]).settings_json)
    result = render(rig, queued)
    with rig.database.session() as session:
        assembly = session.get(OutputAssembly, queued["id"])
        assert [row["take_id"] for row in assembly.settings_json["takes"]] == list(after.values())
        assert assembly.settings_json["audio_version_take_ids"] == pin["audio_version_take_ids"]
        assert assembly.settings_json["audio_version_selection_hash"] == pin["audio_version_selection_hash"]
        assert session.get(Artifact, result["artifact_id"]).metadata_json["audio_version_side"] == "after"


def test_failed_after_without_generated_take_preserves_whole_baseline(rig):
    assert finish(rig, "failed", generated=False) == rig.before
    render(rig, queue(rig))


def test_terminal_run_requires_terminal_job(rig):
    finish(rig)
    with rig.database.session() as session:
        session.get(Job, rig.job_id).status = "running"
    with pytest.raises(ValueError, match="job to finish"):
        queue(rig)


def test_terminal_run_requires_an_existing_job(rig):
    finish(rig)
    with rig.database.session() as session:
        session.get(GenerationRun, rig.run_id).job_id = None
    with pytest.raises(ValueError, match="job to finish"):
        queue(rig)


def test_take_lookup_batches_at_500_ids(rig):
    from pandrator.web.generation_audio_version_assembly import audio_version_assembly_selection

    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        value = deepcopy(run.audio_version_json)
        original = session.get(AudioTake, rig.before[rig.segment_ids[0]])
        for index in range(501):
            segment = GenerationSegment(plan_revision_id=run.plan_revision_id,
                                        ordinal=index + 2, text=f"Retained {index}")
            session.add(segment)
            session.flush()
            take = AudioTake(generation_run_id=run.id, generation_segment_id=segment.id,
                             artifact_id=original.artifact_id, status="completed")
            session.add(take)
            session.flush()
            value["before_take_ids"][segment.id] = take.id
        run.audio_version_json = value
    batch_sizes = []

    def capture(_connection, _cursor, statement, parameters, _context, _executemany):
        if "audio_takes.id IN" in statement:
            batch_sizes.append(len(parameters))

    event.listen(rig.database.engine, "before_cursor_execute", capture)
    try:
        with rig.database.session() as session:
            _selection, rows = audio_version_assembly_selection(
                session, rig.record.id, session.get(GenerationRun, rig.run_id), "before",
            )
        assert len(rows) == 503
        assert batch_sizes == [500, 3]
    finally:
        event.remove(rig.database.engine, "before_cursor_execute", capture)


@pytest.mark.parametrize("side", ["before", "after"])
def test_missing_baseline_or_artifact_rejects(rig, side):
    finish(rig, generated=False)
    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        value = deepcopy(run.audio_version_json)
        value[f"{side}_take_ids"].pop(rig.segment_ids[1])
        run.audio_version_json = value
    with pytest.raises(ValueError, match="complete baseline"):
        queue(rig, side)


def test_missing_file_rejects_preparation(rig):
    with rig.database.session() as session:
        take = session.get(AudioTake, rig.before[rig.segment_ids[0]])
        path = rig.paths.managed_path(session.get(Artifact, take.artifact_id).relative_path)
    path.unlink()
    with pytest.raises(ValueError, match="file is missing"):
        queue(rig, "before")


def test_invalid_side_and_legacy_before_reject(rig):
    with pytest.raises(ValueError, match="before or after"):
        queue(rig, "bogus")
    with pytest.raises(ValueError, match="only for a recast"):
        rig.generation.create_assembly(rig.record.id, generation_run_id=rig.root_id,
                                       audio_version_side="before")
    with pytest.raises(ValueError, match="only for a recast"):
        rig.generation.prepare_assembly(rig.record.id, audio_version_side="before")


def test_preview_map_drift_rejects_before_enqueue(rig):
    finish(rig)
    prepared = rig.generation.prepare_assembly(rig.record.id, generation_run_id=rig.run_id)
    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        value = deepcopy(run.audio_version_json)
        value["after_take_ids"] = dict(rig.before)
        run.audio_version_json = value
    with rig.database.immediate_session() as session, pytest.raises(RevisionConflict):
        rig.generation.create_assembly_in_session(session, rig.record.id,
                                                  generation_run_id=rig.run_id, prepared=prepared)
    with rig.database.session() as session:
        assert not list(session.scalars(select(OutputAssembly)))


def test_deleting_source_run_keeps_shared_baseline_artifacts(rig):
    finish(rig, generated=False)
    rig.generation.delete_run(rig.root_id)
    render(rig, queue(rig, "before"))


def test_worker_rejects_pin_drift_before_rendering(rig):
    finish(rig)
    queued = queue(rig)
    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        value = deepcopy(run.audio_version_json)
        value["after_take_ids"] = dict(rig.before)
        run.audio_version_json = value
    with pytest.raises(RevisionConflict):
        render(rig, queued)


def test_final_transaction_rejects_unavailable_artifact_and_retains_pin(rig):
    finish(rig)
    queued = queue(rig)
    original_register = support.ArtifactService.register

    def invalidate(service, *args, **kwargs):
        artifact = original_register(service, *args, **kwargs)
        if kwargs.get("role") == "assembled_audio":
            with rig.database.session() as session:
                take = session.get(AudioTake, rig.before[rig.segment_ids[1]])
                session.get(Artifact, take.artifact_id).state = "stale"
        return artifact

    with mock.patch.object(support.ArtifactService, "register", invalidate), pytest.raises(RevisionConflict):
        render(rig, queued)
    with rig.database.session() as session:
        assembly = session.get(OutputAssembly, queued["id"])
        assert assembly.status == "failed"
        assert assembly.settings_json["audio_version_side"] == "after"
        assert session.get(Artifact, assembly.artifact_id).state == "stale"


def _assembly_snapshot(rig, queued):
    with rig.database.session() as session:
        return deepcopy(session.get(OutputAssembly, queued["id"]).settings_json["resolved"])


def _export_payload(rig, side=None, snapshot=None, artifact_id=None):
    settings = {"generation_run_id": rig.run_id, "export_mode": "audio"}
    if side is not None:
        settings["audio_version_side"] = side
    payload = {"session_id": rig.record.id, "settings": settings}
    if snapshot is not None:
        payload["resolved_settings_snapshot"] = snapshot
    if artifact_id is not None:
        payload["pinned_assembly_artifact_id"] = artifact_id
    return payload


def test_matching_and_export_resolution_distinguish_sides_after_manual_selection(rig):
    after = finish(rig)
    # Make BEFORE the latest assembly to catch accidental latest-output reuse.
    queued_after = queue(rig)
    artifact_after = render(rig, queued_after)["artifact_id"]
    queued_before = queue(rig, "before")
    artifact_before = render(rig, queued_before)["artifact_id"]
    snapshot = _assembly_snapshot(rig, queued_after)
    assert snapshot == _assembly_snapshot(rig, queued_before)

    with rig.database.session() as session:
        segment = session.get(GenerationSegment, rig.segment_ids[0])
        baseline = session.get(AudioTake, rig.before[segment.id])
        manual = AudioTake(generation_segment_id=segment.id, generation_run_id=rig.root_id,
                           artifact_id=baseline.artifact_id, status="completed", is_active=False)
        session.add(manual)
        session.flush()
        rig.generation.select_take_in_session(session, segment.id, manual.id, segment.revision)

    handlers = WorkflowHandlers(rig.database, rig.paths)
    for side, expected_id, take_ids in (
        ("before", artifact_before, rig.before),
        ("after", artifact_after, after),
        (None, artifact_after, after),
    ):
        with rig.database.session() as session:
            kwargs = {"audio_version_side": side} if side else {}
            match, _resolved, _hash = find_matching_output_assembly(
                session, session_id=rig.record.id, run=session.get(GenerationRun, rig.run_id),
                resolved_settings_snapshot=snapshot, **kwargs,
            )
            assert match.artifact_id == expected_id
            assert match.settings_json["audio_version_take_ids"] == take_ids
        # Both immutable queued snapshots and older direct-call compatibility
        # must honor the requested side, including default AFTER.
        for export_snapshot in (snapshot, None):
            inputs = resolve_export_inputs(handlers._output_workflow_context(),
                                           _export_payload(rig, side, export_snapshot))
            assert inputs.selected_audio.id == expected_id
            assert inputs.selected_audio.metadata_json["audio_version_take_ids"] == take_ids


@pytest.mark.parametrize("side", ["before", "after"])
def test_inline_export_variant_carries_exact_side_and_partial_baseline(rig, side):
    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        manifest = deepcopy(run.audio_version_json)
        manifest["target_segment_ids"] = list(rig.segment_ids)
        run.audio_version_json = manifest
        session.get(GenerationSegment, rig.segment_ids[1]).status = "failed"
    after = finish(rig, "partial")
    prepared = rig.generation.prepare_assembly(
        rig.record.id, generation_run_id=rig.run_id, audio_version_side=side,
        run_override={"output": {"format": "wav"}},
    )
    handlers = WorkflowHandlers(rig.database, rig.paths)
    payload = _export_payload(rig, side, prepared["snapshot"])
    with mock.patch.object(handlers, "export", return_value={"artifact_ids": []}) as export:
        handlers.handler_registry["export.variant"](payload, lambda *_: None, threading.Event())
    exported_payload = export.call_args.args[0]
    assert exported_payload["settings"]["audio_version_side"] == side
    selected_id = exported_payload["pinned_assembly_artifact_id"]
    expected = rig.before if side == "before" else after
    with rig.database.session() as session:
        assembly = session.scalar(select(OutputAssembly).where(OutputAssembly.artifact_id == selected_id))
        assert assembly.status == "completed"
        assert assembly.settings_json["audio_version_side"] == side
        assert assembly.settings_json["audio_version_take_ids"] == expected
        assert [row["take_id"] for row in assembly.settings_json["takes"]] == list(expected.values())
        assert expected[rig.segment_ids[1]] == rig.before[rig.segment_ids[1]]
    inputs = resolve_export_inputs(handlers._output_workflow_context(), exported_payload)
    assert inputs.selected_audio.id == selected_id
    assert inputs.selected_audio.metadata_json["audio_version_take_ids"] == expected


def test_inline_before_live_and_after_export_rejects_live_run_or_job(rig):
    handlers = WorkflowHandlers(rig.database, rig.paths)
    snapshot = rig.generation.prepare_assembly(
        rig.record.id, generation_run_id=rig.run_id, audio_version_side="before",
        run_override={"output": {"format": "wav"}},
    )["snapshot"]
    artifact_id = handlers._ensure_export_generation_assembly(
        session_id=rig.record.id, generation_run_id=rig.run_id, audio_version_side="before",
        resolved_settings_snapshot=snapshot, progress=lambda *_: None, cancel_event=threading.Event(),
    )
    inputs = resolve_export_inputs(handlers._output_workflow_context(),
                                   _export_payload(rig, "before", snapshot, artifact_id))
    assert inputs.selected_audio.metadata_json["audio_version_take_ids"] == rig.before
    for run_status, job_status in (("running", "running"), ("partial", "running")):
        with rig.database.session() as session:
            session.get(GenerationRun, rig.run_id).status = run_status
            session.get(Job, rig.job_id).status = job_status
        with pytest.raises(ValueError, match="finish"):
            handlers._ensure_export_generation_assembly(
                session_id=rig.record.id, generation_run_id=rig.run_id,
                resolved_settings_snapshot=snapshot, progress=lambda *_: None,
                cancel_event=threading.Event(),
            )
        for export_snapshot in (snapshot, None):
            with pytest.raises(ValueError, match="finish"):
                resolve_export_inputs(handlers._output_workflow_context(),
                                      _export_payload(rig, "after", export_snapshot))


@pytest.mark.parametrize("side", ["before", "after"])
def test_export_rejects_unavailable_pinned_take_without_chronological_fallback(rig, side):
    finish(rig)
    queued = queue(rig, side)
    artifact_id = render(rig, queued)["artifact_id"]
    snapshot = _assembly_snapshot(rig, queued)
    with rig.database.session() as session:
        run = session.get(GenerationRun, rig.run_id)
        pinned = audio_version_take_ids(run, side)
        session.delete(session.get(AudioTake, pinned[rig.segment_ids[1]]))
        # Original active audio and newer inactive audio are still available.
        assert len(list(session.scalars(select(AudioTake).where(
            AudioTake.generation_segment_id == rig.segment_ids[1],
            AudioTake.generation_run_id == rig.root_id,
        )))) == 2
    handlers = WorkflowHandlers(rig.database, rig.paths)
    for export_snapshot in (snapshot, None):
        with pytest.raises(ValueError, match="pinned recast"):
            resolve_export_inputs(handlers._output_workflow_context(),
                                  _export_payload(rig, side, export_snapshot, artifact_id))
    with pytest.raises(ValueError, match="pinned recast"):
        handlers._ensure_export_generation_assembly(
            session_id=rig.record.id, generation_run_id=rig.run_id, audio_version_side=side,
            resolved_settings_snapshot=snapshot, progress=lambda *_: None,
            cancel_event=threading.Event(),
        )


@pytest.mark.parametrize("pin_kind", ["missing", "wrong_side", "wrong_project"])
def test_export_rejects_wrong_assembly_pin_without_other_available_side_fallback(rig, pin_kind):
    finish(rig)
    queued_after = queue(rig)
    artifact_after = render(rig, queued_after)["artifact_id"]
    queued_before = queue(rig, "before")
    artifact_before = render(rig, queued_before)["artifact_id"]
    snapshot = _assembly_snapshot(rig, queued_after)
    artifact_id = "unavailable-assembly" if pin_kind == "missing" else artifact_before
    if pin_kind == "wrong_project":
        foreign = SessionService(rig.database).create("Another project", workflow_kind="audiobook")
        with rig.database.session() as session:
            artifact = Artifact(session_id=foreign.id, kind="audio", role="assembled_audio",
                                relative_path="foreign.wav", state="current")
            session.add(artifact)
            session.flush()
            artifact_id = artifact.id
    assert artifact_id != artifact_after
    handlers = WorkflowHandlers(rig.database, rig.paths)
    with pytest.raises(ValueError, match="no longer current"):
        resolve_export_inputs(handlers._output_workflow_context(),
                              _export_payload(rig, "after", snapshot, artifact_id))
