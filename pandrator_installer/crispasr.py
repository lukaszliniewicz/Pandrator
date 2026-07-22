"""Pinned CrispASR release assets and compute-backend detection."""

from __future__ import annotations

import ctypes.util
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

CRISPASR_VERSION = "0.8.20"
CRISPASR_RELEASE_BASE = f"https://github.com/CrispStrobe/CrispASR/releases/download/v{CRISPASR_VERSION}"


@dataclass(frozen=True)
class CrispASRAsset:
    name: str
    sha256: str
    runtime_variant: str
    compiled_backends: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"{CRISPASR_RELEASE_BASE}/{self.name}"


ASSETS = {
    ("windows", "x86_64", "cpu"): CrispASRAsset("crispasr-windows-x86_64-cpu.zip", "7ed04c9d78c0e733f930e9a6c9df04f7584ff5b89eaf0e6425650365e9453701", "cpu", ("cpu",)),
    ("windows", "x86_64", "cuda"): CrispASRAsset("crispasr-windows-x86_64-cuda.zip", "7371783dbe6fef28257e8cb6d501c8e9a98e36b55833c66145895c8815db3b79", "cuda", ("cuda", "cpu")),
    ("windows", "x86_64", "vulkan"): CrispASRAsset("crispasr-windows-x86_64-vulkan.zip", "f26c261a35f469bb571a91774db8e28c996bda4b5e7a77bf5b3f8214f3ce01a4", "vulkan", ("vulkan", "cpu")),
    ("linux", "x86_64", "cpu"): CrispASRAsset("crispasr-linux-x86_64.tar.gz", "c8aae93543a8293a1e07a8afd83c16aca8af342e6ee6aa076d179464fe866e98", "cpu", ("cpu",)),
    ("linux", "x86_64", "cuda"): CrispASRAsset("crispasr-linux-x86_64-cuda.tar.gz", "fccf84c0d627a25a5a4e4ba08b1d1d32b9a62318586badf046a99bfa959e899b", "cuda", ("cuda", "cpu")),
    ("linux", "x86_64", "vulkan"): CrispASRAsset("crispasr-linux-x86_64-vulkan.tar.gz", "1db7b06af2736a45181cdd6abb9ec048d5b9509f16bce9bdd0613bece2e17508", "vulkan", ("vulkan", "cpu")),
    ("linux", "aarch64", "cpu"): CrispASRAsset("crispasr-linux-arm64.tar.gz", "04fdf1675e47a2b7fdfcb5ff7f50d98967573ea2e74b906cc4369670d2cf978a", "cpu", ("cpu",)),
    ("darwin", "aarch64", "metal"): CrispASRAsset("crispasr-macos.tar.gz", "75bc46aec934cac1fc57b98b0cce5af3cb9faa63bb918ae93661703083f87ec6", "metal", ("metal", "cpu")),
    ("darwin", "aarch64", "cpu"): CrispASRAsset("crispasr-macos.tar.gz", "75bc46aec934cac1fc57b98b0cce5af3cb9faa63bb918ae93661703083f87ec6", "metal", ("metal", "cpu")),
}


def normalized_platform(system: str | None = None, machine: str | None = None) -> tuple[str, str]:
    system_name = str(system or platform.system()).strip().lower()
    architecture = str(machine or platform.machine()).strip().lower()
    if architecture in {"amd64", "x64"}:
        architecture = "x86_64"
    elif architecture in {"arm64"}:
        architecture = "aarch64"
    return system_name, architecture


def detect_compute_backends(
    *,
    system: str | None = None,
    machine: str | None = None,
    environ: dict[str, str] | None = None,
    find_executable=shutil.which,
    path_exists=lambda path: Path(path).exists(),
    find_library=ctypes.util.find_library,
) -> dict[str, dict[str, object]]:
    system_name, architecture = normalized_platform(system, machine)
    active = os.environ if environ is None else environ
    cuda = bool(find_executable("nvidia-smi") or active.get("CUDA_PATH") or active.get("CUDA_HOME"))
    if system_name == "windows":
        system_root = active.get("SystemRoot", r"C:\Windows")
        vulkan = bool(find_executable("vulkaninfo") or path_exists(Path(system_root) / "System32" / "vulkan-1.dll"))
    else:
        vulkan = bool(find_executable("vulkaninfo") or find_library("vulkan"))
    metal = system_name == "darwin" and architecture == "aarch64"
    return {
        "auto": {"available": True, "reason": "Use the best detected installed runtime."},
        "cpu": {"available": True, "reason": "Always available."},
        "cuda": {"available": cuda and (system_name, architecture, "cuda") in ASSETS, "reason": "NVIDIA driver/toolkit detected." if cuda else "No NVIDIA runtime detected."},
        "vulkan": {"available": vulkan and (system_name, architecture, "vulkan") in ASSETS, "reason": "Vulkan loader detected." if vulkan else "No Vulkan loader detected."},
        "metal": {"available": metal, "reason": "Apple Silicon Metal runtime." if metal else "Metal is available on Apple Silicon only."},
    }


def resolve_asset(
    requested_backend: str = "auto",
    *,
    system: str | None = None,
    machine: str | None = None,
    detected: dict[str, dict[str, object]] | None = None,
) -> tuple[CrispASRAsset, str]:
    system_name, architecture = normalized_platform(system, machine)
    requested = str(requested_backend or "auto").strip().lower()
    if requested not in {"auto", "cpu", "cuda", "vulkan", "metal"}:
        raise ValueError(f"Unsupported CrispASR compute backend: {requested_backend}")
    statuses = detected or detect_compute_backends(system=system_name, machine=architecture)
    effective = requested
    if requested == "auto":
        effective = next(
            (name for name in ("cuda", "metal", "vulkan", "cpu") if statuses.get(name, {}).get("available") and (system_name, architecture, name) in ASSETS),
            "cpu",
        )
    asset = ASSETS.get((system_name, architecture, effective))
    if asset is None:
        raise ValueError(f"CrispASR has no {effective} release for {system_name}/{architecture}.")
    return asset, effective
