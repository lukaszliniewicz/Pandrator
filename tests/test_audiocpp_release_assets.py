"""Release pins must describe the binary actually installed on each platform."""

from pathlib import Path

from pandrator_manager.components.audiocpp import (
    ASSETS,
    AUDIO_CPP_MODEL_REVISION,
    AUDIO_CPP_RELEASE_BASE,
    AUDIO_CPP_VERSION,
)
from pandrator_manager.models import ComputeVariant


def test_upstream_runtime_assets_use_verified_0_9_3_pins():
    assert AUDIO_CPP_VERSION == "0.9.3"
    for assets in ASSETS.values():
        for asset in assets:
            assert asset.version == "0.9.3"
            assert "v0.9.3" in asset.name
            assert "/v0.9.3/" in asset.url
            assert asset.release_base == AUDIO_CPP_RELEASE_BASE
            assert len(asset.sha256) == 64
            assert all(character in "0123456789abcdef" for character in asset.sha256)


def test_linux_portable_assets_are_pinned():
    cpu = ASSETS[("linux", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("linux", "x86_64", ComputeVariant.VULKAN)][0]
    _assert_release_pin(
        cpu,
        "audio-v0.9.3-bin-ubuntu-x64-cpu-portable.tar.gz",
        "02156f7a7bd9924902cec26be35087d3806c7e348be344b1ec28cac0c1af3929",
    )
    _assert_release_pin(
        vulkan,
        "audio-v0.9.3-bin-ubuntu-x64-vulkan-portable.tar.gz",
        "10ac6b5cb72bc9266e5a600407dbcc2a3eaf87139a3e01c064bf3035d11f81b8",
    )


def test_windows_assets_use_verified_0_9_3_pins():
    cpu = ASSETS[("windows", "x86_64", ComputeVariant.CPU)][0]
    vulkan = ASSETS[("windows", "x86_64", ComputeVariant.VULKAN)][0]
    cuda_binary, cuda_runtime = ASSETS[("windows", "x86_64", ComputeVariant.CUDA)]
    _assert_release_pin(
        cpu,
        "audio-v0.9.3-bin-windows-x64-cpu-portable.zip",
        "f5fa4662b7ec323dbe31d37e224e46159b064836a9005da384551a5248771eeb",
    )
    _assert_release_pin(
        vulkan,
        "audio-v0.9.3-bin-windows-x64-vulkan-portable.zip",
        "51f8d8ce6cc7789bc9dc3d15f35e05f143aaf2ce1cbecc525a4104bcdde82a42",
    )
    _assert_release_pin(
        cuda_binary,
        "audio-v0.9.3-bin-windows-x64-cuda12.4.zip",
        "976b1fe4329407be45b272fc656d91006f081d22706f453f8879bc69dbb4a9b3",
    )
    _assert_release_pin(
        cuda_runtime,
        "audio-v0.9.3-cudart-windows-x64-cuda12.4.zip",
        "866a2bc12c4d887eb3b1b62f82f7a8fdd93391cb77ea02b63b0f360cfb3bb4d8",
    )


def _assert_release_pin(asset, name: str, sha256: str) -> None:
    assert asset.name == name
    assert asset.sha256 == sha256
    assert asset.version == "0.9.3"
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


def test_linux_cuda_uses_upstream_0_9_3_colab_asset_best_effort():
    # Upstream v0.9.3 publishes only the cuda12.8-colab-tagged Linux binary.
    # It is pinned best-effort: untested on local NVIDIA hardware, with the
    # CUDA runtime supplied by the host. Staging fails closed on layout mismatch.
    asset, = ASSETS[("linux", "x86_64", ComputeVariant.CUDA)]
    _assert_release_pin(
        asset,
        "audio-v0.9.3-bin-ubuntu-x64-cuda12.8-colab.tar.gz",
        "5be3e8d515fb244387de87958668ebb729d9442de6a53df12e4c718ea918419d",
    )
    assert asset.kind == "cuda_binary"


def test_linux_cuda_does_not_require_a_pandrator_build_workflow():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/audio-cpp-linux-cuda.yml"
    assert not workflow.exists()
