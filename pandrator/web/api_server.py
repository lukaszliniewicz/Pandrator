"""Waitress adapter that completes active requests after an API termination request."""

from __future__ import annotations

import logging
import signal
from collections.abc import Callable
from types import FrameType
from typing import Any, cast
from wsgiref.types import WSGIApplication

from waitress import wasyncore
from waitress.channel import HTTPChannel
from waitress.server import BaseWSGIServer, create_server

# Waitress stops reading a socket while its request is queued or running unless
# this is at least 1, so a client that gave up would never be noticed.
REQUEST_LOOKAHEAD = 1
_DISCONNECT_WATCHED_METHODS = frozenset({"GET", "HEAD"})


class ReadDisconnectChannel(HTTPChannel):
    """Watch for a departed client only while a read heads the connection's queue.

    With lookahead, waitress reads a connection while its request is queued or
    running, and skips a queued request whose client has left, whatever its
    method. Not reading while a mutation heads the queue leaves that disconnect
    unseen, so the mutation runs to its commit as it did without lookahead. A
    mutation pipelined behind a read can still be dropped with it; clients must
    not pipeline non-idempotent requests (RFC 9112, section 9.3.2).
    """

    def readable(self) -> bool:
        try:
            head = self.requests[0]
        except IndexError:
            head = None
        if head is not None and getattr(head, "command", None) not in _DISCONNECT_WATCHED_METHODS:
            return False
        return super().readable()


def create_api_server(application: WSGIApplication, socket_map: dict[int, Any], **adjustments: Any):
    """Create the waitress server the API and its timing harness share."""

    server = create_server(
        application,
        map=socket_map,
        channel_request_lookahead=REQUEST_LOOKAHEAD,
        **adjustments,
    )
    # Each listener registered itself in the map; give them the read-only watch.
    for dispatcher in socket_map.values():
        if isinstance(dispatcher, BaseWSGIServer):
            dispatcher.channel_class = ReadDisconnectChannel
    return server


def close_waitress_connections(channels: dict[int, wasyncore.dispatcher]) -> None:
    # HTTPChannel.close() does not wake writers waiting on the output condition.
    # Notify them before closing the remaining listener/trigger descriptors.
    for channel in list(channels.values()):
        if isinstance(channel, HTTPChannel):
            channel.handle_close()
    # Waitress's stubs describe sockets here; its native map holds dispatchers.
    close_all = cast(Callable[[dict[int, wasyncore.dispatcher]], None], wasyncore.close_all)
    close_all(channels)


class ApiShutdownIncomplete(RuntimeError):
    """Request shutdown failed, so application resources must remain owned."""


def serve_api(
    app: WSGIApplication,
    *,
    host: str,
    port: int,
    threads: int,
    url_scheme: str,
) -> None:
    """Disconnect clients and wait for active native tasks to finish on shutdown."""

    logging.basicConfig()
    # Waitress stores heterogeneous dispatchers despite its socket-only map annotations.
    socket_map: dict[int, Any] = {}
    server = create_api_server(
        app,
        socket_map,
        host=host,
        port=port,
        threads=threads,
        url_scheme=url_scheme,
    )
    server.print_listen("Serving on http://{}:{}")
    requested = False

    def request_stop(_signum: int, _frame: FrameType | None) -> None:
        nonlocal requested
        requested = True

    previous = signal.signal(signal.SIGTERM, request_stop)
    try:
        try:
            while socket_map and not requested:
                wasyncore.loop(
                    timeout=0.1,
                    map=socket_map,
                    count=1,
                    use_poll=server.adj.asyncore_use_poll,
                )
        except (KeyboardInterrupt, SystemExit):
            pass
        finally:
            try:
                try:
                    close_waitress_connections(socket_map)
                finally:
                    # The native deadline supports floats unlike the installed integer-only stub.
                    shutdown = cast(Callable[..., bool], server.task_dispatcher.shutdown)
                    shutdown(cancel_pending=True, timeout=float("inf"))
            except BaseException as error:
                raise ApiShutdownIncomplete(
                    "API request shutdown did not complete; application resources remain owned."
                ) from error
    finally:
        signal.signal(signal.SIGTERM, previous)
