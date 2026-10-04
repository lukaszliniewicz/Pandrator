"""Native local HTTP and graph resource ownership for the manager bridge."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from unittest import mock

import pytest
import requests
from sqlalchemy.pool import Pool

from pandrator.web.application_services import ApplicationServices
from pandrator.web.manager_proxy import LocalManagerProxy, ManagerConnection, ManagerProxyError
from tests.web_test_support import prepare_web_test_data_root


@dataclass
class NativeGraph:
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
def graph(tmp_path: Path) -> Iterator[NativeGraph]:
    prepare_web_test_data_root(tmp_path)
    services = ApplicationServices.build(data_root=tmp_path)
    with services.database.engine.connect() as checkout:
        checkout.exec_driver_sql("SELECT 1")
        connection = checkout.connection.driver_connection
        assert isinstance(connection, sqlite3.Connection)
    try:
        yield NativeGraph(services, connection, services.database.engine.pool)
    finally:
        services.startup_maintenance.stop(timeout=None)
        services.quick_transcriptions.stop_maintenance(timeout=None)
        services.tts_providers.close()
        services.manager_bridge.session.close()
        services.database.dispose()


def test_native_carrier_close_retires_default_manager_session_once(graph: NativeGraph) -> None:
    session = graph.services.manager_bridge.session
    with mock.patch.object(session, "close", wraps=session.close) as close:
        graph.services.close()
        graph.assert_retired()
        assert close.call_count == 1
        graph.services.close()
        assert close.call_count == 1


@dataclass
class LocalHttp:
    url: str
    release: threading.Event
    body: bytes = b'{"instance_id":"fixture","ok":true}'


@pytest.fixture
def local_http() -> Iterator[LocalHttp]:
    release = threading.Event()
    release.set()
    value = LocalHttp("", release)
    errors: list[BaseException] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            try:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(value.body)))
                self.send_header("X-Pandrator-Manager-Instance", "fixture")
                self.end_headers()
                self.wfile.flush()
                if not release.wait(5):
                    raise RuntimeError("Fixture HTTP body release expired")
                self.wfile.write(value.body)
                self.wfile.flush()
            except BaseException as error:
                errors.append(error)

        def log_message(self, format: str, *args: Any) -> None:
            del format, args

    server = HTTPServer(("127.0.0.1", 0), Handler)
    value.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, name="test-local-manager-http")
    thread.start()
    try:
        yield value
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        assert not thread.is_alive()
        assert errors == []


def assert_terminal(proxy: LocalManagerProxy) -> None:
    with (
        mock.patch.object(proxy, "discover") as discover,
        mock.patch.object(proxy.session, "request") as request,
    ):
        with pytest.raises(RuntimeError, match=r"^Manager proxy is closed\.$"):
            proxy.request_json("GET", "/v1/health")
        discover.assert_not_called()
        request.assert_not_called()
        # Existing path validation still precedes the terminal-state guard.
        with pytest.raises(ValueError, match="allowlisted v1 resources"):
            proxy.request_json("GET", "/invalid")
        discover.assert_not_called()
        request.assert_not_called()


def test_owned_proxy_close_is_idempotent_and_terminal() -> None:
    proxy = LocalManagerProxy()
    try:
        with mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as close:
            proxy.close()
            proxy.close()
            assert close.call_count == 1
        assert_terminal(proxy)
    finally:
        proxy.session.close()


@pytest.mark.parametrize("falsey", [False, True])
def test_borrowed_native_session_stays_caller_owned(local_http: LocalHttp, falsey: bool) -> None:
    class FalseySession(requests.Session):
        def __bool__(self) -> bool:
            return False

    caller = FalseySession() if falsey else requests.Session()
    proxy = LocalManagerProxy(session=caller)
    try:
        assert proxy.session is caller
        with mock.patch.object(caller, "close", wraps=caller.close) as close:
            proxy.close()
            proxy.close()
            assert close.call_count == 0
            assert_terminal(proxy)
            with caller.get(local_http.url + "/v1/health", timeout=5) as response:
                assert response.json() == {"instance_id": "fixture", "ok": True}
    finally:
        caller.close()
        if proxy.session is not caller:
            proxy.session.close()


def test_failed_owned_session_retirement_retries_but_requests_stay_terminal() -> None:
    proxy = LocalManagerProxy()
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
                proxy.close()
            assert caught.value is sentinel
            assert_terminal(proxy)
            proxy.close()
            proxy.close()
            assert calls == [1, 1]
    finally:
        native_close()


@pytest.mark.parametrize("borrowed", [False, True])
def test_close_waits_for_real_streamed_response_body(local_http: LocalHttp, borrowed: bool) -> None:
    # Local HTTP/native requests lifetime evidence, not Manager API acceptance.
    caller = requests.Session() if borrowed else None
    proxy = LocalManagerProxy(session=caller)
    session = proxy.session
    session.stream = True
    local_http.release.clear()
    entered, close_started, close_done = (threading.Event() for _ in range(3))
    responses: list[requests.Response] = []
    results: list[tuple[dict[str, Any], int]] = []
    errors: list[BaseException] = []
    response_closes: list[int] = []
    native_request = session.request
    native_session_close = session.close

    def request(*args: Any, **kwargs: Any) -> requests.Response:
        response = native_request(*args, **kwargs)
        responses.append(response)
        native_json, native_close = response.json, response.close

        def json_body(**json_kwargs: Any) -> Any:
            entered.set()
            return native_json(**json_kwargs)

        def close_response() -> None:
            response_closes.append(1)
            native_close()

        response.json = json_body
        response.close = close_response
        return response

    def client() -> None:
        try:
            results.append(proxy.request_json("GET", "/v1/health", timeout=5))
        except BaseException as error:
            errors.append(error)

    def close() -> None:
        close_started.set()
        try:
            proxy.close()
        except BaseException as error:
            errors.append(error)
        finally:
            close_done.set()

    client_thread = threading.Thread(target=client, name="test-streamed-manager-client")
    closer = threading.Thread(target=close, name="test-streamed-manager-closer")
    with (
        mock.patch.object(
            proxy,
            "discover",
            return_value=ManagerConnection(local_http.url, "fixture", "fixture-secret"),
        ),
        mock.patch.object(session, "request", side_effect=request),
        mock.patch.object(session, "close", wraps=native_session_close) as session_close,
    ):
        try:
            client_thread.start()
            assert entered.wait(5)
            assert not responses[0]._content_consumed
            assert not responses[0].raw.closed
            closer.start()
            assert close_started.wait(5)
            assert not close_done.wait(0.05)
            assert session_close.call_count == 0 and response_closes == []
            assert not responses[0]._content_consumed
            local_http.release.set()
            client_thread.join(timeout=5)
            closer.join(timeout=5)
            assert not client_thread.is_alive() and not closer.is_alive()
            assert errors == [] and close_done.is_set()
            assert results == [({"instance_id": "fixture", "ok": True}, 200)]
            assert response_closes == [1]
            assert session_close.call_count == (0 if borrowed else 1)
            assert_terminal(proxy)
            if caller is not None:
                with caller.get(local_http.url + "/v1/health", timeout=5) as response:
                    assert response.json()["ok"] is True
        finally:
            local_http.release.set()
            for thread in (client_thread, closer):
                if thread.ident is not None:
                    thread.join(timeout=5)
                    assert not thread.is_alive()
            for response in responses:
                response.close()
            native_session_close()


def test_invalid_native_json_preserves_error_and_closes_response(local_http: LocalHttp) -> None:
    local_http.body = b"invalid fixture JSON"
    proxy = LocalManagerProxy()
    responses: list[requests.Response] = []
    calls: list[int] = []
    native_request = proxy.session.request

    def request(*args: Any, **kwargs: Any) -> requests.Response:
        response = native_request(*args, **kwargs)
        responses.append(response)
        native_close = response.close

        def close() -> None:
            calls.append(1)
            native_close()

        response.close = close
        return response

    try:
        with (
            mock.patch.object(
                proxy,
                "discover",
                return_value=ManagerConnection(local_http.url, "fixture", "fixture-secret"),
            ),
            mock.patch.object(proxy.session, "request", side_effect=request),
            mock.patch.object(proxy.session, "close", wraps=proxy.session.close) as session_close,
        ):
            with pytest.raises(ManagerProxyError) as caught:
                proxy.request_json("GET", "/v1/health", timeout=5)
            assert caught.value.code == "manager_invalid_response"
            assert str(caught.value) == "Pandrator Manager returned an invalid response."
            assert calls == [1] and session_close.call_count == 0
            proxy.close()
            assert session_close.call_count == 1
    finally:
        for response in responses:
            response.close()
        proxy.session.close()


@pytest.mark.parametrize("invalid_body", [False, True])
def test_native_response_close_failure_propagates_without_remapping(
    local_http: LocalHttp, invalid_body: bool
) -> None:
    if invalid_body:
        local_http.body = b"invalid fixture JSON"
    proxy = LocalManagerProxy()
    sentinel = RuntimeError("injected native Response.close failure")
    native_request = proxy.session.request
    responses: list[requests.Response] = []
    native_closes: list[Callable[[], None]] = []

    def request(*args: Any, **kwargs: Any) -> requests.Response:
        response = native_request(*args, **kwargs)
        responses.append(response)
        native_closes.append(response.close)
        response.close = mock.Mock(side_effect=sentinel)
        return response

    try:
        with (
            mock.patch.object(
                proxy,
                "discover",
                return_value=ManagerConnection(local_http.url, "fixture", "fixture-secret"),
            ),
            mock.patch.object(proxy.session, "request", side_effect=request),
        ):
            with pytest.raises(RuntimeError) as caught:
                proxy.request_json("GET", "/v1/health", timeout=5)
            assert caught.value is sentinel
            if invalid_body:
                assert isinstance(sentinel.__context__, ManagerProxyError)
                assert sentinel.__context__.code == "manager_invalid_response"
            else:
                assert sentinel.__context__ is None
            proxy.close()
    finally:
        for close in native_closes:
            close()
        proxy.session.close()


def test_maintenance_refusal_preserves_all_native_graph_resources(graph: NativeGraph) -> None:
    with (
        mock.patch.object(type(graph.services.startup_maintenance), "stop", return_value=False),
        mock.patch.object(
            graph.services.manager_bridge.session,
            "close",
            wraps=graph.services.manager_bridge.session.close,
        ) as close,
    ):
        with pytest.raises(RuntimeError, match="maintenance"):
            graph.services.close()
        assert close.call_count == 0
        graph.assert_live()


def test_provider_close_failure_still_retires_manager_then_database(graph: NativeGraph) -> None:
    sentinel = RuntimeError("injected provider close failure")
    order: list[str] = []
    native_manager = graph.services.manager_bridge.close
    native_dispose = graph.services.database.dispose

    def manager() -> None:
        order.append("manager")
        native_manager()

    def dispose() -> None:
        order.append("database")
        native_dispose()

    with (
        mock.patch.object(graph.services.tts_providers, "close", side_effect=sentinel),
        mock.patch.object(graph.services.manager_bridge, "close", side_effect=manager),
        mock.patch.object(graph.services.database, "dispose", side_effect=dispose),
    ):
        with pytest.raises(RuntimeError) as caught:
            graph.services.close()
        assert caught.value is sentinel
        assert order == ["manager", "database"]
        assert graph.services.manager_bridge._session_retired
        assert not graph.services.tts_providers._closed
        assert graph.services.database.engine.pool is not graph.pool
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            graph.connection.execute("SELECT 1")


@pytest.mark.parametrize("provider_also_fails", [False, True])
def test_manager_close_failure_still_disposes_database(
    graph: NativeGraph, provider_also_fails: bool
) -> None:
    manager_error = RuntimeError("injected manager Session.close failure")
    provider_error = RuntimeError("injected provider close failure")
    with mock.patch.object(
        graph.services.manager_bridge.session, "close", side_effect=manager_error
    ):
        with (
            mock.patch.object(graph.services.tts_providers, "close", side_effect=provider_error)
            if provider_also_fails
            else mock.patch.object(
                graph.services.tts_providers, "close", wraps=graph.services.tts_providers.close
            )
        ):
            with pytest.raises(RuntimeError) as caught:
                graph.services.close()
            assert caught.value is manager_error
            if provider_also_fails:
                assert manager_error.__context__ is provider_error
            assert not graph.services.manager_bridge._session_retired
            assert graph.services.database.engine.pool is not graph.pool
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                graph.connection.execute("SELECT 1")
