"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_RELEASE_BASE,
    AUDIO_CPP_VERSION,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_drama3_base_small_pins():
    assert AUDIO_CPP_VERSION == "0.9.2-drama3-base-small"
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.version == "0.9.2-drama3-base-small"
            assert "v0.9.2-drama3-base-small" in asset.name
            assert "/v0.9.2-drama3-base-small/" in asset.url
            assert asset.release_base == AUDIO_CPP_RELEASE_BASE
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    _assert_release_pin(
        cpu,
        "audio-v0.9.2-drama3-base-small-bin-ubuntu-x64-cpu-portable.tar.gz",
        "d5e6110daae50800429680a277b70557d333d552c4a130a8f1d0e56181fa8697",
    )
    _assert_release_pin(
        vulkan,
        "audio-v0.9.2-drama3-base-small-bin-ubuntu-x64-vulkan-portable.tar.gz",
        "dfd0ad832829210eb13a03779bdd90f3e6e68f68edf762bf25b3915067beeb73",
    )


def test_windows_assets_use_verified_drama3_base_small_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    _assert_release_pin(
        cpu,
        "audio-v0.9.2-drama3-base-small-bin-windows-x64-cpu-portable.zip",
        "9654c8f66f87bda768196d934059be896399b43703b06e5f2b857430775bec3b",
    )
    _assert_release_pin(
        vulkan,
        "audio-v0.9.2-drama3-base-small-bin-windows-x64-vulkan-portable.zip",
        "9c943d406af3543cc5005d158ed583210cd7a13371bffcd4b7f262f7a6a5ad16",
    )
    _assert_release_pin(
        cuda_binary,
        "audio-v0.9.2-drama3-base-small-bin-windows-x64-cuda12.4.zip",
        "48852da40790c40f3285bca6c53ff7829b3963080f0e95b6742f868fc1d7c757",
    )
    _assert_release_pin(
        cuda_runtime,
        "audio-v0.9.2-drama3-base-small-cudart-windows-x64-cuda12.4.zip",
        "4b1186a525c9ac70a4fbd031b47e0dfca2ebaa36cfcb760d5be1a76eb2b12da5",
    )


def _assert_release_pin(asset, name: str, sha256: str) -> None:
    assert asset.name == name
    assert asset.sha256 == sha256
    assert asset.version == "0.9.2-drama3-base-small"
    assert asset.release_base == AUDIO_CPP_RELEASE_BASE
    assert asset.url == f"{AUDIO_CPP_RELEASE_BASE}/{name}"


def test_asset_names_versions_digests_and_urls_are_consistent():
    seen_names: set[str] = set()
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.url == f"{asset.release_base}/{asset.name}"
            assert f"v{asset.version}" in asset.name
            assert f"/v{asset.version}/" in asset.url
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)
            assert asset.name not in seen_names
            seen_names.add(asset.name)
    assert AUDIO_CPP_MODEL_REVISION == "dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c"


def test_linux_cuda_uses_upstream_drama3_base_small_colab_asset_best_effort():
    # Upstream v0.9.2-drama3-base-small publishes only the cuda12.8-colab-tagged Linux binary.
    # It is pinned best-effort: untested on local NVIDIA hardware, with the
    # CUDA runtime supplied by the host. Staging fails closed on layout mismatch.
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    _assert_release_pin(
        asset,
        "audio-v0.9.2-drama3-base-small-bin-ubuntu-x64-cuda12.8-colab.tar.gz",
        "fc59545092eab869013c74d11273c17c96a050befe257523b9019d1a5073962c",
    )
    assert asset.kind == "cuda_binary"


def test_linux_cuda_does_not_require_a_pandrator_build_workflow():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml"
    assert not workflow.exists()
