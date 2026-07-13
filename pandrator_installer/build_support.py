"""Build-time helpers shared by installer packaging and its tests."""

from __future__ import annotations

import sys
import sysconfig
from collections.abc import Iterable
from pathlib import Path


OPENSSL_LIBRARY_NAMES = ("libssl.so.3", "libcrypto.so.3")


def openssl_library_directories() -> tuple[Path, ...]:
    """Return plausible OpenSSL locations for the active Python runtime."""

    candidates: list[Path] = []
    multiarch = str(sysconfig.get_config_var("MULTIARCH") or "").strip()

    for prefix_value in (sys.prefix, sys.base_prefix):
        prefix = Path(prefix_value)
        candidates.extend((prefix / "lib", prefix / "lib64"))
        if multiarch:
            candidates.append(prefix / "lib" / multiarch)

    configured_libdir = str(sysconfig.get_config_var("LIBDIR") or "").strip()
    if configured_libdir:
        candidates.append(Path(configured_libdir))

    unique: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        normalized = str(candidate.expanduser().resolve(strict=False))
        if normalized not in seen:
            seen.add(normalized)
            unique.append(Path(normalized))
    return tuple(unique)


def resolve_openssl_runtime_pair(
    search_directories: Iterable[str | Path] | None = None,
) -> tuple[Path, Path]:
    """Find matching OpenSSL 3 libraries in one runtime directory.

    Requiring a colocated pair prevents packaging libssl and libcrypto from
    incompatible installations, while supporting both conventional ``lib``
    and Fedora-style ``lib64`` layouts.
    """

    directories = tuple(
        Path(directory).expanduser().resolve(strict=False)
        for directory in (
            openssl_library_directories()
            if search_directories is None
            else search_directories
        )
    )
    for directory in directories:
        ssl_library = directory / OPENSSL_LIBRARY_NAMES[0]
        crypto_library = directory / OPENSSL_LIBRARY_NAMES[1]
        if ssl_library.is_file() and crypto_library.is_file():
            return ssl_library, crypto_library

    searched = ", ".join(str(directory) for directory in directories) or "<none>"
    raise RuntimeError(
        "Could not locate a matched libssl.so.3/libcrypto.so.3 pair in the "
        f"build Python runtime. Searched: {searched}. Use the project's "
        "'installer-build' Pixi environment, or a Python runtime that ships "
        "both OpenSSL 3 libraries in lib, lib64, or its configured LIBDIR."
    )
