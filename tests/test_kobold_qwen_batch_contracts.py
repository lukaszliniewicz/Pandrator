"""Qwen reader protocol exercised with native responses, queues and threads."""

import base64
import io
import json
import threading

import pytest
import requests
from pydub import AudioSegment

from pandrator.logic import tts_handler


class CountingResponse(requests.Response):
    def __init__(self, lines):
        super().__init__()
        self.status_code = 200
        self.encoding = "utf-8"
        self.headers["Content-Type"] = "application/x-ndjson"
        self.raw = io.BytesIO(("\n".join(lines) + "\n").encode())
        self.close_count = 0

    def close(self):
        self.close_count += 1
        super().close()


@pytest.fixture
def reader_threads(monkeypatch):
    created = []

    def create(*args, **kwargs):
        thread = threading.Thread(*args, **kwargs)
        created.append(thread)
        return thread

    monkeypatch.setattr(tts_handler, "Thread", create)
    yield created
    for thread in created:
        thread.join(timeout=2)
        assert not thread.is_alive(), "Controlled Qwen reader did not finish"


def _items(count=2):
    return [
        {
            "id": str(index),
            "text": f"Sentence {index}.",
            "settings": {
                "service": "kobold_qwen",
                "model": "Prebuilt Voices",
                "voice": "Ryan",
            },
        }
        for index in range(count)
    ]


def _completed_event(item_id="0"):
    buffer = io.BytesIO()
    AudioSegment.silent(duration=20).export(buffer, format="wav")
    return json.dumps(
        {
            "type": "item",
            "id": item_id,
            "status": "completed",
            "response_format": "wav",
            "audio_base64": base64.b64encode(buffer.getvalue()).decode(),
        }
    )


@pytest.mark.parametrize("error", [{"detail": "temporary", "retryable": True}, "malformed"])
def test_native_http_reader_preserves_completed_and_normalized_failed_events(
    monkeypatch, reader_threads, error
):
    response = CountingResponse(
        [
            '{"type":"batch","status":"running"}',
            _completed_event(),
            json.dumps({"type": "item", "id": "1", "status": "failed", "error": error}),
        ]
    )
    requests_seen = []

    def request(url, **options):
        requests_seen.append((url, options))
        return response

    monkeypatch.setattr(tts_handler.requests, "post", request)
    events = list(
        tts_handler.iter_kobold_qwen_batch_audio(
            _items(), base_url="http://qwen.invalid:8042", api_key="fixture-key"
        )
    )
    assert [event["id"] for event in events] == ["0", "1"]
    assert isinstance(events[0]["audio"], AudioSegment)
    assert len(events[0]["audio"]) == 20
    assert events[0]["error"] is None
    assert events[1]["audio"] is None
    assert isinstance(events[1]["error"], dict)
    assert events[1]["error"] == (
        error if isinstance(error, dict) else {"detail": "Qwen batch item failed."}
    )
    assert len(requests_seen) == 1
    assert requests_seen[0][1]["json"]["stream"] is True
    assert response.close_count == 1
    assert len(reader_threads) == 1
    assert reader_threads[0].name == "qwen-batch-reader" and reader_threads[0].daemon


def test_native_http_reader_propagates_bad_ndjson_and_closes_response(monkeypatch, reader_threads):
    response = CountingResponse(["not valid JSON"])
    monkeypatch.setattr(tts_handler.requests, "post", lambda *_args, **_options: response)
    with pytest.raises(RuntimeError, match="invalid NDJSON"):
        list(
            tts_handler.iter_kobold_qwen_batch_audio(
                _items(1), base_url="http://qwen.invalid:8042", api_key="fixture-key"
            )
        )
    assert response.close_count == 1
    assert len(reader_threads) == 1


def test_reader_keeps_exception_identity_after_an_event(monkeypatch, reader_threads):
    audio = AudioSegment.silent(duration=20)
    failure = RuntimeError("controlled reader failure")
    stops = []

    def produce(_items, *, stop_event, **_options):
        stops.append(stop_event)
        yield {"id": "0", "audio": audio, "error": None}
        raise failure

    monkeypatch.setattr(tts_handler, "_iter_kobold_qwen_batch_audio_http", produce)
    stream = tts_handler.iter_kobold_qwen_batch_audio(_items())
    try:
        assert next(stream)["audio"] is audio
        with pytest.raises(RuntimeError) as caught:
            next(stream)
        assert caught.value is failure
        assert stops[0].is_set()
        assert list(stream) == []
    finally:
        stream.close()


def test_reader_close_signals_the_admitted_native_producer(monkeypatch, reader_threads):
    waiting = threading.Event()
    finished = threading.Event()
    audio = AudioSegment.silent(duration=20)
    seen_cancel = []
    cancel = threading.Event()

    def produce(_items, *, stop_event, cancel_event, **_options):
        seen_cancel.append(cancel_event)
        try:
            yield {"id": "0", "audio": audio, "error": None}
            waiting.set()
            assert stop_event.wait(2), "Closing the consumer must signal its producer"
        finally:
            finished.set()

    monkeypatch.setattr(tts_handler, "_iter_kobold_qwen_batch_audio_http", produce)
    stream = tts_handler.iter_kobold_qwen_batch_audio(_items(), cancel_event=cancel)
    try:
        assert next(stream)["audio"] is audio
        assert waiting.wait(1)
        stream.close()
        assert finished.wait(2)
        assert seen_cancel == [cancel]
        assert list(stream) == []
    finally:
        stream.close()


def test_native_reader_cancellation_stops_after_the_inflight_result(monkeypatch, reader_threads):
    cancel = threading.Event()
    cancel.set()
    response = CountingResponse([_completed_event("0"), _completed_event("1")])
    monkeypatch.setattr(tts_handler.requests, "post", lambda *_args, **_options: response)
    events = list(
        tts_handler.iter_kobold_qwen_batch_audio(
            _items(),
            base_url="http://qwen.invalid:8042",
            api_key="fixture-key",
            cancel_event=cancel,
        )
    )
    assert [event["id"] for event in events] == ["0"]
    assert isinstance(events[0]["audio"], AudioSegment)
    assert response.close_count == 1


def test_empty_reader_does_not_start_a_worker_or_request(monkeypatch, reader_threads):
    def request(*_args, **_options):
        pytest.fail("An empty batch must not request synthesis")

    monkeypatch.setattr(tts_handler.requests, "post", request)
    assert list(tts_handler.iter_kobold_qwen_batch_audio([])) == []
    assert reader_threads == []


def test_native_reader_http_error_closes_its_unconsumed_response(monkeypatch, reader_threads):
    response = CountingResponse([])
    response.status_code = 500
    response.reason = "controlled server failure"
    response.url = "http://qwen.invalid:8042/v1/audio/speech/batch"
    monkeypatch.setattr(tts_handler.requests, "post", lambda *_args, **_options: response)
    try:
        with pytest.raises(requests.HTTPError) as caught:
            list(
                tts_handler.iter_kobold_qwen_batch_audio(
                    _items(1), base_url="http://qwen.invalid:8042", api_key="fixture-key"
                )
            )
        assert caught.value.response is response
        assert response.close_count == 1
        assert response.raw.closed
    finally:
        # Keep the intentionally failing pre-fix probe from retaining its own stream.
        response.raw.close()
