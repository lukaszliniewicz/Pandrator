"""HTTP acceptance for shared sound generation and library routes."""

from __future__ import annotations

import io
import wave
from urllib.parse import quote

import pytest
from sqlalchemy import func, select

from pandrator.logic import audio_cpp_sfx
from pandrator.logic.audio_cpp_generation_models import get_model as get_audio_cpp_generation_model
from pandrator.web import audio_import_routes
from pandrator.web.api import create_app
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, SoundVariant


@pytest.fixture(scope="session")
def case(tmp_path_factory):
    root = tmp_path_factory.mktemp("sound-http") / "data"
    app = create_app(data_root=root, testing=True, background_maintenance=False)
    services = app.extensions["pandrator"]
    scopes = {
        "read": ("app.read",),
        "write": ("app.read", "app.write"),
        "run": ("app.read", "app.run"),
        "full": ("app.read", "app.write", "app.run"),
    }
    tokens = {
        name: services["auth"].create_api_token(f"sound-http-{name}", scopes=value)[1]
        for name, value in scopes.items()
    }

    def headers(scope, key=None):
        result = {"Authorization": f"Bearer {tokens[scope]}"}
        if key:
            result["Idempotency-Key"] = key
        return result

    yield {"client": app.test_client(), "services": services, "headers": headers}
    services["database"].dispose()


def _wav(sample: bytes = b"\x00\x00") -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(8000)
        audio.writeframes(sample * 800)
    return output.getvalue()


def _raw_import(case, data: bytes, filename: str, key: str):
    headers = {
        **case["headers"]("full", key),
        "Content-Type": "application/octet-stream",
        "X-Audio-Filename": quote(filename, safe=""),
    }
    return case["client"].put("/api/v1/sounds/import", data=data, headers=headers)


def _imported_artifact(case, candidate_id: str) -> Artifact:
    services = case["services"]
    with services["database"].session() as session:
        variant = session.get(SoundVariant, candidate_id)
        assert variant is not None and variant.artifact_id
        artifact = session.get(Artifact, variant.artifact_id)
        assert artifact is not None
        return artifact


def test_rejected_backend_does_not_create_generation_job(case, monkeypatch):
    def reject_backend(_settings, backend):
        assert backend == "cuda"
        raise audio_cpp_sfx.AudioCppSfxError("CUDA runtime unavailable")

    monkeypatch.setattr(audio_cpp_sfx, "require_backend", reject_backend)
    services = case["services"]
    with services["database"].session() as session:
        before = {
            Job: session.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "sound.generate")
            ),
            SoundVariant: session.scalar(select(func.count()).select_from(SoundVariant)),
        }
    response = case["client"].post(
        "/api/v1/sounds/generate",
        json={
            "prompt": "A short instrumental phrase",
            "model_id": "minimax_h3_q4_k_int8_dit",
            "kind": "music",
            "backend": "cuda",
            "seed": 17,
        },
        headers=case["headers"]("run", "sound-rejected-backend-001"),
    )
    assert response.status_code == 422
    with services["database"].session() as session:
        after = {
            Job: session.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "sound.generate")
            ),
            SoundVariant: session.scalar(select(func.count()).select_from(SoundVariant)),
        }
    assert after == before


def test_http_accepts_cuda_and_uses_h3_int8_model_backend_default(case, monkeypatch):
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "minimax_h3_q4_k_int8_dit",
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "h3-test",
        "minimum_runtime_version": "0.9.0",
    }

    def runtime_identity(_settings, *, model_id):
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
    requests = (
        (
            {
                "prompt": "An instrumental phrase",
                "model_id": "minimax_h3_q4_k_int8_dit",
                "kind": "music",
                "backend": "cuda",
                "seed": 21,
            },
            "sound-h3-cuda-explicit-001",
        ),
        (
            {
                "prompt": "A second instrumental phrase",
                "model_id": "minimax_h3_q4_k_int8_dit",
                "kind": "music",
                "seed": 22,
            },
            "sound-h3-cuda-default-001",
        ),
    )
    for request, key in requests:
        response = case["client"].post(
            "/api/v1/sounds/generate",
            json=request,
            headers=case["headers"]("run", key),
        )
        assert response.status_code == 202, response.get_json()
        receipt = response.get_json()
        assert receipt["resolved_settings"]["backend"] == "cuda"
        with case["services"]["database"].session() as session:
            job = session.get(Job, receipt["job_id"])
            candidate = session.get(SoundVariant, receipt["candidate_id"])
            assert job is not None and candidate is not None
            assert job.payload_json["request"]["backend"] == "cuda"
            assert candidate.provenance_json["backend"] == "cuda"


def test_http_batch_generation_replays_all_candidates_and_conflicts_on_count(case, monkeypatch):
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "ace_step_turbo_q8_0",
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "ace-test",
        "minimum_runtime_version": "0.9.0",
    }
    identity_calls: list[str] = []

    def runtime_identity(_settings, *, model_id):
        identity_calls.append(model_id)
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
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
    key = "sound-batch-replay-001"
    first_response = case["client"].post(
        "/api/v1/sounds/generate",
        json=request,
        headers=case["headers"]("run", key),
    )
    assert first_response.status_code == 202, first_response.get_json()
    first = first_response.get_json()
    candidates = first["candidates"]
    assert first["candidate_count"] == 3
    assert first["cache_hit"] is False
    assert [candidate["seed"] for candidate in candidates] == [2**32 - 1, 0, 1]
    assert len({candidate["candidate_id"] for candidate in candidates}) == 3
    assert len({candidate["job_id"] for candidate in candidates}) == 3
    assert identity_calls == [request["model_id"]]

    with case["services"]["database"].session() as session:
        for child in candidates:
            job = session.get(Job, child["job_id"])
            candidate = session.get(SoundVariant, child["candidate_id"])
            assert job is not None and candidate is not None
            frozen = job.payload_json["request"]
            assert frozen["prompt"] == request["prompt"]
            assert frozen["model_id"] == request["model_id"]
            assert frozen["kind"] == request["kind"]
            assert frozen["backend"] == request["backend"]
            assert frozen["lyrics"] == request["lyrics"]
            assert frozen["parameters"]["bpm"] == request["parameters"]["bpm"]
            assert frozen["parameters"]["sampler_mode"] == request["parameters"]["sampler_mode"]
            assert frozen["seed"] == child["seed"]
            assert "candidate_count" not in frozen
            assert candidate.provenance_json["seed"] == child["seed"]

    replay = case["client"].post(
        "/api/v1/sounds/generate",
        json=request,
        headers=case["headers"]("run", key),
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first
    assert identity_calls == [request["model_id"]]

    conflict = case["client"].post(
        "/api/v1/sounds/generate",
        json={**request, "candidate_count": 2},
        headers=case["headers"]("run", key),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"
    assert identity_calls == [request["model_id"]]


def test_http_batch_enqueue_failure_rolls_back_and_releases_idempotency(case, monkeypatch):
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "test",
        "minimum_runtime_version": "0.9.0",
    }
    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", lambda _settings: identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
    original_enqueue = JobQueue.enqueue_in_session
    calls = 0

    def fail_second_enqueue(self, session, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("simulated second candidate enqueue failure")
        return original_enqueue(self, session, *args, **kwargs)

    monkeypatch.setattr(JobQueue, "enqueue_in_session", fail_second_enqueue)
    services = case["services"]
    with services["database"].session() as session:
        before = (
            session.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "sound.generate")
            ),
            session.scalar(select(func.count()).select_from(SoundVariant)),
        )
    request = {"prompt": "rain", "candidate_count": 3, "seed": 51}
    key = "sound-batch-rollback-001"
    failed = case["client"].post(
        "/api/v1/sounds/generate",
        json=request,
        headers=case["headers"]("run", key),
    )
    assert failed.status_code != 202
    assert calls == 2
    with services["database"].session() as session:
        after_failure = (
            session.scalar(
                select(func.count()).select_from(Job).where(Job.kind == "sound.generate")
            ),
            session.scalar(select(func.count()).select_from(SoundVariant)),
        )
    assert after_failure == before

    monkeypatch.setattr(JobQueue, "enqueue_in_session", original_enqueue)
    retried = case["client"].post(
        "/api/v1/sounds/generate",
        json=request,
        headers=case["headers"]("run", key),
    )
    assert retried.status_code == 202, retried.get_json()
    assert retried.get_json()["candidate_count"] == 3
    assert len(retried.get_json()["candidates"]) == 3


def test_music_route_freezes_validated_generation_controls(case, monkeypatch):
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "stable_audio_3_small_music_q8_0",
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "music-test",
        "minimum_runtime_version": "0.9.0",
    }

    def runtime_identity(_settings, *, model_id):
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
    request = {
        "prompt": "A quiet electronic music bed",
        "model_id": "ace_step_turbo_q8_0",
        "kind": "music",
        "backend": "cpu",
        "lyrics": "stay with me",
        "parameters": {"bpm": 96, "sampler_mode": "heun"},
        "seed": 73,
    }
    response = case["client"].post(
        "/api/v1/sounds/generate",
        json=request,
        headers=case["headers"]("run", "sound-generate-music-001"),
    )
    assert response.status_code == 202, response.get_json()
    receipt = response.get_json()
    assert receipt["resolved_settings"]["kind"] == "music"
    services = case["services"]
    with services["database"].session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        job = session.get(Job, receipt["job_id"])
        assert candidate is not None and job is not None
        canonical = job.payload_json["request"]
        for key in ("kind", "backend", "lyrics", "seed"):
            assert canonical[key] == request[key]
            assert candidate.provenance_json[key] == request[key]
        assert canonical["parameters"]["bpm"] == request["parameters"]["bpm"]
        assert canonical["parameters"]["sampler_mode"] == request["parameters"]["sampler_mode"]
        assert candidate.provenance_json["parameters"] == canonical["parameters"]
        assert job.payload_json["runtime_identity"]["model_id"] == request["model_id"]


def test_music_admission_provenance_roundtrips_through_candidate_and_item_http_get(
    case, monkeypatch
):
    model = get_audio_cpp_generation_model("minimax_music3_q4_0")
    runtime_root = "/private/runtime/audio_cpp"
    model_assets = [
        {"path": asset.path, "size_bytes": asset.size_bytes, "sha256": asset.sha256}
        for asset in model.files
    ]
    model_assets.append(
        {
            "path": f"{runtime_root}/models/private.gguf",
            "size_bytes": 1,
            "sha256": "f" * 64,
        }
    )
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": model.id,
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": model.revision,
        "minimum_runtime_version": "0.9.0",
        "model_assets_revision": model.revision,
        "model_assets": model_assets,
        "runtime_path": runtime_root,
    }

    def runtime_identity(_settings, *, model_id):
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
    request = {
        "prompt": "A quiet electronic instrumental",
        "model_id": model.id,
        "kind": "music",
        "backend": "cpu",
        "lyrics": "stay with me",
        "parameters": {"num_inference_steps": 12, "top_k": 64},
        "seed": 73,
    }
    response = case["client"].post(
        "/api/v1/sounds/generate",
        json=request,
        headers=case["headers"]("run", "sound-generate-music-detail-001"),
    )
    assert response.status_code == 202, response.get_json()
    receipt = response.get_json()
    services = case["services"]
    with services["database"].session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        job = session.get(Job, receipt["job_id"])
        assert candidate is not None and job is not None
        canonical = job.payload_json["request"]
        assert canonical["kind"] == "music"
        assert canonical["lyrics"] == request["lyrics"]

    # Use disposable imported fixture audio to make the admitted candidate savable;
    # this test does not start or invoke the generation worker.
    imported = _raw_import(case, _wav(), "detail-fixture.wav", "sound-music-detail-import-001")
    assert imported.status_code == 201, imported.get_json()
    fixture_artifact = _imported_artifact(case, imported.get_json()["id"])
    with services["database"].immediate_session() as session:
        admitted_candidate = session.get(SoundVariant, receipt["candidate_id"])
        assert admitted_candidate is not None
        admitted_candidate.state = "ready"
        admitted_candidate.artifact_id = fixture_artifact.id

    saved = case["client"].post(
        "/api/v1/sounds/save",
        json={"candidate_id": receipt["candidate_id"], "name": "Music detail fixture"},
        headers=case["headers"]("full", "sound-music-detail-save-001"),
    )
    assert saved.status_code == 200, saved.get_json()
    item_id = saved.get_json()["item_id"]
    read_headers = case["headers"]("read")
    candidate_detail_response = case["client"].get(
        f"/api/v1/sounds/candidates/{receipt['candidate_id']}", headers=read_headers
    )
    item_detail_response = case["client"].get(f"/api/v1/sounds/{item_id}", headers=read_headers)
    assert candidate_detail_response.status_code == 200
    assert item_detail_response.status_code == 200
    candidate_detail = candidate_detail_response.get_json()
    item_detail = item_detail_response.get_json()
    candidate_provenance = candidate_detail["provenance"]
    item_provenance = item_detail["preferred_variant"]["provenance"]
    assert candidate_provenance["kind"] == item_provenance["kind"] == "music"
    assert candidate_provenance["lyrics"] == item_provenance["lyrics"] == request["lyrics"]
    assert (
        candidate_provenance["parameters"]
        == item_provenance["parameters"]
        == canonical["parameters"]
    )

    expected_manifest = [
        {
            "filename": asset.path,
            "bytes": asset.size_bytes,
            "sha256": asset.sha256,
            "revision": model.revision,
        }
        for asset in model.files
    ]
    candidate_identity = candidate_provenance["runtime_identity"]
    item_identity = item_provenance["runtime_identity"]
    assert len(expected_manifest) == 13
    assert (
        candidate_identity["asset_manifest"] == item_identity["asset_manifest"] == expected_manifest
    )
    assert "model_assets" not in candidate_identity
    assert runtime_root not in str(candidate_detail)
    assert runtime_root not in str(item_detail)
    assert "private.gguf" not in str(candidate_detail)
    assert "private.gguf" not in str(item_detail)

    candidate_list = (
        case["client"].get("/api/v1/sounds/candidates?limit=100", headers=read_headers).get_json()
    )
    compact_candidate = next(
        item for item in candidate_list["items"] if item["id"] == receipt["candidate_id"]
    )
    sound_list = case["client"].get("/api/v1/sounds?limit=100", headers=read_headers).get_json()
    compact_item = next(item for item in sound_list["items"] if item["id"] == item_id)
    for compact in (
        compact_candidate["provenance"],
        compact_item["preferred_variant"]["provenance"],
    ):
        assert compact["kind"] == "music"
        assert "lyrics" not in compact
        assert "parameters" not in compact
        assert "runtime_identity" not in compact


def test_generation_replay_avoids_runtime_and_pinned_seed_reuses_candidate(case, monkeypatch):
    seeds = iter((123, 456))
    monkeypatch.setattr(
        "pandrator.web.sound_generation_admission.secrets.randbits", lambda _bits: next(seeds)
    )
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "runtime_version": "0.9.1",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "test",
        "minimum_runtime_version": "0.9.0",
    }
    state = {"calls": 0, "available": True}

    def runtime_identity(_settings):
        state["calls"] += 1
        if not state["available"]:
            raise audio_cpp_sfx.AudioCppSfxError("test runtime unavailable")
        return dict(identity)

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
    client, headers = case["client"], case["headers"]
    body = {"prompt": "HTTP idempotent rain", "duration_seconds": 1.0}
    first = client.post(
        "/api/v1/sounds/generate", json=body, headers=headers("run", "sound-generate-replay-001")
    )
    assert first.status_code == 202, first.get_json()
    receipt = first.get_json()
    seed = receipt["resolved_settings"]["seed"]
    assert isinstance(seed, int) and 0 <= seed <= 2**32 - 1

    state["available"] = False
    replay = client.post(
        "/api/v1/sounds/generate", json=body, headers=headers("run", "sound-generate-replay-001")
    )
    assert replay.status_code == 202
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == receipt
    conflict = client.post(
        "/api/v1/sounds/generate",
        json={**body, "prompt": "different"},
        headers=headers("run", "sound-generate-replay-001"),
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"
    assert state["calls"] == 1

    state["available"] = True
    cached = client.post(
        "/api/v1/sounds/generate",
        json={**body, "seed": seed},
        headers=headers("run", "sound-generate-pinned-001"),
    )
    assert cached.status_code == 202, cached.get_json()
    assert cached.get_json()["cache_hit"] is True
    assert (cached.get_json()["candidate_id"], cached.get_json()["job_id"]) == (
        receipt["candidate_id"],
        receipt["job_id"],
    )
    assert cached.get_json()["resolved_settings"]["seed"] == seed
    fresh = client.post(
        "/api/v1/sounds/generate",
        json=body,
        headers=headers("run", "sound-generate-fresh-002"),
    )
    assert fresh.status_code == 202, fresh.get_json()
    assert fresh.get_json()["resolved_settings"]["seed"] == 456
    assert fresh.get_json()["candidate_id"] != receipt["candidate_id"]
    assert fresh.get_json()["cache_hit"] is False
    fresh_replay = client.post(
        "/api/v1/sounds/generate",
        json=body,
        headers=headers("run", "sound-generate-fresh-002"),
    )
    assert fresh_replay.get_json() == fresh.get_json()
    assert fresh_replay.headers["Idempotency-Replayed"] == "true"
    services = case["services"]
    with services["database"].session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        job = session.get(Job, receipt["job_id"])
        assert candidate is not None and candidate.provenance_json["seed"] == seed
        assert job is not None and job.kind == "sound.generate"
        assert job.payload_json["request"]["seed"] == seed
        assert (
            session.scalar(
                select(func.count())
                .select_from(SoundVariant)
                .where(SoundVariant.fingerprint == candidate.fingerprint)
            )
            == 1
        )
        assert session.scalar(select(func.count()).select_from(Job).where(Job.id == job.id)) == 1
    assert state["calls"] == 3


def test_audio_import_routes_replay_lock_roles_reject_bad_uploads_and_accept_reference(
    case, monkeypatch
):
    client, services = case["client"], case["services"]
    data, changed = _wav(), _wav(b"\x01\x00")
    headers = {**case["headers"]("full", "sound-multipart-001")}
    multipart = client.post(
        "/api/v1/sounds/import",
        data={
            "file": (io.BytesIO(data), "bell.wav"),
            "role": "voice.reference",
            "source_kind": "forged",
        },
        headers=headers,
        content_type="multipart/form-data",
    )
    assert multipart.status_code == 201, multipart.get_json()
    artifact = _imported_artifact(case, multipart.get_json()["id"])
    assert (artifact.kind, artifact.role, artifact.session_id) == ("audio", "sound.original", None)

    first = _raw_import(case, data, "rain.wav", "sound-raw-replay-001")
    replay = _raw_import(case, data, "rain.wav", "sound-raw-replay-001")
    assert first.status_code == replay.status_code == 201
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json() == first.get_json()
    raw_artifact = _imported_artifact(case, first.get_json()["id"])
    assert (raw_artifact.kind, raw_artifact.role, raw_artifact.session_id) == (
        "audio",
        "sound.original",
        None,
    )
    conflict = _raw_import(case, changed, "rain.wav", "sound-raw-replay-001")
    assert conflict.status_code == 409
    assert conflict.get_json()["error"]["code"] == "idempotency_conflict"

    reference = client.put(
        "/api/v1/voices/reference-audio/import",
        data=data,
        headers={
            **case["headers"]("full", "sound-voice-reference-001"),
            "Content-Type": "application/octet-stream",
            "X-Audio-Filename": "reference.wav",
        },
    )
    assert reference.status_code == 201, reference.get_json()
    with services["database"].session() as session:
        managed = session.get(Artifact, reference.get_json()["artifact_id"])
        assert managed is not None
        assert (managed.kind, managed.role, managed.session_id) == (
            "audio",
            "voice.reference",
            None,
        )
        managed_id = managed.id
    voice = client.post(
        "/api/v1/voices",
        json={"name": "HTTP imported reference"},
        headers=case["headers"]("full", "sound-create-voice-001"),
    )
    assert voice.status_code == 201, voice.get_json()
    accepted = client.post(
        f"/api/v1/voices/{voice.get_json()['id']}/samples/from-artifact",
        json={"artifact_id": managed_id, "expected_voice_revision": voice.get_json()["revision"]},
        headers=case["headers"]("full", "sound-reference-guard-001"),
    )
    assert accepted.status_code == 202, accepted.get_json()
    with services["database"].session() as session:
        job = next(
            (
                item
                for item in session.scalars(
                    select(Job).where(Job.kind == "voice.normalize_recording")
                )
                if item.payload_json.get("source_artifact_id") == managed_id
            ),
            None,
        )
        assert job is not None
        assert job.payload_json["source_artifact_role"] == "voice.reference"
        assert job.payload_json["sample_provenance"]["source_kind"] == "imported_voice_reference"

    bad_name = client.put(
        "/api/v1/sounds/import",
        data=b"x",
        headers={
            **case["headers"]("full", "sound-bad-name-001"),
            "Content-Type": "application/octet-stream",
            "X-Audio-Filename": quote("../evil.wav", safe=""),
        },
    )
    assert bad_name.status_code == 422
    invalid = _raw_import(case, b"not a wave", "invalid.wav", "sound-invalid-audio-001")
    assert invalid.status_code == 422
    monkeypatch.setattr(audio_import_routes, "MAX_AUDIO_BYTES", 64)
    oversized = _raw_import(case, b"x" * 65, "large.wav", "sound-oversize-001")
    assert oversized.status_code == 422
    uploads = services["paths"].root / "sounds" / "uploads"
    staging = services["paths"].root / "sounds" / "staging"
    assert not list(uploads.rglob("*"))
    assert not list(staging.iterdir())


def test_sound_reads_do_not_hash_or_probe_managed_audio(case, monkeypatch):
    imported = _raw_import(case, _wav(), "read-check.wav", "sound-read-import-001")
    assert imported.status_code == 201
    candidate_id = imported.get_json()["id"]
    saved = case["client"].post(
        "/api/v1/sounds/save",
        json={"candidate_id": candidate_id, "name": "Read check"},
        headers=case["headers"]("full", "sound-read-save-001"),
    )
    assert saved.status_code == 200, saved.get_json()
    item_id = saved.get_json()["item_id"]
    monkeypatch.setattr(
        "pandrator.web.audio_imports.probe_audio_stream",
        lambda *_: pytest.fail("read probed audio"),
    )
    monkeypatch.setattr(
        "pandrator.web.audio_imports._copy_and_hash", lambda *_: pytest.fail("read hashed audio")
    )
    monkeypatch.setattr(
        "pandrator.web.artifacts.sha256_file", lambda *_: pytest.fail("read hashed artifact")
    )
    client, headers = case["client"], case["headers"]("read")
    assert client.get("/api/v1/sounds", headers=headers).status_code == 200
    assert client.get("/api/v1/sounds/candidates", headers=headers).status_code == 200
    assert (
        client.get(f"/api/v1/sounds/candidates/{candidate_id}", headers=headers).status_code == 200
    )
    assert client.get(f"/api/v1/sounds/{item_id}", headers=headers).status_code == 200
