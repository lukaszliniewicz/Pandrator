"""Persistence, worker-fencing, and publication tests for sound mixes."""

from __future__ import annotations

import hashlib
import json
import threading
import wave
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from sqlalchemy import event, select

from pandrator.runtime import DataPaths
from pandrator.web import jobs as jobs_module
from pandrator.web import sound_mix_manifest
from pandrator.web.artifacts import ArtifactService, sha256_file
from pandrator.web.database import Database
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.media_process import MediaProcessCancelled, MediaProcessResult
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    AudioTake,
    Base,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationSegment,
    Job,
    OutputAssembly,
    SessionRecord,
    SoundVariant,
    utcnow,
)
from pandrator.web.sound_mix_service import SoundMixService
from pandrator.web.sound_mix_state import invalidate_sound_mixes
from pandrator.web.sound_project_models import ProjectSoundCue


def _write_pcm(path: Path, frames: int, value: int = 100) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = np.full(frames, value, dtype="<i2")
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(8000)
        writer.writeframes(data.tobytes())


def _frames(path: Path) -> int:
    with wave.open(str(path), "rb") as reader:
        return reader.getnframes()


def _read_values(path: Path) -> list[int]:
    with wave.open(str(path), "rb") as reader:
        return np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").tolist()


class SoundMixDomain:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.paths = DataPaths.from_value(root / "data").ensure()
        self.database = Database(self.paths.database)
        Base.metadata.create_all(self.database.engine)
        monkeypatch.setattr(jobs_module, "ensure_app_job_allowed", lambda _path: None)
        self.artifacts = ArtifactService(self.database, self.paths)
        self.jobs = JobQueue(self.database)
        self.service = SoundMixService(
            self.database,
            self.paths,
            self.artifacts,
            self.jobs,
        )
        effect_path = self.paths.artifacts / "effect.wav"
        _write_pcm(effect_path, 8000, 500)
        effect = self.artifacts.register(effect_path, kind="audio", role="sound.original")
        with self.database.immediate_session() as session:
            variant = SoundVariant(
                artifact_id=effect.id,
                state="ready",
                sample_rate=8000,
                channels=1,
                frame_count=8000,
                duration_seconds=1.0,
            )
            session.add(variant)
            session.flush()
            self.variant_id = variant.id
            self.effect_artifact_id = effect.id
        self.audiobook = self.create_session("audiobook", 16000, with_takes=True)
        self.voiceover = self.create_session("voiceover", 12000, with_takes=False)
        self.no_cues = self.create_session("audiobook", 16000, with_takes=True)

    def create_session(
        self, workflow_kind: str, frame_count: int, *, with_takes: bool
    ) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            record = SessionRecord(name=f"{workflow_kind} project", workflow_kind=workflow_kind)
            session.add(record)
            session.flush()
            session_id = record.id
            storage_key = record.storage_key
            segment_ids: list[str] = []
            if with_takes:
                plan = GenerationPlan(session_id=session_id)
                session.add(plan)
                session.flush()
                revision = GenerationPlanRevision(
                    plan_id=plan.id,
                    revision_number=1,
                    content_hash=f"speech-{session_id}",
                )
                session.add(revision)
                session.flush()
                plan.active_revision_id = revision.id
                for ordinal, text in enumerate(("Requested first text", "Requested second text")):
                    segment = GenerationSegment(
                        plan_revision_id=revision.id,
                        ordinal=ordinal,
                        text=text,
                    )
                    session.add(segment)
                    session.flush()
                    segment_ids.append(segment.id)

        take_ids: list[str] = []
        take_artifact_ids: list[str] = []
        if with_takes:
            for index, segment_id in enumerate(segment_ids):
                take_path = self.paths.artifacts / f"take-{session_id}-{index}.wav"
                _write_pcm(take_path, 8000, 1000 + index)
                take_artifact = self.artifacts.register(
                    take_path,
                    kind="audio",
                    role="tts_audio",
                    session_id=session_id,
                    metadata={"synthesized_text": f"Actual spoken wording {index}"},
                )
                with self.database.immediate_session() as session:
                    take = AudioTake(
                        generation_segment_id=segment_id,
                        artifact_id=take_artifact.id,
                        status="completed",
                        is_active=True,
                        duration_ms=1000,
                    )
                    session.add(take)
                    session.flush()
                    take_ids.append(take.id)
                    take_artifact_ids.append(take_artifact.id)

        base_path = self.paths.sessions / storage_key / "base.wav"
        _write_pcm(base_path, frame_count, 100)
        metadata: dict[str, Any] = {
            "audio_timeline": {
                "version": 1,
                "sample_rate_hz": 8000,
                "total_frames": frame_count,
            },
            "chapters": [
                {"title": "First", "start_ms": 0},
                {"title": "Second", "start_ms": 1000},
            ],
        }
        if with_takes:
            metadata["takes"] = [
                {
                    "take_id": take_ids[0],
                    "segment_id": segment_ids[0],
                    "artifact_id": take_artifact_ids[0],
                    "start_frame": 0,
                    "end_frame": 8000,
                },
                {
                    "take_id": take_ids[1],
                    "segment_id": segment_ids[1],
                    "artifact_id": take_artifact_ids[1],
                    "start_frame": 8000,
                    "end_frame": 16000,
                },
            ]
        base = self.artifacts.register(
            base_path,
            kind="audio",
            role="assembled_audio",
            session_id=session_id,
            metadata=metadata,
        )
        return {
            "session_id": session_id,
            "base": base,
            "base_path": base_path,
            "segment_ids": segment_ids,
            "take_ids": take_ids,
            "take_artifact_ids": take_artifact_ids,
        }

    def add_cue(
        self,
        project: dict[str, Any],
        anchor: dict[str, Any],
        playback: dict[str, Any],
    ) -> str:
        with self.database.immediate_session() as session:
            result = self.service.project_sounds.add_in_session(
                session,
                project["session_id"],
                {
                    "expected_revision": 0,
                    "variant_id": self.variant_id,
                    "anchor": anchor,
                    "playback": playback,
                },
            )
            return str(result["cue"]["logical_id"])

    def dispose(self) -> None:
        self.database.dispose()


@pytest.fixture
def domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    value = SoundMixDomain(tmp_path, monkeypatch)
    try:
        yield value
    finally:
        value.dispose()


def _render(domain: SoundMixDomain, project: dict[str, Any], **kwargs: Any) -> Artifact:
    return domain.service.render_for_artifact(
        project["session_id"],
        project["base"].id,
        progress=kwargs.pop("progress", lambda _fraction, _detail: None),
        cancel_event=kwargs.pop("cancel_event", threading.Event()),
        **kwargs,
    )


def test_no_effects_returns_original_artifact_identity(domain: SoundMixDomain) -> None:
    project = domain.no_cues
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    assert prepared.has_cues is False
    result = _render(domain, project)
    assert result.id == project["base"].id
    assert domain.service.get_status(project["session_id"])["status"] == "inactive"


def test_already_mixed_audio_cannot_be_used_as_base_or_timeline(
    domain: SoundMixDomain,
) -> None:
    project = domain.no_cues
    mixed_paths = []
    for label in ("base", "timeline"):
        path = domain.paths.sessions / project["base"].id / f"{label}-mix.wav"
        _write_pcm(path, 16000)
        mixed_paths.append(
            domain.artifacts.register(
                path,
                kind="audio",
                role="sound_mix.audio",
                session_id=project["session_id"],
            )
        )
    with pytest.raises(ValueError, match="already a sound mix"):
        domain.service.prepare(project["session_id"], mixed_paths[0].id)
    with pytest.raises(ValueError, match="already a sound mix"):
        domain.service.prepare(
            project["session_id"],
            project["base"].id,
            timeline_artifact_id=mixed_paths[1].id,
        )


def test_fixed_timeline_probe_uses_exact_duration_ticks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "quarter-second.wav"
    _write_pcm(path, 2001)
    payload = {
        "streams": [
            {
                "sample_rate": "8000",
                "channels": 1,
                "duration_ts": "2001",
                "time_base": "1/8000",
                "duration": "N/A",
            }
        ]
    }
    monkeypatch.setattr(
        sound_mix_manifest,
        "run_media_process",
        lambda *_args, **_kwargs: MediaProcessResult(0, stdout=json.dumps(payload)),
    )
    assert sound_mix_manifest._base_geometry(
        {"metadata_json": {}}, "voiceover", path
    ) == (8000, 1, 2001, True)


def test_admission_reuses_job_and_status_uses_at_most_three_selects(
    domain: SoundMixDomain, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    assert prepared.payload.keys() == {
        "session_id",
        "base_artifact_id",
        "base_content_hash",
        "timeline_artifact_id",
        "timeline_content_hash",
        "sound_plan_id",
        "sound_plan_revision",
        "speech_plan_revision_id",
        "request_fingerprint",
    }
    assert str(domain.paths.root) not in repr(prepared.payload)
    with domain.database.immediate_session() as session:
        first = domain.service.admit_in_session(session, prepared)
    with domain.database.immediate_session() as session:
        second = domain.service.admit_in_session(session, prepared)
    assert first["status"] == "queued"
    assert first["job_id"] == second["job_id"]
    with domain.database.snapshot_session() as session:
        job = session.get(Job, first["job_id"])
        assert job is not None
        assert job.kind == "sound.mix"
        assert job.session_id == project["session_id"]
        assert job.max_attempts == 1
        assert job.resource_keys_json == [f"session:{project['session_id']}"]
        assert str(domain.paths.root) not in repr(job.payload_json)

    statements: list[str] = []

    def count_select(_connection, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    monkeypatch.setattr(
        "pandrator.web.sound_mix_admission.sha256_file",
        lambda _path: pytest.fail("status must not hash artifacts"),
    )
    monkeypatch.setattr(
        "pandrator.web.sound_mix_manifest.preferred_pcm_format",
        lambda *_args, **_kwargs: pytest.fail("status must not probe audio"),
    )
    monkeypatch.setattr(
        "pandrator.web.sound_mix_manifest.run_media_process",
        lambda *_args, **_kwargs: pytest.fail("status must not decode audio"),
    )
    event.listen(domain.database.engine, "before_cursor_execute", count_select)
    try:
        status = domain.service.get_status(project["session_id"], project["base"].id)
    finally:
        event.remove(domain.database.engine, "before_cursor_execute", count_select)
    assert len(statements) <= 3
    assert status["status"] == "queued"
    assert status["job_id"] == first["job_id"]


def test_insert_between_frozen_takes_remaps_chapters_and_exact_cache(
    domain: SoundMixDomain,
) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    base_hash = sha256_file(project["base_path"])
    effect = domain.artifacts.resolve(domain.effect_artifact_id)[1]
    effect_hash = sha256_file(effect)
    mixed = _render(domain, project)
    mixed_path = domain.paths.managed_path(mixed.relative_path)
    assert _frames(mixed_path) == 24000
    metadata = mixed.metadata_json
    assert metadata["audio_timeline"]["total_frames"] == 24000
    assert [(row["start_frame"], row["end_frame"]) for row in metadata["takes"]] == [
        (0, 8000),
        (16000, 24000),
    ]
    assert metadata["chapters"][1]["start_ms"] == 2000
    assert metadata["sound_mix"]["insertions"] == [
        {"base_frame": 8000, "added_frames": 8000}
    ]
    assert metadata["sound_mix"]["frame_plan"]["total_frames"] == 24000
    assert "source_path" not in repr(metadata["sound_mix"]["frame_plan"])
    assert sha256_file(project["base_path"]) == base_hash
    assert sha256_file(effect) == effect_hash
    with domain.database.snapshot_session() as session:
        parents = set(
            session.scalars(
                select(ArtifactEdge.parent_artifact_id).where(
                    ArtifactEdge.child_artifact_id == mixed.id
                )
            )
        )
    assert parents == {project["base"].id, domain.effect_artifact_id}
    cached = _render(domain, project)
    assert cached.id == mixed.id
    assert cached.metadata_json["sound_mix"]["request_fingerprint"] == metadata["sound_mix"][
        "request_fingerprint"
    ]

    with domain.database.immediate_session() as session:
        domain.service.project_sounds.update_in_session(
            session,
            project["session_id"],
            session.scalar(
                select(ProjectSoundCue.logical_id).where(
                    ProjectSoundCue.plan_id == metadata["sound_mix"]["sound_plan_id"]
                )
            ),
            {
                "expected_revision": 1,
                "playback": {
                    "mode": "insert",
                    "gain_db": -6.0,
                    "fade_in_ms": 0,
                    "fade_out_ms": 0,
                },
            },
        )
    changed = domain.service.prepare(project["session_id"], project["base"].id)
    assert changed.request_fingerprint != metadata["sound_mix"]["request_fingerprint"]


def test_timed_overlay_applies_trim_gain_fades_without_extending_base(
    domain: SoundMixDomain,
) -> None:
    project = domain.voiceover
    domain.add_cue(
        project,
        {"kind": "source_time", "time_seconds": 0.25},
        {
            "mode": "overlay",
            "offset_ms": 0,
            "gain_db": -6.0,
            "trim_start_seconds": 0.125,
            "trim_end_seconds": 0.75,
            "fade_in_ms": 100,
            "fade_out_ms": 100,
        },
    )
    base_hash = sha256_file(project["base_path"])
    effect = domain.artifacts.resolve(domain.effect_artifact_id)[1]
    effect_hash = sha256_file(effect)
    mixed = _render(domain, project)
    metadata = mixed.metadata_json["sound_mix"]
    assert _frames(domain.paths.managed_path(mixed.relative_path)) == 12000
    clip = metadata["frame_plan"]["clips"][0]
    assert (clip["start_frame"], clip["end_frame"]) == (2000, 7000)
    assert (clip["trim_start_frame"], clip["trim_end_frame"]) == (1000, 6000)
    assert (clip["fade_in_frames"], clip["fade_out_frames"]) == (800, 800)
    assert clip["gain_db"] == -6.0
    assert sha256_file(project["base_path"]) == base_hash
    assert sha256_file(effect) == effect_hash


@pytest.mark.parametrize(
    ("anchor_time", "offset_ms", "fade_ms", "message"),
    [
        (0.1, -200, 0, "before frame zero"),
        (1.3, 0, 0, "fixed-timeline overlay"),
        (0.25, 0, 700, "Fade lengths cannot exceed"),
    ],
)
def test_invalid_overlay_offsets_overruns_and_fades_fail(
    domain: SoundMixDomain,
    anchor_time: float,
    offset_ms: int,
    fade_ms: int,
    message: str,
) -> None:
    project = domain.create_session("voiceover", 12000, with_takes=False)
    domain.add_cue(
        project,
        {"kind": "source_time", "time_seconds": anchor_time},
        {
            "mode": "overlay",
            "offset_ms": offset_ms,
            "trim_start_seconds": 0.125,
            "trim_end_seconds": 0.75,
            "fade_in_ms": fade_ms,
            "fade_out_ms": 0,
        },
    )
    with pytest.raises(ValueError, match=message):
        _render(domain, project)


def test_after_word_resolution_uses_frozen_take_and_exact_synthesized_text_hash(
    domain: SoundMixDomain,
) -> None:
    project = domain.audiobook
    cue_id = domain.add_cue(
        project,
        {
            "kind": "after_word",
            "segment_id": project["segment_ids"][0],
            "quote": "word",
            "occurrence": 1,
        },
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    take_id = project["take_ids"][0]
    take_hash = domain.artifacts.resolve(project["take_artifact_ids"][0])[0].content_hash
    spoken = "Actual spoken wording 0"
    transcript_hash = hashlib.sha256(spoken.encode("utf-8")).hexdigest()
    with domain.database.immediate_session() as session:
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.logical_id == cue_id))
        assert cue is not None
        cue.anchor_json = {
            "kind": "after_word",
            "segment_id": project["segment_ids"][0],
            "quote": "word",
            "occurrence": 1,
            "resolution": {
                "status": "ready",
                "take_id": take_id,
                "artifact_id": project["take_artifact_ids"][0],
                "take_content_hash": take_hash,
                "transcript_hash": transcript_hash,
                "take_frame": 4000,
                "take_sample_rate": 8000,
            },
        }
        cue.placement_status = "ready"
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    assert prepared.cues[0]["anchor_frame"] == 4000

    with domain.database.immediate_session() as session:
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.logical_id == cue_id))
        assert cue is not None
        anchor = dict(cue.anchor_json)
        anchor["resolution"] = {**anchor["resolution"], "artifact_id": "different-artifact"}
        cue.anchor_json = anchor
    with pytest.raises(ValueError, match="placement"):
        domain.service.prepare(project["session_id"], project["base"].id)

    with domain.database.immediate_session() as session:
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.logical_id == cue_id))
        assert cue is not None
        anchor = dict(cue.anchor_json)
        anchor["resolution"] = {
            **anchor["resolution"],
            "artifact_id": project["take_artifact_ids"][0],
            "transcript_hash": hashlib.sha256(
                b"Requested first text"
            ).hexdigest(),
        }
        cue.anchor_json = anchor
    with pytest.raises(ValueError, match="placement"):
        domain.service.prepare(project["session_id"], project["base"].id)

    with domain.database.immediate_session() as session:
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.logical_id == cue_id))
        assert cue is not None
        anchor = dict(cue.anchor_json)
        anchor["resolution"] = {
            **anchor["resolution"],
            "take_id": "not-in-frozen-manifest",
            "transcript_hash": transcript_hash,
        }
        cue.anchor_json = anchor
    with pytest.raises(ValueError, match="placement"):
        domain.service.prepare(project["session_id"], project["base"].id)


def test_timed_word_anchor_is_refused_without_take_to_assembly_mapping(
    domain: SoundMixDomain,
) -> None:
    project = domain.create_session("voiceover", 16000, with_takes=True)
    cue_id = domain.add_cue(
        project,
        {
            "kind": "after_word",
            "segment_id": project["segment_ids"][0],
            "quote": "word",
            "occurrence": 1,
        },
        {"mode": "overlay", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    take_hash = domain.artifacts.resolve(project["take_artifact_ids"][0])[0].content_hash
    with domain.database.immediate_session() as session:
        cue = session.scalar(select(ProjectSoundCue).where(ProjectSoundCue.logical_id == cue_id))
        assert cue is not None
        cue.anchor_json = {
            **cue.anchor_json,
            "resolution": {
                "status": "ready",
                "take_id": project["take_ids"][0],
                "artifact_id": project["take_artifact_ids"][0],
                "take_content_hash": take_hash,
                "transcript_hash": hashlib.sha256(
                    b"Actual spoken wording 0"
                ).hexdigest(),
                "take_frame": 4000,
                "take_sample_rate": 8000,
            },
        }
        cue.placement_status = "ready"
    with pytest.raises(ValueError, match="placement"):
        domain.service.prepare(project["session_id"], project["base"].id)


def test_worker_payload_is_path_free_and_changed_fingerprint_is_rejected(
    domain: SoundMixDomain,
) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    with domain.database.immediate_session() as session:
        receipt = domain.service.admit_in_session(session, prepared)
        job = session.get(Job, receipt["job_id"])
        assert job is not None
        job.status = "running"
        job.lease_owner = "worker"
        job.lease_generation = 4
        job.lease_expires_at = utcnow() + timedelta(minutes=2)
    payload = {
        **prepared.payload,
        "_job_id": receipt["job_id"],
        "_lease_generation": 4,
        "request_fingerprint": "changed-fingerprint",
    }
    lease = current_job_lease.set(
        {"job_id": receipt["job_id"], "owner_id": "worker", "lease_generation": 4}
    )
    try:
        with pytest.raises(ValueError, match="changed after admission"):
            domain.service.run_mix(payload, lambda _fraction, _detail: None, threading.Event())
    finally:
        current_job_lease.reset(lease)
    assert str(domain.paths.root) not in repr(prepared.payload)
    assert "base_path" not in prepared.payload


def test_worker_renders_with_current_lease_and_returns_path_free_receipt(
    domain: SoundMixDomain,
) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    with domain.database.immediate_session() as session:
        admitted = domain.service.admit_in_session(session, prepared)
        job = session.get(Job, admitted["job_id"])
        assert job is not None
        job.status = "running"
        job.lease_owner = "worker"
        job.lease_generation = 5
        job.lease_expires_at = utcnow() + timedelta(minutes=2)
    payload = {
        **prepared.payload,
        "_job_id": admitted["job_id"],
        "_lease_generation": 5,
    }
    lease = current_job_lease.set(
        {"job_id": admitted["job_id"], "owner_id": "worker", "lease_generation": 5}
    )
    try:
        receipt = domain.service.run_mix(
            payload,
            lambda _fraction, _detail: None,
            threading.Event(),
        )
    finally:
        current_job_lease.reset(lease)
    assert receipt["status"] == "ready"
    assert receipt["cache_hit"] is False
    assert receipt["artifact_id"]
    assert receipt["mix_signature"]
    assert str(domain.paths.root) not in repr(receipt)


def test_expired_lease_and_prepublication_cancellation_do_not_publish(
    domain: SoundMixDomain,
) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    with domain.database.immediate_session() as session:
        admitted = domain.service.admit_in_session(session, prepared)
        job = session.get(Job, admitted["job_id"])
        assert job is not None
        job.status = "running"
        job.lease_owner = "worker"
        job.lease_generation = 6
        job.lease_expires_at = utcnow() - timedelta(seconds=1)
    payload = {
        **prepared.payload,
        "_job_id": admitted["job_id"],
        "_lease_generation": 6,
    }
    lease = current_job_lease.set(
        {"job_id": admitted["job_id"], "owner_id": "worker", "lease_generation": 6}
    )
    try:
        with pytest.raises(ValueError, match="publication lease"):
            domain.service.run_mix(payload, lambda _fraction, _detail: None, threading.Event())
    finally:
        current_job_lease.reset(lease)
    with domain.database.snapshot_session() as session:
        assert session.scalar(
            select(Artifact.id).where(
                Artifact.session_id == project["session_id"],
                Artifact.role == "sound_mix.audio",
            )
        ) is None

    canceled = domain.create_session("audiobook", 16000, with_takes=True)
    domain.add_cue(
        canceled,
        {"kind": "before_segment", "segment_id": canceled["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    cancel_event = threading.Event()
    cancel_event.set()
    with pytest.raises(MediaProcessCancelled, match="canceled"):
        _render(domain, canceled, cancel_event=cancel_event)
    with domain.database.snapshot_session() as session:
        assert session.scalar(
            select(Artifact.id).where(
                Artifact.session_id == canceled["session_id"],
                Artifact.role == "sound_mix.audio",
            )
        ) is None


@pytest.mark.parametrize("changed_pin", ["base", "variant"])
def test_changed_base_or_variant_pin_cannot_publish(
    domain: SoundMixDomain,
    changed_pin: str,
) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    mutated = False

    def change_pin(_fraction: float, detail: str) -> None:
        nonlocal mutated
        if mutated or detail != "Normalizing base audio":
            return
        mutated = True
        artifact_id = (
            project["base"].id if changed_pin == "base" else domain.effect_artifact_id
        )
        with domain.database.immediate_session() as session:
            artifact = session.get(Artifact, artifact_id)
            assert artifact is not None
            artifact.content_hash = "f" * 64

    with pytest.raises(ValueError, match="inputs changed"):
        _render(domain, project, progress=change_pin)
    assert mutated
    with domain.database.snapshot_session() as session:
        assert session.scalar(
            select(Artifact.id).where(
                Artifact.session_id == project["session_id"],
                Artifact.role == "sound_mix.audio",
            )
        ) is None


def test_stale_plan_cannot_publish_and_post_commit_cancel_keeps_file(
    domain: SoundMixDomain,
) -> None:
    project = domain.audiobook
    cue_id = domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    called = False

    def change_plan(_fraction: float, detail: str) -> None:
        nonlocal called
        if called or detail != "Normalizing base audio":
            return
        called = True
        with domain.database.immediate_session() as session:
            domain.service.project_sounds.update_in_session(
                session,
                project["session_id"],
                cue_id,
                {
                    "expected_revision": 1,
                    "playback": {
                        "mode": "insert",
                        "gain_db": -3.0,
                        "fade_in_ms": 0,
                        "fade_out_ms": 0,
                    },
                },
            )

    with pytest.raises(ValueError, match="inputs changed"):
        _render(domain, project, progress=change_plan)
    assert called
    with domain.database.snapshot_session() as session:
        assert session.scalar(
            select(Artifact.id).where(
                Artifact.session_id == project["session_id"],
                Artifact.role == "sound_mix.audio",
            )
        ) is None

    # The plan is current again; cancellation after commit must preserve output.
    event_set = threading.Event()

    def late_cancel(_fraction: float, detail: str) -> None:
        if detail == "Sound mix ready":
            event_set.set()

    mixed = _render(domain, project, progress=late_cancel, cancel_event=event_set)
    path = domain.paths.managed_path(mixed.relative_path)
    assert event_set.is_set()
    assert path.is_file()
    assert sha256_file(path) == mixed.content_hash


def test_invalidation_stales_mix_and_export_descendants_only(domain: SoundMixDomain) -> None:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert", "fade_in_ms": 0, "fade_out_ms": 0},
    )
    mixed = _render(domain, project)
    export_path = domain.paths.sessions / mixed.session_id / "export.wav"
    _write_pcm(export_path, 24000, 300)
    export = domain.artifacts.register(
        export_path,
        kind="audio",
        role="finalized_export",
        session_id=project["session_id"],
        parent_ids=[mixed.id],
    )
    dry_path = domain.paths.sessions / mixed.session_id / "dry.wav"
    _write_pcm(dry_path, 16000, 200)
    dry = domain.artifacts.register(
        dry_path,
        kind="audio",
        role="assembled_audio",
        session_id=project["session_id"],
    )
    with domain.database.immediate_session() as session:
        assembly = OutputAssembly(
            session_id=project["session_id"],
            artifact_id=dry.id,
            status="completed",
        )
        session.add(assembly)
        session.flush()
        assembly_id = assembly.id
        take_before = session.get(AudioTake, project["take_ids"][0]).status
        assert invalidate_sound_mixes(session, project["session_id"]) == 1
    with domain.database.snapshot_session() as session:
        assert session.get(Artifact, mixed.id).state == "stale"
        assert session.get(Artifact, export.id).state == "stale"
        assert session.get(Artifact, dry.id).state == "current"
        assert session.get(OutputAssembly, assembly_id).status == "completed"
        assert session.get(AudioTake, project["take_ids"][0]).status == take_before
