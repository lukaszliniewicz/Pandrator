"""Native service lifecycle intent and receipt-loss recovery."""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
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
    HealthProbeSpec,
    HealthState,
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


class _ValidationInterruptedHandler(FilesystemTaskHandler):
    def _execute_validate_service(self, execution, task):
        super()._execute_validate_service(execution, task)
        raise _StopInterrupted()


class _ValidationFailedHandler(FilesystemTaskHandler):
    def _execute_validate_service(self, execution, task):
        super()._execute_validate_service(execution, task)
        raise RuntimeError("injected failure before validation receipt return")


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

    def _spec(self, application, component_id="fixture"):
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

    def _engine(self, application, supervisor, *, handler=None, fault=None):
        engine = OperationEngine(
            application.context,
            application.store,
            application.registry,
            supervisor=supervisor,
            service_spec_factory=lambda component_id, _resolved: self._spec(
                application, component_id
            ),
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

    @staticmethod
    def _validation_record(application, operation_id):
        return next(
            record
            for record in application.store.operation_tasks(operation_id)
            if record.task.kind == "validate_service"
        )

    @staticmethod
    def _reap(process) -> None:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)

    def _interrupt_validation(self, *, starting=False):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        engine = self._engine(
            self.application,
            self.supervisor,
            handler=None if starting else _ValidationInterruptedHandler(),
        )
        emit = self.application.context.event_sink.emit

        def interrupt_start(event_type, *args, **kwargs):
            emit(event_type, *args, **kwargs)
            if event_type == "service.starting":
                raise _StopInterrupted()

        with mock.patch.object(
            self.application.context.event_sink,
            "emit",
            side_effect=interrupt_start if starting else emit,
        ), self.assertRaises(_StopInterrupted):
            engine._execute(operation.id)
        process = self.supervisor._runtime["fixture.service"].process
        assert process is not None
        self.replacement_process = process
        self.addCleanup(self._reap, self.replacement_process)
        self.assertIsNone(self.replacement_process.poll())
        record = self._validation_record(self.application, operation.id)
        self.assertEqual((record.state, record.attempt, record.result), (TaskState.RUNNING, 1, {}))
        return operation

    def _reopen_validation(self):
        self.supervisor.shutdown(stop_children=False)
        application = create_application(self.base / "workspace", registry=self.registry)
        supervisor = self._supervisor(application)
        supervisor.register(self._spec(application))
        return application, supervisor

    def _assert_original_restored(self, application, supervisor):
        self.assertEqual(application.store.configuration_revision(), 1)
        self.assertEqual(active_component_path(application.context.layout, "fixture"), self.original_slot)
        self.assertEqual(supervisor.spec("fixture.service"), self.original_spec)
        self._assert_running(supervisor)
        self.assertIsNotNone(self.replacement_process.poll())

    def _resume_validation(self, *, starting=False):
        operation = self._interrupt_validation(starting=starting)
        stored = self.application.store.list_services()[0]
        self.assertIsNotNone(stored.process)
        assert stored.process is not None
        self.assertEqual(stored.process.pid, self.replacement_process.pid)
        if starting:
            assert stored.health is not None
            self.assertEqual(stored.health.state, HealthState.STARTING)
        application, supervisor = self._reopen_validation()
        adopted = supervisor.snapshot()[0].process
        self.assertIsNotNone(adopted)
        assert adopted is not None
        self.assertEqual(adopted.pid, self.replacement_process.pid)
        self.assertEqual(adopted.ownership_token, stored.process.ownership_token)
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.SUCCEEDED)
        self.assertEqual(application.store.configuration_revision(), 2)
        self._assert_running(supervisor)
        active = active_component_path(application.context.layout, "fixture")
        assert active is not None
        self.assertEqual((active / "marker.txt").read_text(), "two")
        self.assertEqual(supervisor.spec("fixture.service"), self._spec(application))
        self.assertIsNotNone(self.replacement_process.poll())
        record = self._validation_record(application, operation.id)
        self.assertEqual(record.attempt, 2)
        assert self.original_spec is not None
        self.assertEqual(record.result["previous_spec"], self.original_spec.model_dump(mode="json"))
        self.assertFalse((application.context.layout.staging / operation.id).exists())

    def test_validation_resume_restarts_adopted_child_and_keeps_original_spec_receipt(self):
        self._resume_validation()

    def test_validation_resume_adopts_child_before_readiness(self):
        self._resume_validation(starting=True)

    def _cancel_validation(self, *, starting=False):
        operation = self._interrupt_validation(starting=starting)
        new_slot = active_component_path(self.application.context.layout, "fixture")
        application, supervisor = self._reopen_validation()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.CANCELLED)
        self._assert_original_restored(application, supervisor)
        assert new_slot is not None
        self.assertFalse(new_slot.exists())
        self.assertFalse((application.context.layout.staging / operation.id).exists())
        self.assertEqual(self._validation_record(application, operation.id).attempt, 1)

    def test_validation_cancel_restores_previous_spec_and_service(self):
        self._cancel_validation()

    def test_validation_cancel_adopts_and_stops_child_before_readiness(self):
        self._cancel_validation(starting=True)

    def test_validation_receiptless_failure_restores_previous_spec_and_service(self):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        handles = []
        start = self.supervisor.start

        def observe_start(service_id):
            result = start(service_id)
            process = self.supervisor._runtime[service_id].process
            assert process is not None
            handles.append(process)
            return result

        with mock.patch.object(self.supervisor, "start", side_effect=observe_start):
            self._engine(
                self.application, self.supervisor, handler=_ValidationFailedHandler()
            )._execute(operation.id)
        self.assertEqual(self.application.store.get_operation(operation.id).state, OperationState.FAILED)
        self.assertEqual(len(handles), 2)  # Replacement launch, then original service restoration.
        self.replacement_process = handles[0]
        self._assert_original_restored(self.application, self.supervisor)
        self.assertEqual(self._validation_record(self.application, operation.id).result, {})
        self.assertFalse((self.application.context.layout.staging / operation.id).exists())

    def test_validation_foreign_journal_keeps_live_child_slot_and_evidence(self):
        operation = self._interrupt_validation()
        directory = self.application.context.layout.staging / operation.id / "service-validation"
        journals = list(directory.glob("*.json"))
        self.assertEqual(len(journals), 1)
        journal = journals[0]
        payload = json.loads(journal.read_text())
        payload["task_id"] = "foreign:validation"
        journal.write_text(json.dumps(payload))
        evidence = journal.read_bytes()
        application, supervisor = self._reopen_validation()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED)
        self.assertIsNone(self.replacement_process.poll())
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertEqual(application.store.configuration_revision(), 1)
        self.assertEqual(supervisor.spec("fixture.service"), self._spec(application))
        self.assertNotEqual(active_component_path(application.context.layout, "fixture"), self.original_slot)

    def test_validation_missing_supervisor_preserves_recorded_child_slot(self):
        operation = self._interrupt_validation(starting=True)
        slot = active_component_path(self.application.context.layout, "fixture")
        application, supervisor = self._reopen_validation()
        engine = self._engine(application, None)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED)
        self.assertEqual(active_component_path(application.context.layout, "fixture"), slot)
        assert slot is not None
        self.assertTrue(slot.exists())
        self.assertIsNone(self.replacement_process.poll())
        self.assertIsNotNone(supervisor.snapshot()[0].process)
        self.assertTrue((application.context.layout.staging / operation.id).exists())

    def test_validation_journal_write_failure_restores_original_service(self):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        write = service_tasks._atomic_json

        def fail_validation_write(path, payload):
            if path.parent.name == "service-validation":
                raise OSError("injected validation journal publication failure")
            write(path, payload)

        with mock.patch.object(service_tasks, "_atomic_json", side_effect=fail_validation_write):
            self._engine(self.application, self.supervisor)._execute(operation.id)
        self.assertEqual(self.application.store.get_operation(operation.id).state, OperationState.FAILED)
        self.assertEqual(active_component_path(self.application.context.layout, "fixture"), self.original_slot)
        self.assertEqual(self.supervisor.spec("fixture.service"), self.original_spec)
        self._assert_running(self.supervisor)
        self.assertFalse((self.application.context.layout.staging / operation.id).exists())

    def test_validation_recovery_foreign_registered_spec_does_not_stop_child(self):
        operation = self._interrupt_validation()
        application, supervisor = self._reopen_validation()
        # A different registered contract must not authorize stopping this child.
        runtime = supervisor._runtime["fixture.service"]
        foreign = runtime.spec.model_copy(update={"label": "Foreign contract"})
        supervisor._specs["fixture.service"] = foreign
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        with mock.patch.object(supervisor, "stop", wraps=supervisor.stop) as stop:
            engine._execute(operation.id)
        stop.assert_not_called()
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED)
        self.assertIsNone(self.replacement_process.poll())
        self.assertEqual(supervisor.spec("fixture.service"), foreign)
        self.assertNotEqual(active_component_path(application.context.layout, "fixture"), self.original_slot)

    def test_replace_callback_is_not_called_for_running_service(self):
        self._install()
        callback = mock.Mock()
        assert self.original_spec is not None
        replacement = self.original_spec.model_copy(update={"label": "New label"})
        with self.assertRaisesRegex(RuntimeError, "Cannot replace running"):
            self.supervisor.replace_spec(replacement, before_replace=callback)
        callback.assert_not_called()
        self.assertEqual(self.supervisor.spec("fixture.service"), self.original_spec)
        assert self.original_process is not None
        self.assertIsNone(self.original_process.poll())

    def test_replace_callback_failure_leaves_spec_unchanged(self):
        self._install()
        self.supervisor.stop("fixture.service")
        assert self.original_spec is not None
        replacement = self.original_spec.model_copy(update={"label": "New label"})

        def fail(previous):
            self.assertEqual(previous, self.original_spec)
            previous.label = "Mutated callback copy"
            raise OSError("injected checkpoint failure")

        with self.assertRaisesRegex(OSError, "injected checkpoint failure"):
            self.supervisor.replace_spec(replacement, before_replace=fail)
        self.assertEqual(self.supervisor.spec("fixture.service"), self.original_spec)
        self.assertIsNone(self.supervisor.snapshot()[0].process)

    def test_starting_identity_save_failure_terminates_new_child(self):
        self._install()
        self.supervisor.stop("fixture.service")
        processes = []
        popen = subprocess.Popen

        def capture(*args, **kwargs):
            process = popen(*args, **kwargs)
            processes.append(process)
            return process

        with mock.patch(
            "pandrator_manager.supervisor.runtime.subprocess.Popen", side_effect=capture
        ), mock.patch.object(
            self.application.store, "save_service", side_effect=OSError("injected identity save failure")
        ), self.assertRaisesRegex(OSError, "injected identity save failure"):
            self.supervisor.start("fixture.service")
        self.assertEqual(len(processes), 1)
        self.addCleanup(self._reap, processes[0])
        self.assertIsNotNone(processes[0].poll())
        self.assertNotIn("fixture.service", self.supervisor._runtime)
        stored = self.application.store.list_services()[0]
        self.assertIsNone(stored.process)
        self.assertFalse(stored.desired_running)

    def test_readiness_failure_clears_identity_and_preserves_previous_intent(self):
        self._install()
        self.supervisor.stop("fixture.service")
        assert self.original_spec is not None
        for desired in (False, True):
            with self.subTest(desired=desired), socket.socket() as reserved:
                reserved.bind(("127.0.0.1", 0))
                stored = self.application.store.list_services()[0]
                stored.desired_running = desired
                self.application.store.save_service(stored)
                spec = self.original_spec.model_copy(
                    update={
                        "readiness": HealthProbeSpec(kind="tcp", port=reserved.getsockname()[1]),
                        "startup_timeout_seconds": 0.3,
                    }
                )
                self.supervisor.replace_spec(spec)
                with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                    self.supervisor.start("fixture.service")
                self.assertNotIn("fixture.service", self.supervisor._runtime)
                failed = self.application.store.list_services()[0]
                self.assertIsNone(failed.process)
                self.assertEqual(failed.desired_running, desired)
                assert failed.health is not None
                self.assertEqual(failed.health.state, HealthState.FAILED)

    def test_cancel_after_validation_journal_before_spec_mutation_restores_original(self):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        replace = self.supervisor.replace_spec

        def interrupt_after_checkpoint(spec, *, before_replace=None):
            def checkpoint(previous):
                assert before_replace is not None
                before_replace(previous)
                raise _StopInterrupted()

            return replace(spec, before_replace=checkpoint)

        engine = self._engine(self.application, self.supervisor)
        with mock.patch.object(self.supervisor, "replace_spec", side_effect=interrupt_after_checkpoint):
            with self.assertRaises(_StopInterrupted):
                engine._execute(operation.id)
        self.assertEqual(self.supervisor.spec("fixture.service"), self.original_spec)
        self.assertIsNone(self.supervisor.snapshot()[0].process)
        new_slot = active_component_path(self.application.context.layout, "fixture")
        application, supervisor = self._reopen_validation()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.CANCELLED)
        self.assertEqual(supervisor.spec("fixture.service"), self.original_spec)
        self.assertEqual(active_component_path(application.context.layout, "fixture"), self.original_slot)
        self._assert_running(supervisor)
        assert new_slot is not None
        self.assertFalse(new_slot.exists())
