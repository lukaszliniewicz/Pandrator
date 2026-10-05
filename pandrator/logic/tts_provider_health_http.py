"""GET transport for provider-specific HTTP reachability probes."""

from collections.abc import Callable, Iterable

import requests


def probe_get_urls(
    urls: Iterable[str],
    *,
    timeout: int,
    accepts_status: Callable[[int], bool],
    headers: Callable[[], dict[str, str]] | None = None,
) -> bool:
    """Try ordered candidates using the caller's status and header policies."""
    for url in urls:
        try:
            if headers is None:
                response = requests.get(url, timeout=timeout)
            else:
                # Resolve headers for each attempt, inside the request error boundary.
                response = requests.get(url, headers=headers(), timeout=timeout)
            if accepts_status(response.status_code):
                return True
        except requests.exceptions.RequestException:
            continue
    return False
