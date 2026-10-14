from __future__ import annotations

import inspect
import json
import math
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
import requests

from pandrator.logic import kobold_qwen_http, retry_utils, tts_handler
from pandrator.logic.tts_retry import (
    DEFAULT_TTS_MAX_ATTEMPTS,
    DEFAULT_TTS_RETRY_TIMEOUT_SECONDS,
    TtsRetryBudget,
    TtsRetryBudgetExceeded,
    TtsRetryGate,
    current_retry_budget,
    make_retry_scope_key,
    retry_budget_scope,
)
from pandrator.web.tts_provider_contracts import TtsRetryPolicy


class FakeClock:
    def __init__(self, initial: float = 0.0):
        self.now = initial
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return self.now

    def advance(self, seconds: float) -> None:
        with self._lock:
            self.now += seconds

    def advance_to(self, target: float) -> None:
        with self._lock:
            self.now = max(self.now, target)


def provider_error(headers: dict[str, str]) -> RuntimeError:
    response = SimpleNamespace(headers=headers)
    error = RuntimeError("provider rate limit")
    error.response = response  # type: ignore[attr-defined]
    return error


def audio_response(status: int = 200, *, retry_after: str | None = None):
    headers = {"Content-Type": "audio/wav"}
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    response = Mock(status_code=status, headers=headers, text="")
    if status >= 400:
        response.raise_for_status.side_effect = requests.exceptions.HTTPError(
            f"HTTP {status}", response=response
        )
    else:
        response.raise_for_status.return_value = None
    return response


def silero_settings(**overrides):
    return {
        "service": "Silero",
        "silero_model": "v3_en",
        "speaker": "en_0",
        "language": "en",
        **overrides,
    }


def test_retry_after_guidance_can_exceed_the_local_backoff_cap():
    now = datetime(2026, 10, 14, 12, 0, tzinfo=timezone.utc)
    date_value = format_datetime(now + timedelta(seconds=300), usegmt=True)

    for value in ("300", date_value):
        error = provider_error({"Retry-After": value})
        retry_after = retry_utils.retry_after_seconds(error, now=now)
        assert retry_after == pytest.approx(300.0)
        assert retry_utils.retry_delay_seconds(
            1,
            retry_after=retry_after,
            maximum_delay=90.0,
            jitter_ratio=0.0,
        ) == pytest.approx(300.0)


@pytest.mark.parametrize("value", ("NaN", "inf", "-inf", "not-a-date"))
def test_malformed_nonfinite_retry_headers_never_create_infinite_delays(value: str):
    error = provider_error(
        {
            "Retry-After": value,
            "Retry-After-Ms": value,
            "X-RateLimit-Reset": value,
        }
    )
    delay = retry_utils.retry_after_seconds(error)
    calculated = retry_utils.retry_delay_seconds(
        2,
        retry_after=math.inf,
        base_delay=math.nan,
        maximum_delay=math.inf,
    )
    assert math.isfinite(delay)
    assert delay == 0.0
    assert math.isfinite(calculated)
    assert 0.0 <= calculated <= 90.0


def test_default_and_explicit_retry_policy_limits_are_preserved():
    default_policy = TtsRetryPolicy.from_settings({})
    assert DEFAULT_TTS_MAX_ATTEMPTS == 10
    assert DEFAULT_TTS_RETRY_TIMEOUT_SECONDS == 900
    assert default_policy.max_attempts == 10
    assert default_policy.maximum_elapsed_seconds == 900
    assert TtsRetryPolicy.from_settings(
        {"max_attempts": 1, "retry_timeout_seconds": 1}
    ).max_attempts == 1
    assert TtsRetryPolicy.from_settings(
        {"max_attempts": 20, "retry_timeout_seconds": 3600}
    ).max_attempts == 20
    assert TtsRetryPolicy.from_settings(
        {"max_attempts": 20, "retry_timeout_seconds": 3600}
    ).maximum_elapsed_seconds == 3600
    assert (
        inspect.signature(tts_handler.text_to_audio)
        .parameters["max_attempts"]
        .default
        == 10
    )


def test_request_timeout_uses_remaining_monotonic_budget():
    clock = FakeClock(10.0)
    budget = TtsRetryBudget(1, clock=clock)
    budget.start()
    assert budget.request_timeout(300) == 1.0
    clock.advance(0.25)
    assert budget.request_timeout(300) == pytest.approx(0.75)
    clock.advance(0.75)
    with pytest.raises(TtsRetryBudgetExceeded):
        budget.request_timeout(300)


def test_nested_retry_scope_preserves_the_same_budget_and_resets_afterward():
    clock = FakeClock()
    with retry_budget_scope(20, clock=clock) as outer:
        with retry_budget_scope(2, clock=clock) as inner:
            assert inner is outer
            outer.start()
            clock.advance(3)
            assert outer.remaining_seconds() == 17.0
    assert current_retry_budget() is None


def test_tts_request_timeouts_shrink_across_retries_with_one_budget():
    clock = FakeClock()
    gate = TtsRetryGate(clock=clock, waiter=lambda delay, _event: True)
    failed = audio_response(503)
    succeeded = audio_response()
    posted_timeouts: list[float] = []

    def post(_url: str, **kwargs):
        posted_timeouts.append(kwargs["timeout"])
        return failed if len(posted_timeouts) == 1 else succeeded

    def wait(delay: float, _event=None) -> bool:
        clock.advance(delay)
        return True

    with (
        patch.object(tts_handler, "_TTS_RETRY_GATE", gate),
        patch.object(tts_handler.requests, "post", side_effect=post),
        patch.object(tts_handler, "wait_for_retry", side_effect=wait),
        patch.object(retry_utils.random, "uniform", side_effect=lambda low, _high: low),
        patch.object(tts_handler, "_decode_audio_response", return_value=object()),
        retry_budget_scope(1, clock=clock),
    ):
        result = tts_handler.text_to_audio(
            "Retry within budget",
            silero_settings(
                retry_timeout_seconds=1,
                retry_max_delay_seconds=10,
                rate_limit_retry_base_delay_seconds=0.5,
            ),
            max_attempts=2,
            silero_base_url="https://silero.invalid",
        )

    assert result is not None
    assert posted_timeouts == pytest.approx([1.0, 0.6])
    assert clock.now == pytest.approx(0.4)
    assert current_retry_budget() is None


def test_retry_after_that_cannot_fit_budget_is_not_announced_or_retried(caplog):
    clock = FakeClock()
    gate = TtsRetryGate(
        clock=clock,
        waiter=lambda delay, _event: (clock.advance(delay) or True),
    )
    limited = audio_response(429, retry_after="300")
    post = Mock(return_value=limited)
    wait = Mock(return_value=True)
    retry_callback = Mock()

    with (
        patch.object(tts_handler, "_TTS_RETRY_GATE", gate),
        patch.object(tts_handler.requests, "post", post),
        patch.object(tts_handler, "wait_for_retry", wait),
        patch.object(tts_handler, "_decode_audio_response", return_value=object()),
        retry_budget_scope(1, clock=clock),
        pytest.raises(tts_handler.TtsGenerationError) as raised,
    ):
        tts_handler.text_to_audio(
            "Stop at the budget",
            silero_settings(retry_timeout_seconds=1),
            max_attempts=2,
            silero_base_url="https://silero.invalid",
            retry_callback=retry_callback,
        )

    assert post.call_count == 1
    wait.assert_not_called()
    retry_callback.assert_not_called()
    assert "Retrying TTS generation" not in caplog.text
    assert "Retry-After" in str(raised.value)
    assert "HTTP 429" in str(raised.value)
    assert "after 1 attempt(s)" in str(raised.value)
    assert raised.value.retryable
    assert current_retry_budget() is None


@pytest.mark.parametrize(
    ("recovery_elapsed", "expected_wait"),
    [(0.0, 60.0), (2.0, 58.0)],
)
def test_qwen_recovery_observes_remaining_retry_after(
    recovery_elapsed: float, expected_wait: float
):
    clock = FakeClock()
    gate = TtsRetryGate(
        clock=clock,
        waiter=lambda delay, _event: (clock.advance(delay) or True),
    )
    limited = audio_response(503, retry_after="60")
    succeeded = audio_response()
    provider_calls = 0
    retry_waits: list[float] = []
    calls: list[float] = []

    def request_audio(*_args, **_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        calls.append(clock())
        return limited if provider_calls == 1 else succeeded

    def recover(*_args, **kwargs):
        assert kwargs["retry_after"] == 0.0
        clock.advance(recovery_elapsed)
        return True

    def wait(delay: float, _event=None) -> bool:
        retry_waits.append(delay)
        clock.advance(delay)
        return True

    with (
        patch.object(tts_handler, "_TTS_RETRY_GATE", gate),
        patch.object(
            tts_handler, "_request_kobold_qwen_audio", side_effect=request_audio
        ),
        patch.object(
            tts_handler, "_wait_for_kobold_qwen_recovery", side_effect=recover
        ),
        patch.object(tts_handler, "wait_for_retry", side_effect=wait),
        patch.object(
            retry_utils.random, "uniform", side_effect=lambda _low, _high: 0.0
        ),
        patch.object(tts_handler, "_decode_audio_response", return_value=object()),
        retry_budget_scope(120, clock=clock),
    ):
        result = tts_handler.text_to_audio(
            "Count recovery against cooldown",
            {
                "service": "Qwen3 TTS",
                "model": "Voice Cloning",
                "voice": "narrator",
                "retry_timeout_seconds": 120,
            },
            max_attempts=2,
            kobold_qwen_base_url="https://qwen.invalid",
        )

    assert result is not None
    assert provider_calls == 2
    assert retry_waits == [expected_wait]
    assert calls == [0.0, 60.0]
    assert clock() == 60.0


def test_kobold_qwen_post_timeout_is_clamped_and_stops_expired_fallback():
    clock = FakeClock()
    urls = ["https://qwen.invalid/first", "https://qwen.invalid/fallback"]
    response = Mock(status_code=404)
    calls: list[tuple[str, float]] = []

    def post(url: str, **kwargs):
        calls.append((url, kwargs["timeout"]))
        clock.advance(1.0)
        return response

    with (
        patch.object(kobold_qwen_http, "_openai_audio_speech_urls", return_value=urls),
        patch.object(kobold_qwen_http.requests, "post", side_effect=post),
        retry_budget_scope(1, clock=clock),
        pytest.raises(TtsRetryBudgetExceeded),
    ):
        kobold_qwen_http._request_kobold_qwen_audio(
            "Hello",
            {},
            "https://qwen.invalid",
            resolve_api_key=lambda _settings: "",
            build_payload=lambda text, _settings: {"input": text},
        )

    assert calls == [(urls[0], 1.0)]
    assert current_retry_budget() is None


def test_kobold_qwen_readiness_recomputes_timeout_for_health_fallback():
    clock = FakeClock()
    calls: list[tuple[str, float]] = []

    def get(url: str, **kwargs):
        calls.append((url, kwargs["timeout"]))
        if url.endswith("/readyz"):
            clock.advance(0.75)
            return Mock(status_code=404)
        response = Mock(status_code=200)
        response.json.return_value = {"status": "ok", "kobold_online": True}
        return response

    with (
        patch.object(kobold_qwen_http.requests, "get", side_effect=get),
        retry_budget_scope(2, clock=clock),
    ):
        ready = kobold_qwen_http._kobold_qwen_is_ready(
            "https://qwen.invalid", resolve_api_key=lambda: ""
        )

    assert ready
    assert calls == [
        ("https://qwen.invalid/readyz", 2.0),
        ("https://qwen.invalid/health", 1.25),
    ]


def test_readiness_budget_expiry_is_wrapped_with_last_provider_failure():
    clock = FakeClock()
    gate = TtsRetryGate(
        clock=clock,
        waiter=lambda delay, _event: (clock.advance(delay) or True),
    )
    limited = audio_response(503)
    readiness_calls: list[tuple[str, float]] = []

    def get(url: str, **kwargs):
        readiness_calls.append((url, kwargs["timeout"]))
        clock.advance(1.0)
        return Mock(status_code=404)

    with (
        patch.object(tts_handler, "_TTS_RETRY_GATE", gate),
        patch.object(tts_handler, "_request_kobold_qwen_audio", return_value=limited),
        patch.object(kobold_qwen_http.requests, "get", side_effect=get),
        retry_budget_scope(1, clock=clock),
        pytest.raises(tts_handler.TtsGenerationError) as raised,
    ):
        tts_handler.text_to_audio(
            "Keep the original provider failure",
            {
                "service": "Qwen3 TTS",
                "model": "Voice Cloning",
                "voice": "narrator",
                "retry_timeout_seconds": 1,
                "service_recovery_cycles": 1,
            },
            max_attempts=2,
            kobold_qwen_base_url="https://qwen.invalid",
        )

    assert readiness_calls == [("https://qwen.invalid/readyz", 1.0)]
    assert "HTTP 503" in str(raised.value)
    assert "after 1 attempt(s)" in str(raised.value)
    assert raised.value.retryable
    assert isinstance(raised.value.__cause__, TtsRetryBudgetExceeded)
    assert raised.value.__cause__.status_code == 503
    assert isinstance(raised.value.__cause__.__cause__, requests.HTTPError)
    assert current_retry_budget() is None


def test_cancellation_during_individual_wait_returns_none_and_cleans_context():
    response = audio_response(503)
    cancel = threading.Event()

    def interrupt(_delay: float, _event=None) -> bool:
        cancel.set()
        return False

    with (
        patch.object(tts_handler.requests, "post", return_value=response) as post,
        patch.object(tts_handler, "wait_for_retry", side_effect=interrupt),
    ):
        result = tts_handler.text_to_audio(
            "Cancel the retry",
            silero_settings(),
            max_attempts=2,
            cancel_event=cancel,
        )

    assert result is None
    post.assert_called_once()
    assert current_retry_budget() is None


def test_recursive_audio_cpp_lock_wait_is_excluded_from_the_same_budget():
    clock = FakeClock()
    response = audio_response()
    captured_budgets = []

    class LockContext:
        def __enter__(self):
            clock.advance(3.0)

        def __exit__(self, *_args):
            return False

    def request_audio(*_args, **_kwargs):
        budget = current_retry_budget()
        captured_budgets.append(budget)
        return response

    with retry_budget_scope(2, clock=clock) as scope:
        with (
            patch.object(
                tts_handler,
                "audio_cpp_endpoint_lock",
                return_value=LockContext(),
            ),
            patch.object(
                tts_handler,
                "_request_openai_compatible_audio",
                side_effect=request_audio,
            ),
            patch.object(tts_handler, "_decode_audio_response", return_value=object()),
        ):
            result = tts_handler.text_to_audio(
                "Lock wait is outside the budget",
                {"service": "audio_cpp", "model": "fixture-model"},
            )

    assert result is not None
    assert len(captured_budgets) == 1
    assert captured_budgets[0] is scope
    assert captured_budgets[0].started
    assert captured_budgets[0].maximum_elapsed_seconds == 2
    assert captured_budgets[0].remaining_seconds() == 2.0
    assert current_retry_budget() is None


def test_shared_gate_wait_releases_lock_and_rechecks_longer_peer_cooldown():
    clock = FakeClock()
    waiting = threading.Event()
    release = threading.Event()
    waits: list[float] = []

    def waiter(delay: float, _event=None) -> bool:
        waits.append(delay)
        waiting.set()
        assert release.wait(timeout=2)
        clock.advance(delay)
        return True

    gate = TtsRetryGate(clock=clock, waiter=waiter)
    key = make_retry_scope_key(
        provider="provider", endpoint="https://provider.invalid", model="model"
    )
    gate.publish_429(key, 5)

    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(gate.wait_for_admission, key)
        assert waiting.wait(timeout=1)
        publish = pool.submit(gate.publish_429, key, 20)
        assert publish.result(timeout=1) == 2
        release.set()
        admission = pending.result(timeout=2)

    assert admission is not None
    assert clock.now == 20
    assert waits == [5, 15]


def test_parallel_callers_share_cooldown_then_get_staggered_admissions():
    clock = FakeClock()
    first_two_waiters = threading.Barrier(2)
    waiter_lock = threading.Lock()
    initial_wait_count = 0
    waits: list[float] = []

    def waiter(delay: float, _event=None) -> bool:
        nonlocal initial_wait_count
        target = clock() + delay
        with waiter_lock:
            waits.append(delay)
            wait_for_peer = initial_wait_count < 2
            if wait_for_peer:
                initial_wait_count += 1
        if wait_for_peer:
            first_two_waiters.wait(timeout=2)
        clock.advance_to(target)
        return True

    gate = TtsRetryGate(clock=clock, waiter=waiter)
    key = make_retry_scope_key(
        provider="provider", endpoint="https://provider.invalid", model="model"
    )
    gate.publish_429(key, 10)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(gate.wait_for_admission, key) for _ in range(2)]
        admissions = [future.result(timeout=2) for future in futures]

    assert all(admission is not None for admission in admissions)
    assert {admission.generation for admission in admissions if admission} == {1}
    assert clock() == 11
    assert sorted(waits) == [1, 10, 10]


def test_recovery_admissions_stagger_and_current_success_restores_parallelism():
    clock = FakeClock()
    waits: list[float] = []

    def waiter(delay: float, _event=None) -> bool:
        waits.append(delay)
        clock.advance(delay)
        return True

    gate = TtsRetryGate(clock=clock, waiter=waiter)
    key = make_retry_scope_key(
        provider="provider", endpoint="https://provider.invalid", model="model"
    )
    generation = gate.publish_429(key, 5)
    first = gate.wait_for_admission(key)
    second = gate.wait_for_admission(key)
    assert first is not None and second is not None
    assert first.generation == second.generation == generation
    assert waits == [5, 1]

    gate.record_success(first)
    third = gate.wait_for_admission(key)
    assert third is not None
    assert waits == [5, 1]


def test_stale_success_cannot_clear_a_newer_429_generation():
    clock = FakeClock()
    waits: list[float] = []

    def waiter(delay: float, _event=None) -> bool:
        waits.append(delay)
        clock.advance(delay)
        return True

    gate = TtsRetryGate(clock=clock, waiter=waiter)
    key = make_retry_scope_key(
        provider="provider", endpoint="https://provider.invalid", model="model"
    )
    old_admission = gate.wait_for_admission(key)
    assert old_admission is not None
    gate.publish_429(key, 10)
    gate.record_success(old_admission)

    current_admission = gate.wait_for_admission(key)
    assert current_admission is not None
    assert current_admission.generation == old_admission.generation + 1
    assert waits == [10]


def test_final_429_is_published_for_the_next_logical_caller():
    clock = FakeClock()
    gate = TtsRetryGate(
        clock=clock,
        waiter=lambda delay, _event: (clock.advance(delay) or True),
    )
    limited = audio_response(429, retry_after="5")
    succeeded = audio_response()
    calls: list[float] = []

    def post(_url: str, **_kwargs):
        calls.append(clock.now)
        return limited if len(calls) == 1 else succeeded

    with (
        patch.object(tts_handler, "_TTS_RETRY_GATE", gate),
        patch.object(tts_handler.requests, "post", side_effect=post),
        patch.object(tts_handler, "_decode_audio_response", return_value=object()),
    ):
        with retry_budget_scope(20, clock=clock):
            with pytest.raises(tts_handler.TtsGenerationError):
                tts_handler.text_to_audio(
                    "Final 429",
                    silero_settings(retry_timeout_seconds=20),
                    max_attempts=1,
                    silero_base_url="https://silero.invalid",
                )
        with retry_budget_scope(20, clock=clock):
            result = tts_handler.text_to_audio(
                "Peer respects final 429",
                silero_settings(retry_timeout_seconds=20),
                max_attempts=1,
                silero_base_url="https://silero.invalid",
            )

    assert result is not None
    assert calls == [0.0, 5.0]


def test_final_429_without_header_shares_local_backoff_only_with_same_scope():
    clock = FakeClock()
    gate = TtsRetryGate(
        clock=clock,
        waiter=lambda delay, _event: (clock.advance(delay) or True),
    )
    limited = audio_response(429)
    succeeded = audio_response()
    calls: list[tuple[str, float]] = []

    def post(url: str, **_kwargs):
        calls.append((url, clock()))
        return limited if len(calls) == 1 else succeeded

    with (
        patch.object(tts_handler, "_TTS_RETRY_GATE", gate),
        patch.object(tts_handler.requests, "post", side_effect=post),
        patch.object(tts_handler, "_decode_audio_response", return_value=object()),
        patch.object(retry_utils.random, "uniform", side_effect=lambda _low, _high: 0.0),
    ):
        with retry_budget_scope(20, clock=clock):
            with pytest.raises(tts_handler.TtsGenerationError):
                tts_handler.text_to_audio(
                    "Final 429 without Retry-After",
                    silero_settings(
                        retry_timeout_seconds=20,
                        rate_limit_retry_base_delay_seconds=2.0,
                    ),
                    max_attempts=1,
                    silero_base_url="https://silero.invalid",
                )
        with retry_budget_scope(20, clock=clock):
            unrelated = tts_handler.text_to_audio(
                "Unrelated quota scope",
                silero_settings(
                    retry_timeout_seconds=20,
                    rate_limit_retry_base_delay_seconds=2.0,
                ),
                max_attempts=1,
                silero_base_url="https://other-silero.invalid",
            )
        with retry_budget_scope(20, clock=clock):
            same_scope = tts_handler.text_to_audio(
                "Same quota scope",
                silero_settings(
                    retry_timeout_seconds=20,
                    rate_limit_retry_base_delay_seconds=2.0,
                ),
                max_attempts=1,
                silero_base_url="https://silero.invalid",
            )

    assert unrelated is not None and same_scope is not None
    assert [when for _url, when in calls] == [0.0, 0.0, 2.0]


def test_retry_scope_hashes_credentials_and_ignores_voice_text_and_language():
    base = {
        "provider": "example",
        "endpoint": "https://provider.invalid/v1",
        "model": "model-a",
        "account": "top-secret-key",
    }
    first = make_retry_scope_key(**base)
    same_quota = make_retry_scope_key(**base)
    different_endpoint = make_retry_scope_key(**{**base, "endpoint": "https://other.invalid"})
    different_account = make_retry_scope_key(**{**base, "account": "another-key"})
    assert first == same_quota
    assert len(first) == 64
    assert "top-secret-key" not in first
    assert first != different_endpoint
    assert first != different_account

    clock = FakeClock()
    gate_waits: list[float] = []

    def waiter(delay: float, _event=None) -> bool:
        gate_waits.append(delay)
        clock.advance(delay)
        return True

    gate = TtsRetryGate(clock=clock, waiter=waiter)
    gate.publish_429(first, 30)
    assert gate.wait_for_admission(different_endpoint) is not None
    assert gate.wait_for_admission(different_account) is not None
    assert gate_waits == []

    first_settings = silero_settings(speaker="first", language="en")
    second_settings = silero_settings(speaker="second", language="fr")
    first_scope = tts_handler._tts_retry_scope_key(
        first_settings,
        "Silero",
        endpoint_overrides={"silero": "https://silero.invalid"},
    )
    second_scope = tts_handler._tts_retry_scope_key(
        second_settings,
        "Silero",
        endpoint_overrides={"silero": "https://silero.invalid"},
    )
    assert first_scope == second_scope


def test_vertex_retry_scope_shares_quota_by_effective_account_and_project():
    credential = {
        "type": "service_account",
        "client_email": "speech@example.iam.gserviceaccount.com",
        "project_id": "credential-project",
        "private_key": "private-key-material",
    }
    profile = {
        "id": "vertex_ai",
        "provider": "vertex_ai",
        "api_base": "https://aiplatform.googleapis.com",
        "api_key": json.dumps(credential),
        "vertex_project": "billing-project",
        "vertex_location": "global",
        "default_model": "gemini-2.5-flash-tts",
    }
    settings = {
        "service": "Google Vertex AI",
        "model": "gemini-2.5-flash-tts",
        "voice": "voice-one",
        "language": "en",
        "text": "first transcript",
    }

    def scope(settings_for_call, profile_for_call):
        with patch.object(tts_handler, "get_service_config", return_value=profile_for_call):
            return tts_handler._tts_retry_scope_key(
                settings_for_call,
                "Google Vertex AI",
                endpoint_overrides={},
            )

    key = scope(settings, profile)
    parallel_item_key = scope(
        {
            **settings,
            "voice": "voice-two",
            "language": "fr",
            "text": "different transcript",
        },
        profile,
    )
    assert parallel_item_key == key
    assert "private-key-material" not in key
    assert credential["client_email"] not in key

    assert scope(settings, {**profile, "vertex_project": "another-project"}) != key
    assert scope(settings, {**profile, "vertex_location": "us-central1"}) != key
    assert scope(settings, {**profile, "api_base": "https://other.invalid"}) != key
    other_account = {
        **profile,
        "api_key": json.dumps(
            {**credential, "client_email": "another@example.iam.gserviceaccount.com"}
        ),
    }
    assert scope(settings, other_account) != key


def test_shared_gate_wait_is_cancellation_responsive():
    clock = FakeClock()
    cancelled = threading.Event()

    def waiter(_delay: float, _event=None) -> bool:
        cancelled.set()
        return False

    gate = TtsRetryGate(clock=clock, waiter=waiter)
    key = make_retry_scope_key(
        provider="provider", endpoint="https://provider.invalid", model="model"
    )
    gate.publish_429(key, 30)
    assert gate.wait_for_admission(key, cancel_event=cancelled) is None
