"""Explicit runtime context and authoritative workspace layout."""

from __future__ import annotations

import os
import platform
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

from .errors import CancellationRequested, UnsafePathError


class Clock(Protocol):
    def time(self) -> float: ...

    def monotonic(self) -> float: ...


class SystemClock:
    def time(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class EventSink(Protocol):
    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        component_id: str | None = None,
        service_id: str | None = None,
    ) -> None: ...


class NullEventSink:
    def emit(
        self,
        event_type: str,
        payload: Mapping[str, Any],
        *,
        operation_id: str | None = None,
        component_id: str | None = None,
        service_id: str | None = None,
    ) -> None:
        del event_type, payload, operation_id, component_id, service_id


class CancellationToken:
    """Thread-safe cooperative cancellation shared by operation tasks."""

    def __init__(self) -> None:
        self._event = threading.Event()

    @property
    def requested(self) -> bool:
        return self._event.is_set()

    def request(self) -> None:
        self._event.set()

    def raise_if_requested(self) -> None:
        if self.requested:
            raise CancellationRequested()


def _resolved(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


@dataclass(frozen=True, slots=True)
class WorkspaceLayout:
    """All manager paths derive from one canonical user-selected workspace."""

    workspace: Path

    @classmethod
    def from_value(cls, value: str | os.PathLike[str]) -> "WorkspaceLayout":
        workspace = _resolved(value)
        if workspace == workspace.parent:
            raise UnsafePathError(str(workspace))
        return cls(workspace=workspace)

    @property
    def root(self) -> Path:
        return self.workspace / "Pandrator"

    @property
    def bin(self) -> Path:
        return self.root / "bin"

    @property
    def manager_versions(self) -> Path:
        return self.root / "manager" / "versions"

    @property
    def app_versions(self) -> Path:
        return self.root / "app" / "versions"

    @property
    def services(self) -> Path:
        return self.root / "services"

    @property
    def environments(self) -> Path:
        return self.root / "envs"

    @property
    def state(self) -> Path:
        return self.root / "state"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def cache(self) -> Path:
        return self.root / "cache"

    @property
    def staging(self) -> Path:
        return self.root / "state" / "staging"

    @property
    def backups(self) -> Path:
        return self.root / "state" / "backups"

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def database(self) -> Path:
        return self.state / "manager.sqlite3"

    @property
    def descriptor(self) -> Path:
        return self.state / "connection.json"

    @property
    def credential(self) -> Path:
        return self.state / "client.secret"

    @property
    def network_configuration(self) -> Path:
        return self.state / "network.json"

    @property
    def instance_lock(self) -> Path:
        return self.state / "manager.lock"

    @property
    def owned_roots(self) -> tuple[Path, ...]:
        return (
            self.bin,
            self.manager_versions,
            self.app_versions,
            self.services,
            self.environments,
            self.state,
            self.logs,
            self.cache,
        )

    def ensure_base_directories(self) -> None:
        for path in (
            self.bin,
            self.manager_versions,
            self.app_versions,
            self.services,
            self.environments,
            self.state,
            self.logs,
            self.cache,
            self.staging,
            self.backups,
            self.data,
        ):
            path.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def contains(root: Path, candidate: Path) -> bool:
        root = _resolved(root)
        candidate = _resolved(candidate)
        try:
            return os.path.commonpath((str(root), str(candidate))) == str(root)
        except ValueError:
            return False

    def require_within(
        self,
        candidate: str | os.PathLike[str],
        *,
        roots: tuple[Path, ...] | None = None,
        allow_root: bool = False,
    ) -> Path:
        resolved = _resolved(candidate)
        allowed = roots or self.owned_roots
        for root in allowed:
            canonical_root = _resolved(root)
            if self.contains(canonical_root, resolved):
                if resolved == canonical_root and not allow_root:
                    continue
                return resolved
        raise UnsafePathError(str(resolved))

    def service_root(self, component_id: str) -> Path:
        safe_id = str(component_id).strip().lower()
        if not safe_id or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for character in safe_id):
            raise ValueError("Component ID is not filesystem-safe.")
        return self.services / safe_id


@dataclass(frozen=True, slots=True)
class ManagerContext:
    layout: WorkspaceLayout
    system: str = field(default_factory=platform.system)
    architecture: str = field(default_factory=platform.machine)
    clock: Clock = field(default_factory=SystemClock)
    cancellation: CancellationToken = field(default_factory=CancellationToken)
    event_sink: EventSink = field(default_factory=NullEventSink)
    environment: Mapping[str, str] = field(default_factory=lambda: dict(os.environ))

    @property
    def platform_id(self) -> str:
        return f"{self.system.strip().lower()}_{self.architecture.strip().lower()}"

    def with_event_sink(self, event_sink: EventSink) -> "ManagerContext":
        return ManagerContext(
            layout=self.layout,
            system=self.system,
            architecture=self.architecture,
            clock=self.clock,
            cancellation=self.cancellation,
            event_sink=event_sink,
            environment=self.environment,
        )
