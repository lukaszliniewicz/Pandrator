"""Operation-worker completion and daemon workspace ownership."""

import logging
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from pandrator_manager import daemon
from pandrator_manager.application import create_application
from pandrator_manager.components import ComponentRegistry
from pandrator_manager.operations import OperationEngine


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

    def test_daemon_retains_workspace_until_operation_worker_finishes(self) -> None:
        ready, release, finished, shutdown_entered, daemon_done = (
            threading.Event() for _ in range(5)
        )
        engines: list[OperationEngine] = []
        failures: list[BaseException] = []
        cleanup_completion: list[bool] = []
        layout = self.application.context.layout
        witness = Path(self.temporary.name) / "operation-finished.txt"
        supervisor = mock.Mock()
        supervisor.restore_desired.return_value = {}
        supervisor.shutdown.side_effect = lambda **_kwargs: cleanup_completion.append(
            finished.is_set()
        )
        server = mock.Mock()
        server.effective_port = 12345

        def engine_factory(*args, **kwargs) -> OperationEngine:
            engine = OperationEngine(*args, **kwargs)
            engines.append(engine)
            original_shutdown = engine.shutdown

            def execute(operation_id: str) -> None:
                with engine.lifecycle_lock:
                    ready.set()
                    if not release.wait(5):
                        raise RuntimeError("Fixture release timed out")
                    witness.write_text("finished", encoding="utf-8")
                    finished.set()

            def shutdown(*, timeout: float | None = 0.01) -> bool:
                # Shorten the old bounded default; preserve an explicit full drain.
                shutdown_entered.set()
                return original_shutdown(timeout=timeout)

            engine._execute = execute
            engine.shutdown = shutdown
            return engine

        def run_server() -> None:
            engines[0].enqueue("controlled-action")
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
                for engine in engines:
                    engine.shutdown(timeout=5)
                    thread = engine._thread
                    assert thread is not None
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(cleanup_completion, [True])
        self.assertEqual(witness.read_text(encoding="utf-8"), "finished")
        self.assertFalse(layout.instance_lock.exists())
        self.assertFalse(layout.descriptor.exists())
        successor = daemon.ManagerInstanceLock(layout.instance_lock)
        try:
            successor.acquire()
        finally:
            successor.release()
