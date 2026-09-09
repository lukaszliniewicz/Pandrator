import json
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from pandrator_manager.application import create_application
from pandrator_manager.components import builtin_registry
from pandrator_manager.components.audiocpp import (
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_PORT,
    AUDIO_CPP_VERSION,
    MODEL_PACKAGES,
    PANDRATOR_AUDIO_CPP_RELEASE_BASE,
    SUPPORTED_MODEL_IDS,
    resolve_assets,
    server_config,
)
from pandrator_manager.components.catalog import PRESENTATIONS
from pandrator_manager.components.slots import (
    active_component_path,
    component_container,
    component_pointer,
)
from pandrator_manager.context import CancellationToken, ManagerContext, WorkspaceLayout
from pandrator_manager.errors import ManagerError
from pandrator_manager.models import (
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
)
from pandrator_manager.operations.handlers import (
    FilesystemTaskHandler,
    OperationTaskContext,
)
from pandrator_manager.tls import select_ca_bundle


def _write_audio_cpp_archive(
    archive: Path,
    system: str,
    package_ids: list[str],
) -> None:
    server_name = "audiocpp_server.exe" if system.casefold() == "windows" else "audiocpp_server"
    packages_by_family: dict[str, list[dict[str, object]]] = {}
    for package_id in package_ids:
        package = MODEL_PACKAGES[package_id]
        packages_by_family.setdefault(package.family, []).append(
            {"id": package.id, "download": {}}
        )
    with zipfile.ZipFile(archive, "w") as output:
        output.writestr(server_name, "#!/bin/sh\n")
        output.writestr("tools/model_manager_v2.py", "#!/usr/bin/env python3\n")
        for family, packages in packages_by_family.items():
            output.writestr(
                f"model_specs/{family}.json",
                json.dumps(
                    {
                        "family": family,
                        "package_defaults": {
                            "download": {
                                "kind": "huggingface_snapshot",
                                "repo": "audio-cpp/audio.cpp-gguf",
                                "revision": "main",
                            }
                        },
                        "packages": packages,
                    }
                ),
            )


class AudioCppManagerTests(unittest.TestCase):
    def test_qwen_voicedesign_package_is_pinned_as_vdes(self):
        package = MODEL_PACKAGES["qwen3_tts_1_7b_voicedesign_q8_0"]
        presentation = PRESENTATIONS["audio_cpp"]
        voicedesign = next(
            item
            for item in presentation.models
            if item.id == "qwen3_tts_1_7b_voicedesign_q8_0"
        )

        self.assertIn(package.id, SUPPORTED_MODEL_IDS)
        self.assertEqual("qwen3_tts", package.family)
        self.assertEqual(
            "Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF", package.target_directory
        )
        self.assertEqual(
            ("qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf",), package.files
        )
        self.assertEqual("vdes", package.task)
        self.assertEqual(
            "1bcef9a8c021072fca40e00498e00af9091fbe6d3ae4f87567cfee885d6c7554",
            package.sha256[0],
        )
        self.assertEqual(2_816_988_960, voicedesign.estimated_download_bytes)
        self.assertEqual("Apache-2.0", voicedesign.license_name)
        self.assertIn("Qwen3-TTS-12Hz-1.7B-VoiceDesign", voicedesign.license_url)

        config = server_config(ComputeVariant.CPU, [package.id])
        self.assertEqual(
            {
                "id": package.id,
                "family": "qwen3_tts",
                "path": (
                    "models/Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF/"
                    "qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf"
                ),
                "task": "vdes",
                "mode": "offline",
            },
            config["models"][0],
        )

    def test_breeze_package_is_pinned_and_discloses_noncommercial_terms(self):
        package = MODEL_PACKAGES["breeze_tts_2_q8_0"]
        presentation = PRESENTATIONS["audio_cpp"]
        breeze = next(
            item for item in presentation.models if item.id == "breeze_tts_2_q8_0"
        )

        self.assertIn(package.id, SUPPORTED_MODEL_IDS)
        self.assertEqual("breeze_tts", package.family)
        self.assertEqual("tts", package.task)
        self.assertEqual(
            "0de52d61560f9f6b2dfeca79f9100f8fce0c2b17c52ec30622e23e150df1ad88",
            package.sha256[0],
        )
        self.assertEqual(5_079_668_352, breeze.estimated_download_bytes)
        self.assertIn("Non-Commercial", breeze.license_name)
        self.assertIn("commercial use requires", breeze.usage_note)

    def test_server_config_is_local_lazy_and_covers_every_supported_package(self):
        config = server_config(ComputeVariant.VULKAN, list(SUPPORTED_MODEL_IDS))

        self.assertEqual("127.0.0.1", config["host"])
        self.assertEqual(AUDIO_CPP_PORT, config["port"])
        self.assertEqual("vulkan", config["backend"])
        self.assertEqual(0, config["device"])
        self.assertEqual(4, config["threads"])
        self.assertFalse(config["ui"])
        self.assertFalse(config["ui_management"])
        self.assertTrue(config["lazy_load"])
        self.assertEqual(1, config["max_loaded_models"])
        self.assertEqual(
            list(SUPPORTED_MODEL_IDS), [item["id"] for item in config["models"]]
        )
        pocket = next(
            item for item in config["models"] if item["id"].startswith("pocket_tts")
        )
        self.assertEqual("english", pocket["load_options"]["language"])
        firered = next(
            item for item in config["models"] if item["id"].startswith("fireredtts3")
        )
        self.assertEqual("clon", firered["task"])
        breeze = next(
            item for item in config["models"] if item["id"].startswith("breeze_tts")
        )
        self.assertEqual("breeze_tts", breeze["family"])
        self.assertEqual("tts", breeze["task"])

    def test_linux_cuda_resolves_to_the_pandrator_pinned_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ManagerContext(
                layout=WorkspaceLayout.from_value(directory),
                system="Linux",
                architecture="x86_64",
                environment={"CUDA_HOME": directory},
            )
            definition = builtin_registry().definition("audio_cpp")
            assets, effective = resolve_assets(context, ComputeVariant.CUDA, definition)

        self.assertEqual(ComputeVariant.CUDA, effective)
        self.assertEqual(1, len(assets))
        self.assertEqual("cuda_binary", assets[0].kind)
        self.assertEqual("audio.cpp-v0.7.2-linux-x86_64-cuda12.tar.gz", assets[0].name)
        self.assertEqual(
            "fb0f082a1226f38bc0a2ab1373891012243959d6a497df904a1498cbadcbc378",
            assets[0].sha256,
        )
        self.assertEqual(PANDRATOR_AUDIO_CPP_RELEASE_BASE, assets[0].release_base)
        self.assertEqual(
            f"{PANDRATOR_AUDIO_CPP_RELEASE_BASE}/{assets[0].name}", assets[0].url
        )

    def test_plan_requires_model_selection(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            with self.assertRaisesRegex(ValueError, "models.*nonempty"):
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired={
                        "audio_cpp": DesiredComponentState(compute=ComputeVariant.CPU)
                    },
                    persist=False,
                )

    def test_plan_sizes_only_selected_models(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={"models": ["qwen3_tts_1_7b_base_q8_0"]},
                    )
                },
                persist=False,
            )

        stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
        self.assertEqual(["qwen3_tts_1_7b_base_q8_0"], stage.inputs["models"])
        self.assertEqual("cpu", stage.inputs["effective_compute"])
        self.assertLess(stage.estimated_download_bytes, 4 * 1024**3)
        self.assertNotIn("runtime:pixi", {task.id for task in plan.tasks})

    def test_offline_plan_fails_before_model_manager_can_use_network(self):
        with tempfile.TemporaryDirectory() as directory:
            application = create_application(directory)
            with self.assertRaises(ManagerError) as raised:
                application.plan(
                    kind=OperationKind.INSTALL,
                    desired={
                        "audio_cpp": DesiredComponentState(
                            compute=ComputeVariant.CPU,
                            options={
                                "models": ["qwen3_tts_1_7b_base_q8_0"],
                                "offline": True,
                            },
                        )
                    },
                    persist=False,
                )

        self.assertEqual("preflight_failed", raised.exception.code)
        offline = next(
            check
            for check in raised.exception.details["checks"]
            if check["code"] == "offline.audio_cpp"
        )
        self.assertFalse(offline["details"]["model_cache_supported"])

    def test_staging_extracts_runtime_installs_models_and_rechecks_model_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = create_application(root / "workspace")
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={"models": ["qwen3_tts_1_7b_base_q8_0"]},
                    )
                },
                persist=False,
            )
            stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
            archive = root / "audio-cpp-runtime.zip"
            server_name = (
                "audiocpp_server.exe"
                if application.context.system.casefold() == "windows"
                else "audiocpp_server"
            )
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(server_name, "#!/bin/sh\n")
                output.writestr("tools/model_manager_v2.py", "#!/usr/bin/env python3\n")
                output.writestr(
                    "model_specs/qwen3_tts.json",
                    json.dumps(
                        {
                            "family": "qwen3_tts",
                            "package_defaults": {
                                "download": {
                                    "kind": "huggingface_snapshot",
                                    "repo": "audio-cpp/audio.cpp-gguf",
                                    "revision": "main",
                                }
                            },
                            "packages": [
                                {
                                    "id": "qwen3_tts_1_7b_base_q8_0",
                                    "download": {},
                                }
                            ],
                        }
                    ),
                )

            execution = OperationTaskContext(
                context=application.context,
                store=application.store,
                registry=application.registry,
                supervisor=None,
                operation=SimpleNamespace(id="audio-cpp-test"),
                plan=plan,
                prior_results={},
                cancellation=CancellationToken(),
            )

            def install_model(spec):
                argv = list(spec.argv)
                package = MODEL_PACKAGES[argv[3]]
                models_root = Path(argv[argv.index("--models-root") + 1])
                for path in package.required_paths(models_root):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"model")
                package.marker_path(models_root).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "resolved_revision": "fixture-revision",
                            "files": {package.files[0]: {"etag": "fixture"}},
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            handler = FilesystemTaskHandler()
            with (
                mock.patch(
                    "pandrator_manager.operations.handlers.ArtifactDownloader.download",
                    return_value=archive,
                ),
                mock.patch(
                    "pandrator_manager.operations.handlers.CommandRunner.run",
                    side_effect=install_model,
                ) as run,
                mock.patch.object(
                    FilesystemTaskHandler,
                    "_sha256_file",
                    return_value=MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"].sha256[0],
                ),
            ):
                result = handler.execute(execution, stage)
                self.assertEqual(
                    f"audio-cpp-{AUDIO_CPP_VERSION}-cpu-audio-cpp-test",
                    result["revision"],
                )
                target = Path(result["staged_path"])
                config = json.loads(
                    (target / "server.json").read_text(encoding="utf-8")
                )
                marker = MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"].marker_path(
                    target / "models"
                )
                provenance = json.loads(marker.read_text(encoding="utf-8"))[
                    "provenance"
                ]
                self.assertEqual("qwen3_tts_1_7b_base_q8_0", config["models"][0]["id"])
                self.assertTrue(provenance["digest_verified"])
                self.assertEqual(
                    AUDIO_CPP_MODEL_REVISION, provenance["requested_revision"]
                )
                staged_spec = json.loads(
                    (target / "model_specs" / "qwen3_tts.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    AUDIO_CPP_MODEL_REVISION,
                    staged_spec["packages"][0]["download"]["revision"],
                )
                self.assertEqual(1, run.call_count)
                model_install = run.call_args.args[0]
                self.assertEqual(
                    str(select_ca_bundle(application.context.environment).path),
                    model_install.env["SSL_CERT_FILE"],
                )

                required = MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"].required_paths(
                    target / "models"
                )[0]
                required.unlink()
                retried = handler.execute(execution, stage)

            self.assertFalse(retried["reused"])
            self.assertEqual(result["revision"], retried["revision"])
            self.assertEqual(2, run.call_count)

    def test_audio_cpp_stage_revision_is_stable_for_reuse_and_scoped_to_operation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = create_application(root / "workspace")
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={"models": ["qwen3_tts_1_7b_base_q8_0"]},
                    )
                },
                persist=False,
            )
            stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
            archive = root / "audio-cpp-runtime.zip"
            _write_audio_cpp_archive(
                archive,
                application.context.system,
                ["qwen3_tts_1_7b_base_q8_0"],
            )

            def install_model(spec):
                argv = list(spec.argv)
                package = MODEL_PACKAGES[argv[3]]
                models_root = Path(argv[argv.index("--models-root") + 1])
                for path in package.required_paths(models_root):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"model")
                package.marker_path(models_root).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "resolved_revision": "fixture-revision",
                            "files": {
                                filename: {"etag": "fixture"}
                                for filename in package.files
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            handler = FilesystemTaskHandler()
            first_execution = OperationTaskContext(
                context=application.context,
                store=application.store,
                registry=application.registry,
                supervisor=None,
                operation=SimpleNamespace(id="audio-cpp-first"),
                plan=plan,
                prior_results={},
                cancellation=CancellationToken(),
            )
            expected_sha256 = MODEL_PACKAGES[
                "qwen3_tts_1_7b_base_q8_0"
            ].sha256[0]
            with (
                mock.patch(
                    "pandrator_manager.operations.handlers.ArtifactDownloader.download",
                    return_value=archive,
                ) as download,
                mock.patch(
                    "pandrator_manager.operations.handlers.CommandRunner.run",
                    side_effect=install_model,
                ),
                mock.patch.object(
                    FilesystemTaskHandler,
                    "_sha256_file",
                    return_value=expected_sha256,
                ),
            ):
                fresh = handler.execute(first_execution, stage)
                reused = handler.execute(first_execution, stage)

            self.assertFalse(fresh["reused"])
            self.assertTrue(reused["reused"])
            self.assertEqual(fresh["revision"], reused["revision"])
            self.assertEqual(
                f"audio-cpp-{AUDIO_CPP_VERSION}-cpu-audio-cpp-first",
                fresh["revision"],
            )
            self.assertEqual(1, download.call_count)

            second_operation_id = "audio-cpp-second"
            second_target = (
                application.context.layout.staging
                / second_operation_id
                / "audio_cpp"
                / "source"
            )
            shutil.copytree(fresh["staged_path"], second_target)
            second_execution = OperationTaskContext(
                context=application.context,
                store=application.store,
                registry=application.registry,
                supervisor=None,
                operation=SimpleNamespace(id=second_operation_id),
                plan=plan,
                prior_results={},
                cancellation=CancellationToken(),
            )
            with (
                mock.patch(
                    "pandrator_manager.operations.handlers.ArtifactDownloader.download",
                    side_effect=AssertionError("reused staging must not download"),
                ),
                mock.patch.object(
                    FilesystemTaskHandler,
                    "_sha256_file",
                    return_value=expected_sha256,
                ),
            ):
                second = handler.execute(second_execution, stage)

            self.assertTrue(second["reused"])
            self.assertEqual(
                f"audio-cpp-{AUDIO_CPP_VERSION}-cpu-audio-cpp-second",
                second["revision"],
            )
            self.assertNotEqual(fresh["revision"], second["revision"])

    def test_audio_cpp_activation_uses_new_same_runtime_slot_and_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = create_application(root / "workspace")
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={
                            "models": [
                                "qwen3_tts_1_7b_base_q8_0",
                                "voxcpm2_q8_0",
                            ]
                        },
                    )
                },
                persist=False,
            )
            stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
            activate = next(task for task in plan.tasks if task.kind == "activate_component")
            package_ids = ["qwen3_tts_1_7b_base_q8_0", "voxcpm2_q8_0"]
            archive = root / "audio-cpp-runtime.zip"
            _write_audio_cpp_archive(
                archive,
                application.context.system,
                package_ids,
            )

            old_revision = f"audio-cpp-{AUDIO_CPP_VERSION}-cpu"
            container = component_container(application.context.layout, "audio_cpp")
            old_slot = container / "versions" / old_revision
            old_slot.mkdir(parents=True)
            server_name = (
                "audiocpp_server.exe"
                if application.context.system.casefold() == "windows"
                else "audiocpp_server"
            )
            (old_slot / server_name).write_text("old-runtime", encoding="utf-8")
            (old_slot / "tools").mkdir()
            (old_slot / "tools" / "model_manager_v2.py").write_text(
                "old-model-manager", encoding="utf-8"
            )
            old_config = server_config(
                ComputeVariant.CPU,
                ["qwen3_tts_1_7b_base_q8_0"],
            )
            (old_slot / "server.json").write_text(
                json.dumps(old_config),
                encoding="utf-8",
            )
            old_model = MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"].required_paths(
                old_slot / "models"
            )[0]
            old_model.parent.mkdir(parents=True)
            old_model.write_bytes(b"old-qwen-model")
            old_pointer = {
                "component_id": "audio_cpp",
                "version": old_revision,
                "path": str(old_slot),
                "activated_by": "previous-operation",
            }
            component_pointer(application.context.layout, "audio_cpp").write_text(
                json.dumps(old_pointer),
                encoding="utf-8",
            )

            operation_id = "audio-cpp-qwen-voxcpm"
            execution = OperationTaskContext(
                context=application.context,
                store=application.store,
                registry=application.registry,
                supervisor=None,
                operation=SimpleNamespace(id=operation_id),
                plan=plan,
                prior_results={},
                cancellation=CancellationToken(),
            )

            def install_model(spec):
                argv = list(spec.argv)
                package = MODEL_PACKAGES[argv[3]]
                models_root = Path(argv[argv.index("--models-root") + 1])
                for path in package.required_paths(models_root):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"new-model")
                package.marker_path(models_root).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "resolved_revision": "fixture-revision",
                            "files": {
                                filename: {"etag": "fixture"}
                                for filename in package.files
                            },
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            def expected_digest(path: Path) -> str:
                for package_id in package_ids:
                    package = MODEL_PACKAGES[package_id]
                    for required, expected in zip(
                        package.required_paths(path.parents[1]),
                        package.sha256,
                        strict=True,
                    ):
                        if required == path:
                            return expected
                raise AssertionError(f"Unexpected digest path: {path}")

            handler = FilesystemTaskHandler()
            with (
                mock.patch(
                    "pandrator_manager.operations.handlers.ArtifactDownloader.download",
                    return_value=archive,
                ),
                mock.patch(
                    "pandrator_manager.operations.handlers.CommandRunner.run",
                    side_effect=install_model,
                ),
                mock.patch.object(
                    FilesystemTaskHandler,
                    "_sha256_file",
                    side_effect=expected_digest,
                ),
            ):
                staged = handler.execute(execution, stage)
                execution.prior_results[stage.id] = staged
                activated = handler.execute(execution, activate)
                repeated = handler.execute(execution, activate)

            active = active_component_path(application.context.layout, "audio_cpp")
            self.assertIsNotNone(active)
            self.assertEqual(Path(activated["active_path"]), active)
            self.assertEqual(
                f"audio-cpp-{AUDIO_CPP_VERSION}-cpu-audio-cpp-qwen-voxcpm",
                activated["revision"],
            )
            config = json.loads((active / "server.json").read_text(encoding="utf-8"))
            self.assertEqual(package_ids, [item["id"] for item in config["models"]])
            for package_id in package_ids:
                for required in MODEL_PACKAGES[package_id].required_paths(active / "models"):
                    self.assertTrue(required.is_file())
            self.assertEqual(Path(activated["active_path"]), Path(repeated["active_path"]))
            self.assertEqual(activated["revision"], repeated["revision"])
            self.assertEqual(old_config, json.loads((old_slot / "server.json").read_text()))
            self.assertEqual(b"old-qwen-model", old_model.read_bytes())

            handler.rollback(execution, activate, repeated)
            self.assertEqual(old_slot, active_component_path(application.context.layout, "audio_cpp"))
            self.assertEqual(old_config, json.loads((old_slot / "server.json").read_text()))
            self.assertEqual(b"old-qwen-model", old_model.read_bytes())
            self.assertFalse(Path(activated["active_path"]).exists())

    def test_staging_uses_runtime_python_when_running_under_frozen_bootstrap(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            application = create_application(root / "workspace")
            plan = application.plan(
                kind=OperationKind.INSTALL,
                desired={
                    "audio_cpp": DesiredComponentState(
                        compute=ComputeVariant.CPU,
                        options={"models": ["qwen3_tts_1_7b_base_q8_0"]},
                    )
                },
                persist=False,
            )
            stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
            archive = root / "audio-cpp-runtime.zip"
            server_name = (
                "audiocpp_server.exe"
                if application.context.system.casefold() == "windows"
                else "audiocpp_server"
            )
            with zipfile.ZipFile(archive, "w") as output:
                output.writestr(server_name, "#!/bin/sh\n")
                output.writestr("tools/model_manager_v2.py", "#!/usr/bin/env python3\n")
                output.writestr(
                    "model_specs/qwen3_tts.json",
                    json.dumps(
                        {
                            "family": "qwen3_tts",
                            "package_defaults": {
                                "download": {
                                    "kind": "huggingface_snapshot",
                                    "repo": "audio-cpp/audio.cpp-gguf",
                                    "revision": "main",
                                }
                            },
                            "packages": [
                                {
                                    "id": "qwen3_tts_1_7b_base_q8_0",
                                    "download": {},
                                }
                            ],
                        }
                    ),
                )

            execution = OperationTaskContext(
                context=application.context,
                store=application.store,
                registry=application.registry,
                supervisor=None,
                operation=SimpleNamespace(id="audio-cpp-frozen-test"),
                plan=plan,
                prior_results={},
                cancellation=CancellationToken(),
            )

            executed_commands: list[list[str]] = []

            def record_command(spec):
                executed_commands.append(list(spec.argv))
                argv = list(spec.argv)
                package = MODEL_PACKAGES[argv[3]]
                models_root = Path(argv[argv.index("--models-root") + 1])
                for path in package.required_paths(models_root):
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(b"model")
                package.marker_path(models_root).write_text(
                    json.dumps(
                        {
                            "schema_version": 1,
                            "resolved_revision": "fixture-revision",
                            "files": {package.files[0]: {"etag": "fixture"}},
                        }
                    ),
                    encoding="utf-8",
                )
                return SimpleNamespace(returncode=0, stdout="", stderr="")

            fake_launcher = "/opt/pandrator/bin/pandrator-manager"
            fake_runtime_python = root / "runtime" / "python"
            fake_runtime_python.parent.mkdir(parents=True, exist_ok=True)
            fake_runtime_python.write_text("#!/bin/sh\n")

            handler = FilesystemTaskHandler()
            with (
                mock.patch(
                    "pandrator_manager.operations.handlers.ArtifactDownloader.download",
                    return_value=archive,
                ),
                mock.patch(
                    "pandrator_manager.operations.handlers.CommandRunner.run",
                    side_effect=record_command,
                ),
                mock.patch.object(
                    FilesystemTaskHandler,
                    "_sha256_file",
                    return_value=MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"].sha256[0],
                ),
                mock.patch("sys.executable", fake_launcher),
                mock.patch.object(
                    sys,
                    "frozen",
                    True,
                    create=True,
                ),
                mock.patch(
                    "pandrator_manager.operations.handlers.runtime_python",
                    return_value=fake_runtime_python,
                ),
            ):
                handler.execute(execution, stage)

            self.assertEqual(1, len(executed_commands))
            self.assertEqual(str(fake_runtime_python), executed_commands[0][0])
            self.assertNotEqual(fake_launcher, executed_commands[0][0])


if __name__ == "__main__":
    unittest.main()
