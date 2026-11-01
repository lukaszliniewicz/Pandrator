from __future__ import annotations

import base64
import hashlib
import json
import socket
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import delete, func, select

from pandrator.runtime import DataPaths
from pandrator.web import speech_inputs
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database
from pandrator.web.models import (
    Artifact,
    ArtifactEdge,
    SessionRecord,
    Voice,
    VoiceSample,
    Workspace,
)
from pandrator.web.projects.identity import create_session_project
from pandrator.web.speech_input_contracts import (
    PreparedSpeechInput,
    SpeechInputCapture,
    SpeechInputError,
    SpeechInputOwner,
    validate_speech_input_pin,
)
from pandrator.web.speech_inputs import (
    capture_speech_input,
    cleanup_speech_input,
    load_speech_input,
    prepare_speech_input,
    publish_speech_input,
)
from pandrator.web.voice_library import resolve_audio_cpp_voice_reference
from tests.test_speech_input_files import wav_bytes
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class Harness:
    paths: DataPaths
    database: Database
    owner: SpeechInputOwner
    other: SpeechInputOwner
    voice_id: str
    sample_id: str
    artifact_id: str
    settings: dict[str, Any]
    data: bytes

    def capture(
        self, *, owner: SpeechInputOwner | None = None, explicit: bool = True
    ) -> SpeechInputCapture:
        with self.database.snapshot_session() as session:
            return capture_speech_input(
                session,
                self.paths,
                self.settings,
                owner=owner or self.owner,
                voice_id=self.voice_id if explicit else None,
            )

    def prepare(self, capture: SpeechInputCapture | None = None) -> PreparedSpeechInput:
        return prepare_speech_input(
            self.database, self.paths, capture or self.capture(), before_io=lambda: None
        )

    def publish(self, capture: SpeechInputCapture, prepared: PreparedSpeechInput) -> dict[str, Any]:
        with self.database.immediate_session() as session:
            return publish_speech_input(session, self.paths, capture, prepared)

    def load(self, pin: object, *, owner: SpeechInputOwner | None = None) -> dict[str, Any]:
        return load_speech_input(
            self.database,
            self.paths,
            pin,
            self.settings,
            owner=owner or self.owner,
            before_io=lambda: None,
        )


@pytest.fixture
def harness(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Harness]:
    def blocked(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Network is blocked for retained speech tests")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    with database.session() as session:
        one, two = (
            Workspace(kind="team", name="speech one", state="active"),
            Workspace(kind="team", name="speech two", state="active"),
        )
        session.add_all([one, two])
        session.flush()
        owner, other = SpeechInputOwner(one.id), SpeechInputOwner(two.id)
        voice = Voice(
            workspace_id=one.id,
            name="Managed",
            language="en",
            metadata_json={
                "providers": {
                    "audio_cpp": {
                        "resource_kind": "linked_reference",
                        "status": "ready",
                        "voice_id": "chosen",
                    }
                }
            },
        )
        session.add(voice)
        session.flush()
        voice_id = voice.id
    data = wav_bytes()
    path = paths.voices / voice_id / "source.wav"
    path.parent.mkdir()
    path.write_bytes(data)
    artifact = ArtifactService(database, paths).register(
        path, kind="audio", role="voice_sample", workspace_id=owner.workspace_id
    )
    with database.session() as session:
        sample = VoiceSample(
            workspace_id=owner.workspace_id,
            voice_id=voice_id,
            artifact_id=artifact.id,
            transcript="Reviewed source.",
            transcript_language="en",
            transcript_reviewed=True,
        )
        session.add(sample)
        session.flush()
        sample_id = sample.id
    try:
        yield Harness(
            paths,
            database,
            owner,
            other,
            voice_id,
            sample_id,
            artifact.id,
            {"service": "audio_cpp", "model": "synthetic", "voice": "chosen"},
            data,
        )
    finally:
        database.dispose()


def _count(h: Harness) -> int:
    with h.database.snapshot_session() as session:
        return int(session.scalar(select(func.count()).select_from(Artifact)) or 0)


def test_retained_graph_survives_original_delete_and_contains_no_identifiers(
    harness: Harness,
) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    assert _count(h) == 1
    pin = h.publish(capture, prepared)
    assert _count(h) == 3
    envelope_bytes = h.paths.managed_path(prepared.snapshot.relative_path).read_bytes()
    envelope = json.loads(envelope_bytes)
    for forbidden in (
        h.voice_id,
        h.sample_id,
        h.artifact_id,
        h.owner.workspace_id,
        prepared.audio.artifact_id,
        prepared.snapshot.artifact_id,
        "source.wav",
        "providers",
    ):
        assert forbidden not in envelope_bytes.decode()
    assert envelope["reference_text"] == "Reviewed source."
    with h.database.session() as session:
        session.execute(delete(VoiceSample).where(VoiceSample.id == h.sample_id))
        session.execute(delete(Voice).where(Voice.id == h.voice_id))
        original = session.get(Artifact, h.artifact_id)
        assert original is not None
        original.state = "deleted"
        path = h.paths.managed_path(original.relative_path)
        assert [
            (edge.parent_artifact_id, edge.child_artifact_id, edge.relation)
            for edge in session.scalars(select(ArtifactEdge)).all()
        ] == [(prepared.audio.artifact_id, prepared.snapshot.artifact_id, "speech_reference")]
    path.unlink()
    result = h.load(pin)
    assert base64.b64decode(result["audio_cpp_voice_ref"]["data"].split(",", 1)[1]) == h.data
    assert result["audio_cpp_reference_text"] == "Reviewed source."
    assert "audio_cpp_voice_ref" not in h.settings


def test_same_owner_exact_reuse_and_metadata_only_audio_reuse(harness: Harness) -> None:
    h = harness
    first_capture = h.capture()
    first = h.prepare(first_capture)
    first_pin = h.publish(first_capture, first)
    second_capture = h.capture()
    second = h.prepare(second_capture)
    assert not second.audio.owned and not second.snapshot.owned
    assert h.publish(second_capture, second) == first_pin
    cleanup_speech_input(second)
    assert _count(h) == 3 and h.load(first_pin)
    with h.database.session() as session:
        sample, voice = session.get(VoiceSample, h.sample_id), session.get(Voice, h.voice_id)
        assert sample is not None and voice is not None
        sample.transcript = "New review."
        voice.revision += 1
    third_capture = h.capture()
    third = h.prepare(third_capture)
    assert third.audio.artifact_id == first.audio.artifact_id and not third.audio.owned
    assert third.snapshot.owned
    third_pin = h.publish(third_capture, third)
    assert third_pin != first_pin and _count(h) == 4
    assert h.load(third_pin)["audio_cpp_reference_text"] == "New review."
    assert h.load(first_pin)["audio_cpp_reference_text"] == "Reviewed source."


@pytest.mark.parametrize(
    "mutation",
    [
        "revision",
        "registration",
        "transcript",
        "delete_sample",
        "delete_voice",
        "artifact_hash",
        "artifact_path",
        "artifact_state",
        "bytes",
    ],
)
def test_source_drift_before_publication_is_rejected(harness: Harness, mutation: str) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    with h.database.session() as session:
        voice, sample, artifact = (
            session.get(Voice, h.voice_id),
            session.get(VoiceSample, h.sample_id),
            session.get(Artifact, h.artifact_id),
        )
        assert voice is not None and sample is not None and artifact is not None
        if mutation == "revision":
            voice.revision += 1
        elif mutation == "registration":
            voice.metadata_json = {"providers": {}}
        elif mutation == "transcript":
            sample.transcript = "changed without revision"
        elif mutation == "delete_sample":
            session.delete(sample)
        elif mutation == "delete_voice":
            session.delete(sample)
            session.flush()
            session.delete(voice)
        elif mutation == "artifact_hash":
            artifact.content_hash = "f" * 64
        elif mutation == "artifact_path":
            artifact.relative_path = "uploads/missing.wav"
        elif mutation == "artifact_state":
            artifact.state = "deleted"
        elif mutation == "bytes":
            h.paths.managed_path(artifact.relative_path).write_bytes(wav_bytes(2))
    with pytest.raises(SpeechInputError):
        h.publish(capture, prepared)
    cleanup_speech_input(prepared)
    assert not h.paths.managed_path(prepared.snapshot.relative_path).exists()
    assert _count(h) == 1


def test_real_byte_hash_mismatch_and_preparation_failure_cleanup(harness: Harness) -> None:
    h = harness
    with h.database.session() as session:
        artifact = session.get(Artifact, h.artifact_id)
        assert artifact is not None
        h.paths.managed_path(artifact.relative_path).write_bytes(wav_bytes(2))
    capture = h.capture()
    with pytest.raises(SpeechInputError, match="hash"):
        h.prepare(capture)
    assert list(h.paths.artifacts.glob("speech-input-*")) == []
    with h.database.session() as session:
        artifact = session.get(Artifact, h.artifact_id)
        assert artifact is not None
        artifact.content_hash = None
    calls = []

    def fail_after_prepare() -> None:
        calls.append(True)
        if len(calls) == 2:
            raise RuntimeError("proof revoked")

    with pytest.raises(RuntimeError, match="proof revoked"):
        prepare_speech_input(h.database, h.paths, h.capture(), before_io=fail_after_prepare)
    assert list(h.paths.artifacts.glob("speech-input-*")) == []


def test_foreign_owner_is_denied_before_material_reads(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("foreign material read")

    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    with pytest.raises(SpeechInputError):
        h.capture(owner=h.other)
    with pytest.raises(SpeechInputError):
        h.load(pin, owner=h.other)


def test_no_reference_and_explicit_reference_fast_paths(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("unneeded material access")

    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    h.settings["voice"] = "ordinary built in voice"
    capture = h.capture(explicit=False)
    assert not capture.requires_files
    assert h.load(None) == h.settings
    h.settings["voice"] = "chosen"
    h.settings["audio_cpp_voice_ref"] = {"type": "base64", "data": "existing"}
    assert h.capture(explicit=False).settings == h.settings
    h.settings["service"] = "openai"
    assert not h.capture().requires_files


def test_explicit_managed_choice_replaces_inherited_reference(harness: Harness) -> None:
    h = harness
    h.settings["audio_cpp_voice_ref"] = {"data": "stale"}
    capture = h.capture()
    assert capture.requires_files
    assert "audio_cpp_voice_ref" not in capture.settings
    prepared = h.prepare(capture)
    result = h.load(h.publish(capture, prepared))
    assert "stale" not in result["audio_cpp_voice_ref"]["data"]


@pytest.mark.parametrize(
    "pin",
    [
        {},
        {
            "version": True,
            "artifact_id": "00000000-0000-0000-0000-000000000000",
            "content_hash": "0" * 64,
        },
        {"version": 1, "artifact_id": "00000000000000000000000000000000", "content_hash": "0" * 64},
        {
            "version": 1,
            "artifact_id": "00000000-0000-0000-0000-000000000000",
            "content_hash": "F" * 64,
        },
        {
            "version": 1,
            "artifact_id": "00000000-0000-0000-0000-000000000000",
            "content_hash": "0" * 64,
            "extra": 1,
        },
    ],
)
def test_strict_pins(pin: object) -> None:
    with pytest.raises(SpeechInputError):
        validate_speech_input_pin(pin)


@pytest.mark.parametrize(
    "mutation",
    [
        "snapshot_bytes",
        "audio_bytes",
        "missing_edge",
        "edge_relation",
        "extra_edge",
        "header_role",
        "audio_role",
        "audio_root",
    ],
)
def test_corrupt_retained_material_or_graph_is_rejected(harness: Harness, mutation: str) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    with h.database.session() as session:
        header, audio = (
            session.get(Artifact, prepared.snapshot.artifact_id),
            session.get(Artifact, prepared.audio.artifact_id),
        )
        assert header is not None and audio is not None
        edge = session.get(ArtifactEdge, (audio.id, header.id))
        assert edge is not None
        if mutation == "snapshot_bytes":
            h.paths.managed_path(header.relative_path).write_bytes(b"{}")
        elif mutation == "audio_bytes":
            h.paths.managed_path(audio.relative_path).write_bytes(wav_bytes(2))
        elif mutation == "missing_edge":
            session.delete(edge)
        elif mutation == "edge_relation":
            edge.relation = "derived_from"
        elif mutation == "extra_edge":
            session.add(
                ArtifactEdge(
                    parent_artifact_id=h.artifact_id,
                    child_artifact_id=header.id,
                    relation="speech_reference",
                )
            )
        elif mutation == "header_role":
            header.role = "other"
        elif mutation == "audio_role":
            audio.role = "other"
        elif mutation == "audio_root":
            foreign = Artifact(
                workspace_id=h.other.workspace_id,
                kind="audio",
                role="speech_reference_audio",
                mime_type="audio/wav",
                relative_path="uploads/foreign-retained.wav",
                state="current",
            )
            session.add(foreign)
            session.flush()
            edge.parent_artifact_id = foreign.id
    with pytest.raises(SpeechInputError):
        h.load(pin)


def test_project_and_session_roots_are_exact_and_current(harness: Harness) -> None:
    h = harness
    with h.database.session() as session:
        record = SessionRecord(name="speech project", workflow_kind="audiobook")
        project = create_session_project(session, record, workspace_id=h.owner.workspace_id)
        owner = SpeechInputOwner(h.owner.workspace_id, project.id, record.id)
    capture = h.capture(owner=owner)
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    assert h.load(pin, owner=owner)
    for wrong in (
        h.owner,
        SpeechInputOwner(owner.workspace_id, owner.project_id),
        SpeechInputOwner(h.other.workspace_id, owner.project_id, owner.session_id),
        SpeechInputOwner(owner.workspace_id, None, owner.session_id),
    ):
        with pytest.raises(SpeechInputError):
            h.load(pin, owner=wrong)
        with pytest.raises(SpeechInputError):
            h.capture(
                owner=wrong
            ) if wrong.project_id != owner.project_id and wrong.session_id else h.load(
                pin, owner=wrong
            )


def test_alias_ambiguity_and_legacy_four_tuple(harness: Harness) -> None:
    h = harness
    with h.database.session() as session:
        voice = session.get(Voice, h.voice_id)
        assert voice is not None
        duplicate = Voice(
            workspace_id=h.owner.workspace_id, name="Other name", metadata_json=voice.metadata_json
        )
        session.add(duplicate)
        session.flush()
        session.add(
            VoiceSample(
                workspace_id=h.owner.workspace_id, voice_id=duplicate.id, artifact_id=h.artifact_id
            )
        )
    with pytest.raises(SpeechInputError, match="ambiguous"):
        h.capture(explicit=False)
    assert h.capture().requires_files
    with h.database.snapshot_session() as session:
        legacy = resolve_audio_cpp_voice_reference(
            session, h.paths, h.settings, workspace_id=h.owner.workspace_id
        )
        assert legacy is not None and len(legacy) == 4
        with pytest.raises(ValueError, match="ambiguous"):
            resolve_audio_cpp_voice_reference(
                session,
                h.paths,
                h.settings,
                workspace_id=h.owner.workspace_id,
                strict_selection=True,
            )
        explicit = resolve_audio_cpp_voice_reference(
            session,
            h.paths,
            h.settings,
            workspace_id=h.owner.workspace_id,
            voice_id=h.voice_id,
            strict_selection=True,
        )
        assert explicit is not None and explicit[3].id == h.sample_id


def test_selected_custom_audio_cpp_adapter_and_exact_registration(harness: Harness) -> None:
    h = harness
    with h.database.session() as session:
        voice = session.get(Voice, h.voice_id)
        assert voice is not None
        voice.metadata_json = {
            "providers": {
                "external-speech": {
                    "resource_kind": "linked_reference",
                    "status": "ready",
                    "voice_id": "chosen",
                }
            }
        }
    h.settings.update(
        service="Custom",
        preview_service_id="external-speech",
        preview_adapter="audio_cpp",
        openai_audio_endpoint="external-speech",
        provider_configs=[
            {
                "id": "external-speech",
                "api_base": "http://invalid.example",
                "adapter": "audio_cpp",
            }
        ],
    )
    capture = h.capture()
    assert capture.requires_files
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    assert h.load(pin)["audio_cpp_reference_text"] == "Reviewed source."
    del h.settings["preview_adapter"]
    assert h.capture().requires_files


def test_capture_and_publication_do_not_read_source_or_prepared_bytes(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("byte read in capture/publication writer")

    with monkeypatch.context() as patch:
        patch.setattr(speech_inputs, "read_verified_file", forbidden)
        capture = h.capture()
    prepared = h.prepare(capture)
    with monkeypatch.context() as patch:
        patch.setattr(speech_inputs, "read_verified_file", forbidden)
        h.publish(capture, prepared)


def test_corrupt_reuse_candidates_are_skipped_without_borrowing_foreign_owner(
    harness: Harness,
) -> None:
    h = harness
    capture = h.capture()
    first = h.prepare(capture)
    h.publish(capture, first)
    h.paths.managed_path(first.snapshot.relative_path).write_bytes(b"corrupt")
    second = h.prepare()
    assert second.snapshot.owned and not second.audio.owned
    cleanup_speech_input(second)
    h.paths.managed_path(first.audio.relative_path).write_bytes(b"corrupt")
    third = h.prepare()
    assert third.snapshot.owned and third.audio.owned
    cleanup_speech_input(third)


def test_reuse_proof_failure_stops_before_next_material_read(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    h.publish(capture, prepared)
    calls, reads = [], []
    original = speech_inputs.read_verified_file

    def observe(*args: Any, **kwargs: Any) -> Any:
        reads.append(args[1])
        return original(*args, **kwargs)

    def proof() -> None:
        calls.append(True)
        if len(calls) == 2:
            raise SpeechInputError("revoked")

    monkeypatch.setattr(speech_inputs, "read_verified_file", observe)
    with pytest.raises(SpeechInputError, match="revoked"):
        prepare_speech_input(h.database, h.paths, h.capture(), before_io=proof)
    assert len(reads) == 1


@pytest.mark.parametrize(
    "mutation", ["unknown", "version", "reference_text", "audio_hash", "fingerprint", "duplicate"]
)
def test_strict_envelope_even_when_artifact_and_pin_hash_are_replaced(
    harness: Harness, mutation: str
) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    path = h.paths.managed_path(prepared.snapshot.relative_path)
    envelope = json.loads(path.read_bytes())
    if mutation == "unknown":
        envelope["artifact_id"] = h.artifact_id
    elif mutation == "version":
        envelope["version"] = True
    elif mutation == "reference_text":
        envelope["reference_text"] = "é" * (32 * 1024 + 1)
    elif mutation == "audio_hash":
        envelope["audio_sha256"] = "BAD"
    elif mutation == "fingerprint":
        envelope["source_fingerprint"] = "0" * 64
    data = json.dumps(envelope).encode()
    if mutation == "duplicate":
        data = data[:-1] + b', "version": 1}'
    path.write_bytes(data)
    digest = hashlib.sha256(data).hexdigest()
    with h.database.session() as session:
        artifact = session.get(Artifact, prepared.snapshot.artifact_id)
        assert artifact is not None
        artifact.content_hash, artifact.size_bytes = digest, len(data)
    pin["content_hash"] = digest
    with pytest.raises(SpeechInputError):
        h.load(pin)


@pytest.mark.parametrize(
    "field,value", [("model", "other"), ("voice", "other"), ("service", "openai")]
)
def test_retained_input_matches_exact_selected_settings(
    harness: Harness, field: str, value: str
) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    h.settings[field] = value
    with pytest.raises(SpeechInputError):
        h.load(pin)


def test_limits_source_race_and_replaced_prepared_inode(harness: Harness) -> None:
    h = harness
    with h.database.snapshot_session() as session:
        artifact = session.get(Artifact, h.artifact_id)
        assert artifact is not None
        path = h.paths.managed_path(artifact.relative_path)
    capture = h.capture()
    path.write_bytes(wav_bytes(2))
    with pytest.raises(SpeechInputError, match="changed"):
        h.prepare(capture)
    path.write_bytes(h.data)
    capture = h.capture()
    prepared = h.prepare(capture)
    replacement = h.paths.uploads / "replace.json"
    replacement.write_bytes(h.paths.managed_path(prepared.snapshot.relative_path).read_bytes())
    replacement.replace(h.paths.managed_path(prepared.snapshot.relative_path))
    with pytest.raises(SpeechInputError, match="changed"):
        h.publish(capture, prepared)
    cleanup_speech_input(prepared)
    assert h.paths.managed_path(prepared.snapshot.relative_path).is_file()
    path.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    with pytest.raises(SpeechInputError, match="5 MiB"):
        h.capture()


def test_reviewed_text_bound_is_utf8_bytes(harness: Harness) -> None:
    h = harness
    with h.database.session() as session:
        sample = session.get(VoiceSample, h.sample_id)
        assert sample is not None
        sample.transcript = "é" * (32 * 1024 + 1)
    with pytest.raises(SpeechInputError, match="too large"):
        h.capture()


@pytest.mark.parametrize(
    "mutation",
    ["snapshot_state", "audio_state", "snapshot_hash", "audio_hash", "edge", "library_delete"],
)
def test_db_only_current_graph_check(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, mutation: str
) -> None:
    h = harness
    capture = h.capture()
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    with h.database.session() as session:
        header, audio = (
            session.get(Artifact, prepared.snapshot.artifact_id),
            session.get(Artifact, prepared.audio.artifact_id),
        )
        assert header is not None and audio is not None
        if mutation == "snapshot_state":
            header.state = "deleted"
        elif mutation == "audio_state":
            audio.state = "stale"
        elif mutation == "snapshot_hash":
            header.content_hash = "0" * 64
        elif mutation == "audio_hash":
            audio.content_hash = "0" * 64
        elif mutation == "edge":
            edge = session.get(ArtifactEdge, (audio.id, header.id))
            assert edge is not None
            edge.relation = "other"
        elif mutation == "library_delete":
            session.execute(delete(VoiceSample).where(VoiceSample.id == h.sample_id))
            session.execute(delete(Voice).where(Voice.id == h.voice_id))

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("file I/O in DB-only speech input check")

    monkeypatch.setattr(speech_inputs, "read_verified_file", forbidden)
    monkeypatch.setattr(speech_inputs, "file_identity", forbidden)
    with h.database.snapshot_session() as session:
        if mutation == "library_delete":
            speech_inputs.check_speech_input(
                session, pin, owner=h.owner, audio_sha256=prepared.audio.content_hash
            )
        else:
            with pytest.raises(SpeechInputError):
                speech_inputs.check_speech_input(
                    session, pin, owner=h.owner, audio_sha256=prepared.audio.content_hash
                )


def test_workspace_project_and_session_reuse_never_crosses_owner(harness: Harness) -> None:
    h = harness
    capture = h.capture()
    workspace = h.prepare(capture)
    h.publish(capture, workspace)
    with h.database.session() as session:
        first_record = SessionRecord(name="Project reference", workflow_kind="audiobook")
        project = create_session_project(session, first_record, workspace_id=h.owner.workspace_id)
        second_record = SessionRecord(name="Second session", workflow_kind="audiobook")
        create_session_project(
            session, second_record, project_id=project.id, workspace_id=h.owner.workspace_id
        )
        first_owner = SpeechInputOwner(h.owner.workspace_id, project.id, first_record.id)
        second_owner = SpeechInputOwner(h.owner.workspace_id, project.id, second_record.id)
    first_capture = h.capture(owner=first_owner)
    first = h.prepare(first_capture)
    h.publish(first_capture, first)
    second_capture = h.capture(owner=second_owner)
    second = h.prepare(second_capture)
    assert first.audio.owned and first.snapshot.owned
    assert second.audio.owned and second.snapshot.owned
    assert (
        len({workspace.audio.artifact_id, first.audio.artifact_id, second.audio.artifact_id}) == 3
    )
    cleanup_speech_input(second)


def test_envelope_size_limit_before_staging(harness: Harness) -> None:
    h = harness
    h.settings["model"] = "m" * (256 * 1024)
    with pytest.raises(SpeechInputError, match="too large"):
        h.prepare()
    assert list(h.paths.artifacts.glob("speech-input-*")) == []


def test_empty_recorded_hash_uses_real_bytes_and_unreviewed_text_is_empty(harness: Harness) -> None:
    h = harness
    with h.database.session() as session:
        artifact, sample = (
            session.get(Artifact, h.artifact_id),
            session.get(VoiceSample, h.sample_id),
        )
        assert artifact is not None and sample is not None
        artifact.content_hash = None
        sample.transcript_reviewed = False
    capture = h.capture()
    prepared = h.prepare(capture)
    pin = h.publish(capture, prepared)
    result = h.load(pin)
    assert result["audio_cpp_voice_ref_hash"] == hashlib.sha256(h.data).hexdigest()
    assert result["audio_cpp_reference_text"] == ""


def test_concurrent_duplicate_preparations_each_adopt_their_owned_files(harness: Harness) -> None:
    h = harness
    capture = h.capture()
    first, second = h.prepare(capture), h.prepare(capture)
    assert first.snapshot.owned and second.snapshot.owned
    first_pin, second_pin = h.publish(capture, first), h.publish(capture, second)
    assert first_pin != second_pin and _count(h) == 5
    assert h.load(first_pin) == h.load(second_pin)
    for prepared in (first, second):
        assert h.paths.managed_path(prepared.snapshot.relative_path).is_file()
        assert h.paths.managed_path(prepared.audio.relative_path).is_file()
