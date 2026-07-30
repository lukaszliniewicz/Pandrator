"""HTTPS-only, size-bounded, digest-verified downloads."""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin, urlparse

import requests

from ..context import CancellationToken
from ..tls import select_ca_bundle


@dataclass(frozen=True, slots=True)
class ArtifactSpec:
    url: str
    sha256: str
    size_bytes: int | None = None
    filename: str | None = None

    def __post_init__(self) -> None:
        if urlparse(self.url).scheme.lower() != "https":
            raise ValueError("Artifact downloads require HTTPS.")
        digest = self.sha256.strip().lower()
        if len(digest) != 64 or any(value not in "0123456789abcdef" for value in digest):
            raise ValueError("Artifact SHA-256 must be a 64-character hexadecimal digest.")
        if self.size_bytes is not None and self.size_bytes < 0:
            raise ValueError("Artifact size cannot be negative.")


class ArtifactDownloader:
    def __init__(
        self,
        *,
        cancellation: CancellationToken | None = None,
        session: requests.Session | None = None,
        environment: Mapping[str, str] | None = None,
        maximum_bytes: int = 32 * 1024 * 1024 * 1024,
        maximum_redirects: int = 10,
    ) -> None:
        self.cancellation = cancellation or CancellationToken()
        self.session = session or requests.Session()
        self.environment = dict(os.environ if environment is None else environment)
        self.maximum_bytes = int(maximum_bytes)
        self.maximum_redirects = max(0, int(maximum_redirects))
        self.verify = str(select_ca_bundle(self.environment).path)

    @staticmethod
    def matches(path: Path, spec: ArtifactSpec) -> bool:
        try:
            size = path.stat().st_size
        except OSError:
            return False
        if spec.size_bytes is not None and size != spec.size_bytes:
            return False
        digest = hashlib.sha256()
        try:
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except OSError:
            return False
        return digest.hexdigest().lower() == spec.sha256.lower()

    def download(
        self,
        spec: ArtifactSpec,
        destination: Path,
        *,
        offline: bool = False,
    ) -> Path:
        destination = destination.expanduser().resolve(strict=False)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if spec.size_bytes is not None and spec.size_bytes > self.maximum_bytes:
            raise ValueError("Artifact exceeds the configured maximum size.")
        if destination.is_file() and self.matches(destination, spec):
            return destination
        if offline:
            raise FileNotFoundError(
                "The verified artifact is not available in the local cache."
            )

        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{destination.name}.",
            suffix=".part",
            dir=destination.parent,
        )
        os.close(descriptor)
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        written = 0
        try:
            with self._open_https(spec.url) as response:
                response.raise_for_status()
                final_scheme = urlparse(response.url).scheme.lower()
                if final_scheme != "https":
                    raise ValueError("Artifact redirect left HTTPS.")
                declared = response.headers.get("Content-Length")
                if declared:
                    declared_size = int(declared)
                    if declared_size > self.maximum_bytes:
                        raise ValueError(
                            "Artifact exceeds the configured maximum size."
                        )
                    if (
                        spec.size_bytes is not None
                        and declared_size != spec.size_bytes
                    ):
                        raise ValueError(
                            "Artifact Content-Length does not match signed metadata."
                        )
                with temporary.open("wb") as handle:
                    for chunk in response.iter_content(chunk_size=1024 * 1024):
                        self.cancellation.raise_if_requested()
                        if not chunk:
                            continue
                        written += len(chunk)
                        if written > self.maximum_bytes:
                            raise ValueError("Artifact exceeds the configured maximum size.")
                        digest.update(chunk)
                        handle.write(chunk)
                    handle.flush()
                    os.fsync(handle.fileno())
            if spec.size_bytes is not None and written != spec.size_bytes:
                raise ValueError(
                    f"Artifact size mismatch: expected {spec.size_bytes}, received {written}."
                )
            if digest.hexdigest().lower() != spec.sha256.lower():
                raise ValueError("Artifact SHA-256 verification failed.")
            os.replace(temporary, destination)
            return destination
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _open_https(self, url: str):
        current = url
        redirect_statuses = {301, 302, 303, 307, 308}
        for redirect_count in range(self.maximum_redirects + 1):
            self.cancellation.raise_if_requested()
            if urlparse(current).scheme.lower() != "https":
                raise ValueError("Artifact redirect left HTTPS.")
            response = self.session.get(
                current,
                stream=True,
                timeout=(30, 300),
                allow_redirects=False,
                verify=self.verify,
            )
            if response.status_code not in redirect_statuses:
                return response
            location = response.headers.get("Location")
            response.close()
            if not location:
                raise ValueError("Artifact redirect did not include a location.")
            if redirect_count >= self.maximum_redirects:
                raise ValueError("Artifact download exceeded the redirect limit.")
            current = urljoin(current, location)
        raise ValueError("Artifact download exceeded the redirect limit.")
