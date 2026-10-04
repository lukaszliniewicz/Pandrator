"""Default manager bridge owners retire native sessions without retiring borrowers."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from unittest import mock

import pytest
from sqlalchemy.pool import Pool

from pandrator.runtime import DataPaths
from pandrator.web.credentials import hydrate_tts_settings
from pandrator.web.database import Database
from pandrator.web.manager_proxy import LocalManagerProxy, ManagerConnection, ManagerProxyError
from pandrator.web.tts_catalogue_service import TtsCatalogueService
from pandrator.web.tts_providers import TtsProviderRegistry
from tests.test_web_manager_proxy_lifetime import LocalHttp
from tests.test_web_manager_proxy_lifetime import local_http as manager_http_fixture
from tests.web_test_support import prepare_web_test_data_root

manager_http = manager_http_fixture

EXTERNAL = {
    "service": "XTTS",
    "provider_configs": [
        {"id": "xtts", "connection_mode": "external", "api_base": "http://fixture.invalid"}
    ],
}


@dataclass
class BorrowedResources:
    database: Database
    paths: DataPaths
    providers: TtsProviderRegistry
    connection: sqlite3.Connection
    pool: Pool

    def assert_live(self) -> None:
        assert self.connection.execute("SELECT 1").fetchone() == (1,)
        assert self.database.engine.pool is self.pool
        assert not self.providers._closed


@pytest.fixture
def borrowed(tmp_path: Path) -> Iterator[BorrowedResources]:
    paths = prepare_web_test_data_root(tmp_path)
    database = Database(paths.database)
    providers = TtsProviderRegistry()
    with database.engine.connect() as checkout:
        checkout.exec_driver_sql("SELECT 1")
        connection = checkout.connection.driver_connection
        assert isinstance(connection, sqlite3.Connection)
    try:
        yield BorrowedResources(database, paths, providers, connection, database.engine.pool)
    finally:
        providers.close()
        database.dispose()


def test_default_hydration_retires_native_manager_session(borrowed: BorrowedResources) -> None:
    proxy = LocalManagerProxy()
    try:
        with (
            mock.patch(
                "pandrator.web.manager_proxy.LocalManagerProxy", return_value=proxy
            ) as factory,
            mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close,
        ):
            result = hydrate_tts_settings(borrowed.database, borrowed.paths, EXTERNAL)
            assert result == EXTERNAL
            factory.assert_called_once_with()
            assert close.call_count == 1
            assert proxy._closed
            borrowed.assert_live()
    finally:
        proxy.session.close()


def test_default_catalogue_close_retires_native_manager_only(borrowed: BorrowedResources) -> None:
    catalogue = TtsCatalogueService(borrowed.database, borrowed.paths, borrowed.providers)
    proxy = catalogue.manager_bridge
    try:
        with mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close:
            catalogue.close()
            catalogue.close()
            assert close.call_count == 1
            assert proxy._closed
            borrowed.assert_live()
    finally:
        proxy.session.close()


@pytest.mark.parametrize("error_type", [RuntimeError, KeyboardInterrupt])
def test_default_hydration_retires_manager_on_connection_mode_exception(
    borrowed: BorrowedResources, error_type: type[BaseException]
) -> None:
    proxy = LocalManagerProxy()
    sentinel = error_type("injected connection mode selection failure")
    try:
        with (
            mock.patch("pandrator.web.manager_proxy.LocalManagerProxy", return_value=proxy),
            mock.patch(
                "pandrator.web.credentials.effective_tts_connection_mode", side_effect=sentinel
            ),
            mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close,
        ):
            with pytest.raises(error_type) as caught:
                hydrate_tts_settings(borrowed.database, borrowed.paths, EXTERNAL)
            assert caught.value is sentinel
            assert close.call_count == 1 and proxy._closed
            borrowed.assert_live()
    finally:
        proxy.session.close()


def test_no_selection_returns_before_allocating_manager(borrowed: BorrowedResources) -> None:
    settings = {"service": "missing-fixture-service"}
    with mock.patch("pandrator.web.manager_proxy.LocalManagerProxy") as factory:
        result = hydrate_tts_settings(borrowed.database, borrowed.paths, settings)
        assert result == settings and result is not settings
        factory.assert_not_called()
        borrowed.assert_live()


class FalseyManagerProxy(LocalManagerProxy):
    def __bool__(self) -> bool:
        return False


def test_supplied_falsey_native_bridge_is_retained_and_not_retired(
    borrowed: BorrowedResources,
) -> None:
    proxy = FalseyManagerProxy()
    try:
        with (
            mock.patch("pandrator.web.manager_proxy.LocalManagerProxy") as factory,
            mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close,
        ):
            result = hydrate_tts_settings(
                borrowed.database, borrowed.paths, EXTERNAL, manager_bridge=proxy
            )
            assert result == EXTERNAL
            factory.assert_not_called()
            assert close.call_count == 0 and not proxy._closed
            borrowed.assert_live()
    finally:
        proxy.close()
        proxy.session.close()


@pytest.mark.parametrize("invalid_json", [False, True])
def test_default_managed_hydration_uses_native_loopback_and_retires_session(
    borrowed: BorrowedResources,
    manager_http: LocalHttp,
    invalid_json: bool,
) -> None:
    # Reused native local HTTP fixture; no Manager API/provider acceptance claim.
    endpoint = "http://127.0.0.1:9123"
    manager_http.body = (
        b"invalid fixture JSON"
        if invalid_json
        else b'{"id":"tts.xtts","endpoint":"http://127.0.0.1:9123/","health":{"state":"healthy"}}'
    )
    settings = {
        "service": "XTTS",
        "preview_service_id": "xtts",
        "provider_configs": [
            {"id": "xtts", "connection_mode": "managed_local", "managed_service_id": "tts.xtts"}
        ],
    }
    proxy = LocalManagerProxy()
    try:
        with (
            mock.patch(
                "pandrator.web.manager_proxy.LocalManagerProxy", return_value=proxy
            ) as factory,
            mock.patch.object(
                proxy,
                "discover",
                return_value=ManagerConnection(manager_http.url, "fixture", "fixture-secret"),
            ) as discover,
            mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close,
        ):
            if invalid_json:
                with pytest.raises(RuntimeError) as caught:
                    hydrate_tts_settings(borrowed.database, borrowed.paths, settings)
                assert (
                    str(caught.value)
                    == "The managed local XTTS service is unavailable: Pandrator Manager returned an invalid response."
                )
                assert isinstance(caught.value.__cause__, ManagerProxyError)
                assert caught.value.__cause__.code == "manager_invalid_response"
            else:
                result = hydrate_tts_settings(borrowed.database, borrowed.paths, settings)
                assert result == {
                    "service": "XTTS",
                    "preview_service_id": "xtts",
                    "xtts_base_url": endpoint,
                    "preview_api_base": endpoint,
                    "provider_configs": [
                        {
                            "id": "xtts",
                            "connection_mode": "managed_local",
                            "managed_service_id": "tts.xtts",
                            "api_base": endpoint,
                        }
                    ],
                }
                assert "xtts_base_url" not in settings
            factory.assert_called_once_with()
            discover.assert_called_once_with()
            assert close.call_count == 1 and proxy._closed
            with pytest.raises(RuntimeError, match=r"^Manager proxy is closed\.$"):
                proxy.request_json("GET", "/v1/health")
            assert discover.call_count == 1
            borrowed.assert_live()
    finally:
        proxy.session.close()


def test_catalogue_borrowed_falsey_bridge_remains_live(borrowed: BorrowedResources) -> None:
    proxy = FalseyManagerProxy()
    try:
        with (
            mock.patch("pandrator.web.tts_catalogue_service.LocalManagerProxy") as factory,
            mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close,
        ):
            catalogue = TtsCatalogueService(
                borrowed.database, borrowed.paths, borrowed.providers, manager_bridge=proxy
            )
            assert catalogue.manager_bridge is proxy
            catalogue.close()
            catalogue.close()
            factory.assert_not_called()
            assert close.call_count == 0 and not proxy._closed
            borrowed.assert_live()
    finally:
        proxy.close()
        proxy.session.close()


def test_catalogue_owned_retirement_failure_retries_without_retiring_borrowers(
    borrowed: BorrowedResources,
) -> None:
    catalogue = TtsCatalogueService(borrowed.database, borrowed.paths, borrowed.providers)
    proxy = catalogue.manager_bridge
    sentinel = RuntimeError("injected native Session.close failure")
    native_close = proxy.session.close
    calls: list[int] = []

    def close() -> None:
        calls.append(1)
        if len(calls) == 1:
            raise sentinel
        native_close()

    try:
        with mock.patch.object(proxy.session, "close", side_effect=close):
            with pytest.raises(RuntimeError) as caught:
                catalogue.close()
            assert caught.value is sentinel
            assert proxy._closed and not proxy._session_retired
            borrowed.assert_live()
            catalogue.close()
            assert proxy._session_retired
            borrowed.assert_live()
            catalogue.close()
            assert calls == [1, 1]
            borrowed.assert_live()
    finally:
        native_close()
