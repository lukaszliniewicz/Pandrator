"""Single-writer durable task journal with cancellation and rollback."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from ..components import ComponentRegistry
from ..context import ManagerContext
from ..errors import CancellationRequested, ManagerError
from ..models import (
    TERMINAL_OPERATION_STATES,
    OperationRecord,
    OperationState,
    OperationTaskRecord,
    TaskState,
)
from ..releases.authority import ReleaseAuthority
from ..state import ManagerStore
from ..supervisor import ProcessSupervisor
from .handlers import FilesystemTaskHandler, OperationTaskContext


class _StoreCancellation:
    def __init__(self, store: ManagerStore, operation_id: str) -> None:
        self.store = store
        self.operation_id = operation_id

    @property
    def requested(self) -> bool:
        return self.store.cancellation_requested(self.operation_id)

    def raise_if_requested(self) -> None:
        if self.requested:
            raise CancellationRequested()


class OperationEngine:
    def __init__(
        self,
        context: ManagerContext,
        store: ManagerStore,
        registry: ComponentRegistry,
        *,
        supervisor: ProcessSupervisor | None = None,
        task_handler: FilesystemTaskHandler | None = None,
        service_spec_factory=None,
        release_authority: ReleaseAuthority | None = None,
        manager_handoff_callback: (
            Callable[[OperationTaskContext, dict], None] | None
        ) = None,
        fault_injector=None,
    ) -> None:
        self.context = context
        self.store = store
        self.registry = registry
        self.supervisor = supervisor
        self.task_handler = task_handler or FilesystemTaskHandler()
        self.service_spec_factory = service_spec_factory
        self.release_authority = release_authority
        self.manager_handoff_callback = manager_handoff_callback
        self.fault_injector = fault_injector
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._queued: set[str] = set()
        self._queued_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._recover_interrupted()
        self._thread = threading.Thread(
            target=self._worker,
            name="pandrator-manager-operations",
            daemon=True,
        )
        self._thread.start()
        for operation in self.store.list_operations(active_only=True, limit=500):
            if operation.state == OperationState.HANDOFF_PENDING:
                continue
            self.enqueue(operation.id)

    def shutdown(self, *, timeout: float = 5) -> None:
        self._stop_event.set()
        self._queue.put(None)
        if self._thread:
            self._thread.join(timeout=timeout)

    def enqueue(self, operation_id: str) -> None:
        with self._queued_lock:
            if operation_id in self._queued:
                return
            self._queued.add(operation_id)
        self._queue.put(operation_id)

    def _worker(self) -> None:
        while not self._stop_event.is_set():
            try:
                operation_id = self._queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if operation_id is None:
                return
            with self._queued_lock:
                self._queued.discard(operation_id)
            try:
                self._execute(operation_id)
            except Exception:
                logging.exception("Unhandled operation-engine error for %s", operation_id)

    def _recover_interrupted(self) -> None:
        for operation in self.store.list_operations(active_only=True, limit=500):
            if operation.state in {
                OperationState.AWAITING_CONFIRMATION,
                OperationState.HANDOFF_PENDING,
            }:
                continue
            for task in self.store.operation_tasks(operation.id):
                if task.state == TaskState.RUNNING:
                    self.store.update_operation_task(
                        operation.id,
                        task.task.id,
                        state=TaskState.PENDING,
                        attempt=task.attempt,
                        result=task.result,
                        error={
                            "code": "manager_interrupted",
                            "message": "Task will be inspected and resumed.",
                        },
                    )
            operation.state = OperationState.QUEUED
            operation.current_task_id = None
            operation.updated_at = self._now()
            self.store.update_operation(operation)
            self.context.event_sink.emit(
                "operation.recovered",
                {"operation_id": operation.id},
                operation_id=operation.id,
            )

    def _execute(self, operation_id: str) -> None:
        operation = self.store.get_operation(operation_id)
        if (
            operation.state in TERMINAL_OPERATION_STATES
            or operation.state == OperationState.HANDOFF_PENDING
        ):
            return
        plan = self.store.get_plan(operation.plan_id)
        cancellation = _StoreCancellation(self.store, operation.id)
        task_records = self.store.operation_tasks(operation.id)
        results = {
            task.task.id: task.result
            for task in task_records
            if task.state == TaskState.SUCCEEDED
        }
        execution = OperationTaskContext(
            context=self.context,
            store=self.store,
            registry=self.registry,
            supervisor=self.supervisor,
            operation=operation,
            plan=plan,
            prior_results=results,
            cancellation=cancellation,
            release_authority=self.release_authority,
            service_spec_factory=self.service_spec_factory,
        )
        operation.state = OperationState.RUNNING
        operation.updated_at = self._now()
        self.store.update_operation(operation)
        self._event(operation, "operation.running", {})
        try:
            for original_record in task_records:
                current_records = self.store.operation_tasks(operation.id)
                task_record = next(
                    record
                    for record in current_records
                    if record.task.id == original_record.task.id
                )
                if task_record.state == TaskState.SUCCEEDED:
                    continue
                cancellation.raise_if_requested()
                self._assert_dependencies(task_record, current_records)
                self._run_task(execution, task_record)
                completed_record = next(
                    record
                    for record in self.store.operation_tasks(operation.id)
                    if record.task.id == task_record.task.id
                )
                results[task_record.task.id] = completed_record.result
                execution.prior_results = results
                self._update_progress(operation, task_records)
                if (
                    completed_record.result.get("manager_handoff_pending")
                    or completed_record.result.get("external_handoff_pending")
                ):
                    if self.manager_handoff_callback is None:
                        raise RuntimeError(
                            "Manager handoff coordination is unavailable."
                        )
                    operation.state = OperationState.HANDOFF_PENDING
                    operation.current_task_id = task_record.task.id
                    operation.updated_at = self._now()
                    self.store.update_operation(operation)
                    self._event(
                        operation,
                        "operation.external_handoff_pending",
                        {
                            "task_id": task_record.task.id,
                            "handoff_kind": completed_record.result.get(
                                "handoff_kind",
                                "manager-update",
                            ),
                            "version": completed_record.result.get("version"),
                        },
                    )
                    self.manager_handoff_callback(
                        execution,
                        completed_record.result,
                    )
                    return
            cancellation.raise_if_requested()
            operation.state = OperationState.SUCCEEDED
            operation.progress = 1.0
            operation.current_task_id = None
            operation.finished_at = self._now()
            operation.updated_at = operation.finished_at
            self._commit_success(execution, operation)
        except CancellationRequested as error:
            self._rollback(execution, operation, error, cancelled=True)
        except Exception as error:
            payload = self._error_payload(error)
            logging.exception(
                "Operation %s failed in task %s [%s]",
                operation.id,
                operation.current_task_id or "unknown",
                payload["code"],
            )
            self._rollback(execution, operation, error, cancelled=False)
        else:
            cleanup_warnings: list[dict] = []
            try:
                self.task_handler.finalize(execution, succeeded=True)
            except Exception as cleanup_error:
                logging.exception(
                    "Post-commit cleanup failed for operation %s",
                    operation.id,
                )
                cleanup_warnings.append(self._error_payload(cleanup_error))
                operation.recovery = {"cleanup_warnings": cleanup_warnings}
                try:
                    self.store.update_operation(operation)
                except Exception:
                    logging.exception(
                        "Could not persist cleanup warning for operation %s",
                        operation.id,
                    )
                try:
                    self._event(
                        operation,
                        "operation.cleanup_warning",
                        {"warnings": cleanup_warnings},
                    )
                except Exception:
                    logging.exception(
                        "Could not persist cleanup event for operation %s",
                        operation.id,
                    )
            try:
                self._event(
                    operation,
                    "operation.succeeded",
                    {"cleanup_warnings": cleanup_warnings},
                )
            except Exception:
                # The terminal operation and component configuration are
                # already committed. An event-journal failure must never
                # trigger a filesystem rollback after that commit boundary.
                logging.exception(
                    "Could not persist success event for operation %s",
                    operation.id,
                )

    @staticmethod
    def _assert_dependencies(
        selected: OperationTaskRecord,
        records: list[OperationTaskRecord],
    ) -> None:
        states = {record.task.id: record.state for record in records}
        incomplete = [
            dependency
            for dependency in selected.task.dependencies
            if states.get(dependency) != TaskState.SUCCEEDED
        ]
        if incomplete:
            raise RuntimeError(
                f"Task {selected.task.id} has incomplete dependencies: "
                + ", ".join(incomplete)
            )

    def _run_task(
        self,
        execution: OperationTaskContext,
        record: OperationTaskRecord,
    ) -> None:
        now = self._now()
        attempt = record.attempt + 1
        operation = execution.operation
        operation.current_task_id = record.task.id
        operation.updated_at = now
        self.store.update_operation(operation)
        self.store.update_operation_task(
            operation.id,
            record.task.id,
            state=TaskState.RUNNING,
            attempt=attempt,
            started_at=now,
        )
        self._event(
            operation,
            "operation.task_started",
            {
                "task_id": record.task.id,
                "label": record.task.label,
                "attempt": attempt,
            },
            component_id=record.task.component_id,
        )
        result: dict = {}
        try:
            result = self.task_handler.execute(execution, record.task)
            if self.fault_injector is not None:
                self.fault_injector(operation, record.task, result)
        except Exception as error:
            finished = self._now()
            self.store.update_operation_task(
                operation.id,
                record.task.id,
                state=TaskState.FAILED,
                attempt=attempt,
                result=result,
                error=self._error_payload(error),
                started_at=now,
                finished_at=finished,
            )
            self._event(
                operation,
                "operation.task_failed",
                {
                    "task_id": record.task.id,
                    "error": self._error_payload(error),
                },
                component_id=record.task.component_id,
            )
            raise
        finished = self._now()
        self.store.update_operation_task(
            operation.id,
            record.task.id,
            state=TaskState.SUCCEEDED,
            attempt=attempt,
            result=result,
            started_at=now,
            finished_at=finished,
        )
        self._event(
            operation,
            "operation.task_succeeded",
            {"task_id": record.task.id},
            component_id=record.task.component_id,
        )

    def _update_progress(
        self,
        operation: OperationRecord,
        original_records: list[OperationTaskRecord],
    ) -> None:
        current = self.store.operation_tasks(operation.id)
        weights = {
            record.task.id: max(
                1,
                record.task.estimated_download_bytes
                + record.task.estimated_disk_bytes,
            )
            for record in original_records
        }
        total = sum(weights.values()) or 1
        completed = sum(
            weights[record.task.id]
            for record in current
            if record.state == TaskState.SUCCEEDED
        )
        operation.progress = min(1.0, completed / total)
        operation.updated_at = self._now()
        self.store.update_operation(operation)
        self._event(
            operation,
            "operation.progress",
            {"progress": operation.progress},
        )

    def _commit_success(
        self,
        execution: OperationTaskContext,
        operation: OperationRecord,
    ) -> None:
        inspections = {}
        for component_id, desired in execution.plan.desired.items():
            inspection = self.registry.driver(component_id).inspect(
                self.context,
                self.registry.definition(component_id),
                desired,
            )
            inspections[component_id] = inspection
        released_owned_paths = tuple(
            Path(str(moved["source"]))
            for result in execution.prior_results.values()
            for moved in result.get("moved") or ()
            if moved.get("source")
        )
        claimed_owned_paths = tuple(
            (
                Path(str(ownership["path"])),
                str(ownership["owner_kind"]),
                str(ownership["owner_id"]),
                dict(ownership.get("evidence") or {}),
            )
            for result in execution.prior_results.values()
            for ownership in (result.get("ownership"),)
            if isinstance(ownership, dict)
            and ownership.get("path")
            and ownership.get("owner_kind")
            and ownership.get("owner_id")
        )
        release_activations = [
            result["release_activation"]
            for result in execution.prior_results.values()
            if isinstance(result.get("release_activation"), dict)
        ]
        if len(release_activations) > 1:
            raise RuntimeError(
                "An operation cannot publish more than one product release."
            )
        self.store.commit_operation_success(
            operation,
            inspections=inspections,
            desired=execution.plan.desired,
            expected_revision=execution.plan.expected_revision,
            claimed_owned_paths=claimed_owned_paths,
            released_owned_paths=released_owned_paths,
            release_activation=(
                release_activations[0] if release_activations else None
            ),
        )

    def _rollback(
        self,
        execution: OperationTaskContext,
        operation: OperationRecord,
        cause: Exception,
        *,
        cancelled: bool,
    ) -> None:
        error_payload = self._error_payload(cause)
        operation.state = OperationState.ROLLING_BACK
        operation.error_code = (
            "cancelled" if cancelled else str(error_payload["code"])
        )
        operation.error_message = str(error_payload["message"])
        operation.updated_at = self._now()
        self.store.update_operation(operation)
        self._event(
            operation,
            "operation.rolling_back",
            {"error": error_payload},
        )
        rollback_errors: list[dict] = []
        for record in reversed(self.store.operation_tasks(operation.id)):
            if record.state not in {TaskState.SUCCEEDED, TaskState.FAILED}:
                continue
            try:
                self.task_handler.rollback(
                    execution,
                    record.task,
                    record.result,
                )
                self.store.update_operation_task(
                    operation.id,
                    record.task.id,
                    state=TaskState.ROLLED_BACK,
                    attempt=record.attempt,
                    result=record.result,
                    error=record.error,
                    started_at=record.started_at,
                    finished_at=record.finished_at or self._now(),
                )
            except Exception as rollback_error:
                logging.exception(
                    "Rollback failed for operation %s task %s",
                    operation.id,
                    record.task.id,
                )
                rollback_errors.append(
                    {
                        "task_id": record.task.id,
                        "error": self._error_payload(rollback_error),
                    }
                )
        try:
            self.task_handler.finalize(execution, succeeded=False)
        except Exception as cleanup_error:
            logging.exception(
                "Rollback finalization failed for operation %s",
                operation.id,
            )
            rollback_errors.append(
                {
                    "task_id": "finalize",
                    "error": self._error_payload(cleanup_error),
                }
            )
        operation.current_task_id = None
        operation.finished_at = self._now()
        operation.updated_at = operation.finished_at
        operation.recovery = {"rollback_errors": rollback_errors}
        if rollback_errors:
            operation.state = OperationState.RECOVERY_REQUIRED
        else:
            operation.state = (
                OperationState.CANCELLED
                if cancelled
                else OperationState.FAILED
            )
        self.store.update_operation(operation)
        self._event(
            operation,
            f"operation.{operation.state.value}",
            {
                "error": self._error_payload(cause),
                "rollback_errors": rollback_errors,
            },
        )

    def _event(
        self,
        operation: OperationRecord,
        event_type: str,
        payload: dict,
        *,
        component_id: str | None = None,
    ) -> None:
        self.context.event_sink.emit(
            event_type,
            {"operation_id": operation.id, **payload},
            operation_id=operation.id,
            component_id=component_id,
        )

    @staticmethod
    def _error_payload(error: Exception) -> dict:
        if isinstance(error, ManagerError):
            payload = {
                "code": error.code,
                "message": error.message[:2000],
            }
            if error.details:
                payload["details"] = dict(error.details)
            return payload
        return {
            "code": type(error).__name__.lower(),
            "message": str(error)[:2000],
        }

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.context.clock.time(), timezone.utc)
