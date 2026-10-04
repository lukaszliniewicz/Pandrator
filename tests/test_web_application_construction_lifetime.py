"""Native factory owners retire on failure and transfer only on successful return."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest

from pandrator.web import api, application_services
from pandrator.web.application_services import ApplicationServices
from pandrator.web.database import Database
from pandrator.web.manager_proxy import LocalManagerProxy
from pandrator.web.models import AppSetting
from pandrator.web.startup import StartupMaintenance
from pandrator.web.tts_providers import TtsProviderRegistry
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class DatabaseWitness:
    owner: Database
    handle: sqlite3.Connection
    pool: object
    dispose_calls: int = 0


@dataclass
class ManagerWitness:
    owner: LocalManagerProxy
    session_close_calls: int = 0


@dataclass
class RegistryWitness:
    owner: TtsProviderRegistry
    close_calls: int = 0


class NativeOwners:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = root
        self.databases: list[DatabaseWitness] = []
        self.managers: list[ManagerWitness] = []
        self.registries: list[RegistryWitness] = []
        self.carriers: list[ApplicationServices] = []
        self.retirement_order: list[str] = []
        self.registry_close_error: BaseException | None = None
        native_build = ApplicationServices.build.__func__

        def database(*args, **kwargs):
            owner = Database(*args, **kwargs)
            with owner.engine.connect() as connection:
                assert connection.exec_driver_sql("SELECT 1").scalar_one() == 1
                handle = connection.connection.driver_connection
                assert isinstance(handle, sqlite3.Connection)
            witness = DatabaseWitness(owner, handle, owner.engine.pool)
            self.databases.append(witness)
            native_dispose = owner.dispose

            def dispose() -> None:
                witness.dispose_calls += 1
                self.retirement_order.append("database")
                native_dispose()

            monkeypatch.setattr(owner, "dispose", dispose)
            return owner

        def manager(*args, **kwargs):
            owner = LocalManagerProxy(*args, **kwargs)
            witness = ManagerWitness(owner)
            self.managers.append(witness)
            native_close = owner.session.close

            def close() -> None:
                witness.session_close_calls += 1
                self.retirement_order.append("manager")
                native_close()

            monkeypatch.setattr(owner.session, "close", close)
            return owner

        def registry(*args, **kwargs):
            owner = TtsProviderRegistry(*args, **kwargs)
            witness = RegistryWitness(owner)
            self.registries.append(witness)
            native_close = owner.close

            def close() -> None:
                witness.close_calls += 1
                self.retirement_order.append("registry")
                if self.registry_close_error is not None:
                    raise self.registry_close_error
                native_close()

            monkeypatch.setattr(owner, "close", close)
            return owner

        def build(cls, **kwargs):
            owner = native_build(cls, **kwargs)
            self.carriers.append(owner)
            return owner

        monkeypatch.delenv("PANDRATOR_MANAGER_DESCRIPTOR", raising=False)
        monkeypatch.delenv("PANDRATOR_MANAGER_CREDENTIAL", raising=False)
        monkeypatch.setattr(application_services, "Database", database)
        monkeypatch.setattr(application_services, "LocalManagerProxy", manager)
        monkeypatch.setattr(application_services, "TtsProviderRegistry", registry)
        monkeypatch.setattr(ApplicationServices, "build", classmethod(build))

    def assert_live(self) -> None:
        for witness in self.databases:
            assert witness.handle.execute("SELECT 1").fetchone() == (1,)
            assert witness.owner.engine.pool is witness.pool
            assert witness.dispose_calls == 0
        for witness in self.managers:
            assert witness.session_close_calls == 0
        for witness in self.registries:
            assert not witness.owner._closed
            assert witness.owner.service_ids()
            assert witness.close_calls == 0

    def assert_retired(self) -> None:
        for witness in self.databases:
            with pytest.raises(sqlite3.ProgrammingError):
                witness.handle.execute("SELECT 1")
            assert witness.owner.engine.pool is not witness.pool
            assert witness.dispose_calls == 1
        for witness in self.managers:
            assert witness.session_close_calls == 1
        for witness in self.registries:
            assert witness.owner._closed
            assert witness.owner.service_ids() == ()
            assert witness.close_calls == 1

    def teardown(self) -> None:
        self.registry_close_error = None
        for owner in self.carriers:
            assert owner.startup_maintenance.stop(timeout=5)
            assert owner.quick_transcriptions.stop_maintenance(timeout=5)
            for thread in (
                owner.startup_maintenance._thread,
                owner.startup_maintenance._periodic_thread,
                owner.quick_transcriptions._thread,
                owner.workflow_handlers.quick_transcriptions._thread,
            ):
                if thread is not None and thread.ident is not None:
                    thread.join(timeout=5)
                    assert not thread.is_alive()
        try:
            for witness in self.registries:
                witness.owner.close()
                assert witness.owner._closed
        finally:
            try:
                for witness in self.managers:
                    witness.owner.close()
            finally:
                for witness in self.databases:
                    witness.owner.dispose()
                    with pytest.raises(sqlite3.ProgrammingError):
                        witness.handle.execute("SELECT 1")


@pytest.fixture
def owners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[NativeOwners]:
    prepare_web_test_data_root(tmp_path)
    observed = NativeOwners(tmp_path, monkeypatch)
    try:
        yield observed
    finally:
        observed.teardown()


@pytest.mark.parametrize("cut", ["preferences", "registry", "handlers", "carrier"])
def test_build_failure_retires_only_returned_native_owners(owners: NativeOwners, cut: str) -> None:
    error = RuntimeError("controlled build failure")

    def refuse(*_args, **_kwargs):
        raise error

    if cut == "carrier":

        class RefusingCarrier(ApplicationServices):
            def __init__(self, **_kwargs):
                raise error

        with pytest.raises(RuntimeError) as caught:
            RefusingCarrier.build(data_root=owners.root)
    else:
        binding = {
            "preferences": "crispasr_install_preferences",
            "registry": "TtsProviderRegistry",
            "handlers": "WorkflowHandlers",
        }[cut]
        with mock.patch.object(application_services, binding, refuse):
            with pytest.raises(RuntimeError) as caught:
                ApplicationServices.build(data_root=owners.root)
    assert caught.value is error
    assert owners.carriers == []
    assert len(owners.databases) == 1
    assert len(owners.managers) == (0 if cut == "preferences" else 1)
    assert len(owners.registries) == (1 if cut in {"handlers", "carrier"} else 0)
    owners.assert_retired()
    expected = ["database"] if cut == "preferences" else ["manager", "database"]
    if cut in {"handlers", "carrier"}:
        expected.insert(0, "registry")
    assert owners.retirement_order == expected


def test_build_keyboard_interrupt_retires_native_owners(owners: NativeOwners) -> None:
    error = KeyboardInterrupt("controlled build interruption")
    with mock.patch.object(application_services, "WorkflowHandlers", side_effect=error):
        with pytest.raises(KeyboardInterrupt) as caught:
            ApplicationServices.build(data_root=owners.root)
    assert caught.value is error
    assert len(owners.databases) == len(owners.managers) == len(owners.registries) == 1
    assert owners.carriers == []
    owners.assert_retired()
    assert owners.retirement_order == ["registry", "manager", "database"]


def test_database_constructor_refusal_has_no_returned_owner_cleanup(owners: NativeOwners) -> None:
    error = RuntimeError("controlled pre-constructor refusal")
    with mock.patch.object(application_services, "Database", side_effect=error):
        with pytest.raises(RuntimeError) as caught:
            ApplicationServices.build(data_root=owners.root)
    assert caught.value is error
    assert owners.databases == owners.managers == owners.registries == owners.carriers == []
    assert owners.retirement_order == []


@pytest.mark.parametrize("cut", ["reconcile", "guards", "routes", "initial_start"])
def test_post_build_api_failure_retires_native_carrier(owners: NativeOwners, cut: str) -> None:
    error = RuntimeError("controlled post-build failure")
    native_start = threading.Thread.start

    def selected_start(thread):
        if thread.name == "pandrator-startup-maintenance":
            raise error
        return native_start(thread)

    if cut == "reconcile":
        patch = mock.patch.object(application_services.JobQueue, "reconcile", side_effect=error)
    elif cut == "guards":
        patch = mock.patch.object(api.ApiGuards, "register", side_effect=error)
    elif cut == "routes":
        patch = mock.patch.object(api, "register_routes", side_effect=error)
    else:
        patch = mock.patch.object(threading.Thread, "start", selected_start)
    with patch:
        with pytest.raises(RuntimeError) as caught:
            api.create_app(
                data_root=owners.root, testing=True, background_maintenance=cut == "initial_start"
            )
    assert caught.value is error
    assert len(owners.carriers) == 1
    assert len(owners.databases) == len(owners.managers) == len(owners.registries) == 1
    owners.assert_retired()
    assert owners.retirement_order == ["registry", "manager", "database"]


def test_post_build_keyboard_interrupt_retires_native_carrier(owners: NativeOwners) -> None:
    error = KeyboardInterrupt("controlled registration interruption")
    with mock.patch.object(api.ApiGuards, "register", side_effect=error):
        with pytest.raises(KeyboardInterrupt) as caught:
            api.create_app(data_root=owners.root, testing=True, background_maintenance=False)
    assert caught.value is error
    assert len(owners.carriers) == 1
    owners.assert_retired()


@pytest.mark.parametrize(
    "failed_name", ["pandrator-session-purge-maintenance", "quick-transcription-cleanup"]
)
def test_partial_start_waits_for_native_initial_write_before_factory_failure(
    owners: NativeOwners, failed_name: str
) -> None:
    entered, release, callback_finished, fault, factory_finished = (
        threading.Event() for _ in range(5)
    )
    error = RuntimeError("controlled partial startup failure")
    errors: list[BaseException] = []
    returned_apps: list[object] = []
    native_start = threading.Thread.start
    key = "factory.lifetime.fixture"

    def initial_work(owner: StartupMaintenance):
        entered.set()
        try:
            assert release.wait(5)
            with owner.database.session() as session:
                session.add(AppSetting(key=key, value_json={"committed": True}))
            callback_finished.set()
            return {}
        finally:
            owner._completed.set()

    def selected_start(thread):
        if thread.name == failed_name:
            assert entered.wait(5)
            fault.set()
            raise error
        return native_start(thread)

    def construct() -> None:
        try:
            returned_apps.append(
                api.create_app(data_root=owners.root, testing=True, background_maintenance=True)
            )
        except BaseException as caught:
            errors.append(caught)
        finally:
            factory_finished.set()

    factory = threading.Thread(target=construct, name="factory-lifetime-fixture")
    with (
        mock.patch.object(StartupMaintenance, "run", initial_work),
        mock.patch.object(threading.Thread, "start", selected_start),
    ):
        try:
            factory.start()
            assert fault.wait(5)
            assert entered.is_set()
            assert not factory_finished.wait(0.1)
            assert len(owners.carriers) == 1
            initial = owners.carriers[0].startup_maintenance._thread
            assert initial is not None and initial.ident is not None and initial.is_alive()
            owners.assert_live()
            release.set()
            assert factory_finished.wait(5)
            factory.join(timeout=5)
            assert not factory.is_alive()
            assert callback_finished.is_set()
            assert errors == [error]
            assert returned_apps == []
            owners.assert_retired()
            with closing(sqlite3.connect(owners.databases[0].owner.path)) as connection:
                row = connection.execute(
                    "SELECT value_json FROM app_settings WHERE key = ?", (key,)
                ).fetchone()
                assert row is not None and json.loads(row[0]) == {"committed": True}
        finally:
            release.set()
            if factory.ident is not None:
                factory.join(timeout=5)
                assert not factory.is_alive()


@pytest.mark.parametrize("factory", ["build", "app"])
def test_success_transfers_native_resources_to_caller(owners: NativeOwners, factory: str) -> None:
    if factory == "build":
        carrier = ApplicationServices.build(data_root=owners.root)
    else:
        app = api.create_app(data_root=owners.root, testing=True, background_maintenance=False)
        carrier = app.extensions["pandrator"]["services"]
    assert owners.carriers == [carrier]
    assert len(owners.databases) == len(owners.managers) == len(owners.registries) == 1
    owners.assert_live()
    assert owners.retirement_order == []
    carrier.close()
    owners.assert_retired()
    assert owners.retirement_order == ["registry", "manager", "database"]


def test_build_cleanup_failure_keeps_construction_error_and_attempts_other_owners(
    owners: NativeOwners,
) -> None:
    original = RuntimeError("controlled construction failure")
    cleanup = RuntimeError("controlled registry retirement failure")
    owners.registry_close_error = cleanup
    with mock.patch.object(application_services, "WorkflowHandlers", side_effect=original):
        with pytest.raises(RuntimeError) as caught:
            ApplicationServices.build(data_root=owners.root)
    assert caught.value is cleanup
    assert caught.value.__context__ is original
    assert owners.retirement_order == ["registry", "manager", "database"]
    assert len(owners.registries) == 1 and not owners.registries[0].owner._closed
    assert owners.registries[0].close_calls == 1
    assert owners.managers[0].session_close_calls == 1
    database = owners.databases[0]
    assert database.dispose_calls == 1
    assert database.owner.engine.pool is not database.pool
    with pytest.raises(sqlite3.ProgrammingError):
        database.handle.execute("SELECT 1")


def test_api_cleanup_failure_keeps_construction_error_and_unconfirmed_resources(
    owners: NativeOwners,
) -> None:
    original = RuntimeError("controlled guard registration failure")
    cleanup = RuntimeError("controlled carrier retirement failure")
    with (
        mock.patch.object(api.ApiGuards, "register", side_effect=original),
        mock.patch.object(ApplicationServices, "close", side_effect=cleanup),
    ):
        with pytest.raises(RuntimeError) as caught:
            api.create_app(data_root=owners.root, testing=True, background_maintenance=False)
    assert caught.value is cleanup
    assert caught.value.__context__ is original
    assert len(owners.carriers) == 1
    owners.assert_live()
    assert owners.retirement_order == []
