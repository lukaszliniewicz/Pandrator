"""Pinned CrispASR release assets and compute-backend detection."""

from __future__ import annotations

import ctypes.util
import os
import platform
import shutil
from dataclasses import dataclass
from pathlib import Path

CRISPASR_VERSION = "0.8.40"
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
    ("windows", "x86_64", "cpu"): CrispASRAsset("crispasr-windows-x86_64-cpu.zip", "86b67ec6483aebf73cc79f4d3ee6dcf19ff1a3ce5d3097a1bce95b78fc24f8c2", "cpu", ("cpu",)),
    ("windows", "x86_64", "cuda"): CrispASRAsset("crispasr-windows-x86_64-cuda.zip", "5bca3b6095f6167b43d81491201d1365b93b8dccc772e4823e80c26bdd7f6ec8", "cuda", ("cuda", "cpu")),
    ("windows", "x86_64", "vulkan"): CrispASRAsset("crispasr-windows-x86_64-vulkan.zip", "d78135b46d7881aec909aa398def315427ad784a268979accd18fa569a3b5ce9", "vulkan", ("vulkan", "cpu")),
    ("linux", "x86_64", "cpu"): CrispASRAsset("crispasr-linux-x86_64.tar.gz", "dcb322648516fe3de9695e5d264b0ee79a43ee6b0d80f739eddbf6aff132aace", "cpu", ("cpu",)),
    ("linux", "x86_64", "cuda"): CrispASRAsset("crispasr-linux-x86_64-cuda.tar.gz", "5eab290968dd2cd6dfbd3b3adba47fe9ec9ca24ee1fcef9f6e5069c96b6db182", "cuda", ("cuda", "cpu")),
    ("linux", "x86_64", "vulkan"): CrispASRAsset("crispasr-linux-x86_64-vulkan.tar.gz", "062273658d9dd8a38ffb355c4d18ae682e28a5090cf73fd911e6472283421483", "vulkan", ("vulkan", "cpu")),
    ("linux", "aarch64", "cpu"): CrispASRAsset("crispasr-linux-arm64.tar.gz", "8b555f1e2fff44848ebdcf2c17caef46d24888e0f744fea5245860f703471efa", "cpu", ("cpu",)),
    ("darwin", "aarch64", "metal"): CrispASRAsset("crispasr-macos.tar.gz", "dc1d656585efbe65c0026e735f44c9d32f32113b64f7c1d198ad85862feaf0e2", "metal", ("metal", "cpu")),
    ("darwin", "aarch64", "cpu"): CrispASRAsset("crispasr-macos.tar.gz", "dc1d656585efbe65c0026e735f44c9d32f32113b64f7c1d198ad85862feaf0e2", "metal", ("metal", "cpu")),
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
