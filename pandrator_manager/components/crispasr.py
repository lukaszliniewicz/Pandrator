"""Pinned, verified CrispASR native-runtime assets used by the manager driver."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import ManagerContext
from ..models import ComputeVariant
from .host import compute_choices, normalized_architecture, resolve_auto_compute

CRISPASR_VERSION = "0.8.20"
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
        "7ed04c9d78c0e733f930e9a6c9df04f7584ff5b89eaf0e6425650365e9453701",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("windows", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-windows-x86_64-cuda.zip",
        "7371783dbe6fef28257e8cb6d501c8e9a98e36b55833c66145895c8815db3b79",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("windows", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-windows-x86_64-vulkan.zip",
        "f26c261a35f469bb571a91774db8e28c996bda4b5e7a77bf5b3f8214f3ce01a4",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-x86_64.tar.gz",
        "c8aae93543a8293a1e07a8afd83c16aca8af342e6ee6aa076d179464fe866e98",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("linux", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-linux-x86_64-cuda.tar.gz",
        "fccf84c0d627a25a5a4e4ba08b1d1d32b9a62318586badf046a99bfa959e899b",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-linux-x86_64-vulkan.tar.gz",
        "1db7b06af2736a45181cdd6abb9ec048d5b9509f16bce9bdd0613bece2e17508",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-arm64.tar.gz",
        "04fdf1675e47a2b7fdfcb5ff7f50d98967573ea2e74b906cc4369670d2cf978a",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("darwin", "aarch64", ComputeVariant.METAL): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "75bc46aec934cac1fc57b98b0cce5af3cb9faa63bb918ae93661703083f87ec6",
        ComputeVariant.METAL,
        ("metal", "cpu"),
    ),
    ("darwin", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "75bc46aec934cac1fc57b98b0cce5af3cb9faa63bb918ae93661703083f87ec6",
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
