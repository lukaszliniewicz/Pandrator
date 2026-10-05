"""Borrowed transport contract for application domain request builders."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ApplicationRequests(ABC):
    timeout_seconds: float

    @abstractmethod
    def _request_json(
        self,
        path: str,
        *,
        method: str = "GET",
        authenticated: bool = True,
        parameters: dict[str, Any] | list[tuple[str, Any]] | None = None,
        body: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
        if_match_revision: int | str | None = None,
        maximum_body_bytes: int = 512 * 1024,
        request_timeout_seconds: float | None = None,
        _allow_local_retry: bool = True,
    ) -> dict[str, Any]:
        raise NotImplementedError

    @abstractmethod
    def _request_binary_json(
        self,
        path: str,
        *,
        method: str,
        body: bytes,
        content_type: str,
        extra_headers: dict[str, str] | None = None,
        _allow_local_retry: bool = True,
    ) -> dict[str, Any]:
        raise NotImplementedError
