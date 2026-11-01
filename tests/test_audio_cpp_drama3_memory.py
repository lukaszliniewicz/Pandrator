import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import pytest

from pandrator_manager.application import create_application
from pandrator_manager.components.audiocpp import (
    MODEL_PACKAGES,
    server_config,
)
from pandrator_manager.context import CancellationToken
from pandrator_manager.models import (
    ComputeVariant,
    DesiredComponentState,
    OperationKind,
)
from pandrator_manager.operations.handlers import (
    FilesystemTaskHandler,
    OperationTaskContext,
)

DRAMA_MODEL_ID = "fish_drama3_small_iq3_xxs"
QWEN_MODEL_ID = "qwen3_tts_1_7b_base_q8_0"


def test_server_config_applies_disk_expert_settings_only_to_drama3():
    selected = [DRAMA_MODEL_ID, QWEN_MODEL_ID]
    native = server_config(ComputeVariant.CPU, selected)
    explicit_native = server_config(
        ComputeVariant.CPU, selected, drama3_memory="native"
    )
    disk = server_config(ComputeVariant.CPU, selected, drama3_memory="disk")

    assert native == explicit_native
    native_models = {entry["id"]: entry for entry in native["models"]}
    disk_models = {entry["id"]: entry for entry in disk["models"]}
    assert "session_options" not in native_models[DRAMA_MODEL_ID]
    assert "session_options" not in native_models[QWEN_MODEL_ID]
    assert disk_models[DRAMA_MODEL_ID]["session_options"] == {
        "fish_drama3.disk_experts": True,
        "fish_drama3.expert_cache_size": 8,
        "fish_drama3.cpu_expert_layers": 0,
    }
    assert disk_models[QWEN_MODEL_ID] == native_models[QWEN_MODEL_ID]
    assert "fish_drama3.pin_experts" not in disk_models[DRAMA_MODEL_ID]["session_options"]
    assert {key: value for key, value in disk.items() if key != "models"} == {
        key: value for key, value in native.items() if key != "models"
    }


@pytest.mark.parametrize("profile", ["", "auto", "GPU", None, False, [], {}])
def test_server_config_rejects_invalid_drama3_memory_profiles(profile):
    with pytest.raises(ValueError, match="drama3_memory"):
        server_config(
            ComputeVariant.CPU,
            [DRAMA_MODEL_ID],
            drama3_memory=profile,
        )


def _install_plan(tmp_path: Path, options: dict[str, object]):
    application = create_application(tmp_path / "workspace")
    plan = application.plan(
        kind=OperationKind.INSTALL,
        desired={
            "audio_cpp": DesiredComponentState(
                compute=ComputeVariant.CPU,
                options=options,
            )
        },
        persist=False,
    )
    stage = next(task for task in plan.tasks if task.kind == "stage_audio_cpp")
    return application, plan, stage


def test_install_plan_carries_memory_profile_and_preserves_other_options(tmp_path):
    selected = [DRAMA_MODEL_ID, QWEN_MODEL_ID]
    _, _, stage = _install_plan(
        tmp_path,
        {
            "models": selected,
            "drama3_memory": "disk",
            "kokoro_japanese": "enabled",
        },
    )

    resolved_options = stage.inputs["resolved"]["options"]
    assert stage.inputs["drama3_memory"] == "disk"
    assert resolved_options["drama3_memory"] == "disk"
    assert resolved_options["models"] == selected
    assert stage.inputs["models"] == selected
    assert resolved_options["kokoro_japanese"] == "enabled"
    assert stage.inputs["kokoro_japanese"] is True


def _write_qwen_runtime_archive(path: Path, system: str) -> None:
    server_name = "audiocpp_server.exe" if system.casefold() == "windows" else "audiocpp_server"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(server_name, "#!/bin/sh\n")
        archive.writestr(server_name.replace("server", "cli"), "#!/bin/sh\n")
        archive.writestr("tools/model_manager_v2.py", "#!/usr/bin/env python3\n")
        archive.writestr(
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
                    "packages": [{"id": QWEN_MODEL_ID, "download": {}}],
                }
            ),
        )


def test_legacy_stage_task_defaults_to_native_memory_profile(tmp_path):
    application, plan, stage = _install_plan(
        tmp_path,
        {"models": [QWEN_MODEL_ID]},
    )
    legacy_inputs = dict(stage.inputs)
    legacy_inputs.pop("drama3_memory", None)
    legacy_resolved = dict(legacy_inputs["resolved"])
    legacy_options = dict(legacy_resolved["options"])
    legacy_options.pop("drama3_memory", None)
    legacy_resolved["options"] = legacy_options
    legacy_inputs["resolved"] = legacy_resolved
    legacy_stage = stage.model_copy(update={"inputs": legacy_inputs})

    archive = tmp_path / "audio-cpp-runtime.zip"
    _write_qwen_runtime_archive(archive, application.context.system)
    package = MODEL_PACKAGES[QWEN_MODEL_ID]
    execution = OperationTaskContext(
        context=application.context,
        store=application.store,
        registry=application.registry,
        supervisor=None,
        operation=SimpleNamespace(id="audio-cpp-legacy-task"),
        plan=plan,
        prior_results={},
        cancellation=CancellationToken(),
    )

    def install_model(spec):
        argv = list(spec.argv)
        models_root = Path(argv[argv.index("--models-root") + 1])
        for required in package.required_paths(models_root):
            required.parent.mkdir(parents=True, exist_ok=True)
            required.write_bytes(b"fixture model")
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
        ),
        mock.patch.object(
            FilesystemTaskHandler,
            "_sha256_file",
            return_value=package.sha256[0],
        ),
        mock.patch(
            "pandrator_manager.operations.staging_tasks.server_config",
            wraps=server_config,
        ) as config_call,
    ):
        result = handler.execute(execution, legacy_stage)

    config_call.assert_called_once_with(
        stage.inputs["effective_compute"],
        [QWEN_MODEL_ID],
        drama3_memory="native",
    )
    staged_config = json.loads(
        (Path(result["staged_path"]) / "server.json").read_text(encoding="utf-8")
    )
    assert "session_options" not in staged_config["models"][0]
