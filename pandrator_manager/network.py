"""Validated, persisted network exposure for the manager and Pandrator.

Loopback is the default.  Remote access is deliberately represented as one of
two explicit deployment profiles so binding a wildcard address can never
silently turn the installation control plane into an unauthenticated network
service.
"""

from __future__ import annotations

import ipaddress
import json
import os
import socket
import tempfile
from enum import StrEnum
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit, urlunsplit

import psutil
from pydantic import Field, field_validator, model_validator

from .auth import protect_path
from .context import WorkspaceLayout
from .models import StrictModel


class AccessMode(StrEnum):
    LOCAL = "local"
    PRIVATE_NETWORK = "private_network"
    HTTPS_PROXY = "https_proxy"


def _loopback(value: str) -> bool:
    try:
        address = ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError:
        return False
    mapped = getattr(address, "ipv4_mapped", None)
    return bool(address.is_loopback or (mapped and mapped.is_loopback))


def _normalize_public_url(value: str | None) -> str | None:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise ValueError(
            "Public URL must be an HTTP(S) origin without credentials, a path, "
            "a query, or a fragment."
    )
    try:
        _ = parsed.port
    except ValueError as error:
        raise ValueError("Public URL contains an invalid port.") from error
    return urlunsplit(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            "",
            "",
            "",
        )
    )


def _normalize_trusted_host(value: str) -> str:
    candidate = str(value or "").strip().lower().rstrip(".")
    if not candidate or any(character in candidate for character in "/?#@*"):
        raise ValueError("Trusted hosts must be exact hostnames or IP addresses.")
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    try:
        ipaddress.ip_address(candidate.split("%", 1)[0])
        return candidate
    except ValueError:
        pass
    if (
        len(candidate) > 253
        or any(
            not label
            or len(label) > 63
            or label.startswith("-")
            or label.endswith("-")
            or not all(character.isalnum() or character == "-" for character in label)
            for label in candidate.split(".")
        )
    ):
        raise ValueError("Trusted host is not a valid hostname.")
    return candidate


class EndpointExposure(StrictModel):
    """One HTTP service's bind and browser-facing network policy."""

    mode: AccessMode = AccessMode.LOCAL
    bind_host: str = "127.0.0.1"
    port: int = Field(default=0, ge=0, le=65535)
    public_url: str | None = None
    trusted_hosts: tuple[str, ...] = ()
    proxy_hops: int = Field(default=0, ge=0, le=3)
    allow_insecure_remote: bool = False

    @field_validator("bind_host")
    @classmethod
    def validate_bind_host(cls, value: str) -> str:
        candidate = str(value or "").strip()
        try:
            ipaddress.ip_address(candidate.split("%", 1)[0])
        except ValueError as error:
            raise ValueError("Bind host must be a concrete IP address.") from error
        return candidate

    @field_validator("public_url", mode="before")
    @classmethod
    def validate_public_url(cls, value: object) -> str | None:
        return _normalize_public_url(None if value is None else str(value))

    @field_validator("trusted_hosts")
    @classmethod
    def validate_trusted_hosts(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(dict.fromkeys(_normalize_trusted_host(value) for value in values))

    @model_validator(mode="after")
    def validate_profile(self) -> "EndpointExposure":
        if self.mode == AccessMode.LOCAL:
            if not _loopback(self.bind_host):
                raise ValueError("Local access must bind to a loopback address.")
            if self.public_url is not None:
                raise ValueError("Local access does not use a public URL.")
            if self.proxy_hops or self.allow_insecure_remote:
                raise ValueError("Local access cannot trust proxies or insecure remote HTTP.")
            return self

        if not self.public_url:
            raise ValueError("Remote access requires the exact browser-facing public URL.")
        if self.port == 0:
            raise ValueError("Remote access requires a fixed listening port.")
        parsed = urlsplit(self.public_url)
        if self.mode == AccessMode.PRIVATE_NETWORK:
            if parsed.scheme != "http":
                raise ValueError("Private-network mode uses an http:// public URL.")
            if (parsed.port or 80) != self.port:
                raise ValueError(
                    "Private-network public URL port must match the listening port."
                )
            if not self.allow_insecure_remote:
                raise ValueError(
                    "Private-network HTTP requires explicit insecure-remote consent."
                )
            if self.proxy_hops:
                raise ValueError("Private-network mode does not trust proxy headers.")
        elif self.mode == AccessMode.HTTPS_PROXY:
            if parsed.scheme != "https":
                raise ValueError("HTTPS-proxy mode requires an https:// public URL.")
            if self.proxy_hops < 1:
                raise ValueError(
                    "HTTPS-proxy mode requires the number of trusted proxy hops."
                )
            if self.allow_insecure_remote:
                raise ValueError("HTTPS-proxy mode cannot enable insecure remote HTTP.")
        return self

    @property
    def remote_enabled(self) -> bool:
        return self.mode != AccessMode.LOCAL

    @property
    def secure_cookies(self) -> bool:
        return self.mode == AccessMode.HTTPS_PROXY

    @property
    def public_hostname(self) -> str | None:
        return str(urlsplit(self.public_url).hostname or "") if self.public_url else None

    @property
    def allowed_hosts(self) -> tuple[str, ...]:
        values = ["localhost", "127.0.0.1", "::1"]
        if self.public_hostname:
            values.append(self.public_hostname)
        values.extend(self.trusted_hosts)
        return tuple(dict.fromkeys(value.lower().rstrip(".") for value in values))

    @property
    def probe_host(self) -> str:
        return "::1" if ":" in self.bind_host and "." not in self.bind_host else "127.0.0.1"

    @property
    def local_base_url(self) -> str:
        host = f"[{self.probe_host}]" if ":" in self.probe_host else self.probe_host
        return f"http://{host}:{self.port}"

    @property
    def browser_base_url(self) -> str:
        return str(self.public_url or self.local_base_url).rstrip("/")


class NetworkConfiguration(StrictModel):
    schema_version: int = 1
    manager: EndpointExposure = Field(
        default_factory=lambda: EndpointExposure(port=0)
    )
    application: EndpointExposure = Field(
        default_factory=lambda: EndpointExposure(port=8097)
    )


def private_network_candidates(port: int) -> tuple[dict[str, str], ...]:
    """Return usable private IPv4 origins without asking users to find an IP."""

    selected_port = int(port)
    if selected_port < 1 or selected_port > 65535:
        return ()
    try:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
    except (OSError, RuntimeError):
        return ()
    candidates: list[dict[str, str]] = []
    seen: set[str] = set()
    for interface, interface_addresses in addresses.items():
        status = stats.get(interface)
        if status is not None and not status.isup:
            continue
        for item in interface_addresses:
            if item.family != socket.AF_INET:
                continue
            try:
                address = ipaddress.ip_address(str(item.address).split("%", 1)[0])
            except ValueError:
                continue
            if (
                not address.is_private
                or address.is_loopback
                or address.is_link_local
                or address.is_unspecified
                or address.is_multicast
            ):
                continue
            value = str(address)
            if value in seen:
                continue
            seen.add(value)
            candidates.append(
                {
                    "interface": interface,
                    "address": value,
                    "url": f"http://{value}:{selected_port}",
                }
            )
    return tuple(candidates)


def _truthy(value: str | None) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _environment_endpoint(
    current: EndpointExposure,
    environment: Mapping[str, str],
    *,
    prefix: str,
) -> EndpointExposure:
    values = current.model_dump(mode="python")
    mapping = {
        "MODE": "mode",
        "BIND_HOST": "bind_host",
        "PORT": "port",
        "PUBLIC_URL": "public_url",
        "PROXY_HOPS": "proxy_hops",
    }
    touched = False
    for suffix, field in mapping.items():
        raw = environment.get(f"{prefix}_{suffix}")
        if raw is None or not str(raw).strip():
            continue
        values[field] = int(raw) if field in {"port", "proxy_hops"} else str(raw).strip()
        touched = True
    trusted = environment.get(f"{prefix}_TRUSTED_HOSTS")
    if trusted is not None:
        values["trusted_hosts"] = tuple(
            item.strip() for item in trusted.split(",") if item.strip()
        )
        touched = True
    insecure = environment.get(f"{prefix}_ALLOW_INSECURE_REMOTE")
    if insecure is not None:
        values["allow_insecure_remote"] = _truthy(insecure)
        touched = True
    if not touched:
        return current
    if f"{prefix}_MODE" not in environment:
        public_url = str(values.get("public_url") or "")
        if public_url.startswith("https://"):
            values["mode"] = AccessMode.HTTPS_PROXY
        elif public_url or not _loopback(str(values.get("bind_host") or "")):
            values["mode"] = AccessMode.PRIVATE_NETWORK
    return EndpointExposure.model_validate(values)


def load_network_configuration(
    layout: WorkspaceLayout,
    *,
    environment: Mapping[str, str] | None = None,
) -> NetworkConfiguration:
    try:
        payload = json.loads(layout.network_configuration.read_text(encoding="utf-8"))
        configured = NetworkConfiguration.model_validate(payload)
    except FileNotFoundError:
        configured = NetworkConfiguration()
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(
            f"Manager network configuration is invalid: {error}"
        ) from error
    selected_environment = os.environ if environment is None else environment
    return NetworkConfiguration(
        manager=_environment_endpoint(
            configured.manager,
            selected_environment,
            prefix="PANDRATOR_MANAGER",
        ),
        application=_environment_endpoint(
            configured.application,
            selected_environment,
            prefix="PANDRATOR",
        ),
    )


def save_network_configuration(
    layout: WorkspaceLayout,
    configuration: NetworkConfiguration,
) -> None:
    layout.state.mkdir(parents=True, exist_ok=True)
    protect_path(layout.state, directory=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".network.",
        suffix=".tmp",
        dir=layout.state,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(configuration.model_dump_json(indent=2))
            handle.flush()
            os.fsync(handle.fileno())
        protect_path(temporary)
        os.replace(temporary, layout.network_configuration)
        protect_path(layout.network_configuration)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
