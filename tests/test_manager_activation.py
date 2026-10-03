"""Native activation restart and redirected-directory preservation."""

import json
import os
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path

from dulwich import porcelain

from pandrator_manager.application import ManagerApplication, create_application
from pandrator_manager.components import ComponentRegistry
from pandrator_manager.components.builtin import MarkerComponentDriver
from pandrator_manager.components.slots import active_component_path, component_pointer
from pandrator_manager.models import (
    TERMINAL_OPERATION_STATES,
    ComponentDefinition,
    DesiredComponentState,
    ManagedProcessSpec,
    OperationKind,
    OperationState,
    TaskState,
)
from pandrator_manager.operations import OperationEngine
from pandrator_manager.operations.handlers import FilesystemTaskHandler
from pandrator_manager.supervisor import ProcessSupervisor


class ActivationTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.repository = self.base / "origin"
        porcelain.init(str(self.repository))
        self.first_revision = self._commit("one")
        self.registry = ComponentRegistry(
            (
                ComponentDefinition(
                    id="fixture",
                    label="Fixture",
                    driver="marker",
                    source_markers=("marker.txt",),
                    markers=(),
                    owned_paths=("services/fixture",),
                    resource_locks=("component:fixture",),
                    repo_url=str(self.repository),
                ),
            ),
            (MarkerComponentDriver(),),
        )
        self.application = create_application(self.base / "workspace", registry=self.registry)

    def _commit(self, contents: str) -> str:
        (self.repository / "marker.txt").write_text(contents, encoding="utf-8")
        porcelain.add(str(self.repository), paths=["marker.txt"])
        return porcelain.commit(
            str(self.repository),
            message=contents.encode(),
            author=b"Fixture <fixture@example.invalid>",
            committer=b"Fixture <fixture@example.invalid>",
        ).decode()

    def _submit(self, kind: OperationKind = OperationKind.INSTALL):
        plan = self.application.plan(kind=kind, desired={"fixture": DesiredComponentState()})
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        self.assertTrue(created)
        return plan, operation

    @staticmethod
    def _wait(application: ManagerApplication, operation_id: str):
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            operation = application.store.get_operation(operation_id)
            if operation.state in TERMINAL_OPERATION_STATES:
                return operation
            time.sleep(0.02)
        operation = application.store.get_operation(operation_id)
        raise AssertionError(f"Activation {operation_id} timed out in {operation.state}")

    def test_cancel_after_recovery_rolls_back_interrupted_activation(self) -> None:
        class FixtureStop(BaseException):
            pass

        class InterruptedHandler(FilesystemTaskHandler):
            def _execute_activate_component(self, execution, task):
                super()._execute_activate_component(execution, task)
                raise FixtureStop()

        _plan, submitted = self._submit()
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.registry,
            task_handler=InterruptedHandler(),
        )
        self.addCleanup(engine.shutdown)
        with self.assertRaises(FixtureStop):
            engine._execute(submitted.id)
        active = active_component_path(self.application.context.layout, "fixture")
        self.assertIsNotNone(active)
        assert active is not None
        self.assertTrue(active.is_dir())
        restarted = OperationEngine(
            self.application.context,
            self.application.store,
            self.registry,
        )
        self.addCleanup(restarted.shutdown)
        restarted._recover_interrupted()
        interrupted = next(
            record
            for record in self.application.store.operation_tasks(submitted.id)
            if record.task.kind == "activate_component"
        )
        self.assertEqual(interrupted.state, TaskState.PENDING)
        self.assertEqual(interrupted.attempt, 1)
        self.assertEqual(interrupted.result, {})
        self.assertTrue(self.application.store.request_cancellation(submitted.id))
        restarted._execute(submitted.id)
        self.assertEqual(
            self.application.store.get_operation(submitted.id).state, OperationState.CANCELLED
        )
        self.assertIsNone(active_component_path(self.application.context.layout, "fixture"))
        self.assertFalse(active.exists())
        self.assertEqual(self.application.store.configuration_revision(), 0)
        activation = next(
            record
            for record in self.application.store.operation_tasks(submitted.id)
            if record.task.kind == "activate_component"
        )
        self.assertEqual(activation.state, TaskState.ROLLED_BACK)
        self.assertEqual(activation.result, {})

    def test_restart_retries_activation_after_effects_before_receipt_save(self) -> None:
        class FixtureStop(BaseException):
            pass

        class InterruptedHandler(FilesystemTaskHandler):
            def _execute_activate_component(self, execution, task):
                super()._execute_activate_component(execution, task)
                raise FixtureStop()

        plan, submitted = self._submit()
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.registry,
            task_handler=InterruptedHandler(),
        )
        with self.assertRaises(FixtureStop):
            engine._execute(submitted.id)
        activation = next(item for item in plan.tasks if item.kind == "activate_component")
        record = next(
            item
            for item in self.application.store.operation_tasks(submitted.id)
            if item.task.id == activation.id
        )
        self.assertEqual(TaskState.RUNNING, record.state)
        self.assertEqual({}, record.result)
        original_slot = active_component_path(self.application.context.layout, "fixture")
        self.assertIsNotNone(original_slot)
        assert original_slot is not None
        pointer_bytes = component_pointer(self.application.context.layout, "fixture").read_bytes()
        journal = (
            self.application.context.layout.staging / submitted.id / "fixture" / "activation.json"
        )
        self.assertTrue(journal.is_file())
        forward: list[str] = []

        class ObservingHandler(FilesystemTaskHandler):
            def execute(self, execution, task):
                forward.append(task.kind)
                return super().execute(execution, task)

        reopened = create_application(self.base / "workspace", registry=self.registry)
        restarted = OperationEngine(
            reopened.context, reopened.store, self.registry, task_handler=ObservingHandler()
        )
        restarted.start()
        self.addCleanup(restarted.shutdown, timeout=30)
        completed = self._wait(reopened, submitted.id)
        restarted.shutdown(timeout=30)
        self.assertEqual(OperationState.SUCCEEDED, completed.state)
        self.assertEqual(["activate_component"], forward)
        self.assertEqual(original_slot, active_component_path(reopened.context.layout, "fixture"))
        self.assertEqual("one", (original_slot / "marker.txt").read_text())
        self.assertEqual(
            pointer_bytes, component_pointer(reopened.context.layout, "fixture").read_bytes()
        )
        persisted = next(
            item
            for item in reopened.store.operation_tasks(submitted.id)
            if item.task.id == activation.id
        )
        self.assertEqual(2, persisted.attempt)
        self.assertTrue(persisted.result["created_slot"])
        self.assertEqual(str(original_slot), persisted.result["active_path"])
        self.assertEqual(1, reopened.store.configuration_revision())
        self.assertEqual(1, len(reopened.store.owned_paths()))
        self.assertFalse(journal.exists())

    def test_restart_rolls_back_empty_activation_receipt_from_native_journal(self) -> None:
        _plan, installed = self._submit()
        OperationEngine(self.application.context, self.application.store, self.registry)._execute(
            installed.id
        )
        previous = active_component_path(self.application.context.layout, "fixture")
        self.assertIsNotNone(previous)
        assert previous is not None
        previous_pointer = component_pointer(
            self.application.context.layout, "fixture"
        ).read_bytes()
        ownership_before = self.application.store.owned_paths()
        second_revision = self._commit("two")

        class FixtureStop(BaseException):
            pass

        class InterruptedHandler(FilesystemTaskHandler):
            def _execute_activate_component(self, execution, task):
                super()._execute_activate_component(execution, task)
                raise RuntimeError("fixture activation failure before returning receipt")

            def _rollback_activate_component(self, execution, task, result):
                raise FixtureStop()

        plan, submitted = self._submit(OperationKind.UPDATE)
        activation = next(item for item in plan.tasks if item.kind == "activate_component")
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.registry,
            task_handler=InterruptedHandler(),
        )
        with self.assertRaises(FixtureStop):
            engine._execute(submitted.id)
        interrupted = self.application.store.get_operation(submitted.id)
        self.assertEqual(OperationState.ROLLING_BACK, interrupted.state)
        record = next(
            item
            for item in self.application.store.operation_tasks(submitted.id)
            if item.task.id == activation.id
        )
        self.assertEqual({}, record.result)
        journal = (
            self.application.context.layout.staging / submitted.id / "fixture" / "activation.json"
        )
        payload = json.loads(journal.read_text())
        self.assertEqual(json.loads(previous_pointer), payload["previous_pointer"])
        changed = Path(payload["destination"])
        self.assertEqual(second_revision, changed.name)
        self.assertEqual("two", (changed / "marker.txt").read_text())
        forward: list[str] = []
        rollback_receipts: list[dict] = []

        class ObservingHandler(FilesystemTaskHandler):
            def execute(self, execution, task):
                forward.append(task.kind)
                return super().execute(execution, task)

            def _rollback_activate_component(self, execution, task, result):
                rollback_receipts.append(result)
                return super()._rollback_activate_component(execution, task, result)

        reopened = create_application(self.base / "workspace", registry=self.registry)
        restarted = OperationEngine(
            reopened.context, reopened.store, self.registry, task_handler=ObservingHandler()
        )
        restarted.start()
        self.addCleanup(restarted.shutdown, timeout=30)
        completed = self._wait(reopened, submitted.id)
        restarted.shutdown(timeout=30)
        self.assertEqual(OperationState.FAILED, completed.state)
        self.assertEqual(interrupted.error_code, completed.error_code)
        self.assertEqual(interrupted.error_message, completed.error_message)
        self.assertEqual([], forward)
        self.assertEqual([{}], rollback_receipts)
        self.assertEqual(
            previous_pointer, component_pointer(reopened.context.layout, "fixture").read_bytes()
        )
        self.assertEqual("one", (previous / "marker.txt").read_text())
        self.assertFalse(changed.exists())
        self.assertFalse(journal.exists())
        self.assertEqual(1, reopened.store.configuration_revision())
        self.assertEqual(ownership_before, reopened.store.owned_paths())

    def _assert_redirected_forward_rejected(self, *, component_redirect: bool) -> None:
        outside = self.base / "outside"
        outside.mkdir()
        witness = outside / "user-witness.txt"
        witness.write_text("keep")
        container = self.application.context.layout.service_root("fixture")
        redirected = container if component_redirect else container / "versions"
        redirected.parent.mkdir(parents=True, exist_ok=True)
        redirected.symlink_to(outside, target_is_directory=True)
        _plan, submitted = self._submit()
        OperationEngine(self.application.context, self.application.store, self.registry)._execute(
            submitted.id
        )
        completed = self.application.store.get_operation(submitted.id)
        self.assertEqual(OperationState.FAILED, completed.state)
        self.assertEqual(["user-witness.txt"], sorted(item.name for item in outside.iterdir()))
        self.assertEqual("keep", witness.read_text())
        self.assertEqual(0, self.application.store.configuration_revision())
        self.assertEqual([], self.application.store.owned_paths())
        self.assertFalse(component_pointer(self.application.context.layout, "fixture").exists())

    @unittest.skipIf(os.name == "nt", "Native Unix directory symlink fixture")
    def test_redirected_versions_cannot_activate_outside_workspace(self) -> None:
        self._assert_redirected_forward_rejected(component_redirect=False)

    @unittest.skipIf(os.name == "nt", "Native Unix directory symlink fixture")
    def test_redirected_component_cannot_activate_outside_workspace(self) -> None:
        self._assert_redirected_forward_rejected(component_redirect=True)

    def _assert_redirected_rollback_preserved(self, *, empty_receipt: bool) -> None:
        application = create_application(
            self.base / str(empty_receipt) / "workspace", registry=self.registry
        )
        plan = application.plan(
            kind=OperationKind.INSTALL, desired={"fixture": DesiredComponentState()}
        )
        submitted, _created = application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(item.key for item in plan.confirmations),
            idempotency_key=str(uuid.uuid4()),
        )
        outside = self.base / str(empty_receipt) / "outside"
        foreign_slot = outside / self.first_revision
        foreign_slot.mkdir(parents=True)
        witness = foreign_slot / "user-data.txt"
        witness.write_text("unrelated user data")
        versions = application.context.layout.service_root("fixture") / "versions"
        backup = versions.with_name("versions-before-redirection")
        pointer = component_pointer(application.context.layout, "fixture")
        pointer_before: list[bytes] = []

        def redirect():
            pointer_before.append(pointer.read_bytes())
            versions.rename(backup)
            versions.symlink_to(outside, target_is_directory=True)
            raise RuntimeError("fixture directory redirection after activation")

        class InterruptedHandler(FilesystemTaskHandler):
            def _execute_activate_component(self, execution, task):
                result = super()._execute_activate_component(execution, task)
                if empty_receipt:
                    redirect()
                return result

        def reject_activation(_operation, task, _result):
            if task.kind == "activate_component":
                redirect()

        engine = OperationEngine(
            application.context,
            application.store,
            self.registry,
            task_handler=InterruptedHandler(),
            fault_injector=None if empty_receipt else reject_activation,
        )
        engine._execute(submitted.id)
        completed = application.store.get_operation(submitted.id)
        self.assertEqual("unrelated user data", witness.read_text())
        self.assertEqual(OperationState.RECOVERY_REQUIRED, completed.state)
        self.assertEqual(pointer_before[0], pointer.read_bytes())
        self.assertEqual("one", (backup / self.first_revision / "marker.txt").read_text())
        journal = application.context.layout.staging / submitted.id / "fixture" / "activation.json"
        self.assertTrue(journal.is_file())
        self.assertEqual(0, application.store.configuration_revision())
        self.assertEqual([], application.store.owned_paths())

    @unittest.skipIf(os.name == "nt", "Native Unix directory symlink fixture")
    def test_redirected_rollback_preserves_external_files_pointer_and_journal(self) -> None:
        for empty_receipt in (False, True):
            with self.subTest(empty_receipt=empty_receipt):
                self._assert_redirected_rollback_preserved(empty_receipt=empty_receipt)

    def _assert_live_guard_without_service_key(self, *, available):
        application = self.application
        supervisor = ProcessSupervisor(
            application.context, application.store, manager_instance_id=str(uuid.uuid4())
        )
        self.addCleanup(supervisor.shutdown, stop_children=True)
        processes = []
        slots = []

        class StartedThenFailedHandler(FilesystemTaskHandler):
            def _execute_activate_component(self, execution, task):
                receipt = super()._execute_activate_component(execution, task)
                slots.append(Path(receipt["active_path"]))
                spec = ManagedProcessSpec(
                    service_id="fixture.background",
                    component_id="fixture",
                    label="Fixture background child",
                    executable=sys.executable,
                    arguments=("-c", "import time; time.sleep(60)"),
                    cwd=receipt["active_path"],
                    startup_timeout_seconds=3,
                    shutdown_timeout_seconds=1,
                )
                supervisor.register(spec)
                supervisor.start(spec.service_id)
                processes.append(supervisor._runtime[spec.service_id].process)
                raise RuntimeError("injected activation failure with an owned child")

        self.assertIsNone(self.registry.definition("fixture").service_key)
        _plan, operation = self._submit()
        engine = OperationEngine(
            application.context,
            application.store,
            self.registry,
            supervisor=supervisor if available else None,
            task_handler=StartedThenFailedHandler(),
            lifecycle_lock=application.lifecycle_lock,
        )
        self.addCleanup(engine.shutdown)
        engine._execute(operation.id)
        active = active_component_path(application.context.layout, "fixture")
        self.assertEqual(len(processes), 1)
        assert processes[0] is not None
        measured = {
            "active": str(active),
            "new_slot_exists": slots[0].exists(),
            "actual_child_alive": processes[0].poll() is None,
            "identity_recorded": application.store.list_services()[0].process is not None,
        }
        self.assertEqual(
            application.store.get_operation(operation.id).state,
            OperationState.RECOVERY_REQUIRED,
            measured,
        )
        self.assertIsNotNone(active)
        assert active is not None
        self.assertTrue(active.exists())
        self.assertEqual((active / "marker.txt").read_text(), "one")
        self.assertEqual(application.store.configuration_revision(), 0)
        self.assertTrue((application.context.layout.staging / operation.id).exists())
        self.assertEqual(len(processes), 1)
        assert processes[0] is not None
        self.assertIsNone(processes[0].poll())

    def test_live_child_blocks_slot_rollback_without_catalogue_service_key(self):
        self._assert_live_guard_without_service_key(available=True)

    def test_recorded_child_blocks_slot_rollback_without_catalogue_service_key_or_supervisor(self):
        self._assert_live_guard_without_service_key(available=False)
