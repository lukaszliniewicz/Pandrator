"""Decode self-contained local audio without unrestricted pydub subprocesses."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import wave
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from .cancellable_process import run_cancellable
from .media_input_policy import LOCAL_MEDIA_INPUT_OPTIONS

if TYPE_CHECKING:
    from pydub import AudioSegment

_DECODE_ERROR = "Could not decode local audio."
_SAMPLE_FORMAT_BITS = {
    "u8": 8,
    "u8p": 8,
    "s16": 16,
    "s16p": 16,
    "s32": 32,
    "s32p": 32,
    "s64": 64,
    "s64p": 64,
    "flt": 32,
    "fltp": 32,
    "dbl": 64,
    "dblp": 64,
}


def _raw_pcm_segment(
    data: bytes,
    sample_width: int,
    frame_rate: int,
    channels: int,
) -> AudioSegment:
    from pydub import AudioSegment

    if (
        sample_width <= 0
        or frame_rate <= 0
        or channels <= 0
        or len(data) % (sample_width * channels)
    ):
        raise ValueError(_DECODE_ERROR)
    if sample_width == 1:
        data = data.translate(bytes(range(128, 256)) + bytes(range(128)))
    return AudioSegment(data, sample_width=sample_width, frame_rate=frame_rate, channels=channels)


def _read_pcm_wav(handle: BinaryIO) -> tuple[bytes, int, int, int]:
    with wave.open(handle, "rb") as source:
        width, rate, channels = source.getsampwidth(), source.getframerate(), source.getnchannels()
        if source.getcomptype() != "NONE" or width <= 0 or rate <= 0 or channels <= 0:
            raise wave.Error(_DECODE_ERROR)
        data = source.readframes(source.getnframes())
        if len(data) != source.getnframes() * width * channels:
            raise EOFError(_DECODE_ERROR)
    return data, width, rate, channels


def _positive_native_int(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError(_DECODE_ERROR)
    number = float(value)
    if not math.isfinite(number) or number <= 0 or not number.is_integer():
        raise ValueError(_DECODE_ERROR)
    return int(number)


def _pcm_width(stream: dict[str, Any]) -> int:
    codec = str(stream.get("codec_name") or "")
    sample_format = str(stream.get("sample_fmt") or "")
    if sample_format == "fltp" and codec in {"mp3", "mp4", "aac", "webm", "ogg"}:
        return 2
    bits = 0
    for key in ("bits_per_raw_sample", "bits_per_sample"):
        try:
            bits = _positive_native_int(stream.get(key))
        except (TypeError, ValueError, OverflowError):
            continue
        break
    if not bits:
        bits = _SAMPLE_FORMAT_BITS.get(sample_format, 0)
    if not bits:
        raise ValueError(_DECODE_ERROR)
    return next(width for width in (1, 2, 3, 4) if bits <= width * 8 or width == 4)


def _executable(name: str) -> str:
    return str(os.environ.get(f"PANDRATOR_{name.upper()}_EXE") or shutil.which(name) or name)


def load_local_audio_segment(path: str | os.PathLike[str]) -> AudioSegment:
    """Preserve local PCM directly, otherwise restrict both native input readers."""
    from pydub.exceptions import CouldntDecodeError

    source = Path(path)
    # Opening is outside the format fallback: missing/unreadable files stay IO errors.
    with source.open("rb") as handle:
        try:
            data, width, rate, channels = _read_pcm_wav(handle)
        except (wave.Error, EOFError):
            pass
        else:
            return _raw_pcm_segment(data, width, rate, channels)
    try:
        native_source = str(source.resolve())
        probe = run_cancellable(
            [
                _executable("ffprobe"),
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_name,sample_fmt,bits_per_sample,bits_per_raw_sample,sample_rate,channels",
                "-of",
                "json",
                *LOCAL_MEDIA_INPUT_OPTIONS,
                native_source,
            ],
            check=True,
            capture_output=True,
            text=False,
            stdin=subprocess.DEVNULL,
        )
        metadata = json.loads(probe.stdout)
        streams = metadata.get("streams") if isinstance(metadata, dict) else None
        if not isinstance(streams, list) or not streams or not isinstance(streams[0], dict):
            raise ValueError(_DECODE_ERROR)
        stream = streams[0]
        width = _pcm_width(stream)
        rate = _positive_native_int(stream.get("sample_rate"))
        channels = _positive_native_int(stream.get("channels"))
        codec = "pcm_u8" if width == 1 else f"pcm_s{width * 8}le"
        raw_format = "u8" if width == 1 else f"s{width * 8}le"
        decoded = run_cancellable(
            [
                _executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                *LOCAL_MEDIA_INPUT_OPTIONS,
                "-i",
                native_source,
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-c:a",
                codec,
                "-ar",
                str(rate),
                "-ac",
                str(channels),
                "-f",
                raw_format,
                "pipe:1",
            ],
            check=True,
            capture_output=True,
            text=False,
            stdin=subprocess.DEVNULL,
        )
        if not isinstance(decoded.stdout, bytes) or not decoded.stdout:
            raise ValueError(_DECODE_ERROR)
        return _raw_pcm_segment(decoded.stdout, width, rate, channels)
    except (OSError, subprocess.SubprocessError, TypeError, ValueError, OverflowError, IndexError):
        raise CouldntDecodeError(_DECODE_ERROR) from None
