"""Shared component and packaging metadata for the installer and release builder."""

from __future__ import annotations

from dataclasses import dataclass


INSTALLER_STATE_FILENAME = "installer_state.json"
PACKAGING_LAYOUT_FILENAME = "packaging_layout.json"
KOKORO_ENV_NAME = "kokoro_api_server_installer"
KOKORO_GPU_SUPPORT_CONFIG_FLAG = "kokoro_gpu_support"
CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG = "chatterbox_gpu_support"
MAGPIE_GPU_SUPPORT_CONFIG_FLAG = "magpie_gpu_support"
RVC_GPU_SUPPORT_CONFIG_FLAG = "rvc_gpu_support"


@dataclass(frozen=True)
class ComponentDefinition:
    key: str
    label: str
    config_flag: str
    paths: tuple[str, ...] = ()
    markers: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    variant_of: str | None = None
    repo_url: str | None = None
    repo_dirname: str | None = None
    process_attr: str | None = None
    port: int | None = None

    @property
    def packaging_key(self) -> str:
        return self.variant_of or self.key


COMPONENTS: dict[str, ComponentDefinition] = {
    "xtts": ComponentDefinition(
        key="xtts",
        label="XTTS",
        config_flag="xtts_support",
        paths=("xtts2_api",),
        markers=("xtts2_api/run.bat",),
        repo_url="https://github.com/lukaszliniewicz/xtts2_api.git",
        repo_dirname="xtts2_api",
        process_attr="xtts_process",
        port=8020,
    ),
    "xtts_cpu": ComponentDefinition(
        key="xtts_cpu",
        label="XTTS CPU",
        config_flag="xtts_support",
        paths=("xtts2_api",),
        markers=("xtts2_api/run.bat",),
        variant_of="xtts",
        repo_url="https://github.com/lukaszliniewicz/xtts2_api.git",
        repo_dirname="xtts2_api",
        process_attr="xtts_process",
        port=8020,
    ),
    "voxcpm": ComponentDefinition(
        key="voxcpm",
        label="VoxCPM",
        config_flag="voxcpm_support",
        paths=("voxcpm_fastapi",),
        markers=("voxcpm_fastapi/run.bat",),
        repo_url="https://github.com/lukaszliniewicz/voxcpm_fastapi.git",
        repo_dirname="voxcpm_fastapi",
        process_attr="voxcpm_process",
        port=8020,
    ),
    "fishs2": ComponentDefinition(
        key="fishs2",
        label="FishS2",
        config_flag="fishs2_support",
        paths=("fishs2-cpp-fastapi",),
        markers=("fishs2-cpp-fastapi/run.bat",),
        repo_url="https://github.com/lukaszliniewicz/fishs2-cpp-fastapi.git",
        repo_dirname="fishs2-cpp-fastapi",
        process_attr="fishs2_process",
        port=8020,
    ),
    "voxtral": ComponentDefinition(
        key="voxtral",
        label="Voxtral",
        config_flag="voxtral_support",
        paths=("voxtral-fastapi",),
        markers=("voxtral-fastapi/run.ps1",),
        repo_url="https://github.com/lukaszliniewicz/voxtral-fastapi.git",
        repo_dirname="voxtral-fastapi",
        process_attr="voxtral_process",
        port=8000,
    ),
    "kokoro": ComponentDefinition(
        key="kokoro",
        label="Kokoro",
        config_flag="kokoro_support",
        paths=("Kokoro-FastAPI", f"envs/{KOKORO_ENV_NAME}"),
        markers=("Kokoro-FastAPI/api/src/main.py", f"envs/{KOKORO_ENV_NAME}/pixi.toml"),
        repo_url="https://github.com/remsky/Kokoro-FastAPI.git",
        repo_dirname="Kokoro-FastAPI",
        process_attr="kokoro_process",
        port=8880,
    ),
    "kokoro_cpu": ComponentDefinition(
        key="kokoro_cpu",
        label="Kokoro CPU",
        config_flag="kokoro_support",
        paths=("Kokoro-FastAPI", f"envs/{KOKORO_ENV_NAME}"),
        markers=("Kokoro-FastAPI/api/src/main.py", f"envs/{KOKORO_ENV_NAME}/pixi.toml"),
        variant_of="kokoro",
        repo_url="https://github.com/remsky/Kokoro-FastAPI.git",
        repo_dirname="Kokoro-FastAPI",
        process_attr="kokoro_process",
        port=8880,
    ),
    "silero": ComponentDefinition(
        key="silero",
        label="Silero",
        config_flag="silero_support",
        paths=("envs/silero_api_server_installer",),
        markers=("envs/silero_api_server_installer/pixi.toml",),
        process_attr="silero_process",
        port=8001,
    ),
    "whisperx": ComponentDefinition(
        key="whisperx",
        label="WhisperX",
        config_flag="whisperx_support",
        paths=("envs/whisperx_installer",),
        markers=("envs/whisperx_installer/pixi.toml",),
    ),
    "xtts_finetuning": ComponentDefinition(
        key="xtts_finetuning",
        label="XTTS Fine-tuning",
        config_flag="xtts_finetuning_support",
        paths=("easy_xtts_trainer", "envs/easy_xtts_trainer"),
        markers=("easy_xtts_trainer/requirements.txt", "envs/easy_xtts_trainer/pixi.toml"),
        dependencies=("whisperx", "xtts"),
        repo_url="https://github.com/lukaszliniewicz/easy_xtts_trainer.git",
        repo_dirname="easy_xtts_trainer",
    ),
    "rvc": ComponentDefinition(
        key="rvc",
        label="RVC",
        config_flag="rvc_support",
        paths=("rvc-python",),
        markers=("rvc-python/run.bat",),
        repo_url="https://github.com/lukaszliniewicz/rvc-python.git",
        repo_dirname="rvc-python",
        process_attr="rvc_process",
        port=8050,
    ),
    "rvc_cpu": ComponentDefinition(
        key="rvc_cpu",
        label="RVC CPU",
        config_flag="rvc_support",
        paths=("rvc-python",),
        markers=("rvc-python/run.bat",),
        variant_of="rvc",
        repo_url="https://github.com/lukaszliniewicz/rvc-python.git",
        repo_dirname="rvc-python",
        process_attr="rvc_process",
        port=8050,
    ),
    "chatterbox": ComponentDefinition(
        key="chatterbox",
        label="Chatterbox",
        config_flag="chatterbox_support",
        paths=("chatterbox-fastapi",),
        markers=("chatterbox-fastapi/run.py", "chatterbox-fastapi/pyproject.toml"),
        repo_url="https://github.com/lukaszliniewicz/chatterbox-fastapi.git",
        repo_dirname="chatterbox-fastapi",
        process_attr="chatterbox_process",
        port=8040,
    ),
    "chatterbox_cpu": ComponentDefinition(
        key="chatterbox_cpu",
        label="Chatterbox CPU",
        config_flag="chatterbox_support",
        paths=("chatterbox-fastapi",),
        markers=("chatterbox-fastapi/run.py", "chatterbox-fastapi/pyproject.toml"),
        variant_of="chatterbox",
        repo_url="https://github.com/lukaszliniewicz/chatterbox-fastapi.git",
        repo_dirname="chatterbox-fastapi",
        process_attr="chatterbox_process",
        port=8040,
    ),
    "magpie": ComponentDefinition(
        key="magpie",
        label="Magpie",
        config_flag="magpie_support",
        paths=("magpie-fastapi",),
        markers=("magpie-fastapi/run.bat",),
        repo_url="https://github.com/lukaszliniewicz/magpie-fastapi.git",
        repo_dirname="magpie-fastapi",
        process_attr="magpie_process",
        port=8030,
    ),
    "magpie_cpu": ComponentDefinition(
        key="magpie_cpu",
        label="Magpie CPU",
        config_flag="magpie_support",
        paths=("magpie-fastapi",),
        markers=("magpie-fastapi/run.bat",),
        variant_of="magpie",
        repo_url="https://github.com/lukaszliniewicz/magpie-fastapi.git",
        repo_dirname="magpie-fastapi",
        process_attr="magpie_process",
        port=8030,
    ),
}

INSTALL_COMPONENT_KEYS = tuple(COMPONENTS)
RELEASE_COMPONENT_KEYS = tuple(
    key for key, component in COMPONENTS.items() if component.variant_of is None
)

PACKAGING_CONFIG_FLAGS = (
    "cuda_support",
    "xtts_support",
    "voxcpm_support",
    "fishs2_support",
    "silero_support",
    "voxtral_support",
    "kokoro_support",
    KOKORO_GPU_SUPPORT_CONFIG_FLAG,
    "whisperx_support",
    "xtts_finetuning_support",
    "rvc_support",
    RVC_GPU_SUPPORT_CONFIG_FLAG,
    "chatterbox_support",
    CHATTERBOX_GPU_SUPPORT_CONFIG_FLAG,
    "magpie_support",
    MAGPIE_GPU_SUPPORT_CONFIG_FLAG,
)

PACKAGING_EXCLUDED_FILE_PREFIXES = ("pandrator_state.sqlite3",)

PACKAGING_SHARED_PATHS = (
    "Pandrator",
    "Subdub",
    "bin",
    "Calibre Portable",
    ".pixi-home",
    ".pixi-cache",
    "cache",
    "envs/pandrator_installer",
    "config.json",
    INSTALLER_STATE_FILENAME,
    PACKAGING_LAYOUT_FILENAME,
)

PACKAGING_COMPONENT_PATHS = {
    key: component.paths
    for key, component in COMPONENTS.items()
    if component.variant_of is None and component.paths
}

BACKEND_COMPONENT_KEYS = (
    "xtts",
    "voxcpm",
    "fishs2",
    "voxtral",
    "kokoro",
    "silero",
    "chatterbox",
    "magpie",
)

LINUX_READY_BACKEND_KEYS = ("kokoro", "chatterbox")

LINUX_DEFERRED_INSTALL_COMPONENT_KEYS = tuple(
    key
    for key, component in COMPONENTS.items()
    if (component.process_attr or key == "xtts_finetuning")
    and component.packaging_key not in LINUX_READY_BACKEND_KEYS
)


def resolve_dependencies(selected_components: tuple[str, ...] | list[str] | set[str]) -> tuple[str, ...]:
    resolved: list[str] = []

    def visit(component_key: str) -> None:
        if component_key in resolved:
            return
        if component_key not in COMPONENTS:
            raise ValueError(f"Unknown component '{component_key}'.")

        component = COMPONENTS[component_key]
        for dependency in component.dependencies:
            visit(dependency)
        resolved.append(component_key)

    for key in selected_components:
        visit(key)
    return tuple(resolved)
