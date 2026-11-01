from __future__ import annotations

import io
import json
import subprocess
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select
from werkzeug.datastructures import FileStorage

from pandrator.logic import tts_handler, xtts_trainer_handler
from pandrator.runtime import DataPaths
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import Workspace
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.jobs import JobQueue
from pandrator.web.models import (
    AppSetting,
    Artifact,
    Job,
    TrainingRun,
    Voice,
    VoiceSample,
)
from pandrator.web.schemas import TrainingCreateRequest
from pandrator.web.training_lifecycle import TrainingNotFound, TrainingService
from pandrator.web.training_publication import require_training_claim
from pandrator.web.voice_recording_uploads import queue_voice_recording
from pandrator.web.workflow_handlers import WorkflowHandlers
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
class ExecutionCase:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    jobs: JobQueue
    training: TrainingService
    handlers: WorkflowHandlers
    workspaces: dict[str, str]


@pytest.fixture
def execution_case(tmp_path: Path):
    paths = prepare_web_test_data_root(tmp_path / "managed")
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    jobs = JobQueue(database)
    training = TrainingService(database, paths, jobs)
    handlers = WorkflowHandlers(database, paths, jobs=jobs)
    workspaces: dict[str, str] = {}
    with database.session() as session:
        for key, state in (("alpha", "active"), ("beta", "active"), ("archived", "archived")):
            workspace = Workspace(kind="team", name=f"Execution {key}", state=state)
            session.add(workspace)
            session.flush()
            workspaces[key] = workspace.id
    try:
        yield ExecutionCase(database, paths, artifacts, jobs, training, handlers, workspaces)
    finally:
        database.dispose()


def _register_artifact(
    case: ExecutionCase,
    workspace_id: str,
    relative_name: str,
    contents: bytes,
    *,
    kind: str = "audio",
    role: str = "upload",
) -> Artifact:
    path = case.paths.uploads / relative_name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(contents)
    return case.artifacts.register(
        path,
        kind=kind,
        role=role,
        workspace_id=workspace_id,
    )


def _new_voice(case: ExecutionCase, workspace_id: str, name: str) -> Voice:
    with case.database.session() as session:
        voice = Voice(workspace_id=workspace_id, name=name, language="en")
        session.add(voice)
        session.flush()
        return Voice(
            id=voice.id,
            workspace_id=workspace_id,
            name=voice.name,
            language=voice.language,
            revision=voice.revision,
        )


def _new_sample(
    case: ExecutionCase,
    voice: Voice,
    *,
    filename: str = "sample.wav",
) -> tuple[str, Artifact]:
    path = case.paths.voices / voice.id / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_wav_bytes())
    artifact = case.artifacts.register(
        path,
        kind="audio",
        role="voice_sample",
        workspace_id=voice.workspace_id,
    )
    with case.database.session() as session:
        sample = VoiceSample(
            workspace_id=voice.workspace_id,
            voice_id=voice.id,
            artifact_id=artifact.id,
            transcript="A synthetic reference.",
            transcript_language="en",
            transcript_reviewed=True,
        )
        session.add(sample)
        session.flush()
        return sample.id, artifact


def _rows(case: ExecutionCase, *models: type) -> dict[str, tuple[tuple[object, ...], ...]]:
    result: dict[str, tuple[tuple[object, ...], ...]] = {}
    with case.database.snapshot_session() as session:
        for model in models:
            table = model.__table__
            records = session.scalars(select(model).order_by(table.c.id)).all()
            result[table.name] = tuple(
                tuple(getattr(record, column.name) for column in table.columns)
                for record in records
            )
    return result


def _training_request(artifact_id: str, *, voice_id: str | None = None) -> TrainingCreateRequest:
    return TrainingCreateRequest(
        model_name="execution-fixture",
        source_artifact_id=artifact_id,
        voice_id=voice_id,
        settings={"epochs": 1},
    )


@pytest.mark.usefixtures("host_execution_actor")
def test_training_lifecycle_scopes_rows_before_limit_and_preserves_owner(execution_case):
    case = execution_case
    alpha_source = _register_artifact(
        case, case.workspaces["alpha"], "alpha/training.wav", _wav_bytes()
    )
    beta_source = _register_artifact(
        case, case.workspaces["beta"], "beta/training.wav", _wav_bytes()
    )
    alpha_run, alpha_job = case.training.start(
        _training_request(alpha_source.id), workspace_id=case.workspaces["alpha"]
    )
    beta_run, beta_job = case.training.start(
        _training_request(beta_source.id), workspace_id=case.workspaces["beta"]
    )

    assert alpha_run.workspace_id == case.workspaces["alpha"]
    assert beta_run.workspace_id == case.workspaces["beta"]
    with case.database.snapshot_session() as session:
        assert (
            session.get(Job, alpha_job.id).payload_json["workspace_id"] == case.workspaces["alpha"]
        )
        assert session.get(Job, beta_job.id).payload_json["workspace_id"] == case.workspaces["beta"]
    assert [
        item["id"] for item in case.training.list(workspace_id=case.workspaces["alpha"], limit=1)
    ] == [alpha_run.id]
    assert [
        item["id"] for item in case.training.list(workspace_id=case.workspaces["beta"], limit=1)
    ] == [beta_run.id]

    before = _rows(case, TrainingRun, Job)
    with pytest.raises(TrainingNotFound):
        case.training.retry(alpha_run.id, workspace_id=case.workspaces["beta"])
    with pytest.raises(TrainingNotFound):
        case.training.cancel(alpha_run.id, workspace_id=case.workspaces["beta"])
    assert _rows(case, TrainingRun, Job) == before

    canceled = case.training.cancel(alpha_run.id, workspace_id=case.workspaces["alpha"])
    assert canceled["status"] == "canceled"
    retried, retry_job = case.training.retry(alpha_run.id, workspace_id=case.workspaces["alpha"])
    assert retried.workspace_id == case.workspaces["alpha"]
    with case.database.snapshot_session() as session:
        assert (
            session.get(Job, retry_job.id).payload_json["workspace_id"] == case.workspaces["alpha"]
        )
        assert session.get(TrainingRun, alpha_run.id).workspace_id == case.workspaces["alpha"]


def test_training_wrong_input_can_be_caught_and_committed_without_partial_rows(
    execution_case,
):
    case = execution_case
    foreign_source = _register_artifact(
        case, case.workspaces["beta"], "beta/foreign.wav", _wav_bytes()
    )
    before = _rows(case, Artifact, TrainingRun, Job)

    # Mirror callers that catch validation failure inside their writer and still
    # commit unrelated work. Ownership validation must not leave dirty rows.
    with case.database.session() as session:
        try:
            case.training._validate_inputs(
                session,
                foreign_source.id,
                None,
                None,
                {},
                case.workspaces["alpha"],
            )
        except ValueError:
            pass
        else:
            raise AssertionError("cross-workspace training input was accepted")
        session.add(AppSetting(key="execution-test-commit", value_json={"committed": True}))

    assert _rows(case, Artifact, TrainingRun, Job) == before
    with case.database.snapshot_session() as session:
        assert session.get(AppSetting, "execution-test-commit").value_json == {"committed": True}


@pytest.mark.usefixtures("host_execution_actor")
def test_retry_and_cancel_validate_all_retained_training_links_before_refresh(
    execution_case,
):
    case = execution_case
    alpha = case.workspaces["alpha"]
    source = _register_artifact(case, alpha, "alpha/retained-link.wav", _wav_bytes())
    alpha_voice = _new_voice(case, alpha, "Retained owner voice")
    beta_voice = _new_voice(case, case.workspaces["beta"], "Foreign linked voice")
    run, _ = case.training.start(
        _training_request(source.id, voice_id=alpha_voice.id), workspace_id=alpha
    )
    # The live admission remains current; retained corrupt links use their predecessor.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0077_media_scope_consistency")
    with case.database.session() as session:
        session.get(TrainingRun, run.id).voice_id = beta_voice.id
    before = _rows(case, TrainingRun, Job)

    with pytest.raises(TrainingNotFound):
        case.training.retry(run.id, workspace_id=alpha)
    with pytest.raises(TrainingNotFound):
        case.training.cancel(run.id, workspace_id=alpha)
    assert _rows(case, TrainingRun, Job) == before


@pytest.mark.usefixtures("host_execution_actor")
def test_training_claim_checks_frozen_and_legacy_workspace_fields(execution_case):
    case = execution_case
    source = _register_artifact(case, case.workspaces["alpha"], "alpha/claim.wav", _wav_bytes())
    run, _ = case.training.start(
        _training_request(source.id), workspace_id=case.workspaces["alpha"]
    )
    claimed = case.jobs.claim("execution-test-worker", lease_seconds=120)
    assert claimed is not None and claimed.id == run.job_id
    worker_payload = {
        **claimed.payload_json,
        "_job_id": claimed.id,
        "_lease_generation": claimed.lease_generation,
    }
    with case.database.session() as session:
        assert (
            require_training_claim(session, worker_payload, threading.Event()).workspace_id
            == case.workspaces["alpha"]
        )

    legacy_payload = dict(worker_payload)
    legacy_payload.pop("workspace_id")
    with case.database.session() as session:
        job = session.get(Job, claimed.id)
        job.payload_json = dict(job.payload_json)
        job.payload_json.pop("workspace_id")
    with case.database.session() as session:
        assert (
            require_training_claim(session, legacy_payload, threading.Event()).workspace_id
            == case.workspaces["alpha"]
        )

    with pytest.raises(InterruptedError, match="workspace"):
        with case.database.session() as session:
            require_training_claim(session, worker_payload, threading.Event())

    wrong_value = {**worker_payload, "workspace_id": case.workspaces["beta"]}
    with pytest.raises(ValueError, match="workspace"):
        with case.database.session() as session:
            require_training_claim(session, wrong_value, threading.Event())


def test_retained_training_owner_survives_nullable_input_links(execution_case):
    case = execution_case
    source = _register_artifact(case, case.workspaces["alpha"], "alpha/retained.wav", _wav_bytes())
    voice = _new_voice(case, case.workspaces["alpha"], "Retained training voice")
    with case.database.session() as session:
        run = TrainingRun(
            workspace_id=case.workspaces["alpha"],
            kind="xtts",
            voice_id=voice.id,
            source_artifact_id=source.id,
            model_name="execution-fixture",
            status="running",
            settings_json={},
        )
        session.add(run)
        session.flush()
        training_id = run.id
    with case.database.session() as session:
        run = session.get(TrainingRun, training_id)
        run.voice_id = None
        run.source_artifact_id = None
        run.source_text_artifact_id = None
        run.output_artifact_id = None

    legacy_payload = {
        "training_id": training_id,
        "model_name": "execution-fixture",
        "source_artifact_id": None,
        "source_text_artifact_id": None,
        "settings": {},
    }
    with case.database.session() as session:
        retained = require_training_claim(session, legacy_payload, threading.Event())
        assert retained.workspace_id == case.workspaces["alpha"]


@pytest.mark.usefixtures("host_execution_actor")
def test_archived_training_fails_before_trainer(execution_case, monkeypatch):
    case = execution_case
    source = _register_artifact(
        case, case.workspaces["alpha"], "alpha/archived-training.wav", _wav_bytes()
    )
    run, _ = case.training.start(
        _training_request(source.id), workspace_id=case.workspaces["alpha"]
    )
    job = case.jobs.claim("execution-test-worker", lease_seconds=120)
    assert job is not None
    payload = {
        **job.payload_json,
        "_job_id": job.id,
        "_lease_generation": job.lease_generation,
    }
    with case.database.session() as session:
        session.get(Workspace, case.workspaces["alpha"]).state = "archived"
    calls: list[object] = []
    monkeypatch.setattr(
        xtts_trainer_handler, "start_training", lambda *args, **kwargs: calls.append(args)
    )

    with pytest.raises(ValueError):
        case.handlers.train_xtts(payload, lambda *_args: None, threading.Event())
    assert calls == []
    with case.database.snapshot_session() as session:
        assert session.get(TrainingRun, run.id).workspace_id == case.workspaces["alpha"]


def test_voice_recording_foreign_owner_is_rejected_before_file_save(execution_case):
    case = execution_case
    voice = _new_voice(case, case.workspaces["beta"], "Upload owner beta")
    raw = _wav_bytes()
    stream = io.BytesIO(raw)
    incoming = FileStorage(stream=stream, filename="recording.wav")
    before = _rows(case, Artifact, VoiceSample, Job)

    with pytest.raises(ValueError, match="workspace"):
        queue_voice_recording(
            case.database,
            case.paths,
            case.artifacts,
            case.jobs,
            incoming,
            voice_id=voice.id,
            expected_revision=voice.revision,
            noise_reduction="none",
            workspace_id=case.workspaces["alpha"],
        )

    assert stream.tell() == 0
    assert _rows(case, Artifact, VoiceSample, Job) == before
    assert list(case.paths.temporary.glob("voice-*")) == []


@execution_actor_scope(HostActor("pandrator-cli"))
def test_normalization_rejects_missing_archived_and_mismatched_owners_before_ffmpeg(
    execution_case, monkeypatch
):
    case = execution_case
    # NULL ownership is retained legacy data, forbidden since revision 0075.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(case.database.path))
    command.downgrade(config, "0074_actor_attribution")
    alpha = case.workspaces["alpha"]
    beta = case.workspaces["beta"]
    missing_voice = _new_voice(case, alpha, "Missing persisted owner")
    missing_source = _register_artifact(
        case, alpha, "alpha/missing-owner.wav", _wav_bytes(), role="recording_upload"
    )
    with case.database.session() as session:
        session.get(Voice, missing_voice.id).workspace_id = None

    archived_voice = _new_voice(case, alpha, "Archived input owner")
    archived_source = _register_artifact(
        case,
        alpha,
        "archived/archived-owner.wav",
        _wav_bytes(),
        role="recording_upload",
    )
    with case.database.session() as session:
        session.get(Voice, archived_voice.id).workspace_id = case.workspaces["archived"]
    mismatched_voice = _new_voice(case, alpha, "Mismatched input owner")
    foreign_source = _register_artifact(
        case, beta, "beta/mismatched-owner.wav", _wav_bytes(), role="recording_upload"
    )
    calls: list[object] = []
    monkeypatch.setattr(
        "pandrator.web.workflow_voice.subprocess.run",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    cases = (
        (missing_voice, missing_source, alpha),
        (archived_voice, archived_source, alpha),
        (mismatched_voice, foreign_source, alpha),
    )
    for voice, source, workspace_id in cases:
        with pytest.raises(ValueError):
            case.handlers.normalize_voice_recording(
                {
                    "workspace_id": workspace_id,
                    "voice_id": voice.id,
                    "expected_voice_revision": voice.revision,
                    "source_artifact_id": source.id,
                    "noise_reduction": "none",
                },
                lambda *_args: None,
                threading.Event(),
            )

    assert calls == []


@execution_actor_scope(HostActor("pandrator-cli"))
def test_normalization_and_unbound_transcription_publish_explicit_owner(
    execution_case, monkeypatch
):
    case = execution_case
    alpha = case.workspaces["alpha"]
    voice = _new_voice(case, alpha, "Workspace audio voice")
    source = _register_artifact(
        case, alpha, "alpha/raw-reference.wav", _wav_bytes(), role="recording_upload"
    )
    normalize_payload = {
        "workspace_id": alpha,
        "voice_id": voice.id,
        "expected_voice_revision": voice.revision,
        "source_artifact_id": source.id,
        "noise_reduction": "none",
    }

    def fake_ffmpeg(command, **_kwargs):
        Path(command[-1]).write_bytes(_wav_bytes())
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("pandrator.web.workflow_voice.subprocess.run", fake_ffmpeg)
    normalized = case.handlers.normalize_voice_recording(
        normalize_payload, lambda *_args: None, threading.Event()
    )
    with case.database.snapshot_session() as session:
        sample = session.get(VoiceSample, normalized["sample_id"])
        audio = session.get(Artifact, normalized["artifact_id"])
        assert sample.workspace_id == alpha
        assert audio.workspace_id == alpha

    transcription_source = _register_artifact(
        case, alpha, "alpha/unbound-transcription.wav", _wav_bytes()
    )
    transcribe_calls: list[Path] = []

    def fake_transcriber(operation_dir, _source_path, _settings, **_kwargs):
        operation_dir = Path(operation_dir)
        transcribe_calls.append(operation_dir)
        srt = operation_dir / "fixture.srt"
        words = operation_dir / "fixture.json"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nSynthetic words\n", encoding="utf-8")
        words.write_text(
            json.dumps(
                {
                    "schema": "pandrator.transcript.v1",
                    "language": "en",
                    "segments": [
                        {
                            "id": "1",
                            "start_ms": 0,
                            "end_ms": 1000,
                            "text": "Synthetic words",
                            "words": [],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return SimpleNamespace(
            srt_path=str(srt),
            word_timestamps_path=str(words),
            engine="fixture",
            compute_backend="cpu",
            resolved_language="en",
            routing={},
        )

    def hydrate_settings(_database, _paths, _settings, *, workspace_id=None):
        assert workspace_id == alpha
        return {}

    monkeypatch.setattr("pandrator.web.workflow_voice.hydrate_stt_settings", hydrate_settings)
    monkeypatch.setattr(
        "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
        fake_transcriber,
    )
    transcription = case.handlers.transcribe_voice(
        {
            "workspace_id": alpha,
            "sample_artifact_id": transcription_source.id,
            "settings": {"stt_language": "en"},
        },
        lambda *_args: None,
        threading.Event(),
    )

    assert len(transcribe_calls) == 1
    assert transcribe_calls[0].parent == (
        case.paths.voices / "transcriptions" / alpha / transcription_source.id
    )
    with case.database.snapshot_session() as session:
        text_artifact = session.get(Artifact, transcription["artifact_id"])
        timings_artifact = session.get(Artifact, transcription["word_timestamps_artifact_id"])
        assert text_artifact.workspace_id == alpha
        assert timings_artifact.workspace_id == alpha


@execution_actor_scope(HostActor("pandrator-cli"))
def test_tampering_during_normalization_prevents_publication_and_preserves_source(
    execution_case, monkeypatch
):
    case = execution_case
    alpha = case.workspaces["alpha"]
    voice = _new_voice(case, alpha, "Tamper during normalization")
    source = _register_artifact(
        case, alpha, "alpha/tamper-normalize.wav", _wav_bytes(), role="recording_upload"
    )
    _, source_path = case.artifacts.resolve(source.id)
    original_bytes = source_path.read_bytes()
    before = _rows(case, Artifact, Voice, VoiceSample)
    calls = 0

    def archive_after_processing(command, **_kwargs):
        nonlocal calls
        calls += 1
        Path(command[-1]).write_bytes(_wav_bytes())
        with case.database.session() as session:
            session.get(Workspace, alpha).state = "archived"
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr("pandrator.web.workflow_voice.subprocess.run", archive_after_processing)
    with pytest.raises(ValueError, match="workspace"):
        case.handlers.normalize_voice_recording(
            {
                "workspace_id": alpha,
                "voice_id": voice.id,
                "expected_voice_revision": voice.revision,
                "source_artifact_id": source.id,
                "noise_reduction": "none",
            },
            lambda *_args: None,
            threading.Event(),
        )

    assert calls == 1
    assert _rows(case, Artifact, Voice, VoiceSample) == before
    assert source_path.read_bytes() == original_bytes
    assert not list((case.paths.voices / voice.id).glob("sample-*.wav"))


@execution_actor_scope(HostActor("pandrator-cli"))
def test_tampering_during_transcription_prevents_artifact_registration(execution_case, monkeypatch):
    case = execution_case
    alpha = case.workspaces["alpha"]
    source = _register_artifact(case, alpha, "alpha/tamper-transcribe.wav", _wav_bytes())
    _, source_path = case.artifacts.resolve(source.id)
    original_bytes = source_path.read_bytes()
    before = _rows(case, Artifact)

    def archive_after_transcription(operation_dir, _source_path, _settings, **_kwargs):
        operation_dir = Path(operation_dir)
        operation_dir.mkdir(parents=True, exist_ok=True)
        srt = operation_dir / "tampered.srt"
        words = operation_dir / "tampered.json"
        srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nWords\n", encoding="utf-8")
        words.write_text('{"schema":"pandrator.transcript.v1","segments":[]}', encoding="utf-8")
        with case.database.session() as session:
            session.get(Workspace, alpha).state = "archived"
        return SimpleNamespace(
            srt_path=str(srt),
            word_timestamps_path=str(words),
            engine="fixture",
            compute_backend="cpu",
            resolved_language="en",
            routing={},
        )

    def hydrate_settings(_database, _paths, _settings, *, workspace_id=None):
        assert workspace_id == alpha
        return {}

    monkeypatch.setattr("pandrator.web.workflow_voice.hydrate_stt_settings", hydrate_settings)
    monkeypatch.setattr(
        "pandrator.logic.dubbing.transcription.transcribe_source_file_with_metadata",
        archive_after_transcription,
    )
    with pytest.raises(ValueError, match="workspace"):
        case.handlers.transcribe_voice(
            {
                "workspace_id": alpha,
                "sample_artifact_id": source.id,
                "settings": {},
            },
            lambda *_args: None,
            threading.Event(),
        )

    assert _rows(case, Artifact) == before
    assert source_path.read_bytes() == original_bytes


@execution_actor_scope(HostActor("pandrator-cli"))
def test_publish_rejects_foreign_voice_before_provider_call(execution_case, monkeypatch):
    case = execution_case
    voice = _new_voice(case, case.workspaces["alpha"], "Foreign provider guard")
    _new_sample(case, voice)
    calls: list[object] = []
    monkeypatch.setattr(
        case.handlers.tts_providers,
        "upload_voice",
        lambda *args, **kwargs: calls.append((args, kwargs)),
    )

    with pytest.raises(ValueError, match="workspace"):
        case.handlers.publish_voice(
            {
                "workspace_id": case.workspaces["beta"],
                "voice_id": voice.id,
                "service_id": "fixture",
            },
            lambda *_args: None,
            threading.Event(),
        )
    assert calls == []


@execution_actor_scope(HostActor("pandrator-cli"))
def test_direct_host_upload_after_archive_does_not_publish_binding(execution_case, monkeypatch):
    case = execution_case
    alpha = case.workspaces["alpha"]
    voice = _new_voice(case, alpha, "Archive during provider upload")
    _new_sample(case, voice)
    calls: list[tuple[object, object]] = []

    def upload(*args, **kwargs):
        calls.append((args, kwargs))
        with case.database.session() as session:
            session.get(Workspace, alpha).state = "archived"
        return "remote-voice-fixture"

    monkeypatch.setattr(
        "pandrator.web.workflow_voice.hydrate_tts_settings",
        lambda *_args, **_kwargs: {"service": "fixture"},
    )
    monkeypatch.setattr(
        tts_handler,
        "get_service_config",
        lambda *_args: {
            "id": "fixture",
            "name": "Fixture provider",
            "api_base": "https://provider.invalid",
            "adapter": "fixture",
            "voice_reference_text": "ignored",
        },
    )
    monkeypatch.setattr(case.handlers.tts_providers, "upload_voice", upload)
    before_artifacts = _rows(case, Artifact)
    before_voice_files = {
        path.relative_to(case.paths.voices): path.read_bytes()
        for path in case.paths.voices.rglob("*")
        if path.is_file()
    }
    with pytest.raises(ValueError, match="local binding was not applied"):
        case.handlers.publish_voice(
            {
                "workspace_id": alpha,
                "voice_id": voice.id,
                "service_id": "fixture",
                "expected_voice_revision": voice.revision,
            },
            lambda *_args: None,
            threading.Event(),
        )

    assert calls and len(calls) == 1
    assert _rows(case, Artifact) == before_artifacts
    assert {
        path.relative_to(case.paths.voices): path.read_bytes()
        for path in case.paths.voices.rglob("*")
        if path.is_file()
    } == before_voice_files
    with case.database.snapshot_session() as session:
        stored = session.get(Voice, voice.id)
        assert not (stored.metadata_json or {}).get("providers", {}).get("fixture")
        assert stored.revision == voice.revision


def test_sessionless_rvc_outputs_are_owner_scoped(execution_case, monkeypatch):
    case = execution_case
    alpha_source = _register_artifact(
        case, case.workspaces["alpha"], "alpha/source.wav", _wav_bytes()
    )
    beta_source = _register_artifact(case, case.workspaces["beta"], "beta/source.wav", _wav_bytes())

    class Converted:
        def export(self, path, *, format):
            assert format == "wav"
            Path(path).write_bytes(_wav_bytes())

    monkeypatch.setattr("pydub.AudioSegment.from_file", lambda _path: object())
    monkeypatch.setattr("pandrator.logic.rvc_handler.is_rvc_available", lambda: True)
    monkeypatch.setattr(
        "pandrator.logic.rvc_handler.process_with_rvc",
        lambda *_args, **_kwargs: Converted(),
    )
    settings = {"rvc_model": "fixture-model"}
    alpha_result = case.handlers.convert_with_rvc(
        {
            "workspace_id": case.workspaces["alpha"],
            "source_artifact_id": alpha_source.id,
            "settings": settings,
        },
        lambda *_args: None,
        threading.Event(),
    )
    beta_result = case.handlers.convert_with_rvc(
        {
            "workspace_id": case.workspaces["beta"],
            "source_artifact_id": beta_source.id,
            "settings": settings,
        },
        lambda *_args: None,
        threading.Event(),
    )

    alpha_path = case.paths.managed_path(alpha_result["path"])
    beta_path = case.paths.managed_path(beta_result["path"])
    assert alpha_path.parent == case.paths.artifacts / "rvc" / case.workspaces["alpha"]
    assert beta_path.parent == case.paths.artifacts / "rvc" / case.workspaces["beta"]
    assert alpha_path != beta_path and alpha_path.is_file() and beta_path.is_file()
    with case.database.snapshot_session() as session:
        assert (
            session.get(Artifact, alpha_result["artifact_id"]).workspace_id
            == case.workspaces["alpha"]
        )
        assert (
            session.get(Artifact, beta_result["artifact_id"]).workspace_id
            == case.workspaces["beta"]
        )
