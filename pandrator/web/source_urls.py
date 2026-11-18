"""Scheme and host admission for source URLs imported from the public web.

The route refuses an unusable URL before it enqueues work; the download job
repeats the check and adds the DNS resolution it needs at download time.
"""

from __future__ import annotations

from urllib.parse import ParseResult, urlparse

ALLOWED_SOURCE_SCHEMES = frozenset({"http", "https"})


def parse_source_url(raw_url: str) -> ParseResult:
    """Return the parsed URL when it is absolute http(s) and names a host.

    Credentials in the URL are refused: the URL is stored in the job payload
    and shown in work receipts.
    """

    parsed = urlparse(str(raw_url or "").strip())
    if parsed.scheme not in ALLOWED_SOURCE_SCHEMES or not parsed.hostname:
        raise ValueError("Source URL must use http or https.")
    if parsed.username or parsed.password:
        raise ValueError("Source URL must not contain a user name or password.")
    return parsed


__all__ = ["ALLOWED_SOURCE_SCHEMES", "parse_source_url"]
