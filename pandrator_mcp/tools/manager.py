"""Model-safe Manager inspection, planning, and runtime handlers."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from ..context import McpRuntime
from ..errors import NextAction
from ..results import ToolOutcome
from ..schemas import (
    ControlRuntimeInput,
    ExecuteComponentPlanInput,
    PlanComponentChangeInput,
)
from ..work_mapping import (
    manager_work_projection,
    manager_work_reference,
)


def _text(value: object, maximum: int = 2_000) -> str:
    return " ".join(str(value or "").split())[:maximum]


def _target_binding(runtime: McpRuntime) -> dict[str, Any]:
    profile = runtime.profile
    if profile is None:
        return {
            "name": runtime.settings.target_name,
            "mode": None,
        }
    return {
        "name": profile.name,
        "mode": profile.mode.value,
        "canonical_application_origin": (
            profile.expected_identity.canonical_application_origin
            or profile.application_origin
        ),
        "application_instance_id": (
            profile.expected_identity.application_instance_id
        ),
        "manager_instance_id": (
            profile.expected_identity.manager_instance_id
        ),
        "direct_recovery_enrolled": bool(
            profile.manager_recovery_origin
            and profile.manager_recovery_credential
        ),
    }


def manager_status_projection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    if payload.get("available") is False:
        error = (
            payload.get("error")
            if isinstance(payload.get("error"), dict)
            else {}
        )
        return {
            "schema_version": "1",
            "available": False,
            "error": {
                "code": _text(
                    error.get("code") or "manager_unavailable",
                    160,
                ),
                "message": _text(
                    error.get("message")
                    or "Pandrator Manager is unavailable."
                ),
            },
        }
    status = (
        payload.get("status")
        if isinstance(payload.get("status"), dict)
        else payload
    )
    health = (
        payload.get("health")
        if isinstance(payload.get("health"), dict)
        else {}
    )
    identity = (
        payload.get("identity")
        if isinstance(payload.get("identity"), dict)
        else {}
    )
    return {
        "schema_version": "1",
        "available": True,
        "gateway": _text(payload.get("gateway"), 80) or None,
        "manager_version": (
            status.get("manager_version")
            or identity.get("manager_version")
            or health.get("version")
        ),
        "api_versions": list(status.get("api_versions") or ())[:20],
        "manager_instance_id": (
            status.get("instance_id")
            or identity.get("manager_instance_id")
            or health.get("instance_id")
        ),
        "configuration_revision": status.get(
            "configuration_revision"
        ),
        "ready": bool(status.get("ready")),
        "capabilities": [
            _text(value, 160)
            for value in list(status.get("capabilities") or ())[:100]
        ],
        "active_operation_id": (
            _text(status.get("active_operation_id"), 160) or None
        ),
        "health": {
            "status": _text(
                health.get("status") or health.get("state"),
                80,
            )
            or None,
            "protocol_version": (
                _text(health.get("protocol_version"), 80) or None
            ),
        },
    }


def manager_doctor_projection(
    payload: dict[str, Any],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    source = payload.get("checks")
    if isinstance(source, list):
        for item in source[:250]:
            if not isinstance(item, dict):
                continue
            checks.append(
                {
                    "id": _text(item.get("id"), 160),
                    "category": _text(item.get("category"), 80),
                    "status": _text(item.get("status"), 40),
                    "message": _text(item.get("message")),
                    "repairable": bool(item.get("repairable")),
                    "repair_target": (
                        _text(item.get("repair_target"), 160)
                        or None
                    ),
                }
            )
    summary = (
        payload.get("summary")
        if isinstance(payload.get("summary"), dict)
        else {}
    )
    return {
        "schema_version": "1",
        "healthy": bool(payload.get("healthy")),
        "summary": {
            key: max(0, int(summary.get(key) or 0))
            for key in ("pass", "warning", "error")
        },
        "checks": checks,
        "generated_at": payload.get("generated_at"),
    }


def _reference_url(value: object) -> str | None:
    candidate = str(value or "").strip()
    if not candidate or len(candidate) > 2_048:
        return None
    parsed = urlsplit(candidate)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
    ):
        return None
    return candidate


def manager_plan_projection(
    payload: dict[str, Any],
    *,
    target: dict[str, Any],
) -> dict[str, Any]:
    tasks: list[dict[str, Any]] = []
    source_tasks = payload.get("tasks")
    if isinstance(source_tasks, list):
        for item in source_tasks[:300]:
            if not isinstance(item, dict):
                continue
            tasks.append(
                {
                    "id": _text(item.get("id"), 160),
                    "kind": _text(item.get("kind"), 160),
                    "label": _text(item.get("label"), 500),
                    "component_id": (
                        _text(item.get("component_id"), 160) or None
                    ),
                    "dependencies": [
                        _text(value, 160)
                        for value in list(
                            item.get("dependencies") or ()
                        )[:100]
                    ],
                    "resource_locks": [
                        _text(value, 160)
                        for value in list(
                            item.get("resource_locks") or ()
                        )[:100]
                    ],
                    "estimated_download_bytes": max(
                        0,
                        int(
                            item.get(
                                "estimated_download_bytes",
                                0,
                            )
                            or 0
                        ),
                    ),
                    "estimated_disk_bytes": max(
                        0,
                        int(
                            item.get(
                                "estimated_disk_bytes",
                                0,
                            )
                            or 0
                        ),
                    ),
                    "cancellation_boundary": bool(
                        item.get("cancellation_boundary", True)
                    ),
                }
            )
    preflight: list[dict[str, Any]] = []
    source_preflight = payload.get("preflight")
    if isinstance(source_preflight, list):
        for item in source_preflight[:200]:
            if not isinstance(item, dict):
                continue
            preflight.append(
                {
                    "code": _text(item.get("code"), 160),
                    "status": _text(item.get("status"), 40),
                    "message": _text(item.get("message")),
                }
            )
    confirmations: list[dict[str, Any]] = []
    source_confirmations = payload.get("confirmations")
    if isinstance(source_confirmations, list):
        for item in source_confirmations[:30]:
            if not isinstance(item, dict):
                continue
            confirmations.append(
                {
                    "kind": _text(item.get("kind"), 80),
                    "key": _text(item.get("key"), 500),
                    "message": _text(item.get("message")),
                    "reference_url": _reference_url(item.get("url")),
                }
            )
    desired: list[dict[str, Any]] = []
    source_desired = payload.get("desired")
    if isinstance(source_desired, dict):
        for component_id, state in sorted(source_desired.items()):
            if not isinstance(state, dict):
                continue
            options = (
                state.get("options")
                if isinstance(state.get("options"), dict)
                else {}
            )
            desired.append(
                {
                    "component_id": _text(component_id, 160),
                    "present": bool(state.get("present", True)),
                    "compute": _text(
                        state.get("compute") or "auto",
                        40,
                    ),
                    "quantization": (
                        _text(state.get("quantization"), 120) or None
                    ),
                    "options": {
                        _text(key, 80): value
                        for key, value in list(options.items())[:40]
                        if isinstance(
                            value,
                            (str, int, float, bool, type(None)),
                        )
                    },
                }
            )
    application_impacts: list[dict[str, Any]] = []
    application_payload = payload.get("application_impacts")
    if isinstance(application_payload, dict):
        bindings = application_payload.get(
            "managed_provider_bindings"
        )
        if isinstance(bindings, list):
            for item in bindings[:100]:
                if not isinstance(item, dict):
                    continue
                application_impacts.append(
                    {
                        "kind": _text(item.get("kind"), 120),
                        "component_id": _text(
                            item.get("component_id"),
                            160,
                        ),
                        "provider_id": _text(
                            item.get("provider_id"),
                            160,
                        ),
                        "service_id": _text(
                            item.get("service_id"),
                            160,
                        ),
                        "label": _text(item.get("label"), 300),
                        "selected_default": bool(
                            item.get("selected_default")
                        ),
                        "message": _text(item.get("message")),
                    }
                )
    return {
        "schema_version": "1",
        "type": "manager_plan",
        "plan_id": _text(payload.get("id"), 160),
        "plan_digest": _text(payload.get("digest"), 64),
        "kind": _text(payload.get("kind"), 80),
        "expected_revision": payload.get("expected_revision"),
        "target": target,
        "components": desired,
        "tasks": tasks,
        "preflight": preflight,
        "required_confirmations": confirmations,
        "warnings": [
            _text(value)
            for value in list(payload.get("warnings") or ())[:100]
        ],
        "application_impacts": {
            "managed_provider_bindings": application_impacts,
        },
        "estimated_download_bytes": max(
            0,
            int(payload.get("estimated_download_bytes") or 0),
        ),
        "estimated_disk_bytes": max(
            0,
            int(payload.get("estimated_disk_bytes") or 0),
        ),
        "created_at": payload.get("created_at"),
        "expires_at": payload.get("expires_at"),
    }


def _runtime_projection(
    payload: dict[str, Any],
    *,
    action: str,
    runtime_target: str,
) -> dict[str, Any]:
    source_services = (
        payload.get("services")
        if runtime_target == "application"
        else payload.get("items")
    )
    services: list[dict[str, Any]] = []
    if isinstance(source_services, list):
        for item in source_services[:100]:
            if not isinstance(item, dict):
                continue
            health = (
                item.get("health")
                if isinstance(item.get("health"), dict)
                else {}
            )
            services.append(
                {
                    "id": _text(item.get("id"), 160),
                    "component_id": _text(
                        item.get("component_id"),
                        160,
                    ),
                    "desired_running": bool(
                        item.get("desired_running")
                    ),
                    "running": bool(item.get("process")),
                    "health": {
                        "state": _text(
                            health.get("state"),
                            80,
                        )
                        or None,
                        "message": _text(
                            health.get("message")
                        )
                        or None,
                    },
                    "restart_count": max(
                        0,
                        int(item.get("restart_count") or 0),
                    ),
                }
            )
    return {
        "schema_version": "1",
        "action": action,
        "runtime_target": runtime_target,
        "application": (
            {
                "installed": bool(payload.get("installed")),
                "component_state": (
                    _text(payload.get("component_state"), 80) or None
                ),
                "running": bool(payload.get("running")),
                "healthy": bool(payload.get("healthy")),
            }
            if runtime_target == "application"
            else None
        ),
        "services": services,
    }


def manager_status(runtime: McpRuntime) -> dict[str, Any]:
    return manager_status_projection(runtime.manager.status())


def manager_doctor(runtime: McpRuntime) -> dict[str, Any]:
    return manager_doctor_projection(runtime.manager.doctor())


def plan_component_change(
    runtime: McpRuntime,
    arguments: PlanComponentChangeInput,
) -> ToolOutcome:
    raw_plan = runtime.manager.create_plan(
        kind=arguments.kind,
        desired=arguments.desired_states(),
        expected_revision=arguments.expected_revision,
        idempotency_key=arguments.idempotency_key,
    )
    plan = manager_plan_projection(
        raw_plan,
        target=_target_binding(runtime),
    )
    confirmations = [
        str(item.get("key") or "")
        for item in plan["required_confirmations"]
        if isinstance(item, dict) and item.get("key")
    ]
    return ToolOutcome(
        result=plan,
        next_actions=[
            NextAction(
                tool="pandrator_execute_component_plan",
                arguments={
                    "plan_id": plan["plan_id"],
                    "plan_digest": plan["plan_digest"],
                    "accepted_confirmations": confirmations,
                    "idempotency_key": (
                        f"manager-plan:{plan['plan_id']}"
                    ),
                },
                reason=(
                    "Execute only after the user reviews the exact "
                    "Manager tasks, impacts, preflight, target identity, "
                    "and required confirmations."
                ),
            )
        ],
    )


def execute_component_plan(
    runtime: McpRuntime,
    arguments: ExecuteComponentPlanInput,
) -> ToolOutcome:
    raw = runtime.manager.execute_plan(
        plan_id=arguments.plan_id,
        plan_digest=arguments.plan_digest,
        accepted_confirmations=arguments.accepted_confirmations,
        idempotency_key=arguments.idempotency_key,
    )
    operation = manager_work_projection(raw)
    work = manager_work_reference(raw)
    return ToolOutcome(
        result=operation,
        work=work,
        next_actions=[
            NextAction(
                tool="pandrator_get_work",
                arguments={
                    "work_type": "manager_operation",
                    "work_id": work.id,
                    "include_events": True,
                },
                reason=(
                    "Observe the durable Manager operation by its "
                    "returned handle."
                ),
            )
        ],
    )


def control_runtime(
    runtime: McpRuntime,
    arguments: ControlRuntimeInput,
) -> dict[str, Any]:
    raw = runtime.manager.control_runtime(
        action=arguments.action,
        target=arguments.runtime_target,
        service_ids=arguments.service_ids,
        idempotency_key=arguments.idempotency_key,
    )
    return _runtime_projection(
        raw,
        action=arguments.action,
        runtime_target=arguments.runtime_target,
    )
