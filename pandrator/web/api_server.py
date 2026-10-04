"""Waitress adapter with bounded shutdown after an API termination request."""

from __future__ import annotations

import logging
import signal
from types import FrameType
from typing import Any
from wsgiref.types import WSGIApplication

from waitress import wasyncore
from waitress.server import create_server


def serve_api(
    app: WSGIApplication,
    *,
    host: str,
    port: int,
    threads: int,
    url_scheme: str,
) -> None:
    """Finish active tasks within Waitress's existing shutdown allowance."""

    logging.basicConfig()
    # Waitress stores heterogeneous dispatchers despite its socket-only map annotations.
    socket_map: dict[int, Any] = {}
    server = create_server(
        app,
        map=socket_map,
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
                server.task_dispatcher.shutdown()
            finally:
                wasyncore.close_all(socket_map)
    finally:
        signal.signal(signal.SIGTERM, previous)
