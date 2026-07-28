"""Exact-origin and DNS policy for local, LAN, and Internet targets."""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .errors import TargetResolutionError


class TargetMode(StrEnum):
    LOCAL_MANAGED = "local_managed"
    PRIVATE_NETWORK = "private_network"
    EXTERNAL_HTTPS = "external_https"
    EXTERNAL_APPLICATION = "external_application"


class NetworkZone(StrEnum):
    LOOPBACK = "loopback"
    PRIVATE = "private"
    PUBLIC = "public"


_METADATA_RANGES = tuple(
    ipaddress.ip_network(value)
    for value in (
        "169.254.169.254/32",
        "100.100.100.200/32",
        "fd00:ec2::254/128",
    )
)


def normalize_origin(value: str) -> str:
    candidate = str(value or "").strip()
    parsed = urlsplit(candidate)
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise TargetResolutionError("A target endpoint must be one exact HTTP(S) origin.")
    try:
        _ = parsed.port
    except ValueError as error:
        raise TargetResolutionError("A target endpoint contains an invalid port.") from error
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            "",
            "",
            "",
        )
    )


def _default_resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        records = socket.getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
        )
    except OSError as error:
        raise TargetResolutionError(
            "The target hostname could not be resolved.",
            details={"host": host},
        ) from error
    return tuple(dict.fromkeys(str(record[4][0]).split("%", 1)[0] for record in records))


def _forbidden(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        address.is_link_local
        or address.is_multicast
        or address.is_unspecified
        or any(address in network for network in _METADATA_RANGES)
    )


@dataclass(frozen=True, slots=True)
class ResolvedEndpoint:
    origin: str
    scheme: str
    host: str
    port: int
    addresses: tuple[str, ...]
    zone: NetworkZone
    ca_bundle: str | None = None
    proxy_origin: str | None = None


class NetworkPolicy:
    """Resolve once, reject mixed trust zones, and pin the accepted addresses."""

    def __init__(
        self,
        resolver: Callable[[str, int], tuple[str, ...]] | None = None,
    ) -> None:
        self._resolver = resolver or _default_resolver

    def resolve(
        self,
        origin: str,
        *,
        mode: TargetMode,
        allowed_private_cidrs: tuple[str, ...] = (),
        allow_insecure_private_network: bool = False,
        ca_bundle: str | None = None,
        proxy_origin: str | None = None,
    ) -> ResolvedEndpoint:
        normalized = normalize_origin(origin)
        normalized_ca_bundle = None
        if ca_bundle:
            ca_path = Path(ca_bundle).expanduser()
            if not ca_path.exists():
                raise TargetResolutionError("The configured CA bundle does not exist.")
            normalized_ca_bundle = str(ca_path.resolve())
        parsed = urlsplit(normalized)
        scheme = parsed.scheme
        host = str(parsed.hostname)
        port = int(parsed.port or (443 if scheme == "https" else 80))
        raw_addresses = self._resolver(host, port)
        if not raw_addresses:
            raise TargetResolutionError("The target hostname resolved to no addresses.")
        try:
            addresses = tuple(ipaddress.ip_address(value) for value in raw_addresses)
        except ValueError as error:
            raise TargetResolutionError(
                "The target hostname returned an invalid address."
            ) from error
        if any(_forbidden(address) for address in addresses):
            raise TargetResolutionError(
                "The target resolves to a forbidden link-local, metadata, "
                "multicast, or unspecified address."
            )

        cidrs = tuple(ipaddress.ip_network(value, strict=False) for value in allowed_private_cidrs)
        all_loopback = all(address.is_loopback for address in addresses)
        all_private = bool(cidrs) and all(
            any(address in network for network in cidrs) for address in addresses
        )
        all_public = all(address.is_global for address in addresses)

        if mode == TargetMode.LOCAL_MANAGED:
            if scheme != "http" or not all_loopback:
                raise TargetResolutionError(
                    "A locally managed application must resolve only to loopback HTTP."
                )
            zone = NetworkZone.LOOPBACK
        elif mode == TargetMode.PRIVATE_NETWORK:
            if not all_private:
                raise TargetResolutionError(
                    "Every LAN/VPN address must remain inside the configured private CIDRs."
                )
            if scheme == "http" and not allow_insecure_private_network:
                raise TargetResolutionError(
                    "Private-network HTTP requires explicit insecure-transport consent."
                )
            zone = NetworkZone.PRIVATE
        elif mode == TargetMode.EXTERNAL_HTTPS:
            if scheme != "https" or not all_public:
                raise TargetResolutionError(
                    "An external HTTPS target must resolve only to public addresses."
                )
            zone = NetworkZone.PUBLIC
        else:
            if scheme == "http":
                if not allow_insecure_private_network or not all_private:
                    raise TargetResolutionError(
                        "Externally managed HTTP is allowed only for an explicitly "
                        "accepted private CIDR."
                    )
                zone = NetworkZone.PRIVATE
            elif all_private:
                zone = NetworkZone.PRIVATE
            elif all_public:
                zone = NetworkZone.PUBLIC
            else:
                raise TargetResolutionError(
                    "The externally managed target resolves across mixed trust zones."
                )

        normalized_proxy = None
        if proxy_origin:
            normalized_proxy = normalize_origin(proxy_origin)
            proxy_mode = mode
            if mode == TargetMode.EXTERNAL_APPLICATION:
                proxy_mode = (
                    TargetMode.PRIVATE_NETWORK
                    if allowed_private_cidrs
                    else TargetMode.EXTERNAL_HTTPS
                )
            proxy = self.resolve(
                normalized_proxy,
                mode=proxy_mode,
                allowed_private_cidrs=allowed_private_cidrs,
                allow_insecure_private_network=allow_insecure_private_network,
                ca_bundle=normalized_ca_bundle,
            )
            if (
                mode
                in {
                    TargetMode.EXTERNAL_HTTPS,
                    TargetMode.EXTERNAL_APPLICATION,
                }
                and proxy.zone == NetworkZone.PUBLIC
                and proxy.scheme != "https"
            ):
                raise TargetResolutionError("A public explicit proxy must use HTTPS.")
        return ResolvedEndpoint(
            origin=normalized,
            scheme=scheme,
            host=host,
            port=port,
            addresses=tuple(str(address) for address in addresses),
            zone=zone,
            ca_bundle=normalized_ca_bundle,
            proxy_origin=normalized_proxy,
        )
