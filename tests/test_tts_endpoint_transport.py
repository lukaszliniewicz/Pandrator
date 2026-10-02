"""Endpoint/session contracts without a provider server or live application."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import tts_handler
from pandrator.logic.cancellable_process import ProcessCancelled
from pandrator.web.tts_providers import AudioCppAdapter, TtsBatchItem


@pytest.mark.parametrize("service", ["audio_cpp", "audio.cpp", "audio-cpp", "audiocpp"])
@pytest.mark.parametrize("configured", [False, True])
def test_single_and_batch_pool_and_lock_use_actual_synthesis_endpoint(
    monkeypatch, service, configured
):
    original = {"service": service}
    configured_url = "http://203.0.113.20:8060"
    override_url = "http://203.0.113.21:8060"
    if configured:
        original["audio_cpp_base_url"] = configured_url
    expected = configured_url if configured else override_url
    adapter = AudioCppAdapter("audio_cpp")
    sessions = []
    pool_urls = []
    lock_urls = []
    actual_urls = []

    def session_for(url):
        pool_urls.append(url)
        return session

    def synthesize(_text, settings, **options):
        effective = {**settings}
        effective.setdefault("audio_cpp_base_url", options["audio_cpp_base_url"])
        endpoint, error = tts_handler.resolve_openai_audio_endpoint(effective)
        assert endpoint is not None, error
        actual_urls.append(endpoint["base_url"])
        sessions.append(options["request_session"])
        return AudioSegment.silent(duration=20)

    @contextmanager
    def endpoint_lock(settings, _cancel=None):
        endpoint, error = tts_handler.resolve_openai_audio_endpoint(settings)
        assert endpoint is not None, error
        lock_urls.append(endpoint["base_url"])
        yield

    with requests.Session() as session:
        monkeypatch.setattr(adapter, "_session_for_base_url", session_for)
        monkeypatch.setattr(tts_handler, "text_to_audio", synthesize)
        monkeypatch.setattr(tts_handler, "audio_cpp_endpoint_lock", endpoint_lock)
        adapter.synthesize("First", original, audio_cpp_base_url=override_url)
        result = list(
            adapter.synthesize_batch(
                [TtsBatchItem("second", "Second", original)],
                batch_size=1,
                audio_cpp_base_url=override_url,
            )
        )
        assert result[0].error is None
        assert actual_urls == [expected, expected]
        assert pool_urls and set(pool_urls) == {expected}
        assert lock_urls == [expected]
        assert sessions == [session, session]
    assert original == (
        {"service": service, "audio_cpp_base_url": configured_url}
        if configured
        else {"service": service}
    )


@pytest.mark.parametrize("batch", [False, True])
def test_caller_owned_session_does_not_allocate_an_adapter_pool(monkeypatch, batch):
    adapter = AudioCppAdapter("audio_cpp")
    settings = {"service": "audio_cpp", "audio_cpp_base_url": "http://203.0.113.22:8060"}
    seen = []

    def unexpected_pool(_settings):
        pytest.fail("An explicitly supplied session must not allocate a pool")

    def synthesize(_text, _settings, **options):
        seen.append(options["request_session"])
        return AudioSegment.silent(duration=20)

    monkeypatch.setattr(adapter, "_session_for", unexpected_pool)
    monkeypatch.setattr(tts_handler, "text_to_audio", synthesize)
    with requests.Session() as session:
        if batch:
            results = list(
                adapter.synthesize_batch(
                    [TtsBatchItem("one", "One", settings)],
                    batch_size=1,
                    request_session=session,
                )
            )
            assert results[0].error is None
        else:
            adapter.synthesize("One", settings, request_session=session)
        assert seen == [session]


def test_batch_rejects_distinct_configured_endpoints_despite_common_fallback(monkeypatch):
    adapter = AudioCppAdapter("audio_cpp")

    def unexpected_request(*_args, **_kwargs):
        pytest.fail("Mixed endpoints must fail before synthesis")

    monkeypatch.setattr(tts_handler, "text_to_audio", unexpected_request)
    with pytest.raises(ValueError, match="same endpoint"):
        list(
            adapter.synthesize_batch(
                [
                    TtsBatchItem(
                        "one",
                        "One",
                        {"service": "audio_cpp", "audio_cpp_base_url": "http://203.0.113.23:8060"},
                    ),
                    TtsBatchItem(
                        "two",
                        "Two",
                        {"service": "audio_cpp", "audio_cpp_base_url": "http://203.0.113.24:8060"},
                    ),
                ],
                batch_size=2,
                audio_cpp_base_url="http://203.0.113.25:8060",
            )
        )


@pytest.mark.parametrize(
    "url, expected",
    [
        (" HTTP://EXAMPLE.TEST/ ", "http://example.test:80/"),
        ("http://example.test:80", "http://example.test:80/"),
        ("https://EXAMPLE.TEST:443/", "https://example.test:443/"),
        ("http://example.test:8060", "http://example.test:8060/"),
        (
            "https://user:synthetic@example.test/?secret=synthetic#fragment",
            "https://example.test:443/",
        ),
    ],
)
def test_endpoint_identity_and_facade_export_share_one_owner(url, expected):
    from pandrator.logic import tts_endpoint_transport as transport

    assert tts_handler._audio_cpp_endpoint_key is transport.audio_cpp_endpoint_key
    assert tts_handler._audio_cpp_endpoint_key(url) == expected
    assert tts_handler._audio_cpp_endpoint_lock_for(
        url
    ) is transport.audio_cpp_endpoint_lock_for_key(expected)


def test_concurrent_pool_creation_reuses_native_sessions_per_adapter_and_key(monkeypatch):
    adapter = AudioCppAdapter("audio_cpp")
    other_adapter = AudioCppAdapter("audio_cpp")
    original_session = requests.Session
    created = []
    barrier = threading.Barrier(8)
    variants = ["http://example.test", "HTTP://EXAMPLE.TEST:80/"] * 4

    def create_session():
        session = original_session()
        created.append(session)
        return session

    def lookup(url):
        barrier.wait(timeout=2)
        return adapter._session_for_base_url(url)

    monkeypatch.setattr(requests, "Session", create_session)
    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            sessions = list(pool.map(lookup, variants))
        assert len(created) == 1
        assert all(session is created[0] for session in sessions)
        assert adapter._session_for_base_url("http://other.example.test") is not created[0]
        assert other_adapter._session_for_base_url(variants[0]) is not created[0]
        assert len(created) == 3
    finally:
        for session in created:
            session.close()


def test_native_endpoint_guard_reentrancy_and_failed_entry_release():
    url = "http://203.0.113.26:8060"
    settings = {"service": "audio_cpp", "audio_cpp_base_url": url}
    cancel = threading.Event()

    with pytest.raises(RuntimeError, match="controlled failure"):
        with tts_handler.audio_cpp_endpoint_lock(settings, cancel):
            with tts_handler.audio_cpp_endpoint_lock(settings, cancel):
                raise RuntimeError("controlled failure")

    def acquire_from_other_thread():
        lock = tts_handler._audio_cpp_endpoint_lock_for(url)
        acquired = lock.acquire(timeout=1)
        if acquired:
            lock.release()
        return acquired

    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(acquire_from_other_thread).result(timeout=2)
        cancel.set()
        with pytest.raises(ProcessCancelled):
            with tts_handler.audio_cpp_endpoint_lock(settings, cancel):
                pytest.fail("Precancelled guard must not enter")
        assert pool.submit(acquire_from_other_thread).result(timeout=2)


def test_batch_generator_close_releases_endpoint_before_next_synthesis(monkeypatch):
    url = "http://203.0.113.27:8060"
    settings = {"service": "audio_cpp", "audio_cpp_base_url": url}
    adapter = AudioCppAdapter("audio_cpp")
    waiting = threading.Event()
    entered = threading.Event()
    calls = []
    original_lock_for = tts_handler._audio_cpp_endpoint_lock_for

    def observed_lock_for(base_url):
        lock = original_lock_for(base_url)
        if threading.current_thread() is thread:
            waiting.set()
        return lock

    def synthesize(text, _settings, **_options):
        calls.append(text)
        return AudioSegment.silent(duration=20)

    def waiter():
        with tts_handler.audio_cpp_endpoint_lock(settings):
            entered.set()

    thread = threading.Thread(target=waiter, daemon=True)
    monkeypatch.setattr(tts_handler, "_audio_cpp_endpoint_lock_for", observed_lock_for)
    monkeypatch.setattr(tts_handler, "text_to_audio", synthesize)
    with requests.Session() as session:
        stream = adapter.synthesize_batch(
            [TtsBatchItem("one", "One", settings), TtsBatchItem("two", "Two", settings)],
            batch_size=2,
            request_session=session,
        )
        try:
            assert next(stream).id == "one"
            thread.start()
            assert waiting.wait(1)
            assert not entered.wait(0.1)
            stream.close()
            assert entered.wait(1)
            assert calls == ["One"]
        finally:
            stream.close()
            if thread.ident is not None:
                thread.join(timeout=2)
                assert not thread.is_alive()


def custom_endpoint_settings(base_url, speech_url):
    return {
        "service": "Custom",
        "openai_audio_endpoint": "endpoint-fixture",
        "provider_configs": [
            {
                "id": "endpoint-fixture",
                "name": "Fixture audio.cpp",
                "provider": "openai",
                "adapter": "audio_cpp",
                "api_base": base_url,
                "speech_path": speech_url,
            }
        ],
    }


def test_base_paths_share_server_lock_without_combining_session_contexts():
    a = "http://203.0.113.30:8060/server-a"
    b = "http://203.0.113.30:8060/server-b"
    assert tts_handler._configured_endpoint_url(
        a, "/v1/audio/speech"
    ) == tts_handler._configured_endpoint_url(b, "/v1/audio/speech")
    assert tts_handler._audio_cpp_endpoint_lock_for(a) is tts_handler._audio_cpp_endpoint_lock_for(
        b
    )
    assert tts_handler._audio_cpp_endpoint_key(a) != tts_handler._audio_cpp_endpoint_key(b)
    adapter = AudioCppAdapter("audio_cpp")
    first = adapter._session_for_base_url(a)
    second = adapter._session_for_base_url(b)
    try:
        assert first is not second
    finally:
        first.close()
        second.close()


def test_absolute_speech_target_and_base_session_are_both_serialized():
    base = "http://203.0.113.31:8060/proxy"
    target = "http://203.0.113.32:8060/v1/audio/speech"
    settings = custom_endpoint_settings(base, target)

    def can_enter(url):
        lock = tts_handler._audio_cpp_endpoint_lock_for(url)
        entered = lock.acquire(timeout=0.1)
        if entered:
            lock.release()
        return entered

    with ThreadPoolExecutor(max_workers=1) as pool:
        with tts_handler.audio_cpp_endpoint_lock(settings):
            assert not pool.submit(can_enter, base).result(timeout=1)
            assert not pool.submit(can_enter, target).result(timeout=1)
        assert pool.submit(can_enter, base).result(timeout=1)
        assert pool.submit(can_enter, target).result(timeout=1)


def test_opposite_routes_acquire_servers_in_same_order_and_local_guards_first(monkeypatch):
    a = "http://203.0.113.33:8060"
    b = "http://203.0.113.34:8060"
    entered = []

    @contextmanager
    def local(url, _cancel=None):
        entered.append(("local", tts_handler._audio_cpp_endpoint_lock_key(url)))
        yield

    original_lock_for = tts_handler._audio_cpp_endpoint_lock_for

    def lock_for(url):
        entered.append(("endpoint", tts_handler._audio_cpp_endpoint_lock_key(url)))
        return original_lock_for(url)

    monkeypatch.setattr(tts_handler, "local_tts_audio_cpp_guard", local)
    monkeypatch.setattr(tts_handler, "_audio_cpp_endpoint_lock_for", lock_for)
    for base, target in [(a, b + "/v1/audio/speech"), (b, a + "/v1/audio/speech")]:
        entered.clear()
        with tts_handler.audio_cpp_endpoint_lock(custom_endpoint_settings(base, target)):
            assert entered == [
                ("local", a + "/"),
                ("local", b + "/"),
                ("endpoint", a + "/"),
                ("endpoint", b + "/"),
            ]


def test_batch_rejects_distinct_absolute_targets_before_synthesis(monkeypatch):
    base = "http://203.0.113.35:8060"
    first = custom_endpoint_settings(base, "http://203.0.113.36:8060/v1/audio/speech")
    second = custom_endpoint_settings(base, "http://203.0.113.37:8060/v1/audio/speech")
    adapter = AudioCppAdapter("audio_cpp")

    def unexpected(*_args, **_kwargs):
        pytest.fail("Different target servers must fail before synthesis")

    monkeypatch.setattr(tts_handler, "text_to_audio", unexpected)
    with pytest.raises(ValueError, match="same endpoint"):
        list(
            adapter.synthesize_batch(
                [TtsBatchItem("one", "One", first), TtsBatchItem("two", "Two", second)],
                batch_size=2,
            )
        )


def test_cancellation_on_second_server_releases_first_server(monkeypatch):
    first = "http://203.0.113.38:8060"
    second = "http://203.0.113.39:8060"
    settings = custom_endpoint_settings(first, second + "/v1/audio/speech")
    cancel = threading.Event()
    waiting = threading.Event()
    finished = threading.Event()
    errors = []
    original_lock_for = tts_handler._audio_cpp_endpoint_lock_for
    first_lock = original_lock_for(first)
    second_lock = original_lock_for(second)

    def observed_lock_for(url):
        lock = original_lock_for(url)
        if lock is second_lock and threading.current_thread() is thread:
            waiting.set()
        return lock

    def waiter():
        try:
            with tts_handler.audio_cpp_endpoint_lock(settings, cancel):
                pytest.fail("The second server is still held")
        except BaseException as error:
            errors.append(error)
        finally:
            finished.set()

    thread = threading.Thread(target=waiter, daemon=True)
    monkeypatch.setattr(tts_handler, "_audio_cpp_endpoint_lock_for", observed_lock_for)
    try:
        with second_lock:
            thread.start()
            assert waiting.wait(1)
            acquired = first_lock.acquire(blocking=False)
            if acquired:
                first_lock.release()
            assert not acquired
            cancel.set()
            assert finished.wait(1)
            assert len(errors) == 1 and isinstance(errors[0], ProcessCancelled)
            acquired = first_lock.acquire(timeout=1)
            assert acquired
            first_lock.release()
    finally:
        if thread.ident is not None:
            thread.join(timeout=2)
            assert not thread.is_alive()
