"""Typed workspace ownership at library job admission boundaries."""

from __future__ import annotations

import io
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from pandrator.logic import audio_cpp_sfx
from pandrator.runtime import DataPaths
from pandrator.web import google_voice_design_routes, voice_queries
from pandrator.web import jobs as jobs_module
from pandrator.web.accounts.models import Workspace
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, SoundVariant, TrainingRun, Voice, VoiceSample
from pandrator.web.projects.models import Project
from pandrator.web.schemas import TrainingCreateRequest
from pandrator.web.sound_service import SoundService
from pandrator.web.training_lifecycle import TrainingService
from tests.web_test_support import prepare_web_test_data_root


def _wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(16_000)
        audio.writeframes(b"\x00\x00" * 160)
    return output.getvalue()


@dataclass
class LibraryCase:
    app: Any
    client: Any
    csrf: str
    workspace_id: str
    other_workspace_id: str

    @property
    def extension(self) -> dict[str, Any]:
        return self.app.extensions["pandrator"]

    @property
    def database(self):
        return self.extension["database"]

    @property
    def paths(self) -> DataPaths:
        return self.extension["paths"]

    @property
    def artifacts(self):
        return self.extension["artifacts"]


@pytest.fixture
def library_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    prepare_web_test_data_root(tmp_path)
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(
        data_root=tmp_path,
        testing=True,
        bootstrap_tokens=bootstrap,
        background_maintenance=False,
    )
    extension = app.extensions["pandrator"]
    database = extension["database"]
    with database.session() as session:
        workspace = Workspace(kind="team", name="Library job owner")
        other_workspace = Workspace(kind="team", name="Other library owner")
        session.add_all([workspace, other_workspace])
        session.flush()
        workspace_id, other_workspace_id = workspace.id, other_workspace.id

    def selected_team_workspace(session, *, workspace_id: str | None = None):
        return session.get(Workspace, workspace_id or selected_id)

    selected_id = workspace_id
    monkeypatch.setattr(voice_queries, "voice_creation_workspace", selected_team_workspace)
    monkeypatch.setattr(
        google_voice_design_routes,
        "voice_creation_workspace",
        selected_team_workspace,
    )
    monkeypatch.setattr(jobs_module, "ensure_app_job_allowed", lambda _path: None)

    client = app.test_client()
    bootstrap_response = client.post("/api/v1/auth/bootstrap", json={"token": token})
    assert bootstrap_response.status_code == 200
    case = LibraryCase(
        app=app,
        client=client,
        csrf=bootstrap_response.get_json()["csrf_token"],
        workspace_id=workspace_id,
        other_workspace_id=other_workspace_id,
    )
    try:
        yield case
    finally:
        database.dispose()


def _register_audio(
    case: LibraryCase,
    *,
    role: str,
    metadata: dict[str, Any] | None = None,
) -> Artifact:
    path = case.paths.artifacts / "job-library" / f"{uuid4().hex}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes())
    return case.artifacts.register(
        path,
        kind="audio",
        role=role,
        metadata=metadata,
        workspace_id=case.workspace_id,
    )


def _create_voice(
    case: LibraryCase,
    *,
    metadata: dict[str, Any] | None = None,
) -> tuple[str, int]:
    with case.database.session() as session:
        voice = Voice(
            workspace_id=case.workspace_id,
            name=f"Library voice {uuid4().hex[:8]}",
            language="en",
            metadata_json=metadata or {},
        )
        session.add(voice)
        session.flush()
        return voice.id, voice.revision


def _create_sample(case: LibraryCase, voice_id: str) -> str:
    artifact = _register_audio(case, role="voice_sample")
    with case.database.session() as session:
        sample = VoiceSample(
            workspace_id=case.workspace_id,
            voice_id=voice_id,
            artifact_id=artifact.id,
            transcript="A synthetic reviewed reference.",
            transcript_language="en",
            transcript_reviewed=True,
        )
        session.add(sample)
        session.flush()
        return sample.id


def _assert_job_response(
    case: LibraryCase,
    response,
    *,
    kind: str,
) -> dict[str, Any]:
    assert response.status_code == 202, response.get_json()
    body = response.get_json()
    assert body["kind"] == kind
    assert "workspace_id" not in body
    with case.database.session() as session:
        job = session.get(Job, body["id"])
        assert job is not None
        assert job.workspace_id == case.workspace_id
        assert job.project_id is None
        assert body["payload_json"] == job.payload_json
        assert body["result_json"] == job.result_json
        return dict(job.payload_json)


def test_training_admission_uses_training_run_workspace(library_case):
    case = library_case
    source = _register_audio(case, role="training_source")
    training = TrainingService(case.database, case.paths, JobQueue(case.database))
    run, job = training.start(
        TrainingCreateRequest(
            model_name="job-library-fixture",
            source_artifact_id=source.id,
            settings={"epochs": 1},
        ),
        workspace_id=case.workspace_id,
    )

    assert run.workspace_id == case.workspace_id
    with case.database.session() as session:
        saved_run = session.get(TrainingRun, run.id)
        saved_job = session.get(Job, job.id)
        assert saved_run is not None and saved_job is not None
        assert saved_job.workspace_id == saved_run.workspace_id
        assert saved_job.project_id is None
        assert saved_job.payload_json["training_id"] == saved_run.id
        assert saved_job.payload_json["workspace_id"] == saved_run.workspace_id


def test_google_design_job_uses_created_voice_workspace(library_case, monkeypatch):
    def configured_service(_database, _paths, _service_id, *, workspace_id):
        assert workspace_id == library_case.workspace_id
        return {"models": ["gemini-3.8-flash-tts"]}, "fixture-credential"

    monkeypatch.setattr(
        google_voice_design_routes,
        "configured_google_design_service",
        configured_service,
    )
    monkeypatch.setattr(
        google_voice_design_routes,
        "google_voice_scope",
        lambda *_args: {"fingerprint": "fixture-scope", "project": "fixture-project"},
    )

    response = library_case.client.post(
        "/api/v1/voices/design/google",
        json={
            "service_id": "gemini",
            "model": "gemini-3.8-flash-tts",
            "name": "Team Google voice",
            "prompt": "A calm synthetic narrator.",
            "language": "en-GB",
            "voice_category": "androgynous",
        },
        headers={
            "X-CSRF-Token": library_case.csrf,
            "Idempotency-Key": "job-library-google-design",
        },
    )
    assert response.status_code == 202, response.get_json()
    body = response.get_json()
    assert "workspace_id" not in body
    with library_case.database.session() as session:
        voice = session.get(Voice, body["voice_id"])
        job = session.get(Job, body["id"])
        assert voice is not None and job is not None
        assert voice.workspace_id == library_case.workspace_id
        assert job.workspace_id == voice.workspace_id
        assert job.project_id is None
        assert job.payload_json["voice_id"] == voice.id
        assert job.payload_json["workspace_id"] == voice.workspace_id
        assert body["payload_json"] == job.payload_json


@pytest.mark.parametrize("bad_scope", ["foreign", "unscoped", "project"])
@pytest.mark.parametrize("candidate_state", ["queued", "ready"])
def test_sound_cache_skips_job_with_wrong_workspace(
    library_case, monkeypatch, bad_scope: str, candidate_state: str
):
    case = library_case
    identity = {
        "provider": "audio_cpp",
        "operation": "gen",
        "model_id": "stable_audio_3_small_sfx_q8_0",
        "runtime_version": "fixture",
        "runtime_sha256": "a" * 64,
        "model_sha256": "b" * 64,
        "model_revision": "fixture",
        "minimum_runtime_version": "0.0.0",
    }
    monkeypatch.setattr(
        audio_cpp_sfx,
        "runtime_identity",
        lambda _settings, *, model_id=audio_cpp_sfx.MODEL_ID: {
            **identity,
            "model_id": model_id,
        },
    )
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)
    sounds = SoundService(
        case.database,
        case.paths,
        case.artifacts,
        JobQueue(case.database),
        settings_provider=lambda: {
            "audio_cpp_executable": "/trusted/audio.cpp",
            "audio_cpp_cache_dir": "/trusted/cache",
            "audio_cpp_timeout_seconds": 30,
        },
    )
    request = {"prompt": "A steady synthetic rain", "duration_seconds": 1.0, "seed": 17}
    prepared = sounds.prepare_generation(request)
    with case.database.immediate_session() as session:
        original = sounds.generate_in_session(
            session,
            request,
            prepared=prepared,
            workspace_id=case.workspace_id,
        )

    if candidate_state == "ready":
        artifact = _register_audio(case, role="sound.original")
        with case.database.immediate_session() as session:
            variant = session.get(SoundVariant, original["candidate_id"])
            job = session.get(Job, original["job_id"])
            assert variant is not None and job is not None
            variant.state = "ready"
            variant.artifact_id = artifact.id
            job.status = "succeeded"

    with case.database.immediate_session() as session:
        matching_cache = sounds.generate_in_session(
            session,
            request,
            prepared=prepared,
            workspace_id=case.workspace_id,
        )
    assert matching_cache["cache_hit"] is True
    assert matching_cache["candidate_id"] == original["candidate_id"]
    assert matching_cache["job_id"] == original["job_id"]

    with case.database.immediate_session() as session:
        old_job = session.get(Job, original["job_id"])
        assert old_job is not None
        if bad_scope == "project":
            project = Project(
                workspace_id=case.workspace_id,
                name="A project-scoped sound job",
            )
            session.add(project)
            session.flush()
            old_job.project_id = project.id
        else:
            old_job.workspace_id = case.other_workspace_id if bad_scope == "foreign" else None

    with case.database.immediate_session() as session:
        replacement = sounds.generate_in_session(
            session,
            request,
            prepared=prepared,
            workspace_id=case.workspace_id,
        )

    assert replacement["cache_hit"] is False
    assert replacement["candidate_id"] != original["candidate_id"]
    assert replacement["job_id"] != original["job_id"]
    with case.database.session() as session:
        variant = session.get(SoundVariant, replacement["candidate_id"])
        job = session.get(Job, replacement["job_id"])
        assert variant is not None and job is not None
        assert variant.workspace_id == case.workspace_id
        assert job.workspace_id == variant.workspace_id
        assert job.project_id is None
        assert job.payload_json["candidate_id"] == variant.id


def test_voice_library_admissions_keep_team_owner_and_public_job_shape(library_case):
    case = library_case
    voice_id, revision = _create_voice(
        case,
        metadata={
            "providers": {
                "kobold_qwen": {
                    "voice_id": "fixture-managed-voice",
                    "status": "ready",
                    "managed_by": "pandrator",
                    "endpoint_fingerprint": "fixture-endpoint",
                }
            }
        },
    )
    sample_id = _create_sample(case, voice_id)
    headers = {"X-CSRF-Token": case.csrf}

    preview_text = "A reviewed preview transcript."
    preview = _register_audio(
        case,
        role="tts_voice_preview",
        metadata={
            "service_id": "audio_cpp",
            "service_adapter": "audio_cpp",
            "model": "breeze_tts_2_q8_0",
            "preview_text": preview_text,
            "language": "en",
        },
    )
    preview_response = case.client.post(
        f"/api/v1/voices/{voice_id}/samples/from-preview",
        json={
            "artifact_id": preview.id,
            "transcript": preview_text,
            "language": "en",
            "expected_voice_revision": revision,
        },
        headers=headers,
    )
    preview_payload = _assert_job_response(case, preview_response, kind="voice.normalize_recording")
    assert preview_payload["voice_id"] == voice_id

    imported = _register_audio(case, role="upload")
    import_response = case.client.post(
        f"/api/v1/voices/{voice_id}/samples/from-artifact",
        json={
            "artifact_id": imported.id,
            "transcript": "An imported reference.",
            "transcript_reviewed": True,
            "language": "en",
            "expected_voice_revision": revision,
        },
        headers=headers,
    )
    import_payload = _assert_job_response(case, import_response, kind="voice.normalize_recording")
    assert import_payload["voice_id"] == voice_id

    upload_response = case.client.post(
        f"/api/v1/voices/{voice_id}/samples",
        data={
            "file": (io.BytesIO(_wav_bytes()), "capture.wav"),
            "expected_revision": str(revision),
        },
        content_type="multipart/form-data",
        headers=headers,
    )
    upload_payload = _assert_job_response(case, upload_response, kind="voice.normalize_recording")
    assert upload_payload["voice_id"] == voice_id

    publish_response = case.client.post(
        f"/api/v1/voices/{voice_id}/providers/kobold_qwen",
        headers={**headers, "If-Match": f'"{revision}"'},
    )
    publish_payload = _assert_job_response(case, publish_response, kind="voice.publish")
    assert publish_payload["voice_id"] == voice_id
    assert publish_payload["sample_id"] == sample_id

    unpublish_response = case.client.delete(
        f"/api/v1/voices/{voice_id}/providers/kobold_qwen",
        headers={**headers, "If-Match": f'"{revision}"'},
    )
    unpublish_payload = _assert_job_response(case, unpublish_response, kind="voice.unpublish")
    assert unpublish_payload["voice_id"] == voice_id

    transcribe_response = case.client.post(
        f"/api/v1/voices/{voice_id}/samples/{sample_id}/transcribe",
        json={"stt_language": "en"},
        headers=headers,
    )
    transcribe_payload = _assert_job_response(case, transcribe_response, kind="voice.transcribe")
    assert transcribe_payload["voice_id"] == voice_id
    assert transcribe_payload["sample_id"] == sample_id
