"""Every server-side wait stops at the configured ceiling and says so."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pandrator_mcp.schemas.generation_operations import GetGenerationOperationInput
from pandrator_mcp.schemas.media_edit import ProposeMediaEditArguments
from pandrator_mcp.schemas.transcription import GetTranscriptionInput
from pandrator_mcp.schemas.work import GetWorkInput
from pandrator_mcp.settings import McpSettings
from pandrator_mcp.tools import generation_operations, transcription, work
from pandrator_mcp.tools.media_edit import propose_media_edit
from pandrator_mcp.waits import (
    bounded_suggestion,
    effective_wait_seconds,
    wait_ceiling,
    wait_receipt,
)


def settings_runtime(application, ceiling: int | None = 25):
    return SimpleNamespace(
        require_application=lambda: application,
        manager=SimpleNamespace(operation=getattr(application, "get_work", None)),
        application=None,
        settings=(
            SimpleNamespace(maximum_wait_seconds=ceiling)
            if ceiling is not None
            else SimpleNamespace()
        ),
    )


def clock(monkeypatch, module):
    elapsed = [0.0]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        elapsed[0] += seconds

    monkeypatch.setattr(module.time, "monotonic", lambda: elapsed[0])
    monkeypatch.setattr(module.time, "sleep", sleep)
    return elapsed, sleeps


def test_ceiling_defaults_from_the_environment(monkeypatch):
    monkeypatch.delenv("PANDRATOR_MCP_MAX_WAIT_SECONDS", raising=False)
    assert McpSettings(target_name="local").maximum_wait_seconds == 25
    monkeypatch.setenv("PANDRATOR_MCP_MAX_WAIT_SECONDS", "40")
    assert McpSettings(target_name="local").maximum_wait_seconds == 40
    monkeypatch.setenv("PANDRATOR_MCP_MAX_WAIT_SECONDS", "0")
    with pytest.raises(ValueError, match="between 1 and 3600"):
        McpSettings(target_name="local")


def test_wait_receipt_reports_requested_effective_and_timeout():
    assert wait_receipt(requested=240, effective=25, terminal=False) == {
        "wait_requested_seconds": 240,
        "wait_effective_seconds": 25,
        "timed_out": True,
    }
    assert wait_receipt(requested=240, effective=25, terminal=True)["timed_out"] is False
    assert wait_receipt(requested=0, effective=0, terminal=False)["timed_out"] is False
    assert (
        bounded_suggestion(SimpleNamespace(settings=SimpleNamespace(maximum_wait_seconds=5)), 60)
        == 5
    )
    assert bounded_suggestion(SimpleNamespace(), 0) == 25
    assert effective_wait_seconds(SimpleNamespace(), 60) == 25
    assert effective_wait_seconds(SimpleNamespace(), 0) == 0
    assert wait_ceiling(SimpleNamespace()) == 25


def test_get_work_clamps_a_long_wait_to_the_ceiling(monkeypatch):
    states = []

    def get_work(work_id):
        states.append(work_id)
        return {
            "id": work_id,
            "kind": "generation.run",
            "state": "running",
            "progress": 0.3,
            "detail": "Synthesis",
            "poll_after_ms": 1_000,
        }

    runtime = settings_runtime(SimpleNamespace(get_work=get_work))
    elapsed, sleeps = clock(monkeypatch, work)

    outcome = work.get_work(
        runtime,
        GetWorkInput(work_id="job-1", wait_seconds=240),
    )

    assert elapsed[0] == 25
    assert sum(sleeps) == 25
    assert outcome.result["wait_requested_seconds"] == 240
    assert outcome.result["wait_effective_seconds"] == 25
    assert outcome.result["timed_out"] is True
    assert outcome.result["wait"]["requested_seconds"] == 240
    assert outcome.result["wait"]["effective_seconds"] == 25
    assert outcome.result["wait"]["timed_out"] is True
    assert outcome.result["wait"]["poll_count"] == len(states) - 1
    assert outcome.next_actions[0].arguments["wait_seconds"] == 25


def test_a_small_configured_ceiling_is_honoured(monkeypatch):
    def get_work(work_id):
        return {"id": work_id, "state": "queued", "progress": 0.0, "poll_after_ms": 2_000}

    runtime = settings_runtime(SimpleNamespace(get_work=get_work), ceiling=5)
    elapsed, _sleeps = clock(monkeypatch, work)

    outcome = work.get_work(
        runtime,
        GetWorkInput(work_id="job-2", wait_seconds=60),
    )

    assert elapsed[0] == 5
    assert outcome.result["wait_effective_seconds"] == 5
    assert outcome.result["timed_out"] is True
    assert outcome.next_actions[0].arguments["wait_seconds"] == 5


def test_get_generation_operation_clamps_and_suggests_within_the_ceiling(monkeypatch):
    class Application:
        def __init__(self):
            self.poll_timeouts = []

        def get_generation_operation(
            self,
            operation_id,
            *,
            include_coverage=True,
            request_timeout_seconds=None,
        ):
            self.poll_timeouts.append(request_timeout_seconds)
            return {
                "id": operation_id,
                "session_id": "session-1",
                "phase": "synthesis",
                "state": "running",
                "cancellable": True,
                "poll_after_ms": 1_000,
                "work": {
                    "id": "job-1",
                    "kind": "generation.run",
                    "state": "running",
                    "progress": 0.5,
                },
            }

    application = Application()
    runtime = settings_runtime(application)
    elapsed, _sleeps = clock(monkeypatch, generation_operations)

    outcome = generation_operations.get_generation_operation(
        runtime,
        GetGenerationOperationInput(operation_id="operation-1", wait_seconds=60),
    )

    assert elapsed[0] == 25
    assert outcome.result["wait_requested_seconds"] == 60
    assert outcome.result["wait_effective_seconds"] == 25
    assert outcome.result["timed_out"] is True
    # No single poll waits past the ceiling or past what is left of it.
    timeouts = application.poll_timeouts
    assert timeouts and all(
        timeout is not None and 1.0 <= timeout <= 25.0 for timeout in timeouts
    )
    assert timeouts[0] == 25.0
    assert outcome.next_actions[0].tool == "pandrator_get_generation_operation"
    assert outcome.next_actions[0].arguments["wait_seconds"] == 25


def test_media_edit_suggestion_and_wait_respect_the_ceiling():
    class Application:
        def propose_media_edit(self, session_id, **kwargs):
            return {"id": "job-propose", "status": "queued", "progress": 0.0}

        def wait_for_job(self, work_id, *, timeout_seconds):
            self.timeout = timeout_seconds
            return {"id": work_id, "status": "running", "progress": 0.4}

    application = Application()
    runtime = settings_runtime(application, ceiling=10)

    outcome = propose_media_edit(
        runtime,
        ProposeMediaEditArguments(
            session_id="session-1",
            revision=1,
            instructions="Remove pauses.",
            timeout_seconds=600,
            idempotency_key="media:ceiling:1",
        ),
    )

    assert application.timeout == 10
    assert outcome.result["wait_requested_seconds"] == 600
    assert outcome.result["wait_effective_seconds"] == 10
    assert outcome.result["timed_out"] is True
    assert outcome.next_actions[0].arguments["wait_seconds"] == 10


def test_transcription_wait_is_clamped_and_reported():
    class Application:
        def __init__(self):
            self.waits = []

        def get_transcription(self, identifier, *, format=None, wait_seconds=0):
            self.waits.append(wait_seconds)
            return {"id": identifier, "status": "running"}

    application = Application()
    runtime = settings_runtime(application)

    outcome = transcription.get_transcription(
        runtime,
        GetTranscriptionInput(id="transcription-1", wait_seconds=30),
    )

    assert application.waits == [25]
    assert outcome.result["wait_requested_seconds"] == 30
    assert outcome.result["wait_effective_seconds"] == 25
    assert outcome.result["timed_out"] is True
