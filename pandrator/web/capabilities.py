"""Side-effect-free runtime capability probes for setup and feature gating."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths
from pandrator.logic.dubbing.crispasr import MODELS, normalize_engine, normalize_model_quantization
from pandrator.logic.dubbing.stt_backends import probe_crispasr_runtime


def _exists(paths: DataPaths, *candidates: str) -> bool:
    roots = (paths.root, paths.root / "Pandrator", paths.root.parent)
    return any((root / candidate).exists() for root in roots for candidate in candidates)


def probe_gpu() -> dict[str, Any]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return {"available": False, "devices": [], "guidance": "Use a local CPU endpoint or a cloud provider."}
    try:
        result = subprocess.run(
            [executable, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        devices = []
        for line in result.stdout.splitlines():
            name, separator, memory = line.rpartition(",")
            if separator:
                devices.append({"name": name.strip(), "vram_mb": int(memory.strip())})
        maximum = max((item["vram_mb"] for item in devices), default=0)
        if maximum >= 24_000:
            guidance = "LM Studio can host larger quantized instruction models; reserve VRAM for speech services used at the same time."
        elif maximum >= 12_000:
            guidance = "Prefer a medium quantized instruction model and leave headroom when local TTS or STT shares the GPU."
        elif maximum >= 6_000:
            guidance = "Prefer a compact quantized instruction model and use conservative context lengths."
        else:
            guidance = "Use a very compact model, CPU offload, or a remote provider."
        return {"available": bool(devices), "devices": devices, "guidance": guidance}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"available": False, "devices": [], "guidance": "GPU probing failed; choose a model based on the endpoint's own memory report."}


def crispasr_install_preferences(paths: DataPaths) -> dict[str, Any]:
    """Read the model selected by the installer without overriding user settings."""

    config: dict[str, Any] = {}
    for candidate in (
        paths.root / "config.json",
        paths.root / "Pandrator" / "config.json",
        Path(__file__).resolve().parents[2] / "config.json",
    ):
        try:
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(loaded, dict) and (
            "crispasr_engine" in loaded or "crispasr_model_quantization" in loaded
        ):
            config = loaded
            break

    raw_engine = str(config.get("crispasr_engine") or "whisper-large-v3")
    engine = normalize_engine(raw_engine)
    quantization = normalize_model_quantization(
        str(config.get("crispasr_model_quantization") or "f16"),
        engine,
    )
    return {
        "configured": bool(config),
        "engine": engine,
        "quantization": quantization,
    }


def _crispasr_model_cached(paths: DataPaths, engine: str, quantization: str) -> bool:
    filename = MODELS[engine].filename_for(quantization)
    cache_dir = Path(
        os.environ.get("CRISPASR_CACHE_DIR")
        or paths.root / "cache" / "crispasr"
    )
    if not cache_dir.is_dir():
        return False
    return any(cache_dir.rglob(filename))


def probe_capabilities(paths: DataPaths, *, local_mode: bool) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    crispasr = probe_crispasr_runtime()
    preferences = crispasr_install_preferences(paths)
    default_engine = str(preferences["engine"])
    default_quantization = str(preferences["quantization"])
    model_capabilities: dict[str, Any] = {}
    for engine, model in MODELS.items():
        preferred_quantization = default_quantization if engine == default_engine else model.default_quantization
        cached = _crispasr_model_cached(paths, engine, preferred_quantization)
        model_capabilities[engine] = {
            "available": crispasr.installed,
            "installed": cached,
            "download_on_demand": crispasr.installed and not cached,
            "default": engine == default_engine,
            "model": "large-v3" if engine == "whisper" else "tdt-0.6b-v3",
            "precision": preferred_quantization,
            "word_timing": model.word_timing,
        }
    stt = {
        "crispasr": crispasr.installed,
        "version": crispasr.version,
        "executable": crispasr.executable,
        "compute_backends": list(crispasr.compute_backends),
        "default_engine": default_engine,
        "default_model_quantization": default_quantization,
        "models": model_capabilities,
    }
    services = {
        "xtts": _exists(paths, "xtts2_api/run.bat", "xtts2_api/pixi.toml"),
        "kokoro": _exists(paths, "Kokoro-FastAPI/api/src/main.py"),
        "rvc": _exists(paths, "rvc-python/run.bat", "rvc-python/pixi.toml"),
        "chatterbox": _exists(paths, "chatterbox/pixi.toml", "chatterbox-fastapi/pixi.toml"),
    }
    return {
        "mode": "local" if local_mode else "remote",
        "ffmpeg": {"available": bool(ffmpeg), "path": ffmpeg},
        "gpu": probe_gpu(),
        "pycroppdf": {"available": _exists(paths, "PyCropPDF/run.py"), "local_only": True},
        "recording": {"browser_required": True, "secure_context_required": not local_mode, "normalization_available": bool(ffmpeg)},
        "stt": stt,
        "rvc": {"available": services["rvc"]},
        "services": services,
        "operations": {
            "reveal_folder": local_mode,
            "pycroppdf_fallback": local_mode and _exists(paths, "PyCropPDF/run.py"),
            "record_voice": bool(ffmpeg),
            "transcribe_voice": crispasr.installed,
        },
    }
