"""Current native soundtrack mix authority and finite source projections."""

from __future__ import annotations

import hashlib
import math
import shutil
import threading
import wave
from array import array
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import event, func, select, text
from sqlalchemy.exc import InvalidRequestError

from pandrator.web import media_process
from pandrator.web import models as m
from pandrator.web.access.contracts import AccessDenied
from pandrator.web.access.output_mix_policy import require_output_mix_access
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.accounts.models import WorkspaceMembership
from pandrator.web.accounts.resource_grant_models import ApiTokenWorkspaceGrant
from pandrator.web.job_execution import current_job_lease
from pandrator.web.projects.models import ProjectMembership
from pandrator.web.source_resolution import resolve_media_source, resolve_primary_source
from pandrator.web.workflow_audio_preview import AudioPreviewContext, preview_output_mix
from tests.test_account_authentication_integration import case as case  # noqa: F401
from tests.test_provider_workspace_access import no_network as no_network  # noqa: F401
from tests.test_source_library_workspace_http import library as library  # noqa: F401
from tests.test_source_preparation_workspace_access import (
    FULL_SCOPES,
    _actor,
    _attach,
    _detach_current_source,
    _leased_actor,
    _project,
)

RUN_SCOPES = frozenset({"app.read", "app.run"})


def _wav(path):
    samples = array(
        "h", (int(5000 * math.sin(index * 2 * math.pi * 220 / 16000)) for index in range(16000 * 5))
    )
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(samples.tobytes())


def _audio(f, target, *, dubbing=False, takes=True):
    path = f.services.paths.uploads / f"mix-{uuid4()}.wav"
    _wav(path)
    with execution_actor_scope(HostActor("pandrator-cli")):
        artifact = f.services.artifacts.register(
            path,
            kind="audio",
            role="output_assembly" if dubbing else "upload",
            session_id=target["id"],
            metadata={"takes": [{"target_start_ms": 1500}]}
            if dubbing and takes
            else {"origin_private": "not a source input field"},
        )
    return artifact, path


def _context(f):
    return cast(AudioPreviewContext, f.services.workflow_handlers._audio_preview_context())


def _payload(target, source, dubbing, **extra):
    return {
        "session_id": target["id"],
        "source_artifact_id": source.id,
        "dubbing_artifact_id": dubbing.id,
        "duration_seconds": 4,
        "start_seconds": 0,
        **extra,
    }


def _rows(f):
    with f.case.database.snapshot_session() as unit:
        return tuple(
            unit.scalar(select(func.count()).select_from(model)) or 0
            for model in (m.Artifact, m.ArtifactEdge)
        )


def _files(f):
    return {
        path
        for root in (
            f.services.paths.sessions,
            f.services.paths.artifacts,
            f.services.paths.uploads,
        )
        for path in root.rglob("*")
        if path.is_file()
    }


def _fake_native(monkeypatch, *, probe=None, process=None, detection=None):
    probes = Mock(side_effect=probe, return_value=SimpleNamespace(duration_ms=5000))

    def render(command, **_kwargs):
        Path(command[-1]).write_bytes(b"bounded mix output")

    render_process = Mock(side_effect=process or render)
    find = Mock(side_effect=detection, return_value=0.25)
    monkeypatch.setattr(media_process, "probe_audio_stream", probes)
    monkeypatch.setattr(media_process, "run_media_process", render_process)
    monkeypatch.setattr(media_process, "find_first_audible_seconds", find)
    monkeypatch.setattr(media_process, "resolve_ffmpeg_executable", Mock(return_value="ffmpeg"))
    return probes, render_process, find


def _job(
    f, actor, target, source, dubbing, *, kind="output.mix_preview", queued_target=None, **selectors
):
    with execution_actor_scope(actor):
        queued = f.services.jobs.enqueue(
            kind,
            _payload(target, source, dubbing, **selectors),
            session_id=(queued_target or target)["id"],
        )
        claimed = f.services.jobs.claim("output-mix-worker", lease_seconds=900)
    assert claimed is not None and claimed.id == queued.id
    return claimed


@pytest.mark.parametrize("host", [False, True])
def test_trusted_policy_and_legacy_native_register_ports_and_previous_restore(
    library, monkeypatch, host
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, source_path = _audio(f, target)
    dubbing, dubbing_path = _audio(f, target, dubbing=True)
    actor = HostActor("pandrator-cli") if host else None
    unit = Mock()
    with execution_actor_scope(actor):
        assert (
            require_output_mix_access(
                unit, "ignored", "ignored", "ignored", expected=cast(Any, object())
            )
            is None
        )
    assert unit.mock_calls == []
    context = _context(f)
    resolver = Mock(
        side_effect=[
            (SimpleNamespace(id=source.id, session_id=target["id"]), source_path),
            (SimpleNamespace(id=dubbing.id, session_id=target["id"]), dubbing_path),
            (SimpleNamespace(id=source.id, session_id=target["id"]), source_path),
            (SimpleNamespace(id=dubbing.id, session_id=target["id"]), dubbing_path),
        ]
    )
    directory = Mock(wraps=context.session_dir)
    register = Mock(wraps=context.artifacts.register)
    monkeypatch.setattr(context.artifacts, "register", register)
    context = replace(context, resolve_input=resolver, session_dir=directory)
    snapshots = Mock(wraps=f.case.database.snapshot_session)
    monkeypatch.setattr(f.case.database, "snapshot_session", snapshots)
    probes, process, _find = _fake_native(monkeypatch)
    with execution_actor_scope(actor):
        first = preview_output_mix(
            context, _payload(target, source, dubbing), Mock(), threading.Event()
        )
    path = f.services.paths.managed_path(first["artifact"]["relative_path"])
    previous = path.read_bytes()
    assert path.name == "soundtrack-mix-preview.wav"
    assert resolver.call_count == 2 and directory.call_count == 1
    assert register.call_args.kwargs["replace_parent_ids"] is True
    assert set(register.call_args.kwargs) == {
        "kind",
        "role",
        "session_id",
        "parent_ids",
        "replace_parent_ids",
        "settings",
        "metadata",
    }
    assert probes.call_args.kwargs.keys() == {"cancel_event"}
    assert process.call_args.kwargs.keys() == {"cancel_event"}
    if not host:
        snapshots.assert_not_called()
    register.side_effect = RuntimeError("Synthetic legacy registration failure")

    def replacement(command, **_kwargs):
        Path(command[-1]).write_bytes(b"uncommitted replacement")

    process.side_effect = replacement
    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic legacy registration failure"),
    ):
        preview_output_mix(context, _payload(target, source, dubbing), Mock(), threading.Event())
    assert path.read_bytes() == previous and source_path.is_file()
    assert list(path.parent.glob(".soundtrack-mix-preview-*")) == []


@pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Local FFmpeg/FFprobe required",
)
@pytest.mark.parametrize("leased", [False, True])
def test_native_member_mix_with_read_run_without_write_is_target_owned(library, leased):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, source_path = _audio(f, target)
    dubbing, dubbing_path = _audio(f, target, dubbing=True)
    originals = (source_path.read_bytes(), dubbing_path.read_bytes())
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    scope = (
        _leased_actor(actor, _job(f, actor, target, source, dubbing))
        if leased
        else execution_actor_scope(actor)
    )
    with scope:
        result = preview_output_mix(
            _context(f),
            _payload(target, source, dubbing, _job_id="forged", _lease_generation=-1),
            Mock(),
            threading.Event(),
        )
    assert result["artifact"]["role"] == "mix_preview" and result["duration_seconds"] == 4
    path = f.services.paths.managed_path(result["artifact"]["relative_path"])
    assert path.name.startswith("soundtrack-mix-preview-") and path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == result["artifact"]["content_hash"]
    assert originals == (source_path.read_bytes(), dubbing_path.read_bytes())
    with f.case.database.snapshot_session() as unit:
        artifact = unit.get_one(m.Artifact, result["artifact_id"])
        assert artifact.session_id == target["id"] and artifact.workspace_id == u.workspace_id
        assert unit.get(m.ArtifactEdge, (source.id, artifact.id)) is not None
        assert unit.get(m.ArtifactEdge, (dubbing.id, artifact.id)) is not None


@pytest.mark.parametrize("denial", ["viewer", "foreign_admin", "no_run"])
def test_denied_actor_stops_before_body_path_and_native(library, monkeypatch, denial):
    f, u = library, library.u
    target = _project(u.bob, u.bh)
    source, _source_path = _audio(f, target)
    dubbing, _dubbing_path = _audio(f, target, dubbing=True)
    if denial == "viewer":
        with f.case.database.immediate_session() as unit:
            unit.add(
                ProjectMembership(
                    project_id=target["project_id"], user_id=u.alice_id, role="viewer"
                )
            )
        actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    elif denial == "foreign_admin":
        owner = f.case.auth.initial_account().user_id
        actor = _actor(f.case, owner, FULL_SCOPES | {"app.admin"}, workspaces=(f.homes[owner],))
    else:
        target = _project(u.alice, u.ah, u.workspace_id)
        source, _source_path = _audio(f, target)
        dubbing, _dubbing_path = _audio(f, target, dubbing=True)
        actor = _actor(f.case, u.alice_id, frozenset({"app.read"}), workspaces=(u.workspace_id,))
    context = _context(f)
    forbidden = Mock(side_effect=AssertionError("Denied mix used old path/native port"))
    context = replace(context, resolve_input=forbidden, session_dir=forbidden)
    monkeypatch.setattr(media_process, "probe_audio_stream", forbidden)
    with execution_actor_scope(actor), pytest.raises((AccessDenied, SessionNotFound)):
        preview_output_mix(context, _payload(target, source, dubbing), Mock(), threading.Event())
    forbidden.assert_not_called()


@pytest.mark.parametrize("binding", ["kind", "selectors", "root", "lease"])
def test_actual_job_selectors_root_kind_and_live_lease_bind_inputs(library, binding):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    other = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _audio(f, target)
    dubbing, _path = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(
        f,
        actor,
        target,
        source,
        dubbing,
        kind="audio.preview" if binding == "kind" else "output.mix_preview",
        queued_target=other if binding == "root" else None,
        **({"dubbing_artifact_id": "another-dubbing"} if binding == "selectors" else {}),
    )
    with _leased_actor(actor, job):
        token = (
            current_job_lease.set(
                {"job_id": m.new_id(), "owner_id": "forged", "lease_generation": 1}
            )
            if binding == "lease"
            else None
        )
        try:
            with f.case.database.snapshot_session() as unit, pytest.raises(AccessDenied):
                require_output_mix_access(unit, target["id"], source.id, dubbing.id)
        finally:
            if token is not None:
                current_job_lease.reset(token)


def test_same_selected_source_cannot_bypass_dubbing_ordinary_read_and_target(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, _path = _audio(f, origin)
    _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    with execution_actor_scope(actor), f.case.database.snapshot_session() as unit:
        with pytest.raises(KeyError, match="Mix preview input not found"):
            require_output_mix_access(unit, target["id"], source.id, source.id)


def test_selected_cross_project_bytes_do_not_load_origin_metadata(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, path = _audio(f, origin)
    dubbing, _path = _audio(f, target, dubbing=True)
    _attach(f, target["id"], source)
    original = path.read_bytes()
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))

    def deny_source_body(artifact, _context):
        if artifact.id == source.id:
            raise AssertionError("Mix hydrated its selected source ORM body")

    event.listen(m.Artifact, "load", deny_source_body)
    _fake_native(monkeypatch)
    try:
        with execution_actor_scope(actor):
            result = preview_output_mix(
                _context(f), _payload(target, source, dubbing), Mock(), threading.Event()
            )
    finally:
        event.remove(m.Artifact, "load", deny_source_body)
    assert result["artifact"]["session_id"] == target["id"] and path.read_bytes() == original


def test_finite_resolver_avoids_invalid_origin_json_and_raiseloads_hidden_fields(library):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, path = _audio(f, origin)
    asset = _attach(f, target["id"], source)
    with f.case.database.immediate_session() as unit:
        unit.execute(
            text("UPDATE artifacts SET metadata_json = :bad WHERE id = :id"),
            {"bad": "not JSON", "id": source.id},
        )
        unit.execute(
            text("UPDATE source_assets SET metadata_json = :bad WHERE id = :id"),
            {"bad": "not JSON", "id": asset.id},
        )
    with f.case.database.snapshot_session() as unit:
        result = resolve_primary_source(unit, target["id"], include_metadata=False)
        assert result.artifact is not None and result.artifact.id == source.id
        assert result.source_asset is not None and result.source_asset.id == asset.id
        assert result.has_audio and result.name == path.name
        for obj, name in (
            (result.artifact, "metadata_json"),
            (result.source_asset, "metadata_json"),
            (result.source_asset, "external_path"),
        ):
            with pytest.raises(InvalidRequestError):
                getattr(obj, name)
    with f.case.database.snapshot_session() as unit:
        result = resolve_media_source(unit, target["id"], include_metadata=False)
        assert (
            result.artifact is not None
            and result.artifact.id == source.id
            and result.resolution == "attached"
        )


@pytest.mark.parametrize("change", ["detach", "header"])
def test_current_detach_or_header_change_at_progress_denies_before_probe(
    library, monkeypatch, change
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = _project(u.bob, u.bh, u.workspace_id)
    source, _path = _audio(f, origin)
    dubbing, _path = _audio(f, target, dubbing=True)
    asset = _attach(f, target["id"], source)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    probes, process, _find = _fake_native(monkeypatch)
    before = _rows(f)

    def changed(_value, _detail):
        if change == "detach":
            _detach_current_source(f, target["id"], asset.id)
        else:
            with f.case.database.immediate_session() as unit:
                unit.get_one(m.Artifact, source.id).content_hash = "f" * 64

    with execution_actor_scope(actor), pytest.raises(KeyError, match="Mix preview input not found"):
        preview_output_mix(
            _context(f), _payload(target, source, dubbing), changed, threading.Event()
        )
    probes.assert_not_called()
    process.assert_not_called()
    assert _rows(f) == before


@pytest.mark.parametrize("boundary", ["progress", "probe", "process", "prepare", "writer"])
def test_live_role_credential_and_lease_loss_stops_next_effect_or_publication(
    library, monkeypatch, boundary
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _audio(f, target)
    dubbing, _path = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(f, actor, target, source, dubbing)
    context = _context(f)
    before, files = _rows(f), _files(f)
    changed = False

    def revoke():
        nonlocal changed
        if changed:
            return
        changed = True
        with f.case.database.immediate_session() as unit:
            if boundary == "prepare":
                unit.delete(unit.get_one(ApiTokenWorkspaceGrant, (actor.token_id, u.workspace_id)))
            else:
                unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))

    def probe(*_args, **_kwargs):
        if boundary == "probe":
            revoke()
        return SimpleNamespace(duration_ms=5000)

    def process(command, **_kwargs):
        Path(command[-1]).write_bytes(b"private temporary mix")
        if boundary == "process":
            revoke()

    probes, processes, _find = _fake_native(monkeypatch, probe=probe, process=process)

    def progress(value, _detail):
        if boundary == "progress" and value == 0.08:
            revoke()

    if boundary == "prepare":
        prepare = context.artifacts.prepare_registration

        def prepare_then_revoke(path, **kwargs):
            result = prepare(path, **kwargs)
            revoke()
            return result

        monkeypatch.setattr(context.artifacts, "prepare_registration", prepare_then_revoke)
    if boundary == "writer":
        writer = f.case.database.immediate_session

        @contextmanager
        def expire_then_write():
            nonlocal changed
            with writer() as unit:
                unit.get_one(m.Job, job.id).lease_expires_at = m.utcnow() - timedelta(seconds=10)
            changed = True
            with writer() as unit:
                yield unit

        monkeypatch.setattr(f.case.database, "immediate_session", expire_then_write)
    with _leased_actor(actor, job), pytest.raises((AccessDenied, SessionNotFound)):
        preview_output_mix(context, _payload(target, source, dubbing), progress, threading.Event())
    assert changed and _rows(f) == before and _files(f) == files
    assert probes.call_count == (0 if boundary == "progress" else 1 if boundary == "probe" else 2)
    assert processes.call_count == (0 if boundary in {"progress", "probe"} else 1)


def test_repeated_outputs_and_missing_registered_paths_never_get_reused(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _audio(f, target)
    dubbing, _path = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    _fake_native(monkeypatch)
    context = _context(f)
    with execution_actor_scope(actor):
        first = preview_output_mix(
            context, _payload(target, source, dubbing), Mock(), threading.Event()
        )
        first_path = f.services.paths.managed_path(first["artifact"]["relative_path"])
        original = first_path.read_bytes()
        second = preview_output_mix(
            context, _payload(target, source, dubbing), Mock(), threading.Event()
        )
    assert first["artifact_id"] != second["artifact_id"] and first_path.read_bytes() == original
    first_path.unlink()
    identifier = first_path.stem.removeprefix("soundtrack-mix-preview-")
    context = replace(context, new_id=Mock(side_effect=[identifier, str(uuid4())]))
    before = _rows(f)
    with (
        execution_actor_scope(actor),
        pytest.raises(FileExistsError, match="Mix preview output already exists"),
    ):
        preview_output_mix(context, _payload(target, source, dubbing), Mock(), threading.Event())
    assert _rows(f) == before and not first_path.exists()
    assert (
        f.services.paths.managed_path(second["artifact"]["relative_path"]).read_bytes() == original
    )


@pytest.mark.parametrize("fault", ["registration", "cancel"])
def test_failed_or_cancelled_current_mix_cleans_only_owned_unregistered_files(
    library, monkeypatch, fault
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _audio(f, target)
    dubbing, _path = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    context = _context(f)
    cancel = threading.Event()

    def render(command, **_kwargs):
        Path(command[-1]).write_bytes(b"unpublished temporary")
        if fault == "cancel":
            cancel.set()

    _fake_native(monkeypatch, process=render)
    if fault == "registration":
        register = context.artifacts.register_in_session

        def register_then_fail(*args, **kwargs):
            register(*args, **kwargs)
            raise RuntimeError("Synthetic mix registration failure")

        monkeypatch.setattr(context.artifacts, "register_in_session", register_then_fail)
    before, files = _rows(f), _files(f)
    with execution_actor_scope(actor):
        if fault == "cancel":
            assert (
                preview_output_mix(context, _payload(target, source, dubbing), Mock(), cancel) == {}
            )
        else:
            with pytest.raises(RuntimeError, match="Synthetic mix registration failure"):
                preview_output_mix(context, _payload(target, source, dubbing), Mock(), cancel)
    assert _rows(f) == before and _files(f) == files


def test_after_commit_context_failure_preserves_registered_output_after_role_loss(
    library, monkeypatch
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _audio(f, target)
    dubbing, _path = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    context = _context(f)
    _fake_native(monkeypatch)
    writer = f.case.database.immediate_session

    @contextmanager
    def commit_then_raise():
        with writer() as unit:
            yield unit
        with writer() as unit:
            unit.delete(unit.get_one(WorkspaceMembership, (u.workspace_id, u.alice_id)))
        raise RuntimeError("Synthetic mix failure after commit")

    monkeypatch.setattr(f.case.database, "immediate_session", commit_then_raise)
    before = _rows(f)
    with (
        execution_actor_scope(actor),
        pytest.raises(RuntimeError, match="Synthetic mix failure after commit"),
    ):
        preview_output_mix(context, _payload(target, source, dubbing), Mock(), threading.Event())
    assert _rows(f) == (before[0] + 1, before[1] + 2)
    with f.case.database.snapshot_session() as unit:
        output = unit.scalar(
            select(m.Artifact).where(
                m.Artifact.session_id == target["id"], m.Artifact.role == "mix_preview"
            )
        )
        assert output is not None and f.services.paths.managed_path(output.relative_path).is_file()


@pytest.mark.parametrize("timeline", [False, True])
def test_native_boundaries_are_closed_and_dubbing_metadata_is_loaded_once(
    library, monkeypatch, timeline
):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _path = _audio(f, target)
    dubbing, _path = _audio(f, target, dubbing=True, takes=timeline)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    context = _context(f)
    snapshots = f.case.database.snapshot_session
    active = 0
    statements = []

    @contextmanager
    def tracked_snapshot():
        nonlocal active
        with snapshots() as unit:
            active += 1
            try:
                yield unit
            finally:
                active -= 1

    def sql(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.lower())

    def probe(*_args, **_kwargs):
        assert active == 0
        return SimpleNamespace(duration_ms=5000)

    def detection(*_args, **_kwargs):
        assert active == 0
        return 0.25

    def render(command, **_kwargs):
        assert active == 0
        Path(command[-1]).write_bytes(b"bounded current preview")

    probes, processes, find = _fake_native(
        monkeypatch, probe=probe, process=render, detection=detection
    )
    monkeypatch.setattr(f.case.database, "snapshot_session", tracked_snapshot)
    event.listen(f.case.database.engine, "before_cursor_execute", sql)
    try:
        with execution_actor_scope(actor):
            result = preview_output_mix(
                context,
                _payload(target, source, dubbing, start_seconds=None),
                Mock(),
                threading.Event(),
            )
    finally:
        event.remove(f.case.database.engine, "before_cursor_execute", sql)
    assert (
        probes.call_count == 2
        and processes.call_count == 1
        and find.call_count == (0 if timeline else 1)
    )
    assert result["automatic_start_method"] == (
        "assembly_timeline" if timeline else "audio_detection"
    )
    assert (
        sum(statement.startswith("select artifacts.metadata_json") for statement in statements) == 1
    )


def _queue_pins(source, dubbing, *, edit_revision_id=None, edit_content_hash=None):
    return {
        "source_content_hash": source.content_hash,
        "dubbing_content_hash": dubbing.content_hash,
        "source_edit_revision_id": edit_revision_id,
        "source_edit_content_hash": edit_content_hash,
    }


def _edit_plan(f, target, source):
    with f.case.database.immediate_session() as unit:
        plan = m.MediaEditPlan(session_id=target["id"])
        unit.add(plan)
        unit.flush()
        revision = m.MediaEditPlanRevision(
            plan_id=plan.id, revision_number=1,
            source_media_artifact_id=source.id, editorial_transcript_artifact_id=source.id,
            duration_ms=5000, content_hash="reviewed-edit",
        )
        unit.add(revision)
        unit.flush()
        plan.active_revision_id = revision.id
        row = unit.get_one(m.Artifact, source.id)
        row.role = "media_edit_media"
        row.metadata_json = {"media_edit_revision_id": revision.id, "content_hash": revision.content_hash}
        return plan.id, revision.id, revision.content_hash


@pytest.mark.parametrize("changed_input", ["source", "dubbing"])
def test_new_queued_input_hash_change_rejects_before_native(library, monkeypatch, changed_input):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _ = _audio(f, target)
    dubbing, _ = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(f, actor, target, source, dubbing, **_queue_pins(source, dubbing))
    changed = source if changed_input == "source" else dubbing
    with f.case.database.immediate_session() as unit:
        unit.get_one(m.Artifact, changed.id).content_hash = "changed-after-queue"
    probes, process, detection = _fake_native(monkeypatch)
    before, files = _rows(f), _files(f)
    with _leased_actor(actor, job), pytest.raises(KeyError, match="Mix preview input not found"):
        preview_output_mix(
            _context(f),
            _payload(target, source, dubbing, **{f"{changed_input}_content_hash": "changed-after-queue"}),
            Mock(), threading.Event(),
        )
    probes.assert_not_called()
    process.assert_not_called()
    detection.assert_not_called()
    assert _rows(f) == before and _files(f) == files


@pytest.mark.parametrize("boundary", ["waiting", "progress", "process"])
def test_new_queued_edit_revision_change_rejects_next_boundary(library, monkeypatch, boundary):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _ = _audio(f, target)
    dubbing, _ = _audio(f, target, dubbing=True)
    plan_id, revision_id, edit_hash = _edit_plan(f, target, source)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(
        f, actor, target, source, dubbing,
        **_queue_pins(source, dubbing, edit_revision_id=revision_id, edit_content_hash=edit_hash),
    )
    changed = False

    def change():
        nonlocal changed
        if changed:
            return
        with f.case.database.immediate_session() as unit:
            revision = m.MediaEditPlanRevision(
                plan_id=plan_id, revision_number=2,
                source_media_artifact_id=source.id, editorial_transcript_artifact_id=source.id,
                duration_ms=5000, content_hash="new-edit",
            )
            unit.add(revision)
            unit.flush()
            unit.get_one(m.MediaEditPlan, plan_id).active_revision_id = revision.id
        changed = True

    def render(command, **_kwargs):
        Path(command[-1]).write_bytes(b"private temporary preview")
        if boundary == "process":
            change()

    def progress(_value, _detail):
        if boundary == "progress":
            change()

    probes, process, _ = _fake_native(monkeypatch, process=render)
    if boundary == "waiting":
        change()
    before, files = _rows(f), _files(f)
    with _leased_actor(actor, job), pytest.raises(KeyError, match="Mix preview input not found"):
        preview_output_mix(_context(f), _payload(target, source, dubbing), progress, threading.Event())
    assert changed and _rows(f) == before and _files(f) == files
    assert probes.call_count == (2 if boundary == "process" else 0)
    assert process.call_count == (1 if boundary == "process" else 0)


@pytest.mark.parametrize("change", ["metadata", "state", "plan_hash", "missing_active"])
def test_new_queued_edit_pins_require_matching_current_render(library, monkeypatch, change):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _ = _audio(f, target)
    dubbing, _ = _audio(f, target, dubbing=True)
    plan_id, revision_id, edit_hash = _edit_plan(f, target, source)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(
        f, actor, target, source, dubbing,
        **_queue_pins(source, dubbing, edit_revision_id=revision_id, edit_content_hash=edit_hash),
    )
    with f.case.database.immediate_session() as unit:
        if change == "metadata":
            unit.get_one(m.Artifact, source.id).metadata_json = {
                "media_edit_revision_id": "obsolete", "content_hash": edit_hash,
            }
        elif change == "state":
            unit.get_one(m.Artifact, source.id).state = "stale"
        elif change == "plan_hash":
            unit.get_one(m.MediaEditPlanRevision, revision_id).content_hash = "changed-edit-hash"
        else:
            unit.get_one(m.MediaEditPlan, plan_id).active_revision_id = None
    probes, process, _ = _fake_native(monkeypatch)
    with _leased_actor(actor, job), pytest.raises(KeyError, match="Mix preview input not found"):
        preview_output_mix(_context(f), _payload(target, source, dubbing), Mock(), threading.Event())
    probes.assert_not_called()
    process.assert_not_called()


def test_new_queued_null_edit_pin_rejects_introduced_edit_plan(library, monkeypatch):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    source, _ = _audio(f, target)
    dubbing, _ = _audio(f, target, dubbing=True)
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, workspaces=(u.workspace_id,))
    job = _job(f, actor, target, source, dubbing, **_queue_pins(source, dubbing))
    _edit_plan(f, target, source)
    probes, process, _ = _fake_native(monkeypatch)
    with _leased_actor(actor, job), pytest.raises(KeyError, match="Mix preview input not found"):
        preview_output_mix(_context(f), _payload(target, source, dubbing), Mock(), threading.Event())
    probes.assert_not_called()
    process.assert_not_called()


@pytest.mark.parametrize("edited", [False, True])
def test_new_queued_pins_preserve_finite_source_projection(library, monkeypatch, edited):
    f, u = library, library.u
    target = _project(u.alice, u.ah, u.workspace_id)
    origin = target if edited else _project(u.bob, u.bh, u.workspace_id)
    source, _ = _audio(f, origin)
    dubbing, _ = _audio(f, target, dubbing=True)
    pins = _queue_pins(source, dubbing)
    if edited:
        _plan, revision_id, edit_hash = _edit_plan(f, target, source)
        pins = _queue_pins(source, dubbing, edit_revision_id=revision_id, edit_content_hash=edit_hash)
    else:
        _attach(f, target["id"], source)
        with f.case.database.immediate_session() as unit:
            unit.execute(
                text("UPDATE artifacts SET metadata_json = :bad WHERE id = :id"),
                {"bad": "not JSON", "id": source.id},
            )
    actor = _actor(f.case, u.alice_id, RUN_SCOPES, projects=(target["project_id"],))
    job = _job(f, actor, target, source, dubbing, **pins)
    probes, process, _ = _fake_native(monkeypatch)

    def forbid_source_body(artifact, _context):
        if artifact.id == source.id:
            raise AssertionError("Pinned mix hydrated the source Artifact body")

    event.listen(m.Artifact, "load", forbid_source_body)
    try:
        with _leased_actor(actor, job):
            result = preview_output_mix(
                _context(f), _payload(target, source, dubbing), Mock(), threading.Event(),
            )
    finally:
        event.remove(m.Artifact, "load", forbid_source_body)
    assert result["artifact"]["role"] == "mix_preview"
    assert probes.call_count == 2 and process.call_count == 1
