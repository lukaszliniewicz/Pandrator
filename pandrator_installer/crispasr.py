"""Pinned CrispASR release assets and compute-backend detection."""

from __future__ import annotations

import ctypes.util
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

CRISPASR_VERSION = "0.8.32"
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
    ("windows", "x86_64", "cpu"): CrispASRAsset("crispasr-windows-x86_64-cpu.zip", "ac8b6caf4dd448d00c5050907275bce4d154747110c37943aa4f69ee7fac9541", "cpu", ("cpu",)),
    ("windows", "x86_64", "cuda"): CrispASRAsset("crispasr-windows-x86_64-cuda.zip", "9108d2be9b61415cf2c6d758d09a6fbfda369c2cda2d98f1f3d61e1326792d01", "cuda", ("cuda", "cpu")),
    ("windows", "x86_64", "vulkan"): CrispASRAsset("crispasr-windows-x86_64-vulkan.zip", "112a33912d464346ba1c2a75f975864a7ed0a3c1bd1ad0c3cf8806b6919efd7d", "vulkan", ("vulkan", "cpu")),
    ("linux", "x86_64", "cpu"): CrispASRAsset("crispasr-linux-x86_64.tar.gz", "6953d1e6cd8d7d828183befcf76877f1a7e3908514548a511de786887106ff08", "cpu", ("cpu",)),
    ("linux", "x86_64", "cuda"): CrispASRAsset("crispasr-linux-x86_64-cuda.tar.gz", "becc7ae1359713af19fa09446cdc32d55c7cbca137b0a0f8cfddb6d45be04cde", "cuda", ("cuda", "cpu")),
    ("linux", "x86_64", "vulkan"): CrispASRAsset("crispasr-linux-x86_64-vulkan.tar.gz", "8d670a24830610861a3f47c4b2e78eeefc5174ef68cf415c8b0accc6545141fe", "vulkan", ("vulkan", "cpu")),
    ("linux", "aarch64", "cpu"): CrispASRAsset("crispasr-linux-arm64.tar.gz", "eb39ca1274084add172764ce638a600e50fc4b65f4b18776aa78dcf31486570c", "cpu", ("cpu",)),
    ("darwin", "aarch64", "metal"): CrispASRAsset("crispasr-macos.tar.gz", "5e740d35e91a8dcaa79efd3ef0be3412de4796b68066921a9ea6984d2fc6b2ad", "metal", ("metal", "cpu")),
    ("darwin", "aarch64", "cpu"): CrispASRAsset("crispasr-macos.tar.gz", "5e740d35e91a8dcaa79efd3ef0be3412de4796b68066921a9ea6984d2fc6b2ad", "metal", ("metal", "cpu")),
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
