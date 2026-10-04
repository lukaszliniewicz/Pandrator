"""Installation failure waits for its own concurrent bootstrap work."""

import concurrent.futures
import queue
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from pandrator_installer.models import InstallSelection
from pandrator_installer.service import HeadlessInstaller


class InstallerBootstrapLifetimeTests(unittest.TestCase):
    def exercise_failure(self, failure_type: type[BaseException]) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "Pandrator"
            (root / "Pandrator").mkdir(parents=True)
            manifest = root / "envs" / "pandrator_installer" / "pixi.toml"
            manifest.parent.mkdir(parents=True)
            manifest.write_text("", encoding="utf-8")
            installer = HeadlessInstaller(working_dir=directory)
            selection = InstallSelection(pandrator=False, kokoro_cpu=True, fishs2_cpu=True)
            entered, release, finished = threading.Event(), threading.Event(), threading.Event()
            observations: queue.Queue[str] = queue.Queue()
            failures: list[BaseException] = []
            executors: list[concurrent.futures.ThreadPoolExecutor] = []
            native_executor = concurrent.futures.ThreadPoolExecutor
            witness = root / "bootstrap-completed.txt"

            class ObservedExecutor(native_executor):
                def shutdown(self, wait: bool = True, *, cancel_futures: bool = False) -> None:
                    observations.put("joining")
                    super().shutdown(wait=wait, cancel_futures=cancel_futures)

            def create_executor(*args, **kwargs):
                executor = ObservedExecutor(*args, **kwargs)
                executors.append(executor)
                return executor

            def bootstrap(*_args, **_kwargs):
                entered.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("Fixture did not release its bootstrap.")
                witness.write_bytes(b"owned bootstrap completed")
                finished.set()

            def fail_component(*_args, **_kwargs):
                if not entered.wait(timeout=5):
                    raise RuntimeError("Fixture bootstrap did not start.")
                raise failure_type("component failed")

            def install():
                try:
                    installer.install_process(selection)
                except BaseException as error:
                    failures.append(error)
                finally:
                    observations.put("returned")

            with ExitStack() as stack:
                for name, kwargs in {
                    "configure_tls_certificates": {},
                    "is_admin": {"return_value": False},
                    "execute_concurrently": {},
                    "check_pixi": {"return_value": True},
                    "get_pixi_executable": {"return_value": str(root / "pixi")},
                    "create_pixi_env": {},
                    "install_kokoro_api_server": {"side_effect": bootstrap},
                    "install_fishs2_api_server": {"side_effect": fail_component},
                }.items():
                    stack.enter_context(patch.object(installer, name, **kwargs))
                stack.enter_context(
                    patch(
                        "pandrator_installer.workflows.concurrent.futures.ThreadPoolExecutor",
                        side_effect=create_executor,
                    )
                )
                worker = threading.Thread(target=install, name="fixture-installation")
                worker.start()
                try:
                    self.assertEqual(observations.get(timeout=5), "joining")
                    self.assertTrue(entered.is_set())
                    self.assertFalse(finished.is_set())
                    self.assertFalse(witness.exists())
                    self.assertTrue(worker.is_alive())
                finally:
                    release.set()
                    worker.join(timeout=5)
                    for executor in executors:
                        executor.shutdown(wait=True)
                    self.assertFalse(worker.is_alive())
                self.assertTrue(finished.is_set())
                self.assertEqual(witness.read_bytes(), b"owned bootstrap completed")
                self.assertEqual(len(failures), 1)
                self.assertIsInstance(failures[0], failure_type)
                self.assertEqual(str(failures[0]), "component failed")
                self.assertEqual(observations.get(timeout=5), "returned")

    def test_component_failure_joins_owned_bootstrap_before_returning(self):
        self.exercise_failure(RuntimeError)

    def test_interrupted_component_joins_owned_bootstrap_before_propagating(self):
        self.exercise_failure(KeyboardInterrupt)


if __name__ == "__main__":
    unittest.main()
