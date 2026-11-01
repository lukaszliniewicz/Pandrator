from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Barrier, Event, Lock
from types import SimpleNamespace
from typing import Any, cast

import pytest
from sqlalchemy.orm import Session

from pandrator.logic.llm_handler import ChatCompletionResult
from pandrator.web import speech_planning, speech_structure_analysis, tts_optimization
from pandrator.web.accounts.execution_actor import (
    HostActor,
    current_execution_actor,
    execution_actor_scope,
)
from pandrator.web.job_execution import JobLease, current_job_lease
from pandrator.web.speech_structure_analysis import annotate_speech_units
from pandrator.web.tts_optimization import optimization_unit_key, optimize_texts


@dataclass
class _Unit:
    index: int


class _FakeDatabase:
    def __init__(self) -> None:
        self.units: list[_Unit] = []

    @contextmanager
    def immediate_session(self) -> Iterator[Session]:
        unit = _Unit(len(self.units))
        self.units.append(unit)
        yield cast(Session, unit)


def _install_annotation_units(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[_FakeDatabase, list[tuple[str, _Unit]]]:
    database = _FakeDatabase()
    operations: list[tuple[str, _Unit]] = []

    def read_controls(session: Session, _session_id: str) -> dict[str, Any]:
        operations.append(("read", cast(_Unit, session)))
        return {"revision": 0, "characters": []}

    def merge_proposals(
        session: Session,
        _session_id: str,
        _proposals: list[dict[str, Any]],
        *,
        origin: str,
    ) -> dict[str, Any]:
        assert origin == "speech-structure-analysis"
        operations.append(("merge", cast(_Unit, session)))
        return {"characters": []}

    monkeypatch.setattr(speech_structure_analysis, "get_generation_controls", read_controls)
    monkeypatch.setattr(speech_structure_analysis, "merge_character_proposals", merge_proposals)
    return database, operations


def _annotation_response(xml: str, *, cost: float = 0.01) -> ChatCompletionResult:
    return ChatCompletionResult(
        content=json.dumps(
            {
                "items": [{"unit_id": 1, "speech_xml": xml}],
                "character_proposals": [],
            }
        ),
        cost=cost,
        usage={"prompt_tokens": 2, "completion_tokens": 1},
        cost_source="synthetic",
    )


def _single_plan(messages: list[dict[str, str]]) -> ChatCompletionResult:
    payload = json.loads(messages[1]["content"].partition("Plan this single speech sentence:\n")[2])
    return ChatCompletionResult(
        content=json.dumps(
            {
                "case_id": payload["case_id"],
                "decisions": [],
                "discoveries": [],
                "prosody": [],
            }
        ),
        cost=0.01,
        usage={"prompt_tokens": 1, "completion_tokens": 1},
    )


def test_none_ports_keep_legacy_keywords_for_optimizer_planners_and_annotation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = threading.Event()
    ordinary_calls: list[dict[str, Any]] = []

    def legacy_complete(**kwargs: Any) -> ChatCompletionResult:
        ordinary_calls.append(kwargs)
        payload = json.loads(kwargs["messages"][1]["content"].partition("Input JSON:\n")[2])
        return ChatCompletionResult(
            content=json.dumps(
                {
                    "items": [
                        {"index": item["index"], "text": item["text"]} for item in payload["items"]
                    ]
                }
            )
        )

    monkeypatch.setattr(tts_optimization, "chat_completion_with_metadata", legacy_complete)
    optimize_texts(
        ["A sentence."],
        {"llm_tts_batch_size": 1, "llm_concurrent_calls": 1},
        {},
        "synthetic/model",
        event,
        lambda *_args: None,
    )
    assert len(ordinary_calls) == 1
    assert set(ordinary_calls[0]) == {"messages", "model_name", "llm_settings", "cancel_event"}
    assert ordinary_calls[0]["cancel_event"] is event

    single_calls: list[dict[str, Any]] = []
    batch_calls: list[dict[str, Any]] = []

    def single_plan(*args: Any, **kwargs: Any) -> SimpleNamespace:
        single_calls.append(kwargs)
        return SimpleNamespace(text=args[0], plan={}, responses=[])

    def batch_plan(cases: list[dict[str, Any]], **kwargs: Any) -> SimpleNamespace:
        batch_calls.append(kwargs)
        results = [SimpleNamespace(text=case["text"], plan={}, responses=[]) for case in cases]
        return SimpleNamespace(results=results, responses=[])

    monkeypatch.setattr(speech_planning, "plan_speech_text", single_plan)
    monkeypatch.setattr(speech_planning, "plan_speech_text_batch", batch_plan)
    optimize_texts(
        ["A sentence."],
        {"speech_optimization_mode": "guarded", "llm_tts_batch_size": 1},
        {},
        "synthetic/model",
        event,
        lambda *_args: None,
    )
    optimize_texts(
        ["One sentence.", "Another sentence."],
        {"speech_optimization_mode": "guarded", "llm_tts_batch_size": 2},
        {},
        "synthetic/model",
        event,
        lambda *_args: None,
    )
    assert len(single_calls) == 1
    assert len(batch_calls) == 1
    assert all("completion_func" not in call for call in (*single_calls, *batch_calls))

    database, _operations = _install_annotation_units(monkeypatch)
    annotation_calls: list[dict[str, Any]] = []

    def legacy_annotation(**kwargs: Any) -> ChatCompletionResult:
        annotation_calls.append(kwargs)
        return _annotation_response('<segment id="1">Hello</segment>')

    monkeypatch.setattr(
        speech_structure_analysis,
        "chat_completion_with_metadata",
        legacy_annotation,
    )
    annotate_speech_units(
        database,
        "session",
        ["Hello"],
        mode="dialogue",
        llm_settings={},
        model_name="synthetic/model",
        cancel_event=event,
    )
    assert len(annotation_calls) == 1
    assert set(annotation_calls[0]) == {"messages", "model_name", "llm_settings", "cancel_event"}
    assert annotation_calls[0]["cancel_event"] is event


@pytest.mark.parametrize(
    ("case", "texts", "batch_size"),
    [
        ("legacy", ["First sentence.", "Second sentence."], 1),
        ("guarded-single", ["First sentence.", "Second sentence."], 1),
        (
            "guarded-batch",
            ["First sentence.", "Second sentence.", "Third sentence.", "Fourth sentence."],
            2,
        ),
    ],
)
def test_custom_optimizer_tasks_receive_distinct_caller_contexts(
    case: str,
    texts: list[str],
    batch_size: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    actor = HostActor("pandrator-cli")
    lease: JobLease = {
        "job_id": "job-ports",
        "owner_id": "worker-ports",
        "lease_generation": 7,
    }
    prior_actor = current_execution_actor.get()
    prior_lease = current_job_lease.get()
    barrier = Barrier(2)
    mutated = Event()
    marker_lock = Lock()
    seen: list[tuple[object, object, int]] = []

    def forbidden_completion(**_kwargs: Any) -> ChatCompletionResult:
        pytest.fail("A custom completion port must not use the module fallback.")

    monkeypatch.setattr(tts_optimization, "chat_completion_with_metadata", forbidden_completion)
    monkeypatch.setattr(speech_planning, "chat_completion_with_metadata", forbidden_completion)

    def complete(**kwargs: Any) -> ChatCompletionResult:
        assert set(kwargs) == {"messages", "model_name", "llm_settings"}
        messages = kwargs["messages"]
        actor_in_task = current_execution_actor.get()
        lease_in_task = current_job_lease.get()
        with marker_lock:
            ordinal = len(seen)
            seen.append((actor_in_task, lease_in_task, threading.get_ident()))
        barrier.wait(timeout=5)
        if ordinal == 0:
            current_execution_actor.set(None)
            current_job_lease.set(None)
            mutated.set()
        else:
            assert mutated.wait(timeout=5)
            assert actor_in_task is actor
            assert lease_in_task is lease

        if case == "legacy":
            payload = json.loads(messages[1]["content"].partition("Input JSON:\n")[2])
            content = {
                "items": [
                    {"index": item["index"], "text": item["text"]} for item in payload["items"]
                ]
            }
        elif messages[1]["content"].startswith("Plan these speech units:\n"):
            payload = json.loads(messages[1]["content"].partition("Plan these speech units:\n")[2])
            content = {
                "items": [
                    {
                        "batch_item_id": item["batch_item_id"],
                        "plan": {
                            "case_id": item["case"]["case_id"],
                            "decisions": [],
                            "discoveries": [],
                            "prosody": [],
                        },
                    }
                    for item in payload["items"]
                ]
            }
        else:
            payload = json.loads(
                messages[1]["content"].partition("Plan this single speech sentence:\n")[2]
            )
            content = {
                "case_id": payload["case_id"],
                "decisions": [],
                "discoveries": [],
                "prosody": [],
            }
        return ChatCompletionResult(
            content=json.dumps(content), cost=0.01, usage={"prompt_tokens": 1}
        )

    settings: dict[str, Any] = {
        "llm_concurrent_calls": 2,
        "llm_tts_batch_size": batch_size,
    }
    if case != "legacy":
        settings["speech_optimization_mode"] = "guarded"
    with execution_actor_scope(actor):
        lease_token = current_job_lease.set(lease)
        try:
            output, usage = optimize_texts(
                texts,
                settings,
                {},
                "synthetic/model",
                threading.Event(),
                lambda *_args: None,
                completion_func=complete,
            )
            assert current_execution_actor.get() is actor
            assert current_job_lease.get() is lease
        finally:
            current_job_lease.reset(lease_token)
    assert current_execution_actor.get() is prior_actor
    assert current_job_lease.get() is prior_lease
    assert output == texts
    assert usage.response_count == 2
    assert len(seen) == 2
    assert len({thread_id for _actor, _lease, thread_id in seen}) == 2
    assert all(
        actor_seen is actor and lease_seen is lease for actor_seen, lease_seen, _thread in seen
    )


def test_custom_legacy_retry_keeps_order_usage_and_checkpoints(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    checkpoints: dict[str, dict[str, Any]] = {}

    def complete(**kwargs: Any) -> ChatCompletionResult:
        assert set(kwargs) == {"messages", "model_name", "llm_settings"}
        calls.append(kwargs)
        payload = json.loads(kwargs["messages"][1]["content"].partition("Input JSON:\n")[2])
        if len(calls) == 1:
            content = {"items": [{"index": payload["items"][0]["index"], "text": "first"}]}
        else:
            content = {
                "items": [
                    {"index": item["index"], "text": f"{item['text']} revised"}
                    for item in payload["items"]
                ]
            }
        return ChatCompletionResult(
            content=json.dumps(content),
            cost=0.01 * len(calls),
            usage={"prompt_tokens": len(calls), "completion_tokens": 1},
        )

    monkeypatch.setattr(
        tts_optimization,
        "chat_completion_with_metadata",
        lambda **_kwargs: pytest.fail("The custom completion port must handle retries."),
    )
    output, usage = optimize_texts(
        ["one", "", "two", "three"],
        {
            "llm_tts_batch_size": 2,
            "llm_concurrent_calls": 1,
            "tts_structured_max_attempts": 2,
        },
        {},
        "synthetic/model",
        threading.Event(),
        lambda *_args: None,
        completion_func=complete,
        on_unit_completed=lambda key, payload: checkpoints.__setitem__(key, payload),
    )

    assert output == ["one revised", "", "two revised", "three revised"]
    assert len(calls) == 3
    assert usage.response_count == 3
    assert usage.cost == pytest.approx(0.06)
    assert usage.usage == {"prompt_tokens": 6, "completion_tokens": 3}
    assert len(checkpoints) == 2
    assert checkpoints[optimization_unit_key([0, 2], stage=0)]["response_count"] == 2
    assert checkpoints[optimization_unit_key([3], stage=0)]["response_count"] == 1


def test_custom_planner_port_handles_validation_retry_and_checkpoint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, Any]] = []
    checkpoints: dict[str, dict[str, Any]] = {}

    def complete(**kwargs: Any) -> ChatCompletionResult:
        assert set(kwargs) == {"messages", "model_name", "llm_settings"}
        calls.append(kwargs)
        messages = kwargs["messages"]
        content = messages[1]["content"]
        if content.startswith("Plan this single speech sentence:\n"):
            if len(calls) == 1:
                return ChatCompletionResult(
                    content="{malformed",
                    cost=0.01,
                    usage={"prompt_tokens": 1},
                )
            return _single_plan(messages)
        return ChatCompletionResult(content="{}")

    monkeypatch.setattr(
        tts_optimization,
        "chat_completion_with_metadata",
        lambda **_kwargs: pytest.fail("The custom planner port must handle retries."),
    )
    monkeypatch.setattr(
        speech_planning,
        "chat_completion_with_metadata",
        lambda **_kwargs: pytest.fail("The planner must not use its module fallback."),
    )
    output, usage = optimize_texts(
        ["First sentence.", "Second sentence."],
        {
            "speech_optimization_mode": "guarded",
            "llm_tts_batch_size": 1,
            "llm_concurrent_calls": 1,
            "speech_plan_structured_max_attempts": 2,
        },
        {},
        "synthetic/model",
        threading.Event(),
        lambda *_args: None,
        completion_func=complete,
        on_unit_completed=lambda key, payload: checkpoints.__setitem__(key, payload),
    )

    assert output == ["First sentence.", "Second sentence."]
    assert len(calls) == 3
    assert usage.response_count == 3
    assert usage.cost == pytest.approx(0.03)
    assert checkpoints[optimization_unit_key([0], stage=0)]["response_count"] == 2
    assert checkpoints[optimization_unit_key([1], stage=0)]["response_count"] == 1


def test_custom_annotation_port_retries_and_preserves_usage_callbacks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _operations = _install_annotation_units(monkeypatch)
    responses = [
        _annotation_response("<broken", cost=0.02),
        _annotation_response('<segment id="1">Hello</segment>', cost=0.03),
    ]
    calls: list[dict[str, Any]] = []
    usage_events: list[ChatCompletionResult] = []

    def complete(**kwargs: Any) -> ChatCompletionResult:
        assert set(kwargs) == {"messages", "model_name", "llm_settings"}
        calls.append(kwargs)
        return responses[len(calls) - 1]

    monkeypatch.setattr(
        speech_structure_analysis,
        "chat_completion_with_metadata",
        lambda **_kwargs: pytest.fail("The custom annotation port must be used."),
    )
    result = annotate_speech_units(
        database,
        "session",
        ["Hello"],
        mode="dialogue",
        llm_settings={},
        model_name="synthetic/model",
        cancel_event=threading.Event(),
        completion_func=complete,
        on_usage=usage_events.append,
    )

    assert result == ['<segment id="1">Hello</segment>']
    assert len(calls) == 2
    assert usage_events == responses
    assert usage_events[0] is responses[0]
    assert usage_events[1] is responses[1]
    assert "malformed XML" in calls[1]["messages"][0]["content"]


def test_data_guard_precedes_normalization_controls_and_commit_on_same_units(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, operations = _install_annotation_units(monkeypatch)
    source_markup = {"1": '<segment id="author-guid"><dialogue>Hello</dialogue></segment>'}

    def guard(session: Session) -> None:
        operations.append(("guard", cast(_Unit, session)))

    result = annotate_speech_units(
        database,
        "session",
        ["Hello"],
        mode="dialogue",
        llm_settings={},
        model_name="synthetic/model",
        cancel_event=threading.Event(),
        source_markup=source_markup,
        completion_func=lambda **_kwargs: _annotation_response(
            '<segment id="1"><dialogue>Hello</dialogue></segment>'
        ),
        before_data_access=guard,
    )

    assert result == ['<segment id="1"><dialogue>Hello</dialogue></segment>']
    assert [name for name, _unit in operations] == [
        "guard",
        "read",
        "guard",
        "read",
        "guard",
        "merge",
    ]
    assert len(database.units) == 3
    for guard_operation, data_operation, unit in zip(
        operations[::2], operations[1::2], database.units, strict=True
    ):
        assert guard_operation[1] is unit
        assert data_operation[1] is unit


def test_guard_denial_before_controls_stops_model_and_effects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, operations = _install_annotation_units(monkeypatch)
    denial = ValueError("access guard denied")

    def guard(session: Session) -> None:
        operations.append(("guard", cast(_Unit, session)))
        raise denial

    def forbidden(**_kwargs: Any) -> ChatCompletionResult:
        pytest.fail("No model request may follow a denied data guard.")

    monkeypatch.setattr(speech_structure_analysis, "chat_completion_with_metadata", forbidden)
    with pytest.raises(ValueError) as raised:
        annotate_speech_units(
            database,
            "session",
            ["Hello"],
            mode="dialogue",
            llm_settings={},
            model_name="synthetic/model",
            cancel_event=threading.Event(),
            completion_func=forbidden,
            before_data_access=guard,
        )

    assert raised.value is denial
    assert [name for name, _unit in operations] == ["guard"]
    assert len(database.units) == 1


def test_commit_guard_exception_identity_is_not_retried_or_merged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, operations = _install_annotation_units(monkeypatch)
    denial = ValueError("commit guard denied")
    guard_count = 0
    calls = 0

    def guard(session: Session) -> None:
        nonlocal guard_count
        guard_count += 1
        operations.append(("guard", cast(_Unit, session)))
        if guard_count == 2:
            raise denial

    def complete(**kwargs: Any) -> ChatCompletionResult:
        nonlocal calls
        calls += 1
        assert set(kwargs) == {"messages", "model_name", "llm_settings"}
        return _annotation_response('<segment id="1">Hello</segment>')

    with pytest.raises(ValueError) as raised:
        annotate_speech_units(
            database,
            "session",
            ["Hello"],
            mode="dialogue",
            llm_settings={},
            model_name="synthetic/model",
            cancel_event=threading.Event(),
            completion_func=complete,
            before_data_access=guard,
        )

    assert raised.value is denial
    assert calls == 1
    assert guard_count == 2
    assert [name for name, _unit in operations] == ["guard", "read", "guard"]
    assert len(database.units) == 2


def test_off_and_empty_annotation_remain_request_free_and_preserve_markup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database, _operations = _install_annotation_units(monkeypatch)

    def forbidden(**_kwargs: Any) -> ChatCompletionResult:
        pytest.fail("Off and empty annotation must not request a model completion.")

    monkeypatch.setattr(speech_structure_analysis, "chat_completion_with_metadata", forbidden)
    authored = '<segment id="author-guid"><span><ins>calm</ins>Hello</span></segment>'
    result = annotate_speech_units(
        database,
        "session",
        ["Hello"],
        mode="off",
        llm_settings={},
        model_name="synthetic/model",
        cancel_event=threading.Event(),
        source_markup={"1": authored},
        completion_func=forbidden,
    )
    assert result == ['<segment id="1"><span><ins>calm</ins>Hello</span></segment>']
    units_after_off = len(database.units)

    empty = annotate_speech_units(
        database,
        "session",
        [],
        mode="off",
        llm_settings={},
        model_name="synthetic/model",
        cancel_event=threading.Event(),
        completion_func=forbidden,
        before_data_access=lambda _session: pytest.fail(
            "Empty annotation must return before opening a data unit."
        ),
    )
    assert empty == []
    assert len(database.units) == units_after_off
