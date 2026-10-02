"""Endpoint/session contracts without a provider server or live application."""

from contextlib import contextmanager

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import tts_handler
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
