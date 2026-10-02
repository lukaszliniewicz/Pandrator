from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock

from pandrator_manager.context import ManagerContext, WorkspaceLayout
from pandrator_manager.models import (
    HealthProbeSpec,
    HealthResult,
    HealthState,
    ManagedProcessSpec,
    ManagedService,
    ProcessIdentity,
    RestartPolicy,
)
from pandrator_manager.supervisor import ProcessSupervisor
from pandrator_manager.supervisor.runtime import _RuntimeProcess


class _FakeClock:
    def __init__(self, now: float = 100.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class _MemoryStore:
    def __init__(self) -> None:
        self.services: dict[str, ManagedService] = {}
        self.saved: list[ManagedService] = []
        self.fail_next_save = False

    def list_services(self) -> list[ManagedService]:
        return [service.model_copy(deep=True) for service in self.services.values()]

    def save_service(self, service: ManagedService) -> None:
        if self.fail_next_save:
            self.fail_next_save = False
            raise OSError("simulated store failure")
        saved = service.model_copy(deep=True)
        self.services[service.id] = saved
        self.saved.append(saved)


class SupervisorPersistenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = _MemoryStore()
        self.context = ManagerContext(
            layout=WorkspaceLayout.from_value(self.temporary.name),
            environment={},
        )
        self.supervisor = ProcessSupervisor(
            self.context,
            self.store,
            manager_instance_id="manager-test",
            monitor_interval_seconds=1.0,
        )
        self.spec = self._spec()
        self.supervisor.register(self.spec)
        self.runtime = self._runtime(self.spec)
        self.supervisor._runtime[self.spec.service_id] = self.runtime

    @staticmethod
    def _spec(
        service_id: str = "fixture.service",
        *,
        health_failure_threshold: int = 3,
        stable_after_seconds: float = 60.0,
    ) -> ManagedProcessSpec:
        return ManagedProcessSpec(
            service_id=service_id,
            component_id="fixture",
            label="Fixture service",
            executable="fixture-service",
            readiness=HealthProbeSpec(),
            restart=RestartPolicy(
                maximum_restarts=3,
                base_backoff_seconds=0,
                maximum_backoff_seconds=0,
                health_failure_threshold=health_failure_threshold,
                stable_after_seconds=stable_after_seconds,
            ),
        )

    @staticmethod
    def _identity(manager_instance_id: str = "manager-test") -> ProcessIdentity:
        return ProcessIdentity(
            pid=12345,
            create_time=100.0,
            executable="/usr/bin/fixture-service",
            manager_instance_id=manager_instance_id,
            ownership_token="a" * 32,
            process_group_id=12345,
            session_id=12345,
        )

    def _runtime(
        self,
        spec: ManagedProcessSpec,
        *,
        restart_count: int = 0,
        started_monotonic: float = 100.0,
    ) -> _RuntimeProcess:
        return _RuntimeProcess(
            spec=spec,
            identity=self._identity(),
            process=None,
            log_handle=None,
            started_monotonic=started_monotonic,
            restart_count=restart_count,
        )

    def _service(
        self,
        *,
        health_state: HealthState = HealthState.HEALTHY,
        message: str = "",
        restart_count: int = 0,
        service_id: str | None = None,
    ) -> ManagedService:
        service_id = service_id or self.spec.service_id
        return ManagedService(
            id=service_id,
            component_id="fixture",
            service_key=service_id,
            desired_running=True,
            endpoint="http://127.0.0.1:12345",
            port=12345,
            capabilities=("fixture",),
            health=HealthResult(
                state=health_state,
                service_id=service_id,
                protocol_version="v1",
                message=message,
                checked_at=datetime(2026, 10, 2, tzinfo=timezone.utc),
                details={"ready": True, "count": 1},
            ),
            process=self._identity(),
            restart_count=restart_count,
        )

    def test_persistence_interval_defaults_to_five_minutes_and_is_clamped(self) -> None:
        defaulted = ProcessSupervisor(
            self.context,
            self.store,
            manager_instance_id="manager-default",
        )
        self.assertEqual(defaulted.persistence_interval_seconds, 300.0)

        clamped = ProcessSupervisor(
            self.context,
            self.store,
            manager_instance_id="manager-clamped",
            monitor_interval_seconds=75.0,
            persistence_interval_seconds=10.0,
        )
        self.assertEqual(clamped.persistence_interval_seconds, 75.0)

    def test_monitor_probes_each_tick_and_persists_first_and_five_minute_boundary(self):
        clock = _FakeClock()
        checked_at = datetime(2026, 10, 2, tzinfo=timezone.utc)
        checked_times = iter(
            checked_at + timedelta(seconds=offset) for offset in range(3)
        )

        def probe(_spec: ManagedProcessSpec) -> HealthResult:
            return HealthResult(
                state=HealthState.HEALTHY,
                service_id=self.spec.service_id,
                checked_at=next(checked_times),
                details={"ready": True},
            )

        with (
            mock.patch(
                "pandrator_manager.supervisor.runtime.time.monotonic",
                side_effect=clock,
            ),
            mock.patch(
                "pandrator_manager.supervisor.runtime.validate_identity",
                return_value=object(),
            ),
            mock.patch.object(self.supervisor, "_health", side_effect=probe) as probe_mock,
        ):
            self.supervisor.monitor_once()
            self.assertEqual(len(self.store.saved), 1)

            clock.now = 399.999
            self.supervisor.monitor_once()
            self.assertEqual(len(self.store.saved), 1)

            clock.now = 400.0
            self.supervisor.monitor_once()

        self.assertEqual(len(self.store.saved), 2)
        self.assertEqual(probe_mock.call_count, 3)
        self.assertGreater(
            self.store.saved[1].health.checked_at,
            self.store.saved[0].health.checked_at,
        )

    def test_every_meaningful_service_field_change_saves_promptly(self) -> None:
        clock = _FakeClock()
        original = self._service()
        changes = {
            "id": lambda service: setattr(service, "id", "changed.service"),
            "component_id": lambda service: setattr(service, "component_id", "changed"),
            "service_key": lambda service: setattr(service, "service_key", "changed"),
            "desired_running": lambda service: setattr(
                service, "desired_running", False
            ),
            "endpoint": lambda service: setattr(
                service, "endpoint", "http://127.0.0.1:12346"
            ),
            "port": lambda service: setattr(service, "port", 12346),
            "capabilities": lambda service: setattr(
                service, "capabilities", ("changed",)
            ),
            "health.state": lambda service: setattr(
                service.health, "state", HealthState.DEGRADED
            ),
            "health.service_id": lambda service: setattr(
                service.health, "service_id", "changed.service"
            ),
            "health.protocol_version": lambda service: setattr(
                service.health, "protocol_version", "v2"
            ),
            "health.message": lambda service: setattr(
                service.health, "message", "changed"
            ),
            "health.details": lambda service: service.health.details.update(
                {"count": 2}
            ),
            "process.pid": lambda service: setattr(service.process, "pid", 12346),
            "process.create_time": lambda service: setattr(
                service.process, "create_time", 101.0
            ),
            "process.executable": lambda service: setattr(
                service.process, "executable", "/usr/bin/changed-service"
            ),
            "process.manager_instance_id": lambda service: setattr(
                service.process, "manager_instance_id", "manager-changed"
            ),
            "process.ownership_token": lambda service: setattr(
                service.process, "ownership_token", "b" * 32
            ),
            "process.process_group_id": lambda service: setattr(
                service.process, "process_group_id", 12346
            ),
            "process.session_id": lambda service: setattr(
                service.process, "session_id", 12346
            ),
            "restart_count": lambda service: setattr(service, "restart_count", 1),
        }

        with mock.patch(
            "pandrator_manager.supervisor.runtime.time.monotonic",
            side_effect=clock,
        ):
            self.supervisor._save_runtime_service(self.runtime, original, force=True)
            for name, change in changes.items():
                with self.subTest(field=name):
                    candidate = self.runtime.last_persisted_service.model_copy(
                        deep=True
                    )
                    change(candidate)
                    previous_count = len(self.store.saved)
                    self.supervisor._save_runtime_service(self.runtime, candidate)
                    self.assertEqual(len(self.store.saved), previous_count + 1)

    def test_unhealthy_and_recovered_health_are_persisted_immediately(self) -> None:
        clock = _FakeClock()
        self.supervisor._save_runtime_service(
            self.runtime,
            self._service(),
            force=True,
        )
        unhealthy = HealthResult(
            state=HealthState.UNHEALTHY,
            service_id=self.spec.service_id,
            message="probe failed",
            details={"reason": "connection refused"},
        )
        recovered = HealthResult(
            state=HealthState.HEALTHY,
            service_id=self.spec.service_id,
            details={"ready": True},
        )
        with (
            mock.patch(
                "pandrator_manager.supervisor.runtime.time.monotonic",
                side_effect=clock,
            ),
            mock.patch(
                "pandrator_manager.supervisor.runtime.validate_identity",
                return_value=object(),
            ),
            mock.patch.object(
                self.supervisor,
                "_health",
                side_effect=[unhealthy, recovered],
            ) as probe,
        ):
            clock.now = 101.0
            self.supervisor.monitor_once()
            clock.now = 102.0
            self.supervisor.monitor_once()

        self.assertEqual(probe.call_count, 2)
        self.assertEqual(len(self.store.saved), 3)
        self.assertEqual(self.store.saved[1].health.state, HealthState.UNHEALTHY)
        self.assertEqual(self.store.saved[1].health.message, "probe failed")
        self.assertEqual(self.store.saved[2].health.state, HealthState.HEALTHY)

    def test_failure_threshold_and_restart_schedule_run_during_persistence_cooldown(self):
        spec = self._spec(health_failure_threshold=2)
        self.supervisor._runtime.pop(self.spec.service_id)
        self.supervisor.replace_spec(spec)
        runtime = self._runtime(spec)
        self.supervisor._runtime[spec.service_id] = runtime
        self.supervisor._save_runtime_service(
            runtime,
            self._service(service_id=spec.service_id),
            force=True,
        )
        unhealthy = HealthResult(
            state=HealthState.UNHEALTHY,
            service_id=spec.service_id,
            message="still down",
        )
        clock = _FakeClock()
        with (
            mock.patch(
                "pandrator_manager.supervisor.runtime.time.monotonic",
                side_effect=clock,
            ),
            mock.patch(
                "pandrator_manager.supervisor.runtime.validate_identity",
                return_value=object(),
            ),
            mock.patch.object(
                self.supervisor,
                "_health",
                return_value=unhealthy,
            ) as probe,
            mock.patch.object(self.supervisor, "_terminate") as terminate,
            mock.patch.object(self.supervisor, "_start_one") as restart,
        ):
            clock.now = 101.0
            self.supervisor.monitor_once()
            self.assertIn(spec.service_id, self.supervisor._runtime)

            clock.now = 102.0
            self.supervisor.monitor_once()
            self.assertNotIn(spec.service_id, self.supervisor._runtime)
            self.assertIn(spec.service_id, self.supervisor._pending)
            terminate.assert_called_once_with(runtime)

            clock.now = 103.0
            self.supervisor.monitor_once()

        self.assertEqual(probe.call_count, 2)
        restart.assert_called_once_with(spec, restart_count=1)

    def test_stable_runtime_restart_count_reset_saves_immediately(self) -> None:
        spec = self._spec(stable_after_seconds=5.0)
        self.supervisor._runtime.pop(self.spec.service_id)
        self.supervisor.replace_spec(spec)
        runtime = self._runtime(
            spec,
            restart_count=2,
            started_monotonic=90.0,
        )
        self.supervisor._runtime[spec.service_id] = runtime
        clock = _FakeClock(100.0)
        healthy = HealthResult(
            state=HealthState.HEALTHY,
            service_id=spec.service_id,
        )
        initial = self._service(service_id=spec.service_id, restart_count=2)
        initial.health = healthy
        self.supervisor._save_runtime_service(runtime, initial, force=True)
        with (
            mock.patch(
                "pandrator_manager.supervisor.runtime.time.monotonic",
                side_effect=clock,
            ),
            mock.patch(
                "pandrator_manager.supervisor.runtime.validate_identity",
                return_value=object(),
            ),
            mock.patch.object(self.supervisor, "_health", return_value=healthy),
        ):
            self.supervisor.monitor_once()

        self.assertEqual(runtime.restart_count, 0)
        self.assertEqual(len(self.store.saved), 2)
        self.assertEqual(self.store.saved[-1].restart_count, 0)

    def test_failed_save_does_not_poison_cache_and_is_retried(self) -> None:
        clock = _FakeClock()
        service = self._service()
        self.store.fail_next_save = True
        with mock.patch(
            "pandrator_manager.supervisor.runtime.time.monotonic",
            side_effect=clock,
        ):
            with self.assertRaisesRegex(OSError, "simulated store failure"):
                self.supervisor._save_runtime_service(self.runtime, service)

            self.assertIsNone(self.runtime.last_persisted_service)
            self.assertIsNone(self.runtime.last_persisted_monotonic)
            self.supervisor._save_runtime_service(self.runtime, service)

        self.assertEqual(len(self.store.saved), 1)
        self.assertEqual(self.runtime.last_persisted_service, service)

    def test_runtimes_and_persisted_snapshots_do_not_share_mutable_state(self) -> None:
        second_spec = self._spec("other.service")
        self.supervisor.register(second_spec)
        second_runtime = self._runtime(second_spec)
        self.supervisor._runtime[second_spec.service_id] = second_runtime
        first_service = self._service()
        second_service = self._service(service_id=second_spec.service_id)

        self.supervisor._save_runtime_service(self.runtime, first_service)
        self.supervisor._save_runtime_service(second_runtime, second_service)

        self.assertEqual(len(self.store.saved), 2)
        self.assertIsNot(
            self.runtime.last_persisted_service,
            second_runtime.last_persisted_service,
        )
        self.assertIsNot(
            self.runtime.last_persisted_service.health.details,
            first_service.health.details,
        )
        self.assertIsNot(self.store.saved[0], self.runtime.last_persisted_service)

        first_service.health.details["ready"] = False
        self.assertTrue(self.runtime.last_persisted_service.health.details["ready"])
        self.assertTrue(self.store.saved[0].health.details["ready"])
        self.assertTrue(second_runtime.last_persisted_service.health.details["ready"])

    def test_adoption_force_saves_and_seeds_runtime_cache(self) -> None:
        existing = self._service()
        self.store.save_service(existing)
        self.store.saved.clear()
        self.supervisor._runtime.clear()
        with mock.patch(
            "pandrator_manager.supervisor.runtime.validate_identity",
            return_value=object(),
        ):
            self.supervisor._specs.clear()
            self.supervisor.register(self.spec)

        adopted = self.supervisor._runtime[self.spec.service_id]
        self.assertEqual(len(self.store.saved), 1)
        self.assertIsNotNone(adopted.last_persisted_service)
        self.assertIsNotNone(adopted.last_persisted_monotonic)
        self.assertEqual(
            adopted.last_persisted_service.process.manager_instance_id,
            "manager-test",
        )


if __name__ == "__main__":
    unittest.main()
