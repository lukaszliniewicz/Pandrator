"""Declarative component catalogue and narrow driver contracts."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..context import ManagerContext
from ..models import (
    ComponentDefinition,
    ComponentInspection,
    DesiredComponentState,
    HealthResult,
    ManagedService,
    ResolvedComponentState,
    TaskSpec,
)


class ComponentDriver(Protocol):
    driver_id: str

    def inspect(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState | None,
    ) -> ComponentInspection: ...

    def resolve(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
    ) -> ResolvedComponentState: ...

    def plan_install(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]: ...

    def plan_update(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]: ...

    def plan_repair(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        desired: DesiredComponentState,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]: ...

    def plan_remove(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        inspection: ComponentInspection,
    ) -> tuple[TaskSpec, ...]: ...

    def launch_spec(
        self,
        context: ManagerContext,
        definition: ComponentDefinition,
        resolved: ResolvedComponentState,
    ) -> ManagedService | None: ...

    def health_probe(
        self,
        context: ManagerContext,
        service: ManagedService,
    ) -> HealthResult: ...


class ComponentRegistry:
    def __init__(
        self,
        definitions: Iterable[ComponentDefinition] = (),
        drivers: Iterable[ComponentDriver] = (),
    ) -> None:
        self._definitions: dict[str, ComponentDefinition] = {}
        self._drivers: dict[str, ComponentDriver] = {}
        for driver in drivers:
            self.register_driver(driver)
        for definition in definitions:
            self.register(definition)
        self.validate()

    def register_driver(self, driver: ComponentDriver) -> None:
        driver_id = str(driver.driver_id).strip()
        if not driver_id:
            raise ValueError("Component driver ID is required.")
        if driver_id in self._drivers:
            raise ValueError(f"Duplicate component driver ID: {driver_id}")
        self._drivers[driver_id] = driver

    def register(self, definition: ComponentDefinition) -> None:
        if definition.id in self._definitions:
            raise ValueError(f"Duplicate component ID: {definition.id}")
        self._definitions[definition.id] = definition

    def validate(self) -> None:
        service_keys: dict[str, str] = {}
        ports: dict[int, str] = {}
        environments: dict[str, str] = {}
        owned_paths: dict[str, str] = {}
        for component_id, definition in self._definitions.items():
            if definition.driver not in self._drivers:
                raise ValueError(
                    f"Component {component_id} references unknown driver {definition.driver}."
                )
            for dependency in definition.dependencies:
                if dependency not in self._definitions:
                    raise ValueError(
                        f"Component {component_id} depends on unknown component {dependency}."
                    )
                if dependency == component_id:
                    raise ValueError(f"Component {component_id} depends on itself.")
            for conflict in definition.conflicts:
                if conflict not in self._definitions:
                    raise ValueError(
                        f"Component {component_id} conflicts with unknown component {conflict}."
                    )
            if definition.service_key:
                previous = service_keys.setdefault(definition.service_key, component_id)
                if previous != component_id:
                    raise ValueError(
                        f"Components {previous} and {component_id} share service key "
                        f"{definition.service_key}."
                    )
            if definition.default_port:
                previous = ports.setdefault(definition.default_port, component_id)
                if previous != component_id:
                    raise ValueError(
                        f"Components {previous} and {component_id} share port "
                        f"{definition.default_port}."
                    )
            if definition.environment_owner:
                previous = environments.setdefault(
                    definition.environment_owner, component_id
                )
                if previous != component_id:
                    raise ValueError(
                        f"Components {previous} and {component_id} share environment "
                        f"{definition.environment_owner}."
                    )
            for path in definition.owned_paths:
                normalized = path.replace("\\", "/").strip("/").casefold()
                previous = owned_paths.setdefault(normalized, component_id)
                if previous != component_id:
                    raise ValueError(
                        f"Components {previous} and {component_id} both own path {path}."
                    )
        self._assert_acyclic()

    def _assert_acyclic(self) -> None:
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(component_id: str) -> None:
            if component_id in visited:
                return
            if component_id in visiting:
                raise ValueError(f"Component dependency cycle includes {component_id}.")
            visiting.add(component_id)
            for dependency in self._definitions[component_id].dependencies:
                visit(dependency)
            visiting.remove(component_id)
            visited.add(component_id)

        for component_id in self._definitions:
            visit(component_id)

    def definition(self, component_id: str) -> ComponentDefinition:
        try:
            return self._definitions[component_id]
        except KeyError:
            raise KeyError(f"Unknown component: {component_id}") from None

    def driver(self, component_id: str) -> ComponentDriver:
        definition = self.definition(component_id)
        return self._drivers[definition.driver]

    def definitions(self) -> tuple[ComponentDefinition, ...]:
        return tuple(
            sorted(
                self._definitions.values(),
                key=lambda definition: (
                    {
                        "core": 0,
                        "text_to_speech": 1,
                        "speech_to_text": 2,
                        "speech_to_speech": 3,
                        "training": 4,
                    }[definition.section.value],
                    definition.display_order,
                    definition.label.casefold(),
                    definition.id,
                ),
            )
        )

    def resolve_dependencies(self, component_ids: Iterable[str]) -> tuple[str, ...]:
        resolved: list[str] = []

        def visit(component_id: str) -> None:
            if component_id in resolved:
                return
            definition = self.definition(component_id)
            for dependency in definition.dependencies:
                visit(dependency)
            resolved.append(component_id)

        for component_id in component_ids:
            visit(component_id)
        return tuple(resolved)

    def validate_selection(self, desired: dict[str, DesiredComponentState]) -> None:
        present = {
            component_id
            for component_id, state in desired.items()
            if state.present
        }
        for component_id in present:
            definition = self.definition(component_id)
            conflicts = present.intersection(definition.conflicts)
            if conflicts:
                raise ValueError(
                    f"Component {component_id} conflicts with "
                    f"{', '.join(sorted(conflicts))}."
                )
