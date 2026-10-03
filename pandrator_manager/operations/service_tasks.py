"""Managed service task execution and rollback without per-instance state."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from ..components.slots import active_component_path
from ..models import HealthState, ManagedProcessSpec, TaskSpec, TaskState
from ..network import load_network_configuration
from ..runtime_specs import (
    PANDRATOR_CORE_SERVICES,
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_SERVICE_START_ORDER,
    PANDRATOR_SERVICE_STOP_ORDER,
    PANDRATOR_WORKER_SERVICE,
    pandrator_runtime_specs,
)
from .application_recovery import (
    _ApplicationServiceState,
    _ApplicationStartJournal,
    load_application_start_journal,
    require_application_specs_match,
    save_application_start_journal,
)
from .contracts import OperationTaskContext, UnsupportedTask
from .source_tasks import ComponentSourceTasks
from .task_files import _atomic_json


@dataclass(frozen=True, slots=True)
class _ServiceValidationJournal:
    target_spec: ManagedProcessSpec
    previous_spec: ManagedProcessSpec | None
    kept_running: bool


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

    @staticmethod
    def _service_validation_journal_path(
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
            operation_staging / "service-validation" / filename,
            roots=(operation_staging,),
        )

    @staticmethod
    def _load_service_validation_journal(
        execution: OperationTaskContext,
        task: TaskSpec,
        *,
        component_id: str,
        service_id: str,
    ) -> _ServiceValidationJournal | None:
        try:
            path = ServiceTasks._service_validation_journal_path(execution, task)
            journal = json.loads(path.read_text(encoding="utf-8"))
            if (
                not isinstance(journal, dict)
                or type(journal.get("schema_version")) is not int
                or journal.get("schema_version") != 1
                or journal.get("operation_id") != execution.operation.id
                or journal.get("task_id") != task.id
                or journal.get("component_id") != component_id
                or journal.get("service_id") != service_id
                or type(journal.get("kept_running")) is not bool
                or not isinstance(journal.get("target_spec"), dict)
                or "previous_spec" not in journal
                or (
                    journal["previous_spec"] is not None
                    and not isinstance(journal["previous_spec"], dict)
                )
            ):
                raise ValueError
            target_spec = ManagedProcessSpec.model_validate(journal["target_spec"])
            previous_spec = (
                ManagedProcessSpec.model_validate(journal["previous_spec"])
                if journal["previous_spec"] is not None
                else None
            )
            if (
                target_spec.component_id != component_id
                or target_spec.service_id != service_id
                or (
                    previous_spec is not None
                    and (
                        previous_spec.component_id != component_id
                        or previous_spec.service_id != service_id
                    )
                )
            ):
                raise ValueError
            return _ServiceValidationJournal(
                target_spec=target_spec,
                previous_spec=previous_spec,
                kept_running=journal["kept_running"],
            )
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError, RuntimeError) as error:
            raise RuntimeError(
                "The service validation journal is invalid or does not match this operation task."
            ) from error

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
        if spec.component_id != definition.id or spec.service_id != definition.service_key:
            raise RuntimeError("The managed service specification does not match this component.")
        stop_result = execution.prior_results.get(f"{definition.id}:stop", {})
        keep_running = bool(
            desired.options.get("start_after_install", False)
            or stop_result.get("was_running", False)
            or stop_result.get("desired_running", False)
        )
        journal = self._load_service_validation_journal(
            execution,
            task,
            component_id=definition.id,
            service_id=spec.service_id,
        )
        if journal is None:
            def save_journal(previous: ManagedProcessSpec | None) -> None:
                _atomic_json(
                    ServiceTasks._service_validation_journal_path(execution, task),
                    {
                        "schema_version": 1,
                        "operation_id": execution.operation.id,
                        "task_id": task.id,
                        "component_id": definition.id,
                        "service_id": spec.service_id,
                        "kept_running": keep_running,
                        "target_spec": spec.model_dump(mode="json"),
                        "previous_spec": (
                            previous.model_dump(mode="json") if previous is not None else None
                        ),
                    },
                )

            previous_spec = execution.supervisor.replace_spec(
                spec,
                before_replace=save_journal,
            )
        else:
            if spec != journal.target_spec or keep_running != journal.kept_running:
                raise RuntimeError(
                    "The managed service specification or running intent changed during validation recovery."
                )
            current_spec = execution.supervisor.spec(spec.service_id)
            if current_spec is not None and current_spec not in (
                journal.target_spec,
                journal.previous_spec,
            ):
                raise RuntimeError(
                    "The registered service specification does not match validation recovery."
                )
            execution.supervisor.stop(spec.service_id)
            execution.supervisor.replace_spec(spec)
            previous_spec = journal.previous_spec
            keep_running = journal.kept_running
        try:
            service = execution.supervisor.start(spec.service_id)
        except Exception:
            if previous_spec is not None:
                execution.supervisor.replace_spec(previous_spec)
            else:
                execution.supervisor.unregister(spec.service_id)
            raise
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
        if result:
            if not result.get("service_id"):
                return
            if execution.supervisor is None:
                raise RuntimeError(
                    "The process supervisor is unavailable for service validation recovery."
                )
            service_id = str(result["service_id"])
            if result.get("kept_running"):
                execution.supervisor.stop(service_id)
            previous = result.get("previous_spec")
            if isinstance(previous, dict):
                execution.supervisor.replace_spec(ManagedProcessSpec.model_validate(previous))
            else:
                execution.supervisor.unregister(service_id)
            return
        definition = self._definition(execution, task)
        if not definition.service_key:
            return
        journal = self._load_service_validation_journal(
            execution,
            task,
            component_id=definition.id,
            service_id=definition.service_key,
        )
        if journal is None:
            return
        if execution.supervisor is None:
            raise RuntimeError(
                "The process supervisor is unavailable for service validation recovery."
            )
        current_spec = execution.supervisor.spec(definition.service_key)
        if current_spec is not None and current_spec not in (
            journal.target_spec,
            journal.previous_spec,
        ):
            raise RuntimeError(
                "The registered service specification does not match validation recovery."
            )
        execution.supervisor.stop(definition.service_key)
        if journal.previous_spec is not None:
            execution.supervisor.replace_spec(journal.previous_spec)
        else:
            execution.supervisor.unregister(definition.service_key)

    @staticmethod
    def _application_autostart_failed(
        execution: OperationTaskContext,
        error: Exception,
    ) -> dict:
        execution.context.event_sink.emit(
            "application.autostart_failed",
            {"error": str(error)},
            component_id="pandrator",
            operation_id=execution.operation.id,
        )
        return {"started": False, "error": str(error)}

    def _execute_start_application(
        self,
        execution: OperationTaskContext,
        task: TaskSpec,
    ) -> dict:
        journal = load_application_start_journal(execution, task)
        if execution.supervisor is None:
            if journal is not None:
                raise RuntimeError(
                    "The process supervisor is unavailable for application startup recovery."
                )
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
        except Exception as error:
            if journal is not None:
                raise
            return self._application_autostart_failed(execution, error)
        if not isinstance(specifications, tuple):
            raise RuntimeError("Pandrator startup did not produce its required managed services.")
        target: dict[str, ManagedProcessSpec] = {}
        for specification in specifications:
            if (
                not isinstance(specification, ManagedProcessSpec)
                or specification.component_id != "pandrator"
                or specification.service_id not in PANDRATOR_SERVICE_START_ORDER
                or specification.service_id in target
            ):
                raise RuntimeError("Pandrator startup did not produce its required managed services.")
            target[specification.service_id] = specification
        if not PANDRATOR_CORE_SERVICES.issubset(target):
            raise RuntimeError("Pandrator startup did not produce its required managed services.")
        with execution.supervisor.service_transition_guard():
            if journal is None:
                snapshots = {service.id: service for service in execution.supervisor.snapshot()}
                previous: dict[str, _ApplicationServiceState] = {}
                for service_id in PANDRATOR_SERVICE_START_ORDER:
                    snapshot = snapshots.get(service_id)
                    previous[service_id] = _ApplicationServiceState(
                        spec=execution.supervisor.spec(service_id),
                        was_running=bool(snapshot is not None and snapshot.process is not None),
                        desired_running=bool(snapshot is not None and snapshot.desired_running),
                    )
                previous_pointer = execution.prior_results.get("pandrator:activate", {}).get(
                    "previous_pointer"
                )
                previous_path = None
                if previous_pointer is not None:
                    if (
                        not isinstance(previous_pointer, dict)
                        or not isinstance(previous_pointer.get("path"), str)
                        or not previous_pointer["path"]
                    ):
                        raise RuntimeError(
                            "The application startup journal is invalid or does not match this operation task."
                        )
                    previous_path = Path(previous_pointer["path"])
                journal = save_application_start_journal(
                    execution, task, _ApplicationStartJournal(previous, target, previous_path)
                )
            elif target != journal.target:
                raise RuntimeError(
                    "The application launch specifications changed during startup recovery."
                )
            require_application_specs_match(execution.supervisor, journal)
            try:
                for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                    if execution.supervisor.spec(service_id) is not None:
                        execution.supervisor.stop(service_id)
                selected_ids = set(journal.target)
                for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                    if (
                        service_id not in selected_ids
                        and execution.supervisor.spec(service_id) is not None
                    ):
                        execution.supervisor.unregister(service_id)
                for service_id in PANDRATOR_SERVICE_START_ORDER:
                    if service_id in journal.target:
                        execution.supervisor.replace_spec(journal.target[service_id])
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
                return self._application_autostart_failed(execution, error)
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
        journal = load_application_start_journal(execution, task)
        if journal is None and not result.get("started"):
            return
        if execution.supervisor is None:
            raise RuntimeError(
                "The process supervisor is unavailable for application startup recovery."
            )
        with execution.supervisor.service_transition_guard():
            if journal is not None:
                require_application_specs_match(execution.supervisor, journal)
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if execution.supervisor.spec(service_id) is not None:
                    execution.supervisor.stop(service_id)
            if journal is None:
                return
            for service_id in PANDRATOR_SERVICE_STOP_ORDER:
                if execution.supervisor.spec(service_id) is not None:
                    execution.supervisor.unregister(service_id)
            for service_id in PANDRATOR_SERVICE_START_ORDER:
                previous_spec = journal.previous[service_id].spec
                if previous_spec is not None:
                    execution.supervisor.register(previous_spec)

    def _restore_application_after_rollback(self, execution: OperationTaskContext) -> None:
        records = {
            record.task.id: record
            for record in execution.store.operation_tasks(execution.operation.id)
        }
        for task in execution.plan.tasks:
            record = records.get(task.id)
            if (
                task.kind != "start_application"
                or record is None
                or record.state != TaskState.ROLLED_BACK
            ):
                continue
            journal = load_application_start_journal(execution, task)
            if journal is None:
                continue
            if execution.supervisor is None:
                raise RuntimeError(
                    "The process supervisor is unavailable for application startup recovery."
                )
            if active_component_path(execution.context.layout, "pandrator") != journal.previous_active_path:
                raise RuntimeError("The previous application slot has not been restored for rollback.")
            with execution.supervisor.service_transition_guard():
                for service_id in PANDRATOR_SERVICE_START_ORDER:
                    if execution.supervisor.spec(service_id) != journal.previous[service_id].spec:
                        raise RuntimeError(
                            "The previous application specifications have not been restored for rollback."
                        )
                for service_id in PANDRATOR_SERVICE_START_ORDER:
                    previous = journal.previous[service_id]
                    if previous.was_running or previous.desired_running:
                        service = execution.supervisor.start(service_id)
                        if service.health is None or service.health.state != HealthState.HEALTHY:
                            raise RuntimeError(
                                "The previous Pandrator service did not become healthy during rollback."
                            )

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
