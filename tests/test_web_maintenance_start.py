"""Native resource retirement after maintenance start failures and admission races."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy.pool import Pool

from pandrator.web.application_services import ApplicationServices
from pandrator.web.maintenance_threads import MaintenanceThread
from pandrator.web.models import AppSetting
from tests.web_test_support import prepare_web_test_data_root

NAMES = {
    "initial": "pandrator-startup-maintenance",
    "periodic": "pandrator-session-purge-maintenance",
    "quick": "quick-transcription-cleanup",
}


@dataclass
class NativeResources:
    services: ApplicationServices
    connection: sqlite3.Connection
    pool: Pool

    def assert_live(self) -> None:
        assert self.connection.execute("SELECT 1").fetchone() == (1,)
        assert self.services.database.engine.pool is self.pool
        assert not self.services.tts_providers._closed

    def assert_retired(self) -> None:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            self.connection.execute("SELECT 1")
        assert self.services.database.engine.pool is not self.pool
        assert self.services.tts_providers._closed


@pytest.fixture
def resources(tmp_path: Path) -> Iterator[NativeResources]:
    prepare_web_test_data_root(tmp_path)
    services = ApplicationServices.build(data_root=tmp_path)
    with services.database.engine.connect() as checkout:
        checkout.exec_driver_sql("SELECT 1")
        connection = checkout.connection.driver_connection
        assert isinstance(connection, sqlite3.Connection)
    value = NativeResources(services, connection, services.database.engine.pool)
    try:
        yield value
    finally:
        services.startup_maintenance._stop.set()
        services.startup_maintenance._completed.set()
        services.quick_transcriptions._stop.set()
        for thread in (
            services.startup_maintenance._thread,
            services.startup_maintenance._periodic_thread,
            services.quick_transcriptions._thread,
        ):
            # Before-only injection explicitly skipped native start. No liveness
            # inference is used by the product to authorize resource retirement.
            if thread is not None and thread.ident is not None:
                thread.join(timeout=5)
                assert not thread.is_alive()
        services.tts_providers.close()
        services.database.dispose()
        services.manager_bridge.session.close()


def start_owner(services: ApplicationServices, owner: str) -> None:
    if owner == "quick":
        services.quick_transcriptions.start_maintenance()
    else:
        services.startup_maintenance.start()


@pytest.mark.parametrize("owner", NAMES)
@pytest.mark.parametrize("error_type", [RuntimeError, MemoryError, KeyboardInterrupt])
def test_pre_native_start_failure_can_retire_resources(
    resources: NativeResources, owner: str, error_type: type[BaseException]
) -> None:
    sentinel = error_type("injected pre-native start refusal")
    native_start = threading.Thread.start
    entered, release = threading.Event(), threading.Event()

    def first_work() -> dict[str, object]:
        entered.set()
        assert release.wait(5)
        return {}

    def start(thread: threading.Thread) -> None:
        if thread.name == NAMES[owner]:
            raise sentinel
        native_start(thread)

    with (
        mock.patch.object(
            type(resources.services.startup_maintenance), "run", side_effect=first_work
        ),
        mock.patch.object(threading.Thread, "start", start),
    ):
        try:
            with pytest.raises(error_type) as caught:
                start_owner(resources.services, owner)
            assert caught.value is sentinel
            if owner == "periodic":
                assert entered.wait(5)
            resources.assert_live()
            release.set()
            resources.services.close()
            resources.assert_retired()
        finally:
            release.set()


@pytest.mark.parametrize("owner", NAMES)
def test_post_native_start_failure_never_admits_work(
    resources: NativeResources, owner: str
) -> None:
    sentinel = MemoryError("injected post-native start failure")
    native_start = threading.Thread.start
    accessed, release = threading.Event(), threading.Event()

    def work() -> dict[str, object]:
        accessed.set()
        assert release.wait(5)
        resources.assert_live()
        return {}

    def start(thread: threading.Thread) -> None:
        native_start(thread)
        if thread.name == NAMES[owner]:
            raise sentinel

    maintenance = resources.services.startup_maintenance
    target, attribute = (
        (resources.services.quick_transcriptions, "cleanup")
        if owner == "quick"
        else (type(maintenance), "_periodic_purge" if owner == "periodic" else "run")
    )
    with (
        mock.patch.object(target, attribute, side_effect=work),
        mock.patch.object(threading.Thread, "start", start),
    ):
        try:
            with pytest.raises(MemoryError) as caught:
                start_owner(resources.services, owner)
            assert caught.value is sentinel
            assert not accessed.wait(0.05)
            resources.services.close()
            resources.assert_retired()
            thread = (
                resources.services.quick_transcriptions._thread
                if owner == "quick"
                else (maintenance._periodic_thread if owner == "periodic" else maintenance._thread)
            )
            assert thread is not None
            thread.join(timeout=0.1)
            if thread.is_alive():
                # A defective callback must be released before the final verdict.
                release.set()
                thread.join(timeout=5)
            assert not thread.is_alive()
            assert not accessed.is_set()
        finally:
            release.set()


@pytest.mark.parametrize("owner", NAMES)
def test_late_wrapper_model_cannot_access_retired_resources(
    resources: NativeResources, owner: str
) -> None:
    # A separate native driver models delayed callback delivery before the
    # helper's identity publication; this is not an allocator/bootstrap fault.
    native_start = threading.Thread.start
    sentinel = MemoryError("injected unpublished-wrapper start failure")
    release, accessed = threading.Event(), threading.Event()
    drivers: list[threading.Thread] = []
    wrappers: list[threading.Thread] = []

    def work() -> dict[str, object]:
        accessed.set()
        resources.assert_live()
        return {}

    def start(thread: threading.Thread) -> None:
        if thread.name != NAMES[owner]:
            native_start(thread)
            return
        wrappers.append(thread)

        def late_run() -> None:
            assert release.wait(5)
            thread.run()

        driver = threading.Thread(target=late_run, name="test-late-wrapper-driver")
        drivers.append(driver)
        native_start(driver)
        raise sentinel

    maintenance = resources.services.startup_maintenance
    target, attribute = (
        (resources.services.quick_transcriptions, "cleanup")
        if owner == "quick"
        else (type(maintenance), "_periodic_purge" if owner == "periodic" else "run")
    )
    with (
        mock.patch.object(target, attribute, side_effect=work),
        mock.patch.object(threading.Thread, "start", start),
    ):
        try:
            with pytest.raises(MemoryError) as caught:
                start_owner(resources.services, owner)
            assert caught.value is sentinel
            assert wrappers[0].ident is None and not wrappers[0].is_alive()
            assert drivers[0].is_alive()
            resources.services.close()
            resources.assert_retired()
            release.set()
            drivers[0].join(timeout=5)
            assert not drivers[0].is_alive()
            assert not accessed.is_set()
        finally:
            release.set()
            for driver in drivers:
                driver.join(timeout=5)
                assert not driver.is_alive()


@pytest.mark.parametrize("owner", ["initial", "quick"])
def test_after_admission_exception_retains_running_native_writer(
    resources: NativeResources, owner: str
) -> None:
    entered, release, close_started, close_done = (threading.Event() for _ in range(4))
    errors: list[BaseException] = []
    sentinel = MemoryError("injected failure after callback admission")
    original_admit = MaintenanceThread._admit_work

    def work() -> dict[str, object]:
        entered.set()
        assert release.wait(5)
        resources.assert_live()
        with resources.services.database.session() as session:
            session.add(AppSetting(key="maintenance.start.fixture", value_json={"written": True}))
        return {}

    def admit(thread: MaintenanceThread) -> None:
        original_admit(thread)
        if thread.name == NAMES[owner]:
            assert entered.wait(5)
            raise sentinel

    def close() -> None:
        close_started.set()
        try:
            resources.services.close()
        except BaseException as error:
            errors.append(error)
        finally:
            close_done.set()

    target, attribute = (
        (resources.services.quick_transcriptions, "cleanup")
        if owner == "quick"
        else (type(resources.services.startup_maintenance), "run")
    )
    closer = threading.Thread(target=close, name="test-native-resource-closer")
    with (
        mock.patch.object(target, attribute, side_effect=work),
        mock.patch.object(MaintenanceThread, "_admit_work", admit),
    ):
        try:
            with pytest.raises(MemoryError) as caught:
                start_owner(resources.services, owner)
            assert caught.value is sentinel
            closer.start()
            assert close_started.wait(5)
            assert not close_done.wait(0.05)
            resources.assert_live()
            release.set()
            closer.join(timeout=5)
            assert not closer.is_alive()
            assert close_done.is_set() and errors == []
            resources.assert_retired()
            # Independent SQLite observer avoids reopening the retired pool.
            with closing(sqlite3.connect(resources.services.paths.database)) as observer:
                row = observer.execute(
                    "SELECT value_json FROM app_settings WHERE key = ?",
                    ("maintenance.start.fixture",),
                ).fetchone()
                assert row is not None and "true" in row[0]
        finally:
            release.set()
            if closer.ident is not None:
                closer.join(timeout=5)
                assert not closer.is_alive()


def test_cancelled_initial_work_wakes_actual_periodic_waiter(
    resources: NativeResources,
) -> None:
    deferred, release, periodic_entered, accessed = (threading.Event() for _ in range(4))
    original_run = MaintenanceThread._run_work
    maintenance = resources.services.startup_maintenance
    original_wait = maintenance._completed.wait

    def run_wrapper(thread: MaintenanceThread) -> None:
        if thread.name == NAMES["initial"]:
            deferred.set()
            assert release.wait(5)
        original_run(thread)

    def wait() -> bool:
        periodic_entered.set()
        return original_wait()

    def initial_work() -> dict[str, object]:
        accessed.set()
        resources.assert_live()
        return {}

    with (
        mock.patch.object(MaintenanceThread, "_run_work", run_wrapper),
        mock.patch.object(maintenance._completed, "wait", wait),
        mock.patch.object(type(maintenance), "run", side_effect=initial_work),
    ):
        try:
            maintenance.start()
            assert deferred.wait(5) and periodic_entered.wait(5)
            assert not maintenance._completed.is_set()
            assert maintenance.stop(timeout=1) is True
            assert maintenance._completed.is_set()
            assert maintenance._periodic_thread is not None
            assert not maintenance._periodic_thread.is_alive()
            assert maintenance._thread is not None and maintenance._thread.is_alive()
            resources.services.close()
            resources.assert_retired()
            release.set()
            maintenance._thread.join(timeout=5)
            assert not maintenance._thread.is_alive() and not accessed.is_set()
        finally:
            release.set()


def test_repeated_start_preserves_running_callback_and_bounded_finish() -> None:
    entered, release = threading.Event(), threading.Event()
    calls: list[int] = []

    def work() -> None:
        calls.append(1)
        entered.set()
        assert release.wait(5)

    thread = MaintenanceThread(target=work, name="test-repeated-maintenance-start")
    try:
        thread.start()
        assert entered.wait(5)
        with pytest.raises(RuntimeError, match="threads can only be started once"):
            thread.start()
        assert thread.finish(timeout=0.01) is False
        release.set()
        assert thread.finish(timeout=5) is True
        assert thread.finish(timeout=0) is True
        assert calls == [1]
    finally:
        release.set()
        thread.join(timeout=5)
        assert not thread.is_alive()


@pytest.mark.parametrize("owner", ["initial", "quick"])
def test_native_join_failure_preserves_resources(resources: NativeResources, owner: str) -> None:
    entered, release = threading.Event(), threading.Event()
    sentinel = RuntimeError("injected native join failure")

    def work() -> dict[str, object]:
        entered.set()
        assert release.wait(5)
        return {}

    target, attribute = (
        (resources.services.quick_transcriptions, "cleanup")
        if owner == "quick"
        else (type(resources.services.startup_maintenance), "run")
    )
    with mock.patch.object(target, attribute, side_effect=work):
        try:
            start_owner(resources.services, owner)
            assert entered.wait(5)
            thread = (
                resources.services.quick_transcriptions._thread
                if owner == "quick"
                else resources.services.startup_maintenance._thread
            )
            assert thread is not None
            with mock.patch.object(thread, "join", side_effect=sentinel):
                with pytest.raises(RuntimeError) as caught:
                    resources.services.close()
                assert caught.value is sentinel
                resources.assert_live()
            release.set()
            resources.services.close()
            resources.assert_retired()
        finally:
            release.set()
