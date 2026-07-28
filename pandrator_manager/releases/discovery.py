"""Conservative discovery of the canonical signed Manager release manifest."""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlsplit

import requests

from ..context import ManagerContext
from ..errors import ManagerError

DEFAULT_MANAGER_MANIFEST_URL = (
    "https://github.com/lukaszliniewicz/Pandrator/releases/latest/download/"
    "pandrator-manager-release.json"
)
MAXIMUM_MANIFEST_BYTES = 1024 * 1024
_CA_ENVIRONMENT_KEYS = (
    "PANDRATOR_CA_BUNDLE",
    "REQUESTS_CA_BUNDLE",
    "SSL_CERT_FILE",
    "CURL_CA_BUNDLE",
)


def _https_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme.casefold() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.fragment
    ):
        raise ManagerError(
            "invalid_update_channel",
            "The Manager update channel must use an HTTPS URL.",
            http_status=500,
        )
    return value


def manager_manifest_url(context: ManagerContext) -> str:
    configured = str(
        context.environment.get("PANDRATOR_MANAGER_UPDATE_MANIFEST_URL")
        or DEFAULT_MANAGER_MANIFEST_URL
    ).strip()
    return _https_url(configured)


def fetch_manager_manifest(
    context: ManagerContext,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch a bounded JSON document; signature verification remains separate."""

    url = manager_manifest_url(context)
    verify: bool | str = True
    for key in _CA_ENVIRONMENT_KEYS:
        candidate = str(context.environment.get(key) or "").strip()
        if candidate:
            verify = candidate
            break
    client = session or requests.Session()
    try:
        response = client.get(
            url,
            stream=True,
            timeout=(15, 45),
            allow_redirects=True,
            verify=verify,
            headers={
                "Accept": "application/json",
                "User-Agent": "Pandrator-Manager",
            },
        )
        response.raise_for_status()
        _https_url(str(response.url))
        declared = int(response.headers.get("Content-Length") or 0)
        if declared > MAXIMUM_MANIFEST_BYTES:
            raise ManagerError(
                "update_manifest_too_large",
                "The Manager update manifest exceeds the 1 MB limit.",
                http_status=502,
            )
        chunks: list[bytes] = []
        received = 0
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            received += len(chunk)
            if received > MAXIMUM_MANIFEST_BYTES:
                raise ManagerError(
                    "update_manifest_too_large",
                    "The Manager update manifest exceeds the 1 MB limit.",
                    http_status=502,
                )
            chunks.append(chunk)
    except ManagerError:
        raise
    except (OSError, ValueError, requests.RequestException) as error:
        raise ManagerError(
            "update_check_failed",
            "The signed Manager update channel could not be reached.",
            {"reason": str(error)},
            502,
        ) from error
    finally:
        if "response" in locals():
            response.close()
        if session is None:
            client.close()

    try:
        decoded = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ManagerError(
            "invalid_update_manifest",
            "The Manager update channel returned invalid JSON.",
            http_status=502,
        ) from error
    if not isinstance(decoded, dict):
        raise ManagerError(
            "invalid_update_manifest",
            "The Manager update channel did not return a signed manifest.",
            http_status=502,
        )
    return decoded
