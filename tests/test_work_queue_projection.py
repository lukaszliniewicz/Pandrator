"""Queue position, retry state and time left in the durable work projection."""

from __future__ import annotations

import base64
import io
import json
import sqlite3
import threading
import time
import wave
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import requests

from pandrator.logic import tts_handler
from pandrator.web.credentials import SecretRedactor
from pandrator.web.database import Database
from pandrator.web.generation_estimates import block_count, plan_time_estimate
from pandrator.web.generation_progress import (
    RetryTracker,
    open_job_progress_state,
)
from pandrator.web.jobs import JobQueue, Worker
from pandrator.web.models import (
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Job,
    utcnow,
)
from pandrator.web.sessions import SessionService
from pandrator.web.settings_identity import settings_workspace_id
from pandrator.web.tts_providers import (
    LegacyTtsAdapter,
    TtsBatchItem,
    TtsProviderRegistry,
)
from pandrator.web.work import WorkService
from pandrator.web.workflow_plans import WorkflowExecutionPlanService
from pandrator_mcp.schemas.work import GetWorkInput, ListWorkInput
from pandrator_mcp.tools import work as work_tools
from tests.web_test_support import prepare_web_test_data_root

PROJECT = "queue-visibility-project"
VERTEX_CREDENTIAL = json.dumps(
    {
        "type": "service_account",
        "project_id": PROJECT,
        "client_email": "fixture@example.invalid",
        "private_key": "fixture-private-key-material",
    }
)


@pytest.fixture
def case(tmp_path):
    database = Database(prepare_web_test_data_root(tmp_path).database)
    jobs = JobQueue(database)
    services = SimpleNamespace(
        database=database,
        jobs=jobs,
        sessions=SessionService(database),
        work=WorkService(jobs, SecretRedactor(database)),
    )
    try:
        yield services
    finally:
        database.dispose()


def running_job(case, session_id: str | None, keys: list[str], kind: str = "generation.run"):
    job = case.jobs.enqueue(
        kind, session_id=session_id, resource_keys=keys, max_attempts=2
    )
    claimed = case.jobs.claim("holder", kinds={kind})
    assert claimed is not None and claimed.id == job.id
    assert case.jobs.acquire_resources(
        job.id, "holder", keys, lease_generation=claimed.lease_generation
    )
    return job, claimed


def views_by_id(case) -> dict[str, dict[str, Any]]:
    return {view.id: view.model_dump(mode="json") for view in case.work.list(limit=20)}


def test_queue_names_the_blocker_and_its_position(case):
    busy_session = case.sessions.create("Rendering").id
    render, _claimed = running_job(
        case,
        busy_session,
        [f"session:{busy_session}", "service:ffmpeg"],
        "media_edit.render",
    )
    behind = case.jobs.enqueue(
        "export.create", session_id=busy_session, resource_keys=[f"session:{busy_session}"]
    )
    other_session = case.sessions.create("Other").id
    shared, _shared_claim = running_job(case, other_session, ["gpu:default"])
    waiting = case.jobs.enqueue(
        "generation.run",
        session_id=case.sessions.create("Third").id,
        resource_keys=["gpu:default"],
        max_attempts=2,
    )
    views = views_by_id(case)

    # A job in a session with a running job: session_busy names that job.
    assert views[behind.id]["queue"] == {
        "position": 1,
        "lane": "delivery",
        "reason": "session_busy",
        "blocked_by": {
            "work_id": render.id,
            "kind": "media_edit.render",
            "session_id": busy_session,
            "detail": None,
        },
        "resource": None,
    }
    # A held key in another session: resource_busy names the holder and the key.
    assert views[waiting.id]["queue"] == {
        "position": 1,
        "lane": "main",
        "reason": "resource_busy",
        "blocked_by": {
            "work_id": shared.id,
            "kind": "generation.run",
            "session_id": other_session,
            "detail": None,
        },
        "resource": "gpu:default",
    }


def test_evidence_beside_a_render_is_not_reported_as_session_busy(case):
    busy_session = case.sessions.create("Rendering").id
    running_job(
        case,
        busy_session,
        [f"session:{busy_session}", "service:ffmpeg"],
        "media_edit.render",
    )
    evidence = case.jobs.enqueue(
        "subtitle.evidence",
        session_id=busy_session,
        resource_keys=["subtitle-evidence:one"],
    )
    queue = views_by_id(case)[evidence.id]["queue"]
    # The fence lets evidence through; only the delivery lane decides now.
    assert queue["reason"] == "worker_busy"
    assert queue["lane"] == "delivery"


def test_queue_reports_a_busy_lane_and_a_scheduled_retry(case):
    holder_session = case.sessions.create("Generating").id
    running_job(
        case, holder_session, [f"session:{holder_session}", "service:tts:xtts"]
    )
    export_session = case.sessions.create("Exporting").id
    export = case.jobs.enqueue(
        "export.create", session_id=export_session, resource_keys=[f"session:{export_session}"]
    )
    view = case.work.get(export.id).model_dump(mode="json")
    # The delivery lane has nothing of its own running, so the export waits for
    # the lane rather than for the other project's job.
    assert view["queue"]["lane"] == "delivery"
    assert view["queue"]["position"] == 1
    assert view["queue"]["reason"] == "worker_busy"
    assert view["queue"]["blocked_by"] is None

    later_session = case.sessions.create("Scheduled").id
    later = case.jobs.enqueue(
        "export.create", session_id=later_session, resource_keys=[f"session:{later_session}"]
    )
    with case.database.immediate_session() as session:
        session.get(Job, later.id).available_at = utcnow() + timedelta(seconds=30)
    assert case.work.get(later.id).model_dump(mode="json")["queue"] == {
        "position": 2,
        "lane": "delivery",
        "reason": "scheduled",
        "blocked_by": None,
        "resource": None,
    }


def test_queue_position_counts_only_its_own_lane(case):
    session_id = case.sessions.create("Exports").id
    first = case.jobs.enqueue(
        "export.create", session_id=session_id, resource_keys=[f"session:{session_id}"]
    )
    other = case.sessions.create("Second export").id
    second = case.jobs.enqueue(
        "export.create", session_id=other, resource_keys=[f"session:{other}"]
    )
    generation_session = case.sessions.create("Generation").id
    generation = case.jobs.enqueue(
        "generation.run",
        session_id=generation_session,
        resource_keys=[f"session:{generation_session}"],
    )
    views = views_by_id(case)
    assert views[first.id]["queue"]["position"] == 1
    assert views[generation.id]["queue"]["position"] == 1
    # The second export shares its lane and waits its turn; a generation in
    # another session does not push it down.
    assert views[second.id]["queue"]["position"] == 2
    assert views[first.id]["queue"]["lane"] == "delivery"
    assert views[generation.id]["queue"]["lane"] == "main"


def test_a_deferred_job_reads_as_queued_without_a_start_time(case):
    job = case.jobs.enqueue("generation.run", resource_keys=["gpu:default"], max_attempts=2)
    claimed = case.jobs.claim("worker")
    assert claimed is not None
    assert case.jobs.defer_for_resources(
        job.id, "worker", lease_generation=claimed.lease_generation
    )
    view = case.work.get(job.id).model_dump(mode="json")
    assert view["state"] == "queued"
    assert view.get("started_at") is None


def test_mcp_work_tools_pass_the_queue_through():
    payload = {
        "id": "job-1",
        "kind": "export.create",
        "state": "queued",
        "progress": 0.0,
        "poll_after_ms": 1_500,
        "queue": {
            "position": 2,
            "lane": "delivery",
            "reason": "worker_busy",
            "blocked_by": None,
        },
    }
    application = SimpleNamespace(
        list_work=lambda **_kwargs: {"schema_version": "1", "items": [dict(payload)]},
        get_work=lambda _work_id: dict(payload),
    )
    runtime = SimpleNamespace(
        require_application=lambda: application, manager=None, application=None
    )
    listed = work_tools.list_work(runtime, ListWorkInput())
    fetched = work_tools.get_work(runtime, GetWorkInput(work_id="job-1"))
    assert listed["items"][0]["queue"]["reason"] == "worker_busy"
    assert fetched.result["queue"]["position"] == 2


def test_retry_detail_is_written_when_its_progress_value_does_not_move(case):
    seen: list[Any] = []

    def handler(payload, progress, _cancel):
        job_id = payload["_job_id"]
        with open_job_progress_state(job_id) as state:
            assert state is not None
            state.set_blocks(4, 484)
            progress(0.4, "Generating speech: 5 of 484")
            state.retry.record(
                "segment-5",
                attempt=4,
                attempt_limit=10,
                delay_seconds=38,
                provider="Google Vertex AI",
                reason="rate_limited",
            )
            # The next heartbeat carries the retry although the bar is unchanged.
            progress(0.4, "Generating speech: 5 of 484")
            stored = case.jobs.get(job_id)
            seen.append(stored.progress_detail or "")
            seen.append(stored.progress_state_json or {})
            state.retry.record(
                "segment-5",
                attempt=5,
                attempt_limit=10,
                delay_seconds=0,
                provider="Google Vertex AI",
                reason="rate_limited",
            )
            progress(0.4, "Generating speech: 5 of 484")
            seen.append(case.jobs.get(job_id).progress_detail or "")
        return {}

    job = case.jobs.enqueue("generation.run", max_attempts=1)
    assert Worker(case.jobs, "worker", {"generation.run": handler}).run_once()
    assert seen[0].startswith("Generating speech: 5 of 484; 1 request waiting on")
    assert "Google Vertex AI, attempt 4 of 10, next in 3" in seen[0]
    assert seen[1]["blocks_done"] == 4 and seen[1]["blocks_total"] == 484
    assert seen[1]["retry"]["pending"][0]["attempt"] == 4
    # Once the backoff elapses the ordinary detail comes back.
    assert seen[2] == "Generating speech: 5 of 484"
    assert case.jobs.get(job.id).status == "succeeded"


def test_an_expired_retry_detail_is_cleared_with_its_structured_state(case, monkeypatch):
    # Only this handler drives heartbeats, so the expiry below is unambiguous.
    monkeypatch.setattr(
        "pandrator.web.jobs.threading.Thread",
        lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None),
    )
    seen: list[Any] = []

    def handler(payload, progress, _cancel):
        job_id = payload["_job_id"]
        with open_job_progress_state(job_id) as state:
            assert state is not None
            state.retry.record(
                "segment-5",
                attempt=2,
                attempt_limit=10,
                delay_seconds=0.02,
                provider="Google Vertex AI",
                reason="rate_limited",
            )
            progress(0.5, None)
            seen.append(case.jobs.get(job_id).progress_detail or "")
            time.sleep(0.05)
            # The backoff elapsed: one heartbeat now drops both the suffix and
            # the structured retry state it described.
            progress(0.5, None)
            stored = case.jobs.get(job_id)
            seen.append(stored.progress_detail)
            seen.append(stored.progress_state_json)
        return {}

    job = case.jobs.enqueue("generation.run", max_attempts=1)
    assert Worker(case.jobs, "worker", {"generation.run": handler}).run_once()
    assert "waiting on" in seen[0]
    assert seen[1] is None
    assert seen[2] is None
    assert case.jobs.get(job.id).status == "succeeded"


def test_a_busy_heartbeat_keeps_the_pending_progress_state(case, monkeypatch):
    monkeypatch.setattr(
        "pandrator.web.jobs.threading.Thread",
        lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None),
    )
    original = case.jobs.heartbeat
    rejected: list[bool] = []

    def heartbeat(*args, **kwargs):
        if isinstance(kwargs.get("progress_state"), dict) and not rejected:
            rejected.append(True)
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
        return original(*args, **kwargs)

    monkeypatch.setattr(case.jobs, "heartbeat", heartbeat)
    observed: list[Any] = []

    def handler(payload, progress, _cancel):
        job_id = payload["_job_id"]
        with open_job_progress_state(job_id) as state:
            assert state is not None
            state.set_blocks(4, 40)
            # This write is refused as busy; the report must not be lost.
            progress(0.1, "Generating speech: 4 of 40")
            observed.append(case.jobs.get(job_id).progress_state_json)
            # The next heartbeat still sends the pending structured progress.
            progress(0.2, "Generating speech: 5 of 40")
            observed.append(case.jobs.get(job_id).progress_state_json)
        return {}

    job = case.jobs.enqueue("generation.run", max_attempts=1)
    assert Worker(case.jobs, "worker", {"generation.run": handler}).run_once()
    assert rejected
    assert "blocks_done" not in observed[0]
    assert observed[1]["blocks_done"] == 4
    assert observed[1]["blocks_total"] == 40
    # The claim's attempt start survives the report that replaces the state.
    assert observed[1]["attempt_started_at"]
    assert case.jobs.get(job.id).status == "succeeded"


def test_a_second_heartbeat_waits_for_the_first_to_acknowledge(case, monkeypatch):
    # Only the two threads below drive renew_lease here: the monitor would
    # otherwise add a heartbeat of its own.
    real_thread = threading.Thread
    monkeypatch.setattr(
        "pandrator.web.jobs.threading.Thread",
        lambda *_args, **_kwargs: SimpleNamespace(start=lambda: None),
    )
    original = case.jobs.heartbeat
    first_entered = threading.Event()
    release_first = threading.Event()
    entries: list[int] = []
    entries_lock = threading.Lock()

    def heartbeat(*args, **kwargs):
        with entries_lock:
            entries.append(1)
            position = len(entries)
        if position == 1:
            first_entered.set()
            assert release_first.wait(1.0)
        return original(*args, **kwargs)

    monkeypatch.setattr(case.jobs, "heartbeat", heartbeat)

    def handler(payload, progress, _cancel):
        job_id = payload["_job_id"]
        with open_job_progress_state(job_id) as state:
            assert state is not None
            state.set_blocks(1, 10)
            first_done = threading.Event()
            second_done = threading.Event()

            def first() -> None:
                progress(0.1, "Generating speech: 1 of 10")
                first_done.set()

            def second() -> None:
                progress(0.2, "Generating speech: 2 of 10")
                second_done.set()

            threads = [
                real_thread(target=first),
                real_thread(target=second),
            ]
            threads[0].start()
            assert first_entered.wait(1.0)
            threads[1].start()
            # The second heartbeat waits for the first to write and acknowledge
            # instead of collecting values the first one still has in flight.
            assert not second_done.wait(0.2)
            assert len(entries) == 1
            release_first.set()
            for thread in threads:
                thread.join(1.0)
            assert first_done.is_set() and second_done.is_set()
            assert len(entries) == 2
        return {}

    job = case.jobs.enqueue("generation.run", max_attempts=1)
    assert Worker(case.jobs, "worker", {"generation.run": handler}).run_once()
    assert case.jobs.get(job.id).status == "succeeded"


def wav_bytes() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24_000)
        audio.writeframes(b"\x00\x00\x40\x00\xc0\xff\x00\x00")
    return output.getvalue()


def vertex_response(audio: bytes) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response.headers["Content-Type"] = "application/json"
    response._content = json.dumps(
        {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "inlineData": {
                                    "mimeType": "audio/wav",
                                    "data": base64.b64encode(audio).decode("ascii"),
                                }
                            }
                        ]
                    }
                }
            ]
        }
    ).encode("utf-8")
    return response


def vertex_settings() -> dict[str, Any]:
    return {
        "service": "Google Vertex AI",
        "model": "gemini-3.8-flash-tts",
        "voice": "Kore",
        "language": "en",
        "max_attempts": 4,
        "rate_limit_retry_base_delay_seconds": 0.01,
        "retry_max_delay_seconds": 0.02,
        "retry_timeout_seconds": 60,
        "provider_configs": [
            {
                "id": "vertex_ai",
                "name": "Google Vertex AI",
                "provider": "vertex_ai",
                "api_base": "https://aiplatform.googleapis.com",
                "api_key": VERTEX_CREDENTIAL,
                "models": ["gemini-3.8-flash-tts"],
                "voices": ["Kore"],
                "default_model": "gemini-3.8-flash-tts",
                "default_voice": "Kore",
                "vertex_project": PROJECT,
                "vertex_location": "global",
            }
        ],
    }


class RecordingTracker(RetryTracker):
    """Keep every retry the parallel batch reports, before it expires."""

    def __init__(self) -> None:
        super().__init__()
        self.records: list[dict[str, Any]] = []

    def record(self, segment: str, **values: Any) -> None:
        super().record(segment, **values)
        # Keep what was reported; a derived summary expires with its backoff.
        self.records.append({"segment": segment, **values})


def test_parallel_batch_records_vertex_rate_limit_retries(monkeypatch):
    audio = wav_bytes()
    calls: list[float] = []

    def post(_url, **_options):
        calls.append(1.0)
        if len(calls) <= 2:
            limited = requests.Response()
            limited.status_code = 429
            limited._content = b'{"error": "Resource exhausted"}'
            return limited
        return vertex_response(audio)

    monkeypatch.setattr(tts_handler.requests, "post", post)
    monkeypatch.setattr(
        tts_handler, "_vertex_access_token", lambda *_a, **_k: ("token", PROJECT)
    )
    registry = TtsProviderRegistry()
    tracker = RecordingTracker()
    try:
        results = list(
            registry.synthesize_batch(
                [TtsBatchItem(id="segment-1", text="Hello there", settings=vertex_settings())],
                batch_size=3,
                retry_tracker=tracker,
            )
        )
    finally:
        registry.close()

    assert len(calls) == 3
    assert results[0].error is None and results[0].audio is not None
    assert [item["attempt"] for item in tracker.records] == [2, 3]
    first = tracker.records[0]
    assert first["segment"] == "segment-1"
    assert first["attempt_limit"] == 4
    assert first["provider"] == "Google Vertex AI"
    assert first["reason"] == "rate_limited"


class RetryingLegacyAdapter(LegacyTtsAdapter):
    """A function-based provider that reports one retry before answering."""

    def synthesize(self, text, settings, **options):
        callback = options.get("retry_callback")
        if callback is not None:
            callback(1, 3, 0.0, "rate_limited")
        return None


def test_a_single_item_legacy_batch_reports_its_retry():
    registry = TtsProviderRegistry()
    registry.replace(RetryingLegacyAdapter("local_retry_probe"))
    tracker = RecordingTracker()
    try:
        results = list(
            registry.synthesize_batch(
                [
                    TtsBatchItem(
                        id="segment-1",
                        text="Hello there",
                        settings={"service_id": "local_retry_probe"},
                    )
                ],
                batch_size=1,
                retry_tracker=tracker,
            )
        )
    finally:
        registry.close()

    assert len(results) == 1 and results[0].error is None
    # The inherited adapter-level batch dropped the tracker it was given.
    assert [item["segment"] for item in tracker.records] == ["segment-1"]
    assert tracker.records[0]["attempt"] == 1
    assert tracker.records[0]["attempt_limit"] == 3


def test_running_estimate_needs_ten_blocks_or_two_minutes(case):
    job = case.jobs.enqueue("generation.run", max_attempts=1)
    claimed = case.jobs.claim("worker")
    assert claimed is not None
    case.jobs.heartbeat(
        job.id,
        "worker",
        lease_generation=claimed.lease_generation,
        progress=0.1,
        detail="Generating speech: 4 of 40",
        progress_state={"blocks_done": 3, "blocks_total": 40},
    )
    assert "estimate" not in case.work.get(job.id).model_dump(mode="json")

    with case.database.immediate_session() as session:
        stored = session.get(Job, job.id)
        stored.started_at = utcnow() - timedelta(seconds=120)
        stored.progress_state_json = {"blocks_done": 30, "blocks_total": 40}
    estimate = case.work.get(job.id).model_dump(mode="json")["estimate"]
    assert estimate["blocks_done"] == 30
    assert estimate["blocks_total"] == 40
    assert estimate["blocks_per_minute"] == pytest.approx(15.0, rel=0.1)
    assert estimate["remaining_seconds"] == pytest.approx(40.0, rel=0.1)


def test_running_estimate_ignores_blocks_a_resume_kept(case):
    job = case.jobs.enqueue("generation.run", max_attempts=1)
    assert case.jobs.claim("worker") is not None
    with case.database.immediate_session() as session:
        stored = session.get(Job, job.id)
        stored.started_at = utcnow() - timedelta(seconds=120)
        # 300 blocks came from the paused attempt; 30 were synthesised now.
        stored.progress_state_json = {
            "blocks_done": 330,
            "blocks_total": 484,
            "blocks_kept": 300,
        }
    estimate = case.work.get(job.id).model_dump(mode="json")["estimate"]
    assert estimate["blocks_done"] == 330
    assert estimate["blocks_per_minute"] == pytest.approx(15.0, rel=0.1)
    assert estimate["remaining_seconds"] == pytest.approx(154 * 4.0, rel=0.1)


def test_a_reclaimed_job_estimates_from_its_current_attempt(case):
    job = case.jobs.enqueue("generation.run", max_attempts=2)
    first = case.jobs.claim("worker")
    assert first is not None and first.id == job.id
    with case.database.immediate_session() as session:
        stored = session.get(Job, job.id)
        # The first attempt began six minutes ago and then lost its lease.
        stored.started_at = utcnow() - timedelta(seconds=360)
        stored.lease_expires_at = utcnow() - timedelta(seconds=1)
    reclaimed = case.jobs.claim("worker")
    assert reclaimed is not None and reclaimed.id == job.id
    assert reclaimed.started_at is not None
    # The claim measures the new attempt separately from the first claim.
    assert reclaimed.progress_state_json["attempt_started_at"]
    with case.database.immediate_session() as session:
        stored = session.get(Job, job.id)
        # 30 of 40 blocks were synthesised in this attempt's first two minutes.
        stored.progress_state_json = {
            **(stored.progress_state_json or {}),
            "blocks_done": 30,
            "blocks_total": 40,
            "attempt_started_at": (utcnow() - timedelta(seconds=120)).isoformat(),
        }
    estimate = case.work.get(job.id).model_dump(mode="json")["estimate"]
    assert estimate["blocks_per_minute"] == pytest.approx(15.0, rel=0.1)
    assert estimate["remaining_seconds"] == pytest.approx(40.0, rel=0.1)


def test_plan_pace_measures_parallel_waves_not_single_gaps():
    from datetime import UTC, datetime

    from pandrator.web.generation_estimates import _seconds_per_block

    start = datetime(2026, 11, 17, 14, 0, tzinfo=UTC)
    # Three requests at a time: each wave commits three takes within 20 ms,
    # then waits nine seconds for the next. Single gaps are mostly 10 ms.
    moments = [
        start + timedelta(seconds=9 * wave, milliseconds=10 * slot)
        for wave in range(10)
        for slot in range(3)
    ]
    assert _seconds_per_block(moments, 3) == pytest.approx(3.0, rel=0.05)
    # A long pause in the middle of a run does not move the median.
    paused = moments[:15] + [moment + timedelta(hours=2) for moment in moments[15:]]
    assert _seconds_per_block(paused, 3) == pytest.approx(3.0, rel=0.05)
    assert _seconds_per_block(moments[:2], 3) is None


def test_retry_and_estimate_are_absent_for_finished_work(case):
    job = case.jobs.enqueue("generation.run", max_attempts=1)
    claimed = case.jobs.claim("worker")
    assert claimed is not None
    state = {
        "blocks_done": 30,
        "blocks_total": 40,
        "retry": {
            "attempt_limit": 10,
            "provider": "Google Vertex AI",
            "pending": [
                {
                    "segment": "segment-1",
                    "attempt": 4,
                    "attempt_limit": 10,
                    "provider": "Google Vertex AI",
                    "reason": "rate_limited",
                    "retry_at": (utcnow() + timedelta(seconds=30)).isoformat(),
                }
            ],
        },
    }
    case.jobs.heartbeat(
        job.id,
        "worker",
        lease_generation=claimed.lease_generation,
        progress=0.75,
        detail="Generating speech: 31 of 40",
        progress_state=state,
    )
    with case.database.immediate_session() as session:
        stored = session.get(Job, job.id)
        stored.started_at = utcnow() - timedelta(seconds=600)
        # The estimate now measures the current attempt, whose start the claim
        # records; keep this fixture's simulated hour of work explicit.
        stored.progress_state_json = {
            **(stored.progress_state_json or {}),
            "attempt_started_at": (utcnow() - timedelta(seconds=600)).isoformat(),
        }
    running = case.work.get(job.id).model_dump(mode="json")
    assert running["retry"] == {
        "pending_requests": 1,
        "max_attempt": 4,
        "attempt_limit": 10,
        "provider": "Google Vertex AI",
        "next_retry_in_seconds": pytest.approx(30.0, abs=1.0),
    }
    assert running["estimate"]["blocks_done"] == 30

    case.jobs.complete(job.id, "worker", {}, lease_generation=claimed.lease_generation)
    finished = case.work.get(job.id).model_dump(mode="json")
    assert "retry" not in finished and "estimate" not in finished


def seed_history(case, *, session_id: str, blocks: int = 12) -> None:
    """A 12-block session plan and one completed run whose takes were 2 s apart."""

    history_session = case.sessions.create("History").id
    with case.database.immediate_session() as session:
        plan = GenerationPlan(session_id=session_id)
        session.add(plan)
        session.flush()
        planned = GenerationPlanRevision(
            plan_id=plan.id, revision_number=1, content_hash="plan-one"
        )
        session.add(planned)
        session.flush()
        plan.active_revision_id = planned.id
        for ordinal in range(blocks):
            session.add(
                GenerationSegment(
                    plan_revision_id=planned.id,
                    ordinal=ordinal,
                    text=f"Block {ordinal}",
                )
            )
        history_plan = GenerationPlan(session_id=history_session)
        session.add(history_plan)
        session.flush()
        takes = GenerationPlanRevision(
            plan_id=history_plan.id, revision_number=1, content_hash="plan-two"
        )
        session.add(takes)
        session.flush()
        history_plan.active_revision_id = takes.id
        run = GenerationRun(
            session_id=history_session,
            plan_revision_id=takes.id,
            status="completed",
            settings_snapshot_json={
                "tts": {
                    "service": "vertex_ai",
                    "model": "gemini-3.8-flash-tts",
                    "tts_concurrent_requests": 3,
                }
            },
        )
        session.add(run)
        session.flush()
        base = utcnow() - timedelta(hours=1)
        for ordinal in range(blocks):
            segment = GenerationSegment(
                plan_revision_id=takes.id,
                ordinal=ordinal,
                text=f"Take {ordinal}",
            )
            session.add(segment)
            session.flush()
            session.add(
                AudioTake(
                    generation_segment_id=segment.id,
                    generation_run_id=run.id,
                    status="completed",
                    created_at=base + timedelta(seconds=2 * ordinal),
                )
            )


SETTINGS = {
    "tts": {
        "service": "vertex_ai",
        "model": "gemini-3.8-flash-tts",
        "tts_concurrent_requests": 3,
    }
}


def test_plan_estimate_says_no_history_without_a_matching_run(case):
    session_id = case.sessions.create("Planned").id
    with case.database.snapshot_session() as session:
        workspace_id = settings_workspace_id(session, session_id=session_id)
        estimate = plan_time_estimate(
            session,
            workspace_id=workspace_id,
            settings=SETTINGS,
            blocks=block_count(session, session_id),
        )
    # No prepared speech plan: there is nothing to multiply, and nothing to say.
    assert estimate == {"estimated_seconds": None, "estimate_basis": "no_history"}


def test_generation_step_carries_the_plan_estimate(case):
    session_id = case.sessions.create("Estimated").id
    with case.database.snapshot_session() as session:
        workspace_id = settings_workspace_id(session, session_id=session_id)
        assert block_count(session, session_id) is None
    seed_history(case, session_id=session_id)
    with case.database.snapshot_session() as session:
        assert block_count(session, session_id) == 12
        estimate = plan_time_estimate(
            session, workspace_id=workspace_id, settings=SETTINGS, blocks=12
        )
    assert estimate["estimated_seconds"] == pytest.approx(22.0, rel=0.2)
    assert "12 blocks at" in estimate["estimate_basis"]
    assert "12 takes" in estimate["estimate_basis"]

    service = WorkflowExecutionPlanService(
        case.database, None, None, None, None  # type: ignore[arg-type]
    )
    resolved = SimpleNamespace(payload={"settings": SETTINGS})
    steps = [{"order": 1, "stage": "generate_audio", "decision": "run"}]
    service._apply_generation_estimate(
        steps, session_id=session_id, resolved=resolved, supplied={}  # type: ignore[arg-type]
    )
    assert steps[0]["estimated_seconds"] == estimate["estimated_seconds"]
    assert steps[0]["estimate_basis"] == estimate["estimate_basis"]

    # Without history the plan still carries both fields, and says why.
    fresh_session = case.sessions.create("Fresh").id
    fresh_steps = [{"order": 1, "stage": "generate_audio", "decision": "run"}]
    service._apply_generation_estimate(
        fresh_steps, session_id=fresh_session, resolved=resolved, supplied={}  # type: ignore[arg-type]
    )
    assert fresh_steps[0]["estimated_seconds"] is None
    assert fresh_steps[0]["estimate_basis"] == "no_history"
