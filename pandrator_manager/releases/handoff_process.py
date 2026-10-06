"""Retained process identities owned by a manager handoff launch."""

from __future__ import annotations

import subprocess

import psutil


class LaunchedManagerProcess:
    def __init__(self, process: subprocess.Popen, *, native_launcher: bool = False) -> None:
        self._process = process
        self._native_launcher = native_launcher
        self._root: psutil.Process | None = None
        self._owned: dict[tuple[int, float], psutil.Process] = {}
        self._errors: list[str] = []
        self._observed_child = False
        try:
            if process.poll() is None:
                root = psutil.Process(process.pid)
                identity = self._identity(root)
                # A completed retained handle must never authorize its numeric PID.
                if process.poll() is None:
                    if identity is None:
                        self._remember("The live manager root identity could not be verified.")
                    else:
                        self._root = root
                        self._owned[identity] = root
            self.observe()
        except psutil.NoSuchProcess:
            try:
                self.observe()
            except RuntimeError:
                pass
        except (psutil.AccessDenied, OSError, RuntimeError) as error:
            self._remember(str(error))

    @property
    def pid(self) -> int:
        return self._process.pid

    @property
    def returncode(self) -> int | None:
        return self._process.returncode

    def poll(self) -> int | None:
        return self._process.poll()

    def _remember(self, message: str) -> None:
        if message not in self._errors:
            self._errors.append(message)

    @staticmethod
    def _identity(process: psutil.Process) -> tuple[int, float] | None:
        try:
            if not process.is_running():
                return None
            identity = (process.pid, process.create_time())
            return identity if process.is_running() else None
        except psutil.NoSuchProcess:
            return None

    def _live(self, identity: tuple[int, float], process: psutil.Process) -> bool:
        if process is self._root and self._process.poll() is not None:
            return False
        return self._identity(process) == identity

    def observe(self) -> None:
        try:
            for identity, process in list(self._owned.items()):
                try:
                    if not self._live(identity, process):
                        continue
                    children = process.children(recursive=True)
                    if not self._live(identity, process):
                        continue
                    for child in children:
                        child_identity = self._identity(child)
                        if child_identity is not None:
                            self._owned.setdefault(child_identity, child)
                            self._observed_child = True
                except psutil.NoSuchProcess:
                    continue
                except (psutil.AccessDenied, OSError) as error:
                    self._remember(f"Manager process tree inspection failed: {error}")
            if self._root is None and self._process.poll() is None:
                self._remember("The running manager launch has no verified root identity.")
            if (
                self._native_launcher
                and self._process.poll() is not None
                and not self._observed_child
            ):
                self._remember("The native launcher exited before a descendant was observed.")
        except (psutil.AccessDenied, OSError) as error:
            self._remember(f"Manager process tree inspection failed: {error}")
        except psutil.NoSuchProcess:
            # The cached identity disappeared while its children were inspected.
            pass
        if self._errors:
            raise RuntimeError("Manager launch ownership is unresolved: " + "; ".join(self._errors))

    def belongs_to_launch(self, candidate: psutil.Process) -> bool:
        self.observe()
        try:
            identity = self._identity(candidate)
            if identity is None:
                return False
            recorded = self._owned.get(identity)
            return recorded is not None and self._live(identity, recorded)
        except (psutil.AccessDenied, OSError) as error:
            self._remember(f"Manager process identity inspection failed: {error}")
            raise RuntimeError("Manager launch identity could not be verified.") from error

    def terminate(self) -> None:
        try:
            self.observe()
        except RuntimeError:
            # Stop every identity we can still prove even if completion is uncertain.
            pass
        ordered = list(reversed(list(self._owned.items())))
        live: list[psutil.Process] = []
        for identity, process in ordered:
            try:
                if self._live(identity, process):
                    live.append(process)
                    process.terminate()
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError) as error:
                self._remember(f"Owned manager process termination failed: {error}")
        try:
            _gone, survivors = psutil.wait_procs(live, timeout=10)
        except (psutil.AccessDenied, OSError) as error:
            self._remember(f"Owned manager process wait failed: {error}")
            survivors = live
        survivor_identities = {id(process) for process in survivors}
        killed: list[psutil.Process] = []
        for identity, process in ordered:
            if id(process) not in survivor_identities:
                continue
            try:
                if self._live(identity, process):
                    process.kill()
                    killed.append(process)
            except psutil.NoSuchProcess:
                continue
            except (psutil.AccessDenied, OSError) as error:
                self._remember(f"Owned manager process kill failed: {error}")
        try:
            psutil.wait_procs(killed, timeout=10)
        except (psutil.AccessDenied, OSError) as error:
            self._remember(f"Owned manager process final wait failed: {error}")
        try:
            self._process.wait(timeout=1)
        except (subprocess.TimeoutExpired, OSError) as error:
            self._remember(f"Retained manager root could not be reaped: {error}")
        for identity, process in ordered:
            try:
                if self._live(identity, process):
                    self._remember(f"Owned manager process {identity[0]} is still running.")
            except (psutil.AccessDenied, OSError) as error:
                self._remember(f"Owned manager process exit could not be verified: {error}")
        if self._errors:
            raise RuntimeError("Manager launch shutdown is unresolved: " + "; ".join(self._errors))
