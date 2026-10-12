"""Qwen consumer lifetime qualified against real stalled localhost responses."""

from __future__ import annotations

import base64
import io
import json
import queue
import socket
import struct
import sys
import threading
import time
import wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest
import requests

from pandrator.logic import kobold_qwen_stream, tts_handler


@pytest.fixture
def stalled_server():
    entered = threading.Event()
    allow_headers = threading.Event()
    headers_sent = threading.Event()
    release = threading.Event()
    configuration = {"first_event": False, "requests": 0}
    request_lock = threading.Lock()
    body = io.BytesIO()
    with wave.open(body, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(22050)
        output.writeframes(b"\0\0" * 441)
    completed = (
        json.dumps(
            {
                "type": "item",
                "id": "0",
                "status": "completed",
                "response_format": "wav",
                "audio_base64": base64.b64encode(body.getvalue()).decode(),
            }
        ).encode()
        + b"\n"
    )
    padding = json.dumps({"type": "heartbeat", "padding": " " * 2048}).encode() + b"\n"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            with request_lock:
                configuration["requests"] += 1
            self.rfile.read(int(self.headers["Content-Length"]))
            entered.set()
            assert allow_headers.wait(3), "Fixture headers must be released"
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Content-Length", "65536")
            self.end_headers()
            self.wfile.flush()
            headers_sent.set()
            written = 0
            try:
                if configuration["first_event"]:
                    self.wfile.write(completed + padding)
                    self.wfile.flush()
                    written = len(completed + padding)
                release.wait(3)
                self.wfile.write(b"\n" * (65536 - written))
            except (BrokenPipeError, ConnectionResetError):
                pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    worker.start()
    try:
        yield (
            f"http://127.0.0.1:{server.server_port}",
            configuration,
            entered,
            allow_headers,
            headers_sent,
            release,
        )
    finally:
        allow_headers.set()
        release.set()
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
        assert not worker.is_alive()


@pytest.fixture
def readers(monkeypatch):
    created = []

    def factory(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(tts_handler, "Thread", factory)
    return created



@pytest.fixture
def native_interruptions(monkeypatch):
    """Retain socket retirement details when a native lifetime assertion fails."""
    observations = []
    original = tts_handler._iter_kobold_qwen_batch_audio_http

    def observed_http(*args, **kwargs):
        admit = kwargs.get("response_callback")

        def observe_response(response):
            if response is not None:
                raw = response.raw
                shutdown = getattr(raw, "_sock_shutdown", None)
                owner = getattr(shutdown, "__self__", None)
                record = {
                    "callback_available": callable(shutdown),
                    "owner_timeout": owner.gettimeout() if owner is not None else None,
                    "owner_fileno": owner.fileno() if owner is not None else None,
                    "calls": [],
                }
                observations.append(record)
                if callable(shutdown):

                    class ObservedShutdown:
                        def __init__(self):
                            self.__self__ = owner

                        def __call__(self, *shutdown_args):
                            started = time.monotonic()
                            result = {"mode": shutdown_args, "error": None}
                            try:
                                return shutdown(*shutdown_args)
                            except BaseException as error:
                                result["error"] = repr(error)
                                raise
                            finally:
                                result["elapsed"] = time.monotonic() - started
                                if len(record["calls"]) < 8:
                                    record["calls"].append(result)

                    raw._sock_shutdown = ObservedShutdown()
            if admit is not None:
                admit(response)

        yield from original(*args, **{**kwargs, "response_callback": observe_response})

    monkeypatch.setattr(tts_handler, "_iter_kobold_qwen_batch_audio_http", observed_http)
    return observations


def stream_for(server, cancel=None):
    return tts_handler.iter_kobold_qwen_batch_audio(
        [
            {
                "id": str(index),
                "text": "Hello.",
                "settings": {
                    "service": "Qwen3 TTS",
                    "model": "Prebuilt Voices",
                    "speaker": "Ryan",
                },
            }
            for index in range(2)
        ],
        base_url=server[0],
        cancel_event=cancel,
    )


def next_in_thread(stream):
    finished = threading.Event()
    outcome = []

    def consume():
        try:
            outcome.append(next(stream, None))
        except BaseException as error:
            outcome.append(error)
        finally:
            finished.set()

    consumer = threading.Thread(target=consume, daemon=True)
    consumer.start()
    return consumer, finished, outcome


def cleanup(server, readers, consumer=None):
    server[3].set()
    server[5].set()
    if consumer is not None:
        consumer.join(timeout=2)
        assert not consumer.is_alive(), "Fixture consumer survived cleanup"
    for reader in readers:
        reader.join(timeout=2)
        assert not reader.is_alive(), "Fixture reader survived cleanup"


class FakeReceiveOwner:
    def __init__(self, *, timeout=1800.0, socket_timeout=b"\x01\x02\x03\x04", fileno=42):
        self.timeout = timeout
        self.socket_timeout = socket_timeout
        self.descriptor = fileno
        self.fail_blocking_timeout = False
        self.calls = []
        self.release_body = threading.Event()

    def _sock_shutdown(self, how):
        self.calls.append(("shutdown", how))
        if how == socket.SHUT_RDWR:
            self.release_body.set()

    def gettimeout(self):
        self.calls.append(("gettimeout",))
        return self.timeout

    def getsockopt(self, level, option, length):
        self.calls.append(("getsockopt", level, option, length))
        return self.socket_timeout

    def setsockopt(self, level, option, value):
        self.calls.append(("setsockopt", level, option, value))
        self.socket_timeout = value

    def settimeout(self, value):
        self.calls.append(("settimeout", value))
        if value is None and self.fail_blocking_timeout:
            raise RuntimeError("settimeout(None) failed")
        self.timeout = value

    def fileno(self):
        self.calls.append(("fileno",))
        return self.descriptor


class FakeReceiveResponse:
    def __init__(self, owner):
        self.raw = owner

    def close(self):
        self.raw.calls.append(("response_close",))


def test_close_after_first_result_retires_stalled_native_reader(
    stalled_server, readers, native_interruptions
):
    stalled_server[1]["first_event"] = True
    stalled_server[3].set()
    stream = stream_for(stalled_server)
    try:
        assert next(stream)["id"] == "0"
        started = time.monotonic()
        stream.close()
        for reader in readers:
            reader.join(timeout=1)
        assert time.monotonic() - started < 1, native_interruptions
        assert readers and not any(reader.is_alive() for reader in readers)
    finally:
        cleanup(stalled_server, readers)
        stream.close()


@pytest.mark.parametrize("after_first", [False, True])
def test_cancel_while_waiting_retires_stalled_native_reader(
    stalled_server, readers, after_first, native_interruptions
):
    stalled_server[1]["first_event"] = after_first
    stalled_server[3].set()
    cancel = threading.Event()
    stream = stream_for(stalled_server, cancel)
    if after_first:
        assert next(stream)["id"] == "0"
    consumer, finished, outcome = next_in_thread(stream)
    try:
        assert stalled_server[4].wait(1)
        cancel.set()
        assert finished.wait(1), f"Canceled consumer remained blocked on body: {native_interruptions}"
        assert outcome == [None]
        assert readers and not any(reader.is_alive() for reader in readers)
    finally:
        cleanup(stalled_server, readers, consumer)
        stream.close()


def test_cancel_before_headers_keeps_owner_until_transport_can_be_retired(stalled_server, readers):
    cancel = threading.Event()
    stream = stream_for(stalled_server, cancel)
    consumer, finished, outcome = next_in_thread(stream)
    try:
        assert stalled_server[2].wait(1)
        cancel.set()
        assert not finished.wait(0.15), "Cannot retire an unreturned native request yet"
        assert readers and any(reader.is_alive() for reader in readers)
        stalled_server[3].set()
        assert finished.wait(1), "Late transport admission must observe stopped owner"
        assert outcome == [None]
        assert not any(reader.is_alive() for reader in readers)
    finally:
        cleanup(stalled_server, readers, consumer)
        stream.close()


def test_close_retires_reader_when_extra_provider_events_fill_queue(monkeypatch, readers):
    pressure = threading.Event()
    finished = threading.Event()

    class ObservedQueue(queue.Queue):
        def put(self, *args, **kwargs):
            if self.full():
                pressure.set()
            return super().put(*args, **kwargs)

    def producer(_items, **_options):
        try:
            for index in range(20):
                yield {"id": str(index), "audio": None, "error": {"detail": "fixture"}}
        finally:
            finished.set()

    monkeypatch.setattr(kobold_qwen_stream, "Queue", ObservedQueue)
    monkeypatch.setattr(tts_handler, "_iter_kobold_qwen_batch_audio_http", producer)
    stream = stream_for(("http://unused.invalid",))
    try:
        assert next(stream)["id"] == "0"
        assert pressure.wait(1), "Native queue must reach backpressure"
        started = time.monotonic()
        stream.close()
        assert time.monotonic() - started < 1
        assert finished.is_set()
        assert readers and not any(reader.is_alive() for reader in readers)
    finally:
        stream.close()


def test_close_uses_full_duplex_owner_shutdown_for_blocked_body_read():
    body_started = threading.Event()
    release_body = threading.Event()
    shutdown_entered = threading.Event()
    finish_shutdown = threading.Event()
    producer_clear_attempted = threading.Event()
    producer_clear_finished = threading.Event()
    producer_closed = threading.Event()
    raw_closed = threading.Event()
    shutdown_calls = []
    reader_threads = []
    close_thread = None
    close_errors = []

    class FakeRaw:
        def _sock_shutdown(self, how):
            shutdown_calls.append(("owner", how))
            if how == socket.SHUT_RDWR:
                release_body.set()
                shutdown_entered.set()
                finish_shutdown.wait()

        def shutdown(self):
            # Model urllib3's public shutdown path, which only shuts down reads.
            self._sock_shutdown(socket.SHUT_RD)

        def close(self):
            raw_closed.set()

    class FakeResponse:
        raw = FakeRaw()

        def close(self):
            self.raw.close()

    response = FakeResponse()

    def producer(_stop_event, set_response):
        set_response(response)
        try:
            yield {"id": "0", "audio": None, "error": None}
            body_started.set()
            release_body.wait()
        finally:
            producer_clear_attempted.set()
            set_response(None)
            producer_clear_finished.set()
            response.close()
            producer_closed.set()

    def thread_factory(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        reader_threads.append(thread)
        return thread

    stream = kobold_qwen_stream.iter_read_ahead_batch(
        1,
        producer=producer,
        thread_factory=thread_factory,
    )

    def close_stream():
        try:
            stream.close()
        except BaseException as error:
            close_errors.append(error)

    try:
        assert next(stream)["id"] == "0"
        assert body_started.wait(1), "Producer did not enter its simulated body read"
        close_thread = threading.Thread(target=close_stream, daemon=True)
        close_thread.start()
        assert shutdown_entered.wait(1), "Owner-socket shutdown was not attempted"
        assert producer_clear_attempted.wait(1), "Producer did not finish its body read"
        assert not producer_clear_finished.wait(0.1), (
            "Reader released response ownership before shutdown finished"
        )
        assert not raw_closed.is_set(), "Producer must wait before returning the response"
        finish_shutdown.set()
        close_thread.join(timeout=0.9)

        assert not close_thread.is_alive(), "Stream close did not retire the reader promptly"
        assert close_errors == []
        assert reader_threads and not any(thread.is_alive() for thread in reader_threads)
        assert shutdown_calls == [("owner", socket.SHUT_RDWR)]
        assert producer_clear_finished.is_set()
        assert producer_closed.is_set()
        assert raw_closed.is_set(), "Producer must retain response-close ownership"
    finally:
        # Always release the fixture body and bound both joins, even if shutdown regresses.
        release_body.set()
        finish_shutdown.set()
        if close_thread is None:
            stream.close()
        if close_thread is not None:
            close_thread.join(timeout=2)
        for reader in reader_threads:
            reader.join(timeout=2)
        assert close_thread is None or not close_thread.is_alive()
        assert not any(reader.is_alive() for reader in reader_threads)


def test_windows_receive_timeout_maps_and_restores_owner_socket(monkeypatch):
    original_socket_timeout = b"\x01\x02\x03\x04"
    owner = FakeReceiveOwner(socket_timeout=original_socket_timeout)
    response = FakeReceiveResponse(owner)
    body_started = threading.Event()
    producer_cleared = threading.Event()
    monkeypatch.setattr(kobold_qwen_stream, "_IS_WINDOWS", True)

    def producer(_stop_event, set_response):
        set_response(response)
        owner.calls.append(("body_read",))
        body_started.set()
        try:
            yield {"id": "0", "audio": None, "error": None}
            owner.release_body.wait()
        finally:
            set_response(None)
            response.close()
            producer_cleared.set()

    stream = kobold_qwen_stream.iter_read_ahead_batch(1, producer=producer)
    try:
        assert next(stream)["id"] == "0"
        assert body_started.wait(1), "Producer did not enter the simulated body read"

        stream.close()

        assert producer_cleared.is_set()
        assert owner.timeout == 1800.0
        assert owner.socket_timeout == original_socket_timeout
        assert owner.calls == [
            ("gettimeout",),
            ("getsockopt", socket.SOL_SOCKET, socket.SO_RCVTIMEO, 4),
            (
                "setsockopt",
                socket.SOL_SOCKET,
                socket.SO_RCVTIMEO,
                struct.pack("I", 1_800_000),
            ),
            ("settimeout", None),
            ("body_read",),
            ("shutdown", socket.SHUT_RDWR),
            ("fileno",),
            ("settimeout", 1800.0),
            ("setsockopt", socket.SOL_SOCKET, socket.SO_RCVTIMEO, original_socket_timeout),
            ("response_close",),
        ]
    finally:
        owner.release_body.set()
        stream.close()


@pytest.mark.parametrize(
    ("is_windows", "timeout", "has_owner", "socket_timeout"),
    [
        (False, 1800.0, True, b"\x01\x02\x03\x04"),
        (True, None, True, b"\x01\x02\x03\x04"),
        (True, 0.0, True, b"\x01\x02\x03\x04"),
        (True, 1800.0, False, b"\x01\x02\x03\x04"),
        (True, 1800.0, True, b"bad"),
    ],
)
def test_windows_receive_timeout_leaves_unsupported_owners_unchanged(
    monkeypatch, is_windows, timeout, has_owner, socket_timeout
):
    owner = FakeReceiveOwner(timeout=timeout, socket_timeout=socket_timeout)
    if has_owner:
        response = FakeReceiveResponse(owner)
    else:
        response = SimpleNamespace(raw=SimpleNamespace(_sock_shutdown=lambda *_args: None))
    monkeypatch.setattr(kobold_qwen_stream, "_IS_WINDOWS", is_windows)

    restore = kobold_qwen_stream._prepare_windows_receive_timeout(response)

    assert restore is None
    if not is_windows or not has_owner:
        assert owner.calls == []
    elif timeout is None or timeout == 0:
        assert owner.calls == [("gettimeout",)]
    else:
        assert owner.calls == [
            ("gettimeout",),
            ("getsockopt", socket.SOL_SOCKET, socket.SO_RCVTIMEO, 4),
        ]
    assert owner.timeout == timeout
    assert owner.socket_timeout == socket_timeout


def test_windows_receive_timeout_skips_restore_for_closed_owner(monkeypatch):
    owner = FakeReceiveOwner()
    response = FakeReceiveResponse(owner)
    monkeypatch.setattr(kobold_qwen_stream, "_IS_WINDOWS", True)

    restore = kobold_qwen_stream._prepare_windows_receive_timeout(response)

    assert restore is not None
    owner.descriptor = -1
    owner.calls.clear()
    restore()

    assert owner.calls == [("fileno",)]
    assert owner.timeout is None
    assert owner.socket_timeout == struct.pack("I", 1_800_000)


def test_windows_receive_timeout_restores_socket_option_when_setup_fails(monkeypatch):
    original_socket_timeout = b"\x01\x02\x03\x04"
    owner = FakeReceiveOwner(socket_timeout=original_socket_timeout)
    owner.fail_blocking_timeout = True
    monkeypatch.setattr(kobold_qwen_stream, "_IS_WINDOWS", True)

    with pytest.raises(RuntimeError, match=r"settimeout\(None\) failed"):
        kobold_qwen_stream._prepare_windows_receive_timeout(FakeReceiveResponse(owner))

    assert owner.timeout == 1800.0
    assert owner.socket_timeout == original_socket_timeout
    assert owner.calls == [
        ("gettimeout",),
        ("getsockopt", socket.SOL_SOCKET, socket.SO_RCVTIMEO, 4),
        (
            "setsockopt",
            socket.SOL_SOCKET,
            socket.SO_RCVTIMEO,
            struct.pack("I", 1_800_000),
        ),
        ("settimeout", None),
        ("fileno",),
        ("settimeout", 1800.0),
        ("setsockopt", socket.SOL_SOCKET, socket.SO_RCVTIMEO, original_socket_timeout),
    ]


@pytest.mark.skipif(sys.platform != "win32", reason="Requires native Winsock receive timeouts")
def test_windows_short_receive_timeout_raises_once_after_partial_body(
    stalled_server, readers, monkeypatch, native_interruptions
):
    stalled_server[1]["first_event"] = True
    stalled_server[3].set()
    original = tts_handler._iter_kobold_qwen_batch_audio_http

    def set_short_timeout_before_admission(*args, **kwargs):
        callback = kwargs.get("response_callback")

        def admit_with_short_timeout(response):
            if response is not None:
                shutdown = getattr(response.raw, "_sock_shutdown", None)
                owner = getattr(shutdown, "__self__", None)
                assert owner is not None, "The admitted response must expose its socket owner"
                owner.settimeout(0.2)
            if callback is not None:
                callback(response)

        yield from original(
            *args,
            **{**kwargs, "response_callback": admit_with_short_timeout},
        )

    monkeypatch.setattr(
        tts_handler,
        "_iter_kobold_qwen_batch_audio_http",
        set_short_timeout_before_admission,
    )
    stream = stream_for(stalled_server)
    consumer = None
    try:
        assert next(stream)["id"] == "0"
        started = time.monotonic()
        consumer, finished, outcome = next_in_thread(stream)

        assert finished.wait(1), (
            f"Native receive timeout did not retire the reader: {native_interruptions}"
        )
        assert time.monotonic() - started < 1
        assert len(outcome) == 1
        assert isinstance(outcome[0], requests.exceptions.RequestException)
        assert stalled_server[1]["requests"] == 1, (
            "A partial-body timeout must not retry the request"
        )
        assert readers and not any(reader.is_alive() for reader in readers)
    finally:
        cleanup(stalled_server, readers, consumer)
        stream.close()
