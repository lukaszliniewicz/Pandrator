"""Shared presentation model for optional-engine tray controls."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


@dataclass(frozen=True, slots=True)
class EngineMenuItem:
    """One installed engine and the runtime action currently available."""

    component_id: str
    service_id: str
    label: str
    state: str
    state_label: str
    action: str | None
    action_label: str
    enabled: bool
    is_running: bool


@dataclass(frozen=True, slots=True)
class McpMenuItem:
    """MCP sidecar state and its recovery action."""

    available: bool = False
    service_id: str = "pandrator.mcp"
    state: str = "unavailable"
    state_label: str = "Not available"
    action: str | None = None
    action_label: str = "MCP unavailable"
    enabled: bool = False
    is_running: bool = False


@dataclass(frozen=True, slots=True)
class EngineMenuSnapshot:
    """Immutable tray menu state, safe to exchange between UI threads."""

    items: tuple[EngineMenuItem, ...] = ()
    available: bool = True
    busy: bool = False
    message: str = ""
    mcp: McpMenuItem = field(default_factory=McpMenuItem)

    @property
    def running_count(self) -> int:
        return sum(1 for item in self.items if item.is_running)

    @property
    def summary(self) -> str:
        if not self.available:
            return self.message or "Engine status unavailable"
        count = len(self.items)
        if count == 0:
            return "No optional engines installed"
        noun = "engine" if count == 1 else "engines"
        return f"{self.running_count}/{count} {noun} running"


def unavailable_engine_snapshot(
    message: str = "Engine status unavailable",
) -> EngineMenuSnapshot:
    return EngineMenuSnapshot(available=False, message=message)


def _runtime_state(
    inspection_state: str,
    service: Mapping[str, Any] | None,
) -> tuple[str, str, bool]:
    if service is None:
        if inspection_state == "degraded":
            return "degraded", "Needs repair", False
        return "unavailable", "Status unavailable", False

    process = service.get("process")
    desired_running = bool(service.get("desired_running"))
    health = service.get("health") or {}
    health_state = str(health.get("state") or "unknown")

    if process:
        if health_state == "healthy":
            return "running", "Running", True
        if health_state == "starting":
            return "starting", "Starting", True
        if health_state == "degraded":
            return "degraded", "Running with warnings", True
        if health_state == "unhealthy":
            return "unhealthy", "Not responding", True
        if health_state == "failed":
            return "failed", "Failed", True
        return "starting", "Starting", True

    if desired_running:
        if health_state == "failed":
            return "failed", "Failed", False
        return "restarting", "Restarting", False

    if inspection_state == "degraded":
        return "degraded", "Needs repair", False
    if health_state == "failed":
        return "failed", "Failed", False
    return "stopped", "Stopped", False


def _mcp_menu_item(
    service: Mapping[str, Any] | None,
    *,
    busy: bool,
) -> McpMenuItem:
    if service is None:
        return McpMenuItem()
    state, state_label, is_running = _runtime_state("present", service)
    wants_to_run = bool(service.get("process") or service.get("desired_running"))
    action = "restart" if wants_to_run else "start"
    return McpMenuItem(
        available=True,
        service_id=str(service.get("id") or "pandrator.mcp"),
        state=state,
        state_label=state_label,
        action=action,
        action_label="Restart MCP" if action == "restart" else "Start MCP",
        enabled=not busy,
        is_running=is_running,
    )


def build_engine_menu_snapshot(
    components: Iterable[Mapping[str, Any]],
    services: Iterable[Mapping[str, Any]],
    *,
    busy: bool = False,
) -> EngineMenuSnapshot:
    """Build optional-engine state from the public Manager API payloads."""

    service_items = list(services)
    by_id = {
        str(service.get("id") or ""): service
        for service in service_items
        if service.get("id")
    }
    by_component = {
        str(service.get("component_id") or ""): service
        for service in service_items
        if service.get("component_id")
    }
    items: list[EngineMenuItem] = []
    for component in components:
        definition = component.get("definition") or {}
        inspection = component.get("inspection") or {}
        component_id = str(definition.get("id") or "")
        service_id = str(definition.get("service_key") or "")
        inspection_state = str(inspection.get("state") or "unknown")
        if (
            not component_id
            or not service_id
            or inspection_state not in {"present", "degraded"}
        ):
            continue

        service = by_id.get(service_id) or by_component.get(component_id)
        state, state_label, is_running = _runtime_state(
            inspection_state,
            service,
        )
        supported = set(definition.get("supported_actions") or ())
        wants_to_run = bool(
            service
            and (service.get("process") or service.get("desired_running"))
        )
        if wants_to_run and "stop" in supported:
            action = "stop"
            action_label = f"Stop {definition.get('label') or component_id}"
        elif (
            service is not None
            and inspection_state == "present"
            and "start" in supported
        ):
            action = "start"
            action_label = f"Start {definition.get('label') or component_id}"
        else:
            action = None
            action_label = "No runtime action available"

        items.append(
            EngineMenuItem(
                component_id=component_id,
                service_id=str(
                    (service or {}).get("id") or service_id
                ),
                label=str(definition.get("label") or component_id),
                state=state,
                state_label=state_label,
                action=action,
                action_label=action_label,
                enabled=bool(action) and not busy,
                is_running=is_running,
            )
        )

    items.sort(key=lambda item: (item.label.casefold(), item.component_id))
    return EngineMenuSnapshot(
        items=tuple(items),
        busy=busy,
        mcp=_mcp_menu_item(by_id.get("pandrator.mcp"), busy=busy),
    )
