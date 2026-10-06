"""audio.cpp package records shared by inventory and component catalogues."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

AUDIO_CPP_MODEL_REPOSITORY = "audio-cpp/audio.cpp-gguf"
AUDIO_CPP_MODEL_REVISION = "dc6fecccc2b0c6bdda0a8b2f38fa61394fee0b9c"


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
    # Inventory-backed packages carry their own immutable source pin and
    # exact-file staging contract. Manual packages retain the historical
    # audio.cpp repository/revision defaults.
    label: str = ""
    precision: str | None = None
    download_kind: str = "huggingface_snapshot"
    repository: str = AUDIO_CPP_MODEL_REPOSITORY
    revision: str = AUDIO_CPP_MODEL_REVISION
    download_files: tuple[str, ...] = ()
    strip_prefix: str = ""

    @property
    def config_path(self) -> str:
        if self.family == "pocket_tts":
            return f"models/{self.target_directory}"
        gguf = next(path for path in self.files if path.casefold().endswith(".gguf"))
        return f"models/{self.target_directory}/{gguf}"

    def marker_path(self, models_root: Path) -> Path:
        return models_root / self.target_directory / f".audiocpp-package-{self.id}.json"

    def required_paths(self, models_root: Path) -> tuple[Path, ...]:
        root = models_root / self.target_directory
        return tuple(root / relative for relative in self.files)


# Preserve the historical class path for serialized package records.
AudioCppModelPackage.__module__ = "pandrator_manager.components.audiocpp"
