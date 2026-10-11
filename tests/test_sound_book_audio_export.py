"""Frame-preserving sound-mix audiobook export contracts."""

from __future__ import annotations

import hashlib
import shutil
import struct
import threading
import wave
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import select

from pandrator.logic import audio_processor
from pandrator.web import sound_book_audio_export as export_module
from pandrator.web.artifacts import ArtifactService, sha256_file
from pandrator.web.database import Database
from pandrator.web.job_execution import current_job_lease
from pandrator.web.media_process import MediaProcessCancelled
from pandrator.web.models import Artifact, ArtifactEdge, Job, utcnow
from pandrator.web.sessions import SessionService
from pandrator.web.sound_mix_plan import build_sound_mix_plan
from pandrator.web.workflow_output_context import OutputWorkflowContext
from tests.web_test_support import prepare_web_test_data_root


def _write_pcm(path: Path, frames: int, *, rate: int = 8000, value: int = 1000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    samples = [value] * frames
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(rate)
        writer.writeframes(struct.pack(f"<{frames}h", *samples))


class SoundBookAudioExportDomain:
    def __init__(self, root: Path) -> None:
        self.paths = prepare_web_test_data_root(root / "data")
        self.database = Database(self.paths.database)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.record = SessionService(self.database).create(
            "Mixed book", workflow_kind="audiobook"
        )
        self.session_dir = self.paths.sessions / self.record.storage_key
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self.context = OutputWorkflowContext(
            self.database,
            self.paths,
            self.artifacts,
            self.artifacts.resolve,
            lambda _session_id: self.session_dir,
            lambda _session_id: self.record,
            lambda _session_id, operation: self.session_dir / "operations" / operation,
        )
        self.rate = 8000
        self.frames = 16000
        self.audio_path = self.session_dir / "mixed.wav"
        _write_pcm(self.audio_path, self.frames, rate=self.rate)
        frame_plan = build_sound_mix_plan(
            base_frames=self.frames,
            rate=self.rate,
            channels=1,
            base_content_hash="base-content-hash",
            clips=[],
        )
        self.metadata: dict[str, Any] = {
            "sound_mix": {
                "request_fingerprint": "sound-request-fingerprint",
                "mix_signature": "published-mix-signature",
                "frame_plan": frame_plan,
            },
            "audio_timeline": {
                "version": 1,
                "sample_rate_hz": self.rate,
                "total_frames": self.frames,
            },
            "duration_ms": 2000,
            "takes": [{"take_id": "take-1", "start_frame": 0, "end_frame": 8000}],
            "chapters": [
                {"start_ms": 0, "title": "Opening"},
                {"start_ms": 750, "title": "After inserted effect"},
            ],
        }
        self.audio = self.artifacts.register(
            self.audio_path,
            kind="audio",
            role="sound_mix.audio",
            session_id=self.record.id,
            metadata=self.metadata,
        )

    def call(
        self,
        *,
        settings: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        cancel_event: threading.Event | None = None,
        name: str = "Mixed-book",
    ) -> Artifact:
        return export_module.export_mixed_book_audio(
            self.context,
            self.audio,
            session_id=self.record.id,
            export_name=name,
            settings={"format": "wav", **(settings or {})},
            payload=payload or {},
            progress=lambda _fraction, _detail: None,
            cancel_event=cancel_event or threading.Event(),
        )


@pytest.fixture
def domain(tmp_path: Path) -> SoundBookAudioExportDomain:
    return SoundBookAudioExportDomain(tmp_path)


def _fake_encoder(plan, destination: Path, **_kwargs):
    part = plan.parts[0]
    shutil.copyfile(part.path, destination)
    with wave.open(str(part.path), "rb") as reader:
        frames = reader.getnframes()
        rate = reader.getframerate()
    return SimpleNamespace(total_frames=frames, sample_rate_hz=rate)


def test_wav_export_keeps_exact_pcm_and_mix_provenance(
    domain: SoundBookAudioExportDomain, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_hash = sha256_file(domain.audio_path)
    tagged: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        audio_processor,
        "_save_metadata_and_cover",
        lambda *args, **kwargs: tagged.append((args, kwargs)) or True,
    )

    output = domain.call(settings={"format": "wav", "include_sounds": True})
    output_path = domain.paths.managed_path(output.relative_path)

    assert output_path.suffix == ".wav"
    assert sha256_file(domain.audio_path) == source_hash
    with wave.open(str(output_path), "rb") as reader:
        assert reader.getnframes() == domain.frames
        assert reader.getframerate() == domain.rate
        assert reader.getnchannels() == 1
        assert reader.getsampwidth() == 2
        output_samples = reader.readframes(reader.getnframes())
    with wave.open(str(domain.audio_path), "rb") as reader:
        source_samples = reader.readframes(reader.getnframes())
    assert output_samples == source_samples
    assert output.metadata_json["sound_mix"] == domain.metadata["sound_mix"]
    assert domain.audio.metadata_json == domain.metadata
    assert output.metadata_json["chapters"] == domain.metadata["chapters"]
    assert output.metadata_json["audio_timeline"] == domain.metadata["audio_timeline"]
    assert output.metadata_json["requested_format"] == "wav"
    assert output.metadata_json["include_sounds"] is True
    assert output.metadata_json["output_settings"]["sections"]["output"]["format"] == "wav"
    assert output.metadata_json["output_settings"]["sections"]["output"]["include_sounds"] is True
    assert tagged[0][0][1] == "wav"
    assert tagged[0][0][2] == {
        "title": "Mixed book",
        "artist": "",
        "album": "",
        "genre": "",
        "language": "",
    }
    with domain.database.session() as session:
        parents = set(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == output.id
                )
            )
        )
    assert parents == {domain.audio.id}


@pytest.mark.parametrize("format_name", ["mp3", "m4b", "flac", "opus"])
def test_compressed_exports_keep_extension_and_tag_remapped_chapters(
    domain: SoundBookAudioExportDomain,
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
) -> None:
    monkeypatch.setattr(export_module, "assemble_audio_plan", _fake_encoder)
    saved: list[tuple[Any, ...]] = []
    chapters: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        audio_processor,
        "_save_metadata_and_cover",
        lambda *args, **kwargs: saved.append((args, kwargs)) or True,
    )
    monkeypatch.setattr(
        audio_processor,
        "_add_chapters_to_m4b",
        lambda *args, **kwargs: chapters.append((args, kwargs)) or True,
    )

    output = domain.call(settings={"format": format_name, "bitrate": "128k"})

    assert domain.paths.managed_path(output.relative_path).suffix == f".{format_name}"
    assert saved[0][0][1] == format_name
    assert saved[0][0][2]["title"] == "Mixed book"
    assert output.metadata_json["sound_mix"]["mix_signature"] == "published-mix-signature"
    assert output.metadata_json["requested_format"] == format_name
    if format_name == "m4b":
        assert chapters[0][0][1] == [(0.0, "Opening"), (0.75, "After inserted effect")]
        assert chapters[0][1]["total_duration_sec"] == 2.0
        assert len(saved) == 2
    else:
        assert chapters == []
        assert len(saved) == 1


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is unavailable")
def test_streaming_backend_can_encode_a_tiny_mp3(
    domain: SoundBookAudioExportDomain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PANDRATOR_AUDIO_ASSEMBLER", "streaming")
    monkeypatch.setattr(
        audio_processor,
        "_save_metadata_and_cover",
        lambda *_args, **_kwargs: True,
    )

    output = domain.call(settings={"format": "mp3", "bitrate": "64k"})
    output_path = domain.paths.managed_path(output.relative_path)

    assert output_path.suffix == ".mp3"
    assert output_path.stat().st_size > 100
    assert not output_path.read_bytes().startswith(b"RIFF")


def test_optional_cover_is_same_session_lineage_and_used_for_tags(
    domain: SoundBookAudioExportDomain, monkeypatch: pytest.MonkeyPatch
) -> None:
    cover_path = domain.session_dir / "cover.png"
    cover_path.write_bytes(b"test image bytes")
    cover = domain.artifacts.register(
        cover_path,
        kind="image",
        role="cover_image",
        session_id=domain.record.id,
    )
    monkeypatch.setattr(export_module, "assemble_audio_plan", _fake_encoder)
    tagged: list[tuple[Any, ...]] = []
    monkeypatch.setattr(
        audio_processor,
        "_save_metadata_and_cover",
        lambda *args, **kwargs: tagged.append((args, kwargs)) or True,
    )

    output = domain.call(settings={"format": "mp3", "cover_artifact_id": cover.id})

    assert tagged[0][0][3] == str(cover_path)
    assert output.metadata_json["cover_artifact_id"] == cover.id
    with domain.database.session() as session:
        parents = set(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == output.id
                )
            )
        )
    assert parents == {domain.audio.id, cover.id}


def test_invalid_format_or_foreign_cover_is_rejected_before_publication(
    domain: SoundBookAudioExportDomain,
) -> None:
    with pytest.raises(ValueError, match="Unsupported audiobook output format"):
        domain.call(settings={"format": "aac"})

    other = SessionService(domain.database).create("Other book", workflow_kind="audiobook")
    foreign_path = domain.paths.sessions / other.storage_key / "cover.png"
    foreign_path.parent.mkdir(parents=True, exist_ok=True)
    foreign_path.write_bytes(b"foreign image")
    foreign = domain.artifacts.register(
        foreign_path,
        kind="image",
        role="cover_image",
        session_id=other.id,
    )
    with pytest.raises(ValueError, match="current image from this session"):
        domain.call(settings={"cover_artifact_id": foreign.id})

    assert not list((domain.session_dir / "exports" / "audio").glob("Mixed-book*"))


def test_cancellation_after_encoding_leaves_no_published_file(
    domain: SoundBookAudioExportDomain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cancel_event = threading.Event()
    source_hash = sha256_file(domain.audio_path)

    def cancel_after_encoding(plan, destination: Path, **kwargs):
        result = _fake_encoder(plan, destination, **kwargs)
        cancel_event.set()
        return result

    monkeypatch.setattr(export_module, "assemble_audio_plan", cancel_after_encoding)
    with pytest.raises(MediaProcessCancelled):
        domain.call(cancel_event=cancel_event)

    assert sha256_file(domain.audio_path) == source_hash
    assert not list((domain.session_dir / "exports" / "audio").glob("Mixed-book*"))
    assert list((domain.session_dir / "exports" / ".staging").iterdir()) == []


def test_active_lease_identity_is_checked_before_publishing(
    domain: SoundBookAudioExportDomain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export_module, "assemble_audio_plan", _fake_encoder)
    with domain.database.immediate_session() as session:
        job = Job(
            kind="export.create",
            session_id=domain.record.id,
            status="running",
            lease_owner="worker-a",
            lease_generation=4,
            lease_expires_at=utcnow() + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        job_id = job.id
    token = current_job_lease.set(
        {"job_id": job_id, "owner_id": "worker-b", "lease_generation": 4}
    )
    try:
        with pytest.raises(InterruptedError, match="lease is no longer current"):
            domain.call(
                payload={"_job_id": job_id, "_lease_generation": 4}
            )
    finally:
        current_job_lease.reset(token)
    assert not (domain.session_dir / "exports" / "audio").exists()


def test_publication_helper_rechecks_job_claim_after_preflight(
    domain: SoundBookAudioExportDomain,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(export_module, "assemble_audio_plan", _fake_encoder)
    with domain.database.immediate_session() as session:
        job = Job(
            kind="export.create",
            session_id=domain.record.id,
            status="running",
            lease_owner="worker-a",
            lease_generation=5,
            lease_expires_at=utcnow() + timedelta(minutes=5),
        )
        session.add(job)
        session.flush()
        job_id = job.id

    real_publish = export_module.publish_video_export

    def revoke_then_publish(*args, **kwargs):
        with domain.database.immediate_session() as session:
            current = session.get(Job, job_id)
            assert current is not None
            current.status = "canceled"
        return real_publish(*args, **kwargs)

    monkeypatch.setattr(export_module, "publish_video_export", revoke_then_publish)
    token = current_job_lease.set(
        {"job_id": job_id, "owner_id": "worker-a", "lease_generation": 5}
    )
    try:
        with pytest.raises(InterruptedError, match="lease is no longer current"):
            domain.call(payload={"_job_id": job_id, "_lease_generation": 5})
    finally:
        current_job_lease.reset(token)

    assert not list((domain.session_dir / "exports" / "audio").glob("Mixed-book*"))
    assert hashlib.sha256(domain.audio_path.read_bytes()).hexdigest() == domain.audio.content_hash
