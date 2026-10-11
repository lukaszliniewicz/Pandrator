"""Managed, byte-preserving sound and voice-reference import tests."""

from __future__ import annotations

import hashlib
import wave
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.runtime import DataPaths
from pandrator.web import audio_imports
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.models import Artifact, SoundVariant
from pandrator.web.sound_imports import (
    cleanup_prepared_import,
    import_in_session,
    prepare_import,
)


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
    monkeypatch.setattr(
        audio_imports,
        "probe_audio_stream",
        lambda _path: SimpleNamespace(duration_ms=250, sample_rate_hz=44100, channels=2),
    )
    try:
        yield database, paths, artifacts
    finally:
        database.dispose()


def _wav(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(44100)
        audio.writeframes(b"\x01\x00\x02\x00" * 11025)
    return path.read_bytes()


def test_sound_import_preserves_source_bytes_and_registers_global_original(domain, tmp_path: Path):
    database, paths, artifacts = domain
    source = tmp_path / "private-upload" / "ambient.wav"
    original = _wav(source)
    prepared = prepare_import(paths, artifacts, source, "Ambient rain")
    assert prepared.sha256 == hashlib.sha256(original).hexdigest()
    assert prepared.staged_path.read_bytes() == original
    assert prepared.display_name == "Ambient rain"

    with database.immediate_session() as session:
        result = import_in_session(session, paths, artifacts, prepared)
    cleanup_prepared_import(prepared)
    assert source.read_bytes() == original
    assert not prepared.staged_path.exists()
    assert result["candidate"]["status"] == "ready"

    with database.session() as session:
        candidate = session.get(SoundVariant, result["candidate"]["id"])
        artifact = session.get(Artifact, result["artifact_id"])
        assert candidate is not None
        assert candidate.item_id is None
        assert candidate.duration_seconds == 0.25
        assert candidate.provenance_json == {
            "operation": "import",
            "display_name": "Ambient rain",
        }
        assert artifact is not None
        assert (artifact.kind, artifact.role, artifact.session_id) == (
            "audio",
            "sound.original",
            None,
        )
        assert artifact.content_hash == prepared.sha256
        managed = paths.managed_path(artifact.relative_path)
    assert managed.read_bytes() == original


def test_import_rollback_removes_only_its_published_copy(domain, tmp_path: Path):
    database, paths, artifacts = domain
    source = tmp_path / "upload.wav"
    original = _wav(source)
    prepared = prepare_import(paths, artifacts, source, "Temporary")
    with pytest.raises(RuntimeError, match="simulate caller failure"):
        with database.immediate_session() as session:
            import_in_session(session, paths, artifacts, prepared)
            raise RuntimeError("simulate caller failure")

    cleanup_prepared_import(prepared)
    assert source.read_bytes() == original
    with database.session() as session:
        assert list(session.scalars(select(Artifact)).all()) == []
        assert list(session.scalars(select(SoundVariant)).all()) == []
    assert list((paths.root / "sounds" / "audio").glob("*.wav")) == []


def test_shared_audio_publisher_limits_roles_and_does_not_make_sound_candidates(
    domain, tmp_path: Path
):
    database, paths, artifacts = domain
    source = tmp_path / "voice-reference.wav"
    original = _wav(source)
    prepared = audio_imports.prepare_audio_import(paths, artifacts, source, "Character voice")
    with database.immediate_session() as session:
        artifact = audio_imports.publish_audio_import_in_session(
            session,
            prepared,
            artifacts=artifacts,
            paths=paths,
            role="voice.reference",
        )
        artifact_id = artifact.id
    audio_imports.cleanup_prepared_audio_import(prepared)
    with database.session() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None
        assert (artifact.kind, artifact.role, artifact.session_id) == (
            "audio",
            "voice.reference",
            None,
        )
        assert list(session.scalars(select(SoundVariant)).all()) == []
        published = paths.managed_path(artifact.relative_path)
    assert published.read_bytes() == original

    rejected = audio_imports.prepare_audio_import(paths, artifacts, source, "Rejected role")
    try:
        with database.immediate_session() as session:
            with pytest.raises(ValueError, match="Unsupported managed audio import role"):
                audio_imports.publish_audio_import_in_session(
                    session,
                    rejected,
                    artifacts=artifacts,
                    paths=paths,
                    role="arbitrary.role",  # type: ignore[arg-type]
                )
    finally:
        audio_imports.cleanup_prepared_audio_import(rejected)


def test_failed_probe_leaves_no_internal_stage_and_preserves_upload(domain, tmp_path: Path, monkeypatch):
    _database, paths, artifacts = domain
    source = tmp_path / "invalid.wav"
    source.write_bytes(b"not a waveform")
    before = set((paths.root / "sounds" / "staging").glob("audio-import-*"))
    monkeypatch.setattr(
        audio_imports,
        "probe_audio_stream",
        lambda _path: (_ for _ in ()).throw(ValueError("invalid audio")),
    )
    with pytest.raises(ValueError, match="invalid audio"):
        prepare_import(paths, artifacts, source, "Invalid")
    assert source.read_bytes() == b"not a waveform"
    assert set((paths.root / "sounds" / "staging").glob("audio-import-*")) == before
