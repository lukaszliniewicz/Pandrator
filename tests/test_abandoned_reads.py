"""A read whose client left stops at a checkpoint; a queued one is never run.

Waitress reads a socket during a request only with ``channel_request_lookahead``
of at least 1.  With it, a request queued behind busy threads is skipped once its
client closes, and a running ``GET``/``HEAD`` polls the same signal at the top of
each segment step of the identity pass and the cast freeze.  The tests count
probe, ``for_segment`` and ``build_render_parts`` calls and wait only with bounded
loops; none asserts wall-clock time.
"""

from __future__ import annotations

import socket
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Any
from unittest import mock

import pytest
from sqlalchemy import select
from waitress import wasyncore

from pandrator.web import abandoned_requests, api_server, generation_rendering
from pandrator.web import generation_cast_runtime as cast_runtime
from pandrator.web import models as m
from pandrator.web.abandoned_requests import (
    RequestAbandoned,
    bind_disconnect_probe,
    raise_if_abandoned,
    reset_disconnect_probe,
)
from pandrator.web.accounts.execution_actor import execution_actor_scope
from pandrator.web.api import create_app
from pandrator.web.api_server import REQUEST_LOOKAHEAD, ReadDisconnectChannel
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.generation_audio_identity import AudioIdentityContext
from pandrator.web.generation_performance_snapshot import performance_runtime_settings
from tests.test_audio_identity_cost import (  # noqa: F401
    _add_matching_takes,
    case,
    cast_project,
    design,
    no_network,
    project,
    retained,
    varied_plan,
)

WAIT_SECONDS = 20.0


def _wait_until(condition: Callable[[], bool], what: str) -> None:
    deadline = time.monotonic() + WAIT_SECONDS
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.01)


class TurnsTrueAfter:
    """A probe that reports a connected client for ``connected_calls`` calls."""

    def __init__(self, connected_calls: int):
        self.connected_calls = connected_calls
        self.calls = 0

    def __call__(self) -> bool:
        self.calls += 1
        return self.calls > self.connected_calls


@contextmanager
def bound(probe: Callable[[], object]) -> Iterator[None]:
    token = bind_disconnect_probe(probe)
    try:
        yield
    finally:
        reset_disconnect_probe(token)


def _unbound() -> bool:
    return abandoned_requests._PROBE.get() is None


# ------------------------------------------------------------------ the module


def test_checkpoint_is_a_no_op_without_a_probe():
    assert _unbound()
    raise_if_abandoned()


def test_checkpoint_continues_while_the_client_is_connected():
    probe = mock.Mock(return_value=False)
    with bound(probe):
        raise_if_abandoned()
        raise_if_abandoned()
    assert probe.call_count == 2
    assert _unbound()


def test_checkpoint_raises_once_the_client_is_gone():
    probe = mock.Mock(return_value=True)
    with bound(probe), pytest.raises(RequestAbandoned):
        raise_if_abandoned()
    assert probe.call_count == 1
    assert _unbound()


def test_a_probe_that_raises_counts_as_connected_and_is_logged_once(caplog):
    probe = mock.Mock(side_effect=RuntimeError("broken"))
    caplog.set_level("DEBUG", logger=abandoned_requests.__name__)
    with bound(probe):
        for _ in range(3):
            raise_if_abandoned()
    assert probe.call_count == 3
    failures = [r for r in caplog.records if r.name == abandoned_requests.__name__]
    assert len(failures) == 1 and failures[0].levelname == "DEBUG"


def test_request_abandoned_is_not_a_base_exception():
    assert issubclass(RequestAbandoned, Exception)
    assert not issubclass(RequestAbandoned, (KeyboardInterrupt, SystemExit))


# ------------------------------------------------------- the identity and freeze


def test_identity_pass_stops_at_the_first_checkpoint_after_the_client_leaves(varied_plan):  # noqa: F811
    plan, actor = varied_plan
    probe = TurnsTrueAfter(3)
    visited = []
    with execution_actor_scope(actor), plan.c.r.f.case.database.immediate_session() as session:
        context = AudioIdentityContext(session, plan.snapshot)
        with bound(probe), pytest.raises(RequestAbandoned):
            for segment_id in plan.ids:
                visited.append(segment_id)
                context.for_segment(session.get_one(m.GenerationSegment, segment_id))
    assert len(visited) == 4 and probe.calls == 4


def test_list_segments_stops_within_one_segment_of_the_client_leaving(varied_plan):  # noqa: F811
    plan, _actor = varied_plan
    service = plan.c.r.f.services.generation
    session_id = plan.c.p.source["id"]
    full = service.list_segments(session_id)
    assert len(full["items"]) == len(plan.ids)
    calls = [0]
    real = AudioIdentityContext.for_segment

    def counting(self, segment):
        calls[0] += 1
        return real(self, segment)

    # The listing makes one checkpoint per segment in the cast freeze and one more
    # per segment in the identity pass, so any N stops at probe call N + 1.
    stops = (0, 3, 15)
    assert max(stops) < 2 * len(plan.ids), "each N must fall inside the two passes"
    for connected in stops:
        calls[0] = 0
        probe = TurnsTrueAfter(connected)
        with mock.patch.object(AudioIdentityContext, "for_segment", counting):
            with bound(probe), pytest.raises(RequestAbandoned):
                service.list_segments(session_id)
        assert probe.calls == connected + 1
        assert calls[0] <= max(0, connected - len(plan.ids)) + 1
    # An abandoned read changes nothing for the next one.
    assert service.list_segments(session_id) == full


def test_whole_plan_cast_freeze_stops_within_one_segment(varied_plan):  # noqa: F811
    plan, actor = varied_plan
    settings = performance_runtime_settings(plan.snapshot)
    stopped_after = 3
    probe = TurnsTrueAfter(stopped_after)
    with ExitStack() as stack:
        stack.enter_context(execution_actor_scope(actor))
        session = stack.enter_context(plan.c.r.f.case.database.immediate_session())
        parts = stack.enter_context(mock.patch.object(
            generation_rendering, "build_render_parts",
            wraps=generation_rendering.build_render_parts,
        ))
        stack.enter_context(bound(probe))
        with pytest.raises(RequestAbandoned):
            cast_runtime.freeze_cast_snapshot(
                session, plan.revision_id, dict(plan.snapshot), settings,
            )
    assert probe.calls == stopped_after + 1
    # One render per segment frozen before the checkpoint that raised.
    assert parts.call_count == stopped_after


@pytest.mark.parametrize(
    "route", ["generation-segments", "generation-plan/status", "generation-plan/revisions"],
)
def test_each_heavy_read_of_a_cast_plan_with_takes_answers_499(varied_plan, route):  # noqa: F811
    plan, actor = varied_plan
    _add_matching_takes(plan, actor)
    url = f"/api/v1/sessions/{plan.c.p.source['id']}/{route}"
    client = plan.c.r.f.users.alice
    assert client.get(url).status_code == 200
    probe = mock.Mock(return_value=True)
    response = client.get(url, environ_overrides={"waitress.client_disconnected": probe})
    assert response.status_code == 499, response.get_json()
    assert response.get_json()["error"]["code"] == "client_closed_request"
    # The probe is read only at a checkpoint, so one call means the read reached one.
    probe.assert_called_once()
    with plan.c.r.f.case.database.session() as session:
        statuses = session.scalars(
            select(m.AuditEvent.status_code).where(m.AuditEvent.path == url)
        ).all()
    assert sorted(statuses) == [200, 499]
    assert _unbound()


def test_a_worker_thread_never_sees_the_request_threads_probe(varied_plan):  # noqa: F811
    # Guards the threading model the probe relies on rather than code in this
    # change: a plain thread starts with an empty context, so a job started from
    # a request never sees that request's probe.
    plan, actor = varied_plan
    probe = mock.Mock(return_value=True)
    outcome: list[object] = []

    def worker() -> None:
        try:
            with (
                execution_actor_scope(actor),
                plan.c.r.f.case.database.immediate_session() as session,
            ):
                context = AudioIdentityContext(session, plan.snapshot)
                for segment_id in plan.ids:
                    context.for_segment(session.get_one(m.GenerationSegment, segment_id))
            outcome.append(len(plan.ids))
        except BaseException as error:  # reported to the test thread below
            outcome.append(error)

    with bound(probe):
        thread = threading.Thread(target=worker)
        thread.start()
        thread.join(WAIT_SECONDS)
    assert not thread.is_alive()
    assert outcome == [len(plan.ids)]
    probe.assert_not_called()


# ---------------------------------------------------------------- the Flask app


@pytest.fixture
def api():
    with tempfile.TemporaryDirectory() as root:
        bootstrap = BootstrapTokenStore()
        app = create_app(data_root=root, testing=True, bootstrap_tokens=bootstrap)
        # Routes must exist before the app handles its first request.
        app.add_url_rule(
            "/api/v1/abandoned-reads-probe", "abandoned_reads_probe", _probe_view,
            methods=["GET", "POST"],
        )
        client = app.test_client()
        csrf = client.post(
            "/api/v1/auth/bootstrap", json={"token": bootstrap.issue()}
        ).get_json()["csrf_token"]
        services = app.extensions["pandrator"]
        try:
            yield app, client, {"X-CSRF-Token": csrf}, services
        finally:
            services["database"].dispose()


SEEN: list[Any] = []


def _probe_view():
    """Record the probe bound while this handler runs, then hit a checkpoint."""
    SEEN.append(abandoned_requests._PROBE.get())
    raise_if_abandoned()
    return {"ok": True}


@pytest.fixture(autouse=True)
def _clear_seen():
    SEEN.clear()


def _heavy_session(client, headers, services) -> str:
    created = client.post(
        "/api/v1/sessions", json={"name": "Abandoned reads", "workflow_kind": "audiobook"},
        headers=headers,
    )
    assert created.status_code == 201, created.get_json()
    session_id = created.get_json()["id"]
    services["generation"].create_plan(
        session_id, source_revision_id=None, settings={},
        segments=[{"text": f"Speech block number {i} with enough words."} for i in range(6)],
    )
    return session_id


def _audit_statuses(services) -> list[tuple[int, str, str]]:
    with services["database"].session() as session:
        rows = session.execute(
            select(m.AuditEvent.status_code, m.AuditEvent.outcome, m.AuditEvent.request_id)
            .where(m.AuditEvent.path.like("%/generation-segments"))
        )
        return [(row[0], row[1], row[2]) for row in rows]


def test_an_abandoned_get_answers_499_and_is_audited_once(api, caplog):
    app, client, headers, services = api
    url = f"/api/v1/sessions/{_heavy_session(client, headers, services)}/generation-segments"
    caplog.set_level("INFO", logger=app.logger.name)
    probe = mock.Mock(return_value=True)
    response = client.get(url, environ_overrides={"waitress.client_disconnected": probe})
    assert response.status_code == 499
    error = response.get_json()["error"]
    assert error["code"] == "client_closed_request"
    assert error["request_id"] == response.headers["X-Request-ID"]
    assert probe.call_count >= 1
    audited = _audit_statuses(services)
    assert [(status, outcome) for status, outcome, _ in audited] == [(499, "failed")]
    assert audited[0][2] == error["request_id"]
    logged = [r for r in caplog.records if "Request abandoned" in r.getMessage()]
    assert len(logged) == 1 and logged[0].levelname == "INFO"
    assert error["request_id"] in logged[0].getMessage()
    assert "/generation-segments" in logged[0].getMessage()
    assert _unbound()


def test_a_connected_get_matches_the_response_without_a_probe(api):
    _app, client, headers, services = api
    url = f"/api/v1/sessions/{_heavy_session(client, headers, services)}/generation-segments"
    plain = client.get(url)
    probe = mock.Mock(return_value=False)
    probed = client.get(url, environ_overrides={"waitress.client_disconnected": probe})
    assert plain.status_code == probed.status_code == 200
    assert plain.get_json() == probed.get_json()
    assert probe.call_count >= 1
    assert [status for status, _, _ in _audit_statuses(services)] == [200, 200]
    assert _unbound()


def test_a_head_request_is_armed_too(api):
    _app, client, headers, services = api
    url = f"/api/v1/sessions/{_heavy_session(client, headers, services)}/generation-segments"
    probe = mock.Mock(return_value=True)
    response = client.head(url, environ_overrides={"waitress.client_disconnected": probe})
    assert response.status_code == 499
    assert _unbound()


def test_mutations_see_no_probe_even_when_the_client_is_gone(api):
    _app, client, headers, _services = api
    probe = mock.Mock(return_value=True)
    overrides = {"waitress.client_disconnected": probe}
    posted = client.post(
        "/api/v1/abandoned-reads-probe", headers=headers, environ_overrides=overrides,
    )
    assert posted.status_code == 200, posted.get_json()
    assert SEEN == [None]
    probe.assert_not_called()
    got = client.get("/api/v1/abandoned-reads-probe", environ_overrides=overrides)
    assert got.status_code == 499
    assert SEEN[1] is not None
    assert _unbound()


def test_a_non_callable_environ_value_arms_nothing(api):
    _app, client, _headers, _services = api
    response = client.get(
        "/api/v1/abandoned-reads-probe", environ_overrides={"waitress.client_disconnected": True},
    )
    assert response.status_code == 200
    assert SEEN == [None]


# --------------------------------------------------------------- real waitress


@contextmanager
def _waitress(app):
    socket_map: dict[int, Any] = {}
    server = api_server.create_api_server(app, socket_map, host="127.0.0.1", port=0, threads=1)
    stop = threading.Event()

    def serve() -> None:
        while not stop.is_set():
            wasyncore.loop(timeout=0.02, map=socket_map, count=1)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        # A test that failed before closing its clients must not leave them open.
        while _OPEN_CLIENTS:
            _OPEN_CLIENTS.pop().close()
        stop.set()
        thread.join(WAIT_SECONDS)
        server.task_dispatcher.shutdown(cancel_pending=True, timeout=WAIT_SECONDS)
        server.close()


_OPEN_CLIENTS: list[socket.socket] = []


def _send_get(server, path: str, method: str = "GET") -> socket.socket:
    # Bounded, so a server that never answers fails the test instead of hanging CI.
    client = socket.create_connection(
        ("127.0.0.1", int(server.effective_port)), timeout=WAIT_SECONDS
    )
    _OPEN_CLIENTS.append(client)
    length = "" if method == "GET" else "Content-Length: 0\r\n"
    client.sendall(f"{method} {path} HTTP/1.1\r\nHost: localhost\r\n{length}\r\n".encode("ascii"))
    return client


def test_waitress_reports_a_closed_client_to_the_running_request_and_skips_queued_ones(
    monkeypatch,
):
    monkeypatch.undo()  # Real waitress needs loopback sockets, which no_network forbids.
    release = threading.Event()
    started = threading.Event()
    served: list[str] = []
    probes: dict[str, Callable[[], bool]] = {}

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        served.append(path)
        probes[path] = environ["waitress.client_disconnected"]
        if path == "/first":
            started.set()
            assert release.wait(WAIT_SECONDS)
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]

    with _waitress(app) as server:
        first = _send_get(server, "/first")
        assert started.wait(WAIT_SECONDS)
        assert probes["/first"]() is False
        # Queued behind the only thread; its client closes before it is served.
        second = _send_get(server, "/second")
        _wait_until(lambda: len(server.task_dispatcher.queue) == 1, "the request to queue")
        queued = server.task_dispatcher.queue[0]
        second.close()
        # The running request's client closes while the app is blocked.
        first.close()
        _wait_until(lambda: probes["/first"]() is True, "the running request's disconnect")
        # Waitress skips a queued channel only once the loop has seen its close, so
        # release the worker after that, or it could serve /second first.
        _wait_until(lambda: not queued.connected, "the queued request's disconnect")
        release.set()
        _wait_until(
            lambda: not server.task_dispatcher.queue and server.task_dispatcher.active_count == 0,
            "the dispatcher to go idle",
        )
    assert served == ["/first"]


def test_waitress_still_runs_a_queued_mutation_whose_client_left(monkeypatch):
    monkeypatch.undo()  # Real waitress needs loopback sockets, which no_network forbids.
    release = threading.Event()
    started = threading.Event()
    served: list[tuple[str, str, bool]] = []
    probes: dict[str, Callable[[], bool]] = {}

    def app(environ, start_response):
        path = environ["PATH_INFO"]
        probes[path] = environ["waitress.client_disconnected"]
        served.append((environ["REQUEST_METHOD"], path, probes[path]()))
        if path == "/first":
            started.set()
            assert release.wait(WAIT_SECONDS)
        start_response("200 OK", [("Content-Type", "text/plain"), ("Content-Length", "2")])
        return [b"ok"]

    with _waitress(app) as server:
        first = _send_get(server, "/first")
        assert started.wait(WAIT_SECONDS)
        # Queued behind the only thread; its client closes before it is served.
        second = _send_get(server, "/second", method="POST")
        _wait_until(lambda: len(server.task_dispatcher.queue) == 1, "the request to queue")
        second.close()
        # The loop reads every closed socket it watches in one pass, so once the
        # read's disconnect shows, the mutation's close has had its chance too.
        first.close()
        _wait_until(lambda: probes["/first"]() is True, "the running request's disconnect")
        release.set()
        _wait_until(lambda: len(served) == 2, "the queued mutation to run")
        _wait_until(
            lambda: not server.task_dispatcher.queue and server.task_dispatcher.active_count == 0,
            "the dispatcher to go idle",
        )
    assert served == [("GET", "/first", False), ("POST", "/second", False)]


def test_api_server_listeners_use_the_read_only_disconnect_channel(monkeypatch):
    monkeypatch.undo()  # Real waitress needs loopback sockets, which no_network forbids.
    socket_map: dict[int, Any] = {}
    server = api_server.create_api_server(
        lambda environ, start_response: [], socket_map, host="127.0.0.1", port=0, threads=1,
    )
    try:
        assert server.adj.channel_request_lookahead == REQUEST_LOOKAHEAD
        assert server.channel_class is ReadDisconnectChannel
    finally:
        server.task_dispatcher.shutdown(cancel_pending=True, timeout=WAIT_SECONDS)
        server.close()


def test_a_client_that_stays_connected_reads_false_throughout(monkeypatch):
    monkeypatch.undo()  # Real waitress needs loopback sockets, which no_network forbids.
    release = threading.Event()
    started = threading.Event()
    seen: list[bool] = []

    def app(environ, start_response):
        probe = environ["waitress.client_disconnected"]
        started.set()
        assert release.wait(WAIT_SECONDS)
        seen.append(probe())
        start_response("200 OK", [("Content-Length", "2")])
        return [b"ok"]

    with _waitress(app) as server:
        client = _send_get(server, "/")
        assert started.wait(WAIT_SECONDS)
        release.set()
        assert client.recv(4096).startswith(b"HTTP/1.1 200")
        client.close()
    assert seen == [False]


# ------------------------------------------------------------------ serve_api


def test_serve_api_passes_the_shared_lookahead_to_waitress():
    assert REQUEST_LOOKAHEAD == 1
    server = mock.Mock()
    server._map = {}
    server.adj.asyncore_use_poll = False
    with (
        mock.patch.object(api_server, "create_server", return_value=server) as create,
        mock.patch.object(api_server, "close_waitress_connections"),
        mock.patch.object(api_server.signal, "signal", return_value=api_server.signal.SIG_DFL),
        mock.patch.object(api_server.wasyncore, "loop"),
    ):
        server.task_dispatcher.shutdown.return_value = True
        api_server.serve_api(mock.Mock(), host="127.0.0.1", port=0, threads=2, url_scheme="http")
    assert create.call_args.kwargs["channel_request_lookahead"] == REQUEST_LOOKAHEAD
