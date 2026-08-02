"""Typed process supervision with durable identity and bounded restart policy."""

from __future__ import annotations

import json
import logging
import os
import socket
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import urlopen

import psutil

from ..context import ManagerContext
from ..models import (
    HealthResult,
    HealthState,
    ManagedProcessSpec,
    ManagedService,
    ProcessIdentity,
)
from ..processes.identity import (
    IdentityInspectionFailed,
    IdentityMismatch,
    capture_identity,
    validate_identity,
)
from ..processes.runner import CommandRunner
from ..state import ManagerStore


@dataclass(slots=True)
class _RuntimeProcess:
    spec: ManagedProcessSpec
    identity: ProcessIdentity
    process: subprocess.Popen | None
    log_handle: BinaryIO | None
    started_monotonic: float
    restart_count: int = 0
    health_failures: int = 0


@dataclass(slots=True)
class _PendingRestart:
    spec: ManagedProcessSpec
    restart_count: int
    due_monotonic: float


class ProcessSupervisor:
    def __init__(
        self,
        context: ManagerContext,
        store: ManagerStore,
        *,
        manager_instance_id: str,
        monitor_interval_seconds: float = 1.0,
    ) -> None:
        self.context = context
        self.store = store
        self.manager_instance_id = manager_instance_id
        self.monitor_interval_seconds = max(0.1, float(monitor_interval_seconds))
        self._specs: dict[str, ManagedProcessSpec] = {}
        self._runtime: dict[str, _RuntimeProcess] = {}
        self._pending: dict[str, _PendingRestart] = {}
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._monitor_thread: threading.Thread | None = None
        self._environment_builder = CommandRunner(
            cancellation=context.cancellation,
            base_environment=context.environment,
        )

    def register(self, spec: ManagedProcessSpec) -> None:
        with self._lock:
            if spec.service_id in self._specs:
                raise ValueError(f"Duplicate managed service ID: {spec.service_id}")
            occupied = {
                port: service_id
                for service_id, existing in self._specs.items()
                for port in existing.ports
            }
            for port in spec.ports:
                if port in occupied:
                    raise ValueError(
                        f"Services {occupied[port]} and {spec.service_id} both "
                        f"declare port {port}."
                    )
            self._specs[spec.service_id] = spec
            self._validate_graph()
            self._adopt_if_owned(spec)

    def register_many(self, specs: tuple[ManagedProcessSpec, ...]) -> None:
        for spec in specs:
            self.register(spec)

    def spec(self, service_id: str) -> ManagedProcessSpec | None:
        """Return the immutable launch contract currently registered."""

        with self._lock:
            selected = self._specs.get(service_id)
            return selected.model_copy(deep=True) if selected is not None else None

    def replace_spec(
        self,
        spec: ManagedProcessSpec,
    ) -> ManagedProcessSpec | None:
        with self._lock:
            if spec.service_id in self._runtime:
                raise RuntimeError(
                    f"Cannot replace running service specification {spec.service_id}."
                )
            previous = self._specs.pop(spec.service_id, None)
            try:
                occupied = {
                    port: service_id
                    for service_id, existing in self._specs.items()
                    for port in existing.ports
                }
                for port in spec.ports:
                    if port in occupied:
                        raise ValueError(
                            f"Services {occupied[port]} and {spec.service_id} "
                            f"both declare port {port}."
                        )
                self._specs[spec.service_id] = spec
                self._validate_graph()
                self.validate_complete()
            except Exception:
                self._specs.pop(spec.service_id, None)
                if previous is not None:
                    self._specs[previous.service_id] = previous
                raise
            return previous

    def unregister(self, service_id: str) -> ManagedProcessSpec | None:
        with self._lock:
            if service_id in self._runtime or service_id in self._pending:
                raise RuntimeError(
                    f"Cannot unregister active service specification {service_id}."
                )
            previous = self._specs.pop(service_id, None)
            try:
                self.validate_complete()
            except Exception:
                if previous is not None:
                    self._specs[service_id] = previous
                raise
            return previous

    def _validate_graph(self) -> None:
        for spec in self._specs.values():
            unknown = set(spec.dependencies).difference(self._specs)
            if unknown:
                # Registration can be incremental; only reject unknown
                # dependencies once all earlier specs should exist.
                continue
        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(service_id: str) -> None:
            if service_id in visited:
                return
            if service_id in visiting:
                raise ValueError(f"Managed service dependency cycle includes {service_id}.")
            visiting.add(service_id)
            for dependency in self._specs[service_id].dependencies:
                if dependency in self._specs:
                    visit(dependency)
            visiting.remove(service_id)
            visited.add(service_id)

        for service_id in self._specs:
            visit(service_id)

    def validate_complete(self) -> None:
        unknown = {
            dependency
            for spec in self._specs.values()
            for dependency in spec.dependencies
            if dependency not in self._specs
        }
        if unknown:
            raise ValueError(
                "Managed service dependencies are not registered: "
                + ", ".join(sorted(unknown))
            )

    def _adopt_if_owned(self, spec: ManagedProcessSpec) -> None:
        existing = next(
            (
                service
                for service in self.store.list_services()
                if service.id == spec.service_id and service.process is not None
            ),
            None,
        )
        if existing is None:
            return
        try:
            process = validate_identity(existing.process)
        except (IdentityMismatch, IdentityInspectionFailed):
            return
        if process is None:
            return
        # A service from a previous manager instance remains owned only through
        # its full recorded identity; assign the new instance on adoption.
        adopted_identity = ProcessIdentity(
            pid=existing.process.pid,
            create_time=existing.process.create_time,
            executable=existing.process.executable,
            manager_instance_id=self.manager_instance_id,
        )
        self._runtime[spec.service_id] = _RuntimeProcess(
            spec=spec,
            identity=adopted_identity,
            process=None,
            log_handle=None,
            started_monotonic=time.monotonic(),
            restart_count=existing.restart_count,
        )
        existing.process = adopted_identity
        existing.desired_running = True
        self.store.save_service(existing)

    @staticmethod
    def _port_available(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.25)
            return probe.connect_ex(("127.0.0.1", port)) != 0

    def _check_ports(self, spec: ManagedProcessSpec) -> None:
        for port in spec.ports:
            if not self._port_available(port):
                raise RuntimeError(
                    f"{spec.label} cannot start because port {port} is in use "
                    "by an unrecognized process."
                )

    @staticmethod
    def _rotate_log(path: Path, *, maximum_bytes: int = 10 * 1024 * 1024) -> None:
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        if size < maximum_bytes:
            return
        backup = path.with_suffix(path.suffix + ".1")
        try:
            backup.unlink()
        except FileNotFoundError:
            pass
        os.replace(path, backup)

    @staticmethod
    def _popen_options() -> dict:
        if os.name == "nt":
            return {
                "creationflags": (
                    subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_NO_WINDOW
                )
            }
        return {"start_new_session": True}

    def _health(self, spec: ManagedProcessSpec) -> HealthResult:
        probe = spec.readiness
        if probe.kind == "none":
            return HealthResult(
                state=HealthState.HEALTHY,
                service_id=spec.service_id,
            )
        if probe.kind == "tcp":
            try:
                with socket.create_connection(
                    (probe.host, int(probe.port)),
                    timeout=probe.timeout_seconds,
                ):
                    state = HealthState.HEALTHY
            except OSError:
                state = HealthState.UNHEALTHY
            return HealthResult(state=state, service_id=spec.service_id)

        try:
            with urlopen(probe.url, timeout=probe.timeout_seconds) as response:
                if not 200 <= int(response.status) < 300:
                    raise ValueError("Health endpoint did not return success.")
                payload = json.loads(response.read().decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("Health payload must be a JSON object.")
            if (
                probe.expected_service
                and payload.get("service") != probe.expected_service
            ):
                raise ValueError("Health payload identifies another service.")
            protocol = payload.get("protocol_version") or payload.get("api_version")
            if probe.expected_protocol and protocol != probe.expected_protocol:
                raise ValueError("Health payload reports an incompatible protocol.")
            mismatches = {
                key: {"expected": expected, "actual": payload.get(key)}
                for key, expected in probe.expected_json.items()
                if payload.get(key) != expected
            }
            if mismatches:
                raise ValueError(
                    "Health payload does not match the expected contract: "
                    + ", ".join(sorted(mismatches))
                )
            return HealthResult(
                state=HealthState.HEALTHY,
                service_id=spec.service_id,
                protocol_version=str(protocol) if protocol is not None else None,
                details={
                    key: payload[key]
                    for key in (
                        "status",
                        "service",
                        "version",
                        "protocol_version",
                        *probe.expected_json,
                    )
                    if key in payload
                },
            )
        except (URLError, OSError, ValueError, TypeError, json.JSONDecodeError) as error:
            return HealthResult(
                state=HealthState.UNHEALTHY,
                service_id=spec.service_id,
                message=str(error),
            )

    def _service_snapshot(
        self,
        runtime: _RuntimeProcess,
        health: HealthResult,
        *,
        desired_running: bool = True,
    ) -> ManagedService:
        port = runtime.spec.ports[0] if runtime.spec.ports else None
        endpoint = (
            f"http://127.0.0.1:{port}"
            if port is not None
            else None
        )
        return ManagedService(
            id=runtime.spec.service_id,
            component_id=runtime.spec.component_id,
            service_key=runtime.spec.service_id,
            desired_running=desired_running,
            endpoint=endpoint,
            port=port,
            health=health,
            process=runtime.identity,
            restart_count=runtime.restart_count,
        )

    def _start_one(
        self,
        spec: ManagedProcessSpec,
        *,
        restart_count: int = 0,
    ) -> ManagedService:
        self._check_ports(spec)
        log_path = self.context.layout.logs / "services" / f"{spec.service_id}.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        self._rotate_log(log_path)
        log_handle = log_path.open("ab", buffering=0)
        environment = self._environment_builder.environment(spec.environment)
        environment["PANDRATOR_MANAGER_INSTANCE"] = self.manager_instance_id
        process = None
        try:
            process = subprocess.Popen(
                list(spec.argv),
                cwd=spec.cwd,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                shell=False,
                **self._popen_options(),
            )
            identity = capture_identity(
                psutil.Process(process.pid),
                manager_instance_id=self.manager_instance_id,
            )
        except Exception:
            if process is not None and process.poll() is None:
                CommandRunner.terminate_tree(process)
            log_handle.close()
            raise
        runtime = _RuntimeProcess(
            spec=spec,
            identity=identity,
            process=process,
            log_handle=log_handle,
            started_monotonic=time.monotonic(),
            restart_count=restart_count,
        )
        self._runtime[spec.service_id] = runtime
        self.context.event_sink.emit(
            "service.starting",
            {"service_id": spec.service_id, "pid": identity.pid},
            component_id=spec.component_id,
            service_id=spec.service_id,
        )
        deadline = time.monotonic() + spec.startup_timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            health = self._health(spec)
            if health.state == HealthState.HEALTHY:
                service = self._service_snapshot(runtime, health)
                self.store.save_service(service)
                self.context.event_sink.emit(
                    "service.healthy",
                    {"service_id": spec.service_id, "pid": identity.pid},
                    component_id=spec.component_id,
                    service_id=spec.service_id,
                )
                return service
            time.sleep(0.2)
        self._terminate(runtime)
        self._runtime.pop(spec.service_id, None)
        raise RuntimeError(
            f"{spec.label} did not become healthy before its startup timeout. "
            f"See {log_path}."
        )

    def start(self, service_id: str) -> ManagedService:
        with self._lock:
            self.validate_complete()
            if service_id in self._runtime:
                runtime = self._runtime[service_id]
                health = self._health(runtime.spec)
                return self._service_snapshot(runtime, health)
            try:
                spec = self._specs[service_id]
            except KeyError:
                raise KeyError(f"Unknown managed service: {service_id}") from None
            for dependency in spec.dependencies:
                self.start(dependency)
            self._pending.pop(service_id, None)
            return self._start_one(spec)

    def _terminate(self, runtime: _RuntimeProcess) -> None:
        try:
            parent = validate_identity(runtime.identity)
        except (IdentityMismatch, IdentityInspectionFailed) as error:
            raise RuntimeError(
                f"Refusing to stop unverifiable PID {runtime.identity.pid}."
            ) from error
        if parent is None:
            if runtime.log_handle:
                runtime.log_handle.close()
            return
        processes = [*parent.children(recursive=True), parent]
        for process in reversed(processes):
            try:
                process.terminate()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        _, alive = psutil.wait_procs(
            processes,
            timeout=runtime.spec.shutdown_timeout_seconds,
        )
        for process in alive:
            try:
                process.kill()
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                pass
        if alive:
            psutil.wait_procs(alive, timeout=5)
        if runtime.process is not None:
            try:
                runtime.process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if runtime.log_handle:
            runtime.log_handle.close()

    def stop(self, service_id: str) -> ManagedService:
        with self._lock:
            self._pending.pop(service_id, None)
            runtime = self._runtime.pop(service_id, None)
            if runtime is None:
                existing = next(
                    (
                        service
                        for service in self.store.list_services()
                        if service.id == service_id
                    ),
                    ManagedService(
                        id=service_id,
                        component_id=self._specs.get(
                            service_id,
                            ManagedProcessSpec(
                                service_id=service_id,
                                component_id="unknown",
                                label=service_id,
                                executable="unknown",
                            ),
                        ).component_id,
                        service_key=service_id,
                    ),
                )
                existing.desired_running = False
                existing.health = HealthResult(
                    state=HealthState.STOPPED,
                    service_id=service_id,
                )
                existing.process = None
                self.store.save_service(existing)
                return existing
            self._terminate(runtime)
            service = self._service_snapshot(
                runtime,
                HealthResult(
                    state=HealthState.STOPPED,
                    service_id=service_id,
                ),
                desired_running=False,
            )
            service.process = None
            self.store.save_service(service)
            self.context.event_sink.emit(
                "service.stopped",
                {"service_id": service_id},
                component_id=runtime.spec.component_id,
                service_id=service_id,
            )
            return service

    def restart(self, service_id: str) -> ManagedService:
        with self._lock:
            self.stop(service_id)
            return self.start(service_id)

    def start_all(self) -> list[ManagedService]:
        self.validate_complete()
        return [self.start(service_id) for service_id in self._topological_order()]

    def restore_desired(self) -> dict[str, str]:
        """Restore persisted desired-running services after daemon startup.

        Surviving processes are adopted during registration, so this method
        starts only services whose last durable state requested them to run
        and which are no longer alive.  Failures are isolated per service so a
        broken optional backend cannot prevent the manager API from starting.
        """

        self.validate_complete()
        stored = {
            service.id: service
            for service in self.store.list_services()
            if service.desired_running
        }
        failures: dict[str, str] = {}
        for service_id in self._topological_order():
            if service_id not in stored or service_id in self._runtime:
                continue
            try:
                self.start(service_id)
            except Exception as error:
                message = str(error) or type(error).__name__
                failures[service_id] = message
                self.context.event_sink.emit(
                    "service.restore_failed",
                    {"service_id": service_id, "message": message},
                    component_id=self._specs[service_id].component_id,
                    service_id=service_id,
                )
        return failures

    def stop_all(self) -> list[ManagedService]:
        results = []
        for service_id in reversed(self._topological_order()):
            if service_id in self._runtime or service_id in self._pending:
                results.append(self.stop(service_id))
        return results

    def _topological_order(self) -> tuple[str, ...]:
        result: list[str] = []

        def visit(service_id: str) -> None:
            if service_id in result:
                return
            for dependency in self._specs[service_id].dependencies:
                visit(dependency)
            result.append(service_id)

        for service_id in self._specs:
            visit(service_id)
        return tuple(result)

    def snapshot(self) -> list[ManagedService]:
        snapshots: list[ManagedService] = []
        with self._lock:
            stored = {service.id: service for service in self.store.list_services()}
            for service_id, spec in self._specs.items():
                runtime = self._runtime.get(service_id)
                if runtime is None:
                    service = stored.get(service_id)
                    if service is None:
                        port = spec.ports[0] if spec.ports else None
                        service = ManagedService(
                            id=service_id,
                            component_id=spec.component_id,
                            service_key=service_id,
                            endpoint=(
                                f"http://127.0.0.1:{port}"
                                if port is not None
                                else None
                            ),
                            port=port,
                            health=HealthResult(
                                state=HealthState.STOPPED,
                                service_id=service_id,
                            ),
                        )
                    snapshots.append(
                        service
                    )
                else:
                    snapshots.append(
                        self._service_snapshot(runtime, self._health(spec))
                    )
        return snapshots

    def monitor_once(self) -> None:
        with self._lock:
            now = time.monotonic()
            for service_id, pending in list(self._pending.items()):
                if pending.due_monotonic > now:
                    continue
                self._pending.pop(service_id, None)
                try:
                    self._start_one(
                        pending.spec,
                        restart_count=pending.restart_count,
                    )
                except Exception as error:
                    logging.exception("Managed service restart failed: %s", service_id)
                    self._schedule_restart(
                        pending.spec,
                        pending.restart_count,
                        reason=str(error),
                    )

            for service_id, runtime in list(self._runtime.items()):
                try:
                    process = validate_identity(runtime.identity)
                except (IdentityMismatch, IdentityInspectionFailed):
                    process = None
                exited = process is None
                health = (
                    HealthResult(
                        state=HealthState.UNHEALTHY,
                        service_id=service_id,
                        message="Managed process exited.",
                    )
                    if exited
                    else self._health(runtime.spec)
                )
                if not exited and health.state == HealthState.HEALTHY:
                    runtime.health_failures = 0
                    if (
                        now - runtime.started_monotonic
                        >= runtime.spec.restart.stable_after_seconds
                    ):
                        runtime.restart_count = 0
                    self.store.save_service(self._service_snapshot(runtime, health))
                    continue
                runtime.health_failures += 1
                if (
                    not exited
                    and runtime.health_failures
                    < runtime.spec.restart.health_failure_threshold
                ):
                    self.store.save_service(self._service_snapshot(runtime, health))
                    continue
                self._runtime.pop(service_id, None)
                if not exited:
                    self._terminate(runtime)
                else:
                    if runtime.process is not None:
                        try:
                            runtime.process.wait(timeout=5)
                        except (OSError, subprocess.TimeoutExpired):
                            pass
                    if runtime.log_handle:
                        runtime.log_handle.close()
                self._schedule_restart(
                    runtime.spec,
                    runtime.restart_count,
                    reason=health.message,
                    health_failures=runtime.health_failures,
                )

    def _schedule_restart(
        self,
        spec: ManagedProcessSpec,
        previous_restart_count: int,
        *,
        reason: str = "",
        health_failures: int = 0,
    ) -> None:
        next_count = previous_restart_count + 1
        if next_count > spec.restart.maximum_restarts:
            failed = ManagedService(
                id=spec.service_id,
                component_id=spec.component_id,
                service_key=spec.service_id,
                desired_running=True,
                port=spec.ports[0] if spec.ports else None,
                health=HealthResult(
                    state=HealthState.FAILED,
                    service_id=spec.service_id,
                    message="Restart circuit breaker is open.",
                ),
                restart_count=previous_restart_count,
            )
            self.store.save_service(failed)
            self.context.event_sink.emit(
                "service.failed",
                {
                    "service_id": spec.service_id,
                    "restart_count": previous_restart_count,
                    "reason": reason,
                    "health_failures": health_failures,
                },
                component_id=spec.component_id,
                service_id=spec.service_id,
            )
            return
        delay = min(
            spec.restart.maximum_backoff_seconds,
            spec.restart.base_backoff_seconds * (2 ** (next_count - 1)),
        )
        self._pending[spec.service_id] = _PendingRestart(
            spec=spec,
            restart_count=next_count,
            due_monotonic=time.monotonic() + delay,
        )
        self.context.event_sink.emit(
            "service.restart_scheduled",
            {
                "service_id": spec.service_id,
                "restart_count": next_count,
                "delay_seconds": delay,
                "reason": reason,
                "health_failures": health_failures,
            },
            component_id=spec.component_id,
            service_id=spec.service_id,
        )

    def start_monitoring(self) -> None:
        if self._monitor_thread and self._monitor_thread.is_alive():
            return
        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            name="pandrator-manager-supervisor",
            daemon=True,
        )
        self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        while not self._stop_event.wait(self.monitor_interval_seconds):
            try:
                self.monitor_once()
            except Exception:
                logging.exception("Supervisor monitor iteration failed")

    def shutdown(self, *, stop_children: bool = False) -> None:
        self._stop_event.set()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=5)
        if stop_children:
            self.stop_all()
        else:
            # The OS duplicates the log handle into each child. Closing the
            # daemon's copy permits manager restart/adoption without stopping
            # the service or holding the log file open in the old instance.
            with self._lock:
                for runtime in self._runtime.values():
                    if runtime.log_handle:
                        runtime.log_handle.close()
                        runtime.log_handle = None
                    process = runtime.process
                    if process is not None and process.poll() is None:
                        # Ownership is intentionally handed to the next
                        # manager instance. Keep a waiter alive in this
                        # process so an eventual exit is reaped cleanly.
                        threading.Thread(
                            target=process.wait,
                            name=f"pandrator-service-reaper-{process.pid}",
                            daemon=True,
                        ).start()
                    runtime.process = None
