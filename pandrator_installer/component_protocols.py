"""Static contracts for installer launch, health, and component preparation."""

from __future__ import annotations

import subprocess
from typing import Protocol, TypedDict

from .command_protocols import PathArgument


class ProcessStatus(Protocol):
    def poll(self) -> int | None: ...


class CrispAsrInstallResult(TypedDict):
    requested_backend: str
    effective_backend: str
    runtime_variant: str
    compiled_backends: list[str]


class BackendHealthProvider(Protocol):
    def check_xtts_server_online(
        self,
        base_url: str,
        max_attempts: int = 120,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...
    def check_voxcpm_server_online(
        self,
        base_url: str,
        max_attempts: int = 120,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...
    def check_fishs2_server_online(
        self,
        base_url: str,
        max_attempts: int = 120,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...
    def check_magpie_server_online(
        self,
        base_url: str,
        max_attempts: int = 120,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...
    def check_chatterbox_server_online(
        self,
        base_url: str,
        max_attempts: int = 120,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...
    def check_kobold_qwen_server_online(
        self,
        base_url: str,
        max_attempts: int = 120,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...


class KokoroProvider(Protocol):
    def is_kokoro_runtime_ready(
        self, pandrator_path: str, kokoro_repo_path: str, use_gpu: bool = False
    ) -> bool: ...
    def check_kokoro_server_online(
        self,
        url: str,
        max_attempts: int = 90,
        wait_interval: float = 5,
        process: ProcessStatus | None = None,
    ) -> bool: ...
    def install_kokoro_api_server(
        self,
        pandrator_path: str,
        kokoro_repo_path: str,
        env_name: str = ...,
        use_gpu: bool = False,
        runtime_use_gpu: bool | None = None,
    ) -> None: ...
    def run_kokoro_api_server(
        self, pandrator_path: str, env_name: str, kokoro_server_path: str, use_gpu: bool = False
    ) -> subprocess.Popen[bytes] | None: ...


class LaunchPreparationProvider(Protocol):
    def build_xtts_launcher_command(
        self, use_cpu: bool = False, pixi_path: str | None = None
    ) -> list[str]: ...
    def build_voxcpm_launcher_command(self, pixi_path: str | None = None) -> list[str]: ...
    def build_fishs2_launcher_command(self, pixi_path: str | None = None) -> list[str]: ...
    def build_chatterbox_launcher_command(
        self, use_cpu: bool = False, pixi_path: str | None = None
    ) -> list[str]: ...
    def build_kobold_qwen_launcher_command(
        self,
        use_cpu: bool = False,
        pixi_path: str | None = None,
        backend: str | None = None,
        model_size: str = ...,
        quantization: str = "f16",
        initial_model: str = "base",
    ) -> list[str]: ...
    def build_magpie_launcher_command(
        self, use_cpu: bool = False, pixi_path: str | None = None
    ) -> list[str]: ...
    def build_silero_launcher_command(
        self, pandrator_path: str, pixi_path: str | None = None
    ) -> list[str]: ...
    def build_voxtral_launcher_command(
        self, voxtral_repo_path: str, prepare_only: bool = False
    ) -> list[str]: ...
    def get_xtts_pixi_argument(self, xtts_repo_path: str, pixi_path: str | None) -> str | None: ...
    def get_voxcpm_pixi_argument(
        self, voxcpm_repo_path: str, pixi_path: str | None
    ) -> str | None: ...
    def get_fishs2_pixi_argument(
        self, fishs2_repo_path: str, pixi_path: str | None
    ) -> str | None: ...
    def get_chatterbox_pixi_argument(
        self, chatterbox_repo_path: str, pixi_path: str | None
    ) -> str | None: ...
    def get_kobold_qwen_pixi_argument(
        self, kobold_qwen_repo_path: str, pixi_path: str | None
    ) -> str | None: ...
    def get_magpie_pixi_argument(
        self, magpie_repo_path: str, pixi_path: str | None
    ) -> str | None: ...
    def build_rvc_launcher_command(
        self,
        use_cpu: bool = False,
        pixi_path: str | None = None,
        prepare_only: bool = False,
        models_dir: str | None = None,
    ) -> list[str]: ...
    def _read_log_tail_if_exists(
        self, file_path: PathArgument | None, max_lines: int = 40
    ) -> str: ...


class ComponentInstallationProvider(KokoroProvider, Protocol):
    def install_xtts_finetuning_bundled_wheel(
        self, pandrator_path: str, env_name: str, easy_xtts_trainer_path: str
    ) -> bool: ...
    def install_xtts_api_server(
        self, xtts_repo_path: str, use_cpu: bool = False, pixi_path: str | None = None
    ) -> None: ...
    def install_voxcpm_api_server(
        self, voxcpm_repo_path: str, pixi_path: str | None = None
    ) -> None: ...
    def install_fishs2_api_server(
        self,
        fishs2_repo_path: str,
        backend: str = "auto",
        model_quant: str = "q6_k",
        pixi_path: str | None = None,
    ) -> None: ...
    def install_chatterbox_api_server(
        self, chatterbox_repo_path: str, use_cpu: bool = False, pixi_path: str | None = None
    ) -> None: ...
    def install_kobold_qwen_api_server(
        self,
        kobold_qwen_repo_path: str,
        use_cpu: bool = False,
        pixi_path: str | None = None,
        backend: str | None = None,
        model_size: str = ...,
        quantization: str = "f16",
        initial_model: str = "base",
    ) -> None: ...
    def install_magpie_api_server(
        self, magpie_repo_path: str, use_cpu: bool = False, pixi_path: str | None = None
    ) -> None: ...
    def install_voxtral_api_server(self, voxtral_repo_path: str) -> None: ...
    def install_silero_api_server(
        self, silero_repo_path: str, pandrator_path: str | None = None, pixi_path: str | None = None
    ) -> None: ...
    def clone_repo(self, repo_url: str, target_dir: str, branch: str | None = None) -> None: ...
    def install_rvc_api_server(
        self, rvc_repo_path: str, use_cpu: bool = False, pixi_path: str | None = None
    ) -> None: ...
    def ensure_nemo_text_processing_runtime(
        self, pandrator_path: str, env_name: str = "pandrator_installer"
    ) -> None: ...
    def ensure_wtpsplit_runtime(
        self, pandrator_path: str, env_name: str = "pandrator_installer"
    ) -> None: ...
    def ensure_pdf_ocr_runtime(
        self, pandrator_path: str, env_name: str = "pandrator_installer"
    ) -> None: ...
    def remove_legacy_rvc_from_pandrator_env(self, pandrator_path: str) -> None: ...
    def install_whisperx(self, pandrator_path: str, env_name: str) -> None: ...
    def install_crispasr(
        self, pandrator_path: str, requested_backend: str = "auto"
    ) -> CrispAsrInstallResult: ...
