"""Pinned CrispASR release assets and compute-backend detection."""

from __future__ import annotations

import ctypes.util
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

CRISPASR_VERSION = "0.8.36"
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
    ("windows", "x86_64", "cpu"): CrispASRAsset("crispasr-windows-x86_64-cpu.zip", "1d8c853d102671f4036ccf4da8573a6d9ed3d45ae4530aa07573760a4bc93dc1", "cpu", ("cpu",)),
    ("windows", "x86_64", "cuda"): CrispASRAsset("crispasr-windows-x86_64-cuda.zip", "4d14ce34cbc089259e897bed369214f6f920efa31e3236845bb6c7464ed7fba0", "cuda", ("cuda", "cpu")),
    ("windows", "x86_64", "vulkan"): CrispASRAsset("crispasr-windows-x86_64-vulkan.zip", "659e6cc1d3d0c7d65e1ce2df61efd7295c5b017e8a95c4d340c20ba70793d9cc", "vulkan", ("vulkan", "cpu")),
    ("linux", "x86_64", "cpu"): CrispASRAsset("crispasr-linux-x86_64.tar.gz", "8c0547c07e900f9587fc68a947e4e37938745e6a8ecf6aa9e876cf3a98d18e0f", "cpu", ("cpu",)),
    ("linux", "x86_64", "cuda"): CrispASRAsset("crispasr-linux-x86_64-cuda.tar.gz", "5a6e68f4e021a08ae49d265b002cc1eb0a80e2fa41bb0f12200f718d1d65537d", "cuda", ("cuda", "cpu")),
    ("linux", "x86_64", "vulkan"): CrispASRAsset("crispasr-linux-x86_64-vulkan.tar.gz", "8eb99a0c7dde45aecf707a39aef84733df84af4d7f4531813cdaa898d0b5d59a", "vulkan", ("vulkan", "cpu")),
    ("linux", "aarch64", "cpu"): CrispASRAsset("crispasr-linux-arm64.tar.gz", "f1900065e633c73a242e15df10e18fc53174e6ff95aabee60287995fb248df9e", "cpu", ("cpu",)),
    ("darwin", "aarch64", "metal"): CrispASRAsset("crispasr-macos.tar.gz", "0a494b48759ce9756cb0e0fcf72c0beed4c00335f8ae081e571c93d480a500f9", "metal", ("metal", "cpu")),
    ("darwin", "aarch64", "cpu"): CrispASRAsset("crispasr-macos.tar.gz", "0a494b48759ce9756cb0e0fcf72c0beed4c00335f8ae081e571c93d480a500f9", "metal", ("metal", "cpu")),
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
