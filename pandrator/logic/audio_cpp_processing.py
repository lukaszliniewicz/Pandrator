"""Native vocal isolation and voice-sample cleanup via audio.cpp.

Two narrow adapters, both preserving the source file byte-for-byte and never
shifting the timeline:

- :func:`isolate_vocals` runs ``--task sep`` with the BS-RoFormer or
  Mel-Band RoFormer GGUF selected by ``settings["transcription_vocal_isolation"]``.
- :func:`clean_voice_sample` runs ``--task s2s`` with the DeepFilterNet2
  weights through the ``builtin_audio_utils`` family
  (``--load-option utility=deepfilternet2``).

CLI shapes follow audio.cpp 0.8.1 ``docs/audio_tools.md``. The DFN2 route
(``--task s2s --family builtin_audio_utils --model <dir>
--load-option utility=deepfilternet2 --audio <48k.wav> --out <wav>``) was
additionally verified against a native 0.8.1 CPU run on synthetic audio
(48000 frames in, 48000 frames out, 48 kHz mono); acoustic quality is not
claimed by these tests.
Model outputs are validated, never silently accepted empty, and rejected when
their duration drifts from the model input (no trim/shift/repair is attempted).
Duration tolerance is a bounded absolute window only: 1024 samples at the
output rate, capped at 50 ms. There is no duration-relative allowance.

Progress callbacks everywhere in these two modules take
``progress(stage, done, total)`` with these stages:
``download`` (bytes), ``verify`` (bytes), ``reuse`` (bytes, Manager hardlink/copy),
``normalize`` (0..1), ``isolate``/``clean`` (0..1), ``install`` (0..1).
``ensure_model`` progress callbacks are forwarded through the processing calls.
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable, Sequence
from contextlib import nullcontext
from pathlib import Path

from . import audio_cpp_assets as assets
from .audio_cpp_execution import native_audio_cpp_guard
from .cancellable_process import ProcessCancelled, run_cancellable

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int, int], None]

SUPPORTED_ISOLATION_MODES = ("off", "bs_roformer", "mel_band_roformer")
ISOLATION_MODEL = {
    "bs_roformer": "bs_roformer",
    "mel_band_roformer": "mel_band_roformer",
}
SEPARATION_INPUT_HZ = 44100
DFN2_INPUT_HZ = 48000
_DEFAULT_TIMEOUT_SECONDS = 3600.0
_BACKENDS = {"cpu", "best", "cuda", "vulkan", "hip", "metal"}


class AudioProcessingError(RuntimeError):
    """A vocal processing step cannot safely produce output."""


def check_cancelled(event: threading.Event | None) -> None:
    if event is not None and event.is_set():
        raise ProcessCancelled("Audio processing was canceled.")


def isolation_mode(settings: dict) -> str:
    mode = str(settings.get("transcription_vocal_isolation") or "off").strip().lower()
    if mode in {"none", "false", "disabled"}:
        return "off"
    if mode not in SUPPORTED_ISOLATION_MODES:
        raise AudioProcessingError(
            "Unknown transcription_vocal_isolation "
            f"{mode!r}; expected one of {', '.join(SUPPORTED_ISOLATION_MODES)}."
        )
    return mode


def _backend(settings: dict) -> str:
    backend = (
        str(
            settings.get("audio_cpp_backend")
            or settings.get("stt_compute_backend")
            or "best"
        )
        .strip()
        .lower()
    )
    if backend == "auto":
        backend = "best"
    if backend not in _BACKENDS:
        raise AudioProcessingError(f"Unsupported audio.cpp backend: {backend}")
    return backend


def _timeout_seconds(settings: dict, *, duration_seconds: float = 0.0) -> float:
    try:
        timeout = float(settings.get("audio_cpp_timeout_seconds") or 0)
    except (TypeError, ValueError) as error:
        raise AudioProcessingError(
            "audio_cpp_timeout_seconds must be a number of seconds."
        ) from error
    if not math.isfinite(timeout):
        raise AudioProcessingError(
            "audio_cpp_timeout_seconds must be a finite number of seconds."
        )
    if timeout <= 0:
        # RoFormer already windows internally but can run slower than real time
        # on older GPUs. Budget six times the recording plus startup, retaining
        # the one-hour floor and finite 24-hour ceiling. Explicit limits win.
        return min(max(_DEFAULT_TIMEOUT_SECONDS, duration_seconds * 6 + 120), 24 * 3600.0)
    return min(timeout, 24 * 3600.0)


def _int_setting(settings: dict, key: str, default: int, minimum: int) -> int:
    try:
        return max(minimum, int(settings.get(key) or default))
    except (TypeError, ValueError) as error:
        raise AudioProcessingError(f"{key} must be an integer.") from error


def _wav_info(path: Path) -> dict:
    try:
        with wave.open(str(path), "rb") as wav:
            params = wav.getparams()
            frames = wav.getnframes()
            if params.comptype == "NONE" and frames > 0:
                # The header-declared frame count is untrusted: prove the last
                # frame is actually readable without loading the whole file.
                try:
                    wav.setpos(frames - 1)
                    tail = wav.readframes(1)
                except (OSError, wave.Error, EOFError) as error:
                    raise AudioProcessingError(
                        f"Truncated WAV data in file: {path}"
                    ) from error
                if len(tail) < params.sampwidth * params.nchannels:
                    raise AudioProcessingError(f"Truncated WAV data in file: {path}")
    except AudioProcessingError:
        raise
    except (OSError, wave.Error) as error:
        raise AudioProcessingError(f"Not a readable WAV file: {path}") from error
    if params.comptype != "NONE" or frames <= 0:
        raise AudioProcessingError(f"Unsupported or empty WAV file: {path}")
    duration = frames / float(params.framerate or 1)
    if not 0 < duration < 24 * 3600:
        raise AudioProcessingError(f"WAV duration out of bounds: {path}")
    return {
        "channels": params.nchannels,
        "sampwidth": params.sampwidth,
        "rate": params.framerate,
        "frames": frames,
        "duration_s": duration,
    }


def _require_ffmpeg() -> str:
    found = shutil.which("ffmpeg")
    if not found:
        raise AudioProcessingError(
            "Audio normalization requires ffmpeg, which was not found on PATH."
        )
    return found


def _looks_like_wav(path: Path) -> bool:
    try:
        with path.open("rb") as source:
            header = source.read(12)
    except OSError:
        return False
    return len(header) == 12 and header[:4] == b"RIFF" and header[8:12] == b"WAVE"


def _normalize_wav(
    source: Path,
    temporary: Path,
    *,
    rate: int,
    channels: int,
    settings: dict,
    cancel_event: threading.Event | None,
    run_func: Callable | None,
    label: str,
    progress: ProgressCallback | None = None,
) -> tuple[Path, dict]:
    """Return a (path, info) WAV at the required rate/layout, converting if needed."""
    try:
        info = _wav_info(source)
    except AudioProcessingError:
        # A corrupt file wearing a WAV header is rejected outright: handing
        # it to ffmpeg for salvage could silently shift the timeline.
        if _looks_like_wav(source):
            raise
        info = None
    if (
        info is not None
        and info["rate"] == rate
        and info["channels"] == channels
        and info["sampwidth"] == 2
    ):
        if progress is not None:
            progress("normalize", 1, 1)
        return source, info
    if progress is not None:
        progress("normalize", 0, 1)
    ffmpeg = _require_ffmpeg()
    target = temporary / f"{label}-{rate}hz.wav"
    command = [
        ffmpeg,
        "-y",
        "-i",
        str(source),
        "-ac",
        str(channels),
        "-ar",
        str(rate),
        "-c:a",
        "pcm_s16le",
        str(target),
    ]
    _run_child(
        command,
        settings,
        cancel_event,
        run_func,
        purpose=f"{label} normalization",
    )
    if progress is not None:
        progress("normalize", 1, 1)
    return target, _wav_info(target)


def _run_child(
    command: Sequence[str | os.PathLike[str]],
    settings: dict,
    cancel_event: threading.Event | None,
    run_func: Callable | None,
    *,
    purpose: str,
) -> None:
    """Run one child process with cancellation and a finite watchdog timeout."""
    check_cancelled(cancel_event)
    timeout = _timeout_seconds(settings)
    executable_dir = Path(os.fspath(command[0])).parent
    if run_func is not None and run_func is not subprocess.run:
        # Test seam: answers synchronously instead of spawning a child, but
        # settings are still validated above and cancellation still honored.
        try:
            run_func(
                list(command),
                check=True,
                capture_output=True,
                cwd=str(executable_dir),
            )
        except ProcessCancelled:
            raise
        except (OSError, subprocess.CalledProcessError) as error:
            raise AudioProcessingError(
                f"audio.cpp {purpose} failed ({type(error).__name__})."
            ) from error
        check_cancelled(cancel_event)
        return
    # The watchdog never touches the caller's event: cancellation and timeout
    # are observed into an independent internal event, and a timeout always
    # surfaces as an explicit error rather than a cancellation.
    done = threading.Event()
    fired = threading.Event()
    internal = threading.Event()

    def watch() -> None:
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            if done.wait(min(remaining, 0.05)):
                return
            if cancel_event is not None and cancel_event.is_set():
                internal.set()
                return
        if not done.is_set():
            fired.set()
            internal.set()

    watchdog = threading.Thread(target=watch, daemon=True)
    watchdog.start()
    try:
        # FFmpeg normalization does not load a model. Native inference shares
        # the process lock and GPU-residency check with local TTS requests.
        with (
            native_audio_cpp_guard(command, internal)
            if "--family" in command else nullcontext()
        ):
            run_cancellable(
                command,
                cancel_event=internal,
                check=True,
                capture_output=True,
                cwd=str(executable_dir),
            )
    except ProcessCancelled as error:
        if fired.is_set() and (cancel_event is None or not cancel_event.is_set()):
            raise AudioProcessingError(
                f"audio.cpp {purpose} timed out after {timeout:.0f} seconds."
            ) from error
        raise
    except (OSError, subprocess.CalledProcessError) as error:
        raise AudioProcessingError(
            f"audio.cpp {purpose} failed ({type(error).__name__})."
        ) from error
    finally:
        done.set()
        watchdog.join(timeout=5)
    check_cancelled(cancel_event)


def _validate_output_wav(path: Path, expected_duration: float, label: str) -> dict:
    """Refuse empty, unreadable, or timeline-shifted model output."""
    if not path.is_file():
        raise AudioProcessingError(f"audio.cpp {label} produced no output file.")
    try:
        if path.stat().st_size <= 0:
            raise AudioProcessingError(
                f"audio.cpp {label} produced an empty output; not accepted."
            )
    except OSError as error:
        raise AudioProcessingError(
            f"audio.cpp {label} output is unreadable: {error}"
        ) from error
    info = _wav_info(path)
    # Bounded absolute tolerance only: 1024 samples at the output rate,
    # capped at 50 ms. No duration-relative allowance (1% would be 36 s/hour).
    tolerance = min(0.050, 1024.0 / float(info["rate"] or 1))
    if abs(info["duration_s"] - expected_duration) > tolerance:
        raise AudioProcessingError(
            f"audio.cpp {label} output duration "
            f"({info['duration_s']:.3f}s) drifted from its input "
            f"({expected_duration:.3f}s); timeline shifts are not accepted."
        )
    return info


def _reject_same_path(origin: Path, target: Path, operation: str) -> None:
    try:
        same = origin.resolve() == target.resolve()
    except OSError as error:
        raise AudioProcessingError(
            f"Could not resolve {operation} paths: {error}"
        ) from error
    if same:
        raise AudioProcessingError(
            f"{operation} source and destination are the same file; "
            "the original must be preserved, so refusing to overwrite it."
        )


def _atomic_install(staged: Path, destination: Path) -> None:
    # Stage inside the destination directory so os.replace stays on one
    # filesystem (/tmp and the workspace are often different mounts).
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        dir=destination.parent, prefix=".pandrator-install-", delete=False
    ) as output:
        slot = Path(output.name)
    try:
        shutil.copyfile(staged, slot)
        os.replace(slot, destination)
    except BaseException:
        slot.unlink(missing_ok=True)
        raise


def isolate_vocals(
    source: Path | str,
    destination: Path | str,
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
    run_func: Callable | None = None,
) -> dict:
    """Separate vocals with BS/Mel-Band RoFormer; ``off`` skips without side effects."""
    mode = isolation_mode(settings)
    values = dict(settings)
    if mode == "off":
        return {
            "status": "skipped",
            "reason": "transcription_vocal_isolation is off",
            "model": None,
        }
    check_cancelled(cancel_event)
    origin = Path(source)
    target = Path(destination)
    if not origin.is_file():
        raise AudioProcessingError(f"Isolation source not found: {origin}")
    _reject_same_path(origin, target, "Vocal isolation")
    model_id = ISOLATION_MODEL[mode]
    model = assets.ensure_model(
        model_id, values, cancel_event=cancel_event, progress=progress
    )
    executable = assets.resolve_executable(values)
    with tempfile.TemporaryDirectory(prefix="pandrator-isolate-") as temporary:
        root = Path(temporary)
        normalized, model_input = _normalize_wav(
            origin,
            root,
            rate=SEPARATION_INPUT_HZ,
            channels=2,
            settings=values,
            cancel_event=cancel_event,
            run_func=run_func,
            label="isolation-input",
            progress=progress,
        )
        if progress is not None:
            progress("isolate", 0, 1)
        out_dir = root / "stems"
        out_dir.mkdir(exist_ok=True)
        command = [
            str(executable),
            "--task",
            "sep",
            "--family",
            model_id,
            "--model",
            str(model),
            "--backend",
            _backend(values),
            "--device",
            str(_int_setting(values, "audio_cpp_device", 0, 0)),
            "--threads",
            str(_int_setting(values, "audio_cpp_threads", 4, 1)),
            "--audio",
            str(normalized.resolve()),
            "--out-dir",
            str(out_dir),
        ]
        isolation_settings = {
            **values,
            "audio_cpp_timeout_seconds": _timeout_seconds(
                values, duration_seconds=model_input["duration_s"]
            ),
        }
        _run_child(command, isolation_settings, cancel_event, run_func, purpose="vocal isolation")
        check_cancelled(cancel_event)
        vocals = out_dir / "vocals.wav"
        if not vocals.is_file():
            candidates = sorted(out_dir.glob("*vocal*.wav"))
            vocals = candidates[0] if candidates else vocals
        output_info = _validate_output_wav(
            vocals, model_input["duration_s"], "vocal isolation"
        )
        staged = root / "vocals-out.wav"
        shutil.copyfile(vocals, staged)
        _atomic_install(staged, target)
    if progress is not None:
        progress("isolate", 1, 1)
        progress("install", 1, 1)
    return {
        "status": "isolated",
        "model": model_id,
        "family": model_id,
        "source_duration_s": model_input["duration_s"],
        "output_duration_s": output_info["duration_s"],
        "output_sample_rate_hz": output_info["rate"],
        "output_path": str(target),
    }


def clean_voice_sample(
    source: Path | str,
    destination: Path | str,
    settings: dict,
    cancel_event: threading.Event | None = None,
    progress: ProgressCallback | None = None,
    run_func: Callable | None = None,
) -> dict:
    """Denoise a voice sample with DeepFilterNet2 (48 kHz in and out)."""
    values = dict(settings)
    check_cancelled(cancel_event)
    origin = Path(source)
    target = Path(destination)
    if not origin.is_file():
        raise AudioProcessingError(f"Cleanup source not found: {origin}")
    _reject_same_path(origin, target, "Voice cleanup")
    model = assets.ensure_model(
        "deepfilternet2", values, cancel_event=cancel_event, progress=progress
    )
    model_dir = model.parent if model.suffix == ".safetensors" else model
    executable = assets.resolve_executable(values)
    with tempfile.TemporaryDirectory(prefix="pandrator-clean-voice-") as temporary:
        root = Path(temporary)
        normalized, model_input = _normalize_wav(
            origin,
            root,
            rate=DFN2_INPUT_HZ,
            channels=1,
            settings=values,
            cancel_event=cancel_event,
            run_func=run_func,
            label="cleanup-input",
            progress=progress,
        )
        if progress is not None:
            progress("clean", 0, 1)
        enhanced = root / "enhanced.wav"
        command = [
            str(executable),
            "--task",
            "s2s",
            "--family",
            "builtin_audio_utils",
            "--model",
            str(model_dir),
            "--load-option",
            "utility=deepfilternet2",
            "--backend",
            _backend(values),
            "--device",
            str(_int_setting(values, "audio_cpp_device", 0, 0)),
            "--threads",
            str(_int_setting(values, "audio_cpp_threads", 4, 1)),
            "--audio",
            str(normalized.resolve()),
            "--out",
            str(enhanced),
        ]
        _run_child(command, values, cancel_event, run_func, purpose="voice cleanup")
        check_cancelled(cancel_event)
        output_info = _validate_output_wav(
            enhanced, model_input["duration_s"], "voice cleanup"
        )
        _atomic_install(enhanced, target)
    if progress is not None:
        progress("clean", 1, 1)
        progress("install", 1, 1)
    return {
        "status": "cleaned",
        "model": "deepfilternet2",
        "utility": "deepfilternet2",
        "source_duration_s": model_input["duration_s"],
        "output_duration_s": output_info["duration_s"],
        "output_sample_rate_hz": output_info["rate"],
        "output_path": str(target),
    }
