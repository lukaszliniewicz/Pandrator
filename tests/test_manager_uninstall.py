import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

import pandrator_manager.uninstall as uninstall_module
from pandrator_manager.api import create_api
from pandrator_manager.application import create_application
from pandrator_manager.client import ManagerClient, ProductUninstalled
from pandrator_manager.context import WorkspaceLayout
from pandrator_manager.launcher import install_stable_launcher
from pandrator_manager.models import (
    HealthResult,
    HealthState,
    ManagedService,
    OperationState,
)
from pandrator_manager.operations import OperationEngine
from pandrator_manager.uninstall import (
    clear_uninstall_status,
    pending_uninstalls,
    read_uninstall_handoff,
    read_uninstall_status,
)


class _UninstallSupervisor:
    def __init__(self, store):
        self.store = store
        self.services = {
            "fixture.service": ManagedService(
                id="fixture.service",
                component_id="silero",
                service_key="fixture.service",
                desired_running=True,
                health=HealthResult(
                    state=HealthState.STOPPED,
                    service_id="fixture.service",
                ),
            )
        }
        self.store.save_service(self.services["fixture.service"])
        self.started = []

    def snapshot(self):
        return [
            service.model_copy(deep=True)
            for service in self.services.values()
        ]

    def stop_all(self):
        stopped = []
        for service in self.services.values():
            if service.desired_running or service.process is not None:
                service.desired_running = False
                service.process = None
                service.health = HealthResult(
                    state=HealthState.STOPPED,
                    service_id=service.id,
                )
                self.store.save_service(service)
                stopped.append(service.model_copy(deep=True))
        return stopped

    def start(self, service_id):
        service = self.services[service_id]
        service.desired_running = True
        service.health = HealthResult(
            state=HealthState.HEALTHY,
            service_id=service_id,
        )
        self.store.save_service(service)
        self.started.append(service_id)
        return service.model_copy(deep=True)


class ManagerUninstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(
            ignore_cleanup_errors=True
        )
        self.addCleanup(self.temporary.cleanup)
        self.application = create_application(self.temporary.name)
        self.layout = self.application.context.layout
        self.supervisor = _UninstallSupervisor(self.application.store)
        (self.layout.root / "app").mkdir(parents=True, exist_ok=True)
        (self.layout.root / "app" / "software.txt").write_text(
            "owned",
            encoding="utf-8",
        )
        (self.layout.data / "user.txt").write_text(
            "preserve me",
            encoding="utf-8",
        )

    def _plan(
        self,
        *,
        purge_data=False,
        export_data=None,
    ):
        return self.application.uninstall_plan(
            purge_data=purge_data,
            export_data=export_data,
        )

    def _pending_operation(self, plan):
        operation, created = self.application.submit_operation(
            plan_id=plan.id,
            plan_digest=plan.digest,
            accepted_confirmations=tuple(
                confirmation.key
                for confirmation in plan.confirmations
            ),
            idempotency_key=f"uninstall-{plan.id}",
        )
        self.assertTrue(created)
        engine = OperationEngine(
            self.application.context,
            self.application.store,
            self.application.registry,
            supervisor=self.supervisor,
            manager_handoff_callback=lambda _execution, _result: None,
        )
        engine._execute(operation.id)
        pending = self.application.store.get_operation(operation.id)
        self.assertEqual(pending.state, OperationState.HANDOFF_PENDING)
        return operation

    def test_default_plan_preserves_data_and_purge_needs_second_confirmation(self):
        preserved = self._plan()
        self.assertTrue(preserved.impacts["uninstall"]["preserve_data"])
        self.assertFalse(preserved.impacts["uninstall"]["purge_data"])
        self.assertEqual(
            [item.key for item in preserved.confirmations],
            ["uninstall:software"],
        )

        purged = self._plan(purge_data=True)
        self.assertFalse(purged.impacts["uninstall"]["preserve_data"])
        self.assertEqual(
            [item.key for item in purged.confirmations],
            ["uninstall:software", "uninstall:purge-data"],
        )

    def test_authenticated_api_creates_an_idempotent_uninstall_plan(self):
        self.application.instance_id = "uninstall-api"
        api = create_api(
            self.application,
            self.supervisor,
            client_secret="s" * 43,
        )
        client = api.test_client()
        headers = {
            "Authorization": f"Bearer {'s' * 43}",
            "Idempotency-Key": "uninstall-plan",
        }
        first = client.post(
            "/v1/uninstall/plans",
            headers=headers,
            json={"purge_data": False},
        )
        replay = client.post(
            "/v1/uninstall/plans",
            headers=headers,
            json={"purge_data": False},
        )
        self.assertEqual(first.status_code, 201)
        self.assertEqual(first.get_json()["id"], replay.get_json()["id"])
        self.assertEqual(first.get_json()["kind"], "uninstall")

    def test_pending_descriptor_is_authenticated_and_excludes_preserved_data(self):
        plan = self._plan()
        operation = self._pending_operation(plan)
        self.assertEqual(pending_uninstalls(self.layout), (operation.id,))
        envelope, _ = read_uninstall_handoff(
            self.layout,
            operation.id,
        )
        self.assertFalse(envelope.payload.purge_data)
        data = self.layout.data.resolve(strict=False)
        self.assertFalse(
            any(
                Path(target).resolve(strict=False) == data
                or self.layout.contains(data, Path(target))
                for target in envelope.payload.targets
            )
        )

        external = uninstall_module._external_descriptor_path(
            self.layout,
            operation.id,
        )
        tampered = json.loads(external.read_text(encoding="utf-8"))
        tampered["payload"]["purge_data"] = True
        external.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(Exception, "authentication"):
            read_uninstall_handoff(self.layout, operation.id)

    def test_native_uninstall_stages_digest_verified_external_launcher(self):
        suffix = ".exe" if uninstall_module.os.name == "nt" else ""
        source = Path(self.temporary.name) / f"bootstrap{suffix}"
        source.write_bytes(b"native launcher fixture")
        installed = install_stable_launcher(
            self.layout,
            source=source,
        )

        operation = self._pending_operation(self._plan())
        envelope, _ = read_uninstall_handoff(self.layout, operation.id)
        payload = envelope.payload
        self.assertEqual(payload.cleanup_mode, "native_launcher")
        self.assertEqual(payload.cleanup_sha256, installed.sha256)
        cleanup = Path(payload.cleanup_executable)
        self.assertTrue(cleanup.is_file())
        self.assertFalse(self.layout.contains(self.layout.root, cleanup))
        self.assertEqual(cleanup.read_bytes(), source.read_bytes())

    def test_control_paths_reject_traversal_and_non_directory_redirection(self):
        with self.assertRaisesRegex(Exception, "filesystem-safe"):
            read_uninstall_status(self.layout, "../outside")

        control = uninstall_module.uninstall_control_root(self.layout)
        control.write_text("not a directory", encoding="utf-8")
        with self.assertRaisesRegex(Exception, "real directory"):
            pending_uninstalls(self.layout)

    def test_quarantine_cleanup_retries_when_a_child_disappears(self):
        quarantine = Path(self.temporary.name) / "quarantine-race"
        quarantine.mkdir()
        child = quarantine / "cached-artifact.zip"
        child.write_bytes(b"artifact")
        original = uninstall_module.shutil.rmtree
        calls = 0

        def flaky(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                child.unlink()
                raise FileNotFoundError(str(child))
            return original(path)

        with mock.patch.object(
            uninstall_module.shutil,
            "rmtree",
            side_effect=flaky,
        ):
            uninstall_module._remove_quarantine(quarantine)

        self.assertEqual(calls, 2)
        self.assertFalse(quarantine.exists())

    @unittest.skipUnless(
        uninstall_module.os.name == "nt",
        "Windows-only transient file-lock behavior",
    )
    def test_quarantine_cleanup_retries_transient_windows_file_lock(self):
        quarantine = Path(self.temporary.name) / "quarantine-lock"
        quarantine.mkdir()
        (quarantine / "cached-artifact.zip").write_bytes(b"artifact")
        original = uninstall_module.shutil.rmtree
        calls = 0

        def flaky(path):
            nonlocal calls
            calls += 1
            if calls == 1:
                error = PermissionError("temporarily locked")
                error.winerror = 32
                raise error
            return original(path)

        with (
            mock.patch.object(
                uninstall_module.shutil,
                "rmtree",
                side_effect=flaky,
            ),
            mock.patch.object(uninstall_module.time, "sleep"),
        ):
            uninstall_module._remove_quarantine(quarantine)

        self.assertEqual(calls, 2)
        self.assertFalse(quarantine.exists())

    @unittest.skipUnless(
        uninstall_module.os.name == "nt",
        "Windows extended-length path behavior",
    )
    def test_quarantine_cleanup_handles_paths_beyond_max_path(self):
        quarantine = Path(self.temporary.name) / "quarantine-long-path"
        deep = quarantine
        while len(str(deep / "artifact.zip")) <= 270:
            deep /= "release-cache-segment"
        extended = "\\\\?\\" + str(deep.resolve(strict=False))
        uninstall_module.os.makedirs(extended)
        artifact = extended + "\\artifact.zip"
        with open(artifact, "wb") as handle:
            handle.write(b"artifact")
        self.assertGreater(len(str(deep / "artifact.zip")), 260)

        uninstall_module._remove_quarantine(quarantine)

        self.assertFalse(quarantine.exists())

    def test_clear_status_recovers_exact_quarantine_residue_first(self):
        operation_id = "recover-residue"
        control = uninstall_module._safe_control_root(
            self.layout,
            create=True,
        )
        quarantine = control / f"{operation_id}.quarantine"
        quarantine.mkdir()
        (quarantine / "cached-artifact.zip").write_bytes(b"artifact")
        uninstall_module._write_status(
            uninstall_module.uninstall_status_path(
                self.layout,
                operation_id,
            ),
            {
                "status": "succeeded_with_cleanup_residue",
                "operation_id": operation_id,
                "cleanup_residue": str(quarantine),
                "message": "temporarily locked",
            },
        )

        status = clear_uninstall_status(self.layout, operation_id)

        self.assertEqual(status["status"], "succeeded")
        self.assertIsNone(status["cleanup_residue"])
        self.assertFalse(quarantine.exists())
        self.assertIsNone(
            read_uninstall_status(self.layout, operation_id)
        )

    def test_clear_status_preserves_unexpected_residue_journal(self):
        operation_id = "unexpected-residue"
        control = uninstall_module._safe_control_root(
            self.layout,
            create=True,
        )
        outside = Path(self.temporary.name) / "do-not-delete"
        outside.write_text("keep", encoding="utf-8")
        status_path = uninstall_module.uninstall_status_path(
            self.layout,
            operation_id,
        )
        uninstall_module._write_status(
            status_path,
            {
                "status": "succeeded_with_cleanup_residue",
                "operation_id": operation_id,
                "cleanup_residue": str(outside),
                "message": "unexpected path",
            },
        )

        status = clear_uninstall_status(self.layout, operation_id)

        self.assertEqual(
            status["status"],
            "succeeded_with_cleanup_residue",
        )
        self.assertTrue(outside.is_file())
        self.assertTrue(status_path.is_file())
        self.assertTrue(control.is_dir())

    def test_clear_status_retries_a_locked_helper_log(self):
        operation_id = "locked-log"
        control = uninstall_module._safe_control_root(
            self.layout,
            create=True,
        )
        status_path = uninstall_module.uninstall_status_path(
            self.layout,
            operation_id,
        )
        uninstall_module._write_status(
            status_path,
            {
                "status": "succeeded",
                "operation_id": operation_id,
            },
        )
        log = control / "uninstall.log"
        log.write_text("finished", encoding="utf-8")
        original_unlink = Path.unlink
        attempts = 0

        def transient_unlink(path, *args, **kwargs):
            nonlocal attempts
            if path == log and attempts == 0:
                attempts += 1
                raise PermissionError(
                    uninstall_module.errno.EACCES,
                    "helper still owns stdout",
                )
            if path == log:
                attempts += 1
            return original_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(uninstall_module.os, "name", "nt"),
            mock.patch.object(Path, "unlink", transient_unlink),
            mock.patch.object(uninstall_module.time, "sleep") as sleep,
        ):
            status = clear_uninstall_status(self.layout, operation_id)

        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(attempts, 2)
        sleep.assert_called_once()
        self.assertFalse(status_path.exists())
        self.assertFalse(control.exists())

    def test_clear_status_keeps_journal_if_log_cannot_be_removed(self):
        operation_id = "blocked-log"
        control = uninstall_module._safe_control_root(
            self.layout,
            create=True,
        )
        status_path = uninstall_module.uninstall_status_path(
            self.layout,
            operation_id,
        )
        uninstall_module._write_status(
            status_path,
            {
                "status": "succeeded",
                "operation_id": operation_id,
            },
        )
        log = control / "uninstall.log"
        log.write_text("finished", encoding="utf-8")
        original_unlink = Path.unlink

        def blocked_unlink(path, *args, **kwargs):
            if path == log:
                raise PermissionError(
                    uninstall_module.errno.EACCES,
                    "unexpected permanent lock",
                )
            return original_unlink(path, *args, **kwargs)

        with (
            mock.patch.object(uninstall_module.os, "name", "posix"),
            mock.patch.object(Path, "unlink", blocked_unlink),
        ):
            status = clear_uninstall_status(self.layout, operation_id)

        self.assertEqual(status["status"], "succeeded")
        self.assertTrue(status_path.is_file())
        self.assertTrue(log.is_file())
        clear_uninstall_status(self.layout, operation_id)
        self.assertFalse(control.exists())

    def test_completed_uninstall_status_prevents_empty_workspace_recreation(self):
        workspace = Path(self.temporary.name) / "completed"
        workspace.mkdir()
        layout = WorkspaceLayout.from_value(workspace)
        control = uninstall_module._safe_control_root(layout, create=True)
        operation_id = "completed-operation"
        uninstall_module._write_status(
            uninstall_module.uninstall_status_path(layout, operation_id),
            {
                "status": "succeeded",
                "operation_id": operation_id,
            },
        )
        start = mock.Mock()
        with (
            mock.patch.object(ManagerClient, "start_daemon", start),
            self.assertRaises(ProductUninstalled) as raised,
        ):
            ManagerClient.ensure_running(workspace, timeout_seconds=0.1)
        self.assertEqual(raised.exception.status["operation_id"], operation_id)
        start.assert_not_called()
        self.assertFalse(layout.root.exists())
        self.assertFalse(control.exists())

    def test_external_helper_success_preserves_data_and_removes_software(self):
        operation = self._pending_operation(self._plan())
        with mock.patch.object(
            uninstall_module,
            "_wait_for_old_manager",
        ):
            result = uninstall_module.run_uninstall_handoff(
                self.layout.workspace,
                operation.id,
            )
        self.assertEqual(result, 0)
        self.assertFalse((self.layout.root / "app").exists())
        self.assertTrue((self.layout.data / "user.txt").is_file())
        self.assertFalse(self.layout.state.exists())
        status = read_uninstall_status(self.layout, operation.id)
        self.assertEqual(status["status"], "succeeded")
        self.assertEqual(status["preserved_data"], str(self.layout.data))
        clear_uninstall_status(self.layout, operation.id)

    def test_uninstall_reconciles_embedded_legacy_data_before_removal(self):
        legacy = self.layout.root / "Pandrator"
        output = legacy / "Outputs" / "Legacy Book"
        output.mkdir(parents=True)
        (output / "chapter.wav").write_bytes(b"legacy audio")
        self.application.store.record_owned_path(
            legacy,
            owner_kind="legacy_component",
            owner_id="pandrator",
            evidence={"markers": ["Pandrator/pyproject.toml"]},
        )

        plan = self._plan()
        self.assertIn(
            "uninstall:legacy-data",
            [task.id for task in plan.tasks],
        )
        operation = self._pending_operation(plan)
        self.assertEqual(
            (
                self.layout.data
                / "Outputs"
                / "Legacy Book"
                / "chapter.wav"
            ).read_bytes(),
            b"legacy audio",
        )
        with mock.patch.object(
            uninstall_module,
            "_wait_for_old_manager",
        ):
            result = uninstall_module.run_uninstall_handoff(
                self.layout.workspace,
                operation.id,
            )
        self.assertEqual(result, 0)
        self.assertFalse(legacy.exists())
        self.assertEqual(
            (
                self.layout.data
                / "Outputs"
                / "Legacy Book"
                / "chapter.wav"
            ).read_bytes(),
            b"legacy audio",
        )
        clear_uninstall_status(self.layout, operation.id)

    def test_imported_legacy_shared_software_is_removed_but_unknown_files_remain(self):
        fish = self.layout.root / "fishs2-cpp-fastapi"
        fish.mkdir()
        (fish / "run.py").write_text("", encoding="utf-8")
        (fish / "pyproject.toml").write_text("", encoding="utf-8")
        pixi_home = self.layout.root / ".pixi-home"
        (pixi_home / "bin").mkdir(parents=True)
        (pixi_home / "bin" / "pixi.exe").write_bytes(b"pixi")
        pixi_cache = self.layout.root / ".pixi-cache"
        pixi_cache.mkdir()
        (pixi_cache / "cache.bin").write_bytes(b"cache")
        config = self.layout.root / "config.json"
        config.write_text(
            json.dumps({"fishs2_support": False}),
            encoding="utf-8",
        )
        (self.layout.root / "packaging_layout.json").write_text(
            json.dumps(
                {
                    "layout_version": 1,
                    "shared_paths": [
                        ".pixi-home",
                        ".pixi-cache",
                        "config.json",
                        "packaging_layout.json",
                    ],
                    "component_paths": {
                        "fishs2": ["fishs2-cpp-fastapi"],
                    },
                }
            ),
            encoding="utf-8",
        )
        unknown = self.layout.root / "personal-notes.txt"
        unknown.write_text("keep", encoding="utf-8")
        report = self.application.legacy_report()
        self.application.import_legacy(
            source_digest=report.source_digest,
            confirmed=True,
        )

        operation = self._pending_operation(self._plan())
        self.assertTrue((self.layout.data / "config.json").is_file())
        with mock.patch.object(
            uninstall_module,
            "_wait_for_old_manager",
        ):
            result = uninstall_module.run_uninstall_handoff(
                self.layout.workspace,
                operation.id,
            )

        self.assertEqual(result, 0)
        self.assertFalse(fish.exists())
        self.assertFalse(pixi_home.exists())
        self.assertFalse(pixi_cache.exists())
        self.assertFalse(config.exists())
        self.assertTrue(unknown.is_file())
        self.assertTrue((self.layout.data / "config.json").is_file())
        clear_uninstall_status(self.layout, operation.id)

    def test_purge_can_export_data_before_removal(self):
        export = Path(self.temporary.name) / "data-export.zip"
        operation = self._pending_operation(
            self._plan(
                purge_data=True,
                export_data=export,
            )
        )
        with zipfile.ZipFile(export) as archive:
            self.assertEqual(
                archive.read("data/user.txt").decode(),
                "preserve me",
            )
            self.assertIsNone(archive.testzip())
        with mock.patch.object(
            uninstall_module,
            "_wait_for_old_manager",
        ):
            result = uninstall_module.run_uninstall_handoff(
                self.layout.workspace,
                operation.id,
            )
        self.assertEqual(result, 0)
        self.assertFalse(self.layout.data.exists())
        self.assertTrue(export.is_file())
        clear_uninstall_status(self.layout, operation.id)

    def test_precommit_failure_restores_files_state_and_service_desires(self):
        export = Path(self.temporary.name) / "rollback-export.zip"
        operation = self._pending_operation(
            self._plan(export_data=export)
        )
        restart = mock.Mock()
        with (
            mock.patch.object(
                uninstall_module,
                "_wait_for_old_manager",
            ),
            mock.patch.object(
                uninstall_module,
                "_write_authenticated",
                side_effect=RuntimeError("commit failed"),
            ),
            mock.patch.object(
                uninstall_module,
                "_restart_previous_manager",
                restart,
            ),
        ):
            result = uninstall_module.run_uninstall_handoff(
                self.layout.workspace,
                operation.id,
            )
        self.assertEqual(result, 2)
        self.assertTrue((self.layout.root / "app" / "software.txt").is_file())
        self.assertTrue((self.layout.data / "user.txt").is_file())
        self.assertTrue(self.layout.database.is_file())
        self.assertFalse(export.exists())
        failed = self.application.store.get_operation(operation.id)
        self.assertEqual(failed.state, OperationState.FAILED)
        restored_service = {
            service.id: service
            for service in self.application.store.list_services()
        }["fixture.service"]
        self.assertTrue(restored_service.desired_running)
        restart.assert_called_once()


if __name__ == "__main__":
    unittest.main()
