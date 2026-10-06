"""Pinned audio.cpp runtime assets and model package metadata."""

from __future__ import annotations

from dataclasses import dataclass

from ..audio_cpp_packages import (
    AUDIO_CPP_MODEL_REPOSITORY as AUDIO_CPP_MODEL_REPOSITORY,
)
from ..audio_cpp_packages import (
    AUDIO_CPP_MODEL_REVISION as AUDIO_CPP_MODEL_REVISION,
)
from ..audio_cpp_packages import (
    AudioCppModelPackage as AudioCppModelPackage,
)
from ..context import ManagerContext
from ..models import ComputeVariant
from .host import compute_choices, normalized_architecture, resolve_auto_compute

AUDIO_CPP_VERSION = "0.9.0"
AUDIO_CPP_RELEASE_BASE = (
    f"https://github.com/0xShug0/audio.cpp/releases/download/v{AUDIO_CPP_VERSION}"
)
AUDIO_CPP_MAX_REQUEST_BODY_BYTES = 8 * 1024 * 1024
AUDIO_CPP_DEFAULT_MODEL = "qwen3_tts_1_7b_base_q8_0"
AUDIO_CPP_PORT = 8060


@dataclass(frozen=True, slots=True)
class AudioCppAsset:
    """One digest-verified native runtime archive."""

    name: str
    sha256: str
    runtime_variant: ComputeVariant
    kind: str = "runtime"
    release_base: str = AUDIO_CPP_RELEASE_BASE
    # Some platforms intentionally retain a separately pinned older runtime.
    # Never label that archive with the newest global catalogue version.
    version: str = AUDIO_CPP_VERSION

    @property
    def url(self) -> str:
        return f"{self.release_base}/{self.name}"


MANUAL_MODEL_IDS = (
    "qwen3_tts_1_7b_base_q8_0",
    "qwen3_tts_1_7b_customvoice_q8_0",
    "qwen3_tts_1_7b_voicedesign_q8_0",
    "fish_audio_s2_pro_q8_0",
    "voxcpm2_q8_0",
    "magpie_tts_q8_0",
    "chatterbox_q8_0",
    "omnivoice_q8_0",
    "pocket_tts_english_q8_0",
    "fireredtts3_base_q8_0",
    "breeze_tts_2_q8_0",
    "qwen3_tts_0_6b_base_q8_0",
    "chatterbox_turbo_q8_0",
    "supertonic_3_q8_0",
    "fireredtts3_instruct_q8_0",
)


MODEL_PACKAGES: dict[str, AudioCppModelPackage] = {
    "qwen3_tts_1_7b_base_q8_0": AudioCppModelPackage(
        id="qwen3_tts_1_7b_base_q8_0",
        family="qwen3_tts",
        target_directory="Qwen3-TTS-12Hz-1.7B-Base-GGUF",
        files=("qwen3-tts-12hz-1.7b-base-q8_0_v2.gguf",),
        sha256=("b55e06c7890d43c208d15aed8b4ed3f18215f295e47d5960e061b15bff338ab0",),
        task="tts",
    ),
    "qwen3_tts_1_7b_customvoice_q8_0": AudioCppModelPackage(
        id="qwen3_tts_1_7b_customvoice_q8_0",
        family="qwen3_tts",
        target_directory="Qwen3-TTS-12Hz-1.7B-CustomVoice-GGUF",
        files=("qwen3-tts-12hz-1.7b-customvoice-q8_0.gguf",),
        sha256=("3cfaac8e9f13554f6daea3c5e0c53fede71ef5500cbaae7445e5fc3a5bb12e72",),
        task="tts",
    ),
    "qwen3_tts_1_7b_voicedesign_q8_0": AudioCppModelPackage(
        id="qwen3_tts_1_7b_voicedesign_q8_0",
        family="qwen3_tts",
        target_directory="Qwen3-TTS-12Hz-1.7B-VoiceDesign-GGUF",
        files=("qwen3-tts-12hz-1.7b-voicedesign-q8_0.gguf",),
        sha256=("1bcef9a8c021072fca40e00498e00af9091fbe6d3ae4f87567cfee885d6c7554",),
        task="vdes",
    ),
    "fish_audio_s2_pro_q8_0": AudioCppModelPackage(
        id="fish_audio_s2_pro_q8_0",
        family="fish_audio",
        target_directory="Fish-Audio-S2-Pro-GGUF",
        files=("fish-audio-s2-pro-q8_0.gguf",),
        sha256=("4ffc169447b7a26df8bf49e8637adb4000bfa763a22c018b6c03968564259d0b",),
        task="tts",
    ),
    "voxcpm2_q8_0": AudioCppModelPackage(
        id="voxcpm2_q8_0",
        family="voxcpm2",
        target_directory="VoxCPM2-GGUF",
        files=("voxcpm2-q8_0.gguf",),
        sha256=("c8e01ab4416011e12a28f24ede298a1aa5ce64b43f8e8aaad53b1e2fe7c96432",),
        task="tts",
    ),
    "magpie_tts_q8_0": AudioCppModelPackage(
        id="magpie_tts_q8_0",
        family="magpie_tts",
        target_directory="MagpieTTS-Multilingual-357M-GGUF",
        files=("magpie-tts-multilingual-357m-q8_0.gguf",),
        sha256=("c762503a80f9af75db33379763b923c5c8a00ca79374e1e0d4051e6d68151377",),
        task="tts",
    ),
    "chatterbox_q8_0": AudioCppModelPackage(
        id="chatterbox_q8_0",
        family="chatterbox",
        target_directory="Chatterbox-GGUF",
        files=("chatterbox-q8_0.gguf",),
        sha256=("d586dd1aa59613cab8046176fb7ca5ba191c02a9b10ffa5b0d892ed22b470656",),
        task="clon",
    ),
    "omnivoice_q8_0": AudioCppModelPackage(
        id="omnivoice_q8_0",
        family="omnivoice",
        target_directory="OmniVoice-GGUF",
        files=("omnivoice-q8_0.gguf",),
        sha256=("2f4be637278043c6842de5b85d681532030e9eb6ffe0f8b0e320f68238e3da8b",),
        task="tts",
    ),
    "pocket_tts_english_q8_0": AudioCppModelPackage(
        id="pocket_tts_english_q8_0",
        family="pocket_tts",
        target_directory="PocketTTS-GGUF/english",
        files=("pocket-tts-english-q8_0.gguf", "embeddings/alba.safetensors"),
        sha256=(
            "0315406421d515d9ffbde49ed998832ff2962562ef8abde440c85fa0a27d8b2a",
            "69c32db63ca56843d994f81f343f62e0bf2d73f7e4c9bc73e44bb1110b1d8845",
        ),
        task="tts",
        load_options={"language": "english"},
        session_options={"language": "english"},
    ),
    "fireredtts3_base_q8_0": AudioCppModelPackage(
        id="fireredtts3_base_q8_0",
        family="fireredtts3",
        target_directory="FireRedTTS3-Base-GGUF",
        files=("fireredtts3-base-q8_0.gguf",),
        sha256=("68acd5bce0d87a53bb5b88255c65e19df4cbc6017b4bab0824e96f1e2351c3a7",),
        task="clon",
    ),
    "breeze_tts_2_q8_0": AudioCppModelPackage(
        id="breeze_tts_2_q8_0",
        family="breeze_tts",
        target_directory="Breeze-TTS-2-GGUF",
        files=("breeze-tts-2-q8_0.gguf",),
        sha256=("0de52d61560f9f6b2dfeca79f9100f8fce0c2b17c52ec30622e23e150df1ad88",),
        task="tts",
    ),
    "qwen3_tts_0_6b_base_q8_0": AudioCppModelPackage(
        id="qwen3_tts_0_6b_base_q8_0",
        family="qwen3_tts",
        target_directory="Qwen3-TTS-12Hz-0.6B-Base-GGUF",
        files=("qwen3-tts-12hz-0.6b-base-q8_0.gguf",),
        sha256=("771420bd20ff5f35407b4fa9cf9c5461e153800d3d772ef51c9febc0a520855d",),
        task="clon",
    ),
    "chatterbox_turbo_q8_0": AudioCppModelPackage(
        id="chatterbox_turbo_q8_0",
        family="chatterbox_turbo",
        target_directory="Chatterbox-Turbo-GGUF",
        files=("chatterbox-turbo-q8_0.gguf",),
        sha256=("6eed51ff0b2993fec67db6211dca92de5819d83ac50513ba7c065c467eb75910",),
        task="tts",
    ),
    "supertonic_3_q8_0": AudioCppModelPackage(
        id="supertonic_3_q8_0",
        family="supertonic",
        target_directory="Supertonic-3-GGUF",
        files=("supertonic-3-q8_0.gguf",),
        sha256=("af814486a0bc9513fb36afabd9b1155ad14fb2c36a107ac6ffe62ea9adafb662",),
        task="tts",
    ),
    "fireredtts3_instruct_q8_0": AudioCppModelPackage(
        id="fireredtts3_instruct_q8_0",
        family="fireredtts3",
        target_directory="FireRedTTS3-Instruct-GGUF",
        files=("fireredtts3-instruct-q8_0.gguf",),
        sha256=("04cd0ff6624bbfdc6b39e2dd1a0068f9e9769d1c217bf7343e28f3415eb2859e",),
        task="tts",
    ),
}


try:
    from ..audio_cpp_inventory import load_audio_cpp_packages

    _INVENTORY_PACKAGES = load_audio_cpp_packages()
except (ImportError, OSError, ValueError):
    # The manager remains usable with its reviewed built-in packages when a
    # mirrored inventory is absent from a source checkout or an older wheel.
    _INVENTORY_PACKAGES = ()

for _package in _INVENTORY_PACKAGES:
    if _package.id not in MODEL_PACKAGES:
        MODEL_PACKAGES[_package.id] = _package

SUPPORTED_MODEL_IDS = MANUAL_MODEL_IDS + tuple(
    package.id for package in _INVENTORY_PACKAGES if package.id not in MANUAL_MODEL_IDS
)

# Public alias matching the naming used by the other native driver.
# Digests: upstream v0.9.0 release assets, published 2026-09-30. Portable Linux
# builds avoid requiring the release runner's CPU instruction set. Windows CUDA
# stays on the 12.4 binary/runtime pair for driver compatibility; upstream also
# publishes a 13.3 pair which Pandrator does not pin.
ASSETS: dict[tuple[str, str, ComputeVariant], tuple[AudioCppAsset, ...]] = {
    ("linux", "x86_64", ComputeVariant.CPU): (
        AudioCppAsset(
            "audio-v0.9.0-bin-ubuntu-x64-cpu-portable.tar.gz",
            "cf87b6baa46cf45fc8a2816b8f04f0f3f3fce32cca297231a4da56543f19fe87",
            ComputeVariant.CPU,
        ),
    ),
    ("linux", "x86_64", ComputeVariant.VULKAN): (
        AudioCppAsset(
            "audio-v0.9.0-bin-ubuntu-x64-vulkan-portable.tar.gz",
            "ce661a7b39add12e91f1c7cc590d55854a39ec0a2fd04c004b3fed29cb978398",
            ComputeVariant.VULKAN,
        ),
    ),
    # Best-effort Linux CUDA: upstream publishes only the
    # cuda12.8-colab-tagged binary for 0.9.0, with no bundled cudart archive
    # (unlike Windows). It needs a compatible CUDA 12 runtime/driver plus NCCL 2 and has not
    # been tested on local NVIDIA hardware. Staging fails closed when the
    # archive layout differs from the expected archive-root files.
    ("linux", "x86_64", ComputeVariant.CUDA): (
        AudioCppAsset(
            "audio-v0.9.0-bin-ubuntu-x64-cuda12.8-colab.tar.gz",
            "c9ed906f918246669c324f0d31f1b7dd80cbe003c35cf8a54932f333b57ca3f6",
            ComputeVariant.CUDA,
            kind="cuda_binary",
        ),
    ),
    ("windows", "x86_64", ComputeVariant.CPU): (
        AudioCppAsset(
            "audio-v0.9.0-bin-windows-x64-cpu-portable.zip",
            "3ee19466a1a2b5366364ca8447a4794391dd89671e655ffd01aa421ac1668bfa",
            ComputeVariant.CPU,
        ),
    ),
    ("windows", "x86_64", ComputeVariant.VULKAN): (
        AudioCppAsset(
            "audio-v0.9.0-bin-windows-x64-vulkan.zip",
            "f884538138e44528a0bf17bb75dbe7ff7350cb91a14cb72c3e9cc1d4a31d6f1f",
            ComputeVariant.VULKAN,
        ),
    ),
    ("windows", "x86_64", ComputeVariant.CUDA): (
        AudioCppAsset(
            "audio-v0.9.0-bin-windows-x64-cuda12.4.zip",
            "f935d399b1cabb96422d9b35278dcabbbe7c16638a0e100a9b983421511eeb91",
            ComputeVariant.CUDA,
            kind="cuda_binary",
        ),
        AudioCppAsset(
            "audio-v0.9.0-cudart-windows-x64-cuda12.4.zip",
            "155377e0b18d568002a6cb252a36dc2b96aaa93606c0e44ace945ec3123564fe",
            ComputeVariant.CUDA,
            kind="cuda_runtime",
        ),
    ),
}


def model_package(package_id: str) -> AudioCppModelPackage:
    try:
        return MODEL_PACKAGES[package_id]
    except KeyError:
        raise ValueError(f"audio.cpp does not support model package {package_id!r}.") from None


def _platform_error(system: str, architecture: str, compute: ComputeVariant) -> ValueError:
    return ValueError(
        f"audio.cpp has no pinned v{AUDIO_CPP_VERSION} runtime artifact for "
        f"{system}/{architecture}/{compute.value}; supported targets are "
        "Windows and Linux x86_64 with CPU, Vulkan, or CUDA."
    )


def resolve_assets(
    context: ManagerContext,
    requested: ComputeVariant,
    definition,
) -> tuple[tuple[AudioCppAsset, ...], ComputeVariant]:
    """Resolve AUTO using host policy and fail closed for unpinned targets."""

    system = context.system.strip().lower()
    architecture = normalized_architecture(context.architecture)
    effective = (
        resolve_auto_compute(context, definition) if requested == ComputeVariant.AUTO else requested
    )
    assets = ASSETS.get((system, architecture, effective))
    if assets is None:
        raise _platform_error(system, architecture, effective)
    availability = {item["value"]: item for item in compute_choices(context, definition)}
    selected = availability.get(effective.value)
    if selected is not None and not selected["available"]:
        raise ValueError(f"audio.cpp cannot use {effective.value.upper()}: {selected['reason']}")
    return assets, effective


def resolve_asset(
    context: ManagerContext,
    requested: ComputeVariant,
    definition,
) -> tuple[tuple[AudioCppAsset, ...], ComputeVariant]:
    """Backward-compatible singular helper returning all required archives."""

    return resolve_assets(context, requested, definition)


def source_markers_for(system: str) -> tuple[str, ...]:
    """Return platform-specific marker paths used by inspection and verify."""

    suffix = ".exe" if str(system).strip().lower() == "windows" else ""
    return (f"audiocpp_server{suffix}", "tools/model_manager_v2.py", "server.json")


def server_config(
    backend: ComputeVariant | str,
    model_ids: tuple[str, ...] | list[str],
) -> dict:
    """Build the locked-down local audio.cpp server configuration."""

    selected = tuple(model_ids)
    if not selected:
        raise ValueError("audio.cpp requires at least one model package.")
    if len(selected) != len(set(selected)):
        raise ValueError("audio.cpp model packages must be unique.")
    models: list[dict] = []
    for package_id in selected:
        package = model_package(package_id)
        entry = {
            "id": package.id,
            "family": package.family,
            "path": package.config_path,
            "task": package.task,
            "mode": package.mode,
        }
        if package.load_options:
            entry["load_options"] = dict(package.load_options)
        if package.session_options:
            entry["session_options"] = dict(package.session_options)
        models.append(entry)
    selected_backend = str(backend.value if isinstance(backend, ComputeVariant) else backend)
    if selected_backend not in {"cpu", "vulkan", "cuda"}:
        raise ValueError(f"audio.cpp does not support backend {selected_backend!r}.")
    return {
        "host": "127.0.0.1",
        "port": AUDIO_CPP_PORT,
        "backend": selected_backend,
        "device": 0,
        "threads": 4,
        "ui": False,
        "ui_management": False,
        "lazy_load": True,
        "max_loaded_models": 1,
        "idle_unload_ms": 0,
        "log_request_body": False,
        "max_request_body_bytes": AUDIO_CPP_MAX_REQUEST_BODY_BYTES,
        "models": models,
    }
