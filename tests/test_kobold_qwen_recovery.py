"""Characterize Qwen readiness and cancellation with native HTTP responses."""

import json
import threading
from types import SimpleNamespace

import pytest
import requests

from pandrator.logic import tts_handler

BASE_URL = " http://qwen.invalid:8042/// "
NORMALIZED_URL = "http://qwen.invalid:8042"


def _response(status_code, payload=None, *, content=None):
    response = requests.Response()
    response.status_code = status_code
    response.encoding = "utf-8"
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(payload).encode() if content is None else content
    return response


@pytest.fixture
def api_key():
    return "recovery-fixture-key"


@pytest.fixture
def controlled_get(monkeypatch):
    def install(*outcomes):
        pending = iter(outcomes)
        calls = []

        def get(url, **options):
            calls.append((url, options))
            outcome = next(pending)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome

        monkeypatch.setattr(tts_handler.requests, "get", get)
        return calls

    return install


def _expected_gets(api_key, *paths):
    return [
        (
            f"{NORMALIZED_URL}/{path}",
            {"headers": {"Authorization": f"Bearer {api_key}"}, "timeout": 2},
        )
        for path in paths
    ]


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        pytest.param(_response(200), True, id="readyz-ready"),
        pytest.param(_response(503), False, id="readyz-unavailable"),
        pytest.param(
            requests.exceptions.RequestException("fixture failure"),
            False,
            id="readyz-request-error",
        ),
    ],
)
def test_readyz_result_does_not_use_health(controlled_get, api_key, outcome, expected):
    calls = controlled_get(outcome)

    assert tts_handler._kobold_qwen_is_ready(BASE_URL, api_key) is expected

    assert calls == _expected_gets(api_key, "readyz")


@pytest.mark.parametrize(
    ("health", "expected"),
    [
        pytest.param(_response(200, {"status": "ok", "kobold_online": True}), True, id="online"),
        pytest.param(_response(200, {"status": "ok", "kobold_online": False}), False, id="offline"),
        pytest.param(
            _response(200, {"status": "degraded", "kobold_online": True}), False, id="degraded"
        ),
        pytest.param(
            _response(200, {"status": "ok", "kobold_online": 1}), False, id="integer-is-not-true"
        ),
        pytest.param(
            _response(500, {"status": "ok", "kobold_online": True}), False, id="http-error"
        ),
        pytest.param(_response(200, ["ok", True]), False, id="non-object-json"),
        pytest.param(_response(200, content=b"invalid JSON"), False, id="invalid-json"),
    ],
)
def test_only_readyz_404_falls_back_to_health(controlled_get, api_key, health, expected):
    calls = controlled_get(_response(404), health)

    assert tts_handler._kobold_qwen_is_ready(BASE_URL, api_key) is expected

    assert calls == _expected_gets(api_key, "readyz", "health")


@pytest.fixture
def virtual_clock(monkeypatch):
    clock = SimpleNamespace(now=10.0, waits=[])
    monkeypatch.setattr(tts_handler, "time", SimpleNamespace(monotonic=lambda: clock.now))

    def wait(delay, cancel_event):
        clock.waits.append((delay, cancel_event))
        clock.now += delay
        return True

    monkeypatch.setattr(tts_handler, "wait_for_retry", wait)
    return clock


@pytest.fixture
def controlled_readiness(monkeypatch):
    def install(*outcomes):
        pending = iter(outcomes)
        calls = []

        def ready(base_url, api_key):
            calls.append((base_url, api_key))
            return next(pending)

        monkeypatch.setattr(tts_handler, "_kobold_qwen_is_ready", ready)
        return calls

    return install


def test_recovery_immediately_ready_does_not_wait(virtual_clock, controlled_readiness, api_key):
    calls = controlled_readiness(True)
    event = threading.Event()

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(BASE_URL, api_key=api_key, cancel_event=event)
        is True
    )

    assert calls == [(BASE_URL, api_key)]
    assert virtual_clock.waits == []
    assert virtual_clock.now == 10.0


def test_recovery_polls_native_readiness_until_ready(virtual_clock, controlled_get, api_key):
    calls = controlled_get(_response(503), _response(200))
    event = threading.Event()

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(BASE_URL, api_key=api_key, cancel_event=event)
        is True
    )

    assert calls == _expected_gets(api_key, "readyz", "readyz")
    assert virtual_clock.waits == [(0.5, event)]
    assert virtual_clock.now == 10.5


def test_initial_retry_delay_precedes_readiness(monkeypatch, virtual_clock, api_key):
    event = threading.Event()
    observations = []

    def ready(base_url, key):
        observations.append((virtual_clock.now, base_url, key, list(virtual_clock.waits)))
        return True

    monkeypatch.setattr(tts_handler, "_kobold_qwen_is_ready", ready)

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(
            BASE_URL, api_key=api_key, retry_after=0.25, cancel_event=event
        )
        is True
    )

    assert observations == [(10.25, BASE_URL, api_key, [(0.25, event)])]
    assert virtual_clock.waits == [(0.25, event)]


def test_initial_retry_delay_is_clipped_to_deadline(virtual_clock, controlled_readiness, api_key):
    calls = controlled_readiness()
    event = threading.Event()

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(
            BASE_URL, api_key=api_key, timeout_seconds=2, retry_after=9, cancel_event=event
        )
        is False
    )

    assert calls == []
    assert virtual_clock.waits == [(2.0, event)]
    assert virtual_clock.now == 12.0


def test_recovery_timeout_uses_remaining_fractional_poll_delay(
    virtual_clock, controlled_readiness, api_key
):
    calls = controlled_readiness(False, False, False)
    event = threading.Event()

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(
            BASE_URL, api_key=api_key, timeout_seconds=1.2, cancel_event=event
        )
        is False
    )

    assert calls == [(BASE_URL, api_key)] * 3
    assert [delay for delay, _event in virtual_clock.waits] == pytest.approx([0.5, 0.5, 0.2])
    assert all(wait_event is event for _delay, wait_event in virtual_clock.waits)
    assert virtual_clock.now == pytest.approx(11.2)


def test_recovery_pre_cancelled_does_not_query_or_wait(
    virtual_clock, controlled_readiness, api_key
):
    calls = controlled_readiness()
    event = threading.Event()
    event.set()

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(BASE_URL, api_key=api_key, cancel_event=event)
        is False
    )

    assert calls == []
    assert virtual_clock.waits == []


@pytest.mark.parametrize("retry_after", [0.0, 0.25], ids=["poll", "initial-delay"])
def test_cancelled_wait_stops_before_another_readiness_query(
    monkeypatch, virtual_clock, controlled_readiness, api_key, retry_after
):
    calls = controlled_readiness(False)
    event = threading.Event()

    def cancel_wait(delay, cancel_event):
        virtual_clock.waits.append((delay, cancel_event))
        virtual_clock.now += delay
        cancel_event.set()
        return False

    monkeypatch.setattr(tts_handler, "wait_for_retry", cancel_wait)

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(
            BASE_URL, api_key=api_key, retry_after=retry_after, cancel_event=event
        )
        is False
    )

    assert calls == ([(BASE_URL, api_key)] if retry_after == 0.0 else [])
    assert virtual_clock.waits == [(retry_after or 0.5, event)]
    assert event.is_set()


@pytest.mark.parametrize(("timeout_seconds", "clamped"), [(0.2, 1.0), (999.0, 300.0)])
def test_recovery_timeout_clamps_with_one_virtual_wait(
    virtual_clock, controlled_readiness, api_key, timeout_seconds, clamped
):
    calls = controlled_readiness()
    event = threading.Event()

    assert (
        tts_handler._wait_for_kobold_qwen_recovery(
            BASE_URL,
            api_key=api_key,
            timeout_seconds=timeout_seconds,
            retry_after=999,
            cancel_event=event,
        )
        is False
    )

    assert calls == []
    assert virtual_clock.waits == [(clamped, event)]
    assert virtual_clock.now == 10.0 + clamped
