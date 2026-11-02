"""Bounded, attributed worker diagnostics never compete with handler writers."""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pandrator.web import jobs
from pandrator.web.database import Database
from pandrator.web.jobs import JobQueue, Worker, _capture_job_logs, _JobLogHandler
from pandrator.web.models import Job, utcnow
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def runtime(tmp_path):
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    try:
        yield database, JobQueue(database)
    finally:
        database.dispose()


def _logs(queue, job_id):
    return [
        event.payload_json for event in queue.events_for(job_id) if event.event_type == "job.log"
    ]


def test_concurrent_workers_capture_only_own_thread_and_keep_info_until_last_exit(runtime):
    _database, queue = runtime
    first = queue.enqueue("capture", {"marker": "first-worker"})
    second = queue.enqueue("capture", {"marker": "second-worker"})
    ready = threading.Barrier(3)
    emit = threading.Event()
    first_done = threading.Event()
    first_ready = threading.Event()

    def handler(payload, _progress, _cancel):
        if payload["marker"] == "first-worker":
            first_ready.set()
        ready.wait(timeout=5)
        assert emit.wait(5)
        marker = payload["marker"]
        logging.info("own-info %s", marker)
        logging.warning("own-warning %s", marker)
        child = threading.Thread(target=lambda: logging.warning("unattributed-child %s", marker))
        child.start()
        child.join(timeout=5)
        assert not child.is_alive()
        if marker == "second-worker":
            assert first_done.wait(5)
            logging.info("late-info second-worker")
        return {}

    workers = [Worker(queue, name, {"capture": handler}) for name in ("first", "second")]
    root = logging.getLogger()
    original = root.level
    root.setLevel(logging.WARNING)

    def run_first():
        try:
            return workers[0].run_once()
        finally:
            first_done.set()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(run_first)
            assert first_ready.wait(5)
            second_future = executor.submit(workers[1].run_once)
            ready.wait(timeout=5)
            logging.warning("unrelated-request-marker")
            emit.set()
            assert first_future.result(timeout=5)
            assert second_future.result(timeout=5)
        assert root.level == logging.WARNING
    finally:
        emit.set()
        first_done.set()
        root.setLevel(original)

    for job, marker, foreign in (
        (first, "first-worker", "second-worker"),
        (second, "second-worker", "first-worker"),
    ):
        assert queue.get(job.id).status == "succeeded"
        messages = [record["message"] for record in _logs(queue, job.id)]
        assert f"own-info {marker}" in messages
        assert f"own-warning {marker}" in messages
        assert not any(foreign in message for message in messages)
        assert not any(
            "unrelated-request" in message or "unattributed-child" in message
            for message in messages
        )
    assert "late-info second-worker" in [record["message"] for record in _logs(queue, second.id)]


def _capture(queue, kind="capture"):
    job = queue.enqueue(kind, max_attempts=2)
    claimed = queue.claim("owner")
    assert claimed is not None and claimed.id == job.id
    return claimed, _JobLogHandler(queue, job.id, "owner", claimed.lease_generation)


def _record(message, *, exc_info=None):
    return logging.LogRecord("job-fixture", logging.WARNING, __file__, 1, message, (), exc_info)


def test_warning_inside_actual_handler_writer_is_prompt_and_heartbeat_recovers(
    runtime, monkeypatch
):
    database, queue = runtime
    job = queue.enqueue("writer")
    renewed = threading.Event()
    heartbeat_resources = queue.heartbeat_resources

    def renewal(*args, **kwargs):
        result = heartbeat_resources(*args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(queue, "heartbeat_resources", renewal)
    elapsed = []

    def handler(_payload, _progress, cancel):
        with database.immediate_session():
            start = time.monotonic()
            logging.warning("inside-own-writer-marker")
            elapsed.append(time.monotonic() - start)
            assert elapsed[-1] < 1
            assert not cancel.wait(0.6)
        assert renewed.wait(3)
        assert not cancel.is_set()
        return {"writer_released": True}

    assert Worker(queue, "owner", {"writer": handler}).run_once()
    assert elapsed and elapsed[0] < 1
    assert queue.get(job.id).status == "succeeded"
    assert any(row["message"] == "inside-own-writer-marker" for row in _logs(queue, job.id))


def test_busy_flush_retains_records_and_original_emitted_time(runtime):
    database, queue = runtime
    job, capture = _capture(queue)
    record = _record("retry-after-busy")
    capture.emit(record)
    assert not _logs(queue, job.id)
    start = time.monotonic()
    with database.immediate_session():
        assert capture.flush_pending() == 0
        assert len(capture._pending) == 1
    assert time.monotonic() - start < 1
    assert capture.flush_pending() == 1
    assert not capture._pending
    rows = _logs(queue, job.id)
    assert len(rows) == 1
    assert rows[0]["message"] == "retry-after-busy"
    assert datetime.fromisoformat(rows[0]["emitted_at"]) == datetime.fromtimestamp(
        record.created, timezone.utc
    )


def test_buffered_logs_cannot_follow_a_new_claim_generation(runtime):
    database, queue = runtime
    job, capture = _capture(queue)
    capture.emit(_record("old-generation-marker"))
    with database.immediate_session() as unit:
        unit.get_one(Job, job.id).lease_expires_at = utcnow() - timedelta(seconds=1)
    current = queue.claim("owner")
    assert current is not None and current.lease_generation == job.lease_generation + 1
    assert capture.flush_pending() == 0
    assert not _logs(queue, job.id)
    assert len(capture._pending) == 1


def test_unexpected_persistence_error_is_retained_and_reported_without_recursion(
    runtime, monkeypatch
):
    database, queue = runtime
    job, capture = _capture(queue)
    capture.emit(_record("recover-from-persistence-error"))
    writer = database.immediate_session
    fail = Mock(side_effect=RuntimeError("api_key=private-error-value"))
    monkeypatch.setattr(database, "immediate_session", fail)
    capture.handleError = Mock(side_effect=AssertionError("recursive logging error path"))
    assert capture.flush_pending() == 0
    assert len(capture._pending) == 1
    monkeypatch.setattr(database, "immediate_session", writer)
    assert capture.flush_pending() == 2
    messages = [row["message"] for row in _logs(queue, job.id)]
    assert "recover-from-persistence-error" in messages
    assert any("recovered after 1 error(s) (RuntimeError)" in message for message in messages)
    assert not any("private-error-value" in message for message in messages)
    capture.handleError.assert_not_called()


def test_redaction_precedes_bounded_buffering_for_messages_and_trace(runtime):
    _database, queue = runtime
    job, capture = _capture(queue)
    secret = "known-fixture-credential"
    try:
        raise ValueError(f"api_key={secret}")
    except ValueError:
        import sys

        record = _record(
            f"credential {secret}; Bearer abcdefghijklmnop; " + "界" * 9000, exc_info=sys.exc_info()
        )
    capture.emit(_record("safe-first-record"))
    with queue.secret_redactor.scoped_values([secret]):
        capture.emit(record)
    pending = capture._pending[-1]
    assert secret not in pending.message and secret not in pending.trace
    assert "abcdefghijklmnop" not in pending.message
    assert len(pending.message.encode("utf-8")) <= 8192
    assert len(pending.trace.encode("utf-8")) <= 8192
    assert capture.flush_pending() == 2
    rows = _logs(queue, job.id)
    assert secret not in str(rows)
    assert rows[1]["trace"] == pending.trace
    for row in rows:
        assert row["job_kind"] == "capture" and row["status"] == "running"
        assert "changed_entities" in row and "progress" in row


def test_overflow_is_bounded_and_has_one_durable_drop_summary(runtime):
    _database, queue = runtime
    job, capture = _capture(queue)
    for number in range(300):
        capture.emit(_record(f"bounded-record-{number}"))
    assert len(capture._pending) == 256 and capture._dropped == 44
    total = 0
    while count := capture.flush_pending():
        assert count <= 20
        total += count
    rows = _logs(queue, job.id)
    assert total == len(rows) == 257
    summaries = [row for row in rows if "capture dropped" in row["message"]]
    assert len(summaries) == 1 and "44 record(s)" in summaries[0]["message"]
    assert not capture._pending and capture._dropped == 0


def test_terminal_drain_can_flush_full_buffer_before_success(runtime, monkeypatch):
    _database, queue = runtime
    job = queue.enqueue("burst")
    # Exercise full-buffer admission independently of workstation disk speed.
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: 1000.0))
    renewed = threading.Event()
    heartbeat_resources = queue.heartbeat_resources

    def renewal(*args, **kwargs):
        result = heartbeat_resources(*args, **kwargs)
        renewed.set()
        return result

    monkeypatch.setattr(queue, "heartbeat_resources", renewal)

    def handler(_payload, _progress, _cancel):
        assert renewed.wait(3)
        for number in range(256):
            logging.info("terminal-burst-%s", number)
        return {}

    assert Worker(queue, "owner", {"burst": handler}).run_once()
    assert queue.get(job.id).status == "succeeded"
    assert len(_logs(queue, job.id)) == 256


def test_capture_preserves_external_root_level_change():
    root = logging.getLogger()
    original = root.level
    handlers = (logging.NullHandler(), logging.NullHandler())
    root.setLevel(logging.WARNING)
    try:
        with _capture_job_logs(handlers[0]):
            with _capture_job_logs(handlers[1]):
                assert root.level == logging.INFO
                root.setLevel(logging.ERROR)
            assert root.level == logging.ERROR
        assert root.level == logging.ERROR
    finally:
        root.setLevel(original)


def test_quick_transcription_does_not_persist_speech_diagnostics(runtime):
    _database, queue = runtime
    job = queue.enqueue("transcription.quick")

    def handler(_payload, _progress, _cancel):
        logging.info("recognized-private-speech")
        logging.warning("recognized-private-speech-warning")
        return {}

    assert Worker(queue, "owner", {"transcription.quick": handler}).run_once()
    assert queue.get(job.id).status == "succeeded"
    assert not _logs(queue, job.id)


def test_log_persistence_errors_do_not_fail_an_actual_worker(runtime, monkeypatch):
    _database, queue = runtime
    job = queue.enqueue("capture")
    event = queue._event

    def fail_only_log_events(session, job_id, event_type, payload=None):
        if event_type == "job.log":
            raise RuntimeError("synthetic diagnostic persistence failure")
        return event(session, job_id, event_type, payload)

    monkeypatch.setattr(queue, "_event", fail_only_log_events)

    def handler(_payload, _progress, _cancel):
        logging.warning("diagnostic-cannot-persist")
        return {"domain_work_completed": True}

    assert Worker(queue, "owner", {"capture": handler}).run_once()
    current = queue.get(job.id)
    assert current.status == "succeeded" and current.error_code is None
    assert not _logs(queue, job.id)


def test_terminal_drain_waits_within_deadline_for_an_active_monitor_flush(runtime, monkeypatch):
    _database, queue = runtime
    job, capture = _capture(queue)
    capture.emit(_record("first-batch-record"))
    active = threading.Event()
    release = threading.Event()
    event = queue._event

    def held_event(session, job_id, event_type, payload=None):
        if event_type == "job.log" and payload["message"] == "first-batch-record":
            active.set()
            assert release.wait(3)
        return event(session, job_id, event_type, payload)

    monkeypatch.setattr(queue, "_event", held_event)
    with ThreadPoolExecutor(max_workers=1) as executor:
        flushing = executor.submit(capture.flush_pending)
        try:
            assert active.wait(3)
            # Emission stays independent of the active SQLite writer/flush lock.
            capture.emit(_record("terminal-tail-record"))
            timer = threading.Timer(0.02, release.set)
            timer.start()
            try:
                assert capture.flush_pending(deadline=time.monotonic() + 0.5) == 1
            finally:
                timer.join(timeout=1)
            assert flushing.result(timeout=3) == 1
        finally:
            release.set()
    assert [row["message"] for row in _logs(queue, job.id)] == [
        "first-batch-record",
        "terminal-tail-record",
    ]


def test_terminal_flush_lock_wait_respects_remaining_budget(runtime):
    _database, queue = runtime
    _job, capture = _capture(queue)
    capture.emit(_record("deadline-retained-record"))
    with capture._flush_lock:
        start = time.monotonic()
        assert capture.flush_pending(deadline=start + 0.03) == 0
        assert time.monotonic() - start < 0.3
    assert len(capture._pending) == 1
    assert capture.flush_pending() == 1


def test_terminal_budget_expiry_retains_tail_without_failing_job(runtime, monkeypatch):
    _database, queue = runtime
    job = queue.enqueue("burst")
    clock = [1000.0]
    monkeypatch.setattr(jobs, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    flush = _JobLogHandler.flush_pending
    captures = []

    def advance_after_batch(capture, max_records=20, *, deadline=None):
        result = flush(capture, max_records, deadline=deadline)
        if deadline is not None:
            captures.append(capture)
            clock[0] += 1.0
        return result

    monkeypatch.setattr(_JobLogHandler, "flush_pending", advance_after_batch)

    def handler(_payload, _progress, _cancel):
        for number in range(100):
            logging.info("budget-expiry-record-%s", number)
        return {}

    assert Worker(queue, "owner", {"burst": handler}).run_once()
    assert queue.get(job.id).status == "succeeded"
    assert len(captures) == 1 and len(captures[0]._pending) == 80
    assert len(_logs(queue, job.id)) == 20
