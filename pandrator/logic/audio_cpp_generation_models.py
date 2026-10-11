"""Verified, task-scoped audio.cpp generation model registry.

The repository inventory is the source of package file pins. This module only
admits the eight generation families in ``_SUPPORTED_FAMILIES`` and only when a
package has an immutable manifest revision and complete size/SHA-256 pins.
Unavailable package rows are projected separately and are never installable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from .audio_cpp_generation_parameters import parameters_for_model

DEFAULT_MODEL_ID = "stable_audio_3_small_sfx_q8_0"
MINIMUM_RUNTIME_VERSION = "0.9.1"

_SUPPORTED_FAMILIES = frozenset(
    {
        "stable_audio",
        "controlfoley",
        "midashenglm_gen",
        "ace_step",
        "heartmula",
        "minimax_music3",
        "minimax_h3",
        "yue2",
    }
)
_SOURCE_REPOSITORIES = frozenset(
    {
        "audio-cpp/audio.cpp-gguf",
        "CaptainArni/audio.cpp-gguf",
        "audio-cpp/MiniMax-Music3-GGUF",
        "audio-cpp/Yue2-3B-GGUF",
    }
)
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODEL_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_]{0,127}$")
_MODEL_ORDER = {
    "stable_audio": 0,
    "controlfoley": 1,
    "midashenglm_gen": 2,
    "ace_step": 3,
    "heartmula": 4,
    "minimax_music3": 5,
    "minimax_h3": 6,
    "yue2": 7,
}
_YUE2_IDS = {
    "yue2_main_bf16": "yue2-3b-bf16.gguf",
    "yue2_main_q4_0": "yue2-3b-q4_0.gguf",
    "yue2_main_q8_0": "yue2-3b-q8_0.gguf",
}
_YUE2_SHARED_PATHS = frozenset(
    {
        "sidecars/yue2-model-config.json",
        "sidecars/yue2-generation-config.json",
        "sidecars/yue2-qwen.tiktoken",
        "sidecars/yue2-vae-config.json",
        "yue2-vae-f16.gguf",
    }
)
_H3_SHARED_PATHS = frozenset(
    {
        "configuration.json",
        "FL2VA/processor/chat_template.json",
        "FL2VA/processor/merges.txt",
        "FL2VA/processor/preprocessor_config.json",
        "FL2VA/processor/tokenizer.json",
        "FL2VA/processor/tokenizer_config.json",
        "FL2VA/processor/video_preprocessor_config.json",
        "FL2VA/processor/vocab.json",
        "text_encoder_q4_k.gguf",
        "audio_vae_folded_f16.gguf",
        "video_vae.gguf",
    }
)
_EXPECTED_NEW_MODEL_IDS = frozenset(
    {
        *_YUE2_IDS,
        "minimax_h3_q4_k",
        "minimax_h3_q4_k_int8_dit",
    }
)
_GENERAL_BACKENDS = ("cpu", "vulkan", "cuda")


@dataclass(frozen=True, slots=True)
class AssetFile:
    """One manifest-pinned file in a model package."""

    path: str
    source_path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class ModelSpec:
    """Immutable runtime-facing model metadata and its verified assets."""

    id: str
    label: str
    family: str
    description: str
    repository: str
    target_directory: str
    revision: str
    files: tuple[AssetFile, ...]
    kinds: tuple[str, ...]
    duration_minimum: float
    duration_maximum: float
    duration_default: float
    supports_lyrics: bool
    requires_lyrics: bool
    seed_maximum: int
    parameters: tuple[tuple[str, dict[str, Any]], ...]
    backends: tuple[str, ...]
    duration_description: str | None
    notes: str | None

    @property
    def download_bytes(self) -> int:
        return sum(asset.size_bytes for asset in self.files)

    @property
    def is_directory_model(self) -> bool:
        # Yue 2 and Music 3 need their complete component packages. H3 also
        # downloads its full package, but the native CLI receives the exact DiT.
        return self.family in {"minimax_music3", "yue2"}

    @property
    def model_path_asset(self) -> AssetFile:
        if self.is_directory_model:
            raise ValueError(f"{self.id} is a directory model.")
        if self.family == "minimax_h3":
            expected = "dit_int8.gguf" if self.id.endswith("_int8_dit") else "dit.gguf"
            matches = [asset for asset in self.files if asset.path == expected]
            if len(matches) != 1:
                raise ValueError(f"{self.id} must pin its exact {expected} model file.")
            return matches[0]
        if len(self.files) != 1:
            raise ValueError(f"{self.id} must have exactly one direct model file.")
        return self.files[0]

    def parameter_descriptors(self) -> dict[str, dict[str, Any]]:
        return {name: dict(descriptor) for name, descriptor in self.parameters}

    def descriptor(self, *, cached: bool = False) -> dict[str, Any]:
        descriptor: dict[str, Any] = {
            "id": self.id,
            "label": self.label,
            "family": self.family,
            "cached": bool(cached),
            "kinds": list(self.kinds),
            "description": self.description,
            "source_url": f"https://huggingface.co/{self.repository}",
            # These describe build choices. Family-level inference support is
            # intentionally not claimed by this catalogue field.
            "backends": list(self.backends),
            "duration": {
                "minimum": self.duration_minimum,
                "maximum": self.duration_maximum,
                "default": self.duration_default,
            },
            "supports_lyrics": self.supports_lyrics,
            "requires_lyrics": self.requires_lyrics,
            "seed_maximum": self.seed_maximum,
            "parameters": self.parameter_descriptors(),
            "download_bytes": self.download_bytes,
            "available": True,
            "reason": None,
        }
        if self.duration_description is not None:
            descriptor["duration"]["description"] = self.duration_description
        if self.notes is not None:
            descriptor["notes"] = self.notes
        return descriptor




def _safe_relative_path(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError(f"Inventory {field} must be a safe relative POSIX path.")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {"", ".", ".."} for part in value.split("/"))
        or any(":" in part for part in path.parts)
    ):
        raise ValueError(f"Inventory {field} must be a safe relative POSIX path.")
    return path.as_posix()


def validate_weight_manifest(row: dict[str, Any]) -> tuple[str, str, tuple[AssetFile, ...]]:
    """Validate and freeze the asset portion of an inventory package row."""

    if not isinstance(row, dict):
        raise ValueError("Inventory package row must be an object.")
    if row.get("family") not in _SUPPORTED_FAMILIES or row.get("format") != "gguf":
        raise ValueError("Only allowlisted GGUF generation packages are eligible.")
    download = row.get("download")
    manifest = row.get("weight_manifest")
    if not isinstance(download, dict) or not isinstance(manifest, dict):
        raise ValueError("Package must provide download metadata and weight_manifest.")
    if download.get("kind") != "huggingface_snapshot":
        raise ValueError("Package must use a pinned Hugging Face snapshot.")
    availability = row.get("availability")
    if not isinstance(availability, dict):
        raise ValueError("Package must provide availability metadata.")
    if download.get("gated") is not False or availability.get("gated") is not False:
        raise ValueError("Gated packages are not eligible for generation.")
    repository = manifest.get("repository")
    if (
        not isinstance(repository, str)
        or repository not in _SOURCE_REPOSITORIES
        or download.get("repo") != repository
    ):
        raise ValueError("Package repository is outside the generation source allowlist.")
    revision = manifest.get("revision")
    if not isinstance(revision, str) or not _REVISION_RE.fullmatch(revision):
        raise ValueError("Package weight_manifest revision must be an immutable 40-hex commit.")
    raw_files = manifest.get("files")
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError("Package weight_manifest must list at least one file.")
    files: list[AssetFile] = []
    seen: set[str] = set()
    for item in raw_files:
        if not isinstance(item, dict):
            raise ValueError("Every weight_manifest file must be an object.")
        path = _safe_relative_path(item.get("path"), "file.path")
        source_path = _safe_relative_path(item.get("source_path"), "file.source_path")
        size = item.get("size")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise ValueError(f"Manifest file {path!r} must have a positive integer size.")
        digest = item.get("sha256")
        if not isinstance(digest, str) or not _SHA256_RE.fullmatch(digest):
            raise ValueError(f"Manifest file {path!r} must have a pinned SHA-256 digest.")
        if path in seen:
            raise ValueError(f"Manifest contains duplicate file path {path!r}.")
        seen.add(path)
        files.append(AssetFile(path, source_path, size, digest))
    if availability.get("verified") is not True:
        raise ValueError("Package inventory row is not verified.")
    if availability.get("missing_files"):
        raise ValueError("Package manifest reports missing files.")
    return repository, revision, tuple(files)


def _duration_for(package_id: str, family: str) -> tuple[float, float, float]:
    if family == "stable_audio":
        if "_sfx_" in package_id:
            return 1.0, 30.0, 4.0
        return 1.0, 380.0 if "_medium_" in package_id else 120.0, 30.0
    if family == "controlfoley":
        return 1.0, 60.0, 8.0
    if family == "midashenglm_gen":
        return 1.0, 163.84, 20.0
    if family == "yue2":
        return 8.0, 360.0, 30.0
    if family == "minimax_h3":
        return 4.0, 10.0, 5.0
    if family == "ace_step":
        return 10.0, 600.0, 30.0
    if family == "heartmula":
        return 1.0, 240.0, 30.0
    return 1.0, 300.0, 20.0


def _kinds_for(package_id: str, family: str) -> tuple[str, ...]:
    if family == "stable_audio":
        if "_medium_" in package_id:
            return ("sound", "music")
        return ("sound",) if "_sfx_" in package_id else ("music",)
    if family == "controlfoley":
        return ("sound",)
    if family == "midashenglm_gen":
        return ("sound", "music")
    if family == "minimax_h3":
        return ("sound", "music")
    return ("music",)


def _validate_generation_layout(
    package_id: str,
    family: str,
    files: tuple[AssetFile, ...],
    target_directory: str,
) -> None:
    """Require the complete pinned component layout for Yue 2 and H3."""
    paths = {item.path for item in files}
    source_paths = {item.source_path for item in files}
    if family == "yue2":
        main_file = _YUE2_IDS.get(package_id)
        if main_file is None:
            raise ValueError(f"Unsupported Yue 2 generation package {package_id!r}.")
        expected = _YUE2_SHARED_PATHS | {main_file}
        expected_target = "Yue2-3B-GGUF"
    elif family == "minimax_h3":
        if package_id == "minimax_h3_q4_k":
            dit_file = "dit.gguf"
        elif package_id == "minimax_h3_q4_k_int8_dit":
            dit_file = "dit_int8.gguf"
        else:
            raise ValueError(f"Unsupported MiniMax H3 generation package {package_id!r}.")
        expected = _H3_SHARED_PATHS | {dit_file}
        expected_target = "MiniMax-H3-Q4-GGUF"
    else:
        return
    expected_source_paths = (
        expected if family == "yue2" else {f"{expected_target}/{path}" for path in expected}
    )
    if (
        target_directory != expected_target
        or paths != expected
        or source_paths != expected_source_paths
        or any(
            item.source_path
            != (item.path if family == "yue2" else f"{expected_target}/{item.path}")
            for item in files
        )
    ):
        raise ValueError(
            f"Package {package_id!r} must contain the complete exact {family} manifest layout."
        )


def _unavailable_reason(row: dict[str, Any]) -> str:
    availability = row.get("availability")
    availability = availability if isinstance(availability, dict) else {}
    missing = availability.get("missing_files")
    manifest = row.get("weight_manifest")
    manifest = manifest if isinstance(manifest, dict) else {}
    files = manifest.get("files")
    unhashed = (
        [
            item.get("path", "unknown")
            for item in files
            if isinstance(files, list) and isinstance(item, dict)
            if not isinstance(item.get("sha256"), str)
            or not _SHA256_RE.fullmatch(item.get("sha256", ""))
        ]
        if isinstance(files, list)
        else []
    )
    details: list[str] = []
    if isinstance(missing, list) and missing:
        details.append("missing files: " + ", ".join(str(item) for item in missing[:4]))
    if unhashed:
        details.append("files without SHA-256 pins: " + ", ".join(unhashed[:4]))
    if not details:
        details.append("the inventory does not verify the complete model package")
    return "Unavailable because " + "; ".join(details) + "."


def build_registry(
    inventory: dict[str, Any],
) -> tuple[dict[str, ModelSpec], list[dict[str, str | bool]]]:
    """Build a registry from inventory data, rejecting unsafe eligible rows."""

    if not isinstance(inventory, dict) or not isinstance(inventory.get("packages"), list):
        raise ValueError("audio.cpp inventory must contain a packages list.")
    families = inventory.get("families")
    if not isinstance(families, list):
        raise ValueError("audio.cpp inventory must contain a families list.")
    family_map = {
        family.get("id"): family
        for family in families
        if isinstance(family, dict) and isinstance(family.get("id"), str)
    }
    models: dict[str, ModelSpec] = {}
    unavailable: list[dict[str, str | bool]] = []
    for row in inventory["packages"]:
        if not isinstance(row, dict):
            continue
        family = row.get("family")
        package_id = row.get("id")
        if not isinstance(family, str) or not isinstance(package_id, str):
            continue
        if family in _SUPPORTED_FAMILIES and not _MODEL_ID_RE.fullmatch(package_id):
            raise ValueError("Eligible package ID must be a safe lowercase identifier.")
        if row.get("entry_role") == "component":
            # The VAE rows describe a shared component, not standalone models.
            continue
        if family not in _SUPPORTED_FAMILIES:
            continue
        if row.get("format") != "gguf":
            continue
        availability = row.get("availability")
        download = row.get("download")
        if not isinstance(availability, dict) or not isinstance(download, dict):
            raise ValueError(f"Eligible package {package_id!r} lacks status metadata.")
        if availability.get("gated") is True or download.get("gated") is True:
            continue
        repository, revision, files = validate_weight_manifest(row)
        if family in {"minimax_h3", "yue2"}:
            _validate_generation_layout(
                package_id,
                family,
                files,
                _safe_relative_path(row.get("target_directory"), "target_directory"),
            )
        duration_minimum, duration_maximum, duration_default = _duration_for(package_id, family)
        lyrics = family in {"ace_step", "heartmula", "minimax_music3", "yue2"}
        backends = ("cuda",) if package_id == "minimax_h3_q4_k_int8_dit" else _GENERAL_BACKENDS
        duration_description = {
            "controlfoley": (
                "60 seconds is this workflow's request ceiling, not an established model maximum. "
                "Start with 8–10-second effects."
            ),
            "midashenglm_gen": (
                "163.84 seconds is the request ceiling. Training covered 1–20-second clips; "
                "longer output quality is unverified and generation may stop sooner."
            ),
            "heartmula": (
                "Maximum length budget; the model may finish sooner. "
                "This workflow caps ordinary clips at 240 seconds and does not enable infinite mode."
            ),
            "minimax_music3": (
                "Maximum length budget, up to the published five-minute limit; "
                "the model may finish sooner."
            ),
            "yue2": "Maximum length budget; the model may finish sooner.",
            "minimax_h3": "Approximate duration, rounded up to the native frame grid.",
        }.get(family)
        if family == "stable_audio":
            duration_description = (
                "This workflow caps short effects at 30 seconds; the checkpoint supports up to 120 seconds."
                if "_sfx_" in package_id
                else "The checkpoint supports up to 380 seconds of effects or music."
                if "_medium_" in package_id
                else "The instrumental music checkpoint supports up to 120 seconds."
            )
        notes = None
        if family == "controlfoley":
            notes = (
                "Uses the documented text-only mode; video conditioning is not exposed here. "
                "Published video-conditioned evaluations do not establish text-only quality."
            )
        elif family == "midashenglm_gen":
            notes = (
                "Plain descriptions are supported: Pandrator adds this model's task markers. "
                "Prompts with existing task markers are preserved."
            )
        elif family == "ace_step":
            notes = (
                "Describe genre, instruments and mood. For an instrumental, explicitly request "
                "no vocals; empty lyrics alone are not a guaranteed instrumental switch."
            )
        elif family == "heartmula":
            notes = (
                "Use musical styles, instruments and mood in the description or tags. "
                "Empty lyrics alone do not reliably select instrumental output."
            )
        elif family == "minimax_music3":
            notes = (
                "A description alone is insufficient: lyrics are required. "
                "For instrumental passages, use the documented [Instrumental] section marker."
            )
        elif family == "yue2":
            notes = (
                "Empty lyrics are accepted by the native engine, but do not guarantee an instrumental. "
                "The optional instrumental adapter is not included in this package. "
                "CPU and Vulkan inference have not been locally verified."
            )
        elif family == "minimax_h3":
            notes = (
                "Joint audio/video latents are still computed for audio-only requests. "
                "CPU and Vulkan inference have not been locally verified."
            )
            if package_id == "minimax_h3_q4_k_int8_dit":
                notes += " The INT8 DiT variant is experimental and CUDA-only."
            notes += (
                " The published community licence excludes the EU, UK, US and "
                "South Korea; separate permission is needed there."
            )
        spec = ModelSpec(
            id=package_id,
            label=str(row.get("label") or package_id),
            family=family,
            description=str(
                family_map.get(family, {}).get("description") or row.get("label") or package_id
            ),
            repository=repository,
            target_directory=_safe_relative_path(row.get("target_directory"), "target_directory"),
            revision=revision,
            files=files,
            kinds=_kinds_for(package_id, family),
            duration_minimum=duration_minimum,
            duration_maximum=duration_maximum,
            duration_default=duration_default,
            supports_lyrics=lyrics,
            requires_lyrics=family == "minimax_music3",
            seed_maximum=2**31 - 1 if family in {"controlfoley", "midashenglm_gen"} else 2**32 - 1,
            parameters=parameters_for_model(package_id, family),
            backends=backends,
            duration_description=duration_description,
            notes=notes,
        )
        if package_id in models:
            raise ValueError(f"Inventory contains duplicate generation package {package_id!r}.")
        models[package_id] = spec

    if DEFAULT_MODEL_ID not in models:
        raise ValueError(f"Pinned inventory is missing the default package {DEFAULT_MODEL_ID!r}.")
    missing_generation_ids = _EXPECTED_NEW_MODEL_IDS - models.keys()
    if missing_generation_ids:
        raise ValueError(
            "Pinned inventory is missing verified Yue 2 or MiniMax H3 packages: "
            + ", ".join(sorted(missing_generation_ids))
        )
    return models, sorted(unavailable, key=lambda item: item["id"])


def _load_inventory(path: Path | None = None) -> dict[str, Any]:
    inventory_path = path or Path(__file__).with_name("audio_cpp_inventory.json")
    try:
        loaded = json.loads(inventory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("Could not read the pinned audio.cpp model inventory.") from error
    if not isinstance(loaded, dict):
        raise ValueError("Pinned audio.cpp model inventory must be an object.")
    return loaded


_MODELS, _UNAVAILABLE_MODELS = build_registry(_load_inventory())


def model_ids() -> tuple[str, ...]:
    return tuple(
        sorted(
            _MODELS,
            key=lambda model_id: (
                0 if model_id == DEFAULT_MODEL_ID else 1,
                _MODEL_ORDER[_MODELS[model_id].family],
                model_id,
            ),
        )
    )


def get_model(model_id: str) -> ModelSpec:
    try:
        return _MODELS[model_id]
    except KeyError:
        raise ValueError(f"Unknown audio.cpp generation model {model_id!r}.") from None


def all_models() -> tuple[ModelSpec, ...]:
    return tuple(get_model(model_id) for model_id in model_ids())


def unavailable_models() -> list[dict[str, str | bool]]:
    return [dict(item) for item in _UNAVAILABLE_MODELS]


def seed_maximum(model_id: str) -> int:
    return get_model(model_id).seed_maximum


def default_kind(model_id: str) -> str:
    spec = get_model(model_id)
    if spec.family == "stable_audio" and "_medium_" in spec.id:
        # Preserve the established default for clients omitting kind while
        # allowing both effects and music in explicit requests.
        return "music"
    if len(spec.kinds) == 1:
        return spec.kinds[0]
    return "sound"


def model_descriptors(
    *,
    cached_by_id: dict[str, bool] | None = None,
    compiled_backends: tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    cache = cached_by_id or {}
    descriptors: list[dict[str, Any]] = []
    for spec in all_models():
        descriptor = spec.descriptor(cached=cache.get(spec.id, False))
        if compiled_backends is not None:
            available_backends = [
                backend for backend in spec.backends if backend in compiled_backends
            ]
            descriptor["backends"] = available_backends
            if not available_backends:
                descriptor["available"] = False
                descriptor["reason"] = "No model-supported backend is compiled in audio.cpp."
        descriptors.append(descriptor)
    return descriptors
