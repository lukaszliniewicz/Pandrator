"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_RELEASE_BASE,
    AUDIO_CPP_VERSION,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_092_pins():
    assert AUDIO_CPP_VERSION == "0.9.2"
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.version == "0.9.2"
            assert "v0.9.2" in asset.name
            assert "/v0.9.2/" in asset.url
            assert asset.release_base == AUDIO_CPP_RELEASE_BASE
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    assert cpu.name == "audio-v0.9.2-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert cpu.sha256 == "72bfb07f46bb8f4cc0217a604619dbb78cfc388dfa4e639feb412ca7e2410980"
    assert vulkan.name == "audio-v0.9.2-bin-ubuntu-x64-vulkan-portable.tar.gz"
    assert vulkan.sha256 == "37294a0d9d52383475a47df95dc725c83faee0dbfae00088f1509ed4d7f3619d"


def test_windows_assets_use_verified_092_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    assert cpu.name == "audio-v0.9.2-bin-windows-x64-cpu-portable.zip"
    assert cpu.sha256 == "4bea04239d753d2d0a294580ad4af2a0fa81b0de244a1b9146650c7f9fa4e84d"
    assert vulkan.name == "audio-v0.9.2-bin-windows-x64-vulkan-portable.zip"
    assert vulkan.sha256 == "4d11a82c560f7a1dc0934e6fef847d6e0b779cf79854aa7d90d8b28e4ad04d93"
    assert cuda_binary.name == "audio-v0.9.2-bin-windows-x64-cuda12.4.zip"
    assert cuda_binary.sha256 == "099bd43b74a5175fd9e03ac95c9fabe8b97718826d547890a03950483e81b168"
    assert cuda_runtime.name == "audio-v0.9.2-cudart-windows-x64-cuda12.4.zip"
    assert cuda_runtime.sha256 == "50e278d213daa38e782a10b501b764c5c0738381898dacee7243101b890eeb0d"


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


def test_linux_cuda_uses_upstream_092_colab_asset_best_effort():
    # Upstream v0.9.2 publishes only the cuda12.8-colab-tagged Linux binary.
    # It is pinned best-effort: untested on local NVIDIA hardware, with the
    # CUDA runtime supplied by the host. Staging fails closed on layout mismatch.
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    assert asset.version == "0.9.2"
    assert asset.name == "audio-v0.9.2-bin-ubuntu-x64-cuda12.8-colab.tar.gz"
    assert asset.sha256 == "d1ffb5b314bd9905425e9122831fcda015b2e82fb339b1c0812794ba4ebbf3b8"
    assert asset.release_base == AUDIO_CPP_RELEASE_BASE
    assert asset.kind == "cuda_binary"


def test_linux_cuda_does_not_require_a_pandrator_build_workflow():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml"
    assert not workflow.exists()
