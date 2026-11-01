"""Native generation stops at each current-authority effect boundary."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from pandrator.logic import audio_cpp_sfx as sfx
from pandrator.web.access.contracts import AccessDenied


@pytest.fixture
def native_case(tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Native fence tests permit no network")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    events = []
    state = {"denied": False, "runtime_loss": False, "child_loss": False}
    denied = AccessDenied()

    def before_io():
        events.append("authority")
        if state["denied"]:
            raise denied

    def runtime(*_args, **_kwargs):
        events.append("runtime")
        if state["runtime_loss"]:
            state["denied"] = True
        return {}

    def model(*_args, **_kwargs):
        events.append("model")
        return tmp_path / "synthetic.gguf"

    def child(*_args, **_kwargs):
        events.append("child")
        if state["child_loss"]:
            state["denied"] = True

    def inspect(*_args, **_kwargs):
        events.append("wav")
        return {"frames": 8}

    def target(_path):
        events.append("target")
        return False

    monkeypatch.setattr(sfx.os.path, "lexists", target)
    monkeypatch.setattr(sfx, "require_backend", lambda *_args: events.append("backend"))
    monkeypatch.setattr(sfx, "runtime_identity", runtime)
    monkeypatch.setattr(sfx.assets, "ensure_model", model)
    monkeypatch.setattr(sfx.generation_assets, "ensure_model", model)
    monkeypatch.setattr(sfx.assets, "resolve_executable", lambda *_args: Path("synthetic"))
    monkeypatch.setattr(sfx.processing, "_run_child", child)
    monkeypatch.setattr(sfx, "_validate_output_wav", inspect)
    monkeypatch.setattr(
        sfx.generation_payload, "build_argv", lambda *_args, **_kwargs: ["synthetic"]
    )
    monkeypatch.setattr(
        sfx.yue2_adapter,
        "ensure_adapter",
        lambda *_args, **_kwargs: events.append("adapter") or tmp_path / "adapter.gguf",
    )
    return SimpleNamespace(
        output=tmp_path / "output.wav",
        events=events,
        state=state,
        before_io=before_io,
        denied=denied,
    )


def invoke(f, **kwargs):
    request = kwargs.pop("request", {"prompt": "rain", "seed": 7, "duration_seconds": 1})
    return sfx.generate_sound(
        request, f.output, settings={}, cancel_event=threading.Event(), **kwargs
    )


def test_denied_entry_precedes_target_backend_runtime_and_model(native_case):
    f = native_case
    f.state["denied"] = True
    with pytest.raises(AccessDenied) as caught:
        invoke(f, before_io=f.before_io)
    assert caught.value is f.denied and f.events == ["authority"]


def test_runtime_loss_prevents_model_acquisition(native_case):
    f = native_case
    f.state["runtime_loss"] = True
    with pytest.raises(AccessDenied):
        invoke(f, before_io=f.before_io)
    assert f.events == ["authority", "target", "backend", "runtime", "authority"]


def test_progress_loss_prevents_native_process(native_case):
    f = native_case

    def progress(phase, completed, _total):
        assert phase == "generate" and completed == 0
        f.state["denied"] = True

    with pytest.raises(AccessDenied):
        invoke(f, before_io=f.before_io, progress=progress)
    assert "model" in f.events and "child" not in f.events and "wav" not in f.events


def test_child_loss_prevents_output_inspection(native_case):
    f = native_case
    f.state["child_loss"] = True
    with pytest.raises(AccessDenied):
        invoke(f, before_io=f.before_io)
    assert f.events[-2:] == ["child", "authority"] and "wav" not in f.events


def test_instrumental_adapter_has_its_own_fresh_boundary(native_case):
    f = native_case
    calls = 0

    def before_io():
        nonlocal calls
        calls += 1
        if calls == 3:
            raise f.denied

    with pytest.raises(AccessDenied):
        invoke(
            f,
            before_io=before_io,
            request={
                "model_id": "yue2_main_q8_0",
                "prompt": "piano",
                "seed": 4,
                "duration_seconds": 8,
                "parameters": {"instrumental": True},
            },
        )
    assert calls == 3 and "model" in f.events and "adapter" not in f.events
    assert "child" not in f.events


def test_legacy_no_hook_call_and_successful_hook_order(native_case):
    f = native_case
    assert invoke(f) == {"frames": 8}
    assert f.events == ["target", "backend", "runtime", "model", "child", "wav"]
    f.events.clear()
    assert invoke(f, before_io=f.before_io) == {"frames": 8}
    assert f.events == [
        "authority",
        "target",
        "backend",
        "runtime",
        "authority",
        "model",
        "authority",
        "child",
        "authority",
        "wav",
    ]
