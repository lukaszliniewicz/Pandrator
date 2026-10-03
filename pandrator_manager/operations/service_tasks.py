"""Managed service task execution and rollback without per-instance state."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..models import ManagedProcessSpec, TaskSpec
from ..network import load_network_configuration
from ..runtime_specs import (
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_SERVICE_STOP_ORDER,
    PANDRATOR_WORKER_SERVICE,
    pandrator_runtime_specs,
)
from .contracts import OperationTaskContext, UnsupportedTask
from .source_tasks import ComponentSourceTasks
from .task_files import _atomic_json


class ServiceTasks(ComponentSourceTasks):
    """Service validation, application startup and stop task methods."""

    @staticmethod
    def _application_runtime_specs(
        execution: OperationTaskContext,
        preferences: dict[str, str],
    ) -> tuple[ManagedProcessSpec, ...]:
        exposure = load_network_configuration(
            execution.context.layout,
            environment=execution.context.environment,
        ).application
        return pandrator_runtime_specs(
            execution.context.layout,
            exposure=exposure,
            preferences=preferences,
        )

    def _execute_validate_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        if execution.supervisor is None or execution.service_spec_factory is None:
            raise UnsupportedTask(f"{definition.label} service validation is unavailable.")
        desired = execution.plan.desired[definition.id]
        resolved = execution.registry.driver(definition.id).resolve(
            execution.context,
            definition,
            desired,
        )
        spec = execution.service_spec_factory(definition.id, resolved)
        if spec is None:
            raise UnsupportedTask(f"{definition.label} has no managed runtime specification.")
        previous_spec = execution.supervisor.replace_spec(spec)
        try:
            service = execution.supervisor.start(spec.service_id)
        except Exception:
            if previous_spec is not None:
                execution.supervisor.replace_spec(previous_spec)
            else:
                execution.supervisor.unregister(spec.service_id)
            raise
        stop_result = execution.prior_results.get(f"{definition.id}:stop", {})
        keep_running = bool(
            desired.options.get("start_after_install", False)
            or stop_result.get("was_running", False)
            or stop_result.get("desired_running", False)
        )
        if not keep_running:
            execution.supervisor.stop(spec.service_id)
        return {
            "service_id": spec.service_id,
            "health": service.health.model_dump(mode="json") if service.health else None,
            "kept_running": keep_running,
            "previous_spec": (
                previous_spec.model_dump(mode="json") if previous_spec is not None else None
            ),
        }

    def _rollback_validate_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if execution.supervisor is None or not result.get("service_id"):
            return
        service_id = str(result["service_id"])
        if result.get("kept_running"):
            execution.supervisor.stop(service_id)
        previous = result.get("previous_spec")
        if isinstance(previous, dict):
            execution.supervisor.replace_spec(ManagedProcessSpec.model_validate(previous))
        else:
            execution.supervisor.unregister(service_id)

    def _execute_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        if execution.supervisor is None:
            return {
                "started": False,
                "error": "The process supervisor is unavailable.",
            }
        preferences: dict[str, str] = {}
        crispasr = execution.plan.desired.get("crispasr")
        if crispasr is not None and crispasr.present:
            engine = str(crispasr.options.get("engine") or "").strip()
            quantization = str(
                crispasr.quantization or crispasr.options.get("quantization") or ""
            ).strip()
            if engine:
                preferences["CRISPASR_DEFAULT_ENGINE"] = engine
            if quantization:
                preferences["CRISPASR_DEFAULT_QUANTIZATION"] = quantization
        try:
            specifications = self._application_runtime_specs(execution, preferences)
            running = {
                service.id
                for service in execution.supervisor.snapshot()
                if service.process is not None
            }
            # A component update activates a new application slot before this
            # task refreshes the launch contracts. ProcessSupervisor correctly
            # refuses to replace a running contract, so quiesce every
            # application-owned service first.
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if service_id in running:
                    execution.supervisor.stop(service_id)
            selected_ids = {specification.service_id for specification in specifications}
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if (
                    service_id not in selected_ids
                    and execution.supervisor.spec(service_id) is not None
                ):
                    execution.supervisor.unregister(service_id)
            for specification in specifications:
                execution.supervisor.replace_spec(specification)
            service = execution.supervisor.start(PANDRATOR_WORKER_SERVICE)
            mcp_error = None
            if PANDRATOR_MCP_SERVICE in selected_ids:
                try:
                    execution.supervisor.start(PANDRATOR_MCP_SERVICE)
                except Exception as error:
                    mcp_error = str(error) or "Pandrator MCP could not be started."
                    execution.context.event_sink.emit(
                        "application.mcp_start_failed",
                        {"error": mcp_error, "action": "install"},
                        component_id="pandrator",
                        operation_id=execution.operation.id,
                        service_id=PANDRATOR_MCP_SERVICE,
                    )
        except Exception as error:
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                try:
                    if execution.supervisor.spec(service_id) is not None:
                        execution.supervisor.stop(service_id)
                except Exception:
                    pass
            execution.context.event_sink.emit(
                "application.autostart_failed",
                {"error": str(error)},
                component_id="pandrator",
                operation_id=execution.operation.id,
            )
            return {"started": False, "error": str(error)}
        execution.context.event_sink.emit(
            "application.started",
            {"action": "install"},
            component_id="pandrator",
            operation_id=execution.operation.id,
        )
        return {
            "started": True,
            "service_id": service.id,
            "health": (
                service.health.model_dump(mode="json") if service.health is not None else None
            ),
            "mcp_error": mcp_error,
        }

    def _rollback_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if execution.supervisor is None or not result.get("started"):
            return
        for service_id in PANDRATOR_SERVICE_STOP_ORDER:
            if execution.supervisor.spec(service_id) is not None:
                execution.supervisor.stop(service_id)

    @staticmethod
    def _service_stop_journal_path(
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> Path:
        layout = execution.context.layout
        staging = layout.require_within(layout.staging, roots=(layout.root,))
        operation_staging = layout.require_within(
            staging / execution.operation.id,
            roots=(staging,),
        )
        filename = hashlib.sha256(task.id.encode("utf-8")).hexdigest() + ".json"
        return layout.require_within(
            operation_staging / "service-stops" / filename,
            roots=(operation_staging,),
        )

    @staticmethod
    def _load_service_stop_receipt(
        execution: OperationTaskContext,
        task: TaskSpec,
        *,
        component_id: str,
        service_id: str,
    ) -> dict | None:
        try:
            path = ServiceTasks._service_stop_journal_path(execution, task)
            journal = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as error:
            raise RuntimeError(
                "The service stop journal is invalid or does not match this operation task."
            ) from error
        if (
            not isinstance(journal, dict)
            or type(journal.get("schema_version")) is not int
            or journal.get("schema_version") != 1
            or journal.get("operation_id") != execution.operation.id
            or journal.get("task_id") != task.id
            or journal.get("component_id") != component_id
            or journal.get("service_id") != service_id
            or type(journal.get("was_running")) is not bool
            or type(journal.get("desired_running")) is not bool
        ):
            raise RuntimeError(
                "The service stop journal is invalid or does not match this operation task."
            )
        return {
            "service_id": journal["service_id"],
            "was_running": journal["was_running"],
            "desired_running": journal["desired_running"],
        }

    def _execute_stop_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        definition = self._definition(execution, task)
        if not definition.service_key:
            return {
                "service_id": None,
                "was_running": False,
                "desired_running": False,
            }
        result = self._load_service_stop_receipt(
            execution,
            task,
            component_id=definition.id,
            service_id=definition.service_key,
        )
        if execution.supervisor is None:
            if result is not None:
                raise RuntimeError(
                    "The process supervisor is unavailable for service stop recovery."
                )
            return {
                "service_id": None,
                "was_running": False,
                "desired_running": False,
            }
        snapshots = {service.id: service for service in execution.supervisor.snapshot()}
        previous = snapshots.get(definition.service_key)
        if result is None:
            result = {
                "service_id": definition.service_key,
                "was_running": bool(previous is not None and previous.process is not None),
                "desired_running": bool(previous is not None and previous.desired_running),
            }
            _atomic_json(
                ServiceTasks._service_stop_journal_path(execution, task),
                {
                    "schema_version": 1,
                    "operation_id": execution.operation.id,
                    "task_id": task.id,
                    "component_id": definition.id,
                    **result,
                },
            )
        if previous is not None:
            execution.supervisor.stop(definition.service_key)
        return result

    def _rollback_stop_service(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
        result: dict,
    ) -> None:
        if not result:
            definition = self._definition(execution, task)
            if not definition.service_key:
                return
            result = self._load_service_stop_receipt(
                execution,
                task,
                component_id=definition.id,
                service_id=definition.service_key,
            ) or {}
        if (
            (result.get("was_running") or result.get("desired_running"))
            and result.get("service_id")
        ):
            if execution.supervisor is None:
                raise RuntimeError(
                    "The process supervisor is unavailable for service rollback."
                )
            execution.supervisor.start(str(result["service_id"]))
