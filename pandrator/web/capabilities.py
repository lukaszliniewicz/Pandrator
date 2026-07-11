"""Side-effect-free runtime capability probes for setup and feature gating."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths


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


def probe_capabilities(paths: DataPaths, *, local_mode: bool) -> dict[str, Any]:
    ffmpeg = shutil.which("ffmpeg")
    stt = {
        "whisperx": bool(shutil.which("whisperx") or _exists(paths, "envs/whisperx_installer/pixi.toml", "whisperX/pixi.toml")),
        "parakeet_onnx": _exists(paths, "envs/parakeet_onnx_installer/pixi.toml", "parakeet-tdt-0.6b-v3-onnx"),
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
            "transcribe_voice": any(stt.values()),
        },
    }
