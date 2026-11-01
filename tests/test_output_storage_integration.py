"""Storage pressure retains real assembly/export inputs and resumes one job."""

from __future__ import annotations

import hashlib
import subprocess
import wave
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from pandrator.web import audio_assembly, storage_admission
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
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
from pandrator.web.output_storage import estimated_pcm_bytes, estimated_video_bytes
from pandrator.web.sessions import SessionService
from pandrator.web.workflow_handlers import WorkflowHandlers
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
