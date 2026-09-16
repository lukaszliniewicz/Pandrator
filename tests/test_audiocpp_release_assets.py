"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_VERSION,
    PANDRATOR_AUDIO_CPP_RELEASE_BASE,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_080_pins():
    assert AUDIO_CPP_VERSION == "0.8.0"
    for (system, _architecture, backend), assets in ASSETS.items():
        if system == "linux" and backend == ComputeVariant.CUDA:
            continue
        for asset in assets:
            assert asset.version == "0.8.0"
            assert "v0.8.0" in asset.name
            assert "/v0.8.0/" in asset.url
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    assert cpu.name == "audio-v0.8.0-bin-ubuntu-x64-cpu-portable.tar.gz"
    assert cpu.sha256 == "273678101638072dfec69e6c01c75a050ce5d69554e5d6376062e67d99ae95a4"
    assert vulkan.name == "audio-v0.8.0-bin-ubuntu-x64-vulkan-portable.tar.gz"
    assert vulkan.sha256 == "2566b1c5d5fa9cebebf7d77ae7e955aa07c45052dd3f3a3ab635a1f3a27c2823"


def test_windows_assets_use_verified_080_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    assert cpu.name == "audio-v0.8.0-bin-windows-x64-cpu-portable.zip"
    assert cpu.sha256 == "7c562e5008ec39be3758554d08ef3abfa890ecdc1218c57b130ea8a2bbbf7b68"
    assert vulkan.name == "audio-v0.8.0-bin-windows-x64-vulkan.zip"
    assert vulkan.sha256 == "76ead7b2c9d268e2b1a17168815b491de784b817da40f7eb7415140b580da717"
    assert cuda_binary.name == "audio-v0.8.0-bin-windows-x64-cuda12.4.zip"
    assert cuda_binary.sha256 == "54cec128eb0df4b74a06737e39868c5fe1bd551231dce1f96b9ab61b2dbc6b53"
    assert cuda_runtime.name == "audio-v0.8.0-cudart-windows-x64-cuda12.4.zip"
    assert cuda_runtime.sha256 == "8ded289fada63d9357557429362791c05e7ca94ac5890a66e8f763752388016e"


def test_asset_names_versions_digests_and_urls_are_consistent():
    seen_names: set[str] = set()
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.url == f"{asset.release_base}/{asset.name}"
            assert f"v{asset.version}" in asset.name
            if asset.release_base != PANDRATOR_AUDIO_CPP_RELEASE_BASE:
                assert f"/v{asset.version}/" in asset.url
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)
            assert asset.name not in seen_names
            seen_names.add(asset.name)
    assert AUDIO_CPP_MODEL_REVISION == "dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c"


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
