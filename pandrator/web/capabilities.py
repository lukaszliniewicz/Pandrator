"""Side-effect-free runtime capability probes for setup and feature gating."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from pandrator.runtime import DataPaths
from pandrator.logic.dubbing.crispasr import MODELS, normalize_engine, normalize_model_quantization
from pandrator.logic.dubbing.stt_backends import probe_crispasr_runtime


def _exists(paths: DataPaths, *candidates: str) -> bool:
    roots = (paths.root, paths.root / "Pandrator", paths.root.parent)
    return any((root / candidate).exists() for root in roots for candidate in candidates)


GPU_VENDORS = {
    "0x1002": "AMD",
    "0x10de": "NVIDIA",
    "0x8086": "Intel",
    "0x106b": "Apple",
}

BURN_VIDEO_ENCODER_PROFILES = (
    {"id": "libx264", "label": "H.264 software (most compatible)", "hardware": False, "codec": "h264"},
    {"id": "libx265", "label": "H.265 software (smaller, limited browser support)", "hardware": False, "codec": "hevc"},
    {"id": "h264_vaapi", "label": "H.264 VA-API (AMD / Intel GPU)", "hardware": True, "codec": "h264", "vendors": {"AMD", "Intel"}, "platform": "linux"},
    {"id": "hevc_vaapi", "label": "H.265 VA-API (AMD / Intel GPU)", "hardware": True, "codec": "hevc", "vendors": {"AMD", "Intel"}, "platform": "linux"},
    {"id": "h264_amf", "label": "H.264 AMD AMF", "hardware": True, "codec": "h264", "vendors": {"AMD"}, "platform": "windows"},
    {"id": "hevc_amf", "label": "H.265 AMD AMF", "hardware": True, "codec": "hevc", "vendors": {"AMD"}, "platform": "windows"},
    {"id": "h264_nvenc", "label": "H.264 NVIDIA NVENC", "hardware": True, "codec": "h264", "vendors": {"NVIDIA"}},
    {"id": "hevc_nvenc", "label": "H.265 NVIDIA NVENC", "hardware": True, "codec": "hevc", "vendors": {"NVIDIA"}},
    {"id": "h264_qsv", "label": "H.264 Intel Quick Sync", "hardware": True, "codec": "h264", "vendors": {"Intel"}},
    {"id": "hevc_qsv", "label": "H.265 Intel Quick Sync", "hardware": True, "codec": "hevc", "vendors": {"Intel"}},
)


def _normalized_hex(value: object) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    try:
        return f"0x{int(raw, 0):04x}"
    except ValueError:
        return raw


def _vendor_name(vendor_id: object = "", *labels: object) -> str:
    normalized_id = _normalized_hex(vendor_id)
    if normalized_id in GPU_VENDORS:
        return GPU_VENDORS[normalized_id]
    joined = " ".join(str(label or "") for label in labels).lower()
    if "nvidia" in joined:
        return "NVIDIA"
    if "amd" in joined or "advanced micro devices" in joined or "ati" in joined or "radeon" in joined:
        return "AMD"
    if "intel" in joined:
        return "Intel"
    if "apple" in joined:
        return "Apple"
    return "Unknown"


def _device(
    name: object,
    *,
    vendor_id: object = "",
    device_id: object = "",
    vram_mb: object = 0,
    source: str,
    apis: list[str] | None = None,
) -> dict[str, Any]:
    normalized_vendor_id = _normalized_hex(vendor_id)
    normalized_device_id = _normalized_hex(device_id)
    try:
        normalized_vram = max(0, int(vram_mb or 0))
    except (TypeError, ValueError):
        normalized_vram = 0
    normalized_name = str(name or "GPU").strip() or "GPU"
    return {
        "name": normalized_name,
        "vendor": _vendor_name(normalized_vendor_id, normalized_name),
        "vendor_id": normalized_vendor_id,
        "device_id": normalized_device_id,
        "vram_mb": normalized_vram,
        "sources": [source],
        "apis": sorted(set(apis or [])),
    }


def _merge_gpu_devices(devices: list[dict[str, Any]], candidates: list[dict[str, Any]]) -> None:
    for candidate in candidates:
        matching = next(
            (
                item
                for item in devices
                if (
                    candidate.get("vendor_id")
                    and candidate.get("device_id")
                    and item.get("vendor_id") == candidate.get("vendor_id")
                    and item.get("device_id") == candidate.get("device_id")
                )
                or str(item.get("name") or "").casefold() == str(candidate.get("name") or "").casefold()
            ),
            None,
        )
        if matching is None:
            devices.append(candidate)
            continue
        if "vulkan" in candidate.get("sources", []):
            matching["name"] = candidate["name"]
        matching["vendor"] = matching.get("vendor") if matching.get("vendor") != "Unknown" else candidate.get("vendor")
        matching["vendor_id"] = matching.get("vendor_id") or candidate.get("vendor_id")
        matching["device_id"] = matching.get("device_id") or candidate.get("device_id")
        matching["vram_mb"] = max(int(matching.get("vram_mb") or 0), int(candidate.get("vram_mb") or 0))
        matching["sources"] = sorted(set(matching.get("sources", [])) | set(candidate.get("sources", [])))
        matching["apis"] = sorted(set(matching.get("apis", [])) | set(candidate.get("apis", [])))


def _probe_nvidia_smi() -> list[dict[str, Any]]:
    executable = shutil.which("nvidia-smi")
    if not executable:
        return []
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
                devices.append(_device(name, vendor_id="0x10de", vram_mb=memory.strip(), source="nvidia-smi", apis=["cuda"]))
        return devices
    except (OSError, ValueError, subprocess.SubprocessError):
        return []


def _linux_pci_name(card: Path) -> str:
    executable = shutil.which("lspci")
    if not executable:
        return ""
    try:
        slot = (card / "device").resolve().name
        result = subprocess.run([executable, "-s", slot], capture_output=True, text=True, timeout=3, check=True)
        match = re.search(r"(?:VGA compatible controller|3D controller|Display controller):\s*(.+?)(?:\s+\(rev [^)]+\))?$", result.stdout.strip())
        return match.group(1).strip() if match else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _probe_linux_drm(root: Path | None = None) -> list[dict[str, Any]]:
    if not sys.platform.startswith("linux"):
        return []
    root = root or Path("/sys/class/drm")
    if not root.is_dir():
        return []
    devices: list[dict[str, Any]] = []
    for card in sorted(root.glob("card[0-9]*")):
        if not card.name.removeprefix("card").isdigit():
            continue
        try:
            vendor_id = (card / "device" / "vendor").read_text(encoding="ascii").strip()
            device_id = (card / "device" / "device").read_text(encoding="ascii").strip()
        except OSError:
            continue
        try:
            vram_bytes = int((card / "device" / "mem_info_vram_total").read_text(encoding="ascii").strip())
        except (OSError, ValueError):
            vram_bytes = 0
        vendor = _vendor_name(vendor_id)
        name = _linux_pci_name(card) or f"{vendor} GPU ({_normalized_hex(device_id)})"
        devices.append(
            _device(
                name,
                vendor_id=vendor_id,
                device_id=device_id,
                vram_mb=vram_bytes // (1024 * 1024),
                source="linux-drm",
            )
        )
    return devices


def _probe_windows_display_devices() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class DisplayDevice(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128),
                ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128),
                ("DeviceKey", wintypes.WCHAR * 128),
            ]

        enum_display_devices = ctypes.windll.user32.EnumDisplayDevicesW
    except (AttributeError, ImportError, OSError):
        return []
    devices: list[dict[str, Any]] = []
    index = 0
    while True:
        display = DisplayDevice()
        display.cb = ctypes.sizeof(DisplayDevice)
        if not enum_display_devices(None, index, ctypes.byref(display), 0):
            break
        index += 1
        name = str(display.DeviceString or "").strip()
        if not name or display.StateFlags & 0x00000008 or any(
            marker in name.casefold() for marker in ("virtual", "remote display", "basic display", "indirect display")
        ):
            continue
        pnp_id = str(display.DeviceID or "")
        vendor_match = re.search(r"VEN_([0-9A-F]{4})", pnp_id, re.IGNORECASE)
        device_match = re.search(r"DEV_([0-9A-F]{4})", pnp_id, re.IGNORECASE)
        devices.append(
            _device(
                name,
                vendor_id=f"0x{vendor_match.group(1)}" if vendor_match else "",
                device_id=f"0x{device_match.group(1)}" if device_match else "",
                source="windows-display-api",
            )
        )
    return devices


def _probe_windows_video_controllers() -> list[dict[str, Any]]:
    if os.name != "nt":
        return []
    native_devices = _probe_windows_display_devices()
    if native_devices:
        return native_devices
    executable = shutil.which("powershell") or shutil.which("pwsh")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [
                executable,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "Get-CimInstance Win32_VideoController | Select-Object Name,AdapterCompatibility,PNPDeviceID,AdapterRAM,Status | ConvertTo-Json -Compress",
            ],
            capture_output=True,
            text=True,
            timeout=8,
            check=True,
        )
        payload = json.loads(result.stdout or "[]")
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return []
    rows = payload if isinstance(payload, list) else [payload]
    devices: list[dict[str, Any]] = []
    for row in rows:
        name = str(row.get("Name") or "").strip() if isinstance(row, dict) else ""
        if not name or any(marker in name.casefold() for marker in ("virtual", "remote display", "basic display", "indirect display")):
            continue
        pnp_id = str(row.get("PNPDeviceID") or "")
        vendor_match = re.search(r"VEN_([0-9A-F]{4})", pnp_id, re.IGNORECASE)
        device_match = re.search(r"DEV_([0-9A-F]{4})", pnp_id, re.IGNORECASE)
        try:
            vram_bytes = int(row.get("AdapterRAM") or 0)
        except (TypeError, ValueError):
            vram_bytes = 0
        devices.append(
            _device(
                name,
                vendor_id=f"0x{vendor_match.group(1)}" if vendor_match else "",
                device_id=f"0x{device_match.group(1)}" if device_match else "",
                vram_mb=max(0, vram_bytes) // (1024 * 1024),
                source="windows-cim",
            )
        )
    return devices


def _memory_label_mb(value: object) -> int:
    match = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*(GB|MB)", str(value or ""), re.IGNORECASE)
    if not match:
        return 0
    amount = float(match.group(1))
    return int(amount * 1024) if match.group(2).upper() == "GB" else int(amount)


def _probe_macos_displays() -> list[dict[str, Any]]:
    if sys.platform != "darwin":
        return []
    executable = shutil.which("system_profiler")
    if not executable:
        return []
    try:
        result = subprocess.run(
            [executable, "SPDisplaysDataType", "-json"],
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        payload = json.loads(result.stdout or "{}")
    except (OSError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        return []
    devices: list[dict[str, Any]] = []
    for row in payload.get("SPDisplaysDataType", []):
        if not isinstance(row, dict):
            continue
        name = str(row.get("sppci_model") or row.get("_name") or "").strip()
        if not name:
            continue
        devices.append(
            _device(
                name,
                vendor_id="0x106b" if "apple" in f"{name} {row.get('spdisplays_vendor', '')}".casefold() else "",
                device_id=row.get("spdisplays_device-id"),
                vram_mb=_memory_label_mb(row.get("spdisplays_vram") or row.get("spdisplays_vram_shared")),
                source="macos-system-profiler",
                apis=["metal"],
            )
        )
    return devices


def _probe_vulkan() -> list[dict[str, Any]]:
    executable = shutil.which("vulkaninfo")
    if not executable:
        return []
    try:
        result = subprocess.run([executable, "--summary"], capture_output=True, text=True, timeout=8, check=True)
    except (OSError, subprocess.SubprocessError):
        return []
    devices: list[dict[str, Any]] = []
    for block in re.split(r"(?m)^GPU\d+:\s*$", result.stdout)[1:]:
        values = dict(re.findall(r"(?m)^\s*(deviceName|deviceType|vendorID|deviceID)\s*=\s*(.+?)\s*$", block))
        name = str(values.get("deviceName") or "").strip()
        device_type = str(values.get("deviceType") or "").strip()
        if not name or "CPU" in device_type or "llvmpipe" in name.casefold():
            continue
        devices.append(
            _device(
                name,
                vendor_id=values.get("vendorID"),
                device_id=values.get("deviceID"),
                source="vulkan",
                apis=["vulkan"],
            )
        )
    return devices


def probe_gpu() -> dict[str, Any]:
    devices: list[dict[str, Any]] = []
    for candidates in (_probe_nvidia_smi(), _probe_linux_drm(), _probe_windows_video_controllers(), _probe_macos_displays()):
        _merge_gpu_devices(devices, candidates)
    # Linux DRM has reliable memory data while Vulkan usually provides the
    # friendlier marketing name. On Windows/macOS the native display APIs are
    # sufficient and avoid making every capability refresh wait on vulkaninfo.
    if sys.platform.startswith("linux") or not devices:
        _merge_gpu_devices(devices, _probe_vulkan())

    maximum = max((int(item.get("vram_mb") or 0) for item in devices), default=0)
    if not devices:
        guidance = "No hardware GPU was found through NVIDIA, operating-system display APIs, Linux DRM, or Vulkan."
    elif maximum >= 24_000:
        guidance = "Larger local models are practical, but reserve VRAM for speech services used at the same time."
    elif maximum >= 12_000:
        guidance = "Prefer a medium quantized local model and leave headroom for concurrent speech services."
    elif maximum >= 6_000:
        guidance = "Prefer compact quantized local models and conservative context lengths."
    elif maximum:
        guidance = "Use very compact local models, CPU offload, or a remote provider."
    else:
        guidance = "GPU memory could not be read; choose models using the serving endpoint's own memory report."
    if devices and any(item.get("vendor") != "NVIDIA" for item in devices):
        guidance += " Detection confirms hardware presence; each service still needs a compatible Vulkan, ROCm, DirectML, VA-API, or vendor backend."
    return {"available": bool(devices), "devices": devices, "guidance": guidance}


def ffmpeg_video_encoder_ids(executable: str | None) -> set[str]:
    if not executable:
        return set()
    try:
        result = subprocess.run([executable, "-hide_banner", "-encoders"], capture_output=True, text=True, timeout=8, check=True)
    except (OSError, subprocess.SubprocessError):
        return set()
    return {
        match.group(1)
        for line in result.stdout.splitlines()
        if (match := re.match(r"^\s*V\S*\s+(\S+)", line))
    }


def probe_burn_video_encoders(executable: str | None, gpu: dict[str, Any]) -> list[dict[str, Any]]:
    supported = ffmpeg_video_encoder_ids(executable)
    vendors = {str(item.get("vendor") or "") for item in gpu.get("devices", [])}
    vaapi_ready = sys.platform.startswith("linux") and any(Path("/dev/dri").glob("renderD*"))
    profiles: list[dict[str, Any]] = []
    for source in BURN_VIDEO_ENCODER_PROFILES:
        if source["id"] not in supported:
            continue
        required_vendors = set(source.get("vendors") or set())
        if required_vendors and not (required_vendors & vendors):
            continue
        if source.get("platform") == "linux" and not vaapi_ready:
            continue
        if source.get("platform") == "windows" and os.name != "nt":
            continue
        profiles.append({key: value for key, value in source.items() if key not in {"vendors", "platform"}})
    return profiles


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
    gpu = probe_gpu()
    try:
        from pandrator.logic.dubbing_handler import resolve_ffmpeg_for_burned_subtitles

        burn_ffmpeg = resolve_ffmpeg_for_burned_subtitles() or ffmpeg
    except (ImportError, OSError):
        burn_ffmpeg = ffmpeg
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
        "ffmpeg": {
            "available": bool(ffmpeg),
            "path": ffmpeg,
            "burn_path": burn_ffmpeg,
            "burn_video_encoders": probe_burn_video_encoders(burn_ffmpeg, gpu),
        },
        "gpu": gpu,
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
