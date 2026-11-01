"""Focused checks for application-managed LLM attempt hooks."""

from __future__ import annotations

from threading import Event
from typing import Any, Callable
from unittest.mock import Mock

import pytest

from pandrator.logic import llm_handler


def _response(
    content: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
) -> dict[str, Any]:
    return {
        "id": f"response-{prompt_tokens}",
        "model": "openai/attempt-test",
        "choices": [
            {
                "message": {"content": content},
                "finish_reason": "stop" if content else "length",
            }
        ],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
        "_hidden_params": {"response_cost": cost},
    }


def _install_fake_completion(monkeypatch: Any, completion: Callable[..., Any]) -> None:
    monkeypatch.setattr(
        llm_handler,
        "_get_litellm_clients",
        lambda: (completion, None),
    )
    monkeypatch.setattr(
        llm_handler,
        "_resolve_model_request_details",
        lambda *_args: {
            "model": "openai/attempt-test",
            "request_overrides": {},
            "model_record": None,
        },
    )


def test_before_request_denial_propagates_without_native_call_or_retry(monkeypatch: Any):
    completion = Mock(
        return_value=_response("unused", prompt_tokens=1, completion_tokens=1, cost=0.1)
    )
    _install_fake_completion(monkeypatch, completion)
    wait = Mock(return_value=True)
    monkeypatch.setattr(llm_handler, "wait_for_retry", wait)
    blocker = RuntimeError("fresh authority denied")
    before_calls = 0

    def deny_before_request() -> None:
        nonlocal before_calls
        before_calls += 1
        raise blocker

    with pytest.raises(RuntimeError) as raised:
        llm_handler.chat_completion_with_metadata(
            messages=[{"role": "user", "content": "blocked"}],
            model_name="openai/attempt-test",
            llm_settings={"llm_max_attempts": 3},
            before_request=deny_before_request,
        )

    assert raised.value is blocker
    assert before_calls == 1
    completion.assert_not_called()
    wait.assert_not_called()


def test_empty_response_is_observed_once_before_next_attempt_denial(monkeypatch: Any):
    completion = Mock(return_value=_response("", prompt_tokens=3, completion_tokens=2, cost=0.2))
    _install_fake_completion(monkeypatch, completion)
    wait = Mock(return_value=True)
    retry = Mock()
    monkeypatch.setattr(llm_handler, "wait_for_retry", wait)
    blocker = RuntimeError("second application attempt denied")
    before_calls = 0
    observed: list[tuple[str, dict[str, Any], float | None]] = []

    def before_request() -> None:
        nonlocal before_calls
        before_calls += 1
        if before_calls == 2:
            raise blocker

    def observe(result: llm_handler.ChatCompletionResult) -> None:
        observed.append((result.content, dict(result.usage), result.cost))

    with pytest.raises(RuntimeError) as raised:
        llm_handler.chat_completion_with_metadata(
            messages=[{"role": "user", "content": "retry"}],
            model_name="openai/attempt-test",
            llm_settings={"llm_max_attempts": 3},
            retry_callback=retry,
            before_request=before_request,
            response_callback=observe,
        )

    assert raised.value is blocker
    assert before_calls == 2
    assert completion.call_count == 1
    assert wait.call_count == 1
    assert retry.call_count == 1
    assert len(observed) == 1
    assert observed[0][0] == ""
    assert observed[0][1]["prompt_tokens"] == 3
    assert observed[0][1]["completion_tokens"] == 2
    assert "provider_response_count" not in observed[0][1]
    assert observed[0][2] == 0.2


def test_callbacks_observe_each_response_before_usage_and_cost_accumulate(
    monkeypatch: Any,
):
    completion = Mock(
        side_effect=[
            _response("", prompt_tokens=2, completion_tokens=3, cost=0.25),
            _response("ready", prompt_tokens=4, completion_tokens=5, cost=0.75),
        ]
    )
    _install_fake_completion(monkeypatch, completion)
    wait = Mock(return_value=True)
    monkeypatch.setattr(llm_handler, "wait_for_retry", wait)
    events: list[str] = []
    observed: list[tuple[str, dict[str, Any], float | None]] = []

    def before_request() -> None:
        events.append("before")

    def observe(result: llm_handler.ChatCompletionResult) -> None:
        events.append(f"response:{result.content or 'empty'}")
        observed.append((result.content, dict(result.usage), result.cost))

    result = llm_handler.chat_completion_with_metadata(
        messages=[{"role": "user", "content": "retry then succeed"}],
        model_name="openai/attempt-test",
        llm_settings={"llm_max_attempts": 3},
        before_request=before_request,
        response_callback=observe,
    )

    assert events == ["before", "response:empty", "before", "response:ready"]
    assert completion.call_count == 2
    assert wait.call_count == 1
    assert [item[1]["prompt_tokens"] for item in observed] == [2, 4]
    assert [item[1]["completion_tokens"] for item in observed] == [3, 5]
    assert [item[2] for item in observed] == [0.25, 0.75]
    assert all("provider_response_count" not in item[1] for item in observed)
    assert result.content == "ready"
    assert result.usage["prompt_tokens"] == 6
    assert result.usage["completion_tokens"] == 8
    assert result.usage["provider_response_count"] == 2
    assert result.cost == 1.0


def test_response_callback_retryable_exception_propagates_without_retry(
    monkeypatch: Any,
):
    class RateLimited(RuntimeError):
        status_code = 429

    completion = Mock(
        return_value=_response("ready", prompt_tokens=1, completion_tokens=1, cost=0.1)
    )
    _install_fake_completion(monkeypatch, completion)
    wait = Mock(return_value=True)
    monkeypatch.setattr(llm_handler, "wait_for_retry", wait)
    blocker = RateLimited("observer failure must not become a provider retry")
    before = Mock()

    def fail_observer(_result: llm_handler.ChatCompletionResult) -> None:
        raise blocker

    with pytest.raises(RateLimited) as raised:
        llm_handler.chat_completion_with_metadata(
            messages=[{"role": "user", "content": "one response"}],
            model_name="openai/attempt-test",
            llm_settings={"llm_max_attempts": 3},
            before_request=before,
            response_callback=fail_observer,
        )

    assert raised.value is blocker
    assert before.call_count == 1
    assert completion.call_count == 1
    wait.assert_not_called()


def test_cancellation_before_attempt_skips_both_hooks_and_native_call(monkeypatch: Any):
    completion = Mock(
        return_value=_response("unused", prompt_tokens=1, completion_tokens=1, cost=0.1)
    )
    _install_fake_completion(monkeypatch, completion)
    before = Mock()
    observe = Mock()
    wait = Mock(return_value=True)
    monkeypatch.setattr(llm_handler, "wait_for_retry", wait)
    cancel_event = Event()
    cancel_event.set()

    result = llm_handler.chat_completion_with_metadata(
        messages=[{"role": "user", "content": "cancelled"}],
        model_name="openai/attempt-test",
        cancel_event=cancel_event,
        before_request=before,
        response_callback=observe,
    )

    before.assert_not_called()
    observe.assert_not_called()
    completion.assert_not_called()
    wait.assert_not_called()
    assert result.content == ""
    assert result.model == "openai/attempt-test"
    assert result.usage["provider_response_count"] == 0


def test_omitted_hooks_preserve_empty_response_retry_and_aggregate_result(monkeypatch: Any):
    completion = Mock(
        side_effect=[
            _response("", prompt_tokens=2, completion_tokens=1, cost=0.4),
            _response("legacy-compatible", prompt_tokens=3, completion_tokens=4, cost=0.6),
        ]
    )
    _install_fake_completion(monkeypatch, completion)
    wait = Mock(return_value=True)
    monkeypatch.setattr(llm_handler, "wait_for_retry", wait)

    result = llm_handler.chat_completion_with_metadata(
        messages=[{"role": "user", "content": "without hooks"}],
        model_name="openai/attempt-test",
        llm_settings={"llm_max_attempts": 3},
    )

    assert completion.call_count == 2
    assert wait.call_count == 1
    assert result.content == "legacy-compatible"
    assert result.usage["prompt_tokens"] == 5
    assert result.usage["completion_tokens"] == 5
    assert result.usage["provider_response_count"] == 2
    assert result.cost == 1.0
