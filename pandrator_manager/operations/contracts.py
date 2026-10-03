"""Shared contracts for durable operation task execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..components import ComponentRegistry
from ..context import CancellationSignal, ManagerContext
from ..models import ManagedProcessSpec, OperationPlan, OperationRecord
from ..releases.authority import ReleaseAuthority
from ..state import ManagerStore
from ..supervisor import ProcessSupervisor


class UnsupportedTask(RuntimeError):
    pass


@dataclass(slots=True)
class OperationTaskContext:
    context: ManagerContext
    store: ManagerStore
    registry: ComponentRegistry
    supervisor: ProcessSupervisor | None
    operation: OperationRecord
    plan: OperationPlan
    prior_results: dict[str, dict]
    cancellation: CancellationSignal
    release_authority: ReleaseAuthority | None = None
    service_spec_factory: Callable[[str, object], ManagedProcessSpec | None] | None = None

    def check_cancelled(self) -> None:
        self.cancellation.raise_if_requested()
