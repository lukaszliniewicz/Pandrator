import json
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
    MODEL_PACKAGES,
    PANDRATOR_AUDIO_CPP_RELEASE_BASE,
    SUPPORTED_MODEL_IDS,
    resolve_assets,
    server_config,
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


class AudioCppManagerTests(unittest.TestCase):
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
        self.assertEqual("clon", config["models"][-1]["task"])

    def test_linux_cuda_resolves_to_the_pandrator_pinned_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            context = ManagerContext(
                layout=WorkspaceLayout.from_value(directory),
                system="Linux",
                architecture="x86_64",
                environment={"CUDA_HOME": directory},
            )
            definition = builtin_registry().definition("audio_cpp")
            assets, effective = resolve_assets(
                context, ComputeVariant.CUDA, definition
            )

        self.assertEqual(ComputeVariant.CUDA, effective)
        self.assertEqual(1, len(assets))
        self.assertEqual("cuda_binary", assets[0].kind)
        self.assertEqual(
            "audio.cpp-v0.7.1-linux-x86_64-cuda12.tar.gz", assets[0].name
        )
        self.assertEqual(
            "f55d39c048a2fffc96f245111fc47cdfff903550d9d352fa0a7f9e4da2356ab7",
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
                    return_value=MODEL_PACKAGES[
                        "qwen3_tts_1_7b_base_q8_0"
                    ].sha256[0],
                ),
            ):
                result = handler.execute(execution, stage)
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
                self.assertEqual(AUDIO_CPP_MODEL_REVISION, provenance["requested_revision"])
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

                required = MODEL_PACKAGES["qwen3_tts_1_7b_base_q8_0"].required_paths(
                    target / "models"
                )[0]
                required.unlink()
                retried = handler.execute(execution, stage)

            self.assertFalse(retried["reused"])
            self.assertEqual(2, run.call_count)


if __name__ == "__main__":
    unittest.main()
