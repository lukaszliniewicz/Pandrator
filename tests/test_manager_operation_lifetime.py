"""Operation-worker completion and daemon workspace ownership."""

import logging
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast
from unittest import mock

from dulwich import porcelain
from waitress import wasyncore
from waitress.adjustments import Adjustments
from waitress.channel import HTTPChannel
from waitress.task import Task, ThreadedTaskDispatcher

from pandrator_manager import daemon
from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.components import ComponentRegistry
from pandrator_manager.components.slots import active_component_path
from pandrator_manager.models import (
    DesiredComponentState,
    ManagedProcessSpec,
    ManagedService,
    OperationKind,
    OperationRecord,
    OperationState,
    TaskSpec,
    TaskState,
)
from pandrator_manager.operations import OperationEngine
from pandrator_manager.supervisor import ProcessSupervisor
from tests.test_manager_operations import _commit, _registry, _wait


class _BackpressureChannel(Protocol):
    """The native Waitress testing hook is absent from its public stubs."""

    def _flush_outbufs_below_high_watermark(self) -> None: ...


class OperationLifetimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.application = create_application(self.temporary.name, registry=ComponentRegistry())
        self.engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            lifecycle_lock=self.application.lifecycle_lock,
        )

    def _join_worker(self) -> None:
        thread = self.engine._thread
        assert thread is not None
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive())

    def test_timed_out_shutdown_retains_pending_work_for_restart(self) -> None:
        ready, release, second = (threading.Event() for _ in range(3))
        calls: list[str] = []

        def execute(operation_id: str) -> None:
            with self.engine.lifecycle_lock:
                calls.append(operation_id)
                if operation_id == "first":
                    ready.set()
                    if not release.wait(5):
                        raise RuntimeError("Fixture release timed out")
                else:
                    second.set()

        with mock.patch.object(self.engine, "_execute", side_effect=execute):
            try:
                self.engine.start()
                self.engine.enqueue("first")
                self.assertTrue(ready.wait(5))
                self.assertFalse(self.engine.shutdown(timeout=0.01))
                original_worker = self.engine._thread
                with mock.patch.object(self.engine, "_recover_interrupted") as recover:
                    self.engine.start()
                self.assertIs(self.engine._thread, original_worker)
                recover.assert_not_called()
                # Keep pending work and its deduplication intact across restart.
                self.engine.enqueue("second")
                self.engine.enqueue("second")
                release.set()
                self._join_worker()
                self.engine.start()
                self.assertTrue(second.wait(5))
                self.assertTrue(self.engine.shutdown(timeout=5))
                self.assertEqual(calls, ["first", "second"])
            finally:
                release.set()
                self.engine.shutdown(timeout=5)
                self._join_worker()

    def test_unbounded_shutdown_waits_for_operation_completion(self) -> None:
        ready, release, finished, shutdown_entered, shutdown_done = (
            threading.Event() for _ in range(5)
        )
        results: list[bool] = []

        def execute(_operation_id: str) -> None:
            ready.set()
            if not release.wait(5):
                raise RuntimeError("Fixture release timed out")
            finished.set()

        def stop() -> None:
            shutdown_entered.set()
            results.append(self.engine.shutdown(timeout=None))
            shutdown_done.set()

        with mock.patch.object(self.engine, "_execute", side_effect=execute):
            stopper = threading.Thread(target=stop)
            try:
                self.engine.start()
                self.engine.enqueue("first")
                self.assertTrue(ready.wait(5))
                stopper.start()
                self.assertTrue(shutdown_entered.wait(5))
                self.assertFalse(shutdown_done.wait(0.05))
                release.set()
                self.assertTrue(shutdown_done.wait(5))
                self.assertTrue(finished.is_set())
                self.assertEqual(results, [True])
            finally:
                release.set()
                if stopper.ident is not None:
                    stopper.join(timeout=5)
                self.assertFalse(stopper.is_alive())
                self.engine.shutdown(timeout=5)
                self._join_worker()

    def test_worker_can_request_shutdown_without_joining_itself(self) -> None:
        results: list[bool] = []

        def execute(_operation_id: str) -> None:
            results.append(self.engine.shutdown(timeout=None))

        with mock.patch.object(self.engine, "_execute", side_effect=execute):
            try:
                self.engine.start()
                self.engine.enqueue("first")
                self._join_worker()
                self.assertEqual(results, [False])
                self.assertTrue(self.engine.shutdown(timeout=5))
            finally:
                self.engine.shutdown(timeout=5)
                self._join_worker()

    def test_idle_and_never_started_shutdown_report_completion(self) -> None:
        self.assertTrue(self.engine.shutdown(timeout=0))
        self.engine.start()
        self.assertTrue(self.engine.shutdown(timeout=5))
        self._join_worker()

    def test_shutdown_closes_event_stream_without_waiting_for_heartbeat(self) -> None:
        stop = threading.Event()
        self.application.context.event_sink.emit("fixture.ready", {})
        api = create_api(
            self.application,
            mock.Mock(),
            client_secret="fixture-secret",
            shutdown_event=stop,
        )
        with api.test_client() as client:
            response = client.get(
                "/v1/events",
                headers={"Authorization": "Bearer fixture-secret"},
                buffered=False,
            )
            try:
                self.assertEqual(response.status_code, 200)
                chunks = iter(response.response)
                self.assertIn(b"fixture.ready", next(chunks))
                stop.set()
                with self.assertRaises(StopIteration):
                    next(chunks)
            finally:
                stop.set()
                response.close()

    def test_channel_close_wakes_a_backpressured_response_writer(self) -> None:
        ready = threading.Event()
        channels: dict[int, wasyncore.dispatcher] = {}
        server = mock.Mock()
        server.active_channels = {}
        server.pull_trigger.side_effect = ready.set
        left, right = socket.socketpair()
        # Waitress's map annotation describes sockets, while runtime stores channels.
        channel = HTTPChannel(
            server,
            left,
            ("127.0.0.1", 0),
            Adjustments(outbuf_high_watermark=1),
            map=cast(dict[int, socket.socket], channels),
        )
        channel.outbufs[0].append(b"held-response")
        channel.total_outbufs_len = len(b"held-response")
        with mock.patch.object(channel, "_flush_some", return_value=False):
            # This native private hook is absent from Waitress's public stubs.
            flush = cast(_BackpressureChannel, channel)._flush_outbufs_below_high_watermark
            writer = threading.Thread(target=flush)
            try:
                writer.start()
                self.assertTrue(ready.wait(5))
                close = cast(
                    Callable[[dict[int, wasyncore.dispatcher]], None],
                    getattr(daemon, "_close_api_connections", wasyncore.close_all),
                )
                close(channels)
                writer.join(timeout=1)
                self.assertFalse(writer.is_alive())
                self.assertFalse(channel.connected)
            finally:
                channel.handle_close()
                writer.join(timeout=5)
                right.close()
                self.assertFalse(writer.is_alive())

    def test_daemon_retains_workspace_until_operation_worker_finishes(self) -> None:
        self._assert_daemon_retains_workspace("operation")

    def test_startup_restoration_waits_for_recovered_component_update(self) -> None:
        base = Path(self.temporary.name)
        repository = base / "origin"
        porcelain.init(str(repository))
        _commit(repository, "one")
        workspace = base / "component-workspace"
        setup = create_application(workspace, registry=_registry(repository))

        def submit(application, kind: OperationKind) -> OperationRecord:
            plan = application.plan(kind=kind, desired={"fixture": DesiredComponentState()})
            operation, created = application.submit_operation(
                plan_id=plan.id,
                plan_digest=plan.digest,
                accepted_confirmations=tuple(item.key for item in plan.confirmations),
                idempotency_key=str(uuid.uuid4()),
            )
            self.assertTrue(created)
            return operation

        installed = submit(setup, OperationKind.INSTALL)
        OperationEngine(setup.context, setup.store, setup.registry)._execute(installed.id)
        self.assertEqual(setup.store.get_operation(installed.id).state, OperationState.SUCCEEDED)
        application = create_application(
            workspace, registry=_registry(repository, service_key="fixture.service")
        )

        def spec(component_id: str, _resolved=None) -> ManagedProcessSpec:
            active = active_component_path(application.context.layout, component_id)
            assert active is not None
            return ManagedProcessSpec(
                service_id="fixture.service",
                component_id=component_id,
                label="Disposable service",
                executable=sys.executable,
                arguments=("-c", "import time; time.sleep(60)"),
                cwd=str(active),
                startup_timeout_seconds=3,
                shutdown_timeout_seconds=1,
            )

        old_spec = spec("fixture")
        # An interrupted update whose desired service exited before this daemon
        # registered it. Adoption must not conceal a background restoration start.
        application.store.save_service(
            ManagedService(
                id="fixture.service",
                component_id="fixture",
                service_key="fixture.service",
                desired_running=True,
            )
        )
        _commit(repository, "two")
        updated = submit(application, OperationKind.UPDATE)
        stop_task = next(
            task
            for task in application.store.operation_tasks(updated.id)
            if task.task.kind == "stop_service"
        )
        updated.state = OperationState.RUNNING
        updated.current_task_id = stop_task.task.id
        application.store.update_operation(updated)
        application.store.update_operation_task(
            updated.id,
            stop_task.task.id,
            state=TaskState.RUNNING,
            attempt=1,
        )
        application = create_application(
            workspace, registry=_registry(repository, service_key="fixture.service")
        )
        before_stop, allow_stop, after_stop, allow_operation, selected, allow_start, restored = (
            threading.Event() for _ in range(7)
        )
        engines: list[OperationEngine] = []
        failures: list[BaseException] = []
        children: list[subprocess.Popen] = []

        class ControlledSupervisor(ProcessSupervisor):
            def stop(self, service_id: str) -> ManagedService:
                if (
                    threading.current_thread().name == "pandrator-manager-operations"
                    and not after_stop.is_set()
                ):
                    before_stop.set()
                    if not allow_stop.wait(5):
                        raise RuntimeError("Fixture stop gate expired")
                return super().stop(service_id)

            def start(self, service_id: str) -> ManagedService:
                if threading.current_thread().name == "manager-restore-desired-services":
                    selected.set()
                    if not allow_start.wait(5):
                        raise RuntimeError("Fixture restoration gate expired")
                service = super().start(service_id)
                process = self._runtime[service_id].process
                if process is not None and process not in children:
                    children.append(process)
                return service

            def restore_desired(self) -> dict[str, str]:
                try:
                    return super().restore_desired()
                finally:
                    restored.set()

        supervisor = ControlledSupervisor(
            application.context,
            application.store,
            manager_instance_id="startup-fixture",
        )

        def factory(*args, **kwargs) -> OperationEngine:
            kwargs["service_spec_factory"] = spec

            def fault(_operation: OperationRecord, task: TaskSpec, _result: dict) -> None:
                if task.kind == "stop_service":
                    after_stop.set()
                    if not allow_operation.wait(5):
                        raise RuntimeError("Fixture operation gate expired")

            kwargs["fault_injector"] = fault
            engine = OperationEngine(*args, **kwargs)
            engines.append(engine)
            original_start = engine.start

            def start() -> None:
                original_start()
                if not before_stop.wait(5):
                    raise RuntimeError("Recovered update did not reach its stop task")

            engine.start = start
            return engine

        server = mock.Mock()
        server.effective_port = 12345
        server._map = {}

        def run_server() -> None:
            if not allow_operation.wait(5):
                raise RuntimeError("Fixture server gate expired")
            _wait(application, updated.id)
            if not restored.wait(5):
                raise RuntimeError("Fixture restoration did not complete")

        server.run.side_effect = run_server

        def run() -> None:
            try:
                daemon.run_daemon(workspace, register_silero=False, handoff_child="fixture")
            except BaseException as error:
                failures.append(error)

        with (
            mock.patch.object(daemon, "create_application", return_value=application),
            mock.patch.object(daemon, "ProcessSupervisor", return_value=supervisor),
            mock.patch.object(daemon, "OperationEngine", side_effect=factory),
            mock.patch.object(daemon, "create_api", return_value=object()),
            mock.patch.object(daemon, "create_server", return_value=server),
            mock.patch.object(daemon, "pandrator_runtime_specs", return_value=()),
            mock.patch.object(
                daemon, "installed_component_runtime_specs", return_value=(old_spec,)
            ),
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(daemon.logging, "basicConfig"),
            mock.patch.object(daemon, "RotatingFileHandler", return_value=logging.NullHandler()),
        ):
            worker = threading.Thread(target=run)
            try:
                worker.start()
                self.assertTrue(before_stop.wait(5))
                # Give the old implementation time to snapshot True before stop.
                selected_before_stop = selected.wait(0.1)
                allow_stop.set()
                self.assertTrue(after_stop.wait(5))
                allow_start.set()
                restored_during_maintenance = restored.wait(0.1)
                acquired = application.lifecycle_lock.acquire(blocking=False)
                if acquired:
                    application.lifecycle_lock.release()
                self.assertFalse(acquired)
                self.assertFalse(selected_before_stop)
                self.assertFalse(restored_during_maintenance)
                self.assertFalse(application.store.list_services()[0].desired_running)
                self.assertEqual(supervisor._runtime, {})
                allow_operation.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                self.assertTrue(restored.is_set())
                self.assertEqual(
                    application.store.get_operation(updated.id).state, OperationState.SUCCEEDED
                )
                recovered_stop = next(
                    task
                    for task in application.store.operation_tasks(updated.id)
                    if task.task.kind == "stop_service"
                )
                self.assertEqual(recovered_stop.attempt, 2)
                self.assertTrue(recovered_stop.result["desired_running"])
                self.assertEqual(application.store.configuration_revision(), 2)
                active = active_component_path(application.context.layout, "fixture")
                assert active is not None
                self.assertEqual((active / "marker.txt").read_text(), "two")
                self.assertEqual(supervisor.spec("fixture.service"), spec("fixture"))
                self.assertEqual(len(children), 1)
                self.assertIsNone(children[0].poll())
            finally:
                allow_stop.set()
                allow_start.set()
                allow_operation.set()
                worker.join(timeout=10)
                self.assertFalse(worker.is_alive())
                for engine in engines:
                    self.assertTrue(engine.shutdown(timeout=5))
                supervisor.shutdown(stop_children=True)
                for process in children:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                reaper_names = {f"pandrator-service-reaper-{process.pid}" for process in children}
                for thread in threading.enumerate():
                    if thread.name in reaper_names:
                        thread.join(timeout=5)
                        self.assertFalse(thread.is_alive())

    def test_daemon_retains_workspace_until_restoration_finishes(self) -> None:
        self._assert_daemon_retains_workspace("restoration")

    def test_shutdown_skips_restoration_waiting_for_maintenance(self) -> None:
        ready, release, shutdown_entered = (threading.Event() for _ in range(3))
        engines: list[OperationEngine] = []
        failures: list[BaseException] = []
        supervisor = mock.Mock()
        supervisor.restore_desired.return_value = {}
        server = mock.Mock()
        server.effective_port = 12345
        server._map = {}

        def factory(*args, **kwargs) -> OperationEngine:
            engine = OperationEngine(*args, **kwargs)
            engines.append(engine)
            original_start, original_shutdown = engine.start, engine.shutdown

            def execute(operation_id: str) -> None:
                with engine.lifecycle_lock:
                    ready.set()
                    if not release.wait(5):
                        raise RuntimeError("Fixture maintenance release expired")

            def start() -> None:
                original_start()
                engine.enqueue("controlled-maintenance")
                if not ready.wait(5):
                    raise RuntimeError("Fixture maintenance did not start")

            def shutdown(*, timeout: float | None = 5) -> bool:
                shutdown_entered.set()
                return original_shutdown(timeout=timeout)

            engine._execute = execute
            engine.start = start
            engine.shutdown = shutdown
            return engine

        def run() -> None:
            try:
                daemon.run_daemon(
                    self.temporary.name, register_silero=False, handoff_child="fixture"
                )
            except BaseException as error:
                failures.append(error)

        with (
            mock.patch.object(daemon, "create_application", return_value=self.application),
            mock.patch.object(daemon, "ProcessSupervisor", return_value=supervisor),
            mock.patch.object(daemon, "OperationEngine", side_effect=factory),
            mock.patch.object(daemon, "create_api", return_value=object()),
            mock.patch.object(daemon, "create_server", return_value=server),
            mock.patch.object(daemon, "pandrator_runtime_specs", return_value=()),
            mock.patch.object(daemon, "installed_component_runtime_specs", return_value=()),
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(daemon.logging, "basicConfig"),
            mock.patch.object(daemon, "RotatingFileHandler", return_value=logging.NullHandler()),
        ):
            worker = threading.Thread(target=run)
            try:
                worker.start()
                self.assertTrue(shutdown_entered.wait(5))
                supervisor.restore_desired.assert_not_called()
                release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                self.assertEqual(failures, [])
                supervisor.restore_desired.assert_not_called()
            finally:
                release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                for engine in engines:
                    self.assertTrue(engine.shutdown(timeout=5))

    def test_daemon_retains_workspace_until_request_handlers_finish(self) -> None:
        self._assert_daemon_retains_workspace("request")

    def test_failed_request_drain_does_not_retire_supervisor_or_ownership(self) -> None:
        self._assert_daemon_retains_workspace("request", fail_drain=True)

    def _assert_daemon_retains_workspace(self, owner: str, *, fail_drain: bool = False) -> None:
        ready, release, finished, shutdown_entered, daemon_done = (
            threading.Event() for _ in range(5)
        )
        engines: list[OperationEngine] = []
        action_threads: list[threading.Thread] = []
        failures: list[BaseException] = []
        cleanup_completion: list[bool] = []
        layout = self.application.context.layout
        witness = Path(self.temporary.name) / "operation-finished.txt"
        supervisor = mock.Mock()
        supervisor.shutdown.side_effect = lambda **_kwargs: cleanup_completion.append(
            finished.is_set()
        )
        server = mock.Mock()
        server.effective_port = 12345
        server._map = {}

        def action() -> None:
            action_threads.append(threading.current_thread())
            ready.set()
            if not release.wait(5):
                raise RuntimeError("Fixture release timed out")
            witness.write_text("finished", encoding="utf-8")
            finished.set()

        def restore() -> dict[str, str]:
            if owner == "restoration":
                action()
            return {}

        supervisor.restore_desired.side_effect = restore
        dispatcher = ThreadedTaskDispatcher() if owner == "request" else None
        # Waitress is untyped: default5 infers int, but native deadline accepts floats.
        original_dispatcher_shutdown = (
            cast(Callable[[bool, float], bool], dispatcher.shutdown)
            if dispatcher is not None
            else None
        )
        canceled: list[bool] = []

        class ControlledTask(Task):
            def __init__(self) -> None:
                # This transport-free task exercises only dispatcher ownership.
                pass

            def service(self) -> None:
                action()

            def cancel(self) -> None:
                canceled.append(True)

        if dispatcher is not None:
            assert original_dispatcher_shutdown is not None

            def drain(cancel_pending: bool = True, timeout: float = 5) -> bool:
                if fail_drain:
                    raise RuntimeError("Fixture request drain failed")
                # Accelerate only the old finite timeout, exercising real workers.
                assert original_dispatcher_shutdown is not None
                return original_dispatcher_shutdown(
                    cancel_pending, 0.01 if timeout == 5 else timeout
                )

            dispatcher.shutdown = drain
            server.task_dispatcher = dispatcher

        def engine_factory(*args, **kwargs) -> OperationEngine:
            engine = OperationEngine(*args, **kwargs)
            engines.append(engine)
            original_shutdown = engine.shutdown

            def execute(operation_id: str) -> None:
                with engine.lifecycle_lock:
                    action()

            def shutdown(*, timeout: float | None = 0.01) -> bool:
                # Shorten the old bounded default; preserve an explicit full drain.
                shutdown_entered.set()
                return original_shutdown(timeout=timeout)

            engine._execute = execute
            engine.shutdown = shutdown
            return engine

        def run_server() -> None:
            if owner == "operation":
                engines[0].enqueue("controlled-action")
            if dispatcher is not None:
                dispatcher.set_thread_count(1)
                dispatcher.add_task(ControlledTask())
            if not ready.wait(5):
                raise RuntimeError("Fixture operation did not start")

        server.run.side_effect = run_server

        def run() -> None:
            try:
                daemon.run_daemon(
                    self.temporary.name,
                    register_silero=False,
                    handoff_child="fixture",
                )
            except BaseException as error:
                failures.append(error)
            finally:
                daemon_done.set()

        with (
            mock.patch.object(daemon, "create_application", return_value=self.application),
            mock.patch.object(daemon, "ProcessSupervisor", return_value=supervisor),
            mock.patch.object(daemon, "OperationEngine", side_effect=engine_factory),
            mock.patch.object(daemon, "create_api", return_value=object()),
            mock.patch.object(daemon, "create_server", return_value=server),
            mock.patch.object(daemon, "pandrator_runtime_specs", return_value=()),
            mock.patch.object(daemon, "installed_component_runtime_specs", return_value=()),
            mock.patch.object(daemon.signal, "signal"),
            mock.patch.object(daemon.logging, "basicConfig"),
            mock.patch.object(daemon, "RotatingFileHandler", return_value=logging.NullHandler()),
            mock.patch.dict(daemon.os.environ, {"PANDRATOR_OWNER_PASSWORD": ""}),
        ):
            worker = threading.Thread(target=run)
            try:
                worker.start()
                self.assertTrue(shutdown_entered.wait(5))
                if fail_drain:
                    self.assertTrue(daemon_done.wait(5))
                else:
                    self.assertFalse(daemon_done.wait(0.1))
                self.assertTrue(layout.instance_lock.is_file())
                self.assertTrue(layout.descriptor.is_file())
                self.assertFalse(witness.exists())
                contender = daemon.ManagerInstanceLock(layout.instance_lock)
                with self.assertRaises(daemon.ManagerAlreadyRunning):
                    contender.acquire()
                self.assertEqual(cleanup_completion, [])
                release.set()
                self.assertTrue(daemon_done.wait(5))
            finally:
                release.set()
                worker.join(timeout=5)
                self.assertFalse(worker.is_alive())
                if dispatcher is not None:
                    assert original_dispatcher_shutdown is not None
                    original_dispatcher_shutdown(True, 5)
                    with dispatcher.lock:
                        self.assertFalse(dispatcher.threads)
                for thread in action_threads:
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
                for engine in engines:
                    engine.shutdown(timeout=5)
                    thread = engine._thread
                    assert thread is not None
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(canceled, [])
        self.assertEqual(witness.read_text(encoding="utf-8"), "finished")
        if fail_drain:
            self.assertEqual(len(failures), 1)
            self.assertEqual(str(failures[0]), "Fixture request drain failed")
            self.assertEqual(cleanup_completion, [])
            server.close.assert_not_called()
            self.assertTrue(layout.instance_lock.exists())
            self.assertTrue(layout.descriptor.exists())
            return
        self.assertEqual(failures, [])
        self.assertEqual(cleanup_completion, [True])
        self.assertFalse(layout.instance_lock.exists())
        self.assertFalse(layout.descriptor.exists())
        successor = daemon.ManagerInstanceLock(layout.instance_lock)
        try:
            successor.acquire()
        finally:
            successor.release()
