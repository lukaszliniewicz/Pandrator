"""Cooperative checkpoints that stop a read whose client already left."""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from contextvars import ContextVar, Token

_LOGGER = logging.getLogger(__name__)

DisconnectProbe = Callable[[], object]


class RequestAbandoned(Exception):
    """The client of the running request closed its connection."""


class _BoundProbe:
    """A probe plus the one-time flag that limits failure logging per request."""

    __slots__ = ("probe", "failure_logged")

    def __init__(self, probe: DisconnectProbe) -> None:
        self.probe = probe
        self.failure_logged = False


_WATCHED_METHODS = frozenset({"GET", "HEAD"})
# POST routes that only read: a preview with a body. Each one must run in a plain
# read session and write nothing, so stopping it when its client leaves loses
# nothing, exactly as for a GET.
_READ_ONLY_POSTS = (
    re.compile(r"/api/v1/sessions/[^/]+/generation-recast/preview"),
)


def watches_disconnect(method: object, path: object) -> bool:
    """Whether a request may stop once its client leaves.

    Reads (``GET``, ``HEAD``) and the read-only ``POST`` previews above may;
    every other request runs to its commit.
    """

    if method in _WATCHED_METHODS:
        return True
    return method == "POST" and isinstance(path, str) and any(
        pattern.fullmatch(path) for pattern in _READ_ONLY_POSTS
    )


_PROBE: ContextVar[_BoundProbe | None] = ContextVar("pandrator_disconnect_probe", default=None)


def bind_disconnect_probe(probe: DisconnectProbe) -> Token[_BoundProbe | None]:
    """Arm checkpoints in the current context with a waitress-style probe.

    A new thread starts without it, but ``contextvars.copy_context()`` carries it
    along, so work run that way from a request can still stop at a checkpoint.
    """

    return _PROBE.set(_BoundProbe(probe))


def reset_disconnect_probe(token: Token[_BoundProbe | None]) -> None:
    _PROBE.reset(token)


def _disconnected(bound: _BoundProbe) -> bool:
    try:
        return bool(bound.probe())
    except Exception:
        # A broken probe must never abandon a request that may still be wanted.
        if not bound.failure_logged:
            bound.failure_logged = True
            _LOGGER.debug("Client disconnect probe failed; treating client as connected", exc_info=True)
        return False


def raise_if_abandoned() -> None:
    """Raise ``RequestAbandoned`` when a bound probe reports a closed client.

    A no-op wherever no probe is bound: mutations, worker threads and tests.
    """

    bound = _PROBE.get()
    if bound is not None and _disconnected(bound):
        raise RequestAbandoned
