"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_RELEASE_BASE,
    AUDIO_CPP_VERSION,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_091_pins():
    assert AUDIO_CPP_VERSION == "0.9.1"
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.version == "0.9.1"
            assert "v0.9.1" in asset.name
            assert "/v0.9.1/" in asset.url
            assert asset.release_base == AUDIO_CPP_RELEASE_BASE
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    assert cpu.name == "audio-v0.9.1-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert cpu.sha256 == "a73fed5d83b9d411b7b2aac1556396b81fcba57efcb4f03bc3d690ab0db8bfef"
    assert vulkan.name == "audio-v0.9.1-bin-ubuntu-x64-vulkan-portable.tar.gz"
    assert vulkan.sha256 == "1aae59b03d10c0fa289fe11f4529ef8325bec4dad5c003652b99c23f3d9f5e32"


def test_windows_assets_use_verified_091_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    assert cpu.name == "audio-v0.9.1-bin-windows-x64-cpu-portable.zip"
    assert cpu.sha256 == "2fccc73dc97af84da1e81c3af545fa619348a46d023ea7d927ba80bc8680d2a0"
    assert vulkan.name == "audio-v0.9.1-bin-windows-x64-vulkan.zip"
    assert vulkan.sha256 == "3425c4e60f36c16d50b5418e37b8a5f2811741237a8575ae645c119fbceeadd4"
    assert cuda_binary.name == "audio-v0.9.1-bin-windows-x64-cuda12.4.zip"
    assert cuda_binary.sha256 == "f32a40f8fb14ac4772c9c25525f97715db228979178e51654da73aa65005c0ef"
    assert cuda_runtime.name == "audio-v0.9.1-cudart-windows-x64-cuda12.4.zip"
    assert cuda_runtime.sha256 == "8bfdce7cb00b5a51560b5ab0d444344d86a2f0d7e2727bb7f18c15ef4734451b"


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


def test_linux_cuda_uses_upstream_091_colab_asset_best_effort():
    # Upstream v0.9.1 publishes only the cuda12.8-colab-tagged Linux binary.
    # It is pinned best-effort: untested on local NVIDIA hardware and without
    # a bundled cudart archive. Staging still fails closed on layout mismatch.
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    assert asset.version == "0.9.1"
    assert asset.name == "audio-v0.9.1-bin-ubuntu-x64-cuda12.8-colab.tar.gz"
    assert asset.sha256 == "de068e8a22eb8f9c229c8ecaf77e12b6340be4d7d91606604fae7ccc48c174b6"
    assert asset.release_base == AUDIO_CPP_RELEASE_BASE
    assert asset.kind == "cuda_binary"


def test_linux_cuda_does_not_require_a_pandrator_build_workflow():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml"
    assert not workflow.exists()
