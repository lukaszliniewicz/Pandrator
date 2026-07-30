import json
import tempfile
import unittest
from pathlib import Path

from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.models import (
    HealthResult,
    HealthState,
    ManagedService,
)
from pandrator_manager.supervisor import ProcessSupervisor


class _DiagnosticSupervisor:
    def __init__(self, services):
        self.services = list(services)

    def snapshot(self):
        return [service.model_copy(deep=True) for service in self.services]


class ManagerDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.application = create_application(self.temporary.name)
        self.layout = self.application.context.layout

    def check(self, report, check_id):
        return next(check for check in report.checks if check.id == check_id)

    def test_fresh_workspace_report_is_read_only_and_has_no_errors(self):
        revision = self.application.store.configuration_revision()
        events = self.application.store.event_bounds()
        report = self.application.doctor()

        self.assertTrue(report.healthy)
        self.assertEqual(report.summary["error"], 0)
        self.assertEqual(
            self.check(report, "database.manager").status,
            "pass",
        )
        self.assertEqual(
            self.application.store.configuration_revision(),
            revision,
        )
        self.assertEqual(self.application.store.event_bounds(), events)

    def test_invalid_release_pointer_and_unsafe_ownership_are_errors(self):
        slot = self.layout.app_versions / "1.0.0"
        slot.mkdir(parents=True)
        self.application.store.save_release_slot(
            product="pandrator",
            version="1.0.0",
            slot_path=slot,
            manifest_digest="a" * 64,
            active=True,
            healthy=True,
        )
        pointer = self.layout.root / "app" / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "version": "1.0.0",
                    "path": str(Path(self.temporary.name).parent),
                }
            ),
            encoding="utf-8",
        )
        outside = Path(self.temporary.name).parent / "not-manager-owned"
        self.application.store.record_owned_path(
            outside,
            owner_kind="fixture",
            owner_id="fixture",
            evidence={},
        )

        report = self.application.doctor()

        self.assertFalse(report.healthy)
        self.assertEqual(
            self.check(report, "release.pandrator.pointer").status,
            "error",
        )
        ownership = self.check(report, "ownership.manifest")
        self.assertEqual(ownership.status, "error")
        self.assertEqual(
            ownership.details["unsafe_paths"],
            [str(outside.resolve(strict=False))],
        )

    def test_source_component_pointer_is_not_treated_as_a_release_bundle(self):
        slot = self.layout.app_versions / "source-revision"
        slot.mkdir(parents=True)
        pointer = self.layout.root / "app" / "current.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(
            json.dumps(
                {
                    "component_id": "pandrator",
                    "version": slot.name,
                    "path": str(slot),
                    "activated_by": "source-install",
                }
            ),
            encoding="utf-8",
        )

        check = self.check(
            self.application.doctor(),
            "release.pandrator.pointer",
        )

        self.assertEqual(check.status, "pass")
        self.assertFalse(check.repairable)
        self.assertIsNone(check.repair_target)
        self.assertEqual(check.details["install_mode"], "source_component")
        self.assertEqual(check.details["slot_path"], str(slot.resolve()))

    def test_desired_unhealthy_service_is_repairable(self):
        service = ManagedService(
            id="tts.fixture",
            component_id="xtts",
            service_key="tts.fixture",
            desired_running=True,
            health=HealthResult(
                state=HealthState.UNHEALTHY,
                service_id="tts.fixture",
                message="fixture failure",
            ),
        )
        report = self.application.doctor(
            supervisor=_DiagnosticSupervisor([service])
        )
        check = self.check(report, "service.tts.fixture")
        self.assertEqual(check.status, "error")
        self.assertTrue(check.repairable)
        self.assertEqual(check.repair_target, "component:xtts")

    def test_positively_identified_legacy_ownership_is_warning_not_unsafe(self):
        legacy = self.layout.root / "xtts2_api"
        legacy.mkdir()
        self.application.store.record_owned_path(
            legacy,
            owner_kind="legacy_component",
            owner_id="xtts",
            evidence={"markers": ["xtts2_api/run.py"]},
        )

        report = self.application.doctor()
        ownership = self.check(report, "ownership.manifest")

        self.assertEqual(ownership.status, "warning")
        self.assertEqual(ownership.details["unsafe_paths"], [])
        self.assertEqual(ownership.details["legacy_paths"], [str(legacy)])

    def test_authenticated_api_exposes_typed_report(self):
        self.application.instance_id = "doctor-test"
        supervisor = ProcessSupervisor(
            self.application.context,
            self.application.store,
            manager_instance_id="doctor-test",
        )
        api = create_api(
            self.application,
            supervisor,
            client_secret="s" * 43,
        )
        response = api.test_client().get(
            "/v1/doctor",
            headers={"Authorization": f"Bearer {'s' * 43}"},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn("healthy", payload)
        self.assertGreater(len(payload["checks"]), 1)


if __name__ == "__main__":
    unittest.main()
