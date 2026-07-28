"""Conservative host compute-capability projection for planning and clients."""

from __future__ import annotations

import ctypes.util
import functools
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ..context import ManagerContext
from ..models import ComponentDefinition, ComputeVariant

_ROCM_ARCHITECTURE = re.compile(r"\bgfx[0-9a-f]+\b", re.IGNORECASE)
_LEGACY_AMD_PATTERNS = (
    re.compile(r"\b(pol(?:aris)?|ellesmere|baffin|lexa|vega)\b", re.IGNORECASE),
    re.compile(r"\bradeon\s+(?:rx\s*)?[45]\d{2}\b", re.IGNORECASE),
    re.compile(r"\bradeon\s+(?:r[579]|hd)\b", re.IGNORECASE),
    re.compile(r"\bdev_(?:15dd|67[cd-ef][0-9a-f])\b", re.IGNORECASE),
)
_LINUX_DISPLAY_CLASSES = (
    "vga compatible controller",
    "3d controller",
    "display controller",
)


def _supported_rocm_architecture(value: str) -> bool:
    """Accept GPU targets covered by the manager's native ROCm recipes.

    Merely finding ROCm utilities is not proof that PyTorch can use the
    installed GPU. In particular, rocminfo is commonly present on systems
    with Polaris (gfx8) cards even though current binary wheels do not support
    those adapters. Keep automatic selection deliberately conservative.
    """

    architecture = value.casefold()
    return architecture in {"gfx90a", "gfx942", "gfx950"} or architecture.startswith(
        ("gfx10", "gfx11", "gfx12")
    )


def _rocm_capability(
    *,
    system: str,
    environment: Mapping[str, str],
) -> tuple[bool, str]:
    if system != "linux":
        return False, "ROCm is available only on Linux."

    executable = shutil.which("rocminfo")
    if executable is None:
        configured_root = str(environment.get("ROCM_PATH") or "").strip()
        if configured_root:
            candidate = Path(configured_root).expanduser() / "bin" / "rocminfo"
            if candidate.is_file():
                executable = str(candidate)
    if executable is None:
        return False, "No ROCm GPU probe was found."

    try:
        result = subprocess.run(
            (executable,),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False, "ROCm is installed, but its GPU probe could not run."
    if result.returncode:
        return False, "ROCm is installed, but its GPU probe did not succeed."

    architectures = tuple(
        sorted({match.casefold() for match in _ROCM_ARCHITECTURE.findall(result.stdout)})
    )
    if not architectures:
        return (
            False,
            "ROCm is installed, but no compatible AMD GPU agent was reported.",
        )
    supported = tuple(
        architecture
        for architecture in architectures
        if _supported_rocm_architecture(architecture)
    )
    if not supported:
        return (
            False,
            "The detected AMD GPU "
            f"({', '.join(architectures)}) is outside the supported ROCm range; "
            "CPU mode will be used.",
        )
    return True, f"Compatible ROCm GPU agent detected ({', '.join(supported)})."


@functools.lru_cache(maxsize=4)
def _graphics_descriptions(system: str, system_root: str) -> tuple[str, ...]:
    """Read stable adapter identities once per Manager process."""

    if system == "windows":
        powershell = (
            Path(system_root)
            / "System32"
            / "WindowsPowerShell"
            / "v1.0"
            / "powershell.exe"
        )
        if not powershell.is_file():
            return ()
        command = (
            str(powershell),
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "$ErrorActionPreference='Stop';"
                "Get-CimInstance Win32_VideoController | "
                "ForEach-Object { \"{0} {1}\" -f $_.Name,$_.PNPDeviceID }"
            ),
        )
    elif system == "linux":
        lspci = shutil.which("lspci")
        if lspci is None:
            return ()
        command = (lspci, "-nn")
    else:
        return ()
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    if result.returncode:
        return ()
    return tuple(
        line.strip()
        for line in result.stdout.splitlines()
        if line.strip()
    )


def _legacy_amd_only(descriptions: tuple[str, ...]) -> bool:
    # ``lspci -nn`` also reports the AMD HDMI-audio function paired with a
    # graphics card. Do not mistake that separate PCI function for a second,
    # unidentified modern GPU and re-enable Vulkan on a legacy-only host.
    display_descriptions = tuple(
        description
        for description in descriptions
        if any(
            device_class in description.casefold()
            for device_class in _LINUX_DISPLAY_CLASSES
        )
    )
    candidates = display_descriptions or descriptions
    amd = tuple(
        description
        for description in candidates
        if any(
            token in description.casefold()
            for token in ("amd", "ati", "radeon", "ven_1002")
        )
    )
    if not amd:
        return False
    legacy = tuple(
        description
        for description in amd
        if any(pattern.search(description) for pattern in _LEGACY_AMD_PATTERNS)
    )
    modern_amd = set(amd).difference(legacy)
    other_accelerator = any(
        token in description.casefold()
        for description in candidates
        for token in ("nvidia", "intel arc")
    )
    return bool(legacy) and not modern_amd and not other_accelerator


def normalized_architecture(value: str) -> str:
    architecture = str(value or "").strip().lower()
    if architecture in {"amd64", "x64"}:
        return "x86_64"
    if architecture == "arm64":
        return "aarch64"
    return architecture


def detect_compute(
    context: ManagerContext,
) -> dict[ComputeVariant, tuple[bool, str]]:
    system = context.system.strip().lower()
    architecture = normalized_architecture(context.architecture)
    environment = context.environment
    system_root = str(environment.get("SystemRoot") or r"C:\Windows")
    legacy_amd = _legacy_amd_only(
        _graphics_descriptions(system, system_root)
    )

    cuda_found = bool(
        shutil.which("nvidia-smi")
        or environment.get("CUDA_PATH")
        or environment.get("CUDA_HOME")
    )
    if system == "windows":
        system_root_path = Path(system_root)
        vulkan_found = bool(
            shutil.which("vulkaninfo")
            or (system_root_path / "System32" / "vulkan-1.dll").is_file()
        )
    else:
        vulkan_found = bool(
            shutil.which("vulkaninfo") or ctypes.util.find_library("vulkan")
        )
    rocm_found, rocm_reason = _rocm_capability(
        system=system,
        environment=environment,
    )
    metal_found = system == "darwin" and architecture == "aarch64"
    # WGPU can use DirectX on Windows even when the Vulkan loader is absent.
    # The service still performs its authoritative adapter check at startup.
    if legacy_amd and not cuda_found:
        vulkan_found = False
    wgpu_found = (
        system == "windows" or vulkan_found or cuda_found or rocm_found
    ) and not (legacy_amd and not cuda_found)

    return {
        ComputeVariant.CPU: (True, "Available on every supported host."),
        ComputeVariant.CUDA: (
            cuda_found,
            "NVIDIA runtime detected."
            if cuda_found
            else "No NVIDIA runtime was detected.",
        ),
        ComputeVariant.VULKAN: (
            vulkan_found,
            (
                "A legacy AMD adapter was detected; automatic GPU modes are "
                "disabled in this release."
            )
            if legacy_amd and not cuda_found
            else "Vulkan loader detected."
            if vulkan_found
            else "No Vulkan loader was detected.",
        ),
        ComputeVariant.METAL: (
            metal_found,
            "Apple Silicon Metal is available."
            if metal_found
            else "Metal is available only on Apple Silicon.",
        ),
        ComputeVariant.ROCM: (
            rocm_found,
            rocm_reason,
        ),
        ComputeVariant.WGPU: (
            wgpu_found,
            (
                "A legacy AMD adapter was detected; automatic GPU modes are "
                "disabled in this release."
            )
            if legacy_amd and not cuda_found
            else "A compatible graphics API is available; the adapter is verified on launch."
            if wgpu_found
            else "No compatible WGPU graphics API was detected.",
        ),
    }


def compute_choices(
    context: ManagerContext,
    definition: ComponentDefinition,
    *,
    detected: dict[ComputeVariant, tuple[bool, str]] | None = None,
) -> tuple[dict[str, Any], ...]:
    selected_detection = detected or detect_compute(context)
    choices: list[dict[str, Any]] = []
    if len(definition.compute_variants) > 1:
        resolved = resolve_auto_compute(
            context,
            definition,
            detected=selected_detection,
        )
        choices.append(
            {
                "value": ComputeVariant.AUTO.value,
                "label": f"Automatic (recommended: {resolved.value.upper()})",
                "available": True,
                "reason": "Use the best compatible runtime detected on this computer.",
                "resolved": resolved.value,
            }
        )
    for variant in definition.compute_variants:
        available, reason = selected_detection[variant]
        choices.append(
            {
                "value": variant.value,
                "label": variant.value.upper(),
                "available": available,
                "reason": reason,
                "resolved": variant.value,
            }
        )
    return tuple(choices)


def resolve_auto_compute(
    context: ManagerContext,
    definition: ComponentDefinition,
    *,
    detected: dict[ComputeVariant, tuple[bool, str]] | None = None,
) -> ComputeVariant:
    selected_detection = detected or detect_compute(context)
    for candidate in (
        ComputeVariant.CUDA,
        ComputeVariant.METAL,
        ComputeVariant.ROCM,
        ComputeVariant.VULKAN,
        ComputeVariant.WGPU,
        ComputeVariant.CPU,
    ):
        if (
            candidate in definition.compute_variants
            and selected_detection[candidate][0]
        ):
            return candidate
    # The component will fail preflight with a useful unavailable-variant
    # message; keeping resolution deterministic avoids indexing an empty tuple.
    return definition.compute_variants[0]


def require_compute_available(
    context: ManagerContext,
    definition: ComponentDefinition,
    variant: ComputeVariant,
    *,
    detected: dict[ComputeVariant, tuple[bool, str]] | None = None,
) -> None:
    if variant not in definition.compute_variants:
        raise ValueError(
            f"{definition.label} does not support compute variant {variant.value}."
        )
    available, reason = (detected or detect_compute(context))[variant]
    if not available:
        raise ValueError(f"{definition.label} cannot use {variant.value.upper()}: {reason}")
