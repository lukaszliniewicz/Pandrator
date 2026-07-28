"""Pure component inspection and immutable operation planning."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from .components import ComponentRegistry
from .context import ManagerContext
from .environments import PIXI_VERSION
from .errors import RevisionConflict
from .models import (
    ComponentInspection,
    ComponentState,
    ConfirmationRequirement,
    DesiredComponentState,
    OperationKind,
    OperationPlan,
    TaskSpec,
)
from .preflight import HostPreflight


class Planner:
    def __init__(
        self,
        context: ManagerContext,
        registry: ComponentRegistry,
        *,
        plan_ttl_seconds: int = 15 * 60,
    ) -> None:
        self.context = context
        self.registry = registry
        self.preflight = HostPreflight(context, registry)
        self.plan_ttl_seconds = max(60, int(plan_ttl_seconds))

    def inspect(
        self,
        component_id: str,
        desired: DesiredComponentState | None = None,
    ) -> ComponentInspection:
        definition = self.registry.definition(component_id)
        return self.registry.driver(component_id).inspect(
            self.context,
            definition,
            desired,
        )

    def inspect_all(
        self,
        desired: dict[str, DesiredComponentState] | None = None,
    ) -> dict[str, ComponentInspection]:
        desired = desired or {}
        return {
            definition.id: self.inspect(
                definition.id,
                desired.get(definition.id),
            )
            for definition in self.registry.definitions()
        }

    def normalize_desired(
        self,
        desired: dict[str, DesiredComponentState],
    ) -> dict[str, DesiredComponentState]:
        normalized = dict(desired)
        requested_present = [
            component_id
            for component_id, state in normalized.items()
            if state.present
        ]
        for component_id in self.registry.resolve_dependencies(requested_present):
            normalized.setdefault(component_id, DesiredComponentState())
        self.registry.validate_selection(normalized)
        return normalized

    def create_plan(
        self,
        *,
        kind: OperationKind,
        desired: dict[str, DesiredComponentState],
        expected_revision: int,
        actual_revision: int,
    ) -> OperationPlan:
        if expected_revision != actual_revision:
            raise RevisionConflict(expected_revision, actual_revision)
        normalized = self.normalize_desired(desired)
        inspections = {
            component_id: self.inspect(component_id, state)
            for component_id, state in normalized.items()
        }
        ordered_components = list(
            self.registry.resolve_dependencies(
                component_id
                for component_id, state in normalized.items()
                if state.present
            )
        )
        ordered_components.extend(
            component_id
            for component_id in normalized
            if component_id not in ordered_components
        )
        task_groups: dict[str, tuple[TaskSpec, ...]] = {}
        confirmations: list[ConfirmationRequirement] = []
        warnings: list[str] = []

        for component_id in ordered_components:
            desired_state = normalized[component_id]
            definition = self.registry.definition(component_id)
            driver = self.registry.driver(component_id)
            inspection = inspections[component_id]
            if inspection.state == ComponentState.UNSUPPORTED and desired_state.present:
                raise ValueError(inspection.problems[0])

            if desired_state.present:
                if kind == OperationKind.UPDATE and inspection.state == ComponentState.PRESENT:
                    self._require_action(definition, "update")
                    tasks = driver.plan_update(
                        self.context, definition, desired_state, inspection
                    )
                elif kind == OperationKind.REPAIR or inspection.state == ComponentState.DEGRADED:
                    self._require_action(definition, "repair")
                    tasks = driver.plan_repair(
                        self.context, definition, desired_state, inspection
                    )
                elif inspection.state in {
                    ComponentState.ABSENT,
                    ComponentState.UNKNOWN,
                }:
                    self._require_action(definition, "install")
                    tasks = driver.plan_install(
                        self.context, definition, desired_state, inspection
                    )
                else:
                    tasks = ()
            elif inspection.state in {
                ComponentState.PRESENT,
                ComponentState.DEGRADED,
            }:
                self._require_action(definition, "remove")
                tasks = driver.plan_remove(self.context, definition, inspection)
                confirmations.append(
                    ConfirmationRequirement(
                        kind="destructive",
                        key=f"remove:{component_id}",
                        message=f"Remove manager-owned files for {definition.label}.",
                    )
                )
            else:
                tasks = ()

            if tasks and desired_state.present and definition.license_name:
                confirmations.append(
                    ConfirmationRequirement(
                        kind="license",
                        key=f"license:{component_id}",
                        message=(
                            f"Review and accept the {definition.license_name} terms "
                            f"for {definition.label}."
                        ),
                        url=definition.license_url,
                    )
                )
            task_groups[component_id] = tasks

        tasks = self._link_component_dependencies(task_groups)
        preflight = self.preflight.evaluate(
            desired=normalized,
            tasks=tasks,
        )
        self.preflight.require_success(preflight)
        warnings.extend(
            check.message
            for check in preflight
            if check.status == "warning"
        )
        if tasks:
            preflight_task = TaskSpec(
                id="operation:preflight",
                kind="preflight_operation",
                label="Recheck host prerequisites",
                resource_locks=("host:preflight",),
                inputs={
                    "planned_checks": [
                        check.model_dump(mode="json")
                        for check in preflight
                    ]
                },
                verification={"strategy": "repeat_before_mutation"},
                rollback={"strategy": "none"},
            )
            needs_pixi = any(
                task.kind == "stage_component"
                and task.component_id is not None
                and "pixi"
                in self.registry.definition(
                    task.component_id
                ).required_runtime_tools
                for task in tasks
            )
            pixi_task = (
                TaskSpec(
                    id="runtime:pixi",
                    kind="ensure_runtime_tool",
                    label=f"Ensure verified Pixi {PIXI_VERSION}",
                    dependencies=(preflight_task.id,),
                    resource_locks=("runtime-tool:pixi",),
                    inputs={"tool": "pixi", "version": PIXI_VERSION},
                    expected_outputs=("bin/pixi",),
                    verification={
                        "strategy": "version_command",
                        "version": PIXI_VERSION,
                    },
                    rollback={"strategy": "restore_previous_runtime_tool"},
                )
                if needs_pixi
                else None
            )
            adjusted_tasks: list[TaskSpec] = []
            for task in tasks:
                dependencies = list(task.dependencies)
                if not dependencies:
                    dependencies.append(preflight_task.id)
                if (
                    pixi_task is not None
                    and task.kind == "stage_component"
                    and task.component_id is not None
                    and "pixi"
                    in self.registry.definition(
                        task.component_id
                    ).required_runtime_tools
                ):
                    dependencies.append(pixi_task.id)
                unique_dependencies = tuple(dict.fromkeys(dependencies))
                adjusted_tasks.append(
                    task.model_copy(
                        update={"dependencies": unique_dependencies}
                    )
                    if unique_dependencies != task.dependencies
                    else task
                )
            tasks = (
                preflight_task,
                *((pixi_task,) if pixi_task is not None else ()),
                *adjusted_tasks,
            )
        if not tasks:
            warnings.append("The requested state already matches the inspected host.")
        created_at = datetime.fromtimestamp(self.context.clock.time(), timezone.utc)
        expires_at = created_at + timedelta(seconds=self.plan_ttl_seconds)
        plan_id = str(uuid.uuid4())
        plan_payload: dict[str, Any] = {
            "id": plan_id,
            "kind": kind.value,
            "workspace": str(self.context.layout.workspace),
            "expected_revision": expected_revision,
            "desired": {
                key: value.model_dump(mode="json")
                for key, value in sorted(normalized.items())
            },
            "inspections": {
                key: value.model_dump(mode="json")
                for key, value in sorted(inspections.items())
            },
            "tasks": [task.model_dump(mode="json") for task in tasks],
            "preflight": [
                check.model_dump(mode="json")
                for check in preflight
            ],
            "confirmations": [
                requirement.model_dump(mode="json")
                for requirement in confirmations
            ],
            "warnings": warnings,
            "created_at": created_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        digest = hashlib.sha256(
            json.dumps(
                plan_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return OperationPlan(
            id=plan_id,
            kind=kind,
            workspace=str(self.context.layout.workspace),
            expected_revision=expected_revision,
            desired=normalized,
            inspections=inspections,
            tasks=tasks,
            preflight=preflight,
            confirmations=tuple(confirmations),
            warnings=tuple(warnings),
            estimated_download_bytes=sum(
                task.estimated_download_bytes for task in tasks
            ),
            estimated_disk_bytes=sum(task.estimated_disk_bytes for task in tasks),
            created_at=created_at,
            expires_at=expires_at,
            digest=digest,
        )

    @staticmethod
    def _require_action(definition, action: str) -> None:
        if action not in definition.supported_actions:
            raise ValueError(
                f"{definition.label} does not yet support the '{action}' "
                "operation in Pandrator Manager."
            )

    def _link_component_dependencies(
        self,
        task_groups: dict[str, tuple[TaskSpec, ...]],
    ) -> tuple[TaskSpec, ...]:
        linked: list[TaskSpec] = []
        ordered: list[str] = []

        def visit(component_id: str) -> None:
            if component_id in ordered:
                return
            for dependency in self.registry.definition(component_id).dependencies:
                if dependency in task_groups:
                    visit(dependency)
            ordered.append(component_id)

        for component_id in task_groups:
            visit(component_id)
        for component_id in ordered:
            tasks = task_groups[component_id]
            if not tasks:
                continue
            definition = self.registry.definition(component_id)
            dependency_tasks = tuple(
                task_groups[dependency][-1].id
                for dependency in definition.dependencies
                if task_groups.get(dependency)
            )
            if dependency_tasks:
                first = tasks[0].model_copy(
                    update={
                        "dependencies": tuple(
                            dict.fromkeys((*tasks[0].dependencies, *dependency_tasks))
                        )
                    }
                )
                tasks = (first, *tasks[1:])
            linked.extend(tasks)
        return tuple(linked)
