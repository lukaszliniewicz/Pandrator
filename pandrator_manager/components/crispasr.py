"""Pinned, verified CrispASR native-runtime assets used by the manager driver."""

from __future__ import annotations

from dataclasses import dataclass

from ..context import ManagerContext
from ..models import ComputeVariant
from .host import compute_choices, normalized_architecture, resolve_auto_compute

CRISPASR_VERSION = "0.8.40"
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
        "86b67ec6483aebf73cc79f4d3ee6dcf19ff1a3ce5d3097a1bce95b78fc24f8c2",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("windows", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-windows-x86_64-cuda.zip",
        "5bca3b6095f6167b43d81491201d1365b93b8dccc772e4823e80c26bdd7f6ec8",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("windows", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-windows-x86_64-vulkan.zip",
        "d78135b46d7881aec909aa398def315427ad784a268979accd18fa569a3b5ce9",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-x86_64.tar.gz",
        "dcb322648516fe3de9695e5d264b0ee79a43ee6b0d80f739eddbf6aff132aace",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("linux", "x86_64", ComputeVariant.CUDA): CrispASRAsset(
        "crispasr-linux-x86_64-cuda.tar.gz",
        "5eab290968dd2cd6dfbd3b3adba47fe9ec9ca24ee1fcef9f6e5069c96b6db182",
        ComputeVariant.CUDA,
        ("cuda", "cpu"),
    ),
    ("linux", "x86_64", ComputeVariant.VULKAN): CrispASRAsset(
        "crispasr-linux-x86_64-vulkan.tar.gz",
        "062273658d9dd8a38ffb355c4d18ae682e28a5090cf73fd911e6472283421483",
        ComputeVariant.VULKAN,
        ("vulkan", "cpu"),
    ),
    ("linux", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-linux-arm64.tar.gz",
        "8b555f1e2fff44848ebdcf2c17caef46d24888e0f744fea5245860f703471efa",
        ComputeVariant.CPU,
        ("cpu",),
    ),
    ("darwin", "aarch64", ComputeVariant.METAL): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "dc1d656585efbe65c0026e735f44c9d32f32113b64f7c1d198ad85862feaf0e2",
        ComputeVariant.METAL,
        ("metal", "cpu"),
    ),
    ("darwin", "aarch64", ComputeVariant.CPU): CrispASRAsset(
        "crispasr-macos.tar.gz",
        "dc1d656585efbe65c0026e735f44c9d32f32113b64f7c1d198ad85862feaf0e2",
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
