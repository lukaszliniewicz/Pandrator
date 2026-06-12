"""Typed input models shared by GUI and headless installer flows."""

from __future__ import annotations

from dataclasses import dataclass, fields
from pathlib import Path
from typing import Iterable

from .catalog import INSTALL_COMPONENT_KEYS, resolve_dependencies


@dataclass(frozen=True)
class WorkspacePaths:
    workspace: Path

    @classmethod
    def from_value(cls, value: str | Path) -> "WorkspacePaths":
        return cls(Path(value).expanduser().resolve())

    @property
    def install_root(self) -> Path:
        return self.workspace / "Pandrator"

    @property
    def pandrator_repo(self) -> Path:
        return self.install_root / "Pandrator"

    @property
    def subdub_repo(self) -> Path:
        return self.install_root / "Subdub"

    def repository(self, dirname: str) -> Path:
        return self.install_root / dirname

    def environment(self, name: str) -> Path:
        return self.install_root / "envs" / name


@dataclass(frozen=True)
class InstallSelection:
    pandrator: bool = True
    xtts: bool = False
    xtts_cpu: bool = False
    voxcpm: bool = False
    fishs2: bool = False
    silero: bool = False
    voxtral: bool = False
    kokoro: bool = False
    kokoro_cpu: bool = False
    rvc: bool = False
    whisperx: bool = False
    xtts_finetuning: bool = False
    chatterbox: bool = False
    chatterbox_cpu: bool = False
    magpie: bool = False
    magpie_cpu: bool = False

    @classmethod
    def from_components(
        cls,
        components: Iterable[str],
        *,
        install_pandrator: bool = True,
        include_dependencies: bool = True,
    ) -> "InstallSelection":
        selected = {
            str(component).strip().lower().replace("-", "_")
            for component in components
            if str(component).strip()
        }
        unknown = sorted(selected.difference(INSTALL_COMPONENT_KEYS))
        if unknown:
            raise ValueError(
                "Unsupported component(s): "
                + ", ".join(unknown)
                + ". Supported values: "
                + ", ".join(sorted(INSTALL_COMPONENT_KEYS))
            )

        if include_dependencies:
            selected = set(resolve_dependencies(selected))

        selection = cls(
            pandrator=bool(install_pandrator),
            **{field.name: field.name in selected for field in fields(cls) if field.name != "pandrator"},
        )
        selection.validate()
        return selection

    def validate(self) -> None:
        mutually_exclusive = (
            ("xtts", "xtts_cpu"),
            ("kokoro", "kokoro_cpu"),
            ("chatterbox", "chatterbox_cpu"),
            ("magpie", "magpie_cpu"),
        )
        for primary, secondary in mutually_exclusive:
            if getattr(self, primary) and getattr(self, secondary):
                raise ValueError(f"Select either '{primary}' or '{secondary}', not both.")

    def selected_components(self) -> tuple[str, ...]:
        return tuple(
            field.name
            for field in fields(self)
            if field.name != "pandrator" and getattr(self, field.name)
        )

    def any_component_selected(self) -> bool:
        return self.pandrator or bool(self.selected_components())


@dataclass(frozen=True)
class LaunchSelection:
    pandrator: bool = True
    rvc: bool = False
    xtts: bool = False
    disable_deepspeed: bool = False
    xtts_cpu: bool = False
    voxcpm: bool = False
    fishs2: bool = False
    voxtral: bool = False
    kokoro: bool = False
    kokoro_cpu: bool = False
    silero: bool = False
    chatterbox: bool = False
    chatterbox_cpu: bool = False
    magpie: bool = False
    magpie_cpu: bool = False

    def selected_backend_keys(self) -> tuple[str, ...]:
        ordered_flags = (
            ("xtts", self.xtts),
            ("voxcpm", self.voxcpm),
            ("fishs2", self.fishs2),
            ("voxtral", self.voxtral),
            ("silero", self.silero),
            ("kokoro", self.kokoro),
            ("chatterbox", self.chatterbox),
            ("magpie", self.magpie),
        )
        return tuple(key for key, selected in ordered_flags if selected)
