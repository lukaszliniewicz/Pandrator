"""Pinned, verified CrispASR native-runtime assets used by the manager driver."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import ManagerContext
from ..models import ComputeVariant
from .host import compute_choices, normalized_architecture, resolve_auto_compute

CRISPASR_VERSION = "0.8.32"
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
        "ac8b6caf4dd448d00c5050907275bce4d154747110c37943aa4f69ee7fac9541",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("windows", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-windows-x86_64-cuda.zip",
        "9108d2be9b61415cf2c6d758d09a6fbfda369c2cda2d98f1f3d61e1326792d01",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("windows", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-windows-x86_64-vulkan.zip",
        "112a33912d464346ba1c2a75f975864a7ed0a3c1bd1ad0c3cf8806b6919efd7d",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-x86_64.tar.gz",
        "6953d1e6cd8d7d828183befcf76877f1a7e3908514548a511de786887106ff08",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("linux", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-linux-x86_64-cuda.tar.gz",
        "becc7ae1359713af19fa09446cdc32d55c7cbca137b0a0f8cfddb6d45be04cde",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-linux-x86_64-vulkan.tar.gz",
        "8d670a24830610861a3f47c4b2e78eeefc5174ef68cf415c8b0accc6545141fe",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-arm64.tar.gz",
        "eb39ca1274084add172764ce638a600e50fc4b65f4b18776aa78dcf31486570c",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("darwin", "aarch64", ComputeVariant.METAL): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "5e740d35e91a8dcaa79efd3ef0be3412de4796b68066921a9ea6984d2fc6b2ad",
        ComputeVariant.METAL,
        ("metal", "cpu"),
    ),
    ("darwin", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "5e740d35e91a8dcaa79efd3ef0be3412de4796b68066921a9ea6984d2fc6b2ad",
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
