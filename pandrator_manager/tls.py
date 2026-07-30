"""Shared CA-bundle selection for frozen and source Manager runtimes."""

from __future__ import annotations

import os
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import certifi
from dulwich.config import Config, ConfigDict, StackedConfig

from .errors import ManagerError

CA_ENVIRONMENT_KEYS = (
    "PANDRATOR_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
)

SYSTEM_CA_BUNDLES = (
    Path("/etc/pki/tls/cert.pem"),
    Path("/etc/ssl/certs/ca-certificates.crt"),
    Path("/etc/ssl/cert.pem"),
    Path("/etc/pki/ca-trust/extracted/pem/tls-ca-bundle.pem"),
)


@dataclass(frozen=True, slots=True)
class CABundleSelection:
    path: Path
    source: str

    def diagnostic_payload(self) -> dict[str, str | bool]:
        return {
            "path": str(self.path),
            "source": self.source,
            "readable": True,
        }


def _validated_ca_bundle(path: Path) -> Path:
    selected = path.expanduser().resolve(strict=False)
    if not selected.is_file():
        raise FileNotFoundError(str(selected))
    # Validate the payload with the same OpenSSL parser used by HTTPS clients.
    ssl.create_default_context(cafile=str(selected))
    return selected


def select_ca_bundle(
    environment: Mapping[str, str] | None = None,
    *,
    system_candidates: Sequence[Path] = SYSTEM_CA_BUNDLES,
) -> CABundleSelection:
    """Select a verified trust bundle without ever disabling TLS validation."""

    values = os.environ if environment is None else environment
    for key in CA_ENVIRONMENT_KEYS:
        configured = str(values.get(key) or "").strip()
        if not configured:
            continue
        try:
            path = _validated_ca_bundle(Path(configured))
        except (OSError, ssl.SSLError) as error:
            raise ManagerError(
                "invalid_ca_bundle",
                f"{key} does not point to a readable, valid CA bundle.",
                {
                    "environment_key": key,
                    "path": str(
                        Path(configured).expanduser().resolve(strict=False)
                    ),
                    "error_type": type(error).__name__,
                },
                409,
            ) from error
        return CABundleSelection(path=path, source=f"environment:{key}")

    for candidate in system_candidates:
        try:
            path = _validated_ca_bundle(candidate)
        except (OSError, ssl.SSLError):
            continue
        return CABundleSelection(path=path, source="system")

    try:
        bundled = _validated_ca_bundle(Path(certifi.where()))
    except (OSError, ssl.SSLError) as error:
        raise ManagerError(
            "ca_bundle_unavailable",
            "No readable system or packaged CA bundle is available for secure downloads.",
            {"error_type": type(error).__name__},
            500,
        ) from error
    return CABundleSelection(path=bundled, source="certifi")


def dulwich_config_with_ca(
    environment: Mapping[str, str] | None = None,
) -> tuple[Config, CABundleSelection]:
    """Overlay a CA bundle while retaining the user's normal Git configuration."""

    selection = select_ca_bundle(environment)
    override = ConfigDict()
    override.set(b"http", b"sslCAInfo", str(selection.path).encode("utf-8"))
    return (
        StackedConfig([override, *StackedConfig.default_backends()]),
        selection,
    )
