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
    fishs2_cpu: bool = False
    fishs2_backend: str = "auto"
    fishs2_model_quant: str = "q6_k"
    silero: bool = False
    voxtral: bool = False
    kokoro: bool = False
    kokoro_cpu: bool = False
    rvc: bool = False
    rvc_cpu: bool = False
    crispasr: bool = False
    crispasr_backend: str = "auto"
    crispasr_engine: str = "whisper-large-v3"
    crispasr_model_quantization: str = "f16"
    xtts_finetuning: bool = False
    chatterbox: bool = False
    chatterbox_cpu: bool = False
    kobold_qwen: bool = False
    kobold_qwen_cpu: bool = False
    kobold_qwen_backend: str = "auto"
    kobold_qwen_model_size: str = "0.6b"
    kobold_qwen_quantization: str = "f16"
    kobold_qwen_initial_model: str = "base"
    magpie: bool = False
    magpie_cpu: bool = False

    @classmethod
    def from_components(
        cls,
        components: Iterable[str],
        *,
        install_pandrator: bool = True,
        include_dependencies: bool = True,
        fishs2_backend: str = "auto",
        fishs2_model_quant: str = "q6_k",
        crispasr_backend: str = "auto",
        crispasr_engine: str = "whisper-large-v3",
        crispasr_model_quantization: str = "f16",
        kobold_qwen_backend: str = "auto",
        kobold_qwen_model_size: str = "0.6b",
        kobold_qwen_quantization: str = "f16",
        kobold_qwen_initial_model: str = "base",
    ) -> "InstallSelection":
        selected = {
            str(component).strip().lower().replace("-", "_")
            for component in components
            if str(component).strip()
        }
        if selected.intersection({"whisperx", "parakeet_onnx"}):
            selected.difference_update({"whisperx", "parakeet_onnx"})
            selected.add("crispasr")
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
            fishs2_backend=fishs2_backend,
            fishs2_model_quant=fishs2_model_quant,
            crispasr_backend=str(crispasr_backend or "auto").lower(),
            crispasr_engine=str(crispasr_engine or "whisper-large-v3").lower(),
            crispasr_model_quantization=str(crispasr_model_quantization or "f16").lower(),
            kobold_qwen_backend=str(kobold_qwen_backend or "auto").lower(),
            kobold_qwen_model_size=str(kobold_qwen_model_size or "0.6b").lower(),
            kobold_qwen_quantization=str(kobold_qwen_quantization or "f16").lower(),
            kobold_qwen_initial_model=str(kobold_qwen_initial_model or "base").lower(),
            **{field.name: field.name in selected for field in fields(cls) if field.name not in (
                "pandrator", "fishs2_backend", "fishs2_model_quant", "crispasr_backend",
                "crispasr_engine", "crispasr_model_quantization", "kobold_qwen_backend",
                "kobold_qwen_model_size", "kobold_qwen_quantization", "kobold_qwen_initial_model",
            )},
        )
        selection.validate()
        return selection

    def validate(self) -> None:
        if self.crispasr_backend not in {"auto", "cpu", "cuda", "vulkan", "metal"}:
            raise ValueError("CrispASR backend must be auto, cpu, cuda, vulkan, or metal.")
        if self.crispasr_engine not in {"whisper-large-v3", "parakeet-tdt-0.6b-v3"}:
            raise ValueError("CrispASR engine must be whisper-large-v3 or parakeet-tdt-0.6b-v3.")
        crisp_quantizations = {
            "whisper-large-v3": {"f16", "q5_0"},
            "parakeet-tdt-0.6b-v3": {"f16", "q8_0", "q5_0", "q4_k"},
        }
        if self.crispasr_model_quantization not in crisp_quantizations[self.crispasr_engine]:
            raise ValueError("Unsupported CrispASR model quantization for the selected engine.")
        if self.kobold_qwen_backend not in {"auto", "cpu", "cuda", "vulkan", "metal"}:
            raise ValueError("Qwen3 TTS backend must be auto, cpu, cuda, vulkan, or metal.")
        if self.kobold_qwen_model_size not in {"0.6b", "1.7b"}:
            raise ValueError("Qwen3 TTS model size must be 0.6b or 1.7b.")
        if self.kobold_qwen_quantization not in {"q8_0", "f16"}:
            raise ValueError("Qwen3 TTS quantization must be q8_0 or f16.")
        if self.kobold_qwen_initial_model not in {"base", "customvoice", "both"}:
            raise ValueError("Qwen3 TTS model selection must be base, customvoice, or both.")
        if self.kobold_qwen_initial_model in {"customvoice", "both"} and self.kobold_qwen_model_size != "1.7b":
            raise ValueError("Qwen3 TTS CustomVoice is available only for the 1.7B model family.")
        mutually_exclusive = (
            ("xtts", "xtts_cpu"),
            ("kokoro", "kokoro_cpu"),
            ("chatterbox", "chatterbox_cpu"),
            ("kobold_qwen", "kobold_qwen_cpu"),
            ("magpie", "magpie_cpu"),
            ("rvc", "rvc_cpu"),
            ("fishs2", "fishs2_cpu"),
        )
        for primary, secondary in mutually_exclusive:
            if getattr(self, primary) and getattr(self, secondary):
                raise ValueError(f"Select either '{primary}' or '{secondary}', not both.")

    def selected_components(self) -> tuple[str, ...]:
        return tuple(
            field.name
            for field in fields(self)
            if field.name not in (
                "pandrator", "fishs2_backend", "fishs2_model_quant", "crispasr_backend",
                "crispasr_engine", "crispasr_model_quantization", "kobold_qwen_backend",
                "kobold_qwen_model_size", "kobold_qwen_quantization", "kobold_qwen_initial_model",
            ) and getattr(self, field.name)
        )

    def any_component_selected(self) -> bool:
        return self.pandrator or bool(self.selected_components())


def qwen_model_variants(selection: str) -> tuple[str, ...]:
    """Expand the installer-only 'both' choice into service model variants."""
    normalized = str(selection or "base").strip().lower()
    if normalized == "both":
        return ("base", "customvoice")
    if normalized in {"base", "customvoice"}:
        return (normalized,)
    raise ValueError("Qwen3 TTS model selection must be base, customvoice, or both.")


def qwen_effective_model_size(selection: str, model_size: str) -> str:
    """Return the only valid size for the selected Qwen capability set."""
    variants = qwen_model_variants(selection)
    if "customvoice" in variants:
        return "1.7b"
    normalized = str(model_size or "0.6b").strip().lower()
    return normalized if normalized in {"0.6b", "1.7b"} else "0.6b"


@dataclass(frozen=True)
class LaunchSelection:
    pandrator: bool = True
    pandrator_network_access: bool = False
    pandrator_port: int = 8097
    rvc: bool = False
    rvc_cpu: bool = False
    xtts: bool = False
    disable_deepspeed: bool = False
    xtts_cpu: bool = False
    voxcpm: bool = False
    fishs2: bool = False
    fishs2_cpu: bool = False
    voxtral: bool = False
    kokoro: bool = False
    kokoro_cpu: bool = False
    silero: bool = False
    chatterbox: bool = False
    chatterbox_cpu: bool = False
    kobold_qwen: bool = False
    kobold_qwen_cpu: bool = False
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
            ("kobold_qwen", self.kobold_qwen),
            ("magpie", self.magpie),
        )
        return tuple(key for key, selected in ordered_flags if selected)
