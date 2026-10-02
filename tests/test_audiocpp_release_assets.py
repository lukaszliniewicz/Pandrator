"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_RELEASE_BASE,
    AUDIO_CPP_VERSION,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_090_pins():
    assert AUDIO_CPP_VERSION == "0.9.0"
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.version == "0.9.0"
            assert "v0.9.0" in asset.name
            assert "/v0.9.0/" in asset.url
            assert asset.release_base == AUDIO_CPP_RELEASE_BASE
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    assert cpu.name == "audio-v0.9.0-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert cpu.sha256 == "cf87b6baa46cf45fc8a2816b8f04f0f3f3fce32cca297231a4da56543f19fe87"
    assert vulkan.name == "audio-v0.9.0-bin-ubuntu-x64-vulkan-portable.tar.gz"
    assert vulkan.sha256 == "ce661a7b39add12e91f1c7cc590d55854a39ec0a2fd04c004b3fed29cb978398"


def test_windows_assets_use_verified_090_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    assert cpu.name == "audio-v0.9.0-bin-windows-x64-cpu-portable.zip"
    assert cpu.sha256 == "3ee19466a1a2b5366364ca8447a4794391dd89671e655ffd01aa421ac1668bfa"
    assert vulkan.name == "audio-v0.9.0-bin-windows-x64-vulkan.zip"
    assert vulkan.sha256 == "f884538138e44528a0bf17bb75dbe7ff7350cb91a14cb72c3e9cc1d4a31d6f1f"
    assert cuda_binary.name == "audio-v0.9.0-bin-windows-x64-cuda12.4.zip"
    assert cuda_binary.sha256 == "f935d399b1cabb96422d9b35278dcabbbe7c16638a0e100a9b983421511eeb91"
    assert cuda_runtime.name == "audio-v0.9.0-cudart-windows-x64-cuda12.4.zip"
    assert cuda_runtime.sha256 == "155377e0b18d568002a6cb252a36dc2b96aaa93606c0e44ace945ec3123564fe"


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


def test_linux_cuda_uses_upstream_090_colab_asset_best_effort():
    # Upstream v0.9.0 publishes only the cuda12.8-colab-tagged Linux binary.
    # It is pinned best-effort: untested on local NVIDIA hardware and without
    # a bundled cudart archive. Staging still fails closed on layout mismatch.
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    assert asset.version == "0.9.0"
    assert asset.name == "audio-v0.9.0-bin-ubuntu-x64-cuda12.8-colab.tar.gz"
    assert asset.sha256 == "c9ed906f918246669c324f0d31f1b7dd80cbe003c35cf8a54932f333b57ca3f6"
    assert asset.release_base == AUDIO_CPP_RELEASE_BASE
    assert asset.kind == "cuda_binary"


def test_linux_cuda_does_not_require_a_pandrator_build_workflow():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml"
    assert not workflow.exists()
