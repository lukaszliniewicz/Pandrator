"""Background writers must finish before application resources retire."""

from __future__ import annotations

import os
import threading
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy import CursorResult, delete, select

from pandrator.web import startup
from pandrator.web.application_services import ApplicationServices
from pandrator.web.maintenance import apply_retention
from pandrator.web.maintenance_threads import MaintenanceThread
from pandrator.web.models import (
    AppSettingHistory,
    JobEvent,
    SessionRecord,
    SessionSettingHistory,
    utcnow,
)
from tests.web_test_support import prepare_web_test_data_root


@pytest.fixture
def services(tmp_path: Path) -> Iterator[ApplicationServices]:
    prepare_web_test_data_root(tmp_path)
    value = ApplicationServices.build(data_root=tmp_path)
    try:
        yield value
    finally:
        value.startup_maintenance.stop()
        value.quick_transcriptions.stop_maintenance()
        for thread in (
            value.startup_maintenance._thread,
            value.startup_maintenance._periodic_thread,
            value.quick_transcriptions._thread,
        ):
            if thread is not None:
                thread.join(timeout=5)
                assert not thread.is_alive()
        value.tts_providers.close()
        value.database.dispose()


def test_retention_counts_native_deletes_and_preserves_recent_and_external_files(services):
    old = utcnow() - timedelta(days=40)
    recent = utcnow()
    with services.database.session() as session:
        record = SessionRecord(name="retention-contract")
        session.add(record)
        session.flush()
        session_id = record.id
        for created in (old, recent):
            session.add_all([
                JobEvent(event_type="retention.contract", payload_json={}, created_at=created),
                AppSettingHistory(key="retention.contract", revision=1, value_json={}, created_at=created),
                SessionSettingHistory(session_id=session_id, section="tts", revision=1, value_json={}, created_at=created),
            ])
        session.flush()
        for model in (JobEvent, AppSettingHistory, SessionSettingHistory):
            result = session.execute(delete(model).where(model.created_at < old))
            assert isinstance(result, CursorResult)
            assert result.rowcount == 0
    expired = []
    current = []
    for root in (services.paths.temporary, services.paths.logs):
        root.mkdir(parents=True, exist_ok=True)
        expired.append(root / "retention-expired")
        expired[-1].write_text("expired")
        os.utime(expired[-1], (old.timestamp(), old.timestamp()))
        current.append(root / "retention-current")
        current[-1].write_text("current")
    outside = services.paths.uploads / "retention-protected"
    outside.write_text("protected source")
    os.utime(outside, (old.timestamp(), old.timestamp()))
    link = services.paths.temporary / "retention-external-link"
    link.symlink_to(outside)
    assert apply_retention(services.database, services.paths, 30) == {
        "job_events": 1, "app_setting_history": 1, "session_setting_history": 1, "files": 2,
    }
    assert all(not path.exists() for path in expired)
    assert all(path.read_text() == "current" for path in current)
    assert outside.read_text() == "protected source" and link.is_symlink()
    with services.database.session() as session:
        assert len(list(session.scalars(select(JobEvent).where(JobEvent.event_type == "retention.contract")))) == 1
        assert len(list(session.scalars(select(AppSettingHistory).where(AppSettingHistory.key == "retention.contract")))) == 1
        assert len(list(session.scalars(select(SessionSettingHistory).where(SessionSettingHistory.session_id == session_id)))) == 1
        assert session.get(SessionRecord, session_id) is not None
    assert apply_retention(services.database, services.paths, 30) == {
        "job_events": 0, "app_setting_history": 0, "session_setting_history": 0, "files": 0,
    }


@pytest.mark.parametrize("owner", ["startup", "quick"])
def test_close_waits_for_native_cleanup_before_resource_retirement(
    services: ApplicationServices,
    owner: str,
) -> None:
    entered, release, finished, close_started, close_done = (threading.Event() for _ in range(5))
    errors: list[BaseException] = []
    initial_pool = services.database.engine.pool
    if owner == "startup":
        expired = services.paths.temporary / "expired-fixture"
        expired.write_text("disposable retention fixture")
        old = utcnow() - timedelta(days=40)
        os.utime(expired, (old.timestamp(), old.timestamp()))
        with services.database.session() as session:
            session.add(
                AppSettingHistory(key="lifetime.fixture", revision=1, value_json={}, created_at=old)
            )
        native = startup.apply_retention
        target = startup
        attribute = "apply_retention"
        start = services.startup_maintenance.start
    else:
        directory = services.quick_transcriptions.root / str(uuid.uuid4())
        directory.mkdir()
        expired = directory / "orphaned-upload"
        expired.write_text("disposable crash-before-record fixture")
        native = services.quick_transcriptions.cleanup
        target = services.quick_transcriptions
        attribute = "cleanup"
        start = services.quick_transcriptions.start_maintenance

    def work(*args, **kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Fixture work release expired")
        assert not services.tts_providers._closed
        assert services.database.engine.pool is initial_pool
        result = native(*args, **kwargs)
        assert not expired.exists()
        finished.set()
        return result

    def close() -> None:
        close_started.set()
        try:
            services.close()
        except BaseException as error:
            errors.append(error)
        finally:
            close_done.set()

    with mock.patch.object(target, attribute, side_effect=work):
        start()
        assert entered.wait(5)
        thread = (
            services.startup_maintenance._periodic_thread
            if owner == "startup"
            else services.quick_transcriptions._thread
        )
        assert thread is not None
        original_join = thread.join

        def join(timeout: float | None = None) -> None:
            # Native before measurements retain the real two-second allowance.
            # This regression accelerates only that old finite timeout.
            original_join(timeout=0.01 if timeout == 2 else timeout)

        with mock.patch.object(thread, "join", side_effect=join):
            closer = threading.Thread(target=close)
            try:
                closer.start()
                assert close_started.wait(5)
                assert not close_done.wait(0.1)
                assert not services.tts_providers._closed
                assert services.database.engine.pool is initial_pool
                assert services.startup_maintenance._stop.is_set()
                assert services.quick_transcriptions._stop.is_set()
                assert expired.exists()
                release.set()
                closer.join(timeout=5)
                assert not closer.is_alive()
                assert close_done.is_set() and finished.is_set()
                assert errors == []
                assert services.tts_providers._closed
                assert services.database.engine.pool is not initial_pool
            finally:
                release.set()
                closer.join(timeout=5)
                assert not closer.is_alive()


@pytest.mark.parametrize("owner", ["startup", "quick"])
def test_bounded_stop_reports_incomplete_then_complete_work(
    services: ApplicationServices,
    owner: str,
) -> None:
    entered, release = threading.Event(), threading.Event()
    if owner == "startup":
        start = services.startup_maintenance.start
        stop = services.startup_maintenance.stop
        target, attribute = startup, "apply_retention"
    else:
        start = services.quick_transcriptions.start_maintenance
        stop = services.quick_transcriptions.stop_maintenance
        target, attribute = services.quick_transcriptions, "cleanup"

    def work(*_args, **_kwargs):
        entered.set()
        if not release.wait(5):
            raise RuntimeError("Fixture work release expired")
        return {}

    with mock.patch.object(target, attribute, side_effect=work):
        try:
            start()
            assert entered.wait(5)
            assert stop(timeout=0.01) is False
            release.set()
            assert stop(timeout=None) is True
            assert stop(timeout=0) is True
        finally:
            release.set()


@pytest.mark.parametrize("owner", ["startup", "quick"])
def test_maintenance_thread_can_request_stop_without_joining_itself(
    services: ApplicationServices,
    owner: str,
) -> None:
    results: list[bool] = []
    errors: list[BaseException] = []
    if owner == "startup":
        maintenance = services.startup_maintenance
        stop = maintenance.stop
    else:
        maintenance = services.quick_transcriptions
        stop = maintenance.stop_maintenance

    def request_stop() -> None:
        try:
            results.append(stop(timeout=None))
        except BaseException as error:
            errors.append(error)

    thread = MaintenanceThread(target=request_stop, name="test-self-stop")
    maintenance._thread = thread
    try:
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []
        assert results == [False]
        assert maintenance._stop.is_set()
        assert stop(timeout=None) is True
    finally:
        thread.join(timeout=5)
        assert not thread.is_alive()


def test_close_failure_still_requests_quick_stop_and_preserves_resources(
    services: ApplicationServices,
) -> None:
    initial_pool = services.database.engine.pool
    with (
        mock.patch.object(
            type(services.startup_maintenance), "stop", side_effect=RuntimeError("stop failed")
        ),
        mock.patch.object(
            services.quick_transcriptions,
            "stop_maintenance",
            wraps=services.quick_transcriptions.stop_maintenance,
        ) as quick,
    ):
        with pytest.raises(RuntimeError, match="stop failed"):
            services.close()
        assert quick.called
        assert services.quick_transcriptions._stop.is_set()
        assert not services.tts_providers._closed
        assert services.database.engine.pool is initial_pool


def test_close_refuses_resource_retirement_when_completion_is_unconfirmed(
    services: ApplicationServices,
) -> None:
    initial_pool = services.database.engine.pool
    with mock.patch.object(type(services.startup_maintenance), "stop", return_value=False):
        with pytest.raises(RuntimeError, match="maintenance"):
            services.close()
        assert not services.tts_providers._closed
        assert services.database.engine.pool is initial_pool


def test_provider_retirement_failure_still_disposes_database_after_maintenance(
    services: ApplicationServices,
) -> None:
    initial_pool = services.database.engine.pool
    with mock.patch.object(
        services.tts_providers, "close", side_effect=RuntimeError("provider close failed")
    ):
        with pytest.raises(RuntimeError, match="provider close failed"):
            services.close()
        assert services.startup_maintenance._stop.is_set()
        assert services.quick_transcriptions._stop.is_set()
        assert services.database.engine.pool is not initial_pool
