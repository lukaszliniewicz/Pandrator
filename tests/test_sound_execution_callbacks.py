"""Focused compatibility and denial-order tests for sound execution fences."""

from __future__ import annotations

import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, call

import pytest

from pandrator.logic.dubbing import crispasr
from pandrator.logic.model_acquisition import ModelAcquisitionDenied
from pandrator.web import sound_resolution_audio as audio
from pandrator.web import sound_resolution_cache as cache
from pandrator.web import sound_resolution_worker as worker
from pandrator.web.access import sound_execution_policy as policy
from pandrator.web.access.artifact_policy import ArtifactNotFound
from pandrator.web.access.session_policy import SessionNotFound
from pandrator.web.accounts.execution_actor import HostActor, execution_actor_scope
from pandrator.web.settings_policy import RevisionConflict


def _deny() -> None:
    raise PermissionError("revoked")


def _item() -> Any:
    return SimpleNamespace(
        path=Path("selected.wav"),
        transcript="word",
        identity={"engine": "crispasr"},
        language=None,
        reason=None,
        take_id="take",
        artifact_id="artifact",
        cue_id="cue",
        take_hash="hash",
        transcript_hash="transcript-hash",
        anchor={"kind": "next_pause"},
        playback={},
    )


def test_none_factory_does_not_touch_database_or_callbacks() -> None:
    database: Any = Mock()
    callbacks = (Mock(), Mock(), Mock())
    with execution_actor_scope(None):
        result = policy.sound_execution_callbacks(
            database,
            "sid",
            before_io=callbacks[0],
            authorization_guard=callbacks[1],
            authorize_input=callbacks[2],
        )
        assert policy.sound_execution_callbacks(database, "sid") == (None, None, None)
        policy.require_sound_processing_session(database, "sid")
        policy.require_sound_input_access(database, "sid", "artifact")
    assert all(actual is expected for actual, expected in zip(result, callbacks, strict=True))
    assert database.mock_calls == []
    assert all(callback.mock_calls == [] for callback in callbacks)


def test_scoped_defaults_use_fresh_units_and_exact_input_binding(monkeypatch) -> None:
    units: list[Any] = []

    @contextmanager
    def snapshot():
        unit = object()
        units.append(unit)
        yield unit

    database: Any = SimpleNamespace(snapshot_session=snapshot)
    guard, inputs = Mock(), Mock()
    monkeypatch.setattr(policy, "require_sound_processing_session", guard)
    monkeypatch.setattr(policy, "require_sound_input_access", inputs)
    with execution_actor_scope(HostActor("pandrator-cli")):
        before_io, authorization_guard, authorize_input = policy.sound_execution_callbacks(
            database, "sid", session_artifact_ids=("base", "timeline")
        )
        assert (
            before_io is not None
            and authorization_guard is not None
            and authorize_input is not None
        )
        before_io()
        before_io()
        unit: Any = object()
        authorization_guard(unit)
        authorize_input(unit, "base")
        authorize_input(unit, "timeline")
        authorize_input(unit, "variant")
    assert len(units) == 2 and units[0] is not units[1]
    assert guard.call_args_list == [call(units[0], "sid"), call(units[1], "sid"), call(unit, "sid")]
    assert inputs.call_args_list == [
        ((unit, "sid", "base"), {"session_bound": True}),
        ((unit, "sid", "timeline"), {"session_bound": True}),
        ((unit, "sid", "variant"), {"session_bound": False}),
    ]


def test_supplied_callbacks_are_not_wrapped_or_invoked() -> None:
    database: Any = Mock()
    callbacks = (Mock(), Mock(), Mock())
    with execution_actor_scope(HostActor("pandrator-cli")):
        result = policy.sound_execution_callbacks(
            database,
            "sid",
            before_io=callbacks[0],
            authorization_guard=callbacks[1],
            authorize_input=callbacks[2],
        )
    assert all(actual is expected for actual, expected in zip(result, callbacks, strict=True))
    assert database.mock_calls == []
    assert all(callback.mock_calls == [] for callback in callbacks)


def test_default_io_uses_supplied_guard_once() -> None:
    unit: Any = object()
    database: Any = SimpleNamespace(snapshot_session=lambda: nullcontext(unit))
    guard = Mock()
    with execution_actor_scope(HostActor("pandrator-cli")):
        before_io, selected, _ = policy.sound_execution_callbacks(
            database, "sid", authorization_guard=guard
        )
        assert before_io is not None
        assert selected is guard
        before_io()
    guard.assert_called_once_with(unit)


@pytest.mark.parametrize("operation", ["alignment", "normalization", "take_hash"])
def test_denial_precedes_audio_or_file_work(monkeypatch, tmp_path, operation) -> None:
    native = Mock(side_effect=AssertionError("native work must not start"))
    monkeypatch.setattr(audio, "align_book_text", native)
    monkeypatch.setattr(audio, "_normalize_part", native)
    monkeypatch.setattr(audio.crispasr, "resolve_executable", native)
    item = _item()
    with pytest.raises(PermissionError, match="revoked"):
        if operation == "alignment":
            audio.align_take(item, {}, tmp_path, threading.Event(), before_io=_deny)
        elif operation == "normalization":
            audio.normalize_take(item, tmp_path / "out.wav", {}, threading.Event(), before_io=_deny)
        else:
            worker._verify_take(item, before_io=_deny)
    native.assert_not_called()


def test_alignment_rechecks_after_native_lock(monkeypatch, tmp_path) -> None:
    events: list[str] = []

    @contextmanager
    def lock(*_args):
        events.append("lock")
        yield

    def fence():
        events.append("fence")
        if "lock" in events:
            raise PermissionError("revoked")

    native = Mock()
    monkeypatch.setattr(audio, "native_audio_cpp_guard", lock)
    monkeypatch.setattr(audio.crispasr, "resolve_executable", lambda _: "crispasr")
    monkeypatch.setattr(audio, "align_book_text", native)
    with pytest.raises(PermissionError, match="revoked"):
        audio.align_take(_item(), {}, tmp_path, threading.Event(), before_io=fence)
    assert events == ["fence", "lock", "fence"]
    native.assert_not_called()


@pytest.mark.parametrize("stop", [1, 2, 3])
def test_vad_fences_prefetch_native_and_output(monkeypatch, tmp_path, stop) -> None:
    calls = 0
    prefetch, native = Mock(return_value=None), Mock()
    monkeypatch.setattr(crispasr, "_prefetch_windows_vad_model", prefetch)
    monkeypatch.setattr(crispasr, "build_vad_export_command", lambda *_args, **_kwargs: ["vad"])
    monkeypatch.setattr(crispasr, "_run_tool", native)

    def fence():
        nonlocal calls
        calls += 1
        if calls == stop:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        crispasr.run_vad_export("source.wav", tmp_path / "missing.json", {}, before_io=fence)
    assert prefetch.call_count == (0 if stop == 1 else 1)
    assert native.call_count == (1 if stop == 3 else 0)


def test_none_audio_callbacks_preserve_native_kwargs(monkeypatch, tmp_path) -> None:
    event = threading.Event()
    item = _item()
    aligned = object()

    def align(path, text, settings, workdir, cancel_event):
        assert (path, text, settings, workdir, cancel_event) == (
            item.path,
            "word",
            {},
            tmp_path,
            event,
        )
        return aligned

    def normalize(part, destination, encoding, *, ffmpeg_executable, cancel_event):
        assert part.path == item.path
        assert destination == tmp_path / "out.wav"
        assert encoding.sample_rate_hz == 16000 and encoding.channels == 1
        assert ffmpeg_executable == "ffmpeg" and cancel_event is event

    monkeypatch.setattr(audio, "align_book_text", align)
    monkeypatch.setattr(audio, "_normalize_part", normalize)
    monkeypatch.setattr(audio.crispasr, "resolve_executable", lambda _: "crispasr")
    monkeypatch.setattr(audio, "native_audio_cpp_guard", lambda *_args: nullcontext())
    assert audio.align_take(item, {}, tmp_path, event) is aligned
    audio.normalize_take(item, tmp_path / "out.wav", {}, event)


@pytest.mark.parametrize("stage", ["alignment", "normalization", "boundary"])
@pytest.mark.parametrize(
    "error",
    [
        PermissionError("revoked"),
        SessionNotFound(),
        ArtifactNotFound(),
        ModelAcquisitionDenied(),
        worker.SoundResolutionLeaseLost("lost"),
        RevisionConflict("changed"),
    ],
)
def test_worker_preserves_fence_exceptions(monkeypatch, tmp_path, stage, error) -> None:
    item = _item()
    aligned: Any = SimpleNamespace(text="word", diagnostics={"identity": item.identity})
    snapshot = SimpleNamespace(items=(item,), session_id="sid", settings={})
    database = SimpleNamespace(snapshot_session=lambda: nullcontext(object()))
    service = SimpleNamespace(database=database, paths=SimpleNamespace(temporary=tmp_path))
    monkeypatch.setattr(worker, "_lease", lambda *_args: None)
    monkeypatch.setattr(worker, "_pins", lambda *_args: snapshot)
    monkeypatch.setattr(worker, "_verify_take", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        worker, "find_cache", lambda *_args, **_kwargs: None if stage == "alignment" else aligned
    )
    monkeypatch.setattr(worker, "normalize_take", lambda *_args, **_kwargs: None)

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(
        worker,
        {
            "alignment": "align_take",
            "normalization": "normalize_take",
            "boundary": "resolve_boundary",
        }[stage],
        fail,
    )
    with execution_actor_scope(None), pytest.raises(type(error)) as caught:
        worker.run_resolution(service, {}, lambda *_args: None, threading.Event())
    assert caught.value is error


@pytest.mark.parametrize("stop", [1, 2, 3])
def test_cache_denial_is_not_swallowed_as_a_miss(monkeypatch, stop) -> None:
    path = Mock()
    path.is_file.return_value = True
    paths: Any = SimpleNamespace(managed_path=Mock(return_value=path))
    artifact = SimpleNamespace(id="cache", content_hash="hash", relative_path="cache.json")
    session: Any = Mock()
    session.scalars.return_value.all.return_value = [artifact]
    session.get.return_value = object()
    hashing = Mock(return_value="hash")
    monkeypatch.setattr(cache, "sha256_file", hashing)
    calls = 0

    def fence():
        nonlocal calls
        calls += 1
        if calls == stop:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        cache.find_cache(session, paths, "sid", _item(), before_io=fence)
    assert path.is_file.call_count == (0 if stop == 1 else 1)
    assert hashing.call_count == (1 if stop == 3 else 0)
    path.read_text.assert_not_called()


def test_take_hash_rechecks_after_inspection(monkeypatch) -> None:
    item = _item()
    item.path = Mock()
    item.path.is_file.return_value = True
    item.take_hash = "hash"
    hashing = Mock()
    monkeypatch.setattr(worker, "sha256_file", hashing)
    calls = 0

    def fence():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise PermissionError("revoked")

    with pytest.raises(PermissionError, match="revoked"):
        worker._verify_take(item, before_io=fence)
    item.path.is_file.assert_called_once_with()
    hashing.assert_not_called()
