"""Book exports capture one selected run and exact WAV assembly without changing profiles."""

import threading

import pytest
from sqlalchemy import select

from pandrator.web.export_contract import normalize_export_mode
from pandrator.web.models import Artifact, AudioTake, GenerationRun, GenerationSegment
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflows import WorkflowService
from tests import test_web_audio_assembly as fixtures


@pytest.fixture
def case():
    state = fixtures.DurableOutputAssemblyTests()
    state.setUp()
    state._plan_with_takes()
    with state.database.session() as session:
        segment = session.scalar(select(GenerationSegment))
        run = GenerationRun(
            session_id=state.record.id,
            plan_revision_id=segment.plan_revision_id,
            sequence_number=1,
            status="completed",
        )
        session.add(run)
        session.flush()
        run_id = run.id
        for take in session.scalars(select(AudioTake)):
            take.generation_run_id = run_id
    yield state, run_id
    state.tearDown()


@pytest.mark.parametrize("mode", ["video_book", "subtitles"])
def test_queue_freezes_lossless_assembly_and_preserves_audio_profile(case, mode):
    state, run_id = case
    workflows = WorkflowService(state.database, state.jobs)
    resolved = workflows.resolve_stage(
        state.record.id,
        "export",
        {"generation_run_id": run_id, "export_mode": mode, "format": "mp3"},
    )
    assert resolved.job_kind == "export.variant"
    assert resolved.payload["settings"]["format"] == "wav"
    assert resolved.payload["resolved_settings_snapshot"]["output"]["format"] == "wav"
    assert resolved.payload["export_contract"]["export_mode"] == mode
    assert state.settings.get(state.record.id, "output")["effective"]["format"] == "wav"
    assert resolved.payload["settings"]["stt_compute_backend"] == "cpu"


def test_legacy_matching_wav_is_reassembled_once_then_reused(case):
    state, run_id = case
    snapshot, _ = state.settings.resolve(state.record.id, ["audio", "output"])
    snapshot["output"]["export_mode"] = "video_book"
    handler = WorkflowHandlers(state.database, state.paths)

    def assemble():
        return handler._ensure_export_generation_assembly(
            session_id=state.record.id,
            generation_run_id=run_id,
            resolved_settings_snapshot=snapshot,
            progress=lambda *_: None,
            cancel_event=threading.Event(),
        )

    first = assemble()
    with state.database.session() as session:
        artifact = session.get(Artifact, first)
        metadata = dict(artifact.metadata_json)
        metadata.pop("audio_timeline")
        artifact.metadata_json = metadata
    second = assemble()
    assert second != first
    assert assemble() == second


def test_book_export_contract_rejects_other_workflows():
    assert normalize_export_mode("video_book", workflow_kind="audiobook") == "video_book"
    for workflow in ("subtitles", "voiceover", "media_edit"):
        with pytest.raises(ValueError):
            normalize_export_mode("video_book", workflow_kind=workflow)
