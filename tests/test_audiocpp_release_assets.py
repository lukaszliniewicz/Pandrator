"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_VERSION,
    PANDRATOR_AUDIO_CPP_RELEASE_BASE,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_074_pins():
    assert AUDIO_CPP_VERSION == "0.7.4"
    for (system, _architecture, backend), assets in ASSETS.items():
        if system == "linux" and backend == ComputeVariant.CUDA:
            continue
        for asset in assets:
            assert asset.version == "0.7.4"
            assert "v0.7.4" in asset.name
            assert "/v0.7.4/" in asset.url
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    assert cpu.name == "audio-v0.7.4-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert cpu.sha256 == "8a93751b832c533e3261e760b4fd24af2397af3a193c862315653afd0dccc6ad"
    assert vulkan.name == "audio-v0.7.4-bin-ubuntu-x64-vulkan-portable.tar.gz"
    assert vulkan.sha256 == "34a46387c4151bf8bd0bbbaac46fc6de57df539a177ab23d52ecd5aa940173b1"


def test_linux_cuda_stays_on_existing_verified_release():
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    assert asset.version == "0.7.2"
    assert asset.name == "audio.cpp-v0.7.2-linux-x86_64-cuda12.tar.gz"
    assert asset.sha256 == "fb0f082a1226f38bc0a2ab1373891012243959d6a497df904a1498cbadcbc378"
    assert asset.release_base == PANDRATOR_AUDIO_CPP_RELEASE_BASE


def test_future_cuda_build_is_manual_and_pinned_to_074():
    workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml").read_text()
    assert "on:\n  workflow_dispatch:" in workflow
    assert "\n  push:" not in workflow
    assert "\n  pull_request:" not in workflow
    assert workflow.count("5ba81ac54fb071b835680973f8868546b4db372b") == 2
    assert "PACKAGE_NAME: audio.cpp-v0.7.4-linux-x86_64-cuda12" in workflow
    assert "v0.7.2" not in workflow
    assert "v072" not in workflow
