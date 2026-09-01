import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from contextlib import closing, redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

import certifi
import psutil

from pandrator_manager.application import create_application
from pandrator_manager.artifacts import (
    ArtifactDownloader,
    ArtifactSpec,
    SafeExtractor,
)
from pandrator_manager.cli import main as manager_cli_main
from pandrator_manager.components import ComponentRegistry, builtin_registry
from pandrator_manager.components.builtin import (
    BUILTIN_COMPONENTS,
    MarkerComponentDriver,
)
from pandrator_manager.components.host import resolve_auto_compute
from pandrator_manager.context import CancellationToken, ManagerContext, WorkspaceLayout
from pandrator_manager.environments import (
    PIXI_VERSION,
    PixiAsset,
    PixiBootstrapper,
    PixiEnvironmentManager,
    PixiEnvironmentSpec,
    pixi_asset_for,
)
from pandrator_manager.errors import (
    CancellationRequested,
    ConflictError,
    ManagerError,
    RevisionConflict,
    UnsafePathError,
)
from pandrator_manager.legacy import LegacyImporter
from pandrator_manager.legacy_data import (
    legacy_data_inventory,
    reconcile_legacy_data,
    rollback_legacy_data,
)
from pandrator_manager.models import (
    ComponentDefinition,
    ComponentInspection,
    ComponentState,
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
    ResolvedComponentState,
)
from pandrator_manager.network import private_network_candidates
from pandrator_manager.processes import CommandRunner, CommandSpec
from pandrator_manager.runtime_specs import component_runtime_spec
from pandrator_manager.state import ManagerStore


class WorkspaceLayoutTests(unittest.TestCase):
    def test_xtts_builtin_is_pinned_to_lifecycle_wrapper_revision(self):
        xtts = next(item for item in BUILTIN_COMPONENTS if item.id == "xtts")
        self.assertEqual(
            "9d9421080b7db528215d6e63867b95956d36af90",
            xtts.source_revision,
        )

    def test_layout_uses_distinct_owned_and_user_data_zones(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            self.assertEqual(layout.root, Path(directory).resolve() / "Pandrator")
            self.assertNotIn(layout.data, layout.owned_roots)
            self.assertTrue(layout.database.parent.is_dir())
            self.assertTrue(layout.data.is_dir())

    def test_path_boundary_rejects_parent_escape_and_owned_root_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            with self.assertRaises(UnsafePathError):
                layout.require_within(layout.services / ".." / ".." / "outside")
            with self.assertRaises(UnsafePathError):
                layout.require_within(layout.services)
            self.assertEqual(
                layout.require_within(layout.services / "xtts"),
                layout.services / "xtts",
            )


class ManagerMcpConfigCliTests(unittest.TestCase):
    def test_mcp_config_requires_explicit_credential_acknowledgement(self):
        output = StringIO()
        error = StringIO()
        with mock.patch(
            "pandrator_manager.cli.ManagerClient.ensure_running"
        ) as ensure_running, redirect_stdout(output), redirect_stderr(error):
            result = manager_cli_main(["mcp-config", "codex"])

        self.assertEqual(2, result)
        ensure_running.assert_not_called()
        self.assertNotIn("Authorization", output.getvalue())
        self.assertIn("--include-credential", error.getvalue())

    def test_mcp_config_uses_the_private_application_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            runtime = Path(directory) / "runtime-python"
            runtime.touch()
            rendered = "[mcp_servers.pandrator]\nurl = 'http://127.0.0.1:8099/mcp'\n"
            completed = subprocess.CompletedProcess(
                args=(),
                returncode=0,
                stdout=rendered,
                stderr="",
            )
            output = StringIO()
            with mock.patch(
                "pandrator_manager.cli.ManagerClient.ensure_running",
                return_value=mock.Mock(layout=layout),
            ), mock.patch(
                "pandrator_manager.cli.runtime_python",
                return_value=runtime,
            ), mock.patch(
                "pandrator_manager.cli.application_root",
                return_value=Path(directory),
            ), mock.patch(
                "pandrator_manager.cli.subprocess.run",
                return_value=completed,
            ) as run, redirect_stdout(output):
                result = manager_cli_main(
                    [
                        "--workspace",
                        directory,
                        "mcp-config",
                        "codex",
                        "--include-credential",
                    ]
                )

        self.assertEqual(0, result)
        self.assertEqual(rendered, output.getvalue())
        command = run.call_args.args[0]
        self.assertEqual(str(runtime), command[0])
        self.assertIn("managed-host-config", command)
        self.assertIn(str(layout.workspace), command)
        self.assertNotIn("Authorization", " ".join(command))

    def test_mcp_paths_wraps_the_managed_target_without_target_plumbing(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.mcp_configuration.parent.mkdir(parents=True)
            layout.mcp_configuration.write_text("{}", encoding="utf-8")
            source = Path(directory) / "Downloads"
            with mock.patch(
                "pandrator_manager.cli.ManagerClient.ensure_running",
                return_value=mock.Mock(layout=layout),
            ), mock.patch(
                "pandrator_manager.cli._run_application_mcp",
                return_value='{"saved": true}\n',
            ) as run, redirect_stdout(StringIO()):
                result = manager_cli_main(
                    [
                        "--workspace",
                        directory,
                        "mcp-paths",
                        "source-add",
                        "downloads",
                        str(source),
                    ]
                )

        self.assertEqual(0, result)
        self.assertEqual(
            (
                "target",
                "--config",
                str(layout.mcp_configuration),
                "source-root-add",
                "managed-local",
                "downloads",
                str(source.resolve()),
            ),
            run.call_args.args[1],
        )

    def test_mcp_paths_list_combines_sources_with_output_readiness(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.mcp_configuration.parent.mkdir(parents=True)
            layout.mcp_configuration.write_text("{}", encoding="utf-8")
            output = StringIO()
            with mock.patch(
                "pandrator_manager.cli.ManagerClient.ensure_running",
                return_value=mock.Mock(layout=layout),
            ), mock.patch(
                "pandrator_manager.cli._run_application_mcp",
                side_effect=(
                    '{"source_roots": [{"name": "downloads", "path": "/input"}]}',
                    (
                        '{"targets": [{"name": "managed-local", '
                        '"local_output_root_configured": true}]}'
                    ),
                ),
            ), redirect_stdout(output):
                result = manager_cli_main(
                    [
                        "--workspace",
                        directory,
                        "mcp-paths",
                        "list",
                    ]
                )

        self.assertEqual(0, result)
        payload = json.loads(output.getvalue())
        self.assertEqual("downloads", payload["source_roots"][0]["name"])
        self.assertTrue(payload["output_root_configured"])


class StateStoreTests(unittest.TestCase):
    def test_schema_and_typed_state_survive_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "manager.sqlite3"
            store = ManagerStore(database)
            application = create_application(
                directory,
                registry=builtin_registry(),
            )
            inspection = application.planner.inspect(
                "fish_speech",
                DesiredComponentState(compute=ComputeVariant.CPU),
            )
            revision = store.save_component(
                inspection,
                desired=DesiredComponentState(compute=ComputeVariant.CPU),
                bump_revision=True,
            )
            reopened = ManagerStore(database)
            desired, persisted = reopened.component_records()["fish_speech"]
            self.assertEqual(revision, 1)
            self.assertEqual(reopened.schema_version(), 6)
            self.assertEqual(reopened.configuration_revision(), 1)
            self.assertEqual(desired.compute, ComputeVariant.CPU)
            self.assertEqual(persisted.component_id, "fish_speech")

    def test_version_four_database_gains_browser_and_automation_state(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "manager.sqlite3"
            original = ManagerStore(database)
            original.set_setting("migration-fixture", {"preserved": True})
            with closing(sqlite3.connect(database)) as connection:
                connection.execute("DROP TABLE browser_sessions")
                connection.execute("DROP TABLE automation_enrollment_grants")
                connection.execute("DROP TABLE automation_tokens")
                connection.execute("DROP TABLE automation_clients")
                connection.execute(
                    "DELETE FROM schema_migrations WHERE version IN (5, 6)"
                )
                connection.commit()

            migrated = ManagerStore(database)
            with migrated.transaction() as connection:
                table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='browser_sessions'
                    """
                ).fetchone()
                automation_table = connection.execute(
                    """
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name='automation_clients'
                    """
                ).fetchone()

            self.assertEqual(6, migrated.schema_version())
            self.assertIsNotNone(table)
            self.assertIsNotNone(automation_table)
            self.assertEqual(
                {"preserved": True},
                migrated.setting("migration-fixture"),
            )

    def test_idempotency_rejects_reusing_a_key_for_another_request(self):
        from pandrator_manager.models import OperationRecord, OperationState

        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={"silero": DesiredComponentState()},
            )
            record = OperationRecord(
                id="operation-one",
                plan_id=plan.id,
                kind=OperationKind.INSTALL,
                state=OperationState.QUEUED,
            )
            created, is_new = application.store.create_operation(
                record,
                idempotency_key="stable-key",
                request_payload={"plan_id": plan.id},
            )
            repeated, repeated_is_new = application.store.create_operation(
                record,
                idempotency_key="stable-key",
                request_payload={"plan_id": plan.id},
            )
            self.assertTrue(is_new)
            self.assertFalse(repeated_is_new)
            self.assertEqual(created.id, repeated.id)
            with self.assertRaisesRegex(Exception, "different request"):
                application.store.create_operation(
                    record,
                    idempotency_key="stable-key",
                    request_payload={"plan_id": "different"},
                )

    def test_begin_operation_replays_idempotently_and_blocks_new_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            first_plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={"silero": DesiredComponentState()},
            )
            first, created = application.submit_operation(
                plan_id=first_plan.id,
                plan_digest=first_plan.digest,
                accepted_confirmations=(),
                idempotency_key="first-operation",
            )
            self.assertTrue(created)
            replay, replayed = application.submit_operation(
                plan_id=first_plan.id,
                plan_digest=first_plan.digest,
                accepted_confirmations=(),
                idempotency_key="first-operation",
            )
            self.assertFalse(replayed)
            self.assertEqual(first.id, replay.id)

            second_plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={"fish_speech": DesiredComponentState()},
            )
            with self.assertRaises(ConflictError) as raised:
                application.submit_operation(
                    plan_id=second_plan.id,
                    plan_digest=second_plan.digest,
                    accepted_confirmations=(),
                    idempotency_key="second-operation",
                )
            self.assertEqual(
                first.id,
                (raised.exception.details or {}).get("active_operation_id"),
            )
            with application.store.transaction() as connection:
                consumed = connection.execute(
                    "SELECT consumed_at FROM plans WHERE plan_id=?",
                    (second_plan.id,),
                ).fetchone()["consumed_at"]
            self.assertIsNone(consumed)


class RegistryAndPlanningTests(unittest.TestCase):
    def test_legacy_amd_adapter_disables_automatic_vulkan(self):
        definition = builtin_registry().definition("qwen_tts")
        context = ManagerContext(
            layout=WorkspaceLayout.from_value("fixture"),
            system="Windows",
            architecture="AMD64",
            environment={"SystemRoot": r"C:\Windows"},
        )
        with mock.patch(
            "pandrator_manager.components.host._graphics_descriptions",
            return_value=(
                (
                    "AMD Radeon(TM) Vega 8 Graphics "
                    r"PCI\VEN_1002&DEV_15DD"
                ),
            ),
        ), mock.patch(
            "pandrator_manager.components.host.shutil.which",
            return_value=None,
        ), mock.patch(
            "pandrator_manager.components.host.Path.is_file",
            return_value=True,
        ):
            resolved = resolve_auto_compute(context, definition)

        self.assertEqual(ComputeVariant.CPU, resolved)

    def test_polaris_display_is_not_masked_by_its_amd_audio_function(self):
        definition = builtin_registry().definition("qwen_tts")
        context = ManagerContext(
            layout=WorkspaceLayout.from_value("fixture"),
            system="Linux",
            architecture="x86_64",
            environment={},
        )
        with mock.patch(
            "pandrator_manager.components.host._graphics_descriptions",
            return_value=(
                (
                    "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
                    "[AMD/ATI] Ellesmere [Radeon RX 470/480/570/570X/580/580X/590]"
                ),
                (
                    "03:00.1 Audio device: Advanced Micro Devices, Inc. [AMD/ATI] "
                    "Ellesmere HDMI Audio"
                ),
            ),
        ), mock.patch(
            "pandrator_manager.components.host.shutil.which",
            return_value=None,
        ), mock.patch(
            "pandrator_manager.components.host.ctypes.util.find_library",
            return_value="libvulkan.so.1",
        ):
            resolved = resolve_auto_compute(context, definition)

        self.assertEqual(ComputeVariant.VULKAN, resolved)

    def test_qwen_auto_uses_q8_vulkan_but_avoids_f16_on_polaris(self):
        registry = builtin_registry()
        definition = registry.definition("qwen_tts")
        driver = registry.driver("qwen_tts")
        context = ManagerContext(
            layout=WorkspaceLayout.from_value("fixture"),
            system="Linux",
            architecture="x86_64",
            environment={},
        )
        descriptions = (
            (
                "03:00.0 VGA compatible controller: Advanced Micro Devices, Inc. "
                "[AMD/ATI] Ellesmere [Radeon RX 470/480/570/570X/580/580X/590]"
            ),
            (
                "03:00.1 Audio device: Advanced Micro Devices, Inc. [AMD/ATI] "
                "Ellesmere HDMI Audio"
            ),
        )
        with mock.patch(
            "pandrator_manager.components.host._graphics_descriptions",
            return_value=descriptions,
        ), mock.patch(
            "pandrator_manager.components.host.shutil.which",
            return_value=None,
        ), mock.patch(
            "pandrator_manager.components.host.ctypes.util.find_library",
            return_value="libvulkan.so.1",
        ):
            q8 = driver.resolve(
                context,
                definition,
                DesiredComponentState(
                    compute=ComputeVariant.AUTO,
                    quantization="q8_0",
                ),
            )
            f16 = driver.resolve(
                context,
                definition,
                DesiredComponentState(
                    compute=ComputeVariant.AUTO,
                    quantization="f16",
                ),
            )

        self.assertEqual(ComputeVariant.VULKAN, q8.compute)
        self.assertEqual(ComputeVariant.CPU, f16.compute)

    def test_local_speech_catalogue_matches_runtime_model_contracts(self):
        registry = builtin_registry()
        qwen = registry.definition("qwen_tts")
        qwen_initial = next(
            option for option in qwen.install_options
            if option.key == "initial_model"
        )
        self.assertEqual(
            ["base", "customvoice"],
            [choice.value for choice in qwen_initial.choices],
        )

        fish = registry.definition("fish_speech")
        fish_quantization = next(
            option for option in fish.install_options
            if option.key == "quantization"
        )
        self.assertIn(
            "q3_k",
            {choice.value for choice in fish_quantization.choices},
        )

        chatterbox = registry.definition("chatterbox")
        self.assertEqual(
            {
                "chatterbox-turbo",
                "chatterbox-multilingual",
                "chatterbox-en",
            },
            {model.id for model in chatterbox.models},
        )

    def test_qwen_legacy_model_choices_resolve_to_service_cli_values(self):
        registry = builtin_registry()
        definition = registry.definition("qwen_tts")
        driver = registry.driver("qwen_tts")
        context = ManagerContext(
            layout=WorkspaceLayout.from_value("fixture"),
            system="Linux",
            architecture="x86_64",
            environment={},
        )

        customvoice = driver.resolve(
            context,
            definition,
            DesiredComponentState(
                compute=ComputeVariant.CPU,
                quantization="q8_0",
                options={
                    "initial_model": "custom_voice",
                    "model_size": "1.7b",
                },
            ),
        )
        both = driver.resolve(
            context,
            definition,
            DesiredComponentState(
                compute=ComputeVariant.CPU,
                quantization="q8_0",
                options={
                    "initial_model": "both",
                    "model_size": "1.7b",
                },
            ),
        )

        self.assertEqual("customvoice", customvoice.options["initial_model"])
        self.assertEqual("base", both.options["initial_model"])

    def test_model_configuration_reaches_qwen_and_fish_bootstraps(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            for component_id in ("qwen_tts", "fish_speech"):
                slot = (
                    layout.services
                    / component_id
                    / "versions"
                    / "fixture"
                )
                slot.mkdir(parents=True)
                (slot / "pyproject.toml").write_text("", encoding="utf-8")
                (layout.services / component_id / "current.json").write_text(
                    json.dumps({"path": str(slot)}),
                    encoding="utf-8",
                )

            qwen = component_runtime_spec(
                layout,
                "qwen_tts",
                ResolvedComponentState(
                    compute=ComputeVariant.CPU,
                    quantization="f16",
                    platform="test",
                    options={
                        "initial_model": "customvoice",
                        "model_size": "1.7b",
                    },
                ),
            )
            fish = component_runtime_spec(
                layout,
                "fish_speech",
                ResolvedComponentState(
                    compute=ComputeVariant.VULKAN,
                    quantization="q3_k",
                    platform="test",
                ),
            )

        self.assertIsNotNone(qwen)
        self.assertEqual(
            "customvoice",
            qwen.arguments[qwen.arguments.index("--initial-model") + 1],
        )
        self.assertEqual(
            "1.7b",
            qwen.arguments[qwen.arguments.index("--model-size") + 1],
        )
        self.assertEqual(
            "f16",
            qwen.arguments[qwen.arguments.index("--quantization") + 1],
        )
        self.assertIsNotNone(fish)
        self.assertEqual("q3_k", fish.environment["FISHS2_MODEL_QUANT"])
        self.assertEqual("vulkan", fish.environment["FISHS2_BACKEND"])

    def test_rocm_requires_a_supported_gpu_agent_and_polaris_falls_back_to_cpu(self):
        definition = builtin_registry().definition("kokoro")
        context = ManagerContext(
            layout=WorkspaceLayout.from_value("fixture"),
            system="Linux",
            architecture="x86_64",
            environment={},
        )

        def executable(name):
            return "/usr/bin/rocminfo" if name == "rocminfo" else None

        with mock.patch(
            "pandrator_manager.components.host.shutil.which",
            side_effect=executable,
        ), mock.patch(
            "pandrator_manager.components.host.ctypes.util.find_library",
            return_value=None,
        ), mock.patch(
            "pandrator_manager.components.host.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/rocminfo"],
                0,
                stdout="  Name:                    gfx803\n",
            ),
        ):
            resolved = resolve_auto_compute(context, definition)

        self.assertEqual(ComputeVariant.CPU, resolved)

    def test_rocm_auto_selection_accepts_a_supported_gpu_agent(self):
        definition = builtin_registry().definition("kokoro")
        context = ManagerContext(
            layout=WorkspaceLayout.from_value("fixture"),
            system="Linux",
            architecture="x86_64",
            environment={},
        )

        def executable(name):
            return "/usr/bin/rocminfo" if name == "rocminfo" else None

        with mock.patch(
            "pandrator_manager.components.host.shutil.which",
            side_effect=executable,
        ), mock.patch(
            "pandrator_manager.components.host.ctypes.util.find_library",
            return_value=None,
        ), mock.patch(
            "pandrator_manager.components.host.subprocess.run",
            return_value=subprocess.CompletedProcess(
                ["/usr/bin/rocminfo"],
                0,
                stdout="  Name:                    gfx1100\n",
            ),
        ):
            resolved = resolve_auto_compute(context, definition)

        self.assertEqual(ComputeVariant.ROCM, resolved)

    def test_builtin_registry_has_stable_components_and_unique_runtime_ownership(self):
        definitions = builtin_registry().definitions()
        self.assertEqual(len(definitions), 13)
        self.assertIn("fish_speech", {definition.id for definition in definitions})
        ports = [
            definition.default_port
            for definition in definitions
            if definition.default_port
        ]
        self.assertEqual(len(ports), len(set(ports)))

    def test_marker_inspection_reports_validated_slot_revision_only(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            slot = layout.services / "fixture" / "versions" / "abc1234"
            slot.mkdir(parents=True)
            (slot / "marker.txt").write_text("managed", encoding="utf-8")
            (layout.services / "fixture" / "current.json").write_text(
                json.dumps(
                    {
                        "component_id": "fixture",
                        "version": "abc1234",
                        "path": str(slot),
                    }
                ),
                encoding="utf-8",
            )
            definition = ComponentDefinition(
                id="fixture",
                label="Fixture",
                source_markers=("marker.txt",),
                markers=("legacy-marker.txt",),
                compute_variants=(ComputeVariant.CPU,),
            )
            context = ManagerContext(
                layout=layout,
                system="Linux",
                architecture="x86_64",
                environment={},
            )
            inspection = MarkerComponentDriver().inspect(
                context,
                definition,
                DesiredComponentState(compute=ComputeVariant.CPU),
            )
            self.assertEqual(ComponentState.PRESENT, inspection.state)
            self.assertIsNone(inspection.installed_version)
            self.assertEqual("abc1234", inspection.installed_revision)

            (layout.services / "fixture" / "current.json").write_text(
                json.dumps({"path": str(slot)}),
                encoding="utf-8",
            )
            manual = MarkerComponentDriver().inspect(
                context,
                definition,
                DesiredComponentState(compute=ComputeVariant.CPU),
            )
            self.assertIsNone(manual.installed_version)
            self.assertIsNone(manual.installed_revision)

    def test_registry_rejects_duplicate_ports_paths_and_dependency_cycles(self):
        driver = MarkerComponentDriver()
        one = ComponentDefinition(
            id="one",
            label="One",
            compute_variants=(ComputeVariant.CPU,),
            owned_paths=("services/shared",),
            default_port=8123,
        )
        duplicate = ComponentDefinition(
            id="two",
            label="Two",
            compute_variants=(ComputeVariant.CPU,),
            owned_paths=("services/shared",),
            default_port=8123,
        )
        with self.assertRaisesRegex(ValueError, "share port"):
            ComponentRegistry((one, duplicate), (driver,))
        cyclic_one = one.model_copy(
            update={"dependencies": ("two",), "default_port": 8123}
        )
        cyclic_two = duplicate.model_copy(
            update={
                "dependencies": ("one",),
                "owned_paths": ("services/two",),
                "default_port": 8124,
            }
        )
        with self.assertRaisesRegex(ValueError, "cycle"):
            ComponentRegistry((cyclic_one, cyclic_two), (driver,))

    def test_plan_links_dependencies_and_rejects_stale_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            # Exercise graph composition independently of the production
            # qualification gate that currently marks fine-tuning remove-only.
            definitions = tuple(
                definition.model_copy(
                    update={
                        "supported_actions": (
                            "install",
                            "update",
                            "repair",
                            "remove",
                        )
                    }
                )
                if definition.id == "xtts_finetuning"
                else definition
                for definition in BUILTIN_COMPONENTS
            )
            application = create_application(
                directory,
                registry=ComponentRegistry(
                    definitions,
                    (MarkerComponentDriver(),),
                ),
            )
            desired = {
                "xtts_finetuning": DesiredComponentState(
                    compute=ComputeVariant.CPU
                )
            }
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired=desired,
            )
            task_ids = [task.id for task in plan.tasks]
            self.assertEqual(task_ids[0], "operation:preflight")
            self.assertEqual(task_ids[1], "runtime:pixi")
            self.assertIn("xtts:activate", task_ids)
            self.assertTrue(plan.preflight)
            first_trainer = next(
                task for task in plan.tasks if task.id == "xtts_finetuning:stage"
            )
            self.assertIn("xtts:validate-service", first_trainer.dependencies)
            first_xtts = next(task for task in plan.tasks if task.id == "xtts:stage")
            self.assertIn("operation:preflight", first_xtts.dependencies)
            self.assertIn("runtime:pixi", first_xtts.dependencies)
            self.assertIn("xtts", plan.desired)
            with self.assertRaises(RevisionConflict):
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired=desired,
                    expected_revision=99,
                )

    def test_pandrator_install_plan_starts_application_after_all_tasks(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "pandrator": DesiredComponentState(
                        options={"start_after_install": True}
                    )
                },
            )

        start = plan.tasks[-1]
        self.assertEqual("pandrator:start", start.id)
        self.assertEqual("start_application", start.kind)
        self.assertEqual(
            {task.id for task in plan.tasks[:-1]},
            set(start.dependencies),
        )

    def test_private_network_candidates_prefer_active_private_ipv4_interfaces(self):
        addresses = {
            "lo": [mock.Mock(family=socket.AF_INET, address="127.0.0.1")],
            "ethernet": [
                mock.Mock(family=socket.AF_INET, address="192.168.20.14")
            ],
            "offline": [
                mock.Mock(family=socket.AF_INET, address="10.10.10.10")
            ],
        }
        stats = {
            "lo": mock.Mock(isup=True),
            "ethernet": mock.Mock(isup=True),
            "offline": mock.Mock(isup=False),
        }
        with (
            mock.patch(
                "pandrator_manager.network.psutil.net_if_addrs",
                return_value=addresses,
            ),
            mock.patch(
                "pandrator_manager.network.psutil.net_if_stats",
                return_value=stats,
            ),
        ):
            candidates = private_network_candidates(8097)

        self.assertEqual(
            (
                {
                    "interface": "ethernet",
                    "address": "192.168.20.14",
                    "url": "http://192.168.20.14:8097",
                },
            ),
            candidates,
        )

    def test_preflight_rejects_missing_configured_ca_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "missing-ca.pem"
            with mock.patch.dict(
                os.environ,
                {"REQUESTS_CA_BUNDLE": str(missing)},
                clear=False,
            ):
                application = create_application(directory)
            with self.assertRaises(ManagerError) as raised:
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired={"silero": DesiredComponentState()},
                )
            self.assertEqual(raised.exception.code, "preflight_failed")
            checks = (raised.exception.details or {}).get("checks", [])
            self.assertTrue(
                any(check["code"] == "tls.requests_ca_bundle" for check in checks)
            )

    def test_preflight_rejects_source_install_in_offline_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            with self.assertRaises(ManagerError) as raised:
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired={
                        "silero": DesiredComponentState(
                            options={"offline": True}
                        )
                    },
                )
            self.assertEqual(raised.exception.code, "preflight_failed")
            checks = (raised.exception.details or {}).get("checks", [])
            self.assertTrue(
                any(check["code"] == "offline.silero" for check in checks)
            )

    def test_preflight_rejects_an_explicit_unavailable_compute_variant(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            with mock.patch(
                "pandrator_manager.preflight.require_compute_available",
                side_effect=ValueError(
                    "Fish Speech cannot use CUDA: no NVIDIA runtime was detected."
                ),
            ), self.assertRaises(ManagerError) as raised:
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired={
                        "fish_speech": DesiredComponentState(
                            compute=ComputeVariant.CUDA
                        )
                    },
                )

        self.assertEqual("preflight_failed", raised.exception.code)
        checks = (raised.exception.details or {}).get("checks", [])
        compute = next(
            check
            for check in checks
            if check["code"] == "compute.fish_speech"
        )
        self.assertEqual("error", compute["status"])
        self.assertEqual("cuda", compute["details"]["compute"])

    def test_preflight_reports_occupied_port_without_terminating_listener(self):
        with tempfile.TemporaryDirectory() as directory:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self.addCleanup(listener.close)
            if os.name == "nt":
                listener.setsockopt(
                    socket.SOL_SOCKET,
                    socket.SO_EXCLUSIVEADDRUSE,
                    1,
                )
            listener.bind(("127.0.0.1", 0))
            occupied_port = listener.getsockname()[1]
            definition = ComponentDefinition(
                id="fixture",
                label="Fixture",
                service_key="tts.fixture",
                source_markers=("run.py",),
                owned_paths=("services/fixture",),
                repo_url="https://example.invalid/fixture.git",
                default_port=occupied_port,
            )
            application = create_application(
                directory,
                registry=ComponentRegistry(
                    (definition,),
                    (MarkerComponentDriver(),),
                ),
            )
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={"fixture": DesiredComponentState()},
            )
            port_check = next(
                check
                for check in plan.preflight
                if check.code == f"port.{occupied_port}"
            )
            self.assertEqual(port_check.status, "warning")
            listener.listen(1)

    def test_probe_and_plan_core_do_not_import_qt(self):
        with tempfile.TemporaryDirectory() as directory:
            before = set(sys.modules)
            application = create_application(directory)
            application.probe(persist=False)
            application.plan(
                kind=OperationKind.INSTALL,
                desired={"silero": DesiredComponentState()},
                persist=False,
            )
            imported = set(sys.modules).difference(before)
            self.assertFalse(any(name.startswith("PyQt") for name in imported))

    def test_service_update_stops_before_replacement_and_validates_last(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ManagerContext(
                layout=WorkspaceLayout.from_value(directory)
            )
            definition = ComponentDefinition(
                id="fixture",
                label="Fixture",
                service_key="tts.fixture",
                source_markers=("run.py",),
                owned_paths=("services/fixture",),
                repo_url="https://example.invalid/fixture.git",
            )
            tasks = MarkerComponentDriver().plan_update(
                context,
                definition,
                DesiredComponentState(),
                ComponentInspection(
                    component_id="fixture",
                    state=ComponentState.PRESENT,
                ),
            )
            self.assertEqual(tasks[0].id, "fixture:stop")
            self.assertIn("fixture:stop", tasks[1].dependencies)
            self.assertEqual(tasks[-1].id, "fixture:validate-service")


class LegacyImporterTests(unittest.TestCase):
    def test_voxcpm_legacy_backend_preserves_explicit_cpu(self):
        self.assertEqual(
            LegacyImporter._compute("voxcpm", {"voxcpm_backend": "cpu"}),
            ComputeVariant.CPU,
        )
        self.assertEqual(
            LegacyImporter._compute("voxcpm", {}),
            ComputeVariant.CUDA,
        )

    def test_crispasr_uses_resolved_legacy_runtime_backend(self):
        self.assertEqual(
            LegacyImporter._compute(
                "crispasr",
                {
                    "crispasr_backend": "auto",
                    "crispasr_runtime_variant": "vulkan",
                },
            ),
            ComputeVariant.VULKAN,
        )
        self.assertEqual(
            LegacyImporter._compute(
                "crispasr",
                {
                    "crispasr_backend": "cuda",
                    "crispasr_runtime_variant": "unknown",
                },
            ),
            ComputeVariant.CUDA,
        )
        self.assertEqual(
            LegacyImporter._compute(
                "crispasr",
                {"crispasr_backend": "auto"},
            ),
            ComputeVariant.CPU,
        )

    def test_legacy_inspection_is_read_only_and_maps_fish_cpu_as_one_component(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.root.mkdir()
            fish = layout.root / "fishs2-cpp-fastapi"
            fish.mkdir()
            (fish / "run.py").write_text("", encoding="utf-8")
            (fish / "pyproject.toml").write_text("", encoding="utf-8")
            (layout.root / "config.json").write_text(
                json.dumps(
                    {
                        "fishs2_support": True,
                        "fishs2_gpu_support": False,
                        "fishs2_backend": "auto",
                        "fishs2_model_quant": "q6_k",
                    }
                ),
                encoding="utf-8",
            )
            layout.ensure_base_directories()
            store = ManagerStore(layout.database)
            context = ManagerContext(layout=layout)
            importer = LegacyImporter(context, store, builtin_registry())

            report = importer.inspect()

            self.assertTrue(report.valid)
            self.assertEqual(
                report.desired["fish_speech"].compute,
                ComputeVariant.CPU,
            )
            self.assertEqual(
                report.inspections["fish_speech"].state,
                ComponentState.PRESENT,
            )
            self.assertFalse((layout.state / "legacy").exists())
            revision = importer.apply(report, confirmed=True)
            self.assertEqual(revision, 1)
            self.assertTrue((layout.state / "legacy").is_dir())
            self.assertTrue(fish.is_dir())

    def test_known_legacy_data_directories_are_not_reported_as_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.root.mkdir()
            (layout.root / "config.json").write_text("{}", encoding="utf-8")
            for name in (
                "sessions",
                "artifacts",
                "uploads",
                "rvc_models",
            ):
                (layout.root / name).mkdir()
            (layout.root / "migration-web-v1.json").write_text(
                '{"status":"complete"}',
                encoding="utf-8",
            )
            (layout.root / ".flask-secret").write_text(
                "legacy-secret",
                encoding="utf-8",
            )
            layout.ensure_base_directories()
            importer = LegacyImporter(
                ManagerContext(layout=layout),
                ManagerStore(layout.database),
                builtin_registry(),
            )

            report = importer.inspect()

            self.assertIsNotNone(report)
            self.assertNotIn("sessions", report.unknown_paths)
            self.assertNotIn("artifacts", report.unknown_paths)
            self.assertNotIn("uploads", report.unknown_paths)
            self.assertNotIn("rvc_models", report.unknown_paths)
            self.assertNotIn(
                "migration-web-v1.json",
                report.unknown_paths,
            )
            self.assertNotIn(".flask-secret", report.unknown_paths)

    def test_legacy_data_reconciliation_is_additive_conflict_safe_and_reversible(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            legacy_app = layout.root / "Pandrator"
            output = legacy_app / "Outputs" / "Book"
            output.mkdir(parents=True)
            (output / "chapter.wav").write_bytes(b"audio")
            (layout.root / "config.json").write_text(
                '{"legacy": true}',
                encoding="utf-8",
            )
            (layout.data / "config.json").write_text(
                '{"current": true}',
                encoding="utf-8",
            )

            inventory = legacy_data_inventory(layout)
            result = reconcile_legacy_data(
                layout,
                inventory=inventory,
            )

            self.assertEqual(
                (layout.data / "Outputs" / "Book" / "chapter.wav").read_bytes(),
                b"audio",
            )
            self.assertTrue((output / "chapter.wav").is_file())
            conflict = (
                layout.data
                / "legacy-conflicts"
                / "workspace"
                / "config.json"
            )
            self.assertEqual(conflict.read_text(encoding="utf-8"), '{"legacy": true}')
            self.assertEqual(len(result["conflicts"]), 1)

            rollback_legacy_data(layout, result)
            self.assertFalse(
                (layout.data / "Outputs" / "Book" / "chapter.wav").exists()
            )
            self.assertFalse(conflict.exists())
            self.assertEqual(
                (layout.data / "config.json").read_text(encoding="utf-8"),
                '{"current": true}',
            )
            self.assertTrue((output / "chapter.wav").is_file())

    def test_legacy_inventory_revision_detects_changes_after_plan_review(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            output = layout.root / "Outputs" / "chapter.wav"
            output.parent.mkdir()
            output.write_bytes(b"first")

            first = legacy_data_inventory(layout)
            output.write_bytes(b"changed content")
            second = legacy_data_inventory(layout)

            self.assertNotEqual(
                first.revision_digest,
                second.revision_digest,
            )
            self.assertNotEqual(
                first.as_dict(),
                second.as_dict(),
            )

    def test_legacy_import_discovers_disabled_components_and_validated_shared_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.root.mkdir()
            fish = layout.root / "fishs2-cpp-fastapi"
            fish.mkdir()
            (fish / "run.py").write_text("", encoding="utf-8")
            (fish / "pyproject.toml").write_text("", encoding="utf-8")
            pixi = layout.root / ".pixi-home" / "bin"
            pixi.mkdir(parents=True)
            (pixi / ("pixi.exe" if os.name == "nt" else "pixi")).write_bytes(
                b"pixi"
            )
            (layout.root / ".pixi-cache").mkdir()
            (layout.root / "config.json").write_text(
                json.dumps({"fishs2_support": False}),
                encoding="utf-8",
            )
            packaging = layout.root / "packaging_layout.json"
            packaging.write_text(
                json.dumps(
                    {
                        "layout_version": 1,
                        "shared_paths": [
                            ".pixi-home",
                            ".pixi-cache",
                            "../outside",
                        ],
                        "component_paths": {
                            "fishs2": ["fishs2-cpp-fastapi"],
                        },
                    }
                ),
                encoding="utf-8",
            )
            layout.ensure_base_directories()
            store = ManagerStore(layout.database)
            importer = LegacyImporter(
                ManagerContext(layout=layout),
                store,
                builtin_registry(),
            )

            report = importer.inspect()

            self.assertIn("fish_speech", report.positively_identified)
            self.assertIn("fish_speech", report.desired)
            self.assertTrue(
                any(
                    "disabled" in warning
                    for warning in report.warnings
                )
            )
            owned = {Path(item.path).name for item in report.ownership}
            self.assertIn("fishs2-cpp-fastapi", owned)
            self.assertIn(".pixi-home", owned)
            self.assertIn(".pixi-cache", owned)
            self.assertNotIn("outside", owned)

            reviewed_digest = report.source_digest
            packaging.write_text(
                packaging.read_text(encoding="utf-8") + " ",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "changed"):
                importer.apply(report, confirmed=True)
            self.assertNotEqual(
                reviewed_digest,
                importer.inspect().source_digest,
            )

            current = importer.inspect()
            importer.apply(current, confirmed=True)
            records = {
                Path(record["path"]).name: record
                for record in store.owned_paths()
            }
            self.assertEqual(
                records["fishs2-cpp-fastapi"]["owner_id"],
                "fish_speech",
            )
            self.assertEqual(
                records[".pixi-home"]["owner_kind"],
                "legacy_shared",
            )

    def test_application_import_requires_reviewed_digest_and_is_idempotent(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            layout = application.context.layout
            (layout.root / "config.json").write_text(
                json.dumps({"silero_support": False}),
                encoding="utf-8",
            )
            legacy_database = layout.root / "pandrator.sqlite3"
            with closing(sqlite3.connect(legacy_database)) as database:
                database.execute("CREATE TABLE fixture(value TEXT NOT NULL)")
                database.execute("INSERT INTO fixture VALUES ('legacy')")
                database.commit()
            (layout.root / "migration-web-v1.json").write_text(
                '{"version":1,"status":"complete"}',
                encoding="utf-8",
            )
            (layout.root / ".flask-secret").write_text(
                "existing-cookie-secret",
                encoding="utf-8",
            )
            report = application.legacy_report()
            self.assertIsNotNone(report)
            self.assertTrue(report.valid)

            imported = application.import_legacy(
                source_digest=report.source_digest,
                confirmed=True,
            )
            replay = application.import_legacy(
                source_digest=report.source_digest,
                confirmed=True,
            )

            self.assertEqual(imported["status"], "imported")
            self.assertEqual(replay["status"], "already_imported")
            self.assertEqual(
                imported["configuration_revision"],
                replay["configuration_revision"],
            )
            with closing(
                sqlite3.connect(layout.data / "pandrator.sqlite3")
            ) as database:
                self.assertEqual(
                    database.execute(
                        "SELECT value FROM fixture"
                    ).fetchone(),
                    ("legacy",),
                )
            self.assertIn(
                str(layout.data / "pandrator.sqlite3"),
                imported["data_reconciliation"]["created"],
            )
            self.assertEqual(
                (layout.data / "migration-web-v1.json").read_text(
                    encoding="utf-8"
                ),
                '{"version":1,"status":"complete"}',
            )
            self.assertEqual(
                (layout.data / ".flask-secret").read_text(
                    encoding="utf-8"
                ),
                "existing-cookie-secret",
            )
            self.assertEqual(
                imported["data_reconciliation"],
                replay["data_reconciliation"],
            )

    def test_malformed_legacy_config_is_quarantined_only_after_confirmation(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            source = application.context.layout.root / "config.json"
            source.write_text("{broken", encoding="utf-8")
            importer = LegacyImporter(
                application.context,
                application.store,
                application.registry,
            )
            report = importer.inspect()
            self.assertFalse(report.valid)
            self.assertFalse((application.context.layout.state / "quarantine").exists())
            with self.assertRaisesRegex(ValueError, "confirmation"):
                importer.apply(report, confirmed=False)
            importer.apply(report, confirmed=True)
            self.assertTrue((application.context.layout.state / "quarantine").is_dir())
            self.assertEqual(source.read_text(encoding="utf-8"), "{broken")


class CoreAdapterTests(unittest.TestCase):
    def test_rvc_runtime_uses_platform_script_and_passes_shared_pixi_path(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            versions = layout.services / "rvc" / "versions"
            slot = versions / "fixture"
            slot.mkdir(parents=True)
            script = slot / ("run.bat" if os.name == "nt" else "run.sh")
            script.write_text("", encoding="utf-8")
            (layout.services / "rvc" / "current.json").write_text(
                json.dumps({"path": str(slot)}),
                encoding="utf-8",
            )
            spec = component_runtime_spec(
                layout,
                "rvc",
                ResolvedComponentState(
                    compute=ComputeVariant.CPU,
                    platform="test",
                ),
            )
            self.assertIsNotNone(spec)
            self.assertIn(str(script), spec.arguments)
            self.assertIn("--pixi-path", spec.arguments)
            self.assertIn(
                str(layout.bin / ("pixi.exe" if os.name == "nt" else "pixi")),
                spec.arguments,
            )

    def test_artifact_spec_and_extractor_reject_unsafe_inputs(self):
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            ArtifactSpec(url="http://example.test/file", sha256="0" * 64)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive = root / "unsafe.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("../outside.txt", "unsafe")
            with self.assertRaisesRegex(ValueError, "unsafe path"):
                SafeExtractor().extract(archive, root / "output")
            self.assertFalse((root / "outside.txt").exists())

    def test_verified_download_cache_supports_offline_reuse(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "artifact.bin"
            target.write_bytes(b"cached release")
            spec = ArtifactSpec(
                url="https://example.invalid/artifact.bin",
                sha256=hashlib.sha256(b"cached release").hexdigest(),
                size_bytes=len(b"cached release"),
            )
            selected = ArtifactDownloader().download(
                spec,
                target,
                offline=True,
            )
            self.assertEqual(selected, target.resolve())
            target.write_bytes(b"tampered")
            with self.assertRaisesRegex(FileNotFoundError, "cache"):
                ArtifactDownloader().download(
                    spec,
                    target,
                    offline=True,
                )

    def test_artifact_downloader_uses_custom_ca_and_rejects_http_redirect(self):
        with tempfile.TemporaryDirectory() as directory:
            ca_bundle = Path(directory) / "company.pem"
            ca_bundle.write_bytes(Path(certifi.where()).read_bytes())
            session = mock.Mock()
            redirect = mock.Mock(
                status_code=302,
                headers={"Location": "http://example.invalid/artifact.bin"},
            )
            session.get.return_value = redirect
            downloader = ArtifactDownloader(
                session=session,
                environment={"PANDRATOR_CA_BUNDLE": str(ca_bundle)},
            )

            with self.assertRaisesRegex(ValueError, "left HTTPS"):
                downloader._open_https("https://example.invalid/artifact.bin")

            redirect.close.assert_called_once_with()
            session.get.assert_called_once_with(
                "https://example.invalid/artifact.bin",
                stream=True,
                timeout=(30, 300),
                allow_redirects=False,
                verify=str(ca_bundle.resolve()),
            )

    def test_command_runner_bounds_output_and_reaps_timed_out_tree(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "child.pid"
            code = (
                "import subprocess,sys,time,pathlib;"
                "child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(60)']);"
                f"pathlib.Path({str(marker)!r}).write_text(str(child.pid));"
                "time.sleep(60)"
            )
            runner = CommandRunner()
            with self.assertRaises(subprocess.TimeoutExpired):
                runner.run(
                    CommandSpec(
                        argv=(sys.executable, "-c", code),
                        timeout_seconds=1.5,
                        label="timeout-test",
                    )
                )
            child_pid = int(marker.read_text(encoding="utf-8"))
            deadline = time.monotonic() + 5
            while psutil.pid_exists(child_pid) and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertFalse(psutil.pid_exists(child_pid))

            result = runner.run(
                CommandSpec(
                    argv=(sys.executable, "-c", "print('x' * 10000)"),
                    output_limit_bytes=1024,
                )
            )
            self.assertTrue(result.output_truncated)
            self.assertLessEqual(len(result.stdout.encode("utf-8")), 1024)

    def test_cancelled_command_never_starts(self):
        cancellation = CancellationToken()
        cancellation.request()
        with self.assertRaises(CancellationRequested):
            CommandRunner(cancellation=cancellation).run(
                CommandSpec(argv=(sys.executable, "-c", "pass"))
            )

    def test_pixi_manifest_is_complete_and_first_install_is_not_locked(self):
        manifest = PixiEnvironmentManager.manifest(
            PixiEnvironmentSpec(
                name="example",
                python="3.11",
                conda_packages=("ffmpeg", "numpy=1.26.4"),
                pypi_packages=("requests>=2.32,<3",),
            )
        )
        self.assertIn('python = "3.11"', manifest)
        self.assertIn('"numpy" = "1.26.4"', manifest)
        self.assertIn('"requests" = "<3,>=2.32"', manifest)

    def test_pixi_bootstrap_selects_qualified_platform_assets(self):
        self.assertEqual(
            pixi_asset_for("Windows", "AMD64").member,
            "pixi.exe",
        )
        self.assertEqual(
            pixi_asset_for("Linux", "arm64").architecture,
            "aarch64",
        )
        with self.assertRaises(ManagerError) as raised:
            pixi_asset_for("Darwin", "arm64")
        self.assertEqual(
            raised.exception.code,
            "unsupported_runtime_tool_platform",
        )

    def test_pixi_bootstrap_promotes_cached_verified_archive_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            archive = Path(directory) / "pixi-fixture.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("pixi.exe", b"fixture-pixi")
            payload = archive.read_bytes()
            asset = PixiAsset(
                system="windows",
                architecture="x86_64",
                url="https://example.invalid/pixi-fixture.zip",
                sha256=hashlib.sha256(payload).hexdigest(),
                member="pixi.exe",
            )
            cache = (
                layout.cache
                / "artifacts"
                / f"pixi-{PIXI_VERSION}"
                / asset.filename
            )
            cache.parent.mkdir(parents=True)
            cache.write_bytes(payload)
            runner = mock.Mock()
            runner.run.return_value = mock.Mock(
                stdout=f"pixi {PIXI_VERSION}",
                stderr="",
            )
            bootstrapper = PixiBootstrapper(
                ManagerContext(
                    layout=layout,
                    system="Windows",
                    architecture="AMD64",
                    environment={},
                ),
                runner=runner,
                asset=asset,
            )
            operation_staging = layout.staging / "operation"
            operation_backup = layout.backups / "operation"

            result = bootstrapper.ensure(
                operation_staging,
                operation_backup,
                offline=True,
            )

            self.assertTrue(result["changed"])
            self.assertEqual(result["version"], PIXI_VERSION)
            self.assertEqual(
                bootstrapper.target.read_bytes(),
                b"fixture-pixi",
            )
            self.assertEqual(
                result["ownership"]["owner_kind"],
                "runtime_tool",
            )
            bootstrapper.rollback(operation_staging)
            self.assertFalse(bootstrapper.target.exists())

    def test_pixi_bootstrap_requires_ownership_to_replace_and_restores_previous(self):
        with tempfile.TemporaryDirectory() as directory:
            layout = WorkspaceLayout.from_value(directory)
            layout.ensure_base_directories()
            archive = Path(directory) / "pixi-fixture.zip"
            with zipfile.ZipFile(archive, "w") as handle:
                handle.writestr("pixi.exe", b"new-pixi")
            payload = archive.read_bytes()
            asset = PixiAsset(
                system="windows",
                architecture="x86_64",
                url="https://example.invalid/pixi-fixture.zip",
                sha256=hashlib.sha256(payload).hexdigest(),
                member="pixi.exe",
            )
            cache = (
                layout.cache
                / "artifacts"
                / f"pixi-{PIXI_VERSION}"
                / asset.filename
            )
            cache.parent.mkdir(parents=True)
            cache.write_bytes(payload)
            context = ManagerContext(
                layout=layout,
                system="Windows",
                architecture="AMD64",
                environment={},
            )
            runner = mock.Mock()
            runner.run.return_value = mock.Mock(
                stdout="pixi 0.1.0",
                stderr="",
            )
            bootstrapper = PixiBootstrapper(
                context,
                runner=runner,
                asset=asset,
            )
            bootstrapper.target.write_bytes(b"old-pixi")
            operation_staging = layout.staging / "operation"
            operation_backup = layout.backups / "operation"
            with self.assertRaises(ManagerError) as raised:
                bootstrapper.ensure(
                    operation_staging,
                    operation_backup,
                    offline=True,
                )
            self.assertEqual(raised.exception.code, "runtime_tool_conflict")
            self.assertEqual(bootstrapper.target.read_bytes(), b"old-pixi")

            runner.run.side_effect = [
                mock.Mock(stdout="pixi 0.1.0", stderr=""),
                mock.Mock(stdout=f"pixi {PIXI_VERSION}", stderr=""),
            ]
            result = bootstrapper.ensure(
                operation_staging,
                operation_backup,
                replace_existing=True,
                offline=True,
            )
            self.assertTrue(result["changed"])
            self.assertEqual(bootstrapper.target.read_bytes(), b"new-pixi")
            bootstrapper.rollback(operation_staging)
            self.assertEqual(bootstrapper.target.read_bytes(), b"old-pixi")


if __name__ == "__main__":
    unittest.main()
