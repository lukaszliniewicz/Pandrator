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

from pandrator.web import startup
from pandrator.web.application_services import ApplicationServices
from pandrator.web.models import AppSettingHistory, utcnow
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

    thread = threading.Thread(target=request_stop)
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
