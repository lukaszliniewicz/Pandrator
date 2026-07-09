"""Shared STT backend identifiers, language support, and availability checks."""

from __future__ import annotations

import importlib.util
import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ...constants import LANGUAGE_DISPLAY_NAMES, WHISPER_LANGUAGES
from .languages import normalize_language_code

STT_BACKEND_WHISPERX = "whisperx"
STT_BACKEND_PARAKEET_ONNX = "parakeet_onnx"

STT_BACKEND_LABELS = {
    STT_BACKEND_WHISPERX: "WhisperX",
    STT_BACKEND_PARAKEET_ONNX: "ONNX Parakeet",
}

PARAKEET_ONNX_ENV_NAME = "parakeet_onnx_installer"
WHISPERX_ENV_NAME = "whisperx_installer"

PARAKEET_V3_LANGUAGE_CODES = (
    "bg",
    "hr",
    "cs",
    "da",
    "nl",
    "en",
    "et",
    "fi",
    "fr",
    "de",
    "el",
    "hu",
    "it",
    "lv",
    "lt",
    "mt",
    "pl",
    "pt",
    "ro",
    "sk",
    "sl",
    "es",
    "sv",
    "ru",
    "uk",
)

_ALIASES = {
    "": STT_BACKEND_WHISPERX,
    "whisper": STT_BACKEND_WHISPERX,
    "whisperx": STT_BACKEND_WHISPERX,
    "parakeet": STT_BACKEND_PARAKEET_ONNX,
    "onnx-parakeet": STT_BACKEND_PARAKEET_ONNX,
    "onnx_parakeet": STT_BACKEND_PARAKEET_ONNX,
    "parakeet-onnx": STT_BACKEND_PARAKEET_ONNX,
    "parakeet_onnx": STT_BACKEND_PARAKEET_ONNX,
}


def normalize_stt_backend(raw_value: str | None) -> str:
    normalized = str(raw_value or "").strip().lower().replace(" ", "_")
    normalized = normalized.replace("-", "_")
    return _ALIASES.get(normalized, STT_BACKEND_WHISPERX)


@dataclass(frozen=True)
class STTLanguageOption:
    name: str
    code: str


@dataclass(frozen=True)
class STTBackendStatus:
    backend: str
    label: str
    installed: bool
    reason: str


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _install_root() -> Path:
    return _repo_root().parent


def _candidate_manifest_paths(env_var: str, env_name: str, environ: dict[str, str]) -> tuple[Path, ...]:
    candidates: list[Path] = []
    explicit = str(environ.get(env_var) or "").strip()
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(
        [
            _repo_root() / "envs" / env_name / "pixi.toml",
            _install_root() / "envs" / env_name / "pixi.toml",
        ]
    )
    return tuple(candidates)


def _safe_find_spec(module_name: str) -> bool:
    try:
        return importlib.util.find_spec(module_name) is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False


def _status_from_checks(
    backend: str,
    *,
    env_var: str,
    env_name: str,
    module_name: str,
    executable_name: str = "",
    environ: dict[str, str],
    path_exists: Callable[[Path], bool],
    find_module: Callable[[str], bool],
    find_executable: Callable[[str], str | None],
) -> STTBackendStatus:
    for manifest_path in _candidate_manifest_paths(env_var, env_name, environ):
        if path_exists(manifest_path):
            return STTBackendStatus(
                backend=backend,
                label=STT_BACKEND_LABELS[backend],
                installed=True,
                reason=f"Pixi manifest found at {manifest_path}",
            )

    if find_module(module_name):
        return STTBackendStatus(
            backend=backend,
            label=STT_BACKEND_LABELS[backend],
            installed=True,
            reason=f"Python module '{module_name}' is importable",
        )

    if executable_name:
        executable_path = find_executable(executable_name)
        if executable_path:
            return STTBackendStatus(
                backend=backend,
                label=STT_BACKEND_LABELS[backend],
                installed=True,
                reason=f"Executable '{executable_name}' found at {executable_path}",
            )

    return STTBackendStatus(
        backend=backend,
        label=STT_BACKEND_LABELS[backend],
        installed=False,
        reason="Optional STT backend is not installed",
    )


def detect_stt_backend_statuses(
    *,
    environ: dict[str, str] | None = None,
    path_exists: Callable[[Path], bool] | None = None,
    find_module: Callable[[str], bool] | None = None,
    find_executable: Callable[[str], str | None] | None = None,
) -> dict[str, STTBackendStatus]:
    active_environ = dict(os.environ if environ is None else environ)
    exists = path_exists or (lambda path: path.is_file())
    module_finder = find_module or _safe_find_spec
    executable_finder = find_executable or shutil.which

    return {
        STT_BACKEND_WHISPERX: _status_from_checks(
            STT_BACKEND_WHISPERX,
            env_var="WHISPERX_PIXI_MANIFEST",
            env_name=WHISPERX_ENV_NAME,
            module_name="whisperx",
            executable_name="whisperx",
            environ=active_environ,
            path_exists=exists,
            find_module=module_finder,
            find_executable=executable_finder,
        ),
        STT_BACKEND_PARAKEET_ONNX: _status_from_checks(
            STT_BACKEND_PARAKEET_ONNX,
            env_var="PARAKEET_PIXI_MANIFEST",
            env_name=PARAKEET_ONNX_ENV_NAME,
            module_name="onnx_asr",
            environ=active_environ,
            path_exists=exists,
            find_module=module_finder,
            find_executable=executable_finder,
        ),
    }


def installed_stt_backends(**kwargs) -> tuple[str, ...]:
    statuses = detect_stt_backend_statuses(**kwargs)
    return tuple(backend for backend, status in statuses.items() if status.installed)


def is_stt_backend_installed(backend: str, **kwargs) -> bool:
    normalized = normalize_stt_backend(backend)
    return detect_stt_backend_statuses(**kwargs).get(normalized, STTBackendStatus(normalized, normalized, False, "")).installed


def select_available_stt_backend(preferred_backend: str, statuses: dict[str, STTBackendStatus] | None = None) -> str:
    normalized = normalize_stt_backend(preferred_backend)
    active_statuses = statuses or detect_stt_backend_statuses()
    if active_statuses.get(normalized) and active_statuses[normalized].installed:
        return normalized
    for backend in (STT_BACKEND_WHISPERX, STT_BACKEND_PARAKEET_ONNX):
        if active_statuses.get(backend) and active_statuses[backend].installed:
            return backend
    return normalized


def language_options_for_backend(backend: str) -> tuple[STTLanguageOption, ...]:
    normalized = normalize_stt_backend(backend)
    if normalized == STT_BACKEND_PARAKEET_ONNX:
        return tuple(
            STTLanguageOption(name=LANGUAGE_DISPLAY_NAMES.get(code, code.upper()), code=code)
            for code in PARAKEET_V3_LANGUAGE_CODES
        )

    return tuple(
        STTLanguageOption(name=language, code=normalize_language_code(language, default=""))
        for language in WHISPER_LANGUAGES
    )


def normalize_stt_language_for_backend(backend: str, language: str) -> STTLanguageOption:
    options = language_options_for_backend(backend)
    if not options:
        return STTLanguageOption(name=str(language or "").strip(), code="")

    requested_name = str(language or "").strip()
    requested_code = normalize_language_code(requested_name, default="").lower()
    requested_name_normalized = requested_name.lower()
    for option in options:
        if option.code.lower() == requested_code or option.name.lower() == requested_name_normalized:
            return option

    for option in options:
        if option.code.lower() == "en":
            return option

    return options[0]
