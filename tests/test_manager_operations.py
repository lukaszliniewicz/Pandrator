import tempfile
import time
import unittest
import uuid
from pathlib import Path
from unittest import mock

from dulwich import porcelain

from pandrator_manager.application import create_application
from pandrator_manager.components import ComponentRegistry
from pandrator_manager.components.builtin import MarkerComponentDriver
from pandrator_manager.components.slots import active_component_path
from pandrator_manager.models import (
    TERMINAL_OPERATION_STATES,
    ComponentDefinition,
    DesiredComponentState,
    OperationKind,
    OperationState,
    TaskState,
)
from pandrator_manager.operations import OperationEngine
from pandrator_manager.operations.handlers import FilesystemTaskHandler


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


def _registry(repository: Path) -> ComponentRegistry:
    definition = ComponentDefinition(
        id="fixture",
        label="Fixture component",
        driver="marker",
        source_markers=("marker.txt",),
        markers=(),
        owned_paths=("services/fixture",),
        resource_locks=("component:fixture",),
        repo_url=str(repository),
    )
    return ComponentRegistry((definition,), (MarkerComponentDriver(),))


def _wait(application, operation_id: str, timeout: float = 10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = application.store.get_operation(operation_id)
        if operation.state in TERMINAL_OPERATION_STATES:
            return operation
        time.sleep(0.02)
    raise AssertionError(f"Operation {operation_id} did not finish.")


class OperationEngineTests(unittest.TestCase):
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
        self.assertEqual((active / "marker.txt").read_text(encoding="utf-8"), "one")

    def test_execution_rechecks_preflight_before_staging(self):
        plan = self.application.plan(
            kind=OperationKind.INSTALL,
            desired={"fixture": DesiredComponentState()},
        )
        self.application.context.environment["REQUESTS_CA_BUNDLE"] = str(
            self.base / "missing-ca.pem"
        )
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
        self.assertEqual(
            (restored / "marker.txt").read_text(encoding="utf-8"),
            "one",
        )

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
        self.addCleanup(engine.shutdown)
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
