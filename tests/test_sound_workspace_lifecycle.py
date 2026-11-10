"""Workspace isolation across sound creation, queries, imports, and workers."""

from __future__ import annotations

import sqlite3
import threading
import wave
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import select

from pandrator.logic import audio_cpp_sfx
from pandrator.runtime import DataPaths
from pandrator.web import audio_imports
from pandrator.web import jobs as jobs_module
from pandrator.web.accounts.identity import initial_personal_workspace
from pandrator.web.accounts.models import User, Workspace, WorkspaceMembership
from pandrator.web.artifacts import ArtifactService
from pandrator.web.database import Database, sqlite_url
from pandrator.web.job_execution import current_job_lease
from pandrator.web.jobs import JobQueue
from pandrator.web.models import Artifact, Job, SoundItem, SoundVariant, utcnow
from pandrator.web.sound_imports import (
    cleanup_prepared_import,
    import_in_session,
    prepare_import,
)
from pandrator.web.sound_service import SoundService
from tests.legacy_schema_support import prepare_read_only_pinned_schema
from tests.web_test_support import prepare_web_test_data_root


def _use_pre_library_schema(database: Database) -> None:
    """Exercise stored-corruption defenses before the library constraints."""
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(database.path))
    command.downgrade(config, "0077_media_scope_consistency")
    prepare_read_only_pinned_schema(
        database.path,
        (Artifact, Job, SoundItem, SoundVariant, Workspace),
    )


@dataclass
class SoundDomain:
    database: Database
    paths: DataPaths
    artifacts: ArtifactService
    jobs: JobQueue
    sounds: SoundService
    runtime_identity: dict[str, str]


@pytest.fixture
def domain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[SoundDomain]:
    paths = prepare_web_test_data_root(tmp_path / "data")
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
        "model_revision": "test-revision",
        "minimum_runtime_version": "0.9.0",
    }

    def runtime_identity(_settings, *, model_id="stable_audio_3_small_sfx_q8_0"):
        return {**identity, "model_id": model_id}

    monkeypatch.setattr(audio_cpp_sfx, "runtime_identity", runtime_identity)
    monkeypatch.setattr(audio_cpp_sfx, "require_backend", lambda _settings, _backend: None)

    def settings_provider() -> dict[str, str | int]:
        return {
            "audio_cpp_executable": str(tmp_path / "synthetic-audio-cpp"),
            "audio_cpp_cache_dir": str(tmp_path / "synthetic-cache"),
            "audio_cpp_timeout_seconds": 30,
        }

    sounds = SoundService(
        database,
        paths,
        artifacts,
        jobs,
        settings_provider=settings_provider,
    )
    monkeypatch.setattr(
        audio_imports,
        "probe_audio_stream",
        lambda _path: SimpleNamespace(duration_ms=250, sample_rate_hz=44100, channels=2),
    )
    try:
        yield SoundDomain(database, paths, artifacts, jobs, sounds, identity)
    finally:
        database.dispose()


def _workspace(database: Database, name: str) -> str:
    with database.immediate_session() as session:
        owner = session.scalar(select(User).where(User.legacy_subject == "owner"))
        assert owner is not None
        workspace = Workspace(kind="team", name=name)
        session.add(workspace)
        session.flush()
        session.add(
            WorkspaceMembership(
                workspace_id=workspace.id,
                user_id=owner.id,
                role="owner",
            )
        )
        workspace_id = workspace.id
    return workspace_id


def _new_variant(
    database: Database,
    *,
    workspace_id: str,
    item_id: str | None = None,
    state: str = "discarded",
    fingerprint: str | None = None,
    artifact_id: str | None = None,
) -> str:
    with database.immediate_session() as session:
        variant = SoundVariant(
            workspace_id=workspace_id,
            item_id=item_id,
            artifact_id=artifact_id,
            state=state,
            fingerprint=fingerprint,
            provenance_json={"operation": "test"},
        )
        session.add(variant)
        session.flush()
        variant_id = variant.id
    return variant_id


def _new_item(database: Database, *, workspace_id: str, name: str) -> str:
    with database.immediate_session() as session:
        item = SoundItem(
            workspace_id=workspace_id,
            name=name,
            tags_json=[],
            revision=1,
            archived=False,
        )
        session.add(item)
        session.flush()
        item_id = item.id
    return item_id


def _admit(
    domain: SoundDomain,
    request: dict,
    *,
    workspace_id: str | None = None,
) -> dict:
    prepared = domain.sounds.prepare_generation(request)
    with domain.database.immediate_session() as session:
        return domain.sounds.generate_in_session(
            session,
            request,
            prepared=prepared,
            workspace_id=workspace_id,
        )


def _claim(domain: SoundDomain, receipt: dict) -> tuple[dict, str]:
    owner_id = "sound-workspace-test-worker"
    with domain.database.immediate_session() as session:
        job = session.get(Job, receipt["job_id"])
        assert job is not None
        job.status = "running"
        job.lease_owner = owner_id
        job.lease_generation = 1
        job.lease_expires_at = utcnow() + timedelta(minutes=2)
        payload = {
            **job.payload_json,
            "_job_id": job.id,
            "_lease_generation": 1,
        }
    return payload, owner_id


def _write_wave(path: Path, *, frames: int = 4410) -> dict[str, int | float]:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(2)
        output.setsampwidth(2)
        output.setframerate(44100)
        output.writeframes(b"\x01\x00\x02\x00" * frames)
    return {
        "frames": frames,
        "sample_rate": 44100,
        "channels": 2,
        "sample_width": 2,
        "duration_seconds": frames / 44100,
    }


def _install_synthetic_generator(monkeypatch: pytest.MonkeyPatch) -> list[bytes]:
    outputs: list[bytes] = []

    def generate_sound(_request, output_path, *, settings, cancel_event, progress):
        assert settings["audio_cpp_executable"].endswith("synthetic-audio-cpp")
        assert not cancel_event.is_set()
        progress("synthetic", 1, 1)
        properties = _write_wave(output_path)
        outputs.append(output_path.read_bytes())
        return properties

    monkeypatch.setattr(audio_cpp_sfx, "generate_sound", generate_sound)
    return outputs


def _run(domain: SoundDomain, payload: dict, owner_id: str) -> dict:
    token = current_job_lease.set(
        {
            "job_id": payload["_job_id"],
            "owner_id": owner_id,
            "lease_generation": payload["_lease_generation"],
        }
    )
    try:
        return domain.sounds.run_generate(
            payload,
            lambda _fraction, _detail: None,
            threading.Event(),
        )
    finally:
        current_job_lease.reset(token)


def _all_pages(fetch) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        page = fetch(cursor=cursor, limit=1)
        rows.extend(page["items"])
        cursor = page["next_cursor"]
        if cursor is None:
            return rows


def _command_snapshot(database: Database) -> tuple[list[tuple], list[tuple]]:
    with database.session() as session:
        items = [
            (item.id, item.name, item.revision, item.preferred_variant_id, item.archived)
            for item in session.scalars(select(SoundItem).order_by(SoundItem.id)).all()
        ]
        variants = [
            (variant.id, variant.workspace_id, variant.item_id, variant.state)
            for variant in session.scalars(select(SoundVariant).order_by(SoundVariant.id)).all()
        ]
    return items, variants


def _write_source_wav(path: Path) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    _write_wave(path)
    return path.read_bytes()


def test_generation_cache_and_paged_library_queries_stay_in_one_workspace(domain):
    workspace_a = _workspace(domain.database, "Sound workspace A")
    workspace_b = _workspace(domain.database, "Sound workspace B")
    request = {"prompt": "same synthetic rain", "duration_seconds": 1.0, "seed": 71}

    first_a = _admit(domain, request, workspace_id=workspace_a)
    first_b = _admit(domain, request, workspace_id=workspace_b)
    repeated_a = _admit(domain, request, workspace_id=workspace_a)
    repeated_b = _admit(domain, request, workspace_id=workspace_b)

    assert first_a["candidate_id"] != first_b["candidate_id"]
    assert repeated_a["candidate_id"] == first_a["candidate_id"]
    assert repeated_b["candidate_id"] == first_b["candidate_id"]
    assert repeated_a["cache_hit"] is True
    assert repeated_b["cache_hit"] is True
    with domain.database.session() as session:
        candidates = {
            variant.id: variant.workspace_id
            for variant in session.scalars(select(SoundVariant)).all()
        }
        for receipt, workspace_id in ((first_a, workspace_a), (first_b, workspace_b)):
            job = session.get(Job, receipt["job_id"])
            assert job is not None
            assert job.payload_json["workspace_id"] == workspace_id
            assert candidates[receipt["candidate_id"]] == workspace_id

    item_ids = {
        workspace_a: [
            _new_item(domain.database, workspace_id=workspace_a, name=f"A {i}") for i in range(3)
        ],
        workspace_b: [
            _new_item(domain.database, workspace_id=workspace_b, name=f"B {i}") for i in range(3)
        ],
    }
    candidate_ids = {
        workspace_a: [first_a["candidate_id"]]
        + [_new_variant(domain.database, workspace_id=workspace_a) for _ in range(2)],
        workspace_b: [first_b["candidate_id"]]
        + [_new_variant(domain.database, workspace_id=workspace_b) for _ in range(2)],
    }

    sound_rows = _all_pages(
        lambda **page: domain.sounds.list_sounds(workspace_id=workspace_a, **page)
    )
    candidate_rows = _all_pages(
        lambda **page: domain.sounds.list_candidates(workspace_id=workspace_a, **page)
    )
    assert {row["id"] for row in sound_rows} == set(item_ids[workspace_a])
    assert {row["id"] for row in candidate_rows} == set(candidate_ids[workspace_a])
    assert not (set(item_ids[workspace_b]) & {row["id"] for row in sound_rows})
    assert not (set(candidate_ids[workspace_b]) & {row["id"] for row in candidate_rows})

    with pytest.raises(KeyError):
        domain.sounds.get_sound(item_ids[workspace_b][0], workspace_id=workspace_a)
    with pytest.raises(KeyError):
        domain.sounds.get_candidate(first_b["candidate_id"], workspace_id=workspace_a)
    assert (
        domain.sounds.get_sound(item_ids[workspace_a][0], workspace_id=workspace_a)["id"]
        in item_ids[workspace_a]
    )
    assert (
        domain.sounds.get_candidate(first_a["candidate_id"], workspace_id=workspace_a)["id"]
        == first_a["candidate_id"]
    )


def test_foreign_preferred_variant_is_never_projected(domain):
    workspace_a = _workspace(domain.database, "Preferred workspace A")
    workspace_b = _workspace(domain.database, "Preferred workspace B")
    item_id = _new_item(domain.database, workspace_id=workspace_a, name="Owned sound")
    _use_pre_library_schema(domain.database)
    foreign_variant = _new_variant(
        domain.database,
        workspace_id=workspace_b,
        item_id=item_id,
        state="ready",
    )
    with domain.database.immediate_session() as session:
        item = session.get(SoundItem, item_id)
        assert item is not None
        item.preferred_variant_id = foreign_variant

    with pytest.raises(ValueError, match="preferred variant"):
        domain.sounds.get_sound(item_id, workspace_id=workspace_a)
    listed = domain.sounds.list_sounds(workspace_id=workspace_a)
    owned_item = next(row for row in listed["items"] if row["id"] == item_id)
    assert owned_item["preferred_variant"] is None


def test_cross_workspace_commands_reject_before_any_change_even_when_committed(domain):
    workspace_a = _workspace(domain.database, "Command workspace A")
    workspace_b = _workspace(domain.database, "Command workspace B")

    artifacts: dict[str, str] = {}
    for workspace_id, filename in ((workspace_a, "a.wav"), (workspace_b, "b.wav")):
        path = domain.paths.root / "synthetic" / filename
        _write_source_wav(path)
        artifact = domain.artifacts.register(
            path,
            kind="audio",
            role="sound.original",
            session_id=None,
            workspace_id=workspace_id,
            calculate_hash=True,
        )
        artifacts[workspace_id] = artifact.id

    candidate_a = _new_variant(
        domain.database,
        workspace_id=workspace_a,
        state="ready",
        artifact_id=artifacts[workspace_a],
    )
    candidate_a_unbound = _new_variant(
        domain.database,
        workspace_id=workspace_a,
        state="ready",
        artifact_id=artifacts[workspace_a],
    )
    candidate_b = _new_variant(
        domain.database,
        workspace_id=workspace_b,
        state="ready",
        artifact_id=artifacts[workspace_b],
    )
    item_a = _new_item(domain.database, workspace_id=workspace_a, name="Owned by A")
    item_b = _new_item(domain.database, workspace_id=workspace_b, name="Owned by B")
    with domain.database.immediate_session() as session:
        candidate = session.get(SoundVariant, candidate_a)
        assert candidate is not None
        candidate.item_id = item_a
        session.flush([candidate])
        session.get(SoundItem, item_a).preferred_variant_id = candidate_a

    # Model a tampered preferred link: the row FK is valid, but its variant
    # carries a different owner. Validation must happen before item edits.
    _use_pre_library_schema(domain.database)
    foreign_preferred = _new_variant(
        domain.database,
        workspace_id=workspace_b,
        item_id=item_a,
        state="ready",
        artifact_id=artifacts[workspace_b],
    )
    before = _command_snapshot(domain.database)

    with domain.database.session() as session:
        with pytest.raises(KeyError):
            domain.sounds.save_in_session(
                session,
                {"candidate_id": candidate_b, "name": "must not create"},
                workspace_id=workspace_a,
            )
    assert _command_snapshot(domain.database) == before

    with domain.database.session() as session:
        with pytest.raises(KeyError):
            domain.sounds.save_in_session(
                session,
                {
                    "candidate_id": candidate_a_unbound,
                    "item_id": item_b,
                    "expected_revision": 1,
                },
                workspace_id=workspace_a,
            )
    assert _command_snapshot(domain.database) == before

    with domain.database.session() as session:
        with pytest.raises(KeyError):
            domain.sounds.save_in_session(
                session,
                {
                    "candidate_id": candidate_b,
                    "item_id": item_a,
                    "expected_revision": 1,
                },
                workspace_id=workspace_a,
            )
    assert _command_snapshot(domain.database) == before

    with domain.database.session() as session:
        with pytest.raises(ValueError):
            domain.sounds.update_in_session(
                session,
                item_a,
                {
                    "expected_revision": 1,
                    "name": "must not be committed",
                    "preferred_variant_id": foreign_preferred,
                },
                workspace_id=workspace_a,
            )
    assert _command_snapshot(domain.database) == before

    with domain.database.session() as session:
        with pytest.raises(KeyError):
            domain.sounds.discard_in_session(
                session,
                candidate_b,
                workspace_id=workspace_a,
            )
    assert _command_snapshot(domain.database) == before


def test_import_and_generation_publish_bytes_under_the_selected_workspace(
    domain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    workspace_id = _workspace(domain.database, "Import and generation workspace")
    source = tmp_path / "upload" / "ambient.wav"
    original = _write_source_wav(source)
    prepared = prepare_import(domain.paths, domain.artifacts, source, "Ambient rain")
    try:
        with domain.database.immediate_session() as session:
            imported = import_in_session(
                session,
                domain.paths,
                domain.artifacts,
                prepared,
                workspace_id=workspace_id,
            )
        assert source.read_bytes() == original
        assert prepared.staged_path.exists() is False
        with domain.database.session() as session:
            imported_candidate = session.get(SoundVariant, imported["candidate"]["id"])
            imported_artifact = session.get(Artifact, imported["artifact_id"])
            assert imported_candidate is not None and imported_artifact is not None
            assert imported_candidate.workspace_id == workspace_id
            assert imported_artifact.workspace_id == workspace_id
            imported_path = domain.paths.managed_path(imported_artifact.relative_path)
        assert imported_path.read_bytes() == original
    finally:
        cleanup_prepared_import(prepared)

    outputs = _install_synthetic_generator(monkeypatch)
    request = {"prompt": "synthetic thunder", "duration_seconds": 1.0, "seed": 83}
    receipt = _admit(domain, request, workspace_id=workspace_id)
    payload, worker_id = _claim(domain, receipt)
    result = _run(domain, payload, worker_id)
    assert result["status"] == "ready"
    assert len(outputs) == 1

    with domain.database.session() as session:
        candidate = session.get(SoundVariant, receipt["candidate_id"])
        artifact = session.get(Artifact, result["artifact_id"])
        assert candidate is not None and artifact is not None
        assert candidate.workspace_id == workspace_id
        assert artifact.workspace_id == candidate.workspace_id
        generated_path = domain.paths.managed_path(artifact.relative_path)
    assert generated_path.read_bytes() == outputs[0]


def test_archived_and_missing_workspaces_fail_before_import_or_generation_mutates(
    domain, tmp_path: Path
):
    archived_workspace = _workspace(domain.database, "Archived sound workspace")
    with domain.database.immediate_session() as session:
        workspace = session.get(Workspace, archived_workspace)
        assert workspace is not None
        workspace.state = "archived"

    request = {"prompt": "must not enqueue", "seed": 97}
    with domain.database.immediate_session() as session:
        with pytest.raises(ValueError, match="inactive"):
            domain.sounds.generate_in_session(
                session,
                request,
                prepared=domain.sounds.prepare_generation(request),
                workspace_id=archived_workspace,
            )
    with domain.database.immediate_session() as session:
        with pytest.raises(ValueError, match="missing workspace"):
            domain.sounds.generate_in_session(
                session,
                request,
                prepared=domain.sounds.prepare_generation(request),
                workspace_id="missing-sound-workspace",
            )
    with domain.database.session() as session:
        assert session.scalar(select(Job.id).where(Job.kind == "sound.generate")) is None
        assert session.scalar(select(SoundVariant.id)) is None

    source = tmp_path / "upload" / "rejected.wav"
    _write_source_wav(source)
    prepared = prepare_import(domain.paths, domain.artifacts, source, "Rejected import")
    staged_path = prepared.staged_path
    try:
        with domain.database.session() as session:
            with pytest.raises(ValueError, match="missing workspace"):
                import_in_session(
                    session,
                    domain.paths,
                    domain.artifacts,
                    prepared,
                    workspace_id="missing-sound-workspace",
                )
        assert staged_path.is_file()
        assert source.is_file()
        with domain.database.session() as session:
            assert session.scalar(select(Artifact.id)) is None
            assert session.scalar(select(SoundVariant.id)) is None
    finally:
        cleanup_prepared_import(prepared)


def test_archived_or_tampered_candidate_ownership_stops_worker_before_provider(
    domain, monkeypatch: pytest.MonkeyPatch
):
    workspace_id = _workspace(domain.database, "Worker sound workspace")
    outputs = _install_synthetic_generator(monkeypatch)
    request = {"prompt": "must not reach provider", "seed": 101}
    receipt = _admit(domain, request, workspace_id=workspace_id)
    payload, worker_id = _claim(domain, receipt)

    with domain.database.immediate_session() as session:
        workspace = session.get(Workspace, workspace_id)
        assert workspace is not None
        workspace.state = "archived"
    with pytest.raises(ValueError, match="inactive workspace"):
        _run(domain, payload, worker_id)
    assert outputs == []
    assert list((domain.paths.root / "sounds" / "audio").glob("*.wav")) == []
    assert list((domain.paths.root / "sounds" / "staging").glob("sound-generate-*.wav")) == []

    # A different active workspace in a queued job payload cannot change the
    # persisted candidate owner.
    workspace_id = _workspace(domain.database, "Second worker sound workspace")
    receipt = _admit(domain, {**request, "seed": 103}, workspace_id=workspace_id)
    with domain.database.immediate_session() as session:
        job = session.get(Job, receipt["job_id"])
        assert job is not None
        job.payload_json = {**job.payload_json, "workspace_id": "tampered-workspace"}
    tampered_payload, worker_id = _claim(domain, receipt)
    with pytest.raises(ValueError, match="missing workspace"):
        _run(domain, tampered_payload, worker_id)
    assert outputs == []
    assert list((domain.paths.root / "sounds" / "audio").glob("*.wav")) == []
    assert list((domain.paths.root / "sounds" / "staging").glob("sound-generate-*.wav")) == []


def test_missing_persisted_candidate_workspace_stops_worker_before_provider(domain, monkeypatch):
    workspace_id = _workspace(domain.database, "Deleted sound workspace")
    outputs = _install_synthetic_generator(monkeypatch)
    request = {"prompt": "missing candidate owner", "seed": 107}
    receipt = _admit(domain, request, workspace_id=workspace_id)
    payload, worker_id = _claim(domain, receipt)

    _use_pre_library_schema(domain.database)
    domain.database.dispose()
    with sqlite3.connect(domain.paths.database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            "UPDATE sound_variants SET workspace_id = ? WHERE id = ?",
            ("missing-sound-workspace", receipt["candidate_id"]),
        )

    with pytest.raises(ValueError, match="missing workspace"):
        _run(domain, payload, worker_id)
    assert outputs == []
    assert list((domain.paths.root / "sounds" / "audio").glob("*.wav")) == []
    assert list((domain.paths.root / "sounds" / "staging").glob("sound-generate-*.wav")) == []


def test_worker_rechecks_candidate_workspace_before_publication(domain, monkeypatch):
    _use_pre_library_schema(domain.database)
    workspace_id = _workspace(domain.database, "Publication sound workspace")
    tampered_workspace_id = _workspace(domain.database, "Tampered sound workspace")
    request = {"prompt": "tamper during synthetic generation", "seed": 108}
    receipt = _admit(domain, request, workspace_id=workspace_id)
    payload, worker_id = _claim(domain, receipt)
    provider_calls = 0

    def tampering_generator(_request, output_path, *, settings, cancel_event, progress):
        nonlocal provider_calls
        provider_calls += 1
        assert settings["audio_cpp_executable"].endswith("synthetic-audio-cpp")
        assert not cancel_event.is_set()
        progress("synthetic", 1, 1)
        properties = _write_wave(output_path)
        with domain.database.immediate_session() as session:
            candidate = session.get(SoundVariant, receipt["candidate_id"])
            assert candidate is not None
            candidate.workspace_id = tampered_workspace_id
        return properties

    monkeypatch.setattr(audio_cpp_sfx, "generate_sound", tampering_generator)
    with pytest.raises(ValueError, match="workspace"):
        _run(domain, payload, worker_id)

    assert provider_calls == 1
    with domain.database.session() as session:
        assert session.scalar(select(Artifact.id)) is None
    assert list((domain.paths.root / "sounds" / "audio").glob("*.wav")) == []
    assert list((domain.paths.root / "sounds" / "staging").glob("sound-generate-*.wav")) == []


def test_ready_replay_rejects_artifact_from_another_workspace(domain, monkeypatch):
    workspace_id = _workspace(domain.database, "Replay sound workspace")
    foreign_workspace_id = _workspace(domain.database, "Foreign replay workspace")
    outputs = _install_synthetic_generator(monkeypatch)
    receipt = _admit(
        domain,
        {"prompt": "ready replay ownership", "seed": 110},
        workspace_id=workspace_id,
    )
    payload, worker_id = _claim(domain, receipt)
    result = _run(domain, payload, worker_id)
    assert result["status"] == "ready"
    assert len(outputs) == 1

    # Keep current-schema generation coverage, then exercise the legacy replay fence.
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parents[1] / "pandrator/web/migrations"))
    config.set_main_option("sqlalchemy.url", sqlite_url(domain.database.path))
    command.downgrade(config, "0076_project_graph_consistency")
    prepare_read_only_pinned_schema(
        domain.database.path,
        (Artifact, Job, SoundItem, SoundVariant, Workspace),
    )
    with domain.database.immediate_session() as session:
        artifact = session.get(Artifact, result["artifact_id"])
        assert artifact is not None
        artifact.workspace_id = foreign_workspace_id

    with pytest.raises(ValueError, match="different workspace ownership"):
        _run(domain, payload, worker_id)
    assert len(outputs) == 1


def test_legacy_job_and_omitted_context_keep_initial_personal_workspace_behavior(
    domain, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    outputs = _install_synthetic_generator(monkeypatch)
    legacy_request = {"prompt": "legacy candidate owner", "seed": 109}
    legacy_receipt = _admit(domain, legacy_request)
    with domain.database.immediate_session() as session:
        job = session.get(Job, legacy_receipt["job_id"])
        assert job is not None
        job.payload_json = {
            key: value for key, value in job.payload_json.items() if key != "workspace_id"
        }
    payload, worker_id = _claim(domain, legacy_receipt)
    result = _run(domain, payload, worker_id)

    source = tmp_path / "upload" / "solo.wav"
    original = _write_source_wav(source)
    solo_import = domain.sounds.import_audio(source, "Solo import")

    voice_source = tmp_path / "upload" / "voice-reference.wav"
    voice_original = _write_source_wav(voice_source)
    voice_prepared = audio_imports.prepare_audio_import(
        domain.paths,
        domain.artifacts,
        voice_source,
        "Solo voice reference",
    )
    try:
        with domain.database.immediate_session() as session:
            voice_artifact = audio_imports.publish_audio_import_in_session(
                session,
                voice_prepared,
                artifacts=domain.artifacts,
                paths=domain.paths,
                role="voice.reference",
            )
            voice_artifact_id = voice_artifact.id
    finally:
        audio_imports.cleanup_prepared_audio_import(voice_prepared)

    with domain.database.session() as session:
        personal_workspace_id = initial_personal_workspace(session).id
        variant = session.get(SoundVariant, legacy_receipt["candidate_id"])
        generated_artifact = session.get(Artifact, result["artifact_id"])
        imported_variant = session.get(SoundVariant, solo_import["candidate"]["id"])
        imported_artifact = session.get(Artifact, solo_import["artifact_id"])
        voice_artifact = session.get(Artifact, voice_artifact_id)
        assert variant is not None and generated_artifact is not None
        assert imported_variant is not None and imported_artifact is not None
        assert voice_artifact is not None
        assert variant.workspace_id == personal_workspace_id
        assert generated_artifact.workspace_id == personal_workspace_id
        assert imported_variant.workspace_id == personal_workspace_id
        assert imported_artifact.workspace_id == personal_workspace_id
        assert voice_artifact.workspace_id == personal_workspace_id
        generated_path = domain.paths.managed_path(generated_artifact.relative_path)
        imported_path = domain.paths.managed_path(imported_artifact.relative_path)
        voice_path = domain.paths.managed_path(voice_artifact.relative_path)
    assert generated_path.read_bytes() == outputs[0]
    assert imported_path.read_bytes() == original
    assert voice_path.read_bytes() == voice_original
    assert source.read_bytes() == original
