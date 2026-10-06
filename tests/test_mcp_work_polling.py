"""Bounded status traffic and timely completion during MCP work waits."""

from types import SimpleNamespace

import pytest

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
