"""Scoped sound preparation/render denial protocol and conservative rollback."""

from __future__ import annotations

import os
import socket
import threading
import wave
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import requests.sessions
from sqlalchemy import event, func, select

from pandrator.web import sound_mix_manifest as manifest
from pandrator.web import sound_mix_publication as publication
from pandrator.web import sound_mix_render as renderer
from pandrator.web.artifacts import sha256_file
from pandrator.web.job_execution import current_job_lease
from pandrator.web.models import Artifact
from pandrator.web.sound_mix_admission import find_verified_mix_cache
from pandrator.web.sound_mix_claims import _cleanup_uncommitted, _validate_claim
from pandrator.web.sound_mix_inputs import _read_take_wav
from pandrator.web.sound_mix_plan import build_sound_mix_plan
from tests.test_sound_mix_service import SoundMixDomain, _render, _write_pcm
from tests.test_sound_mix_service import domain as domain


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def blocked(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("Sound callback acceptance must stay offline")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(requests.sessions.Session, "request", blocked)


def _cue(domain: SoundMixDomain) -> dict[str, Any]:
    project = domain.audiobook
    domain.add_cue(
        project,
        {"kind": "before_segment", "segment_id": project["segment_ids"][1]},
        {"mode": "insert"},
    )
    return project


def _prepared(domain: SoundMixDomain):
    project = _cue(domain)
    return domain.service.prepare(project["session_id"], project["base"].id)


def test_guard_runs_before_protected_snapshot_or_input_queries(domain: SoundMixDomain):
    project = _cue(domain)
    statements: list[str] = []
    denied = OSError("callback denied")
    events: list[str] = []

    def guard(session):
        assert session.autoflush is False
        events.append("guard")
        raise denied

    event.listen(
        domain.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with pytest.raises(OSError) as error:
        domain.service.prepare(project["session_id"], project["base"].id, authorization_guard=guard)
    assert error.value is denied and events == ["guard"]
    assert not any(statement.lstrip().upper().startswith("SELECT") for statement in statements)


@pytest.mark.parametrize("blocked_input", ["base", "variant", "take"])
def test_denied_input_header_prevents_protected_artifact_or_take_body(
    domain: SoundMixDomain, blocked_input: str
):
    project = _cue(domain)
    denied_id = {
        "base": project["base"].id,
        "variant": domain.effect_artifact_id,
        "take": project["take_artifact_ids"][1],
    }[blocked_input]
    statements: list[str] = []
    events: list[tuple[str, str]] = []

    def guard(session):
        assert not session.autoflush
        events.append(("guard", ""))

    def authorize(session, identifier):
        assert not session.autoflush and events[0][0] == "guard"
        events.append(("input", identifier))
        if identifier == denied_id:
            raise PermissionError("resource ceiling")

    event.listen(
        domain.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with pytest.raises(PermissionError, match="resource ceiling"):
        domain.service.prepare(
            project["session_id"],
            project["base"].id,
            authorization_guard=guard,
            authorize_input=authorize,
        )
    assert ("input", denied_id) in events
    if blocked_input in {"base", "variant"}:
        assert not any("artifacts.relative_path" in statement for statement in statements)
    else:
        assert not any(
            "SELECT audio_takes.id, audio_takes.generation_segment_id" in statement
            for statement in statements
        )
        assert any(
            "SELECT audio_takes.id, audio_takes.artifact_id" in statement
            for statement in statements
        )


def test_snapshot_authorizes_distinct_base_timeline_variant_and_take_ids_once(
    domain: SoundMixDomain,
):
    project = _cue(domain)
    units: list[Any] = []
    authorized: dict[Any, list[str]] = {}

    def guard(session):
        assert session.autoflush is False
        units.append(session)
        authorized[session] = []

    def authorize(session, identifier):
        assert session in authorized and not session.autoflush
        authorized[session].append(identifier)

    prepared = domain.service.prepare(
        project["session_id"],
        project["base"].id,
        timeline_artifact_id=project["base"].id,
        authorization_guard=guard,
        authorize_input=authorize,
    )
    assert prepared.has_cues
    first = authorized[units[0]]
    assert set(first) == {
        project["base"].id,
        domain.effect_artifact_id,
        project["take_artifact_ids"][1],
    }
    assert all(count == 1 for count in Counter(first).values())


@pytest.mark.parametrize("exception", [ValueError("revoked IO"), OSError("revoked IO")])
def test_probe_and_take_wave_callbacks_are_outside_media_fallbacks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception: Exception
):
    path = tmp_path / "input.wav"
    _write_pcm(path, 10)

    def denied():
        raise exception

    def forbidden(*_args, **_kwargs):
        raise AssertionError("Denied callback reached media process")

    monkeypatch.setattr(manifest, "run_media_process", forbidden)
    for operation in (
        lambda: manifest._base_geometry({"metadata_json": {}}, "voiceover", path, before_io=denied),
        lambda: manifest._probe_audio_geometry(path, before_io=denied),
        lambda: _read_take_wav(path, before_io=denied),
    ):
        with pytest.raises(type(exception)) as error:
            operation()
        assert error.value is exception


@pytest.mark.parametrize(
    "exception", [ValueError("late normalize denial"), OSError("late normalize denial")]
)
def test_normalization_stops_before_the_next_open_after_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, exception: Exception
):
    source, destination = tmp_path / "source.wav", tmp_path / "normalized.wav"
    _write_pcm(source, 100)
    real_wave_open = wave.open
    opened: list[Any] = []
    revoked = False

    def open_wave(path, mode=None):
        nonlocal revoked
        result = real_wave_open(path, mode)
        if mode == "rb":
            opened.append(path)
            revoked = True
        return result

    def before_io():
        if revoked:
            raise exception

    monkeypatch.setattr(wave, "open", open_wave)
    with pytest.raises(type(exception)) as error:
        renderer.normalize_mix_audio(
            source,
            destination,
            rate=8000,
            channels=1,
            cancel_event=threading.Event(),
            before_io=before_io,
        )
    assert error.value is exception and len(opened) == 1 and not destination.exists()


def test_renderer_checks_new_clip_reader_after_late_progress_revocation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    base, clip, output = tmp_path / "base.wav", tmp_path / "clip.wav", tmp_path / "output.wav"
    frames = renderer.MAX_RENDER_BLOCK_FRAMES * 2
    _write_pcm(base, frames)
    _write_pcm(clip, 10)
    plan = build_sound_mix_plan(
        base_frames=frames,
        rate=8000,
        channels=1,
        base_content_hash="synthetic",
        clips=[
            {
                "cue_id": "late",
                "variant_id": "variant",
                "content_hash": "synthetic",
                "anchor_frame": renderer.MAX_RENDER_BLOCK_FRAMES + 1,
                "mode": "overlay",
                "offset_frames": 0,
                "source_frames": 10,
            }
        ],
        fixed_timeline=True,
    )
    opened: list[str] = []
    real_open = wave.open
    revoked = False
    callbacks = 0
    denied = OSError("late reader denied")

    def open_wave(path, mode=None):
        if mode == "rb":
            opened.append(str(path))
        return real_open(path, mode)

    def before_io():
        nonlocal callbacks
        callbacks += 1
        if revoked:
            raise denied

    def progress(_fraction, _detail):
        nonlocal revoked
        revoked = True

    monkeypatch.setattr(wave, "open", open_wave)
    with pytest.raises(OSError) as error:
        renderer.render_sound_mix(
            base,
            {"late": clip},
            plan,
            output,
            cancel_event=threading.Event(),
            progress=progress,
            before_io=before_io,
        )
    assert error.value is denied
    assert opened.count(str(clip)) == 1 and opened.count(str(base)) == 2
    assert callbacks == 6 and not output.exists()


@pytest.mark.parametrize("exception", [ValueError("cache IO denial"), OSError("cache IO denial")])
def test_cache_hash_denial_is_not_a_cache_miss(domain: SoundMixDomain, exception: Exception):
    project = _cue(domain)
    _render(domain, project)
    prepared = domain.service.prepare(project["session_id"], project["base"].id)

    calls = 0

    def denied():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise exception

    with pytest.raises(type(exception)) as error:
        find_verified_mix_cache(
            domain.database,
            domain.paths,
            prepared.session_id,
            prepared.request_fingerprint,
            before_io=denied,
            authorization_guard=lambda _s: None,
        )
    assert error.value is exception and calls == 2


def test_cache_input_is_authorized_before_its_path_or_body(domain: SoundMixDomain):
    project = _cue(domain)
    cached = _render(domain, project)
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    statements: list[str] = []
    seen: list[str] = []

    def authorize(session, identifier):
        assert not session.autoflush
        seen.append(identifier)
        raise PermissionError("cache input denied")

    event.listen(
        domain.database.engine,
        "before_cursor_execute",
        lambda _c, _cu, statement, _p, _co, _e: statements.append(statement),
    )
    with pytest.raises(PermissionError, match="cache input denied"):
        find_verified_mix_cache(
            domain.database,
            domain.paths,
            prepared.session_id,
            prepared.request_fingerprint,
            authorization_guard=lambda _s: None,
            authorize_input=authorize,
        )
    assert seen == [cached.id]
    assert not any("artifacts.relative_path" in statement for statement in statements)


def test_inactive_return_still_checks_its_writer_without_content_io(domain: SoundMixDomain):
    project = domain.no_cues
    calls = 0
    denied = OSError("inactive return revoked")

    def guard(session):
        nonlocal calls
        assert not session.autoflush
        calls += 1
        if calls == 2:
            raise denied

    def no_io():
        raise AssertionError("Inactive mix must not inspect content")

    with pytest.raises(OSError) as error:
        _render(domain, project, authorization_guard=guard, before_io=no_io)
    assert error.value is denied and calls == 2


def test_cached_final_return_checks_its_actual_writer(domain: SoundMixDomain):
    project = _cue(domain)
    cached = _render(domain, project)
    prepared = domain.service.prepare(project["session_id"], project["base"].id)
    assert prepared.cache_artifact_id == cached.id
    units: list[Any] = []
    denied = PermissionError("cached return revoked")

    def guard(session):
        assert not session.autoflush
        units.append(session)
        if len(units) == 2:
            raise denied

    with pytest.raises(PermissionError) as error:
        publication.render_prepared(
            domain.database,
            domain.paths,
            domain.artifacts,
            prepared,
            progress=lambda *_a: None,
            cancel_event=threading.Event(),
            authorization_guard=guard,
        )
    assert error.value is denied and len(units) == 2
    assert domain.paths.managed_path(cached.relative_path).is_file()


@pytest.mark.parametrize("deny_at", [1, 2])
def test_final_writer_denial_before_or_after_registration_rolls_back_owned_output(
    domain: SoundMixDomain, deny_at: int
):
    prepared = _prepared(domain)
    calls = 0
    denied = PermissionError("publication revoked")

    def guard(session):
        nonlocal calls
        assert not session.autoflush
        calls += 1
        if calls == deny_at:
            raise denied

    with pytest.raises(PermissionError) as error:
        publication.render_prepared(
            domain.database,
            domain.paths,
            domain.artifacts,
            prepared,
            progress=lambda *_a: None,
            cancel_event=threading.Event(),
            authorization_guard=guard,
        )
    assert error.value is denied and calls == deny_at
    with domain.database.snapshot_session() as session:
        assert (
            session.scalar(
                select(func.count()).select_from(Artifact).where(Artifact.role == "sound_mix.audio")
            )
            == 0
        )
    assert (
        list((domain.paths.sessions / prepared.storage_key / "sounds" / "mixes").glob("*.wav"))
        == []
    )


def test_guarded_continuation_lease_succeeds_only_with_passing_guard(domain: SoundMixDomain):
    prepared = _prepared(domain)
    queued = domain.jobs.enqueue(
        "workflow.continue", {"session_id": prepared.session_id}, session_id=prepared.session_id
    )
    job = domain.jobs.claim("callback-worker", lease_seconds=300)
    assert job is not None and job.id == queued.id
    assert job.lease_owner is not None
    token = current_job_lease.set(
        {"job_id": job.id, "owner_id": job.lease_owner, "lease_generation": job.lease_generation}
    )
    calls: list[Any] = []

    def guard(session):
        assert not session.autoflush
        calls.append(session)

    try:
        with domain.database.snapshot_session() as session:
            with pytest.raises(ValueError, match="no longer owns"):
                _validate_claim(session, prepared, threading.Event())
        artifact, receipt = publication.render_prepared(
            domain.database,
            domain.paths,
            domain.artifacts,
            prepared,
            progress=lambda *_a: None,
            cancel_event=threading.Event(),
            authorization_guard=guard,
        )
        assert artifact.id == receipt["artifact_id"] and len(calls) == 2
        with domain.database.snapshot_session() as session:
            with pytest.raises(ValueError, match="no longer owns"):
                _validate_claim(
                    session,
                    prepared,
                    threading.Event(),
                    require_sound_mix=True,
                    authorization_guard=guard,
                )
    finally:
        current_job_lease.reset(token)


@pytest.mark.parametrize("state", ["owned", "replacement", "registered", "uncertain"])
def test_cleanup_requires_owned_inode_and_confirmed_durable_absence(
    domain: SoundMixDomain, monkeypatch: pytest.MonkeyPatch, state: str
):
    output = domain.paths.artifacts / "rollback.wav"
    _write_pcm(output, 10)
    identity = (output.stat().st_dev, output.stat().st_ino)
    digest = sha256_file(output)
    relative = domain.paths.relative_managed_path(output)
    if state == "replacement":
        other = output.with_name("replacement.wav")
        _write_pcm(other, 10)
        os.replace(other, output)
        assert output.stat().st_ino != identity[1]
    elif state == "registered":
        artifact = domain.artifacts.register(output, kind="audio")
        with domain.database.immediate_session() as session:
            session.get_one(Artifact, artifact.id).content_hash = "different-recorded-hash"
    elif state == "uncertain":

        def uncertain() -> Any:
            raise OSError("uncertain database outcome")

        monkeypatch.setattr(domain.database, "snapshot_session", uncertain)
    _cleanup_uncommitted(domain.database, output, relative, digest, owned_identity=identity)
    assert output.exists() is (state != "owned")


def test_promoted_inode_mismatch_preserves_replacement(
    domain: SoundMixDomain, monkeypatch: pytest.MonkeyPatch
):
    prepared = _prepared(domain)
    promoted: list[Path] = []
    real_replace = os.replace

    def replace(source, destination):
        source, destination = Path(source), Path(destination)
        replacement = source.with_name("replacement.wav")
        _write_pcm(replacement, 25)
        real_replace(replacement, source)
        real_replace(source, destination)
        promoted.append(destination)

    monkeypatch.setattr(publication, "os", SimpleNamespace(path=os.path, replace=replace))
    with pytest.raises(ValueError, match="rendered inode"):
        publication.render_prepared(
            domain.database,
            domain.paths,
            domain.artifacts,
            prepared,
            progress=lambda *_a: None,
            cancel_event=threading.Event(),
            authorization_guard=lambda _s: None,
        )
    assert len(promoted) == 1 and promoted[0].is_file()
