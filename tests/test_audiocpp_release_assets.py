"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_RELEASE_BASE,
    AUDIO_CPP_VERSION,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_081_pins():
    assert AUDIO_CPP_VERSION == "0.8.1"
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.version == "0.8.1"
            assert "v0.8.1" in asset.name
            assert "/v0.8.1/" in asset.url
            assert asset.release_base == AUDIO_CPP_RELEASE_BASE
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    assert cpu.name == "audio-v0.8.1-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert cpu.sha256 == "90e8d538338cc209875a18c940529302805563e54738489da1d684c6e0de12d0"
    assert vulkan.name == "audio-v0.8.1-bin-ubuntu-x64-vulkan-portable.tar.gz"
    assert vulkan.sha256 == "63f778ef4c863ece0bca85b97c78e9629bd561724b1aa5330a9d47806608bbfa"


def test_windows_assets_use_verified_081_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    assert cpu.name == "audio-v0.8.1-bin-windows-x64-cpu-portable.zip"
    assert cpu.sha256 == "fc6a20cc881b0882569d0eca060235a1904863b96b531f79145ce00acf8f8bfd"
    assert vulkan.name == "audio-v0.8.1-bin-windows-x64-vulkan.zip"
    assert vulkan.sha256 == "c787971e025ba8ef900f0482a2cc36a049367081fe89f4841aae521a0b49de32"
    assert cuda_binary.name == "audio-v0.8.1-bin-windows-x64-cuda12.4.zip"
    assert cuda_binary.sha256 == "28bbe8ac62a06c5d9d42ba3066b051f433dc9a8f456c544e03e87202f0fa8c52"
    assert cuda_runtime.name == "audio-v0.8.1-cudart-windows-x64-cuda12.4.zip"
    assert cuda_runtime.sha256 == "025faacfdc3dec215ee07cb9be7d1ef2016402723f3721a30500ceee02cc4701"


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


def test_linux_cuda_uses_upstream_colab_asset_best_effort():
    # Upstream v0.8.1 publishes only the cuda12.8-colab-tagged Linux binary.
    # It is pinned best-effort: untested on local NVIDIA hardware and without
    # a bundled cudart archive. Staging still fails closed on layout mismatch.
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    assert asset.version == "0.8.1"
    assert asset.name == "audio-v0.8.1-bin-ubuntu-x64-cuda12.8-colab.tar.gz"
    assert asset.sha256 == "f969811783f206b6d1f6566c020211ab9df7b6bb96c6eded6ad7a58deb725025"
    assert asset.release_base == AUDIO_CPP_RELEASE_BASE
    assert asset.kind == "cuda_binary"


def test_linux_cuda_does_not_require_a_pandrator_build_workflow():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml"
    assert not workflow.exists()
