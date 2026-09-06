"""Pinned audio.cpp runtime assets and model package metadata."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..context import ManagerContext
from ..models import ComputeVariant
from .host import compute_choices, normalized_architecture, resolve_auto_compute

AUDIO_CPP_VERSION = "0.7.2"
AUDIO_CPP_RELEASE_BASE = (
    f"https://github.com/0xShug0/audio.cpp/releases/download/v{AUDIO_CPP_VERSION}"
)
PANDRATOR_AUDIO_CPP_RELEASE_BASE = (
    "https://github.com/lukaszliniewicz/Pandrator/releases/download/v.0.8.18"
)
AUDIO_CPP_MODEL_REPOSITORY = "audio-cpp/audio.cpp-gguf"
AUDIO_CPP_MODEL_REVISION = "dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c"
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

    @property
    def url(self) -> str:
        return f"{self.release_base}/{self.name}"


@dataclass(frozen=True, slots=True)
class AudioCppModelPackage:
    """A package id and its stable v0.7.2 model-spec output layout."""

    id: str
    family: str
    target_directory: str
    files: tuple[str, ...]
    sha256: tuple[str, ...]
    task: str
    mode: str = "offline"
    load_options: dict[str, str] | None = None
    session_options: dict[str, str] | None = None

    @property
    def config_path(self) -> str:
        if self.id == "pocket_tts_english_q8_0":
            return f"models/{self.target_directory}"
        return f"models/{self.target_directory}/{self.files[0]}"

    def marker_path(self, models_root: Path) -> Path:
        return models_root / self.target_directory / f".audiocpp-package-{self.id}.json"

    def required_paths(self, models_root: Path) -> tuple[Path, ...]:
        root = models_root / self.target_directory
        return tuple(root / relative for relative in self.files)


SUPPORTED_MODEL_IDS = (
    "qwen3_tts_1_7b_base_q8_0",
    "qwen3_tts_1_7b_customvoice_q8_0",
    "fish_audio_s2_pro_q8_0",
    "voxcpm2_q8_0",
    "magpie_tts_q8_0",
    "chatterbox_q8_0",
    "omnivoice_q8_0",
    "pocket_tts_english_q8_0",
    "fireredtts3_base_q8_0",
    "breeze_tts_2_q8_0",
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
}

# Public alias matching the naming used by the other native driver.
ASSETS: dict[tuple[str, str, ComputeVariant], tuple[AudioCppAsset, ...]] = {
    ("linux", "x86_64", ComputeVariant.CPU): (
        AudioCppAsset(
            "audio-v0.7.2-bin-ubuntu-x64-cpu.tar.gz",
            "6f5e43dd7b80e8ddf688ef84b411fadcd1f934d2c83963178bc4e2d9c4f07736",
            ComputeVariant.CPU,
        ),
    ),
    ("linux", "x86_64", ComputeVariant.VULKAN): (
        AudioCppAsset(
            "audio-v0.7.2-bin-ubuntu-x64-vulkan.tar.gz",
            "fee1f978cee76453cf17f00196554bc2ee294645739538af0726a143b6a69a23",
            ComputeVariant.VULKAN,
        ),
    ),
    ("linux", "x86_64", ComputeVariant.CUDA): (
        AudioCppAsset(
            "audio.cpp-v0.7.2-linux-x86_64-cuda12.tar.gz",
            "fb0f082a1226f38bc0a2ab1373891012243959d6a497df904a1498cbadcbc378",
            ComputeVariant.CUDA,
            kind="cuda_binary",
            release_base=PANDRATOR_AUDIO_CPP_RELEASE_BASE,
        ),
    ),
    ("windows", "x86_64", ComputeVariant.CPU): (
        AudioCppAsset(
            "audio-v0.7.2-bin-windows-x64-cpu-portable.zip",
            "0b1f4bd78c5226ee3fa0eb24d95d603a429439cdf5dab45872d44a87412dd8c1",
            ComputeVariant.CPU,
        ),
    ),
    ("windows", "x86_64", ComputeVariant.VULKAN): (
        AudioCppAsset(
            "audio-v0.7.2-bin-windows-x64-vulkan.zip",
            "15b8232eae740e21e507d87f827a89966de9451b085a45932d9e214e032962c1",
            ComputeVariant.VULKAN,
        ),
    ),
    ("windows", "x86_64", ComputeVariant.CUDA): (
        AudioCppAsset(
            "audio-v0.7.2-bin-windows-x64-cuda12.4.zip",
            "06c426095008022a2984ff1c75de4c9fab463c4201c0ff0a5dc4e14043f52326",
            ComputeVariant.CUDA,
            kind="cuda_binary",
        ),
        AudioCppAsset(
            "audio-v0.7.2-cudart-windows-x64-cuda12.4.zip",
            "7115be4d462817ad293f7932a8ac436d51023128e6728af09bba92a85593f393",
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
