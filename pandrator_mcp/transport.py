"""Requests adapter that connects only to policy-approved DNS answers."""

from __future__ import annotations

import socket
import sys
from typing import Any, cast
from urllib.parse import urlsplit

from requests.adapters import HTTPAdapter
from requests.models import PreparedRequest
from urllib3.connection import HTTPConnection, HTTPSConnection
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.exceptions import (
    ConnectTimeoutError,
    NewConnectionError,
)
from urllib3.util import connection as connection_util
from urllib3.util import parse_url


class _PinnedConnection:
    pinned_addresses: tuple[str, ...]

    def __init__(
        self,
        *args: Any,
        pinned_addresses: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        self.pinned_addresses = tuple(pinned_addresses)
        super().__init__(*args, **kwargs)

    def _new_conn(self) -> socket.socket:
        # The mixin is always paired with urllib3's HTTP(S)Connection below.
        connection = cast(HTTPConnection, self)
        last_error: OSError | None = None
        for address in self.pinned_addresses:
            try:
                sock = connection_util.create_connection(
                    (address, connection.port),
                    connection.timeout,
                    source_address=connection.source_address,
                    socket_options=connection.socket_options,
                )
                sys.audit("http.client.connect", self, connection.host, connection.port)
                return sock
            except socket.timeout as error:
                raise ConnectTimeoutError(
                    connection,
                    f"Connection to {connection.host} timed out. (connect timeout={connection.timeout})",
                ) from error
            except OSError as error:
                last_error = error
        raise NewConnectionError(
            connection,
            f"Failed to connect to a policy-approved address: {last_error}",
        ) from last_error


class PinnedHTTPConnection(_PinnedConnection, HTTPConnection):
    pass


class PinnedHTTPSConnection(_PinnedConnection, HTTPSConnection):
    pass


class PinnedHTTPConnectionPool(HTTPConnectionPool):
    # urllib3 accepts custom ConnectionCls and forwards pool kwargs at runtime;
    # its type declaration rejects the required pinned_addresses constructor kwarg.
    ConnectionCls = PinnedHTTPConnection  # pyright: ignore[reportAssignmentType]

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_addresses: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            host,
            port,
            pinned_addresses=pinned_addresses,
            **kwargs,
        )


class PinnedHTTPSConnectionPool(HTTPSConnectionPool):
    # Same urllib3 custom-connection declaration limitation as the HTTP pool.
    ConnectionCls = PinnedHTTPSConnection  # pyright: ignore[reportAssignmentType]

    def __init__(
        self,
        host: str,
        port: int,
        *,
        pinned_addresses: tuple[str, ...],
        **kwargs: Any,
    ) -> None:
        super().__init__(
            host,
            port,
            pinned_addresses=pinned_addresses,
            **kwargs,
        )


class PinnedAddressAdapter(HTTPAdapter):
    """Keep Host/SNI intact while bypassing a second untrusted DNS lookup."""

    def __init__(self, origin: str, addresses: tuple[str, ...]) -> None:
        super().__init__(pool_connections=1, pool_maxsize=1, max_retries=0)
        parsed = urlsplit(origin)
        self.origin = origin
        self.scheme = parsed.scheme
        self.host = str(parsed.hostname)
        self.port = int(parsed.port or (443 if self.scheme == "https" else 80))
        self.addresses = tuple(addresses)
        self._pools: list[HTTPConnectionPool] = []

    def get_connection_with_tls_context(
        self,
        request: PreparedRequest,
        verify: bool | str,
        proxies: dict[str, str] | None = None,
        cert: Any = None,
    ) -> HTTPConnectionPool:
        if proxies:
            # An explicitly configured proxy is the connection and DNS trust
            # boundary. TargetRegistry validates its origin and addresses.
            return super().get_connection_with_tls_context(
                request,
                verify,
                proxies,
                cert,
            )
        request_url = request.url
        if request_url is None:
            raise ValueError("The request escaped its pinned target origin.")
        parsed = parse_url(request_url)
        if (
            parsed.scheme != self.scheme
            or parsed.host != self.host
            or int(parsed.port or self.port) != self.port
        ):
            raise ValueError("The request escaped its pinned target origin.")
        host_params, pool_kwargs = self.build_connection_pool_key_attributes(
            request,
            verify,
            cert,
        )
        pool_class = (
            PinnedHTTPSConnectionPool if self.scheme == "https" else PinnedHTTPConnectionPool
        )
        if self.scheme == "http":
            pool_kwargs = {}
        pool = pool_class(
            str(host_params["host"]),
            int(host_params.get("port") or self.port),
            pinned_addresses=self.addresses,
            **pool_kwargs,
        )
        self._pools.append(pool)
        return pool

    def close(self) -> None:
        for pool in self._pools:
            pool.close()
        self._pools.clear()
        super().close()
