import faulthandler
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from dulwich import porcelain

import pandrator_manager.operations.handlers as handlers_module
from pandrator_manager.application import create_application
from pandrator_manager.components import ComponentRegistry
from pandrator_manager.components.builtin import MarkerComponentDriver
from pandrator_manager.components.slots import (
    active_component_path,
    component_pointer,
)
from pandrator_manager.context import CancellationToken
from pandrator_manager.errors import CancellationRequested, ManagerError
from pandrator_manager.models import (
    TERMINAL_OPERATION_STATES,
    ComponentDefinition,
    DesiredComponentState,
    HealthResult,
    HealthState,
    ManagedProcessSpec,
    ManagedService,
    OperationKind,
    OperationState,
    TaskState,
)
from pandrator_manager.operations import OperationEngine
from pandrator_manager.operations.engine import _StoreCancellation
from pandrator_manager.operations.handlers import (
    FilesystemTaskHandler,
    OperationTaskContext,
)
from pandrator_manager.supervisor import ProcessSupervisor


def _commit(repository: Path, content: str) -> str:
    (repository / "marker.txt").write_text(content, encoding="utf-8")
    porcelain.add(str(repository), paths=["marker.txt"])
    commit = porcelain.commit(
        str(repository),
        message=f"version {content}".encode(),
        author=b"Pandrator tests <tests@example.invalid>",
        committer=b"Pandrator tests <tests@example.invalid>",
    )
    return commit.decode()


def _registry(
    repository: Path,
    *,
    source_revision: str | None = None,
    service_key: str | None = None,
) -> ComponentRegistry:
    definition = ComponentDefinition(
        id="fixture",
        label="Fixture component",
        driver="marker",
        source_markers=("marker.txt",),
        markers=(),
        owned_paths=("services/fixture",),
        resource_locks=("component:fixture",),
        repo_url=str(repository),
        source_revision=source_revision,
        service_key=service_key,
    )
    return ComponentRegistry((definition,), (MarkerComponentDriver(),))


# These operations clone and activate files on disk. Windows CI can take more
# than ten seconds even for the local fixture; this is not a cancellation SLA.
def _wait(application, operation_id: str, timeout: float = 30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = application.store.get_operation(operation_id)
        if operation.state in TERMINAL_OPERATION_STATES:
            return operation
        time.sleep(0.02)
    faulthandler.dump_traceback()
    operation = application.store.get_operation(operation_id)
    raise AssertionError(
        f"Operation {operation_id} did not finish within {timeout}s: "
        f"state={operation.state}, current_task={operation.current_task_id}."
    )


class OperationEngineTests(unittest.TestCase):
    def test_transient_database_lock_does_not_abort_subprocess_cancellation_poll(self):
        store = mock.Mock()
        store.cancellation_requested.side_effect = sqlite3.OperationalError(
            "database is locked"
        )
        cancellation = _StoreCancellation(store, "operation-one")

        with self.assertLogs(level="WARNING"):
            self.assertFalse(cancellation.requested)

    def test_cancellation_poll_does_not_hide_other_database_errors(self):
        store = mock.Mock()
        store.cancellation_requested.side_effect = sqlite3.OperationalError(
            "disk I/O error"
        )
        cancellation = _StoreCancellation(store, "operation-one")

        with self.assertRaisesRegex(sqlite3.OperationalError, "disk I/O"):
            _ = cancellation.requested

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.repository = self.base / "origin"
        porcelain.init(str(self.repository))
        self.first_revision = _commit(self.repository, "one")
        self.application = create_application(
            self.base / "workspace",
            registry=_registry(self.repository),
        )

    def _plan_and_submit(
        self,
        kind: OperationKind,
        desired: DesiredComponentState,
    ):
        plan = self.application.plan(
            kind=kind,
            desired={"fixture": desired},
        )
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        return plan, operation

    def test_install_journals_tasks_and_activates_versioned_slot(self):
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)

        _plan, submitted = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        completed = _wait(self.application, submitted.id)

        self.assertEqual(completed.state, OperationState.SUCCEEDED)
        self.assertEqual(completed.progress, 1)
        tasks = self.application.store.operation_tasks(submitted.id)
        self.assertTrue(tasks)
        self.assertTrue(all(task.state == TaskState.SUCCEEDED for task in tasks))
        active = active_component_path(
            self.application.context.layout,
            "fixture",
        )
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual((active / "marker.txt").read_text(encoding="utf-8"), "one")

    def test_install_checks_out_pinned_source_revision(self):
        _commit(self.repository, "two")
        application = create_application(
            self.base / "pinned-workspace",
            registry=_registry(
                self.repository, source_revision=self.first_revision
            ),
        )
        engine = OperationEngine(
            application.context,
            application.store,
            application.registry,
        )
        application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        plan = application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        submitted, created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)

        completed = _wait(application, submitted.id)
        self.assertEqual(completed.state, OperationState.SUCCEEDED)
        active = active_component_path(application.context.layout, "fixture")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual((active / "marker.txt").read_text(encoding="utf-8"), "one")

    def test_retry_rechecks_pinned_revision_on_reused_staging(self):
        second_revision = _commit(self.repository, "two")
        application = create_application(
            self.base / "pinned-workspace",
            registry=_registry(
                self.repository, source_revision=self.first_revision
            ),
        )
        plan = application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        submitted, created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        execution = OperationTaskContext(
            context=application.context,
            store=application.store,
            registry=application.registry,
            supervisor=None,
            operation=submitted,
            plan=plan,
            prior_results={},
            cancellation=CancellationToken(),
        )
        stage = next(
            task for task in plan.tasks if task.kind == "stage_component"
        )
        handler = FilesystemTaskHandler()
        real_reset = porcelain.reset
        with mock.patch.object(
            porcelain,
            "reset",
            side_effect=RuntimeError("injected reset failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "source revision"):
                handler.execute(execution, stage)

        target = handler._staging_source(execution, "fixture")
        self.assertEqual((target / "marker.txt").read_text(), "two")
        self.assertEqual(handler._revision(target), second_revision)

        with mock.patch.object(porcelain, "reset", wraps=real_reset) as reset:
            result = handler.execute(execution, stage)

        self.assertTrue(result["reused"])
        self.assertEqual(result["revision"], self.first_revision)
        self.assertEqual(
            (target / "marker.txt").read_text(encoding="utf-8"), "one"
        )
        reset.assert_called_once()

    def test_already_pinned_staging_is_reused_without_reset(self):
        application = create_application(
            self.base / "pinned-workspace",
            registry=_registry(
                self.repository, source_revision=self.first_revision
            ),
        )
        plan = application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        submitted, created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key for confirmation in plan.confirmations
            ),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        execution = OperationTaskContext(
            context=application.context,
            store=application.store,
            registry=application.registry,
            supervisor=None,
            operation=submitted,
            plan=plan,
            prior_results={},
            cancellation=CancellationToken(),
        )
        stage = next(
            task for task in plan.tasks if task.kind == "stage_component"
        )
        handler = FilesystemTaskHandler()
        handler.execute(execution, stage)

        with mock.patch.object(porcelain, "reset") as reset:
            result = handler.execute(execution, stage)

        self.assertTrue(result["reused"])
        self.assertEqual(result["revision"], self.first_revision)
        reset.assert_not_called()

    def test_shared_source_helpers_preserve_subclasses_and_fresh_facade_bindings(self):
        _commit(self.repository, "two")
        application = create_application(
            self.base / "source-helper-workspace",
            registry=_registry(self.repository, source_revision=self.first_revision),
        )
        plan = application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        operation, created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        execution = OperationTaskContext(
            context=application.context,
            store=application.store,
            registry=application.registry,
            supervisor=None,
            operation=operation,
            plan=plan,
            prior_results={},
            cancellation=application.context.cancellation,
        )
        stage = next(task for task in plan.tasks if task.kind == "stage_component")
        verify = next(task for task in plan.tasks if task.kind == "verify_component")
        calls: list[str] = []

        class ObservingHandler(FilesystemTaskHandler):
            @staticmethod
            def _definition(execution, task):
                calls.append("definition")
                return FilesystemTaskHandler._definition(execution, task)

            def _staging_source(self, execution, component_id):
                calls.append("staging_source")
                return super()._staging_source(execution, component_id)

            @staticmethod
            def _revision(repository):
                calls.append("revision")
                return FilesystemTaskHandler._revision(repository)

            @staticmethod
            def _markers_present(root, markers):
                calls.append("markers_present")
                return FilesystemTaskHandler._markers_present(root, markers)

            @staticmethod
            def _source_markers(execution, definition):
                calls.append("source_markers")
                return FilesystemTaskHandler._source_markers(execution, definition)

            def _stage_result(self, execution, component_id):
                calls.append("stage_result")
                return super()._stage_result(execution, component_id)

        handler = ObservingHandler()
        native_writer = handlers_module._atomic_text
        with mock.patch.object(handlers_module.porcelain, "clone", wraps=porcelain.clone) as clone:
            for attempt, content in enumerate(("# first adapter\n", "# rebound adapter\n")):
                with (
                    mock.patch.object(
                        handlers_module,
                        "generated_runtime_files",
                        return_value={"fixture_adapter.py": content},
                    ) as generator,
                    mock.patch.object(handlers_module, "_atomic_text", wraps=native_writer) as writer,
                ):
                    result = handler.execute(execution, stage)
                target = Path(result["staged_path"])
                self.assertEqual(result["reused"], bool(attempt))
                self.assertEqual(result["revision"], self.first_revision)
                self.assertEqual((target / "marker.txt").read_text(), "one")
                self.assertEqual((target / "fixture_adapter.py").read_text(), content)
                generator.assert_called_once_with("fixture")
                writer.assert_called_once_with(target / "fixture_adapter.py", content)
                execution.prior_results[stage.id] = result
                verified = handler.execute(execution, verify)
                self.assertEqual(verified["verified_path"], str(target))
                self.assertEqual(verified["revision"], self.first_revision)
            clone.assert_called_once()
        self.assertTrue(
            {
                "definition",
                "staging_source",
                "revision",
                "markers_present",
                "source_markers",
                "stage_result",
            }.issubset(calls)
        )
        # Fresh clone reset checks must call cls._revision on the subclass.
        self.assertGreaterEqual(calls.count("revision"), 5)
        target = Path(execution.prior_results[stage.id]["staged_path"])
        shutil.rmtree(target)
        rejected = ManagerError("fixture_source_rejected", "Current facade error helper.")
        clone_error = RuntimeError("fixture clone denial")
        with (
            mock.patch.object(handlers_module.porcelain, "clone", side_effect=clone_error),
            mock.patch.object(
                handlers_module, "_source_acquisition_error", return_value=rejected
            ) as translate,
            self.assertRaises(ManagerError) as caught,
        ):
            handler.execute(execution, stage)
        self.assertIs(caught.exception, rejected)
        self.assertIs(caught.exception.__cause__, clone_error)
        translate.assert_called_once()
        self.assertIs(translate.call_args.kwargs["error"], clone_error)
        self.assertEqual(translate.call_args.kwargs["repo_url"], str(self.repository))

    def test_execution_rechecks_preflight_before_staging(self):
        plan = self.application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        environment = self.application.context.environment
        assert isinstance(environment, dict)
        environment["REQUESTS_CA_BUNDLE"] = str(self.base / "missing-ca.pem")
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        submitted, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=(),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)

        completed = _wait(self.application, submitted.id)
        self.assertEqual(completed.state, OperationState.FAILED)
        tasks = self.application.store.operation_tasks(submitted.id)
        self.assertEqual(tasks[0].task.id, "operation:preflight")
        self.assertEqual(tasks[0].state, TaskState.ROLLED_BACK)
        self.assertEqual(tasks[0].error["code"], "preflight_failed")
        self.assertIsNotNone(tasks[0].started_at)
        self.assertIsNotNone(tasks[0].finished_at)
        self.assertTrue(
            all(task.state == TaskState.PENDING for task in tasks[1:])
        )
        self.assertIsNone(
            active_component_path(self.application.context.layout, "fixture")
        )

    def test_application_autostart_replaces_running_specs_before_restart(self):
        handler = FilesystemTaskHandler()
        supervisor = mock.Mock()
        supervisor.snapshot.return_value = [
            mock.Mock(id="pandrator.api", process=object()),
            mock.Mock(id="pandrator.worker", process=object()),
        ]
        api_spec = mock.Mock(service_id="pandrator.api")
        worker_spec = mock.Mock(service_id="pandrator.worker")
        supervisor.start.return_value = mock.Mock(
            id="pandrator.worker",
            health=None,
        )
        execution = mock.Mock()
        execution.supervisor = supervisor
        execution.plan.desired = {}
        execution.context.layout = self.application.context.layout
        execution.context.environment = {}
        network = mock.Mock(application=mock.sentinel.exposure)

        with (
            mock.patch(
                "pandrator_manager.operations.handlers.load_network_configuration",
                return_value=network,
            ),
            mock.patch(
                "pandrator_manager.operations.handlers.pandrator_runtime_specs",
                return_value=(api_spec, worker_spec),
            ),
        ):
            result = handler._execute_start_application(
                execution,
                mock.Mock(),
            )

        self.assertTrue(result["started"])
        lifecycle_calls = [
            call
            for call in supervisor.mock_calls
            if call[0] in {"snapshot", "stop", "replace_spec", "start"}
        ]
        self.assertEqual(
            lifecycle_calls,
            [
                mock.call.snapshot(),
                mock.call.stop("pandrator.worker"),
                mock.call.stop("pandrator.api"),
                mock.call.replace_spec(api_spec),
                mock.call.replace_spec(worker_spec),
                mock.call.start("pandrator.worker"),
            ],
        )

    def test_stop_and_validate_preserve_desired_running_when_process_is_absent(self):
        handler = FilesystemTaskHandler()
        supervisor = mock.Mock()
        supervisor.snapshot.return_value = [
            ManagedService(
                id="fixture.service",
                component_id="fixture",
                service_key="fixture.service",
                desired_running=True,
                health=HealthResult(
                    state=HealthState.UNHEALTHY,
                    service_id="fixture.service",
                ),
                process=None,
            )
        ]
        execution = mock.Mock()
        execution.supervisor = supervisor
        execution.context.layout = self.application.context.layout
        execution.operation.id = str(uuid.uuid4())
        definition = mock.Mock(
            id="fixture",
            label="Fixture component",
            service_key="fixture.service",
        )
        task = mock.Mock(component_id="fixture", id="fixture:stop")
        with mock.patch.object(handler, "_definition", return_value=definition):
            stopped = handler._execute_stop_service(execution, task)

        self.assertFalse(stopped["was_running"])
        self.assertTrue(stopped["desired_running"])
        supervisor.stop.assert_called_once_with("fixture.service")

        execution.plan.desired = {"fixture": DesiredComponentState()}
        execution.prior_results = {"fixture:stop": stopped}
        execution.registry.driver.return_value.resolve.return_value = (
            mock.sentinel.resolved
        )
        replacement = ManagedProcessSpec(
            service_id="fixture.service",
            component_id="fixture",
            label="Fixture service",
            executable=sys.executable,
            arguments=("-c", "pass"),
        )
        execution.service_spec_factory.return_value = replacement

        def replace_spec(_spec, *, before_replace=None):
            if before_replace is not None:
                before_replace(None)
            return None

        supervisor.replace_spec.side_effect = replace_spec
        supervisor.start.return_value = mock.Mock(health=None)
        validation_task = mock.Mock(component_id="fixture", id="fixture:validate-service")
        with mock.patch.object(handler, "_definition", return_value=definition):
            validated = handler._execute_validate_service(execution, validation_task)

        self.assertTrue(validated["kept_running"])
        supervisor.start.assert_called_once_with("fixture.service")
        # The service was already absent, so validation must not stop the
        # replacement merely because the old process was transiently gone.
        supervisor.stop.assert_called_once_with("fixture.service")

        handler._rollback_stop_service(
            execution,
            task,
            {
                "service_id": "fixture.service",
                "was_running": False,
                "desired_running": True,
            },
        )
        self.assertEqual(
            supervisor.start.call_args_list,
            [mock.call("fixture.service"), mock.call("fixture.service")],
        )

    def test_operation_execution_holds_shared_lifecycle_lock(self):
        lifecycle_lock = self.application.lifecycle_lock
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            lifecycle_lock=lifecycle_lock,
        )
        entered = threading.Event()
        release = threading.Event()

        def observe_lock(_operation_id):
            entered.set()
            self.assertTrue(release.wait(timeout=5))

        with mock.patch.object(engine, "_execute_locked", side_effect=observe_lock):
            worker = threading.Thread(target=engine._execute, args=("operation",))
            worker.start()
            self.assertTrue(entered.wait(timeout=5))
            self.assertFalse(lifecycle_lock.acquire(blocking=False))
            release.set()
            worker.join(timeout=5)

        self.assertFalse(worker.is_alive())

    def test_update_activation_failure_restores_previous_slot(self):
        first_engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        self.application.attach_operation_queue(first_engine)
        first_engine.start()
        _, first = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        self.assertEqual(
            _wait(self.application, first.id).state,
            OperationState.SUCCEEDED,
        )
        first_engine.shutdown()
        previous = active_component_path(
            self.application.context.layout,
            "fixture",
        )
        self.assertIsNotNone(previous)

        _commit(self.repository, "two")

        def fail_after_activation(_operation, task, _result):
            if task.kind == "activate_component":
                raise RuntimeError("injected post-activation failure")

        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            fault_injector=fail_after_activation,
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        _, submitted = self._plan_and_submit(
            OperationKind.UPDATE,
            DesiredComponentState(),
        )
        completed = _wait(self.application, submitted.id)

        self.assertEqual(completed.state, OperationState.FAILED)
        restored = active_component_path(
            self.application.context.layout,
            "fixture",
        )
        self.assertEqual(restored, previous)
        assert restored is not None
        self.assertEqual(
            (restored / "marker.txt").read_text(encoding="utf-8"),
            "one",
        )

    def test_service_update_rollback_restores_native_spec_and_running_intent(self):
        application = create_application(
            self.base / "service-rollback-workspace",
            registry=_registry(self.repository, service_key="fixture.service"),
        )
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id="service-rollback-test",
        )
        self.addCleanup(supervisor.shutdown, stop_children=True)

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

        def fail_after_update_validation(operation, task, _result):
            if operation.kind == OperationKind.UPDATE and task.kind == "validate_service":
                raise RuntimeError("injected failure after native service validation")

        engine = OperationEngine(
            application.context,
            application.store,
            application.registry,
            supervisor=supervisor,
            service_spec_factory=service_spec_factory,
            fault_injector=fail_after_update_validation,
        )
        application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)

        def submit(kind, desired):
            plan = application.plan(kind=kind, desired={"fixture": desired})
            operation, created = application.submit_operation(
                plan_id=plan.id,
                plan_digest=plan.digest,
                accepted_confirmations=tuple(item.key for item in plan.confirmations),
                idempotency_key=str(uuid.uuid4()),
            )
            self.assertTrue(created)
            return operation

        installed = submit(
            OperationKind.INSTALL,
            DesiredComponentState(options={"start_after_install": True}),
        )
        self.assertEqual(_wait(application, installed.id).state, OperationState.SUCCEEDED)
        previous = active_component_path(application.context.layout, "fixture")
        previous_spec = supervisor.spec("fixture.service")
        previous_process = supervisor._runtime["fixture.service"].process
        self.assertIsNotNone(previous_process)
        assert previous_process is not None
        self.assertIsNone(previous_process.poll())

        _commit(self.repository, "two")
        updated = submit(OperationKind.UPDATE, DesiredComponentState())
        self.assertEqual(_wait(application, updated.id).state, OperationState.FAILED)
        records = {item.task.kind: item for item in application.store.operation_tasks(updated.id)}
        stopped = records["stop_service"]
        validated = records["validate_service"]
        self.assertEqual(stopped.state, TaskState.ROLLED_BACK)
        self.assertTrue(stopped.result["was_running"])
        self.assertTrue(stopped.result["desired_running"])
        self.assertEqual(validated.state, TaskState.ROLLED_BACK)
        self.assertTrue(validated.result["kept_running"])
        self.assertIsNotNone(previous_spec)
        assert previous_spec is not None
        self.assertEqual(validated.result["previous_spec"], previous_spec.model_dump(mode="json"))
        self.assertEqual(supervisor.spec("fixture.service"), previous_spec)
        self.assertEqual(active_component_path(application.context.layout, "fixture"), previous)
        self.assertIsNotNone(previous_process.poll())
        restored_process = supervisor._runtime["fixture.service"].process
        self.assertIsNotNone(restored_process)
        assert restored_process is not None
        self.assertIsNone(restored_process.poll())
        self.assertIsNot(restored_process, previous_process)
        self.assertTrue(supervisor.snapshot()[0].desired_running)
        self.assertEqual(application.store.configuration_revision(), 1)

    def test_failed_validation_stop_preserves_live_slot_for_recovery(self):
        application = create_application(
            self.base / "service-workspace",
            registry=_registry(
                self.repository,
                service_key="fixture.service",
            ),
        )
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id="manager-test",
        )

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
        )
        application.attach_operation_queue(engine)
        plan = application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        submitted, created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=(),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        real_stop = supervisor.stop
        owned_runtime = None
        owned_process = None
        try:
            with mock.patch.object(
                supervisor,
                "stop",
                side_effect=RuntimeError("injected unverifiable stop"),
            ):
                engine.start()
                completed = _wait(application, submitted.id)
                engine.shutdown()

            self.assertEqual(completed.state, OperationState.RECOVERY_REQUIRED)
            active = active_component_path(application.context.layout, "fixture")
            self.assertIsNotNone(active)
            assert active is not None
            self.assertTrue(active.is_dir())
            self.assertEqual(
                (active / "marker.txt").read_text(encoding="utf-8"),
                "one",
            )
            self.assertIn("fixture.service", supervisor._runtime)
            rollback_errors = completed.recovery.get("rollback_errors", [])
            self.assertTrue(
                any(
                    "live or unverifiable" in str(item.get("error", {}).get("message"))
                    for item in rollback_errors
                )
            )
        finally:
            owned_runtime = supervisor._runtime.get("fixture.service")
            if owned_runtime is not None:
                owned_process = owned_runtime.process
            try:
                engine.shutdown()
            finally:
                try:
                    if "fixture.service" in supervisor._runtime:
                        real_stop("fixture.service")
                finally:
                    if owned_process is not None:
                        if owned_process.poll() is None:
                            owned_process.kill()
                        self.assertIsNotNone(owned_process.wait(timeout=5))
                    if (
                        owned_runtime is not None
                        and owned_runtime.log_handle is not None
                    ):
                        owned_runtime.log_handle.close()
                        owned_runtime.log_handle = None

    def test_slot_removal_blocks_a_concurrent_runtime_start(self):
        application = create_application(
            self.base / "concurrent-service-workspace",
            registry=_registry(
                self.repository,
                service_key="fixture.service",
            ),
        )
        supervisor = ProcessSupervisor(
            application.context,
            application.store,
            manager_instance_id="manager-test",
        )
        versions = application.context.layout.services / "fixture" / "versions"
        active = versions / "concurrent"
        active.mkdir(parents=True)
        (active / "marker.txt").write_text("one", encoding="utf-8")
        pointer = component_pointer(application.context.layout, "fixture")
        pointer.write_text(
            f'{{"path": "{active}"}}',
            encoding="utf-8",
        )
        supervisor.register(
            ManagedProcessSpec(
                service_id="fixture.service",
                component_id="fixture",
                label="Fixture service",
                executable=sys.executable,
                arguments=("-c", "import time; time.sleep(60)"),
                cwd=str(active),
                startup_timeout_seconds=3,
                shutdown_timeout_seconds=1,
            )
        )
        execution = mock.Mock(
            context=application.context,
            registry=application.registry,
            supervisor=supervisor,
        )
        task = mock.Mock(component_id="fixture")
        handler = FilesystemTaskHandler()
        removal_entered = threading.Event()
        allow_removal = threading.Event()
        rollback_errors = []
        start_errors = []
        real_rmtree = shutil.rmtree

        def blocking_rmtree(path):
            if Path(path) == active:
                removal_entered.set()
                self.assertTrue(allow_removal.wait(timeout=5))
            real_rmtree(path)

        def rollback():
            try:
                handler._rollback_activate_component(
                    execution,
                    task,
                    {
                        "active_path": str(active),
                        "created_slot": True,
                        "previous_pointer": None,
                    },
                )
            except Exception as error:
                rollback_errors.append(error)

        def start():
            try:
                supervisor.start("fixture.service")
            except Exception as error:
                start_errors.append(error)

        with mock.patch(
            "pandrator_manager.operations.handlers.shutil.rmtree",
            side_effect=blocking_rmtree,
        ):
            rollback_thread = threading.Thread(target=rollback)
            rollback_thread.start()
            self.assertTrue(removal_entered.wait(timeout=5))
            start_thread = threading.Thread(target=start)
            start_thread.start()
            time.sleep(0.1)
            self.assertTrue(start_thread.is_alive())
            self.assertNotIn("fixture.service", supervisor._runtime)
            self.assertTrue(active.is_dir())
            allow_removal.set()
            rollback_thread.join(timeout=5)
            start_thread.join(timeout=5)

        self.assertFalse(rollback_thread.is_alive())
        self.assertFalse(start_thread.is_alive())
        self.assertEqual(rollback_errors, [])
        self.assertFalse(active.exists())
        self.assertTrue(start_errors)
        self.assertNotIn("fixture.service", supervisor._runtime)

    def test_cancelled_queued_operation_rolls_back_without_activation(self):
        _plan, submitted = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        self.application.store.request_cancellation(submitted.id)
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        engine.start()
        self.addCleanup(engine.shutdown)

        completed = _wait(self.application, submitted.id)
        self.assertEqual(completed.state, OperationState.CANCELLED)
        self.assertIsNone(
            active_component_path(self.application.context.layout, "fixture")
        )

    def _assert_interrupted_rollback_resumes(self, *, savepoint, cancelled=False, legacy=False):
        plan, submitted = self._plan_and_submit(OperationKind.INSTALL, DesiredComponentState())
        store = self.application.store
        activation = next(item for item in plan.tasks if item.kind == "activate_component")
        native_operation_save = store.update_operation
        native_task_save = store.update_operation_task
        stopped: list[str] = []

        class FixtureDaemonStop(BaseException):
            pass

        def save_operation(operation):
            if savepoint == "terminal" and operation.state in TERMINAL_OPERATION_STATES and not stopped:
                stopped.append("before terminal operation save")
                raise FixtureDaemonStop()
            return native_operation_save(operation)

        def save_task(operation_id, task_id, **kwargs):
            result = native_task_save(operation_id, task_id, **kwargs)
            if (
                savepoint == "receipt"
                and task_id == activation.id
                and kwargs.get("state") == TaskState.ROLLED_BACK
                and not stopped
            ):
                stopped.append("after activation rollback receipt save")
                raise FixtureDaemonStop()
            return result

        def reject_activation(operation, task, _result):
            if task.kind == "activate_component":
                if cancelled:
                    store.request_cancellation(operation.id)
                    raise CancellationRequested()
                raise ManagerError("fixture_rejected", "Keep the previous activation.", {"marker": "original cause"})

        engine = OperationEngine(
            self.application.context,
            store,
            self.application.registry,
            fault_injector=reject_activation,
        )
        with (
            mock.patch.object(store, "update_operation", side_effect=save_operation),
            mock.patch.object(store, "update_operation_task", side_effect=save_task),
            self.assertRaises(FixtureDaemonStop),
        ):
            engine._execute(submitted.id)
        self.assertEqual(len(stopped), 1)
        interrupted = store.get_operation(submitted.id)
        self.assertEqual(interrupted.state, OperationState.ROLLING_BACK)
        self.assertIsNone(active_component_path(self.application.context.layout, "fixture"))
        before_tasks = store.operation_tasks(submitted.id)
        before_attempts = {item.task.id: item.attempt for item in before_tasks}
        rolled_receipts = {item.task.id: item.result for item in before_tasks if item.state == TaskState.ROLLED_BACK}
        self.assertIn(activation.id, rolled_receipts)
        if legacy:
            interrupted.recovery = {}
            store.update_operation(interrupted)
        reopened = create_application(self.base / "workspace", registry=_registry(self.repository))
        forward: list[str] = []
        rollback: list[str] = []
        seen_receipts: list[dict] = []

        class ObservingHandler(FilesystemTaskHandler):
            def execute(self, execution, task):
                forward.append(task.id)
                return super().execute(execution, task)

            def rollback(self, execution, task, result):
                rollback.append(task.id)
                seen_receipts.append(dict(execution.prior_results))
                return super().rollback(execution, task, result)

            def finalize(self, execution, *, succeeded):
                seen_receipts.append(dict(execution.prior_results))
                return super().finalize(execution, succeeded=succeeded)

        restarted = OperationEngine(reopened.context, reopened.store, reopened.registry, task_handler=ObservingHandler())
        native_recover = restarted._recover_interrupted
        recovered_states: list[OperationState] = []

        def recover():
            native_recover()
            recovered_states.append(reopened.store.get_operation(submitted.id).state)

        with mock.patch.object(restarted, "_recover_interrupted", side_effect=recover):
            restarted.start()
        self.addCleanup(restarted.shutdown, timeout=30)
        completed = _wait(reopened, submitted.id)
        restarted.shutdown(timeout=30)
        self.assertEqual(forward, [])
        self.assertEqual(recovered_states, [OperationState.ROLLING_BACK])
        self.assertEqual(completed.state, OperationState.CANCELLED if cancelled else OperationState.FAILED)
        self.assertEqual(completed.error_code, "cancelled" if cancelled else "fixture_rejected")
        self.assertEqual(completed.error_message, interrupted.error_message)
        self.assertEqual(completed.recovery, {"rollback_errors": []})
        expected_cause = {
            "code": "cancelled" if cancelled else "fixture_rejected",
            "message": interrupted.error_message,
        }
        if not cancelled and not legacy:
            expected_cause["details"] = {"marker": "original cause"}
        if not legacy:
            self.assertEqual(interrupted.recovery["rollback_cause"], expected_cause)
            self.assertIs(interrupted.recovery["rollback_cancelled"], cancelled)
        terminal_events = [
            event for event in reopened.store.events_after(0, limit=1000)
            if event.operation_id == submitted.id
            and event.event_type == f"operation.{completed.state.value}"
        ]
        self.assertEqual(len(terminal_events), 1)
        self.assertEqual(terminal_events[0].payload["error"], expected_cause)
        self.assertEqual(reopened.store.configuration_revision(), 0)
        self.assertIsNone(active_component_path(reopened.context.layout, "fixture"))
        after_tasks = reopened.store.operation_tasks(submitted.id)
        self.assertEqual({item.task.id: item.attempt for item in after_tasks}, before_attempts)
        for item in after_tasks:
            self.assertIn(item.state, {TaskState.ROLLED_BACK, TaskState.PENDING})
        self.assertTrue(seen_receipts)
        for receipts in seen_receipts:
            for task_id, value in rolled_receipts.items():
                self.assertEqual(receipts.get(task_id), value)
        self.assertNotIn(activation.id, rollback)

    def test_restart_continues_rollback_after_terminal_save_interruption(self):
        self._assert_interrupted_rollback_resumes(savepoint="terminal")

    def test_restart_continues_partially_recorded_rollback(self):
        self._assert_interrupted_rollback_resumes(savepoint="receipt")

    def test_restart_preserves_cancelled_partial_rollback(self):
        self._assert_interrupted_rollback_resumes(savepoint="receipt", cancelled=True)

    def test_restart_supports_legacy_rollback_cause_fields(self):
        self._assert_interrupted_rollback_resumes(savepoint="terminal", legacy=True)

    def test_interrupted_running_task_is_recovered_and_retried(self):
        _plan, submitted = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        running = submitted.model_copy(
            update={"state": OperationState.RUNNING}
        )
        self.application.store.update_operation(running)
        first = self.application.store.operation_tasks(submitted.id)[0]
        self.application.store.update_operation_task(
            submitted.id,
            first.task.id,
            state=TaskState.RUNNING,
            attempt=1,
        )

        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        engine.start()
        self.addCleanup(engine.shutdown, timeout=30)
        completed = _wait(self.application, submitted.id)

        self.assertEqual(completed.state, OperationState.SUCCEEDED)
        retried = self.application.store.operation_tasks(submitted.id)[0]
        self.assertEqual(retried.attempt, 2)

    def test_remove_uses_positive_ownership_and_preserves_user_data(self):
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        _, installed = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        self.assertEqual(
            _wait(self.application, installed.id).state,
            OperationState.SUCCEEDED,
        )
        user_data = self.application.context.layout.data / "keep.txt"
        user_data.write_text("keep", encoding="utf-8")

        _, submitted = self._plan_and_submit(
            OperationKind.REMOVE,
            DesiredComponentState(present=False),
        )
        completed = _wait(self.application, submitted.id)

        self.assertEqual(completed.state, OperationState.SUCCEEDED)
        self.assertIsNone(
            active_component_path(self.application.context.layout, "fixture")
        )
        self.assertEqual(user_data.read_text(encoding="utf-8"), "keep")
        self.assertFalse(
            [
                record
                for record in self.application.store.owned_paths()
                if record["owner_id"] == "fixture"
            ]
        )

    def test_database_commit_failure_rolls_back_files_and_leaves_no_ownership(self):
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        self.application.attach_operation_queue(engine)
        with mock.patch.object(
            self.application.store,
            "commit_operation_success",
            side_effect=RuntimeError("injected database commit failure"),
        ):
            engine.start()
            try:
                _plan, submitted = self._plan_and_submit(
                    OperationKind.INSTALL,
                    DesiredComponentState(),
                )
                completed = _wait(
                    self.application,
                    submitted.id,
                    timeout=30,
                )
            finally:
                engine.shutdown(timeout=30)

        self.assertEqual(completed.state, OperationState.FAILED)
        self.assertIsNone(
            active_component_path(self.application.context.layout, "fixture")
        )
        self.assertEqual(self.application.store.configuration_revision(), 0)
        self.assertEqual(self.application.store.owned_paths(), [])
        self.assertNotIn(
            "fixture",
            self.application.store.component_records(),
        )

    def test_cleanup_failure_after_commit_does_not_roll_back_success(self):
        class FailingCleanupHandler(FilesystemTaskHandler):
            def finalize(self, execution, *, succeeded):
                super().finalize(execution, succeeded=succeeded)
                if succeeded:
                    raise RuntimeError("injected cleanup failure")

        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            task_handler=FailingCleanupHandler(),
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        _plan, submitted = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        completed = _wait(self.application, submitted.id)
        deadline = time.monotonic() + 2
        while (
            "cleanup_warnings" not in completed.recovery
            and time.monotonic() < deadline
        ):
            time.sleep(0.01)
            completed = self.application.store.get_operation(submitted.id)

        self.assertEqual(completed.state, OperationState.SUCCEEDED)
        self.assertIn("cleanup_warnings", completed.recovery)
        self.assertIsNotNone(
            active_component_path(self.application.context.layout, "fixture")
        )
        self.assertEqual(self.application.store.configuration_revision(), 1)
        self.assertEqual(
            self.application.store.owned_paths()[0]["owner_id"],
            "fixture",
        )

    def test_failed_activation_without_a_task_result_uses_rollback_journal(self):
        class InterruptedActivationHandler(FilesystemTaskHandler):
            def _execute_activate_component(self, execution, task):
                super()._execute_activate_component(execution, task)
                raise RuntimeError("interrupted after activation")

        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            task_handler=InterruptedActivationHandler(),
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        _plan, submitted = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        completed = _wait(self.application, submitted.id)

        self.assertEqual(completed.state, OperationState.FAILED)
        self.assertIsNone(
            active_component_path(self.application.context.layout, "fixture")
        )
        self.assertEqual(self.application.store.owned_paths(), [])

    def test_failed_remove_without_a_task_result_restores_operation_backup(self):
        first_engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
        )
        self.application.attach_operation_queue(first_engine)
        first_engine.start()
        _, installed = self._plan_and_submit(
            OperationKind.INSTALL,
            DesiredComponentState(),
        )
        self.assertEqual(
            _wait(self.application, installed.id).state,
            OperationState.SUCCEEDED,
        )
        first_engine.shutdown()

        class InterruptedRemoveHandler(FilesystemTaskHandler):
            def _execute_remove_owned_component(self, execution, task):
                super()._execute_remove_owned_component(execution, task)
                raise RuntimeError("interrupted after remove")

        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            task_handler=InterruptedRemoveHandler(),
        )
        self.application.attach_operation_queue(engine)
        engine.start()
        self.addCleanup(engine.shutdown)
        _, submitted = self._plan_and_submit(
            OperationKind.REMOVE,
            DesiredComponentState(present=False),
        )
        completed = _wait(self.application, submitted.id)

        self.assertEqual(completed.state, OperationState.FAILED)
        restored = active_component_path(
            self.application.context.layout,
            "fixture",
        )
        self.assertIsNotNone(restored)
        assert restored is not None
        self.assertEqual(
            (restored / "marker.txt").read_text(encoding="utf-8"),
            "one",
        )
        self.assertEqual(
            self.application.store.owned_paths()[0]["owner_id"],
            "fixture",
        )


if __name__ == "__main__":
    unittest.main()
