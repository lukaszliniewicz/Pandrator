"""Local audio.cpp execution coordination without a running server or model."""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path

import pytest

from pandrator.logic import audio_cpp_execution as guard
from pandrator.logic import tts_handler
from pandrator.logic.cancellable_process import ProcessCancelled


@pytest.fixture(autouse=True)
def isolated_lock(monkeypatch, tmp_path):
    monkeypatch.setattr(guard, "_lock_path", lambda: tmp_path / "execution.lock")


def _command(tmp_path: Path, backend: str = "vulkan") -> list[str]:
    return [str(tmp_path / "audiocpp_cli"), "--backend", backend]


def _responses(monkeypatch, *values):
    calls = []
    queue = iter(values)

    def open_response(request, *, timeout):
        calls.append((request, timeout))
        value = next(queue)
        if isinstance(value, BaseException):
            raise value
        return io.BytesIO(json.dumps(value).encode("utf-8"))

    monkeypatch.setattr(guard.urllib.request, "urlopen", open_response)
    return calls


def test_nested_lock_and_exception_release():
    with guard.local_audio_cpp_lock():
        with guard.local_audio_cpp_lock(timeout=0.01):
            pass
    with pytest.raises(RuntimeError, match="boom"):
        with guard.local_audio_cpp_lock():
            raise RuntimeError("boom")
    with guard.local_audio_cpp_lock(timeout=0.1):
        pass


def test_threads_serialize_and_cancellation_releases():
    entered = threading.Event()
    finished = threading.Event()
    cancellation = threading.Event()
    result = []

    def waiter():
        entered.set()
        try:
            with guard.local_audio_cpp_lock(cancellation, timeout=2):
                result.append("entered")
        except ProcessCancelled:
            result.append("cancelled")
        finally:
            finished.set()

    with guard.local_audio_cpp_lock():
        thread = threading.Thread(target=waiter)
        thread.start()
        assert entered.wait(1)
        cancellation.set()
        assert finished.wait(1)
        assert result == ["cancelled"]
    thread.join(timeout=1)
    with guard.local_audio_cpp_lock(timeout=0.1):
        pass


def test_cancel_after_acquisition_releases():
    cancellation = threading.Event()
    with guard.local_audio_cpp_lock(cancellation):
        cancellation.set()
    with pytest.raises(ProcessCancelled):
        with guard.local_audio_cpp_lock(cancellation):
            pass
    with guard.local_audio_cpp_lock(timeout=0.1):
        pass


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "5"])
def test_invalid_timeout(timeout):
    with pytest.raises(ValueError):
        with guard.local_audio_cpp_lock(timeout=timeout):
            pass


def test_elapsed_timeout():
    done = threading.Event()
    result = []

    def waiter():
        try:
            with guard.local_audio_cpp_lock(timeout=0.06):
                result.append("entered")
        except guard.AudioCppExecutionError:
            result.append("timeout")
        finally:
            done.set()

    with guard.local_audio_cpp_lock():
        thread = threading.Thread(target=waiter)
        thread.start()
        assert done.wait(1)
    thread.join(timeout=1)
    assert result == ["timeout"]


@pytest.mark.skipif(os.name == "nt", reason="Uses the POSIX test runner")
def test_processes_serialize(tmp_path):
    marker = tmp_path / "entered"
    script = (
        "import pathlib, sys\n"
        "from pandrator.logic import audio_cpp_execution as g\n"
        "g._lock_path = lambda: pathlib.Path(sys.argv[1])\n"
        "with g.local_audio_cpp_lock():\n"
        "    pathlib.Path(sys.argv[2]).write_text('yes')\n"
    )
    process = None
    try:
        with guard.local_audio_cpp_lock():
            process = subprocess.Popen(
                [sys.executable, "-c", script, str(guard._lock_path()), str(marker)],
                cwd=Path(__file__).resolve().parents[1],
            )
            time.sleep(0.15)
            assert not marker.exists()
            assert process.poll() is None
        assert process.wait(timeout=2) == 0
        assert marker.read_text() == "yes"
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait()


@pytest.mark.parametrize(
    "url,expected",
    [
        ("http://127.0.0.1:8060", True),
        ("http://localhost:8060", True),
        ("http://[::1]:8060", True),
        ("https://example.org:8060", False),
        ("http://192.0.2.1:8060", False),
    ],
)
def test_tts_only_local(monkeypatch, url, expected):
    calls = []

    @contextmanager
    def fake_lock(_event=None, **_kwargs):
        calls.append("lock")
        yield

    monkeypatch.setattr(guard, "local_audio_cpp_lock", fake_lock)
    with guard.local_tts_audio_cpp_guard(url):
        pass
    assert bool(calls) is expected


def test_tts_endpoint_guard_wraps_existing_endpoint_lock(monkeypatch):
    order = []

    @contextmanager
    def shared(url, cancel_event=None):
        order.append(("shared-in", url))
        yield
        order.append(("shared-out", url))

    @contextmanager
    def endpoint(url):
        order.append(("endpoint-in", url))
        yield
        order.append(("endpoint-out", url))

    monkeypatch.setattr(
        tts_handler,
        "resolve_openai_audio_endpoint",
        lambda _settings: (
            {"adapter": "audio_cpp", "base_url": "http://localhost:8060"},
            None,
        ),
    )
    monkeypatch.setattr(tts_handler, "local_tts_audio_cpp_guard", shared)
    monkeypatch.setattr(tts_handler, "_audio_cpp_endpoint_lock_for", endpoint)
    with tts_handler.audio_cpp_endpoint_lock({}):
        order.append(("yield", ""))
    assert [item[0] for item in order] == [
        "shared-in",
        "endpoint-in",
        "yield",
        "endpoint-out",
        "shared-out",
    ]


def test_cpu_skips_residency_http(monkeypatch, tmp_path):
    monkeypatch.setattr(
        guard.urllib.request, "urlopen", lambda *_args, **_kwargs: pytest.fail("HTTP")
    )
    with guard.native_audio_cpp_guard(_command(tmp_path, "cpu")):
        pass


def test_missing_backend_checks_residency(monkeypatch, tmp_path):
    calls = _responses(monkeypatch, {"data": []})
    with guard.native_audio_cpp_guard([str(tmp_path / "audiocpp_cli")]):
        pass
    assert len(calls) == 1


def test_gpu_empty_models_proceeds_without_unload(monkeypatch, tmp_path):
    calls = _responses(monkeypatch, {"data": []})
    with guard.native_audio_cpp_guard(_command(tmp_path)):
        pass
    assert len(calls) == 1
    assert calls[0][1] == 5


def test_gpu_releases_only_loaded_ids_and_verifies(monkeypatch, tmp_path):
    calls = _responses(
        monkeypatch,
        {"data": [{"id": "active", "loaded": True}, {"id": "idle", "loaded": False}]},
        {"unloaded": ["active"], "not_found": []},
        {"data": [{"id": "active", "loaded": False}]},
    )
    with guard.native_audio_cpp_guard(_command(tmp_path)):
        pass
    assert len(calls) == 3
    assert isinstance(calls[1][0], guard.urllib.request.Request)
    assert calls[1][0].get_method() == "POST"
    assert json.loads(calls[1][0].data) == {"model_ids": ["active"]}
    assert calls[1][1] == 10


@pytest.mark.parametrize(
    "responses",
    [
        ({"data": [{"id": "x"}]},),
        ({"data": "bad"},),
        ({"data": [{"id": "x", "loaded": "true"}]},),
        ({"data": [{"id": "", "loaded": True}]},),
        ({"data": [{"id": "x", "loaded": True}]}, {"error": "unsupported"}),
        (
            {"data": [{"id": "x", "loaded": True}]},
            {"unloaded": [], "not_found": []},
            {"data": [{"id": "x", "loaded": True}]},
        ),
    ],
)
def test_gpu_invalid_residency_fails_closed(monkeypatch, tmp_path, responses):
    _responses(monkeypatch, *responses)
    with pytest.raises(guard.AudioCppExecutionError):
        with guard.native_audio_cpp_guard(_command(tmp_path)):
            pass


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(TimeoutError("timed out")),
        urllib.error.HTTPError("http://127.0.0.1", 404, "missing", {}, None),
        TimeoutError("timed out"),
    ],
)
def test_gpu_probe_errors_fail_closed(monkeypatch, tmp_path, error):
    _responses(monkeypatch, error)
    with pytest.raises(guard.AudioCppExecutionError):
        with guard.native_audio_cpp_guard(_command(tmp_path)):
            pass


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.HTTPError("http://127.0.0.1", 404, "missing", {}, None),
        urllib.error.URLError(TimeoutError("timed out")),
    ],
)
def test_gpu_unload_errors_fail_closed(monkeypatch, tmp_path, error):
    calls = _responses(monkeypatch, {"data": [{"id": "active", "loaded": True}]}, error)
    with pytest.raises(
        guard.AudioCppExecutionError, match="stop its service or choose CPU"
    ):
        with guard.native_audio_cpp_guard(_command(tmp_path)):
            pass
    assert len(calls) == 2


def test_gpu_oversize_response_fails_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(
        guard.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: io.BytesIO(b"x" * (1024 * 1024 + 1)),
    )
    with pytest.raises(guard.AudioCppExecutionError, match="too large"):
        with guard.native_audio_cpp_guard(_command(tmp_path)):
            pass


@pytest.mark.parametrize(
    "error",
    [
        urllib.error.URLError(ConnectionRefusedError(111, "refused")),
        ConnectionRefusedError(111, "refused"),
    ],
)
def test_connection_refused_allows_stopped_service(monkeypatch, tmp_path, error):
    calls = _responses(monkeypatch, error)
    with guard.native_audio_cpp_guard(_command(tmp_path)):
        pass
    assert len(calls) == 1


@pytest.mark.parametrize(
    "host,port,expected",
    [
        ("127.0.0.1", 9876, "http://127.0.0.1:9876"),
        ("0.0.0.0", 9876, "http://127.0.0.1:9876"),
        ("::", 9876, "http://[::1]:9876"),
        ("::1", 9876, "http://[::1]:9876"),
    ],
)
def test_server_config_address(tmp_path, host, port, expected):
    (tmp_path / "server.json").write_text(json.dumps({"host": host, "port": port}))
    assert guard._server_url(_command(tmp_path)) == expected


@pytest.mark.parametrize(
    "config",
    [
        {"host": "example.org", "port": 8060},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": "8060"},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1"},
        [],
    ],
)
def test_invalid_server_config_fails_closed(tmp_path, config):
    (tmp_path / "server.json").write_text(json.dumps(config))
    with pytest.raises(guard.AudioCppExecutionError):
        guard._server_url(_command(tmp_path))
