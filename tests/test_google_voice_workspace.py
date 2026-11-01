"""Owned Google design publication and paid-result reconciliation with fake HTTP."""

from __future__ import annotations

import io
import threading
import wave
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from pandrator.runtime import DataPaths
from pandrator.web import google_voice_design as design
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import Artifact, Voice, Workspace
from pandrator.web.workflow_voice import VoiceWorkflowContext
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class DesignHarness:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    context: VoiceWorkflowContext
    workspace_id: str
    other_workspace_id: str
    voice_id: str
    payload: dict
    wav: bytes


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[DesignHarness]:
    paths = prepare_web_test_data_root(tmp_path / "data")
    database = Database(paths.database)
    artifacts = ArtifactService(database, paths)
    model = "gemini-3.8-flash-tts"
    with database.session() as session:
        workspace = Workspace(kind="team", name="Design owner")
        other = Workspace(kind="team", name="Another design owner")
        session.add_all([workspace, other])
        session.flush()
        voice = Voice(
            workspace_id=workspace.id,
            name="Designed voice",
            description="Synthetic design prompt",
            metadata_json={
                "google_voice_design": {"state": "queued", "job_id": "synthetic-design-job"}
            },
        )
        session.add(voice)
        session.flush()
        workspace_id, other_id, voice_id = workspace.id, other.id, voice.id
    service = {"models": [model], "adapter": "google", "api_base": "https://synthetic.invalid"}
    monkeypatch.setattr(design, "configured_google_design_service", lambda *_args, **_kwargs: (service, "fake"))
    monkeypatch.setattr(
        design, "_scope_for_payload", lambda *_args, **_kwargs: {"project": "fake-project", "fingerprint": "fake"}
    )
    monkeypatch.setattr(
        design,
        "_creation_endpoint_and_headers",
        lambda *_args, **_kwargs: ("https://synthetic.invalid", {}, False),
    )
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(24000)
        writer.writeframes(b"\x01\x00" * 2400)
    payload = {
        "job_id": "synthetic-design-job",
        "workspace_id": workspace_id,
        "voice_id": voice_id,
        "name": "Designed voice",
        "prompt": "Synthetic design prompt",
        "model": model,
        "service_id": "gemini",
        "language": "en",
    }
    context = cast(
        VoiceWorkflowContext, SimpleNamespace(database=database, paths=paths, artifacts=artifacts)
    )
    try:
        yield DesignHarness(
            database,
            paths,
            artifacts,
            context,
            workspace_id,
            other_id,
            voice_id,
            payload,
            buffer.getvalue(),
        )
    finally:
        database.dispose()


@execution_actor_scope(HostActor("pandrator-cli"))
def _run(harness: DesignHarness, payload: dict | None = None) -> dict:
    return design.design_google_voice(
        harness.context,
        harness.payload if payload is None else payload,
        lambda *_args, **_kwargs: None,
        threading.Event(),
    )


@pytest.mark.parametrize("legacy_context", ["explicit", "omitted", "null"])
def test_design_preview_and_ready_replay_keep_the_persisted_team_owner(
    harness: DesignHarness, monkeypatch: pytest.MonkeyPatch, legacy_context: str
) -> None:
    calls = []

    def fake_create(*_args, **_kwargs):
        before_io = _kwargs.get("before_io")
        if callable(before_io):
            before_io()
        calls.append(True)
        return {"id": "synthetic-remote-id", "sample_audio": {}}

    monkeypatch.setattr(design.google_tts_catalogue_http, "create_google_voice", fake_create)
    monkeypatch.setattr(design, "_sample_audio_to_wav", lambda _: harness.wav)
    payload = dict(harness.payload)
    if legacy_context == "omitted":
        payload.pop("workspace_id")
    elif legacy_context == "null":
        payload["workspace_id"] = None
    result = _run(harness, payload)
    assert result["provider_voice_id"] == "synthetic-remote-id"
    assert len(calls) == 1
    with harness.database.session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        assert artifact is not None and artifact.workspace_id == harness.workspace_id
        assert harness.paths.managed_path(artifact.relative_path).read_bytes() == harness.wav
    assert _run(harness, payload) == result
    assert len(calls) == 1
    with pytest.raises(IntegrityError, match="artifact workspace is immutable"):
        with harness.database.session() as session:
            artifact = session.get(Artifact, result["artifact_id"])
            assert artifact is not None
            artifact.workspace_id = harness.other_workspace_id
    foreign_path = harness.paths.artifacts / "foreign-preview.wav"
    foreign_path.write_bytes(harness.wav)
    foreign = harness.artifacts.register(
        foreign_path,
        kind="audio",
        role="tts_voice_preview",
        workspace_id=harness.other_workspace_id,
    )
    with harness.database.session() as session:
        voice = session.get(Voice, harness.voice_id)
        assert voice is not None
        metadata = deepcopy(voice.metadata_json)
        metadata["google_voice_design"]["preview_artifact_id"] = foreign.id
        metadata["providers"]["gemini"]["preview_artifact_id"] = foreign.id
        voice.metadata_json = metadata
    with pytest.raises(ValueError, match="workspace"):
        _run(harness, payload)
    assert len(calls) == 1


@pytest.mark.parametrize("bad_scope", ["foreign", "archived"])
def test_changed_frozen_scope_stops_before_provider_or_metadata_mutation(
    harness: DesignHarness, monkeypatch: pytest.MonkeyPatch, bad_scope: str
) -> None:
    def forbidden_provider(*_args, **_kwargs):
        pytest.fail("invalid ownership must fail before provider work")

    monkeypatch.setattr(design.google_tts_catalogue_http, "create_google_voice", forbidden_provider)
    payload = dict(harness.payload)
    if bad_scope == "foreign":
        payload["workspace_id"] = harness.other_workspace_id
    else:
        with harness.database.session() as session:
            workspace = session.get(Workspace, harness.workspace_id)
            assert workspace is not None
            workspace.state = "archived"
    with harness.database.session() as session:
        voice = session.get(Voice, harness.voice_id)
        assert voice is not None
        before = deepcopy(voice.metadata_json), voice.revision
    with pytest.raises(ValueError, match="workspace"):
        _run(harness, payload)
    with harness.database.session() as session:
        voice = session.get(Voice, harness.voice_id)
        assert voice is not None and (voice.metadata_json, voice.revision) == before
        assert session.scalar(select(Artifact.id)) is None


def test_direct_host_archive_during_creation_does_not_publish_binding(
    harness: DesignHarness, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = []

    def archive_during_create(*_args, **_kwargs):
        before_io = _kwargs.get("before_io")
        if callable(before_io):
            before_io()
        calls.append(True)
        with harness.database.session() as session:
            workspace = session.get(Workspace, harness.workspace_id)
            assert workspace is not None
            workspace.state = "archived"
        return {"id": "paid-remote-id", "sample_audio": {}}

    monkeypatch.setattr(
        design.google_tts_catalogue_http, "create_google_voice", archive_during_create
    )
    monkeypatch.setattr(
        design,
        "_sample_audio_to_wav",
        lambda _: pytest.fail("archived workspace must not start preview publication"),
    )
    with pytest.raises(ValueError, match="not_applied"):
        _run(harness)
    assert calls == [True]
    with harness.database.session() as session:
        voice = session.get(Voice, harness.voice_id)
        assert voice is not None and voice.workspace_id == harness.workspace_id
        receipt = voice.metadata_json["google_voice_design"]
        assert not receipt.get("provider_voice_id")
        assert receipt["state"] != "created"
        assert not voice.metadata_json.get("providers", {}).get("gemini")
        assert session.scalar(select(Artifact.id)) is None
    assert not (harness.paths.artifacts / "google-voice-design").exists()
