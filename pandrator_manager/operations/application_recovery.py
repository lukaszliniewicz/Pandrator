"""Immutable application startup checkpoints and recovery contract validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..components.slots import component_container
from ..models import ManagedProcessSpec, TaskSpec
from ..runtime_specs import PANDRATOR_CORE_SERVICES, PANDRATOR_SERVICE_START_ORDER
from ..supervisor import ProcessSupervisor
from .contracts import OperationTaskContext
from .task_files import _atomic_json

_INVALID_JOURNAL = "The application startup journal is invalid or does not match this operation task."


@dataclass(frozen=True, slots=True)
class _ApplicationServiceState:
    spec: ManagedProcessSpec | None
    was_running: bool
    desired_running: bool


@dataclass(frozen=True, slots=True)
class _ApplicationStartJournal:
    previous: dict[str, _ApplicationServiceState]
    target: dict[str, ManagedProcessSpec]
    previous_active_path: Path | None


def application_start_journal_path(
    execution: OperationTaskContext,
    task: TaskSpec,
) -> Path:
    try:
        if task.kind != "start_application" or task.component_id != "pandrator":
            raise ValueError
        layout = execution.context.layout
        staging = layout.require_within(layout.staging, roots=(layout.root,))
        operation_staging = layout.require_within(
            staging / execution.operation.id,
            roots=(staging,),
        )
        filename = hashlib.sha256(task.id.encode("utf-8")).hexdigest() + ".json"
        return layout.require_within(
            operation_staging / "application-starts" / filename,
            roots=(operation_staging,),
        )
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise RuntimeError(_INVALID_JOURNAL) from error


def _parse_application_start_journal(
    execution: OperationTaskContext,
    task: TaskSpec,
    payload: object,
) -> _ApplicationStartJournal:
    try:
        if (
            task.kind != "start_application"
            or task.component_id != "pandrator"
            or not isinstance(payload, dict)
            or type(payload.get("schema_version")) is not int
            or payload.get("schema_version") != 1
            or payload.get("operation_id") != execution.operation.id
            or payload.get("task_id") != task.id
            or payload.get("component_id") != "pandrator"
            or not isinstance(payload.get("previous"), dict)
            or not isinstance(payload.get("target"), dict)
            or "previous_active_path" not in payload
        ):
            raise ValueError
        raw_previous = payload["previous"]
        raw_target = payload["target"]
        if set(raw_previous) != set(PANDRATOR_SERVICE_START_ORDER) or set(raw_target) not in (
            set(PANDRATOR_CORE_SERVICES),
            set(PANDRATOR_SERVICE_START_ORDER),
        ):
            raise ValueError
        previous: dict[str, _ApplicationServiceState] = {}
        target: dict[str, ManagedProcessSpec] = {}
        for service_id in PANDRATOR_SERVICE_START_ORDER:
            raw_state = raw_previous[service_id]
            if (
                not isinstance(raw_state, dict)
                or "spec" not in raw_state
                or type(raw_state.get("was_running")) is not bool
                or type(raw_state.get("desired_running")) is not bool
            ):
                raise ValueError
            raw_spec = raw_state["spec"]
            if raw_spec is not None and not isinstance(raw_spec, dict):
                raise ValueError
            spec = ManagedProcessSpec.model_validate(raw_spec) if raw_spec is not None else None
            if spec is not None and (
                spec.component_id != "pandrator" or spec.service_id != service_id
            ):
                raise ValueError
            if spec is None and (raw_state["was_running"] or raw_state["desired_running"]):
                raise ValueError
            previous[service_id] = _ApplicationServiceState(
                spec=spec,
                was_running=raw_state["was_running"],
                desired_running=raw_state["desired_running"],
            )
            if service_id in raw_target:
                if not isinstance(raw_target[service_id], dict):
                    raise ValueError
                target_spec = ManagedProcessSpec.model_validate(raw_target[service_id])
                if target_spec.component_id != "pandrator" or target_spec.service_id != service_id:
                    raise ValueError
                target[service_id] = target_spec
        raw_path = payload["previous_active_path"]
        previous_active_path = None
        if raw_path is not None:
            if not isinstance(raw_path, str) or not raw_path:
                raise ValueError
            layout = execution.context.layout
            container = layout.require_within(
                component_container(layout, "pandrator"), roots=(layout.root,),
            )
            versions = layout.require_within(container / "versions", roots=(container,))
            previous_active_path = layout.require_within(Path(raw_path), roots=(versions,))
        return _ApplicationStartJournal(previous, target, previous_active_path)
    except (OSError, ValueError, TypeError, RuntimeError) as error:
        raise RuntimeError(_INVALID_JOURNAL) from error


def load_application_start_journal(
    execution: OperationTaskContext,
    task: TaskSpec,
) -> _ApplicationStartJournal | None:
    path = application_start_journal_path(execution, task)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError, TypeError) as error:
        raise RuntimeError(_INVALID_JOURNAL) from error
    return _parse_application_start_journal(execution, task, payload)


def save_application_start_journal(
    execution: OperationTaskContext,
    task: TaskSpec,
    journal: _ApplicationStartJournal,
) -> _ApplicationStartJournal:
    path = application_start_journal_path(execution, task)
    payload = {
        "schema_version": 1,
        "operation_id": execution.operation.id,
        "task_id": task.id,
        "component_id": "pandrator",
        "previous": {
            service_id: {
                "spec": state.spec.model_dump(mode="json") if state.spec is not None else None,
                "was_running": state.was_running,
                "desired_running": state.desired_running,
            }
            for service_id, state in journal.previous.items()
        },
        "target": {
            service_id: spec.model_dump(mode="json") for service_id, spec in journal.target.items()
        },
        "previous_active_path": (
            str(journal.previous_active_path) if journal.previous_active_path is not None else None
        ),
    }
    normalized = _parse_application_start_journal(execution, task, payload)
    # Publish the normalized representation used by the first forward attempt.
    payload["previous_active_path"] = (
        str(normalized.previous_active_path) if normalized.previous_active_path is not None else None
    )
    _atomic_json(path, payload)
    return normalized


def require_application_specs_match(
    supervisor: ProcessSupervisor,
    journal: _ApplicationStartJournal,
) -> None:
    for service_id in PANDRATOR_SERVICE_START_ORDER:
        current = supervisor.spec(service_id)
        if current is not None and current not in (
            journal.previous[service_id].spec,
            journal.target.get(service_id),
        ):
            raise RuntimeError(
                "The registered application specifications do not match startup recovery."
            )
