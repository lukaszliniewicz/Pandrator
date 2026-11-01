"""Storage pressure retains real assembly/export inputs and resumes one job."""

from __future__ import annotations

import hashlib
import subprocess
import threading
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web import audio_assembly, export_video, soundtrack_export, storage_admission
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.export_inputs import ExportInputs, MediaExportSelection
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import (
    Artifact,
    AudioTake,
    GenerationSegment,
    Job,
    OutputAssembly,
    ResourceClaim,
    StorageReservation,
    utcnow,
)
from pandrator.web.output_storage import (
    estimated_pcm_bytes,
    estimated_video_bytes,
    reserve_output_space,
    storage_effect_scope,
)
from pandrator.web.sessions import SessionService
from pandrator.web.storage_admission import (
    StorageAdmission,
    StorageJobLease,
    StoragePressure,
    StorageRequirement,
)
from pandrator.web.workflow_handlers import WorkflowHandlers
from pandrator.web.workflow_output_context import OutputWorkflowContext
from pandrator.web.workflows import WorkflowService
from pandrator.web.workspace import GenerationService
from pandrator.web.workspace_settings import WorkspaceSettingsService
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    disk = SimpleNamespace(free=10**12)
    monkeypatch.setattr(storage_admission, "shutil", SimpleNamespace(disk_usage=lambda _path: disk))
    try:
        yield SimpleNamespace(
            paths=paths,
            database=database,
            disk=disk,
            artifacts=ArtifactService(database, paths),
            queue=JobQueue(database),
            handlers=WorkflowHandlers(database, paths),
            settings=WorkspaceSettingsService(database),
        )
    finally:
        database.dispose()


def _resume_now(case, job_id):
    with case.database.session() as session:
        job = session.get(Job, job_id)
        assert job is not None
        job.available_at = utcnow() - timedelta(seconds=1)
    case.disk.free = 10**12


def _assert_held(case, job_id):
    job = case.queue.get(job_id)
    assert job.status == "queued" and job.error_code == "storage_pressure"
    assert job.attempts == 0 and job.lease_owner is None and job.available_at is not None
    assert "Completed inputs and recordings are retained" in job.progress_detail
    with case.database.session() as session:
        for model in [StorageReservation, ResourceClaim]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_assembly_storage_hold_preserves_takes_and_resumes_same_assembly(runtime):
    case = runtime
    record = SessionService(case.database).create("Storage assembly", workflow_kind="audiobook")
    directory = case.paths.sessions / record.storage_key
    directory.mkdir()
    generation = GenerationService(case.database, case.queue, case.settings, case.artifacts)
    plan = generation.create_plan(record.id, source_revision_id=None, segments=[{"text": "Amen."}])
    take_path = directory / "accepted-take.wav"
    with wave.open(str(take_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x01\x00" * 1600)
    artifact = case.artifacts.register(
        take_path, kind="audio", role="generation_take", session_id=record.id
    )
    before_hash = hashlib.sha256(take_path.read_bytes()).hexdigest()
    with case.database.session() as session:
        segment = session.scalar(
            select(GenerationSegment).where(
                GenerationSegment.plan_revision_id == plan["active_revision_id"]
            )
        )
        assert segment is not None
        segment.status = "completed"
        take = AudioTake(
            generation_segment_id=segment.id,
            artifact_id=artifact.id,
            status="completed",
            kind="tts",
            duration_ms=100,
            is_active=True,
        )
        session.add(take)
        session.flush()
        take_id = take.id
    assembly = generation.create_assembly(record.id, run_override={"output": {"format": "wav"}})
    worker = Worker(
        case.queue,
        "storage-assembly-worker",
        {
            "generation.assemble": case.handlers.assemble_generation_output,
        },
    )
    case.disk.free = 1
    with patch.object(
        audio_assembly, "assemble_audio_plan", wraps=audio_assembly.assemble_audio_plan
    ) as render:
        assert worker.run_once()
        render.assert_not_called()
        _assert_held(case, assembly["job_id"])
        with case.database.session() as session:
            held = session.get(OutputAssembly, assembly["id"])
            assert held is not None and held.status == "queued"
        assert hashlib.sha256(take_path.read_bytes()).hexdigest() == before_hash
        assert not list((directory / "assemblies").rglob("*.wav"))
        _resume_now(case, assembly["job_id"])
        assert worker.run_once()
        assert render.call_count == 1
    job = case.queue.get(assembly["job_id"])
    assert job.status == "succeeded" and job.attempts == 1 and job.lease_generation == 2
    with case.database.session() as session:
        completed = session.get(OutputAssembly, assembly["id"])
        retained = session.get(AudioTake, take_id)
        assert completed is not None and completed.status == "completed"
        assert retained is not None and retained.artifact_id == artifact.id and retained.is_active
        assert session.scalar(select(func.count()).select_from(AudioTake)) == 1
        assert session.scalar(select(func.count()).select_from(StorageReservation)) == 0
    assert hashlib.sha256(take_path.read_bytes()).hexdigest() == before_hash


def test_video_storage_hold_pins_caption_and_resumes_one_real_export(runtime):
    case = runtime
    record = SessionService(case.database).create(
        "Storage video", workflow_kind="voiceover", source_language="en"
    )
    directory = case.paths.sessions / record.storage_key
    directory.mkdir()
    source = directory / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=64x64:r=25:d=2",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=2",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
        timeout=30,
    )
    source_artifact = case.artifacts.register(
        source, kind="source", role="upload", session_id=record.id
    )
    source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    master_text = "1\n00:00:01,900 --> 00:00:02,000\nTonight: Amen.\n"
    master_path = directory / "reviewed.srt"
    master_path.write_text(master_text)
    master = case.artifacts.register(
        master_path, kind="srt", role="correction", session_id=record.id
    )
    job = WorkflowService(case.database, case.queue).run_stage(
        record.id,
        "export",
        {
            "export_mode": "media",
            "audio_mode": "preserve",
            "subtitle_mode": "soft",
            "subtitle_text_source": "selected_artifact",
            "subtitle_selection": "source",
        },
    )
    original_payload = job.payload_json
    worker = Worker(case.queue, "storage-export-worker", {"export.create": case.handlers.export})
    # Permit the small caption preflight, but reject a complete video copy.
    case.disk.free = 16 * 1024**2 + 2 * 65536 + 4 * len(master_text.encode()) + 1
    assert worker.run_once()
    _assert_held(case, job.id)
    with case.database.session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.kind == "export")
            )
            == 0
        )
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_hash
    later = directory / "later.srt"
    later.write_text(master_text.replace("Tonight: Amen.", "Later text must not be substituted."))
    case.artifacts.register(later, kind="srt", role="correction", session_id=record.id)
    _resume_now(case, job.id)
    assert worker.run_once()
    finished = case.queue.get(job.id)
    assert finished.status == "succeeded", finished.error_message
    assert finished.id == job.id and finished.attempts == 1 and finished.lease_generation == 2
    assert finished.payload_json == original_payload
    assert len(finished.result_json["artifact_ids"]) == 1
    final_artifact, final_path = case.artifacts.resolve(finished.result_json["artifact_ids"][0])
    assert final_artifact.metadata_json["media_durations_ms"]["video"] == pytest.approx(2000)
    embedded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(final_path), "-map", "0:s:0", "-f", "srt", "-"],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert "Tonight: Amen." in embedded and "Later text" not in embedded
    assert master.id in {
        pin["artifact_id"] for pin in original_payload["subtitle_track_snapshot"]["tracks"]
    }
    assert source_artifact.content_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert master_path.read_text() == master_text
    with case.database.session() as session:
        assert session.scalar(select(func.count()).select_from(StorageReservation)) == 0


def test_estimates_distinguish_pcm_copy_and_quality_encode():
    assert (
        estimated_pcm_bytes(1000, sample_rate=48000, channels=2, sample_width=3) == 288000 + 65536
    )
    assert estimated_video_bytes(1000000, 1000) == 1000000 + 65536
    assert (
        estimated_video_bytes(1000000, 1000, transcode=True, width=3840, height=2160, fps=30)
        > 2000000
    )
    with pytest.raises(ValueError):
        estimated_pcm_bytes(float("nan"))


@pytest.mark.parametrize("invalid", [None, 0, 1, "true"])
def test_output_helper_validates_preserve_existing_even_for_zero_bytes(runtime, invalid):
    with pytest.raises(TypeError, match="preserve_existing must be a boolean"):
        reserve_output_space(runtime.database, runtime.paths.root, 0, preserve_existing=invalid)


@pytest.mark.parametrize("audio_mode", ["mixed", "dubbed"])
def test_video_preparation_retains_envelope_through_uncached_soundtrack(
    runtime, monkeypatch, audio_mode,
):
    case = runtime
    sessions = SessionService(case.database)
    record = sessions.create("Nested storage", workflow_kind="voiceover")
    directory = case.paths.sessions / record.storage_key
    directory.mkdir()
    source, speech = directory / "source.mp4", directory / "speech.wav"
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=1280x720:r=25:d=2",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=2",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "0", "-c:a", "aac", str(source),
    ], check=True, capture_output=True, timeout=45)
    with wave.open(str(speech), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(48000)
        output.writeframes(b"\x01\x00" * 96000)
    source_artifact = case.artifacts.register(
        source, kind="source", role="upload", session_id=record.id,
    )
    speech_artifact = case.artifacts.register(
        speech, kind="audio", role="assembled_audio", session_id=record.id,
    )
    leases = []
    for name in ("video", "competitor"):
        job = case.queue.enqueue("export.create", {}, session_id=record.id)
        lease = StorageJobLease(job.id, name, 1)
        with case.database.session() as unit:
            row = unit.get(Job, job.id)
            assert row is not None
            row.status, row.lease_owner, row.lease_generation = "running", name, 1
            row.lease_expires_at = utcnow() + timedelta(minutes=5)
        leases.append(lease)
    video_lease, competitor = leases

    def operation_dir(_session_id, stage):
        result = directory / stage
        result.mkdir(exist_ok=True)
        return result

    def unexpected_mix(*_args, **_kwargs):
        pytest.fail("No sound cues should invoke the sound-mix renderer")

    context = OutputWorkflowContext(
        case.database, case.paths, case.artifacts, case.artifacts.resolve,
        lambda _session_id: directory, sessions.get, operation_dir,
        _render_sound_mix=unexpected_mix,
    )
    inputs = ExportInputs(
        record.id, {"audio_match_source_duration": True}, record, {}, None,
        [source_artifact], {"upload": source_artifact}, speech_artifact, (), None, None,
    )
    selection = MediaExportSelection(
        source_artifact, None, [], speech_artifact, "media", "srt", "none", audio_mode,
    )
    events = []

    def reserved_bytes(stage):
        with case.database.snapshot_session() as unit:
            rows = list(unit.scalars(select(StorageReservation)))
            assert len(rows) == 1
            row = rows[0]
            assert (row.job_id, row.lease_owner, row.lease_generation) == (
                video_lease.job_id, video_lease.owner_id, video_lease.generation,
            )
            events.append((stage, row.reserved_bytes))
            return row.reserved_bytes

    original_reserve = export_video.reserve_output_space
    original_master = soundtrack_export.ensure_soundtrack_master
    original_mux = export_video.run_video_command

    def reserve_video(*args, **kwargs):
        original_reserve(*args, **kwargs)
        assert kwargs["label"] == "video preparation"
        reserved_bytes("parent reservation")

    def ensure_uncached_master(*args, **kwargs):
        parent_bytes = reserved_bytes("before soundtrack")
        with case.database.snapshot_session() as unit:
            assert unit.scalar(select(func.count()).select_from(Artifact).where(
                Artifact.role == "soundtrack_master",
            )) == 0
        # Simulate parent bytes already materialized. Only the new soundtrack
        # estimate plus metadata and headroom fits this free-space reading.
        leaf_bytes = estimated_pcm_bytes(2000) + 65536
        assert parent_bytes > leaf_bytes
        case.disk.free = 16 * 1024**2 + leaf_bytes
        try:
            master = original_master(*args, **kwargs)
        finally:
            case.disk.free = 10**12
        assert reserved_bytes("after soundtrack") == parent_bytes
        assert case.artifacts.resolve(master.id)[1].is_file()
        return master

    def mux_video(command, *args, **kwargs):
        parent_bytes = reserved_bytes("before mux")
        assert parent_bytes == events[0][1]
        leaf_bytes = estimated_pcm_bytes(2000) + 65536
        competitor_bytes = 4 * 1024**2
        # A reduced leaf reservation would admit this competing job, while
        # retaining the full video envelope must reject it.
        case.disk.free = 16 * 1024**2 + competitor_bytes + (parent_bytes + leaf_bytes) // 2
        other = Database(case.database.path)
        try:
            with pytest.raises(StoragePressure) as failure:
                StorageAdmission(other).reserve(competitor, [
                    StorageRequirement(directory, competitor_bytes),
                ])
            assert failure.value.reserved_bytes == parent_bytes
            assert reserved_bytes("after rejected competitor") == parent_bytes
        finally:
            other.dispose()
            case.disk.free = 10**12
        original_mux(command, *args, **kwargs)
        assert Path(command[-1]).stat().st_size > 0

    monkeypatch.setattr(export_video, "reserve_output_space", reserve_video)
    monkeypatch.setattr(soundtrack_export, "ensure_soundtrack_master", ensure_uncached_master)
    monkeypatch.setattr(export_video, "run_video_command", mux_video)
    scratch = directory / "scratch"
    scratch.mkdir()
    token = current_job_lease.set({
        "job_id": video_lease.job_id, "owner_id": video_lease.owner_id,
        "lease_generation": video_lease.generation,
    })
    try:
        with storage_effect_scope(case.database):
            prepared = export_video._prepare_video_audio(
                context, inputs, selection, media_path=source, scratch_dir=scratch,
                ffmpeg_executable="ffmpeg", video_encoder="libx264", video_audio_bitrate="192k",
                progress=lambda *_args: None, cancel_event=threading.Event(),
            )
            assert prepared.path.is_file()
            assert reserved_bytes("completed preparation") == events[0][1]
    finally:
        current_job_lease.reset(token)
    stages = [stage for stage, _bytes in events]
    if audio_mode == "mixed":
        assert stages == [
            "parent reservation", "before soundtrack", "after soundtrack", "before mux",
            "after rejected competitor", "completed preparation",
        ]
    else:
        assert stages == [
            "parent reservation", "before mux", "after rejected competitor", "completed preparation",
        ]
    with case.database.snapshot_session() as unit:
        assert unit.scalar(select(func.count()).select_from(StorageReservation)) == 0
