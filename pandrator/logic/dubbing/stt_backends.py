"""CrispASR model choices, languages, and runtime availability."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...constants import LANGUAGE_DISPLAY_NAMES, WHISPER_LANGUAGES
from .crispasr import (
    MODELS,
    STT_ENGINE_MOSS,
    STT_ENGINE_PARAKEET,
    STT_ENGINE_WHISPER,
    candidate_executables,
    normalize_engine,
    resolve_executable,
)
from .languages import normalize_language_code

# Compatibility names retained for one migration cycle.
STT_BACKEND_WHISPERX = STT_ENGINE_WHISPER
STT_BACKEND_PARAKEET_ONNX = STT_ENGINE_PARAKEET

STT_BACKEND_LABELS = {engine: model.label for engine, model in MODELS.items()}

PARAKEET_V3_LANGUAGE_CODES = (
    "bg", "hr", "cs", "da", "nl", "en", "et", "fi", "fr", "de", "el", "hu", "it",
    "lv", "lt", "mt", "pl", "pt", "ro", "sk", "sl", "es", "sv", "ru", "uk",
)


def normalize_stt_backend(raw_value: str | None) -> str:
    return normalize_engine(raw_value)


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


@dataclass(frozen=True)
class CrispASRRuntimeStatus:
    installed: bool
    executable: str
    version: str
    compute_backends: tuple[str, ...]
    reason: str


def probe_crispasr_runtime(
    *,
    environ: dict[str, str] | None = None,
    path_exists: Callable[[Path], bool] | None = None,
    run_func: Callable[..., Any] = subprocess.run,
) -> CrispASRRuntimeStatus:
    exists = path_exists or (lambda path: path.is_file())
    explicit = str((os.environ if environ is None else environ).get("CRISPASR_EXECUTABLE") or "").strip()
    candidates = ([Path(explicit)] if explicit else []) + list(candidate_executables(environ))
    executable = next((str(path) for path in candidates if exists(path)), "")
    if not executable:
        discovered = resolve_executable(environ=environ)
        if discovered != "crispasr":
            executable = discovered
    if not executable:
        return CrispASRRuntimeStatus(False, "", "", (), "CrispASR is not installed")
    try:
        result = run_func([executable, "--version"], check=True, capture_output=True, text=True, timeout=10)
        output = "\n".join((str(getattr(result, "stdout", "") or ""), str(getattr(result, "stderr", "") or "")))
    except (OSError, subprocess.SubprocessError) as error:
        return CrispASRRuntimeStatus(False, executable, "", (), f"CrispASR probe failed: {error}")
    version_match = re.search(r"^\s*version\s*:\s*(\S+)", output, re.MULTILINE | re.IGNORECASE)
    backend_match = re.search(r"^\s*ggml backends\s*:\s*(.+)$", output, re.MULTILINE | re.IGNORECASE)
    backends = tuple(
        item.lower() for item in re.split(r"[\s,]+", backend_match.group(1).strip()) if item
    ) if backend_match else ()
    return CrispASRRuntimeStatus(
        True,
        executable,
        version_match.group(1) if version_match else "unknown",
        backends,
        f"CrispASR {version_match.group(1) if version_match else 'runtime'} ({', '.join(backends) or 'backend unknown'})",
    )


def detect_stt_backend_statuses(**kwargs) -> dict[str, STTBackendStatus]:
    runtime = probe_crispasr_runtime(**kwargs)
    return {
        engine: STTBackendStatus(engine, STT_BACKEND_LABELS[engine], runtime.installed, runtime.reason)
        for engine in MODELS
    }


def installed_stt_backends(**kwargs) -> tuple[str, ...]:
    return tuple(key for key, status in detect_stt_backend_statuses(**kwargs).items() if status.installed)


def is_stt_backend_installed(backend: str, **kwargs) -> bool:
    return detect_stt_backend_statuses(**kwargs)[normalize_stt_backend(backend)].installed


def select_available_stt_backend(preferred_backend: str, statuses=None) -> str:
    normalized = normalize_stt_backend(preferred_backend)
    active = statuses or detect_stt_backend_statuses()
    if active.get(normalized) and active[normalized].installed:
        return normalized
    return next((engine for engine in MODELS if active.get(engine) and active[engine].installed), normalized)


def language_options_for_backend(backend: str) -> tuple[STTLanguageOption, ...]:
    normalized = normalize_stt_backend(backend)
    if normalized == STT_ENGINE_PARAKEET:
        return tuple(STTLanguageOption(LANGUAGE_DISPLAY_NAMES.get(code, code.upper()), code) for code in PARAKEET_V3_LANGUAGE_CODES)
    if normalized == STT_ENGINE_MOSS:
        # Crisp's MOSS pipeline owns automatic language selection, so there is
        # no useful forced-language choice to expose here.
        return (STTLanguageOption("Automatic", "auto"),)
    return tuple(STTLanguageOption(language, normalize_language_code(language, default="")) for language in WHISPER_LANGUAGES)


def normalize_stt_language_for_backend(backend: str, language: str) -> STTLanguageOption:
    options = language_options_for_backend(backend)
    requested_name = str(language or "").strip().lower()
    requested_code = normalize_language_code(requested_name, default="").lower()
    return next(
        (option for option in options if option.code.lower() == requested_code or option.name.lower() == requested_name),
        next((option for option in options if option.code in {"auto", "en"}), options[0]),
    )
