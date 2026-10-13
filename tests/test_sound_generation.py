"""Admission, cache identity, and lease-fenced sound publication tests."""

from __future__ import annotations

import hashlib
import threading
import wave
from datetime import timedelta
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import ValidationError
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web import jobs as jobs_module
from pandrator.web import sound_generation_worker
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, SoundVariant, utcnow
from pandrator.web.sound_schemas import SoundGenerate
from pandrator.web.sound_service import SoundService


@pytest.fixture
def domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    paths = DataPaths.from_value(tmp_path / "data").ensure()
    config = Config()
    config.set_main_option(
        "script_location",
        str(Path(__file__).parents[1] / "pandrator" / "web" / "migrations"),
    )
    config.set_main_option("sqlalchemy.url", sqlite_url(paths.database))
    command.upgrade(config, "head")
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    jobs = JobQueue(database)
    monkeypatch.setattr(jobs_module, "ensure_app_job_allowed", lambda _path: None)
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "revision",
        "minimum_runtime_version": "0.9.0",
    }

    def runtime_identity(_settings, *, model_id="stable_audio_3_small_sfx_q8_0"):
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(
        "pandrator.logic.audio_cpp_sfx.runtime_identity",
        runtime_identity,
    )
    monkeypatch.setattr(
        "pandrator.logic.audio_cpp_sfx.require_backend",
        lambda _settings, _backend: None,
    )
    def settings_provider():
        return {
            "audio_cpp_executable": "/trusted/audio.cpp",
            "audio_cpp_cache_dir": "/trusted/cache",
            "audio_cpp_timeout_seconds": 30,
            "api_secret": "MUST_NOT_BE_STORED",
        }
    sounds = SoundService(
        database,
        paths,
        artifacts,
        jobs,
        settings_provider=settings_provider,
    )
    try:
        yield database, paths, artifacts, jobs, sounds, identity
    finally:
        database.dispose()


def _admit(database, sounds, payload, prepared):
    with database.immediate_session() as session:
        return sounds.generate_in_session(session, payload, prepared=prepared)


def _claim(database, receipt, *, generation=1, owner="sound-worker"):
    with database.immediate_session() as session:
        job = session.get(Job, receipt["job_id"])
        assert job is not None
        job.status = "running"
        job.lease_owner = owner
        job.lease_generation = generation
        job.lease_expires_at = utcnow() + timedelta(minutes=2)
        payload = {
            **job.payload_json,
            "_job_id": job.id,
            "_lease_generation": generation,
        }
    return payload, owner


def _write_wave(
    path: Path, *, frames=44100, sample_rate=44100
) -> dict[str, int | float]:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(sample_rate)
        output.writeframes(b"\x01\x00\x02\x00" * frames)
    return {
        "frames": frames,
        "sample_rate": sample_rate,
        "channels": 2,
        "sample_width": 2,
        "duration_seconds": frames / sample_rate,
    }


def _run_with_lease(sounds, payload, owner, *, cancel_event=None):
    lease = current_job_lease.set(
        {
            "job_id": payload["_job_id"],
            "owner_id": owner,
            "lease_generation": payload["_lease_generation"],
        }
    )
    try:
        return sounds.run_generate(
            payload,
            lambda _value, _detail: None,
            cancel_event or threading.Event(),
        )
    finally:
        current_job_lease.reset(lease)


def test_strict_request_and_seeded_admission_cache_and_resource_keys(domain, monkeypatch):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    with pytest.raises(ValidationError):
        SoundGenerate.model_validate({"prompt": "rain", "audio_cpp_executable": "/tmp/x"})
    with pytest.raises(ValidationError):
        SoundGenerate.model_validate({"prompt": "rain", "seed": 2**32})
    with pytest.raises(ValidationError):
        SoundGenerate.model_validate({"prompt": "rain", "duration_seconds": float("inf")})

    monkeypatch.setattr(
        "pandrator.web.sound_generation_admission.secrets.randbits", lambda bits: 123
    )
    payload = {"prompt": "steady rain", "duration_seconds": 1.0}
    prepared = sounds.prepare_generation(payload)
    assert "seed" not in prepared.request_template
    first = _admit(database, sounds, payload, prepared)
    assert first["status"] == "queued"
    assert first["cache_hit"] is False
    assert first["resolved_settings"]["seed"] == 123

    explicit = {"prompt": "steady rain", "duration_seconds": 1.0, "seed": 123}
    replay_prepare = sounds.prepare_generation(explicit)
    cached = _admit(database, sounds, explicit, replay_prepare)
    assert cached["candidate_id"] == first["candidate_id"]
    assert cached["job_id"] == first["job_id"]
    assert cached["cache_hit"] is True

    with database.session() as session:
        job = session.get(Job, first["job_id"])
        candidate = session.get(SoundVariant, first["candidate_id"])
        assert job is not None and candidate is not None
        assert job.kind == "sound.generate"
        assert job.session_id is None
        assert job.max_attempts == 1
        assert job.resource_keys_json == ["service:tts:audio_cpp"]
        assert set(job.payload_json) == {"candidate_id", "request", "runtime_identity"}
        assert job.payload_json["request"]["num_inference_steps"] == 8
        assert job.payload_json["request"]["guidance_scale"] == 1.0
        assert candidate.fingerprint is not None and len(candidate.fingerprint) == 64
        assert candidate.provenance_json["threads"] == first["resolved_settings"]["threads"]
        assert candidate.provenance_json["kind"] == "sound"
        assert candidate.provenance_json["num_inference_steps"] == 8
        assert candidate.provenance_json["guidance_scale"] == 1.0
        assert "MUST_NOT_BE_STORED" not in str(job.payload_json)
        assert "MUST_NOT_BE_STORED" not in str(candidate.provenance_json)

    with database.session() as session:
        session.get(Job, first["job_id"]).status = "failed"
    fresh_after_failure = _admit(database, sounds, explicit, replay_prepare)
    assert fresh_after_failure["candidate_id"] != first["candidate_id"]
    with database.session() as session:
        session.get(Job, fresh_after_failure["job_id"]).status = "canceled"
    fresh_after_cancel = _admit(database, sounds, explicit, replay_prepare)
    assert fresh_after_cancel["candidate_id"] != fresh_after_failure["candidate_id"]


@pytest.mark.parametrize("candidate_count", [True, 1.0, None, 0, 9])
def test_http_generation_schema_rejects_invalid_candidate_count(candidate_count):
    with pytest.raises(ValidationError):
        SoundGenerate.model_validate(
            {"prompt": "rain", "candidate_count": candidate_count}
        )


def test_batch_admission_wraps_seed_and_freezes_each_request(domain, monkeypatch):
    database, _paths, _artifacts, _jobs, sounds, identity = domain
    from pandrator.logic import audio_cpp_sfx

    identity_calls: list[str] = []

    def runtime_identity(_settings, *, model_id):
        identity_calls.append(model_id)
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    request = {
        "prompt": "A quiet electronic music bed",
        "model_id": "ace_step_turbo_q8_0",
        "candidate_count": 3,
        "kind": "music",
        "backend": "cpu",
        "lyrics": "stay with me",
        "parameters": {"bpm": 96, "sampler_mode": "heun"},
        "seed": 2**32 - 1,
    }
    prepared = sounds.prepare_generation(request)
    receipt = _admit(database, sounds, request, prepared)

    assert identity_calls == [request["model_id"]]
    assert receipt["candidate_count"] == 3
    assert receipt["cache_hit"] is False
    children = receipt["candidates"]
    assert [child["seed"] for child in children] == [2**32 - 1, 0, 1]
    assert len({child["candidate_id"] for child in children}) == 3
    assert len({child["job_id"] for child in children}) == 3

    with database.session() as session:
        for child in children:
            job = session.get(Job, child["job_id"])
            candidate = session.get(SoundVariant, child["candidate_id"])
            assert job is not None and candidate is not None
            frozen = job.payload_json["request"]
            assert "candidate_count" not in frozen
            assert frozen["prompt"] == request["prompt"]
            assert frozen["model_id"] == request["model_id"]
            assert frozen["kind"] == request["kind"]
            assert frozen["backend"] == request["backend"]
            assert frozen["lyrics"] == request["lyrics"]
            assert frozen["parameters"]["bpm"] == request["parameters"]["bpm"]
            assert frozen["parameters"]["sampler_mode"] == request["parameters"]["sampler_mode"]
            assert frozen["seed"] == child["seed"]
            assert candidate.provenance_json["seed"] == child["seed"]
            assert job.payload_json["runtime_identity"]["model_id"] == request["model_id"]
            assert "candidate_count" not in candidate.provenance_json

    replay = _admit(database, sounds, request, prepared)
    assert replay["cache_hit"] is True
    assert [child["candidate_id"] for child in replay["candidates"]] == [
        child["candidate_id"] for child in children
    ]
    assert [child["seed"] for child in replay["candidates"]] == [
        child["seed"] for child in children
    ]
    assert identity_calls == [request["model_id"]]


def test_batch_omitted_seed_is_resolved_once(domain, monkeypatch):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    random_calls: list[int] = []

    def random_seed(bits):
        random_calls.append(bits)
        return 37

    monkeypatch.setattr(
        "pandrator.web.sound_generation_admission.secrets.randbits", random_seed
    )
    request = {"prompt": "rain", "candidate_count": 3}
    prepared = sounds.prepare_generation(request)
    receipt = _admit(database, sounds, request, prepared)
    assert random_calls == [32]
    assert [child["seed"] for child in receipt["candidates"]] == [37, 38, 39]


def test_batch_admission_combines_cached_first_candidate_and_new_jobs(domain):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    base = {"prompt": "rain", "seed": 61}
    first = _admit(database, sounds, base, sounds.prepare_generation(base))
    batch = {**base, "candidate_count": 3}
    receipt = _admit(database, sounds, batch, sounds.prepare_generation(batch))

    assert receipt["cache_hit"] is False
    assert [child["cache_hit"] for child in receipt["candidates"]] == [True, False, False]
    # candidate_count is admission-only, so the first child reuses the legacy candidate.
    assert receipt["candidates"][0]["candidate_id"] == first["candidate_id"]


def test_batch_admission_rejects_count_larger_than_seed_space_before_writes(
    domain, monkeypatch
):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    monkeypatch.setattr("pandrator.logic.audio_cpp_sfx.seed_maximum", lambda _model_id: 1)
    request = {"prompt": "rain", "candidate_count": 3, "seed": 0}
    prepared = sounds.prepare_generation(request)
    with pytest.raises(ValueError, match="seed space"):
        _admit(database, sounds, request, prepared)
    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "sound.generate")) is None
        assert session.scalar(select(SoundVariant.id)) is None


def test_default_candidate_count_preserves_single_candidate_receipt_and_payload(domain):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    request = {"prompt": "rain", "seed": 91}
    prepared = sounds.prepare_generation(request)
    receipt = _admit(database, sounds, request, prepared)
    assert prepared.candidate_count == 1
    assert set(receipt) == {
        "candidate_id",
        "job_id",
        "item_id",
        "status",
        "cache_hit",
        "resolved_settings",
    }
    with database.session() as session:
        job = session.get(Job, receipt["job_id"])
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        assert job is not None and candidate is not None
        assert set(job.payload_json) == {"candidate_id", "request", "runtime_identity"}
        assert "candidate_count" not in job.payload_json["request"]
        assert "candidate_count" not in candidate.provenance_json


def test_prepared_batch_count_must_match_submitted_count(domain):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    request = {"prompt": "rain", "candidate_count": 2, "seed": 19}
    prepared = sounds.prepare_generation(request)
    with pytest.raises(ValueError, match="count does not match"):
        _admit(database, sounds, {**request, "candidate_count": 3}, prepared)
    with database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "sound.generate")) is None
        assert session.scalar(select(SoundVariant.id)) is None


def test_music_controls_are_frozen_and_change_the_candidate_fingerprint(domain):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    request = {
        "prompt": "A quiet electronic music bed",
        "model_id": "ace_step_turbo_q8_0",
        "kind": "music",
        "backend": "cpu",
        "lyrics": "stay with me",
        "parameters": {"bpm": 96, "sampler_mode": "heun"},
        "seed": 73,
    }
    prepared = sounds.prepare_generation(request)
    first = _admit(database, sounds, request, prepared)
    assert first["status"] == "queued"

    with database.session() as session:
        job = session.get(Job, first["job_id"])
        candidate = session.get(SoundVariant, first["candidate_id"])
        assert job is not None and candidate is not None
        canonical = job.payload_json["request"]
        for key in ("kind", "backend", "lyrics", "parameters", "seed"):
            assert canonical[key] == prepared.request_template[key]
            if key == "parameters":
                assert canonical[key]["bpm"] == request[key]["bpm"]
                assert canonical[key]["sampler_mode"] == request[key]["sampler_mode"]
                assert candidate.provenance_json[key] == canonical[key]
            else:
                assert candidate.provenance_json[key] == canonical[key]
        assert job.payload_json["runtime_identity"]["model_id"] == request["model_id"]
        assert "audio_cpp_executable" not in str(job.payload_json)
        assert "api_secret" not in str(candidate.provenance_json)

    for control, changed in (
        ("lyrics", "stay beside me"),
        ("parameters", {**request["parameters"], "bpm": 97}),
    ):
        changed_request = {**request, control: changed}
        changed_prepared = sounds.prepare_generation(changed_request)
        changed_receipt = _admit(
            database, sounds, changed_request, changed_prepared
        )
        assert changed_receipt["cache_hit"] is False
        assert changed_receipt["candidate_id"] != first["candidate_id"]


def test_instrumental_adapter_identity_survives_admission_and_public_readback(domain, monkeypatch):
    from pandrator.logic import audio_cpp_yue2_adapter as adapter

    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    request = {
        "prompt": "A quiet piano introduction", "model_id": "yue2_main_q8_0",
        "seed": 73, "parameters": {"instrumental": True},
    }
    prepared = sounds.prepare_generation(request)
    first = _admit(database, sounds, request, prepared)
    repeated = _admit(database, sounds, request, sounds.prepare_generation(request))
    assert repeated["cache_hit"] is True
    assert repeated["candidate_id"] == first["candidate_id"]
    assert first["resolved_settings"]["lyrics"] == "[instrumental]"
    with database.session() as session:
        candidate = session.get(SoundVariant, first["candidate_id"])
        assert candidate.provenance_json["runtime_identity"]["adapters"] == [adapter.identity()]
        assert candidate.provenance_json["parameters"]["instrumental"] is True
    detail = sounds.get_candidate(first["candidate_id"])
    assert detail["provenance"]["parameters"]["instrumental"] is True
    assert detail["provenance"]["runtime_identity"]["asset_manifest"][-1] == {
        "filename": adapter.FILENAME, "sha256": adapter.SHA256,
        "revision": adapter.REVISION, "bytes": adapter.SIZE_BYTES,
    }
    ordinary = {**request, "parameters": {}}
    unadapted = _admit(database, sounds, ordinary, sounds.prepare_generation(ordinary))
    assert unadapted["candidate_id"] != first["candidate_id"]
    monkeypatch.setattr(adapter, "REVISION", "a" * 40)
    changed = _admit(database, sounds, request, sounds.prepare_generation(request))
    assert changed["candidate_id"] != first["candidate_id"]


def test_changed_instrumental_adapter_stops_queued_work_before_generation(domain, monkeypatch):
    from pandrator.logic import audio_cpp_yue2_adapter as adapter

    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    request = {
        "prompt": "Solo piano", "model_id": "yue2_main_q8_0",
        "seed": 73, "parameters": {"instrumental": True},
    }
    receipt = _admit(database, sounds, request, sounds.prepare_generation(request))
    payload, owner = _claim(database, receipt)
    monkeypatch.setattr(adapter, "SHA256", "f" * 64)
    monkeypatch.setattr(
        "pandrator.logic.audio_cpp_sfx.generate_sound",
        lambda *_args, **_kwargs: pytest.fail("changed adapter reached generation"),
    )
    with pytest.raises(RuntimeError, match="changed after admission"):
        _run_with_lease(sounds, payload, owner)


def test_stable_music_request_uses_model_default_duration_without_lyrics(domain):
    _database, _paths, _artifacts, _jobs, sounds, _identity = domain
    prepared = sounds.prepare_generation(
        {"prompt": "Instrumental music bed", "model_id": "stable_audio_3_small_music_q8_0"}
    )
    assert prepared.request_template["duration_seconds"] == 30.0


def test_omitted_music_seed_uses_model_specific_seed_maximum(domain, monkeypatch):
    database, _paths, _artifacts, _jobs, sounds, _identity = domain
    model_id = "controlfoley_large_44k_q8_0"
    maximum = 2**31 - 1
    monkeypatch.setattr(
        "pandrator.logic.audio_cpp_sfx.seed_maximum", lambda requested_model: maximum
    )
    monkeypatch.setattr(
        "pandrator.web.sound_generation_admission.secrets.randbelow",
        lambda upper: upper - 1,
    )
    request = {"prompt": "Soft falling rain", "model_id": model_id}
    receipt = _admit(database, sounds, request, sounds.prepare_generation(request))
    assert receipt["resolved_settings"]["seed"] == maximum
    with database.session() as session:
        job = session.get(Job, receipt["job_id"])
        assert job is not None
        assert job.payload_json["request"]["seed"] == maximum


def test_generation_request_contract_accepts_bounded_music_controls():
    request = SoundGenerate.model_validate(
        {
            "prompt": "m" * 8000,
            "model_id": "stable_audio_3_small_music_q8_0",
            "duration_seconds": 600.0,
            "kind": "music",
            "backend": "vulkan",
            "lyrics": "l" * 16000,
            "parameters": {"steps": 64, "guidance_scale": 3.5, "loop": True},
        }
    )
    assert request.duration_seconds == 600.0
    assert request.kind == "music"
    assert request.backend == "vulkan"
    assert request.parameters == {"steps": 64, "guidance_scale": 3.5, "loop": True}
    assert SoundGenerate.model_validate(
        {"prompt": "music", "model_id": "stable_audio_3_small_music_q8_0"}
    ).duration_seconds is None

    invalid_requests = (
        {"prompt": "m" * 8001},
        {"prompt": "music", "duration_seconds": 601.0},
        {"prompt": "music", "parameters": {"nested": {"path": "/tmp/model"}}},
        {"prompt": "music", "parameters": {f"p{index}": index for index in range(33)}},
        {"prompt": "music", "parameters": {"k" * 65: True}},
        {"prompt": "music", "parameters": {"scale": float("nan")}},
    )
    for payload in invalid_requests:
        with pytest.raises(ValidationError):
            SoundGenerate.model_validate(payload)


@pytest.mark.parametrize("sample_rate", [44100, 48000])
def test_generation_publishes_audio_without_rewriting_and_recovers(
    domain, monkeypatch, sample_rate
):
    database, paths, _artifacts, _jobs, sounds, identity = domain
    request = {"prompt": "distant thunder", "duration_seconds": 1.0, "seed": 77}
    receipt = _admit(database, sounds, request, sounds.prepare_generation(request))
    payload, owner = _claim(database, receipt)
    generated = []

    def fake_generate(request, output_path, *, settings, cancel_event, progress):
        assert settings == {
            "audio_cpp_executable": "/trusted/audio.cpp",
            "audio_cpp_cache_dir": "/trusted/cache",
            "audio_cpp_timeout_seconds": 30.0,
        }
        assert "api_secret" not in settings
        generated.append(output_path)
        progress("model", 1, 1)
        return _write_wave(output_path, frames=sample_rate, sample_rate=sample_rate)

    monkeypatch.setattr(
        "pandrator.web.sound_generation_worker.audio_cpp_sfx.generate_sound", fake_generate
    )
    result = _run_with_lease(sounds, payload, owner)
    assert result["status"] == "ready"
    assert result["job_id"] == receipt["job_id"]
    assert result["artifact_id"]

    with database.session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        artifact = session.get(Artifact, result["artifact_id"])
        job = session.get(Job, receipt["job_id"])
        assert candidate is not None and candidate.state == "ready"
        assert candidate.frame_count == sample_rate
        assert artifact is not None
        assert (artifact.kind, artifact.role, artifact.session_id) == (
            "audio",
            "sound.original",
            None,
        )
        assert artifact.metadata_json["kind"] == "sound"
        assert artifact.metadata_json["model_id"] == "stable_audio_3_small_sfx_q8_0"
        assert artifact.metadata_json["request"]["prompt"] == "distant thunder"
        assert "audio_cpp_executable" not in str(artifact.metadata_json)
        assert job is not None and job.result_json["candidate_id"] == candidate.id
        audio_path = paths.managed_path(artifact.relative_path)
        content_hash = artifact.content_hash
    assert audio_path.is_file()
    assert not generated[0].exists()
    with wave.open(str(audio_path), "rb") as published:
        assert published.getframerate() == sample_rate
        assert published.getnframes() == sample_rate
        assert published.readframes(sample_rate) == b"\x01\x00\x02\x00" * sample_rate
    assert content_hash == hashlib.sha256(audio_path.read_bytes()).hexdigest()

    recovered = _run_with_lease(sounds, payload, owner)
    assert recovered == result
    assert len(generated) == 1
    ready_cache = _admit(database, sounds, request, sounds.prepare_generation(request))
    assert ready_cache["cache_hit"] is True
    assert ready_cache["status"] == "ready"
    assert ready_cache["candidate_id"] == receipt["candidate_id"]


def test_stale_lease_cannot_publish_after_native_work(domain, monkeypatch):
    database, paths, _artifacts, _jobs, sounds, _identity = domain
    request = {"prompt": "glass chime", "duration_seconds": 1.0, "seed": 9}
    receipt = _admit(database, sounds, request, sounds.prepare_generation(request))
    payload, owner = _claim(database, receipt)

    def lose_lease_during_generation(request, output_path, *, settings, cancel_event, progress):
        _write_wave(output_path)
        with database.session() as session:
            job = session.get(Job, receipt["job_id"])
            assert job is not None
            job.lease_generation += 1
        return _write_wave(output_path)

    monkeypatch.setattr(
        "pandrator.web.sound_generation_worker.audio_cpp_sfx.generate_sound",
        lose_lease_during_generation,
    )
    with pytest.raises(RuntimeError, match="lease or cancellation changed"):
        _run_with_lease(sounds, payload, owner)
    with database.session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        artifacts = list(session.scalars(select(Artifact)).all())
        assert candidate is not None and candidate.state == "queued"
        assert candidate.artifact_id is None
        assert artifacts == []
    assert list((paths.root / "sounds" / "audio").glob("*.wav")) == []
    assert list((paths.root / "sounds" / "staging").glob("sound-generate-*.wav")) == []


def test_lease_lost_at_final_publication_fence_rolls_back_own_file(domain, monkeypatch):
    database, paths, _artifacts, _jobs, sounds, _identity = domain
    request = {"prompt": "ceramic bell", "duration_seconds": 1.0, "seed": 41}
    receipt = _admit(database, sounds, request, sounds.prepare_generation(request))
    payload, owner = _claim(database, receipt)

    def fake_generate(request, output_path, *, settings, cancel_event, progress):
        return _write_wave(output_path)

    monkeypatch.setattr(
        "pandrator.web.sound_generation_worker.audio_cpp_sfx.generate_sound",
        fake_generate,
    )
    real_require_job = sound_generation_worker._require_job
    checks = 0

    def lose_lease_on_final_check(session, job_payload, lease, cancel_event, *, check_cancel=True):
        nonlocal checks
        checks += 1
        if checks == 3:
            job = session.get(Job, job_payload["_job_id"], populate_existing=True)
            assert job is not None
            job.lease_generation += 1
        return real_require_job(
            session,
            job_payload,
            lease,
            cancel_event,
            check_cancel=check_cancel,
        )

    monkeypatch.setattr(sound_generation_worker, "_require_job", lose_lease_on_final_check)
    with pytest.raises(RuntimeError, match="lease or cancellation changed"):
        _run_with_lease(sounds, payload, owner)
    assert checks == 3

    with database.session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        assert candidate is not None and candidate.state == "queued"
        assert candidate.artifact_id is None
        assert list(session.scalars(select(Artifact)).all()) == []
    assert list((paths.root / "sounds" / "audio").glob("*.wav")) == []
    assert list((paths.root / "sounds" / "staging").glob("sound-generate-*.wav")) == []


def test_cancellation_and_runtime_identity_change_stop_before_publication(domain, monkeypatch):
    database, paths, _artifacts, _jobs, sounds, identity = domain
    request = {"prompt": "footsteps", "duration_seconds": 1.0, "seed": 4}
    receipt = _admit(database, sounds, request, sounds.prepare_generation(request))
    payload, owner = _claim(database, receipt)

    called = []
    monkeypatch.setattr(
        "pandrator.web.sound_generation_worker.audio_cpp_sfx.generate_sound",
        lambda *_args, **_kwargs: called.append(True),
    )
    canceled = threading.Event()
    canceled.set()
    with pytest.raises(RuntimeError, match="lease or cancellation changed"):
        _run_with_lease(sounds, payload, owner, cancel_event=canceled)
    assert called == []

    new_identity = {**identity, "runtime_sha256": "c" * 64}
    monkeypatch.setattr(
        "pandrator.logic.audio_cpp_sfx.runtime_identity",
        lambda _settings: new_identity,
    )
    with pytest.raises(RuntimeError, match="changed after admission"):
        _run_with_lease(sounds, payload, owner)
    with database.session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        assert candidate is not None and candidate.state == "queued"
        assert candidate.artifact_id is None
    assert list((paths.root / "sounds" / "audio").glob("*.wav")) == []
