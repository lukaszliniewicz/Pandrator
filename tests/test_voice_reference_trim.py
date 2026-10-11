from __future__ import annotations

import json
import shutil
import sys
import threading
import types
import wave
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.logic import tts_handler
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.media_process import MediaProcessCancelled
from pandrator.web.models import Artifact, ArtifactEdge, Voice, VoiceSample
from pandrator.web.voice_lifecycle_schemas import VoiceReferenceImportRequest
from pandrator.web.voice_reference_trim import (
    VoiceReferenceAudioInfo,
    VoiceReferenceTrimError,
    inspect_voice_reference_trim,
    probe_voice_reference_audio,
    validate_voice_reference_trim,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def voice_app(tmp_path):
    prepare_web_test_data_root(str(tmp_path / "data"))
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=str(tmp_path / "data"),
        testing=True,
        bootstrap_tokens=bootstrap,
    )
    client = app.test_client()
    csrf = client.post("/api/v1/auth/bootstrap", json={"token": token}).get_json()["csrf_token"]
    yield client, app.extensions["pandrator"], csrf
    app.extensions["pandrator"]["database"].dispose()


def _headers(csrf: str, key: str | None = None) -> dict[str, str]:
    headers = {"X-CSRF-Token": csrf}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _make_voice(client, csrf: str) -> dict:
    response = client.post(
        "/api/v1/voices",
        json={"name": "Cropped reference"},
        headers=_headers(csrf),
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _register_wav(extension, *, name: str, metadata: dict | None = None):
    source = extension["paths"].uploads / name
    source.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 8000
    frame_count = sample_rate * 4
    with wave.open(str(source), "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"\x01\x00" * frame_count)
    artifact = extension["artifacts"].register(
        source,
        kind="audio",
        role="recording_upload",
        metadata=metadata,
    )
    _, managed_source = extension["artifacts"].resolve(artifact.id)
    return artifact, managed_source, managed_source.read_bytes()


def _queue_import(client, csrf, voice, artifact, *, start=1.0, end=3.0, key=None, **extra):
    request = {
        "artifact_id": artifact.id,
        "expected_voice_revision": voice["revision"],
        "start_seconds": start,
        "end_seconds": end,
        **extra,
    }
    return client.post(
        f"/api/v1/voices/{voice['id']}/samples/from-artifact",
        json=request,
        headers=_headers(csrf, key),
    )


def _wave_shape(path: Path) -> tuple[int, int, int]:
    with wave.open(str(path), "rb") as audio:
        return audio.getnchannels(), audio.getframerate(), audio.getnframes()


def test_import_schema_requires_finite_ordered_crop_bounds():
    default = VoiceReferenceImportRequest.model_validate(
        {"artifact_id": "a", "expected_voice_revision": 1}
    )
    assert default.start_seconds == 0.0
    assert default.end_seconds is None

    accepted = VoiceReferenceImportRequest.model_validate(
        {
            "artifact_id": "a",
            "expected_voice_revision": 1,
            "start_seconds": 1,
            "end_seconds": 2,
        }
    )
    assert accepted.start_seconds == 1.0
    assert accepted.end_seconds == 2.0

    for start, end in (
        (-1, 2),
        (True, 2),
        (1, 1),
        (1, 0),
        (1, float("nan")),
        (1, float("inf")),
        ("1", 2),
    ):
        with pytest.raises(ValueError):
            VoiceReferenceImportRequest.model_validate(
                {
                    "artifact_id": "a",
                    "expected_voice_revision": 1,
                    "start_seconds": start,
                    "end_seconds": end,
                }
            )


def test_crop_validation_allows_one_frame_end_tolerance_only():
    info = VoiceReferenceAudioInfo(Decimal("4"), 8000, 1)
    accepted = validate_voice_reference_trim(1.0, 4.000125, info)
    assert accepted.end_seconds == Decimal("4")
    assert accepted.requested_end_seconds == Decimal("4.000125")

    with pytest.raises(VoiceReferenceTrimError, match="exceeds"):
        validate_voice_reference_trim(1.0, 4.00025, info)
    with pytest.raises(VoiceReferenceTrimError, match="before the end"):
        validate_voice_reference_trim(4.0, None, info)


def test_crop_probe_honors_existing_managed_audio_duration_limit(monkeypatch, tmp_path):
    result = types.SimpleNamespace(
        stdout=json.dumps(
            {
                "streams": [
                    {
                        "duration_ts": "2408000",
                        "time_base": "1/8000",
                        "sample_rate": "8000",
                        "channels": 1,
                    }
                ],
                "format": {"duration": "301.000"},
            }
        )
    )
    monkeypatch.setattr(
        "pandrator.web.voice_reference_trim.run_media_process",
        lambda *args, **kwargs: result,
    )
    with pytest.raises(VoiceReferenceTrimError, match="300 seconds"):
        probe_voice_reference_audio(tmp_path / "managed.wav")


def test_crop_probe_and_worker_respect_cancellation(tmp_path):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(MediaProcessCancelled):
        inspect_voice_reference_trim(
            tmp_path / "not-read.wav",
            0.5,
            1.0,
            cancel_event=cancel,
        )


def test_import_crops_then_normalizes_preserving_source_and_crop_transcript(
    voice_app,
):
    client, extension, csrf = voice_app
    voice = _make_voice(client, csrf)
    source_artifact, source_path, original = _register_wav(
        extension,
        name="long-reference.wav",
        metadata={
            "transcript": "This transcript describes the full recording.",
            "transcript_reviewed": True,
        },
    )
    response = _queue_import(
        client,
        csrf,
        voice,
        source_artifact,
        start=1.0,
        end=3.0,
        transcript="Words from this crop.",
        transcript_reviewed=True,
    )
    assert response.status_code == 202, response.get_json()
    job_payload = response.get_json()["payload_json"]
    assert job_payload["start_seconds"] == 1.0
    assert job_payload["end_seconds"] == 3.0
    assert job_payload["sample_provenance"]["reference_trim"] == {
        "start_seconds": 1.0,
        "end_seconds": 3.0,
        "requested_end_seconds": 3.0,
        "source_duration_seconds": 4.0,
    }

    result = extension["workflow_handlers"].normalize_voice_recording(
        job_payload,
        lambda *_args: None,
        threading.Event(),
    )
    sample_artifact, sample_path = extension["artifacts"].resolve(result["artifact_id"])
    assert _wave_shape(sample_path) == (1, 24000, 48000)
    assert source_path.read_bytes() == original
    assert sample_artifact.role == "voice_sample"
    assert (
        sample_artifact.metadata_json["sample_provenance"]["reference_trim"]["start_seconds"] == 1.0
    )
    with extension["database"].session() as session:
        sample = session.get(VoiceSample, result["sample_id"])
        edge = session.scalar(
            select(ArtifactEdge).where(
                ArtifactEdge.parent_artifact_id == source_artifact.id,
                ArtifactEdge.child_artifact_id == sample_artifact.id,
            )
        )
        assert edge is not None
        assert sample.transcript == "Words from this crop."
        assert sample.transcript_reviewed

    # The separate existing transcription route points ASR at the cropped sample.
    transcription = client.post(
        f"/api/v1/voices/{voice['id']}/samples/{result['sample_id']}/transcribe",
        json={},
        headers=_headers(csrf),
    )
    assert transcription.status_code == 202, transcription.get_json()
    assert transcription.get_json()["payload_json"]["sample_artifact_id"] == sample_artifact.id


def test_crop_is_applied_before_optional_noise_reduction(voice_app, monkeypatch):
    client, extension, csrf = voice_app
    voice = _make_voice(client, csrf)
    source_artifact, _source_path, _original = _register_wav(
        extension, name="cleanup-reference.wav"
    )
    queued = _queue_import(client, csrf, voice, source_artifact)
    assert queued.status_code == 202, queued.get_json()
    job_payload = queued.get_json()["payload_json"]
    job_payload["noise_reduction"] = "deepfilternet2"
    cleanup_input = {}

    def clean_voice_sample(source, destination, settings, cancel_event=None, progress=None):
        with wave.open(str(source), "rb") as audio:
            cleanup_input.update(
                sample_rate=audio.getframerate(),
                frames=audio.getnframes(),
                channels=audio.getnchannels(),
            )
        shutil.copy2(source, destination)
        if progress is not None:
            progress("clean", 1, 1)
        return {"method": "test double"}

    cleanup_module = types.ModuleType("pandrator.logic.audio_cpp_processing")
    cleanup_module.__dict__["clean_voice_sample"] = clean_voice_sample
    monkeypatch.setitem(sys.modules, "pandrator.logic.audio_cpp_processing", cleanup_module)

    result = extension["workflow_handlers"].normalize_voice_recording(
        job_payload,
        lambda *_args: None,
        threading.Event(),
    )
    _, output = extension["artifacts"].resolve(result["artifact_id"])
    assert cleanup_input == {"sample_rate": 48000, "frames": 96000, "channels": 1}
    assert _wave_shape(output) == (1, 24000, 48000)


def test_cropped_reference_does_not_inherit_source_transcript(voice_app):
    client, extension, csrf = voice_app
    voice = _make_voice(client, csrf)
    source_artifact, _source_path, _original = _register_wav(
        extension,
        name="transcript-is-source-scoped.wav",
        metadata={
            "transcript": "This transcript describes the full recording.",
            "transcript_reviewed": True,
        },
    )
    response = _queue_import(
        client,
        csrf,
        voice,
        source_artifact,
        start=0.5,
        end=2.5,
    )
    assert response.status_code == 202, response.get_json()
    result = extension["workflow_handlers"].normalize_voice_recording(
        response.get_json()["payload_json"],
        lambda *_args: None,
        threading.Event(),
    )
    with extension["database"].session() as session:
        sample = session.get(VoiceSample, result["sample_id"])
        assert sample.transcript is None
        assert not sample.transcript_reviewed


def test_crop_rejects_bounds_beyond_one_frame_without_queuing(voice_app):
    client, extension, csrf = voice_app
    voice = _make_voice(client, csrf)
    source_artifact, _source_path, _original = _register_wav(
        extension, name="bounded-reference.wav"
    )
    response = _queue_import(
        client,
        csrf,
        voice,
        source_artifact,
        start=1.0,
        end=4.00025,
    )
    assert response.status_code == 422
    assert response.get_json()["error"]["code"] == "validation_error"


def test_worker_rechecks_source_hash_and_voice_revision_before_publication(voice_app):
    client, extension, csrf = voice_app
    voice = _make_voice(client, csrf)

    for mutation in ("source", "revision"):
        source_artifact, source_path, _original = _register_wav(
            extension, name=f"{mutation}-guard.wav"
        )
        response = _queue_import(client, csrf, voice, source_artifact)
        assert response.status_code == 202, response.get_json()
        if mutation == "source":
            source_path.write_bytes(b"changed after preflight")
        else:
            with extension["database"].session() as session:
                session.get(Voice, voice["id"]).revision += 1
            voice["revision"] += 1

        with pytest.raises(ValueError, match="changed|voice changed"):
            extension["workflow_handlers"].normalize_voice_recording(
                response.get_json()["payload_json"],
                lambda *_args: None,
                threading.Event(),
            )
        with extension["database"].session() as session:
            assert not session.scalars(select(VoiceSample)).all()


def test_crop_import_idempotency_replays_after_source_retirement(voice_app):
    client, extension, csrf = voice_app
    voice = _make_voice(client, csrf)
    source_artifact, _source_path, _original = _register_wav(
        extension, name="idempotent-reference.wav"
    )
    first = _queue_import(
        client,
        csrf,
        voice,
        source_artifact,
        start=0.5,
        end=2.5,
        key="crop-import-idempotency",
    )
    assert first.status_code == 202, first.get_json()
    with extension["database"].session() as session:
        session.get(Artifact, source_artifact.id).state = "deleted"
        session.get(Voice, voice["id"]).revision += 1
    replay = _queue_import(
        client,
        csrf,
        voice,
        source_artifact,
        start=0.5,
        end=2.5,
        key="crop-import-idempotency",
    )
    assert replay.status_code == 202, replay.get_json()
    assert replay.headers["Idempotency-Replayed"] == "true"
    assert replay.get_json()["id"] == first.get_json()["id"]


def test_audiovae_encoder_capacity_error_is_not_retried():
    response = tts_handler.requests.Response()
    response.status_code = 500
    response._content = json.dumps(
        {"error": {"message": "AudioVAE encoder sample capacity exceeded"}}
    ).encode("utf-8")
    with (
        patch(
            "pandrator.logic.tts_handler._request_openai_compatible_audio",
            return_value=response,
        ) as request,
        patch("pandrator.logic.tts_handler.wait_for_retry") as wait,
        pytest.raises(tts_handler.TtsGenerationError, match="sample capacity exceeded"),
    ):
        tts_handler.text_to_audio(
            "Too long for the encoder.",
            {"service": "audio.cpp", "model": "voxcpm2_q8_0"},
            max_attempts=5,
        )
    assert request.call_count == 1
    wait.assert_not_called()
