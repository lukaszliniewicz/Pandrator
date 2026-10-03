"""Native multi-service application startup and rollback recovery."""

from __future__ import annotations

import json
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
    HealthState,
    ManagedProcessSpec,
    OperationKind,
    OperationState,
    TaskState,
)
from pandrator_manager.operations import OperationEngine
from pandrator_manager.operations.contracts import OperationTaskContext
from pandrator_manager.operations.handlers import FilesystemTaskHandler
from pandrator_manager.runtime_specs import (
    PANDRATOR_API_SERVICE,
    PANDRATOR_MCP_SERVICE,
    PANDRATOR_SERVICE_START_ORDER,
    PANDRATOR_WORKER_SERVICE,
)
from pandrator_manager.supervisor import ProcessSupervisor


class _StartupCut(BaseException):
    pass


class _InterruptedHandler(FilesystemTaskHandler):
    def _execute_start_application(self, execution, task):
        result = super()._execute_start_application(execution, task)
        if not result.get("started"):
            raise AssertionError(f"Native fixture startup failed: {result}")
        raise _StartupCut()


class ApplicationRecoveryTests(unittest.TestCase):
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
                    id="pandrator",
                    label="Fixture application",
                    driver="marker",
                    repo_url=str(self.repository),
                    source_markers=("marker.txt",),
                    owned_paths=("app",),
                    resource_locks=("component:pandrator",),
                ),
            ),
            (MarkerComponentDriver(),),
        )
        self.include_mcp = False
        self.supervisors: list[ProcessSupervisor] = []
        self.processes: list[subprocess.Popen] = []
        self.addCleanup(self._cleanup)
        self.application = create_application(self.base / "workspace", registry=self.registry)
        self.supervisor = self._supervisor(self.application)

    def _cleanup(self) -> None:
        for supervisor in reversed(self.supervisors):
            self._capture(supervisor)
            supervisor.shutdown(stop_children=True)
        for process in set(self.processes):
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)

    def _capture(self, supervisor) -> tuple[subprocess.Popen, ...]:
        selected = tuple(
            runtime.process
            for runtime in supervisor._runtime.values()
            if runtime.process is not None
        )
        self.processes.extend(selected)
        return selected

    def _commit(self, marker):
        (self.repository / "marker.txt").write_text(marker)
        porcelain.add(str(self.repository), paths=["marker.txt"])
        porcelain.commit(
            str(self.repository),
            message=marker.encode(),
            author=b"Fixture <fixture@example.invalid>",
            committer=b"Fixture <fixture@example.invalid>",
        )

    def _supervisor(self, application):
        supervisor = ProcessSupervisor(
            application.context, application.store, manager_instance_id=str(uuid.uuid4())
        )
        self.supervisors.append(supervisor)
        return supervisor

    def _specs(self, application):
        active = active_component_path(application.context.layout, "pandrator")
        assert active is not None

        def spec(service_id, dependencies=()):
            return ManagedProcessSpec(
                service_id=service_id,
                component_id="pandrator",
                label=service_id,
                executable=sys.executable,
                arguments=("-c", "import time; time.sleep(60)"),
                cwd=str(active),
                dependencies=dependencies,
                startup_timeout_seconds=3,
                shutdown_timeout_seconds=1,
                required=service_id != PANDRATOR_MCP_SERVICE,
            )

        api = spec(PANDRATOR_API_SERVICE)
        worker = spec(PANDRATOR_WORKER_SERVICE, (PANDRATOR_API_SERVICE,))
        if self.include_mcp:
            # The real factory also returns optional MCP before worker.
            return api, spec(PANDRATOR_MCP_SERVICE, (PANDRATOR_WORKER_SERVICE,)), worker
        return api, worker

    def _engine(self, application, supervisor, *, handler_class=FilesystemTaskHandler, fault=None):
        handler = handler_class()
        factory = mock.patch.object(
            handler,
            "_application_runtime_specs",
            side_effect=lambda _execution, _preferences: self._specs(application),
        )
        factory.start()
        self.addCleanup(factory.stop)
        engine = OperationEngine(
            application.context,
            application.store,
            self.registry,
            supervisor=supervisor,
            task_handler=handler,
            fault_injector=fault,
            lifecycle_lock=application.lifecycle_lock,
        )
        self.addCleanup(engine.shutdown)
        return engine

    def _submit(self, kind):
        plan = self.application.plan(
            kind=kind,
            desired={"pandrator": DesiredComponentState(options={"start_after_install": True})},
        )
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        return operation

    @staticmethod
    def _start_record(application, operation_id):
        return next(
            record
            for record in application.store.operation_tasks(operation_id)
            if record.task.kind == "start_application"
        )

    def _install(self):
        operation = self._submit(OperationKind.INSTALL)
        self._engine(self.application, self.supervisor)._execute(operation.id)
        self.assertEqual(
            self.application.store.get_operation(operation.id).state, OperationState.SUCCEEDED
        )
        self.assertTrue(self._start_record(self.application, operation.id).result["started"])
        self.original_slot = active_component_path(self.application.context.layout, "pandrator")
        self.original_specs = {
            sid: self.supervisor.spec(sid) for sid in PANDRATOR_SERVICE_START_ORDER
        }
        self.original_processes = self._capture(self.supervisor)
        self.assertEqual(len(self.original_processes), 3 if self.include_mcp else 2)
        self.assertTrue(all(process.poll() is None for process in self.original_processes))
        self.assertEqual(self.application.store.configuration_revision(), 1)

    def _interrupt_update(self, *, mid_stop=False):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        engine = self._engine(
            self.application,
            self.supervisor,
            handler_class=FilesystemTaskHandler if mid_stop else _InterruptedHandler,
        )
        stop = self.supervisor.stop

        def cut_after_worker(service_id):
            result = stop(service_id)
            if service_id == PANDRATOR_WORKER_SERVICE:
                raise _StartupCut()
            return result

        with (
            mock.patch.object(
                self.supervisor, "stop", side_effect=cut_after_worker if mid_stop else stop
            ),
            self.assertRaises(_StartupCut),
        ):
            engine._execute(operation.id)
        self.replacements = self._capture(self.supervisor)
        self.new_slot = active_component_path(self.application.context.layout, "pandrator")
        record = self._start_record(self.application, operation.id)
        self.assertEqual((record.state, record.attempt, record.result), (TaskState.RUNNING, 1, {}))
        return operation

    def _reopen(self):
        self.supervisor.shutdown(stop_children=False)
        application = create_application(self.base / "workspace", registry=self.registry)
        supervisor = self._supervisor(application)
        supervisor.register_many(self._specs(application))
        return application, supervisor

    def _assert_original(self, application, supervisor):
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.original_slot
        )
        self.assertEqual(application.store.configuration_revision(), 1)
        for sid, original in self.original_specs.items():
            self.assertEqual(supervisor.spec(sid), original)
            if original is not None:
                runtime = supervisor._runtime[sid]
                self.assertEqual(runtime.spec.cwd, str(self.original_slot))
                snapshot = next(item for item in supervisor.snapshot() if item.id == sid)
                self.assertTrue(snapshot.desired_running)
                self.assertIsNotNone(snapshot.process)
                assert snapshot.health is not None
                self.assertEqual(snapshot.health.state, HealthState.HEALTHY)
        self.assertTrue(all(process.poll() is not None for process in self.replacements))
        assert self.new_slot is not None
        self.assertFalse(self.new_slot.exists())
        self._capture(supervisor)

    def _cancel(self, *, mid_stop=False):
        operation = self._interrupt_update(mid_stop=mid_stop)
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.CANCELLED
        )
        self._assert_original(application, supervisor)
        self.assertFalse((application.context.layout.staging / operation.id).exists())

    def test_cancel_after_receipt_loss_restores_original_slot_specs_and_children(self):
        self._cancel()

    def test_cancel_mid_service_stop_restores_entire_previous_service_set(self):
        self._cancel(mid_stop=True)

    def test_initial_install_orders_mcp_after_its_worker_dependency(self):
        self.include_mcp = True
        self._install()
        self.assertEqual(set(self.supervisor._runtime), set(PANDRATOR_SERVICE_START_ORDER))

    def test_optional_mcp_failure_keeps_core_services_running(self):
        self.include_mcp = True
        operation = self._submit(OperationKind.INSTALL)
        start = self.supervisor.start

        def fail_mcp(service_id):
            if service_id == PANDRATOR_MCP_SERVICE:
                raise RuntimeError("injected optional MCP launch failure")
            return start(service_id)

        with mock.patch.object(self.supervisor, "start", side_effect=fail_mcp):
            self._engine(self.application, self.supervisor)._execute(operation.id)
        self.assertEqual(
            self.application.store.get_operation(operation.id).state, OperationState.SUCCEEDED
        )
        receipt = self._start_record(self.application, operation.id).result
        self.assertTrue(receipt["started"])
        self.assertEqual(receipt["mcp_error"], "injected optional MCP launch failure")
        self.assertEqual(
            set(self.supervisor._runtime), {PANDRATOR_API_SERVICE, PANDRATOR_WORKER_SERVICE}
        )
        self.assertTrue(all(process.poll() is None for process in self._capture(self.supervisor)))
        self.assertFalse((self.application.context.layout.staging / operation.id).exists())

    def test_ordinary_core_launch_failure_remains_reported_without_failing_install(self):
        operation = self._submit(OperationKind.INSTALL)
        start = self.supervisor.start

        def fail_after_core_start(service_id):
            start(service_id)
            self._capture(self.supervisor)
            raise RuntimeError("injected core launch failure")

        with mock.patch.object(self.supervisor, "start", side_effect=fail_after_core_start):
            self._engine(self.application, self.supervisor)._execute(operation.id)
        self.assertEqual(
            self.application.store.get_operation(operation.id).state, OperationState.SUCCEEDED
        )
        receipt = self._start_record(self.application, operation.id).result
        self.assertFalse(receipt["started"])
        self.assertEqual(receipt["error"], "injected core launch failure")
        self.assertEqual(self.application.store.configuration_revision(), 1)
        self.assertTrue(all(process.poll() is not None for process in self.processes))
        self.assertTrue(all(item.process is None for item in self.supervisor.snapshot()))
        self.assertFalse((self.application.context.layout.staging / operation.id).exists())

    def test_retry_replaces_adopted_children_and_commits_new_slot(self):
        operation = self._interrupt_update()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.SUCCEEDED
        )
        self.assertEqual(application.store.configuration_revision(), 2)
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.new_slot
        )
        self.assertTrue(all(process.poll() is not None for process in self.replacements))
        self.assertEqual(len(self._capture(supervisor)), 2)
        self.assertEqual(self._start_record(application, operation.id).attempt, 2)
        self.assertFalse((application.context.layout.staging / operation.id).exists())

    def test_failure_after_retried_start_keeps_original_checkpoint(self):
        operation = self._interrupt_update()
        application, supervisor = self._reopen()

        def fail_after_start(_operation, task, _result):
            if task.kind == "start_application":
                raise RuntimeError("injected post-retry startup failure")

        engine = self._engine(application, supervisor, fault=fail_after_start)
        engine._recover_interrupted()
        engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.FAILED)
        self._assert_original(application, supervisor)
        self.assertEqual(self._start_record(application, operation.id).attempt, 2)

    def test_cancel_first_install_stops_children_and_removes_slot(self):
        operation = self._submit(OperationKind.INSTALL)
        with self.assertRaises(_StartupCut):
            self._engine(
                self.application, self.supervisor, handler_class=_InterruptedHandler
            )._execute(operation.id)
        replacements = self._capture(self.supervisor)
        slot = active_component_path(self.application.context.layout, "pandrator")
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.CANCELLED
        )
        self.assertEqual(application.store.configuration_revision(), 0)
        self.assertIsNone(active_component_path(application.context.layout, "pandrator"))
        assert slot is not None
        self.assertFalse(slot.exists())
        self.assertFalse(supervisor._runtime)
        self.assertTrue(all(supervisor.spec(sid) is None for sid in PANDRATOR_SERVICE_START_ORDER))
        self.assertTrue(all(process.poll() is not None for process in replacements))

    def _journal(self, operation):
        directory = self.application.context.layout.staging / operation.id / "application-starts"
        journals = list(directory.glob("*.json"))
        self.assertEqual(len(journals), 1)
        return journals[0]

    def test_foreign_checkpoint_preserves_live_children_slot_and_journal(self):
        operation = self._interrupt_update()
        journal = self._journal(operation)
        payload = json.loads(journal.read_text())
        payload["operation_id"] = str(uuid.uuid4())
        journal.write_text(json.dumps(payload))
        evidence = journal.read_bytes()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        with mock.patch.object(supervisor, "stop", wraps=supervisor.stop) as stop:
            engine._execute(operation.id)
        stop.assert_not_called()
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED
        )
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.new_slot
        )
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertTrue(all(process.poll() is None for process in self.replacements))

    def test_missing_supervisor_keeps_live_children_slot_and_checkpoint(self):
        operation = self._interrupt_update()
        journal = self._journal(operation)
        evidence = journal.read_bytes()
        application, _supervisor = self._reopen()
        engine = self._engine(application, None)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED
        )
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.new_slot
        )
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertTrue(all(process.poll() is None for process in self.replacements))

    def test_foreign_registered_contract_is_not_stopped_or_replaced(self):
        operation = self._interrupt_update()
        application, supervisor = self._reopen()
        api = supervisor.spec(PANDRATOR_API_SERVICE)
        assert api is not None
        foreign = api.model_copy(update={"label": "Foreign contract"})
        supervisor._specs[PANDRATOR_API_SERVICE] = foreign
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        with mock.patch.object(supervisor, "stop", wraps=supervisor.stop) as stop:
            engine._execute(operation.id)
        stop.assert_not_called()
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED
        )
        self.assertEqual(supervisor.spec(PANDRATOR_API_SERVICE), foreign)
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.new_slot
        )
        self.assertTrue(all(process.poll() is None for process in self.replacements))

    def test_checkpoint_write_failure_does_not_stop_previous_children(self):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        with (
            mock.patch(
                "pandrator_manager.operations.application_recovery._atomic_json",
                side_effect=OSError("injected application checkpoint failure"),
            ),
            mock.patch.object(self.supervisor, "stop", wraps=self.supervisor.stop) as stop,
        ):
            self._engine(self.application, self.supervisor)._execute(operation.id)
        stop.assert_not_called()
        self.assertEqual(
            self.application.store.get_operation(operation.id).state,
            OperationState.RECOVERY_REQUIRED,
        )
        self.assertTrue(all(process.poll() is None for process in self.original_processes))
        self.assertEqual(self.application.store.configuration_revision(), 1)
        slot = active_component_path(self.application.context.layout, "pandrator")
        assert slot is not None
        self.assertEqual((slot / "marker.txt").read_text(), "two")
        self.assertTrue((self.application.context.layout.staging / operation.id).exists())

    def test_factory_failure_before_checkpoint_keeps_previous_services_running(self):
        self._install()
        self._commit("two")
        operation = self._submit(OperationKind.UPDATE)
        engine = self._engine(self.application, self.supervisor)
        with (
            mock.patch.object(
                engine.task_handler,
                "_application_runtime_specs",
                side_effect=RuntimeError("injected specification resolution failure"),
            ),
            mock.patch.object(self.supervisor, "stop", wraps=self.supervisor.stop) as stop,
        ):
            engine._execute(operation.id)
        stop.assert_not_called()
        self.assertEqual(
            self.application.store.get_operation(operation.id).state, OperationState.SUCCEEDED
        )
        self.assertFalse(self._start_record(self.application, operation.id).result["started"])
        self.assertTrue(all(process.poll() is None for process in self.original_processes))
        self.assertEqual(self.application.store.configuration_revision(), 2)

    def test_factory_failure_during_recovery_rolls_back_previous_checkpoint(self):
        operation = self._interrupt_update()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        with mock.patch.object(
            engine.task_handler,
            "_application_runtime_specs",
            side_effect=RuntimeError("injected recovery resolution failure"),
        ):
            engine._execute(operation.id)
        self.assertEqual(application.store.get_operation(operation.id).state, OperationState.FAILED)
        self._assert_original(application, supervisor)

    def test_interrupted_old_api_restoration_resumes_without_duplicate_child(self):
        operation = self._interrupt_update()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        start = supervisor.start
        restored_api = []

        def cut_after_old_api(service_id):
            service = start(service_id)
            if service_id == PANDRATOR_API_SERVICE:
                runtime = supervisor._runtime[service_id]
                self.assertEqual(runtime.spec.cwd, str(self.original_slot))
                assert runtime.process is not None
                restored_api.append(runtime.process)
                self.processes.append(runtime.process)
                raise _StartupCut()
            return service

        with mock.patch.object(supervisor, "start", side_effect=cut_after_old_api):
            with self.assertRaises(_StartupCut):
                engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.ROLLING_BACK
        )
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.original_slot
        )
        self.assertEqual(self._start_record(application, operation.id).state, TaskState.ROLLED_BACK)
        self.assertTrue((application.context.layout.staging / operation.id).exists())
        supervisor.shutdown(stop_children=False)
        reopened = create_application(self.base / "workspace", registry=self.registry)
        adopted = self._supervisor(reopened)
        adopted.register_many(self._specs(reopened))
        original_api = adopted._runtime[PANDRATOR_API_SERVICE].identity
        self.assertEqual(original_api.pid, restored_api[0].pid)
        resumed = self._engine(reopened, adopted)
        resumed._recover_interrupted()
        resumed._execute(operation.id)
        self.assertEqual(reopened.store.get_operation(operation.id).state, OperationState.CANCELLED)
        self._assert_original(reopened, adopted)
        self.assertEqual(adopted._runtime[PANDRATOR_API_SERVICE].identity.pid, restored_api[0].pid)
        self.assertIsNone(restored_api[0].poll())
        self.assertFalse((reopened.context.layout.staging / operation.id).exists())

    def test_unsafe_stop_preserves_live_slot_and_checkpoint(self):
        operation = self._interrupt_update()
        journal = self._journal(operation)
        evidence = journal.read_bytes()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        with mock.patch.object(
            supervisor, "stop", side_effect=RuntimeError("injected stop refusal")
        ):
            engine._execute(operation.id)
        self.assertEqual(
            application.store.get_operation(operation.id).state, OperationState.RECOVERY_REQUIRED
        )
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.new_slot
        )
        self.assertEqual(journal.read_bytes(), evidence)
        self.assertTrue(all(process.poll() is None for process in self.replacements))

    def test_partial_old_service_restoration_failure_retains_checkpoint(self):
        operation = self._interrupt_update()
        application, supervisor = self._reopen()
        engine = self._engine(application, supervisor)
        engine._recover_interrupted()
        application.store.request_cancellation(operation.id)
        start = supervisor.start

        def fail_old_worker(service_id):
            if service_id == PANDRATOR_WORKER_SERVICE:
                raise RuntimeError("injected old worker restoration failure")
            return start(service_id)

        with mock.patch.object(supervisor, "start", side_effect=fail_old_worker):
            engine._execute(operation.id)
        record = application.store.get_operation(operation.id)
        self.assertEqual(record.state, OperationState.RECOVERY_REQUIRED)
        self.assertEqual(record.recovery["rollback_errors"][0]["task_id"], "finalize")
        self.assertEqual(
            active_component_path(application.context.layout, "pandrator"), self.original_slot
        )
        self.assertTrue((application.context.layout.staging / operation.id).exists())
        self.assertIn(PANDRATOR_API_SERVICE, supervisor._runtime)
        self.assertNotIn(PANDRATOR_WORKER_SERVICE, supervisor._runtime)
        self.assertTrue(all(process.poll() is not None for process in self.replacements))
        self._capture(supervisor)

    def test_malformed_checkpoint_fields_are_rejected_without_mutation(self):
        from pandrator_manager.operations.application_recovery import load_application_start_journal

        operation = self._interrupt_update()
        journal = self._journal(operation)
        original = journal.read_bytes()
        record = self._start_record(self.application, operation.id)
        context = OperationTaskContext(
            context=self.application.context,
            store=self.application.store,
            registry=self.registry,
            supervisor=self.supervisor,
            operation=self.application.store.get_operation(operation.id),
            plan=self.application.store.get_plan(operation.plan_id),
            prior_results={},
            cancellation=CancellationToken(),
        )
        cases = []
        for name in (
            "bool_schema",
            "string_intent",
            "foreign_spec",
            "unknown_target",
            "outside_slot",
            "missing_slot",
        ):
            payload = json.loads(original)
            if name == "bool_schema":
                payload["schema_version"] = True
            elif name == "string_intent":
                payload["previous"][PANDRATOR_API_SERVICE]["was_running"] = "false"
            elif name == "foreign_spec":
                payload["previous"][PANDRATOR_API_SERVICE]["spec"]["service_id"] = (
                    PANDRATOR_WORKER_SERVICE
                )
            elif name == "unknown_target":
                payload["target"]["foreign.service"] = payload["target"][PANDRATOR_API_SERVICE]
            elif name == "outside_slot":
                payload["previous_active_path"] = str(self.base / "outside")
            else:
                del payload["previous_active_path"]
            cases.append((name, payload))
        with mock.patch.object(self.supervisor, "stop", wraps=self.supervisor.stop) as stop:
            for name, payload in cases:
                with self.subTest(name=name):
                    journal.write_text(json.dumps(payload))
                    evidence = journal.read_bytes()
                    with self.assertRaisesRegex(RuntimeError, "startup journal is invalid"):
                        load_application_start_journal(context, record.task)
                    self.assertEqual(journal.read_bytes(), evidence)
            journal.write_bytes(original)
            parsed = load_application_start_journal(context, record.task)
            self.assertIsNotNone(parsed)
        stop.assert_not_called()
        self.assertTrue(all(process.poll() is None for process in self.replacements))
