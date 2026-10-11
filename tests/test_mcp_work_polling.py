"""Bounded status traffic and timely completion during MCP work waits."""

from types import SimpleNamespace

import pytest

from pandrator_mcp.errors import PandratorMcpError
from pandrator_mcp.schemas.work import GetWorkInput
from pandrator_mcp.tools import work as work_tools


def polling_runtime(monkeypatch, state_at):
    elapsed = [0.0]
    calls = []
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    def inspect(_work_id):
        calls.append(elapsed[0])
        state, progress = state_at(elapsed[0])
        return {
            "id": "work-1", "state": state, "progress": progress,
            "detail": "Generating audio", "poll_after_ms": 750,
        }

    monkeypatch.setattr(work_tools.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(work_tools.time, "sleep", sleep)
    runtime = SimpleNamespace(
        require_application=lambda: SimpleNamespace(get_work=inspect),
        manager=SimpleNamespace(operation=inspect), application=None,
    )
    return runtime, calls, sleeps, elapsed


@pytest.mark.parametrize("kind", ["job", "manager_operation"])
def test_unchanged_wait_bounds_reads_and_obeys_deadline(monkeypatch, kind):
    runtime, calls, sleeps, elapsed = polling_runtime(
        monkeypatch, lambda _time: ("running", 0.5),
    )
    outcome = work_tools.get_work(
        runtime, GetWorkInput(work_id="work-1", work_type=kind, wait_seconds=25),
    )
    assert outcome.work.state == "running"
    assert outcome.result["wait"]["timed_out"] is True
    assert elapsed[0] == 25
    assert len(calls) <= 7
    assert max(sleeps) <= 10
    assert outcome.next_actions[0].arguments["work_id"] == "work-1"


def test_progress_change_resets_backoff_and_reports_terminal_completion(monkeypatch):
    def state_at(elapsed):
        if elapsed >= 7:
            return "succeeded", 1.0
        return "running", 0.5 if elapsed >= 4 else 0.0

    runtime, _calls, _sleeps, elapsed = polling_runtime(monkeypatch, state_at)
    outcome = work_tools.get_work(runtime, GetWorkInput(work_id="work-1", wait_seconds=25))
    assert outcome.work.state == "succeeded"
    assert elapsed[0] <= 8
    assert outcome.result["wait"]["timed_out"] is False
    assert not outcome.next_actions


@pytest.mark.parametrize("child_state", ["running", "succeeded"])
def test_generation_admission_wait_keeps_parent_receipt_and_reads_child_once(
    monkeypatch, child_state
):
    elapsed = [0.0]
    calls = []
    parent_reads = [
        {
            "id": "admission-job-1",
            "kind": "generation.admit",
            "state": "running",
            "progress": 0.2,
            "poll_after_ms": 750,
        },
        {
            "id": "admission-job-1",
            "kind": "generation.admit",
            "state": "succeeded",
            "progress": 1.0,
            "poll_after_ms": 0,
            "result_summary": {
                "generation_run_id": "run-1",
                "job_id": "generation-job-1",
                "session_id": "session-1",
                "status": "queued",
                "stage": "generating",
                "speech_plan_revision_id": "revision-1",
            },
        },
    ]
    child = {
        "id": "generation-job-1",
        "kind": "generation.run",
        "state": child_state,
        "progress": 1.0 if child_state == "succeeded" else 0.35,
        "detail": "Synthesis",
        "cancellable": child_state != "succeeded",
        "poll_after_ms": 0 if child_state == "succeeded" else 750,
    }

    def get_work(work_id):
        calls.append(work_id)
        if work_id == "admission-job-1":
            return parent_reads.pop(0)
        assert work_id == "generation-job-1"
        return child

    monkeypatch.setattr(work_tools.time, "monotonic", lambda: elapsed[0])

    def sleep(seconds):
        elapsed[0] += seconds

    monkeypatch.setattr(work_tools.time, "sleep", sleep)
    runtime = SimpleNamespace(
        require_application=lambda: SimpleNamespace(get_work=get_work),
        manager=SimpleNamespace(operation=get_work),
        application=None,
    )

    outcome = work_tools.get_work(
        runtime,
        GetWorkInput(work_id="admission-job-1", wait_seconds=5),
    )

    assert calls == ["admission-job-1", "admission-job-1", "generation-job-1"]
    assert outcome.work.id == "admission-job-1"
    assert outcome.work.state == "succeeded"
    assert outcome.result["id"] == "admission-job-1"
    assert outcome.result["state"] == "succeeded"
    assert outcome.result["wait"]["poll_count"] == 1
    assert outcome.result["wait"]["timed_out"] is False
    assert outcome.result["generation_run_id"] == "run-1"
    assert outcome.result["followup_work"] == {
        "type": "job",
        "id": "generation-job-1",
        "state": child_state,
        "progress": child["progress"],
        "detail": child["detail"],
        "cancellable": child["cancellable"],
        "poll_after_ms": child["poll_after_ms"],
    }
    assert outcome.next_actions[0].tool == "pandrator_get_work"
    assert outcome.next_actions[0].arguments == {"work_id": "generation-job-1"}


def test_generation_admission_child_lookup_failure_is_marked_pending():
    calls = []
    marker = {
        "generation_run_id": "run-1",
        "job_id": "generation-job-1",
        "session_id": "session-1",
        "status": "queued",
        "stage": "generating",
        "speech_plan_revision_id": "revision-1",
    }

    def get_work(work_id):
        calls.append(work_id)
        if work_id == "admission-job-1":
            return {
                "id": work_id,
                "kind": "generation.admit",
                "state": "succeeded",
                "progress": 1.0,
                "result_summary": marker,
            }
        raise PandratorMcpError(
            "application_unavailable",
            "The downstream application is unavailable.",
            retryable=True,
        )

    runtime = SimpleNamespace(
        require_application=lambda: SimpleNamespace(get_work=get_work),
        manager=SimpleNamespace(operation=get_work),
        application=None,
    )

    outcome = work_tools.get_work(runtime, GetWorkInput(work_id="admission-job-1"))

    assert calls == ["admission-job-1", "generation-job-1"]
    assert outcome.work.id == "admission-job-1"
    assert outcome.work.state == "succeeded"
    assert outcome.result["followup_work"] == {
        "type": "job",
        "id": "generation-job-1",
        "state": "queued",
        "progress": None,
        "detail": None,
        "cancellable": True,
        "poll_after_ms": 1000,
    }
    assert outcome.result["followup_work_lookup_pending"] is True
    assert outcome.result["generation_run_id"] == "run-1"
    assert outcome.result["wait"]["poll_count"] == 0
    assert outcome.next_actions[0].arguments == {"work_id": "generation-job-1"}
