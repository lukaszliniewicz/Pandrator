"""Compatible SFX facade for independent audio.cpp sound and music generation."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import wave
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from . import audio_cpp_assets as assets
from . import audio_cpp_generation_assets as generation_assets
from . import audio_cpp_generation_models as generation_models
from . import audio_cpp_generation_payload as generation_payload
from . import audio_cpp_processing as processing
from . import audio_cpp_yue2_adapter as yue2_adapter

PROVIDER = "audio_cpp"
OPERATION = "gen"
MODEL_ID = assets.STABLE_AUDIO_SFX_MODEL_ID
MODEL_REVISION = assets.STABLE_AUDIO_SFX_REVISION
MODEL_SHA256 = assets.STABLE_AUDIO_SFX_SHA256
MINIMUM_RUNTIME_VERSION = assets.STABLE_AUDIO_SFX_MINIMUM_RUNTIME_VERSION
SAMPLE_RATE = 44_100
CHANNELS = 2
SAMPLE_WIDTH = 2
DEFAULT_DURATION_SECONDS = 4.0
DEFAULT_INFERENCE_STEPS = 8
DEFAULT_GUIDANCE_SCALE = 1.0
_VERSION_PROBE_TIMEOUT_SECONDS = 2.0
_VERSION_PATTERN = re.compile(r"(?m)^\s*audio\.cpp\s*(\d+\.\d+\.\d+)(?:\s|$)")
_BACKENDS_PATTERN = re.compile(r"(?im)^\s*backends\s*:\s*([^\r\n]*)$")
_KNOWN_BACKENDS = ("cpu", "vulkan", "cuda")

ProgressCallback = Callable[[str, int, int], None]
RunFunction = Callable[..., Any]
_RuntimeStatKey = tuple[str, int, int]


class AudioCppSfxError(RuntimeError):
    """Audio generation cannot safely use the configured local runtime."""


_runtime_lock = threading.Lock()
_runtime_versions: dict[_RuntimeStatKey, tuple[str | None, str | None]] = {}
_runtime_backends: dict[_RuntimeStatKey, tuple[str, ...]] = {}
_runtime_hashes: dict[_RuntimeStatKey, str] = {}


def _logical_cpu_count() -> int:
    count = os.cpu_count()
    return count if isinstance(count, int) and count > 0 else 1


def _version_tuple(version: str) -> tuple[int, int, int]:
    return tuple(int(part) for part in version.split("."))  # type: ignore[return-value]


def _runtime_stat_key(path: Path) -> _RuntimeStatKey:
    resolved = path.resolve()
    stat = resolved.stat()
    return (str(resolved), stat.st_mtime_ns, stat.st_size)


def _runtime_status(
    settings: dict,
) -> tuple[Path | None, _RuntimeStatKey | None, str | None, str | None]:
    try:
        executable = assets.resolve_executable(settings)
        key = _runtime_stat_key(executable)
    except (assets.AudioAssetsError, OSError, RuntimeError):
        return None, None, None, "audio.cpp executable is unavailable."

    with _runtime_lock:
        # A replacement at the same path must not retain metadata or hashes
        # belonging to the previous stat identity.
        for previous_key in tuple(_runtime_versions):
            if previous_key[0] == key[0] and previous_key != key:
                _runtime_versions.pop(previous_key, None)
                _runtime_backends.pop(previous_key, None)
                _runtime_hashes.pop(previous_key, None)
        cached = _runtime_versions.get(key)
    if cached is not None:
        version, reason = cached
        return executable, key, version, reason

    compiled_backends: tuple[str, ...] = ()
    try:
        result = subprocess.run(
            [str(executable), "--version"],
            timeout=_VERSION_PROBE_TIMEOUT_SECONDS,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        version, reason = None, "audio.cpp version probe timed out after 2 seconds."
    except OSError:
        version, reason = None, "audio.cpp version probe could not run."
    else:
        output = result.stdout if isinstance(result.stdout, str) else ""
        match = _VERSION_PATTERN.search(output) if result.returncode == 0 else None
        version = match.group(1) if match is not None else None
        backend_match = _BACKENDS_PATTERN.search(output) if version is not None else None
        if backend_match is None:
            compiled_backends = ("cpu",) if version is not None else ()
        else:
            names = {
                item.strip().lower()
                for item in re.split(r"[,\s]+", backend_match.group(1))
                if item.strip()
            }
            compiled_backends = tuple(
                backend for backend in _KNOWN_BACKENDS if backend in names
            )
        if version is None:
            reason = (
                "Could not determine the audio.cpp version; audio generation "
                f"requires {MINIMUM_RUNTIME_VERSION} or newer."
            )
        elif _version_tuple(version) < _version_tuple(MINIMUM_RUNTIME_VERSION):
            reason = (
                f"audio.cpp {version} is too old; audio generation requires "
                f"{MINIMUM_RUNTIME_VERSION} or newer."
            )
        else:
            reason = None

    try:
        if _runtime_stat_key(executable) != key:
            return (
                executable,
                None,
                None,
                "audio.cpp runtime changed during version detection; retry.",
            )
    except OSError:
        return None, None, None, "audio.cpp executable is unavailable."

    with _runtime_lock:
        _runtime_versions[key] = (version, reason)
        _runtime_backends[key] = compiled_backends
    return executable, key, version, reason


def _hash_executable(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def validate_request(payload: dict, *, resolved_seed: int | None = None) -> dict[str, Any]:
    """Validate and canonicalize either legacy SFX or a registered generation request."""
    return generation_payload.validate_request(payload, resolved_seed=resolved_seed)


def seed_maximum(model_id: str) -> int:
    """Return the model-specific seed ceiling from the immutable registry."""
    return generation_models.seed_maximum(model_id)


def runtime_backends(settings: dict) -> tuple[str, ...]:
    """Return the backend flags reported by the managed runtime binary."""
    _executable, key, version, _reason = _runtime_status(settings)
    if key is None or version is None:
        return ()
    with _runtime_lock:
        return _runtime_backends.get(key, ("cpu",))


def require_backend(settings: dict, backend: str) -> None:
    """Fail closed unless the managed runtime advertises the requested backend."""
    if not isinstance(backend, str) or backend.strip().lower() not in _KNOWN_BACKENDS:
        raise AudioCppSfxError("Unknown audio.cpp backend.")
    normalized = backend.strip().lower()
    _executable, key, version, reason = _runtime_status(settings)
    if key is None or version is None or reason is not None:
        raise AudioCppSfxError(reason or "audio.cpp runtime is unavailable.")
    with _runtime_lock:
        compiled = _runtime_backends.get(key, ("cpu",))
    if normalized not in compiled:
        raise AudioCppSfxError(
            f"The {normalized} backend is not compiled in the configured audio.cpp runtime."
        )


def sound_catalogue(settings: dict) -> dict[str, Any]:
    """Return a safe, read-only projection of local audio generation models."""

    _executable, _stat_key, version, reason = _runtime_status(settings)
    runtime_available = version is not None and reason is None
    compiled_backends = runtime_backends(settings)
    cached_by_id: dict[str, bool] = {}
    for model in generation_models.all_models():
        if model.id == MODEL_ID:
            # Keep the legacy SFX cache probe wired through the established
            # public cache_path/model_info APIs (and their exact cache layout).
            path = assets.cache_path(MODEL_ID, settings)
            try:
                cached_by_id[model.id] = (
                    path.is_file()
                    and not path.is_symlink()
                    and path.stat().st_size == assets.MODELS[MODEL_ID]["size_bytes"]
                )
            except OSError:
                cached_by_id[model.id] = False
        else:
            cached_by_id[model.id] = generation_assets.is_cached(model.id, settings)
    model_descriptors = generation_models.model_descriptors(
        cached_by_id=cached_by_id,
        compiled_backends=compiled_backends if runtime_available else None,
    )
    unavailable = generation_models.unavailable_models()
    for descriptor in model_descriptors:
        if not descriptor["available"]:
            unavailable.append(
                {
                    "id": descriptor["id"],
                    "label": descriptor["label"],
                    "family": descriptor["family"],
                    "available": False,
                    "reason": descriptor["reason"],
                }
            )
    for descriptor in model_descriptors:
        if descriptor["id"] == MODEL_ID:
            # Preserve fields consumed by the established SFX catalogue path.
            descriptor.update(
                {
                    "task": "gen",
                    "model_revision": MODEL_REVISION,
                    "model_sha256": MODEL_SHA256,
                    "minimum_runtime_version": MINIMUM_RUNTIME_VERSION,
                }
            )
            break

    return {
        "provider": PROVIDER,
        "operation": OPERATION,
        "models": model_descriptors,
        "unavailable_models": unavailable,
        "runtime": {
            "available": runtime_available,
            "version": version,
            "reason": reason,
            "backends": list(compiled_backends),
        },
        "backends": list(compiled_backends),
        "defaults": {
            "model_id": MODEL_ID,
            "duration_seconds": DEFAULT_DURATION_SECONDS,
            "threads": min(4, _logical_cpu_count()),
            "backend": "cpu",
            "num_inference_steps": DEFAULT_INFERENCE_STEPS,
            "guidance_scale": DEFAULT_GUIDANCE_SCALE,
            "kind": "sound",
            "lyrics": "",
            "parameters": {},
        },
    }


def runtime_identity(settings: dict, model_id: str = MODEL_ID) -> dict[str, Any]:
    """Return version and full immutable model identity without private paths."""

    try:
        model_spec = generation_models.get_model(model_id)
    except ValueError as error:
        raise AudioCppSfxError(str(error)) from error

    executable, key, version, reason = _runtime_status(settings)
    if executable is None or key is None or version is None or reason is not None:
        raise AudioCppSfxError(reason or "audio.cpp runtime is unavailable.")

    with _runtime_lock:
        runtime_sha256 = _runtime_hashes.get(key)
    if runtime_sha256 is None:
        try:
            runtime_sha256 = _hash_executable(executable)
            if _runtime_stat_key(executable) != key:
                raise AudioCppSfxError(
                    "audio.cpp runtime changed while its identity was being read; retry."
                )
        except OSError as error:
            raise AudioCppSfxError("Could not identify the audio.cpp runtime binary.") from error
        with _runtime_lock:
            _runtime_hashes[key] = runtime_sha256

    identity: dict[str, Any] = {
        "provider": PROVIDER,
        "operation": OPERATION,
        "model_id": model_id,
        "runtime_version": version,
        "runtime_sha256": runtime_sha256,
        "model_revision": model_spec.revision,
        "minimum_runtime_version": MINIMUM_RUNTIME_VERSION,
        "model_assets": [
            {
                "path": asset.path,
                "size_bytes": asset.size_bytes,
                "sha256": asset.sha256,
            }
            for asset in model_spec.files
        ],
    }
    if len(model_spec.files) == 1:
        identity["model_sha256"] = model_spec.files[0].sha256
    else:
        manifest = json.dumps(
            identity["model_assets"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        identity["model_sha256"] = hashlib.sha256(manifest).hexdigest()
    if model_id == MODEL_ID:
        # Keep the original pinned SFX identity fields byte-for-byte compatible.
        identity["model_revision"] = MODEL_REVISION
        identity["model_sha256"] = MODEL_SHA256
    return identity


def generation_fingerprint(request: Mapping[str, Any], identity: Mapping[str, Any]) -> str:
    """Hash canonical provider, operation, request, and runtime/model identity."""

    canonical = json.dumps(
        {
            "provider": PROVIDER,
            "operation": OPERATION,
            "request": dict(request),
            "identity": dict(identity),
        },
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def request_runtime_identity(settings: dict, request: Mapping[str, Any]) -> dict[str, Any]:
    """Identify the runtime, base model, and any selected immutable adapter."""
    canonical = validate_request(dict(request), resolved_seed=0)
    model_id = canonical["model_id"]
    identity = (
        runtime_identity(settings)
        if model_id == MODEL_ID
        else runtime_identity(settings, model_id=model_id)
    )
    if canonical.get("parameters", {}).get("instrumental") is True:
        adapter = yue2_adapter.identity()
        identity = {
            **identity,
            "adapters": [adapter],
            "model_assets": [
                *identity.get("model_assets", []),
                {
                    "path": adapter["filename"],
                    "size_bytes": adapter["size_bytes"],
                    "sha256": adapter["sha256"],
                    "revision": adapter["revision"],
                },
            ],
        }
    return identity


def _validate_output_wav(
    path: Path, duration_seconds: float, model_id: str = MODEL_ID
) -> dict[str, int | float]:
    if path.is_symlink() or not path.is_file():
        raise processing.AudioProcessingError("audio.cpp produced no regular output WAV.")
    try:
        info = processing._wav_info(path)
    except processing.AudioProcessingError as error:
        raise processing.AudioProcessingError(
            "audio.cpp produced an invalid WAV."
        ) from error

    if (
        info["rate"] <= 0
        or info["channels"] not in {1, 2}
        or info["sampwidth"] != SAMPLE_WIDTH
    ):
        if model_id == MODEL_ID:
            raise processing.AudioProcessingError(
                "Stable Audio SFX output must be 44.1 kHz stereo PCM16."
            )
        raise processing.AudioProcessingError(
            "audio.cpp output must be mono or stereo PCM16 with a positive sample rate."
        )
    if model_id == MODEL_ID:
        if info["rate"] != SAMPLE_RATE or info["channels"] != CHANNELS:
            raise processing.AudioProcessingError(
                "Stable Audio SFX output must be 44.1 kHz stereo PCM16."
            )
    if generation_models.get_model(model_id).family == "stable_audio":
        requested_frames = duration_seconds * info["rate"]
        if abs(info["frames"] - requested_frames) > 1.0:
            raise processing.AudioProcessingError(
                "audio.cpp output duration does not match the requested duration."
            )

    if model_id == MODEL_ID:
        has_nonzero_audio = False
        try:
            with wave.open(str(path), "rb") as wav_file:
                while audio_chunk := wav_file.readframes(8192):
                    if any(audio_chunk):
                        has_nonzero_audio = True
                        break
        except (OSError, wave.Error) as error:
            raise processing.AudioProcessingError(
                "Stable Audio SFX produced an invalid WAV."
            ) from error
        if not has_nonzero_audio:
            raise processing.AudioProcessingError("Stable Audio SFX output must contain nonzero audio.")

    return {
        "frames": info["frames"],
        "sample_rate": info["rate"],
        "channels": info["channels"],
        "sample_width": info["sampwidth"],
        "duration_seconds": info["duration_s"],
    }


def generate_sound(
    request: dict,
    output_path: Path,
    *,
    settings: dict,
    cancel_event: threading.Event,
    progress: ProgressCallback | None = None,
    run_func: RunFunction = subprocess.run,
) -> dict[str, int | float]:
    """Generate one validated audio.cpp WAV into caller-owned private staging."""

    canonical_request = validate_request(request)
    model_id = canonical_request["model_id"]
    target = Path(output_path)
    if os.path.lexists(target):
        raise processing.AudioProcessingError(
            "audio.cpp output path already exists; refusing to overwrite it."
        )
    if not target.parent.is_dir():
        raise processing.AudioProcessingError("audio.cpp output parent must already exist.")

    processing.check_cancelled(cancel_event)
    require_backend(settings, canonical_request["backend"])
    if model_id == MODEL_ID:
        runtime_identity(settings)
        model_path = assets.ensure_model(
            model_id,
            dict(settings),
            cancel_event=cancel_event,
            progress=progress,
        )
    else:
        runtime_identity(settings, model_id=model_id)
        model_path = generation_assets.ensure_model(
            model_id,
            dict(settings),
            cancel_event=cancel_event,
            progress=progress,
        )
    executable = assets.resolve_executable(settings)
    processing.check_cancelled(cancel_event)

    adapter_options: dict[str, Path] = {}
    if canonical_request.get("parameters", {}).get("instrumental") is True:
        adapter_options["instrumental_adapter_path"] = yue2_adapter.ensure_adapter(
            dict(settings), cancel_event=cancel_event, progress=progress
        )
    command = generation_payload.build_argv(
        executable, canonical_request, model_path, target, **adapter_options
    )
    if progress is not None:
        progress("generate", 0, 1)
    processing._run_child(
        command,
        dict(settings),
        cancel_event,
        run_func,
        purpose="audio.cpp generation",
    )
    processing.check_cancelled(cancel_event)
    wav_properties = _validate_output_wav(
        target, canonical_request["duration_seconds"], model_id
    )
    if progress is not None:
        progress("generate", 1, 1)
    return wav_properties


__all__ = [
    "AudioCppSfxError",
    "DEFAULT_DURATION_SECONDS",
    "DEFAULT_GUIDANCE_SCALE",
    "DEFAULT_INFERENCE_STEPS",
    "MINIMUM_RUNTIME_VERSION",
    "MODEL_ID",
    "MODEL_REVISION",
    "MODEL_SHA256",
    "generate_sound",
    "generation_fingerprint",
    "runtime_identity",
    "request_runtime_identity",
    "runtime_backends",
    "require_backend",
    "seed_maximum",
    "sound_catalogue",
    "validate_request",
]
