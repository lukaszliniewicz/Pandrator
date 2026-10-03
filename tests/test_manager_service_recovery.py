"""Native interrupted service-stop intent and cancellation recovery."""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

from dulwich import porcelain

from pandrator_manager.application import create_application
from pandrator_manager.components import ComponentRegistry
from pandrator_manager.components.builtin import MarkerComponentDriver
from pandrator_manager.components.slots import active_component_path
from pandrator_manager.context import CancellationToken
from pandrator_manager.models import (
    ComponentDefinition,
    DesiredComponentState,
    ManagedProcessSpec,
    OperationKind,
    OperationState,
    TaskState,
)
from pandrator_manager.operations import OperationEngine, service_tasks
from pandrator_manager.operations.contracts import OperationTaskContext
from pandrator_manager.operations.handlers import FilesystemTaskHandler
from pandrator_manager.supervisor import ProcessSupervisor


class _StopInterrupted(BaseException):
    pass


class _InterruptedHandler(FilesystemTaskHandler):
    def _execute_stop_service(self, execution, task):
        super()._execute_stop_service(execution, task)
        raise _StopInterrupted()


class ServiceRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repository = self.base / "origin"
        porcelain.init(str(self.repository))
        self._commit("one")
        self.registry = ComponentRegistry(
            (
                ComponentDefinition(
                    id="fixture",
                    label="Fixture service",
                    driver="marker",
                    repo_url=str(self.repository),
                    source_markers=("marker.txt",),
                    owned_paths=("services/fixture",),
                    resource_locks=("component:fixture",),
                    service_key="fixture.service",
                ),
            ),
            (MarkerComponentDriver(),),
        )
        self.application = create_application(self.base / "workspace", registry=self.registry)
        self.supervisor = self._supervisor(self.application)

    def _commit(self, contents: str) -> None:
        (self.repository / "marker.txt").write_text(contents, encoding="utf-8")
        porcelain.add(str(self.repository), paths=["marker.txt"])
        porcelain.commit(
            str(self.repository),
            message=contents.encode(),
            author=b"Fixture <fixture@example.invalid>",
            committer=b"Fixture <fixture@example.invalid>",
        )

    def _supervisor(self, application):
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id=str(uuid.uuid4()),
        )
        self.addCleanup(self._stop_children, supervisor)
        return supervisor

    @staticmethod
    def _stop_children(supervisor) -> None:
        # Keep handles even if native shutdown removes the runtime entries.
        runtimes = list(supervisor._runtime.values())
        try:
            supervisor.shutdown(stop_children=True)
        finally:
            for runtime in runtimes:
                process = runtime.process
                if process is not None:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                if runtime.log_handle is not None:
                    runtime.log_handle.close()
                    runtime.log_handle = None

    def _engine(self, application, supervisor, *, handler=None, fault=None):
        def service_spec_factory(component_id, _resolved):
            active = active_component_path(application.context.layout, component_id)
            self.assertIsNotNone(active)
            return ManagedProcessSpec(
                service_id="fixture.service",
                component_id=component_id,
                label="Fixture service",
                executable=sys.executable,
                arguments=("-c", "import time; time.sleep(60)"),
                cwd=str(active),
                startup_timeout_seconds=3,
                shutdown_timeout_seconds=1,
            )

        engine = OperationEngine(
            application.context,
            application.store,
            application.registry,
            supervisor=supervisor,
            service_spec_factory=service_spec_factory,
            task_handler=handler,
            fault_injector=fault,
            lifecycle_lock=application.lifecycle_lock,
        )
        self.addCleanup(engine.shutdown)
        return engine

    def _submit(self, kind, desired=None):
        plan = self.application.plan(
            kind=kind,
            desired={"fixture": desired if desired is not None else DesiredComponentState()},
        )
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        return operation

    def _install(self) -> None:
        installed = self._submit(
            OperationKind.INSTALL,
            DesiredComponentState(options={"start_after_install": True}),
        )
        self._engine(self.application, self.supervisor)._execute(installed.id)
        self.assertEqual(
            self.application.store.get_operation(installed.id).state,
            OperationState.SUCCEEDED,
        )
        self.original_spec = self.supervisor.spec("fixture.service")
        self.original_slot = active_component_path(self.application.context.layout, "fixture")
        self.original_process = self.supervisor._runtime["fixture.service"].process
        self.assertIsNotNone(self.original_process)
        assert self.original_process is not None
        self.assertIsNone(self.original_process.poll())
        self.assertTrue(self.supervisor.snapshot()[0].desired_running)
        self.assertEqual(self.application.store.configuration_revision(), 1)

    def _interrupt(self):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        interrupted = self._engine(self.application, self.supervisor, handler=_InterruptedHandler())
        with self.assertRaises(_StopInterrupted):
            interrupted._execute(operation.id)
        stop = self._stop_record(self.application, operation.id)
        self.assertEqual(stop.state, TaskState.RUNNING)
        self.assertEqual(stop.attempt, 1)
        self.assertEqual(stop.result, {})
        assert self.original_process is not None
        self.assertIsNotNone(self.original_process.poll())
        self.assertFalse(self.supervisor.snapshot()[0].desired_running)
        self.assertIsNone(self.supervisor.snapshot()[0].process)
        return operation

    @staticmethod
    def _stop_record(application, operation_id):
        return next(
            record
            for record in application.store.operation_tasks(operation_id)
            if record.task.kind == "stop_service"
        )

    def _reopen(self):
        self.supervisor.shutdown(stop_children=False)
        application = create_application(self.base / "workspace", registry=self.registry)
        supervisor = self._supervisor(application)
        assert self.original_spec is not None
        supervisor.register(self.original_spec)
        return application, supervisor

    def _journal(self, operation):
        directory = self.application.context.layout.staging / operation.id / "service-stops"
        journals = list(directory.glob("*.json"))
        self.assertEqual(len(journals), 1)
        return journals[0]

    def _assert_running(self, supervisor) -> None:
        service = supervisor.snapshot()[0]
        self.assertTrue(service.desired_running)
        self.assertIsNotNone(service.process)
        process = supervisor._runtime["fixture.service"].process
        self.assertIsNotNone(process)
        assert process is not None
        self.assertIsNone(process.poll())

    def _resume(self, *, rollback: bool) -> None:
        operation = self._interrupt()
        application, supervisor = self._reopen()

        def fail_after_activation(_operation, task, _result):
            if task.kind == "activate_component":
                raise RuntimeError("injected activation failure after interrupted stop")

        engine = self._engine(
            application, supervisor, fault=fail_after_activation if rollback else None
        )
        engine._recover_interrupted()
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state,
            OperationState.FAILED if rollback else OperationState.SUCCEEDED,
        )
        stopped = self._stop_record(application, operation.id)
        self.assertEqual(stopped.attempt, 2)
        self.assertTrue(stopped.result["was_running"])
        self.assertTrue(stopped.result["desired_running"])
        self._assert_running(supervisor)
        active = active_component_path(application.context.layout, "fixture")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual((active / "marker.txt").read_text(), "one" if rollback else "two")
        self.assertEqual(application.store.configuration_revision(), 1 if rollback else 2)
        if rollback:
            self.assertEqual(active, self.original_slot)
            self.assertEqual(supervisor.spec("fixture.service"), self.original_spec)
        self.assertFalse((application.context.layout.staging / operation.id).exists())

    def test_resume_keeps_original_running_intent(self) -> None:
        self._resume(rollback=False)

    def test_resumed_failure_restores_original_running_intent(self) -> None:
        self._resume(rollback=True)

    def test_cancel_after_recovery_uses_empty_receipt_journal(self) -> None:
        operation = self._interrupt()
        journal = self._journal(operation)
        original = journal.read_bytes()
        # Native archive staging replaces this component directory. The
        # service journal must survive that scope's cleanup.
        component_staging = self.application.context.layout.staging / operation.id / "fixture"
        component_staging.mkdir()
        (component_staging / "partial-output").write_text("fixture")
        shutil.rmtree(component_staging)
        self.assertEqual(journal.read_bytes(), original)
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        self.assertTrue(application.store.request_cancellation(operation.id))
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.CANCELLED
        )
        stopped = self._stop_record(application, operation.id)
        self.assertEqual(stopped.state, TaskState.ROLLED_BACK)
        self.assertEqual(stopped.attempt, 1)
        self.assertEqual(stopped.result, {})
        for record in application.store.operation_tasks(operation.id):
            if record.attempt == 0:
                self.assertEqual(record.state, TaskState.PENDING)
        self._assert_running(supervisor)
        self.assertEqual(supervisor.spec("fixture.service"), self.original_spec)
        self.assertEqual(
            active_component_path(application.context.layout, "fixture"), self.original_slot
        )
        self.assertEqual(application.store.configuration_revision(), 1)
        self.assertFalse(journal.exists())

    def test_journal_write_failure_does_not_stop_original_child(self) -> None:
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        with mock.patch.object(
            service_tasks, "_atomic_json", side_effect=OSError("fixture disk failure")
        ):
            self._engine(self.application, self.supervisor)._execute(operation.id)
        self.assertEqual(
            self.application.store.get_operation(operation.id).state, OperationState.FAILED
        )
        self._assert_running(self.supervisor)
        self.assertIs(self.supervisor._runtime["fixture.service"].process, self.original_process)
        self.assertEqual(self.application.store.configuration_revision(), 1)

    def _assert_invalid_journal(self, replacement) -> None:
        operation = self._interrupt()
        journal = self._journal(operation)
        payload = json.loads(journal.read_text())
        journal.write_text(replacement(payload), encoding="utf-8")
        evidence = journal.read_bytes()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        with (
            mock.patch.object(supervisor, "start", wraps=supervisor.start) as start,
            mock.patch.object(supervisor, "stop", wraps=supervisor.stop) as stop,
        ):
            engine._execute(operation.id)
        start.assert_not_called()
        stop.assert_not_called()
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED
        )
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertEqual(application.store.configuration_revision(), 1)
        self.assertEqual(
            active_component_path(application.context.layout, "fixture"), self.original_slot
        )

    def test_corrupt_journal_preserves_recovery_evidence(self) -> None:
        self._assert_invalid_journal(lambda _payload: "{broken")

    def test_foreign_operation_journal_is_not_replayed(self) -> None:
        def foreign(payload):
            payload["operation_id"] = str(uuid.uuid4())
            return json.dumps(payload)

        self._assert_invalid_journal(foreign)

    def test_string_running_flag_is_not_treated_as_truthy_intent(self) -> None:
        def invalid_flag(payload):
            payload["was_running"] = "false"
            return json.dumps(payload)

        self._assert_invalid_journal(invalid_flag)

    def test_unavailable_supervisor_keeps_recovery_journal(self) -> None:
        operation = self._interrupt()
        journal = self._journal(operation)
        evidence = journal.read_bytes()
        application, _supervisor = self._reopen()
        engine = self._engine(application, None)
        engine._recover_interrupted()
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED
        )
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertEqual(application.store.configuration_revision(), 1)

    def test_legacy_empty_receipt_without_journal_keeps_noop_behavior(self) -> None:
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        record = self._stop_record(self.application, operation.id)
        execution = OperationTaskContext(
            context=self.application.context,
            store=self.application.store,
            registry=self.registry,
            supervisor=self.supervisor,
            operation=self.application.store.get_operation(operation.id),
            plan=self.application.store.get_plan(operation.plan_id),
            prior_results={},
            cancellation=CancellationToken(),
        )
        with mock.patch.object(self.supervisor, "start", wraps=self.supervisor.start) as start:
            FilesystemTaskHandler()._rollback_stop_service(execution, record.task, {})
        start.assert_not_called()
        self._assert_running(self.supervisor)
