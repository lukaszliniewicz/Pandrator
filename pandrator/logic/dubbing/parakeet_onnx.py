"""ONNX Parakeet transcription adapter for Pandrator dubbing."""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from .models import SubtitleSegment
from .srt_utils import compose_srt
from .stt_backends import STT_BACKEND_PARAKEET_ONNX, normalize_stt_language_for_backend

logger = logging.getLogger(__name__)

DEFAULT_PARAKEET_MODEL = "nemo-parakeet-tdt-0.6b-v3"
DEFAULT_PARAKEET_VAD_MAX_SPEECH_SECONDS = 15.0
DEFAULT_PARAKEET_VAD_THRESHOLD = 0.5
DEFAULT_PARAKEET_VAD_MIN_SILENCE_MS = 100.0
DEFAULT_PARAKEET_VAD_MIN_SPEECH_MS = 250.0
DEFAULT_PARAKEET_VAD_SPEECH_PAD_MS = 30.0
DEFAULT_PARAKEET_VAD_BATCH_SIZE = 8
PARAKEET_RESULT_MARKER = "PARAKEET_ONNX_RESULT="


class ParakeetTranscriptionError(RuntimeError):
    """Raised when ONNX Parakeet transcription cannot complete."""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def configure_huggingface_cache(settings: dict[str, Any] | None = None) -> None:
    settings = settings or {}
    explicit_cache = str(settings.get("parakeet_hf_cache_dir") or "").strip()
    if explicit_cache:
        cache_root = Path(explicit_cache).expanduser()
        os.environ["HF_HOME"] = str(cache_root)
        os.environ.setdefault("HF_HUB_CACHE", str(cache_root / "hub"))
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(cache_root / "hub"))
        os.environ.setdefault("TRANSFORMERS_CACHE", str(cache_root / "transformers"))
    else:
        cache_root = Path(os.environ.get("HF_HOME") or (_repo_root() / "cache" / "huggingface")).expanduser()
        os.environ.setdefault("HF_HOME", str(cache_root))
        os.environ.setdefault("HF_HUB_CACHE", str(cache_root / "hub"))
        os.environ.setdefault("HUGGINGFACE_HUB_CACHE", str(cache_root / "hub"))
        os.environ.setdefault("TRANSFORMERS_CACHE", str(cache_root / "transformers"))

    if os.name == "nt":
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS", "1")
        os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _json_safe(value: Any) -> Any:
    try:
        import numpy as np

        if isinstance(value, np.generic):
            return value.item()
        if isinstance(value, np.ndarray):
            return [_json_safe(item) for item in value.tolist()]
    except Exception:
        pass

    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def build_vad_options(settings: dict[str, Any]) -> dict[str, float | int]:
    options: dict[str, float | int] = {
        "max_speech_duration_s": _as_float(
            settings.get("parakeet_vad_max_speech_seconds"),
            DEFAULT_PARAKEET_VAD_MAX_SPEECH_SECONDS,
        ),
        "threshold": _as_float(
            settings.get("parakeet_vad_threshold"),
            DEFAULT_PARAKEET_VAD_THRESHOLD,
        ),
        "min_silence_duration_ms": _as_float(
            settings.get("parakeet_vad_min_silence_ms"),
            DEFAULT_PARAKEET_VAD_MIN_SILENCE_MS,
        ),
        "min_speech_duration_ms": _as_float(
            settings.get("parakeet_vad_min_speech_ms"),
            DEFAULT_PARAKEET_VAD_MIN_SPEECH_MS,
        ),
        "speech_pad_ms": _as_float(
            settings.get("parakeet_vad_speech_pad_ms"),
            DEFAULT_PARAKEET_VAD_SPEECH_PAD_MS,
        ),
        "batch_size": max(
            1,
            _as_int(settings.get("parakeet_vad_batch_size"), DEFAULT_PARAKEET_VAD_BATCH_SIZE),
        ),
    }
    neg_threshold = _as_float(settings.get("parakeet_vad_neg_threshold"), 0.0)
    if neg_threshold > 0:
        options["neg_threshold"] = neg_threshold
    return options


def _seconds_to_ms(value: Any) -> int:
    return max(0, int(round(_as_float(value, 0.0) * 1000)))


def _absolute_timestamps(start: float, timestamps: Any) -> list[Any]:
    absolute: list[Any] = []
    for timestamp in _json_safe(timestamps) or []:
        if isinstance(timestamp, (int, float)):
            absolute.append(round(float(start) + float(timestamp), 6))
        elif isinstance(timestamp, dict):
            item = dict(timestamp)
            if isinstance(item.get("start"), (int, float)):
                item["start"] = round(float(start) + float(item["start"]), 6)
            if isinstance(item.get("end"), (int, float)):
                item["end"] = round(float(start) + float(item["end"]), 6)
            absolute.append(item)
        else:
            absolute.append(timestamp)
    return absolute


def serialize_segment(segment: Any, output_index: int) -> dict[str, Any] | None:
    text = str(getattr(segment, "text", "") or "").strip()
    if not text:
        return None

    start = _as_float(getattr(segment, "start", 0.0), 0.0)
    end = _as_float(getattr(segment, "end", start), start)
    if end <= start:
        end = start + 0.1

    timestamps = _json_safe(getattr(segment, "timestamps", []))
    return {
        "index": output_index,
        "start": round(start, 6),
        "end": round(end, 6),
        "text": text,
        "tokens": _json_safe(getattr(segment, "tokens", [])),
        "timestamps": timestamps,
        "absolute_timestamps": _absolute_timestamps(start, timestamps),
        "logprobs": _json_safe(getattr(segment, "logprobs", [])),
    }


def segments_to_srt_content(segments: list[dict[str, Any]]) -> str:
    subtitles = [
        SubtitleSegment(
            index=index,
            start_ms=_seconds_to_ms(segment.get("start")),
            end_ms=max(
                _seconds_to_ms(segment.get("end")),
                _seconds_to_ms(segment.get("start")) + 100,
            ),
            text=str(segment.get("text") or "").strip(),
        )
        for index, segment in enumerate(segments, start=1)
        if str(segment.get("text") or "").strip()
    ]
    return compose_srt(subtitles)


def transcribe_audio_in_process(
    audio_path: str | os.PathLike[str],
    *,
    session_dir: str | os.PathLike[str],
    video_name: str,
    settings: dict[str, Any],
) -> str:
    configure_huggingface_cache(settings)

    try:
        import onnx_asr
    except ImportError as exc:
        raise ParakeetTranscriptionError(
            "ONNX Parakeet transcription requires the optional onnx-asr package. "
            "Install the ONNX Parakeet STT component or run from its Pixi environment."
        ) from exc

    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    model_name = str(settings.get("parakeet_model") or DEFAULT_PARAKEET_MODEL).strip()
    quantization = str(settings.get("parakeet_quantization") or "").strip() or None
    providers = ["CPUExecutionProvider"]
    vad_enabled = bool(settings.get("parakeet_vad_enabled", True))
    selected_language = normalize_stt_language_for_backend(
        STT_BACKEND_PARAKEET_ONNX,
        str(settings.get("stt_language") or settings.get("whisper_language") or settings.get("language") or ""),
    )

    logger.info("Loading ONNX Parakeet model %s with quantization=%s", model_name, quantization or "fp32")
    model = onnx_asr.load_model(model_name, quantization=quantization, providers=providers)
    if vad_enabled:
        vad = onnx_asr.load_vad("silero", providers=providers)
        model = model.with_vad(vad, **build_vad_options(settings))
    model = model.with_timestamps()

    recognized = model.recognize(str(audio_path))
    raw_segments = list(recognized) if vad_enabled else [recognized]
    segments: list[dict[str, Any]] = []
    for raw_segment in raw_segments:
        serialized = serialize_segment(raw_segment, len(segments) + 1)
        if serialized:
            segments.append(serialized)

    srt_path = session_path / f"{video_name}.srt"
    json_path = session_path / f"{video_name}_parakeet.json"
    txt_path = session_path / f"{video_name}_parakeet.txt"

    payload = {
        "backend": "parakeet_onnx",
        "model": model_name,
        "quantization": quantization or "",
        "providers": providers,
        "selected_language": selected_language.name,
        "selected_language_code": selected_language.code,
        "language_mode": "auto_detect",
        "vad_enabled": vad_enabled,
        "vad_options": build_vad_options(settings) if vad_enabled else {},
        "audio_path": str(audio_path),
        "srt_path": str(srt_path),
        "segments": segments,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    srt_path.write_text(segments_to_srt_content(segments), encoding="utf-8")
    if bool(settings.get("parakeet_save_txt")):
        txt_path.write_text("\n".join(segment["text"] for segment in segments), encoding="utf-8")

    if not segments:
        raise ParakeetTranscriptionError("ONNX Parakeet did not produce any non-empty segments.")

    return str(srt_path)


def transcribe_audio_via_pixi(
    audio_path: str | os.PathLike[str],
    *,
    session_dir: str | os.PathLike[str],
    video_name: str,
    settings: dict[str, Any],
    pixi_executable: str,
    pixi_manifest: str,
    run_func: Callable[..., Any] = subprocess.run,
) -> str:
    settings_json = json.dumps(settings, ensure_ascii=False)
    command = [
        pixi_executable,
        "run",
        "--manifest-path",
        pixi_manifest,
        "--executable",
        "python",
        "-m",
        "pandrator.logic.dubbing.parakeet_onnx",
        "--audio",
        str(audio_path),
        "--session-dir",
        str(session_dir),
        "--video-name",
        str(video_name),
        "--settings-json",
        settings_json,
    ]
    env = os.environ.copy()
    configure_huggingface_cache(settings)
    env.update({key: value for key, value in os.environ.items() if key.startswith("HF_") or key in {"HUGGINGFACE_HUB_CACHE", "TRANSFORMERS_CACHE"}})
    result = run_func(
        command,
        check=True,
        capture_output=True,
        text=True,
        cwd=str(_repo_root()),
        env=env,
    )
    stdout = str(getattr(result, "stdout", "") or "")
    stderr = str(getattr(result, "stderr", "") or "")
    if stderr:
        logger.warning("ONNX Parakeet warning: %s", stderr)
    for line in reversed(stdout.splitlines()):
        if line.startswith(PARAKEET_RESULT_MARKER):
            payload = json.loads(line[len(PARAKEET_RESULT_MARKER) :])
            srt_path = str(payload.get("srt_path") or "")
            if srt_path and Path(srt_path).exists():
                return srt_path

    expected_path = Path(session_dir) / f"{video_name}.srt"
    if expected_path.exists():
        return str(expected_path)
    raise ParakeetTranscriptionError("ONNX Parakeet subprocess finished without reporting an SRT path.")


def transcribe_audio_with_parakeet(
    audio_path: str | os.PathLike[str],
    *,
    session_dir: str | os.PathLike[str],
    video_name: str,
    settings: dict[str, Any],
    pixi_executable: str = "",
    pixi_manifest: str = "",
    run_func: Callable[..., Any] = subprocess.run,
) -> str:
    if pixi_executable and pixi_manifest and Path(pixi_manifest).is_file():
        try:
            return transcribe_audio_via_pixi(
                audio_path,
                session_dir=session_dir,
                video_name=video_name,
                settings=settings,
                pixi_executable=pixi_executable,
                pixi_manifest=pixi_manifest,
                run_func=run_func,
            )
        except subprocess.CalledProcessError as exc:
            stderr = str(getattr(exc, "stderr", "") or "")
            raise ParakeetTranscriptionError(f"ONNX Parakeet subprocess failed: {stderr or exc}") from exc

    return transcribe_audio_in_process(
        audio_path,
        session_dir=session_dir,
        video_name=video_name,
        settings=settings,
    )


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run ONNX Parakeet transcription.")
    parser.add_argument("--audio", required=True)
    parser.add_argument("--session-dir", required=True)
    parser.add_argument("--video-name", required=True)
    parser.add_argument("--settings-json", default="{}")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    settings = json.loads(args.settings_json or "{}")
    srt_path = transcribe_audio_in_process(
        args.audio,
        session_dir=args.session_dir,
        video_name=args.video_name,
        settings=settings,
    )
    print(PARAKEET_RESULT_MARKER + json.dumps({"srt_path": srt_path}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
