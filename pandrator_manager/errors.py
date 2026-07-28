"""Typed manager errors shared by transports and clients."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ManagerError(RuntimeError):
    code: str
    message: str
    details: dict[str, Any] | None = None
    http_status: int = 400

    def __str__(self) -> str:
        return self.message


class ConflictError(ManagerError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("conflict", message, details, 409)


class NotFoundError(ManagerError):
    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__("not_found", message, details, 404)


class RevisionConflict(ManagerError):
    def __init__(self, expected: int, actual: int):
        super().__init__(
            "revision_conflict",
            "Manager configuration changed after this request was prepared.",
            {"expected_revision": expected, "actual_revision": actual},
            409,
        )


class PlanExpired(ManagerError):
    def __init__(self, plan_id: str):
        super().__init__(
            "plan_expired",
            "The operation plan has expired; create and review a new plan.",
            {"plan_id": plan_id},
            409,
        )


class CancellationRequested(ManagerError):
    def __init__(self):
        super().__init__(
            "cancelled",
            "The operation was cancelled.",
            http_status=409,
        )


class UnsafePathError(ManagerError):
    def __init__(self, path: str):
        super().__init__(
            "unsafe_path",
            "The requested path is outside manager-owned workspace roots.",
            {"path": path},
            400,
        )
