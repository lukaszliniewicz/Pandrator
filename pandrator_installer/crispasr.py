"""Pinned CrispASR release assets and compute-backend detection."""

from __future__ import annotations

import ctypes.util
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

CRISPASR_VERSION = "0.8.9"
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
    ("windows", "x86_64", "cpu"): CrispASRAsset("crispasr-windows-x86_64-cpu.zip", "3ccc4ce194c5a5c9a435118a530d602ba23e2a291f099ee2ca698c5d37945656", "cpu", ("cpu",)),
    ("windows", "x86_64", "cuda"): CrispASRAsset("crispasr-windows-x86_64-cuda.zip", "0de7b2ed37862124b9083e533589fc9fc4fb6c893ccd99eadac096b92684afa0", "cuda", ("cuda", "cpu")),
    ("windows", "x86_64", "vulkan"): CrispASRAsset("crispasr-windows-x86_64-vulkan.zip", "39b803fce62e534215e210a214376996754f0d5c9827cd5ba9cfd230084a5aff", "vulkan", ("vulkan", "cpu")),
    ("linux", "x86_64", "cpu"): CrispASRAsset("crispasr-linux-x86_64.tar.gz", "9137baa6cf689462093feb6123d0917e0df64bc9b2744ca0c68ada7e02845126", "cpu", ("cpu",)),
    ("linux", "x86_64", "cuda"): CrispASRAsset("crispasr-linux-x86_64-cuda.tar.gz", "c77c81a46b9a31de53903386a4c471c2013ad03d9ec3c076fad2659b32454487", "cuda", ("cuda", "cpu")),
    ("linux", "x86_64", "vulkan"): CrispASRAsset("crispasr-linux-x86_64-vulkan.tar.gz", "9da47fd486e333bd093f6079b8966b9dea9e031ded5936dc1e08ba0f67f553d7", "vulkan", ("vulkan", "cpu")),
    ("linux", "aarch64", "cpu"): CrispASRAsset("crispasr-linux-arm64.tar.gz", "72872630b5ad93e11916b7871d686a3f8697377a2ee4e43ff7bbba8ddb915bc1", "cpu", ("cpu",)),
    ("darwin", "aarch64", "metal"): CrispASRAsset("crispasr-macos.tar.gz", "5d5fbaf60431142d1f99a23ed0d4b94923f6a235030694c6ea4ce5a68e2aa5da", "metal", ("metal", "cpu")),
    ("darwin", "aarch64", "cpu"): CrispASRAsset("crispasr-macos.tar.gz", "5d5fbaf60431142d1f99a23ed0d4b94923f6a235030694c6ea4ce5a68e2aa5da", "metal", ("metal", "cpu")),
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
