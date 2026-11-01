"""Bulk generation preparation is fresh, guarded, and always cleaned up."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web.accounts.execution_actor import (
    actor_from_principal,
    current_execution_actor,
    execution_actor_scope,
)
from tests.test_translation_project_operations import case as case  # noqa: F401
from tests.test_translation_project_operations import (
    execute,
    jobs,
    preview,
    reviewed_plan,
)


def _generation_preview(case: Any) -> tuple[dict[str, Any], dict[str, Any], str]:
    branch = case.branches[0]
    revision_id, _translated = reviewed_plan(case, branch)
    operation = preview(
        case,
        action="generate",
        branches=[branch],
        key="bulk-generation-preview",
    )
    assert operation["eligible_count"] == 1
    return branch, operation, revision_id


def _stored_preview(case: Any, operation_id: str) -> dict[str, Any]:
    with case.database.session() as db:
        operation = db.get(m.TranslationProjectOperation, operation_id)
        assert operation is not None
        return deepcopy(operation.preview_json)


def _accepted_confirmations(operation: dict[str, Any]) -> list[str]:
    return sorted(
        {
            value
            for child in operation["children"]
            for value in child.get("preview", {}).get("required_confirmations", [])
        }
    )


def test_generation_preview_is_metadata_only_and_json_serializable(case: Any) -> None:
    branch = case.branches[0]
    reviewed_plan(case, branch)
    generation = case.services.generation
    prepare_start = generation.prepare_start
    calls: list[dict[str, Any]] = []

    def prepare_spy(session_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append(dict(kwargs))
        return prepare_start(session_id, **kwargs)

    with patch.object(generation, "prepare_start", side_effect=prepare_spy):
        operation = preview(
            case,
            action="generate",
            branches=[branch],
            key="bulk-generation-preview",
        )

    assert len(calls) == 1
    assert calls[0]["retain_speech_inputs"] is False
    stored = _stored_preview(case, operation["id"])
    capture = stored["captures"][branch["id"]]
    prepared = capture["prepared"]
    assert "_speech_preparation" not in prepared
    json.dumps(stored, sort_keys=True)


def test_generation_child_prepares_fresh_outside_writer_and_cleans_after_commit(
    case: Any,
) -> None:
    branch, operation, revision_id = _generation_preview(case)
    child = operation["children"][0]
    preview_before = _stored_preview(case, operation["id"])
    generation = case.services.generation
    prepare_start = generation.prepare_start
    start_in_session = generation.start_in_session
    cleanup_prepared_start = generation.cleanup_prepared_start
    snapshot_session = case.database.snapshot_session
    immediate_session = case.database.immediate_session
    enqueue_guard = case.operations._enqueue_guard
    actor = actor_from_principal(case.principal)
    sentinel = object()
    state: dict[str, Any] = {
        "writer_depth": 0,
        "snapshot_sessions": set(),
        "guards": [],
        "prepare_kwargs": None,
        "prepared": None,
        "started": None,
        "cleaned": [],
    }

    @contextmanager
    def tracked_snapshot_session():
        with snapshot_session() as db:
            state["snapshot_sessions"].add(id(db))
            yield db

    @contextmanager
    def tracked_immediate_session():
        with immediate_session() as db:
            state["writer_depth"] += 1
            try:
                yield db
            finally:
                state["writer_depth"] -= 1

    def guard_spy(db: Any, *args: Any, **kwargs: Any) -> Any:
        state["guards"].append(
            (id(db), state["writer_depth"], current_execution_actor.get())
        )
        return enqueue_guard(db, *args, **kwargs)

    def prepare_spy(session_id: str, **kwargs: Any) -> dict[str, Any]:
        assert state["writer_depth"] == 0
        assert current_execution_actor.get() == actor
        assert kwargs["speech_plan_revision_id"] == revision_id
        assert callable(kwargs["before_io"])
        state["prepare_kwargs"] = dict(kwargs)
        kwargs["before_io"]()
        prepared = prepare_start(session_id, **kwargs)
        prepared["_test_process_local_sentinel"] = sentinel
        state["prepared"] = prepared
        return prepared

    def start_spy(db: Any, session_id: str, *, prepared: dict[str, Any]) -> dict[str, Any]:
        assert state["writer_depth"] == 1
        assert current_execution_actor.get() == actor
        assert prepared is state["prepared"]
        assert prepared["_test_process_local_sentinel"] is sentinel
        state["started"] = prepared
        return start_in_session(db, session_id, prepared=prepared)

    def cleanup_spy(prepared: dict[str, Any]) -> None:
        assert state["writer_depth"] == 0
        state["cleaned"].append(prepared)
        cleanup_prepared_start(prepared)

    with (
        patch.object(case.database, "snapshot_session", new=tracked_snapshot_session),
        patch.object(case.database, "immediate_session", new=tracked_immediate_session),
        patch.object(case.operations, "_enqueue_guard", side_effect=guard_spy),
        patch.object(generation, "prepare_start", side_effect=prepare_spy),
        patch.object(generation, "start_in_session", side_effect=start_spy),
        patch.object(generation, "cleanup_prepared_start", side_effect=cleanup_spy),
        execution_actor_scope(None),
    ):
        result = case.operations._start_child(
            operation["id"],
            child,
            case.principal,
            case.target,
            _accepted_confirmations(operation),
        )
        assert current_execution_actor.get() is None

    assert result["generation_run_id"]
    assert state["prepare_kwargs"] is not None
    assert state["prepare_kwargs"].get("retain_speech_inputs", True) is True
    assert state["started"] is state["prepared"]
    assert state["cleaned"] == [state["prepared"]]
    assert state["guards"]
    assert any(
        session_id in state["snapshot_sessions"]
        and writer_depth == 0
        and seen_actor == actor
        for session_id, writer_depth, seen_actor in state["guards"]
    )
    assert not any(
        key == "_test_process_local_sentinel"
        for key in preview_before["captures"][branch["id"]]["prepared"]
    )
    assert _stored_preview(case, operation["id"]) == preview_before
    assert len(jobs(case)) == 1


@pytest.mark.parametrize(
    "field, drifted",
    [
        ("snapshot_input_hash", "changed-input-hash"),
        ("snapshot_guard", "changed-guard"),
        ("snapshot_source_run_id", "changed-source-run"),
        ("speech_plan_revision_id", "changed-revision"),
        ("settings_source_identity", {"changed": True}),
    ],
)
def test_generation_capture_drift_blocks_child_and_cleans_preparation(
    case: Any, field: str, drifted: Any
) -> None:
    _branch, operation, _revision_id = _generation_preview(case)
    generation = case.services.generation
    prepare_start = generation.prepare_start
    cleanup_prepared_start = generation.cleanup_prepared_start
    prepared_results: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []

    def prepare_spy(session_id: str, **kwargs: Any) -> dict[str, Any]:
        prepared = prepare_start(session_id, **kwargs)
        prepared[field] = drifted
        prepared_results.append(prepared)
        return prepared

    def cleanup_spy(prepared: dict[str, Any]) -> None:
        cleaned.append(prepared)
        cleanup_prepared_start(prepared)

    with (
        patch.object(generation, "prepare_start", side_effect=prepare_spy),
        patch.object(generation, "cleanup_prepared_start", side_effect=cleanup_spy),
    ):
        result = execute(case, operation, key=f"bulk-drift-{field}")

    assert result["children"][0]["state"] == "blocked"
    assert len(prepared_results) == 1
    assert cleaned == prepared_results
    assert jobs(case) == []


def test_receipt_replay_skips_or_cleans_losing_fresh_preparation(case: Any) -> None:
    _branch, operation, _revision_id = _generation_preview(case)
    child = operation["children"][0]
    generation = case.services.generation
    first = case.operations._start_child(
        operation["id"],
        child,
        case.principal,
        case.target,
        _accepted_confirmations(operation),
    )
    job_count = len(jobs(case))
    assert job_count == 1

    with patch.object(
        generation,
        "prepare_start",
        side_effect=AssertionError("an ordinary receipt replay must return before preparation"),
    ):
        ordinary_replay = case.operations._start_child(
            operation["id"],
            child,
            case.principal,
            case.target,
            _accepted_confirmations(operation),
        )
    assert ordinary_replay == first

    prepare_start = generation.prepare_start
    cleanup_prepared_start = generation.cleanup_prepared_start
    losing_preparations: list[dict[str, Any]] = []
    cleaned: list[dict[str, Any]] = []

    def prepare_spy(session_id: str, **kwargs: Any) -> dict[str, Any]:
        prepared = prepare_start(session_id, **kwargs)
        prepared["snapshot_guard"] = "drift-after-concurrent-winner"
        losing_preparations.append(prepared)
        return prepared

    def cleanup_spy(prepared: dict[str, Any]) -> None:
        cleaned.append(prepared)
        cleanup_prepared_start(prepared)

    with (
        patch.object(case.operations, "_receipt", return_value=None),
        patch.object(generation, "prepare_start", side_effect=prepare_spy),
        patch.object(generation, "cleanup_prepared_start", side_effect=cleanup_spy),
    ):
        concurrent_winner = case.operations._start_child(
            operation["id"],
            child,
            case.principal,
            case.target,
            _accepted_confirmations(operation),
        )

    assert concurrent_winner == first
    assert len(losing_preparations) == 1
    assert cleaned == losing_preparations
    assert len(jobs(case)) == job_count


@pytest.mark.parametrize("error_type", [ValueError, KeyError])
def test_preparation_failure_rechecks_committed_receipt_without_writer(
    case: Any, error_type: type[Exception]
) -> None:
    _branch, operation, _revision_id = _generation_preview(case)
    child = operation["children"][0]
    first = case.operations._start_child(
        operation["id"],
        child,
        case.principal,
        case.target,
        _accepted_confirmations(operation),
    )
    original_receipt = case.operations._receipt
    receipt_reads = 0
    preparation_calls = 0

    def stale_initial_receipt(db: Any, stored_operation: Any, stored_child: Any) -> Any:
        nonlocal receipt_reads
        receipt_reads += 1
        if receipt_reads == 1:
            return {}
        return original_receipt(db, stored_operation, stored_child)

    def preparation_lost_to_winner(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        nonlocal preparation_calls
        preparation_calls += 1
        raise error_type("prepared source changed after a concurrent child committed")

    with (
        patch.object(case.operations, "_receipt", side_effect=stale_initial_receipt),
        patch.object(
            case.services.generation,
            "prepare_start",
            side_effect=preparation_lost_to_winner,
        ),
        patch.object(
            case.database,
            "immediate_session",
            side_effect=AssertionError("receipt recovery must use a snapshot read"),
        ),
    ):
        recovered = case.operations._start_child(
            operation["id"],
            child,
            case.principal,
            case.target,
            _accepted_confirmations(operation),
        )

    assert recovered == first
    assert receipt_reads == 2
    assert preparation_calls == 1
    assert len(jobs(case)) == 1


def test_rollback_after_start_cleans_preparation_after_writer_exits(case: Any) -> None:
    _branch, operation, _revision_id = _generation_preview(case)
    child = operation["children"][0]
    generation = case.services.generation
    cleanup_prepared_start = generation.cleanup_prepared_start
    immediate_session = case.database.immediate_session
    state: dict[str, Any] = {"writer_depth": 0, "cleaned": [], "prepared": None}

    @contextmanager
    def tracked_immediate_session():
        with immediate_session() as db:
            state["writer_depth"] += 1
            try:
                yield db
            finally:
                state["writer_depth"] -= 1

    prepare_start = generation.prepare_start

    def prepare_spy(session_id: str, **kwargs: Any) -> dict[str, Any]:
        prepared = prepare_start(session_id, **kwargs)
        state["prepared"] = prepared
        return prepared

    def fail_child_receipt(
        db: Any,
        reservation: Any,
        **kwargs: Any,
    ) -> None:
        assert reservation.record.operation_id == "executeProjectGenerationChild"
        assert kwargs["status_code"] == 202
        raise RuntimeError("injected post-publication receipt failure")

    def cleanup_spy(prepared: dict[str, Any]) -> None:
        assert state["writer_depth"] == 0
        state["cleaned"].append(prepared)
        cleanup_prepared_start(prepared)

    with (
        patch.object(case.database, "immediate_session", new=tracked_immediate_session),
        patch.object(generation, "prepare_start", side_effect=prepare_spy),
        patch.object(case.services.idempotency, "complete", side_effect=fail_child_receipt),
        patch.object(generation, "cleanup_prepared_start", side_effect=cleanup_spy),
        pytest.raises(RuntimeError, match="post-publication receipt failure"),
    ):
        case.operations._start_child(
            operation["id"],
            child,
            case.principal,
            case.target,
            _accepted_confirmations(operation),
        )

    assert state["prepared"] is not None
    assert state["cleaned"] == [state["prepared"]]
    assert jobs(case) == []
    with case.database.session() as db:
        assert db.scalar(
            select(m.GenerationRun.id).where(
                m.GenerationRun.session_id == child["session_id"]
            )
        ) is None
