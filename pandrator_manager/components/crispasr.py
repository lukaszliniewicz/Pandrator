"""Pinned, verified CrispASR native-runtime assets used by the manager driver."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import ManagerContext
from ..models import ComputeVariant
from .host import compute_choices, normalized_architecture, resolve_auto_compute

CRISPASR_VERSION = "0.8.36"
CRISPASR_RELEASE_BASE = (
    f"https://github.com/CrispStrobe/CrispASR/releases/download/v{CRISPASR_VERSION}"
)


@dataclass(frozen=True, slots=True)
class CrispASRAsset:
    name: str
    sha256: str
    runtime_variant: ComputeVariant
    compiled_backends: tuple[str, ...]

    @property
    def url(self) -> str:
        return f"{CRISPASR_RELEASE_BASE}/{self.name}"


ASSETS: dict[tuple[str, str, ComputeVariant], CrispASRAsset] = {
    ("windows", "x86_64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-windows-x86_64-cpu.zip",
        "1d8c853d102671f4036ccf4da8573a6d9ed3d45ae4530aa07573760a4bc93dc1",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("windows", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-windows-x86_64-cuda.zip",
        "4d14ce34cbc089259e897bed369214f6f920efa31e3236845bb6c7464ed7fba0",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("windows", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-windows-x86_64-vulkan.zip",
        "659e6cc1d3d0c7d65e1ce2df61efd7295c5b017e8a95c4d340c20ba70793d9cc",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-x86_64.tar.gz",
        "8c0547c07e900f9587fc68a947e4e37938745e6a8ecf6aa9e876cf3a98d18e0f",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("linux", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-linux-x86_64-cuda.tar.gz",
        "5a6e68f4e021a08ae49d265b002cc1eb0a80e2fa41bb0f12200f718d1d65537d",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-linux-x86_64-vulkan.tar.gz",
        "8eb99a0c7dde45aecf707a39aef84733df84af4d7f4531813cdaa898d0b5d59a",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-arm64.tar.gz",
        "f1900065e633c73a242e15df10e18fc53174e6ff95aabee60287995fb248df9e",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("darwin", "aarch64", ComputeVariant.METAL): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "0a494b48759ce9756cb0e0fcf72c0beed4c00335f8ae081e571c93d480a500f9",
        ComputeVariant.METAL,
        ("metal", "cpu"),
    ),
    ("darwin", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "0a494b48759ce9756cb0e0fcf72c0beed4c00335f8ae081e571c93d480a500f9",
        ComputeVariant.METAL,
        ("metal", "cpu"),
    ),
}


def resolve_asset(
    context: ManagerContext,
    requested: ComputeVariant,
    definition,
) -> tuple[CrispASRAsset, ComputeVariant]:
    system = context.system.strip().lower()
    architecture = normalized_architecture(context.architecture)
    effective = (
        resolve_auto_compute(context, definition)
        if requested == ComputeVariant.AUTO
        else requested
    )
    availability = {
        item["value"]: item
        for item in compute_choices(context, definition)
    }
    selected = availability.get(effective.value)
    if selected is not None and not selected["available"]:
        raise ValueError(
            f"CrispASR cannot use {effective.value.upper()}: {selected['reason']}"
        )
    asset = ASSETS.get((system, architecture, effective))
    if asset is None:
        raise ValueError(
            f"CrispASR has no {effective.value} release for "
            f"{system}/{architecture}."
        )
    return asset, effective
