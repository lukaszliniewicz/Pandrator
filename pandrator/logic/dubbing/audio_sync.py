"""Audio alignment and mix helpers for Pandrator-native dubbing."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .models import AudioAlignmentBlock
from .srt_utils import parse_srt

logger = logging.getLogger(__name__)


class AudioSyncError(RuntimeError):
    """Raised when native dubbing audio synchronization cannot complete."""


@dataclass(frozen=True)
class AudioMixResult:
    output_video_path: str = ""
    original_audio_path: str = ""
    amplified_dubbed_audio_path: str = ""
    mixed_audio_path: str = ""


@dataclass(frozen=True)
class AudioSyncResult:
    output_video_path: str = ""
    aligned_audio_path: str = ""
    original_audio_path: str = ""
    amplified_dubbed_audio_path: str = ""
    mixed_audio_path: str = ""


@dataclass(frozen=True)
class AudioAlignmentAdjustment:
    """Timing decision for one generated speech block."""

    available_ms: int
    drift_ms: int
    start_delay_ms: int
    speed_factor: float


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _load_speech_blocks(speech_blocks_file: str | os.PathLike[str]) -> list[dict[str, Any]]:
    with Path(speech_blocks_file).open("r", encoding="utf-8-sig") as handle:
        payload = json.load(handle)
    if not isinstance(payload, list):
        raise ValueError("Speech blocks JSON must contain a list.")
    return [item for item in payload if isinstance(item, dict)]


def _sentence_sort_key(path: Path) -> tuple[int, str]:
    match = re.search(r"_(\d+)\.wav$", path.name, re.IGNORECASE)
    if match:
        return int(match.group(1)), path.name.lower()
    return 10**9, path.name.lower()


def match_sentence_wav_files(
    sentence_wavs_dir: str | os.PathLike[str],
    block_number: str | int,
) -> list[Path]:
    wavs_dir = Path(sentence_wavs_dir)
    if not wavs_dir.is_dir():
        return []

    normalized_number = str(block_number or "").strip()
    block_int = _coerce_int(normalized_number, -1)
    accepted_suffixes = {f"_{normalized_number}.wav"}
    if block_int >= 0:
        accepted_suffixes.update(
            {
                f"_{block_int}.wav",
                f"_{block_int:04d}.wav",
            }
        )

    matches = [
        path
        for path in wavs_dir.iterdir()
        if path.is_file()
        and path.suffix.lower() == ".wav"
        and any(path.name.lower().endswith(suffix.lower()) for suffix in accepted_suffixes)
    ]
    return sorted(matches, key=_sentence_sort_key)


def create_alignment_blocks(
    srt_file: str | os.PathLike[str],
    speech_blocks_file: str | os.PathLike[str],
    sentence_wavs_dir: str | os.PathLike[str],
) -> list[AudioAlignmentBlock]:
    with Path(srt_file).open("r", encoding="utf-8-sig") as handle:
        subtitles = parse_srt(handle.read())
    if not subtitles:
        raise ValueError(f"No subtitles found in {srt_file}.")

    subtitles_by_index = {segment.index: segment for segment in subtitles}
    speech_blocks = _load_speech_blocks(speech_blocks_file)
    alignment_blocks: list[AudioAlignmentBlock] = []
    invalid_blocks: list[str] = []

    for block in speech_blocks:
        subtitle_ids = [_coerce_int(value, -1) for value in list(block.get("subtitles") or [])]
        block_subtitles = sorted(
            {
                subtitle_id: subtitles_by_index[subtitle_id]
                for subtitle_id in subtitle_ids
                if subtitle_id in subtitles_by_index
            }.values(),
            key=lambda item: (item.start_ms, item.end_ms, item.index),
        )
        if not block_subtitles:
            invalid_blocks.append(f"{block.get('number') or '?'} (no matching subtitle cues)")
            continue

        block_number = str(block.get("number") or "").strip()
        wav_files = match_sentence_wav_files(sentence_wavs_dir, block_number)
        if not wav_files:
            invalid_blocks.append(f"{block_number or '?'} (no generated audio)")
            continue
        new_block = AudioAlignmentBlock(
            number=block_number,
            text=str(block.get("text") or ""),
            start_ms=block_subtitles[0].start_ms,
            end_ms=block_subtitles[-1].end_ms,
            audio_files=wav_files,
            subtitles=[segment.index for segment in block_subtitles],
        )

        alignment_blocks.append(new_block)

    if invalid_blocks:
        raise AudioSyncError("Cannot synchronize incomplete speech blocks: " + ", ".join(invalid_blocks))
    if not alignment_blocks:
        raise ValueError("No alignment blocks could be created from the selected subtitles and speech blocks.")
    alignment_blocks.sort(key=lambda item: (item.start_ms, item.end_ms, item.number))
    merged: list[AudioAlignmentBlock] = []
    for block in alignment_blocks:
        if merged and merged[-1].subtitles[-1:] == block.subtitles[:1]:
            previous = merged[-1]
            merged[-1] = AudioAlignmentBlock(
                number=f"{previous.number}-{block.number}",
                text=f"{previous.text} {block.text}".strip(),
                start_ms=previous.start_ms,
                end_ms=max(previous.end_ms, block.end_ms),
                audio_files=[*previous.audio_files, *block.audio_files],
                subtitles=sorted(set([*previous.subtitles, *block.subtitles])),
            )
        else:
            merged.append(block)
    return merged


def _atempo_filter_chain(factor: float) -> str:
    filters: list[str] = []
    remaining = float(factor)
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    # Even a sub-percent overrun matters when it repeats over hundreds of
    # subtitle cues.  The previous 1% dead zone silently skipped those small
    # corrections and allowed drift to accumulate.
    if abs(remaining - 1.0) > 0.0001:
        filters.append(f"atempo={remaining:.6f}")
    return ",".join(filters)


def _speed_up_audio_segment(
    audio_segment: Any,
    factor: float,
    temp_dir: Path,
    *,
    ffmpeg_executable: str,
    run_func: Callable[..., Any],
) -> Any:
    filter_chain = _atempo_filter_chain(factor)
    if not filter_chain:
        return audio_segment

    from pydub import AudioSegment

    temp_input = temp_dir / "temp_speedup_input.wav"
    temp_output = temp_dir / "temp_speedup_output.wav"
    try:
        audio_segment.export(temp_input, format="wav")
        run_func(
            [
                ffmpeg_executable,
                "-i",
                str(temp_input),
                "-af",
                filter_chain,
                "-y",
                str(temp_output),
            ],
            check=True,
            capture_output=True,
        )
        return AudioSegment.from_wav(temp_output)
    finally:
        for path in (temp_input, temp_output):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass


def alignment_adjustment(
    audio_duration_ms: int,
    window_duration_ms: int,
    drift_ms: int,
    *,
    delay_start_ms: int,
    max_speed_factor: float,
) -> AudioAlignmentAdjustment:
    """Plan speed and placement against the *remaining* timing window.

    Subdub calculated catch-up speed from the current clip length alone. That
    could let accumulated drift grow when a later clip was also longer than its
    subtitle window. This calculation accounts for both conditions together.
    """
    duration = max(0, int(audio_duration_ms))
    window = max(1, int(window_duration_ms))
    drift = max(0, int(drift_ms))
    available = max(1, window - drift)
    maximum = min(4.0, max(1.0, float(max_speed_factor)))
    # Aim one millisecond inside the slot so codec/filter frame rounding does
    # not turn an exact calculation back into a small overrun.
    target_duration = max(1, available - 1)
    needed = (duration / target_duration) if duration > available else 1.0
    speed_factor = min(maximum, max(1.0, needed))
    estimated_duration = int(math.ceil(duration / speed_factor)) if duration else 0
    slack = max(0, available - estimated_duration)
    start_delay = 0
    if drift == 0 and slack:
        start_delay = min(max(0, int(delay_start_ms)), int(slack * 0.7))
    return AudioAlignmentAdjustment(
        available_ms=available,
        drift_ms=drift,
        start_delay_ms=start_delay,
        speed_factor=speed_factor,
    )


def align_audio_blocks(
    alignment_blocks: list[AudioAlignmentBlock],
    session_dir: str | os.PathLike[str],
    *,
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
    sentence_gap_ms: int = 100,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
    output_path: str | os.PathLike[str] | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> str:
    from pydub import AudioSegment

    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    if not alignment_blocks:
        raise AudioSyncError("No generated speech blocks were supplied for synchronization.")
    ordered_blocks = sorted(alignment_blocks, key=lambda item: (item.start_ms, item.end_ms, item.number))
    final_audio = AudioSegment.silent(duration=0)
    current_time = 0
    maximum_speed = min(4.0, max(1.0, float(speed_up_percent) / 100.0))
    sentence_gap = max(0, min(5000, int(sentence_gap_ms)))
    alignment_details: list[dict[str, Any]] = []

    for index, block in enumerate(ordered_blocks):
        block_start_ms = max(0, int(block.start_ms))
        if index < len(ordered_blocks) - 1:
            slot_end_ms = max(block_start_ms + 1, int(ordered_blocks[index + 1].start_ms))
        else:
            slot_end_ms = max(block_start_ms + 1, int(block.end_ms))
        window_duration = slot_end_ms - block_start_ms

        if current_time < block_start_ms:
            final_audio += AudioSegment.silent(duration=block_start_ms - current_time)
            current_time = block_start_ms
        drift_at_start = max(0, current_time - block_start_ms)
        block_audio = AudioSegment.silent(duration=0)
        for wav_path in block.audio_files:
            if not wav_path.exists():
                raise AudioSyncError(f"Speech block {block.number} is missing generated audio: {wav_path.name}")
            if len(block_audio) > 0:
                block_audio += AudioSegment.silent(duration=sentence_gap)
            block_audio += AudioSegment.from_wav(wav_path)
        original_audio_duration = len(block_audio)
        if original_audio_duration <= 0:
            raise AudioSyncError(f"Speech block {block.number} contains no audible generated audio.")
        adjustment = alignment_adjustment(
            original_audio_duration,
            window_duration,
            drift_at_start,
            delay_start_ms=delay_start_ms,
            max_speed_factor=maximum_speed,
        )
        processed_audio = block_audio
        applied_speed = adjustment.speed_factor
        if original_audio_duration > adjustment.available_ms and applied_speed > 1.0001:
            with tempfile.TemporaryDirectory(prefix=".sync-", dir=session_path) as temporary:
                temporary_path = Path(temporary)
                for _attempt in range(2):
                    processed_audio = _speed_up_audio_segment(
                        block_audio,
                        applied_speed,
                        temporary_path,
                        ffmpeg_executable=ffmpeg_executable,
                        run_func=run_func,
                    )
                    if len(processed_audio) <= adjustment.available_ms or applied_speed >= maximum_speed - 0.0001:
                        break
                    # FFmpeg works in audio frames, so a theoretically exact
                    # ratio can still leave a few milliseconds of overrun.
                    # Recalculate once from the measured output duration.
                    applied_speed = min(
                        maximum_speed,
                        applied_speed * (len(processed_audio) / max(1, adjustment.available_ms - 1)),
                    )
        if adjustment.start_delay_ms > 0:
            final_audio += AudioSegment.silent(duration=adjustment.start_delay_ms)
            current_time += adjustment.start_delay_ms
        final_audio += processed_audio
        current_time += len(processed_audio)
        if current_time < slot_end_ms:
            final_audio += AudioSegment.silent(duration=slot_end_ms - current_time)
            current_time = slot_end_ms
        drift_after_ms = max(0, current_time - slot_end_ms)
        effective_speed = original_audio_duration / max(1, len(processed_audio))
        alignment_details.append(
            {
                "block": block.number,
                "window_ms": window_duration,
                "available_ms": adjustment.available_ms,
                "original_audio_ms": original_audio_duration,
                "processed_audio_ms": len(processed_audio),
                "requested_speed_factor": round(applied_speed, 6),
                "effective_speed_factor": round(effective_speed, 6),
                "speed_adjusted": effective_speed > 1.0001,
                "start_delay_ms": adjustment.start_delay_ms,
                "drift_before_ms": drift_at_start,
                "drift_after_ms": drift_after_ms,
            }
        )
        logger.info(
            "Aligned block %s: window=%dms audio=%dms speed=%.3fx delay=%dms drift=%dms -> %dms",
            block.number,
            window_duration,
            original_audio_duration,
            applied_speed,
            adjustment.start_delay_ms,
            drift_at_start,
            drift_after_ms,
        )

    destination = Path(output_path) if output_path else session_path / "aligned_audio.wav"
    destination.parent.mkdir(parents=True, exist_ok=True)
    final_audio.export(destination, format="wav")
    if diagnostics is not None:
        adjusted = [item for item in alignment_details if item["speed_adjusted"]]
        diagnostics.clear()
        diagnostics.update(
            {
                "mode": "subtitle_timed",
                "configured_max_speed_factor": maximum_speed,
                "configured_max_start_delay_ms": max(0, int(delay_start_ms)),
                "configured_sentence_gap_ms": sentence_gap,
                "block_count": len(alignment_details),
                "speed_adjusted_block_count": len(adjusted),
                "max_effective_speed_factor": max(
                    (float(item["effective_speed_factor"]) for item in alignment_details),
                    default=1.0,
                ),
                "total_original_audio_ms": sum(int(item["original_audio_ms"]) for item in alignment_details),
                "total_processed_audio_ms": sum(int(item["processed_audio_ms"]) for item in alignment_details),
                "final_drift_ms": int(alignment_details[-1]["drift_after_ms"]) if alignment_details else 0,
                "blocks": alignment_details,
            }
        )
    return str(destination)


def parse_ffmpeg_max_volume(stderr_text: str) -> float:
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", str(stderr_text or ""))
    if not match:
        raise ValueError("FFmpeg volume analysis did not contain max_volume.")
    return float(match.group(1))


DUCKING_RATIOS = {
    "off": 1.0,
    "gentle": 3.0,
    "balanced": 8.0,
    "strong": 20.0,
}


def _bounded_float(value: Any, default: float, minimum: float, maximum: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(parsed):
        return default
    return min(maximum, max(minimum, parsed))


def build_mix_filter_complex(
    *,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    normalize_voice: bool = True,
    pad_source: bool = False,
) -> str:
    """Build a clipping-safe voiceover mix with optional source ducking."""
    source_gain = _bounded_float(source_gain_db, 0.0, -60.0, 12.0)
    voice_gain = _bounded_float(voice_gain_db, 0.0, -30.0, 12.0)
    target_lufs = _bounded_float(voice_lufs, -16.0, -30.0, -8.0)
    attack = int(_bounded_float(attack_ms, 25.0, 1.0, 2000.0))
    release = int(_bounded_float(release_ms, 350.0, 10.0, 5000.0))
    preset = str(ducking or "strong").strip().lower()
    ratio = DUCKING_RATIOS.get(preset, DUCKING_RATIOS["strong"])
    source_filters = ["aresample=48000:async=1000:first_pts=0", f"volume={source_gain:.2f}dB"]
    if pad_source:
        source_filters.append("apad")
    source = f"[0:a]{','.join(source_filters)}[source]"
    voice_filters = []
    if normalize_voice:
        voice_filters.append(f"loudnorm=I={target_lufs:.1f}:LRA=11:TP=-1.5")
    voice_filters.extend(
        [
            "aresample=48000:async=1000:first_pts=0",
            f"volume={voice_gain:.2f}dB",
            "apad",
        ]
    )
    voice = f"[1:a]{','.join(voice_filters)}[voice]"
    if ratio <= 1.0:
        mix = (
            "[source][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.891251:attack=5:release=100:latency=1[mixed]"
        )
    else:
        mix = (
            "[voice]asplit=2[voice_sidechain][voice_mix];"
            f"[source][voice_sidechain]sidechaincompress=threshold=0.031623:ratio={ratio:.1f}:"
            f"attack={attack}:release={release}:makeup=1[ducked];"
            "[ducked][voice_mix]amix=inputs=2:duration=first:dropout_transition=0:normalize=0,"
            "alimiter=limit=0.891251:attack=5:release=100:latency=1[mixed]"
        )
    return f"{source};{voice};{mix}"


MIX_FILTER_COMPLEX = build_mix_filter_complex()


def build_extract_original_audio_command(
    video_path: str | os.PathLike[str],
    output_audio_path: str | os.PathLike[str],
    *,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(video_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        "44100",
        "-ac",
        "2",
        str(output_audio_path),
    ]


def build_volume_analysis_command(
    audio_path: str | os.PathLike[str],
    *,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(audio_path),
        "-af",
        "volumedetect",
        "-vn",
        "-sn",
        "-dn",
        "-f",
        "null",
        os.devnull,
    ]


def build_amplify_audio_command(
    audio_path: str | os.PathLike[str],
    output_audio_path: str | os.PathLike[str],
    amplification_db: float,
    *,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(audio_path),
        "-af",
        f"volume={amplification_db}dB",
        str(output_audio_path),
    ]


def build_normalize_dubbed_audio_command(
    audio_path: str | os.PathLike[str],
    output_audio_path: str | os.PathLike[str],
    *,
    voice_lufs: float = -16.0,
    voice_gain_db: float = 0.0,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    target_lufs = _bounded_float(voice_lufs, -16.0, -30.0, -8.0)
    voice_gain = _bounded_float(voice_gain_db, 0.0, -30.0, 12.0)
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(audio_path),
        "-af",
        f"loudnorm=I={target_lufs:.1f}:LRA=11:TP=-1.5,volume={voice_gain:.2f}dB,aresample=48000",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(output_audio_path),
    ]


def build_mix_audio_command(
    original_audio_path: str | os.PathLike[str],
    amplified_dubbed_audio_path: str | os.PathLike[str],
    mixed_audio_path: str | os.PathLike[str],
    *,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    normalize_voice: bool = True,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    mix_filter = build_mix_filter_complex(
        source_gain_db=source_gain_db,
        voice_gain_db=voice_gain_db,
        voice_lufs=voice_lufs,
        ducking=ducking,
        attack_ms=attack_ms,
        release_ms=release_ms,
        normalize_voice=normalize_voice,
    )
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(original_audio_path),
        "-i",
        str(amplified_dubbed_audio_path),
        "-filter_complex",
        mix_filter,
        "-map",
        "[mixed]",
        "-ar",
        "48000",
        "-ac",
        "2",
        str(mixed_audio_path),
    ]


def build_mix_video_audio_command(
    video_path: str | os.PathLike[str],
    dubbed_audio_path: str | os.PathLike[str],
    output_video_path: str | os.PathLike[str],
    *,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    audio_bitrate: str = "192k",
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    mix_filter = build_mix_filter_complex(
        source_gain_db=source_gain_db,
        voice_gain_db=voice_gain_db,
        voice_lufs=voice_lufs,
        ducking=ducking,
        attack_ms=attack_ms,
        release_ms=release_ms,
        pad_source=True,
    )
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(dubbed_audio_path),
        "-filter_complex",
        mix_filter,
        "-map",
        "0:v:0",
        "-map",
        "[mixed]",
        "-map_metadata",
        "0",
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-b:a",
        str(audio_bitrate or "192k"),
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_video_path),
    ]


def build_mux_mixed_audio_command(
    video_path: str | os.PathLike[str],
    mixed_audio_path: str | os.PathLike[str],
    output_video_path: str | os.PathLike[str],
    *,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(video_path),
        "-i",
        str(mixed_audio_path),
        "-c:v",
        "copy",
        "-c:a",
        "aac",
        "-map",
        "0:v:0",
        "-map",
        "1:a:0",
        "-map_metadata",
        "0",
        "-movflags",
        "+faststart",
        "-shortest",
        str(output_video_path),
    ]


def media_has_audio_stream(
    media_path: str | os.PathLike[str],
    *,
    ffprobe_executable: str = "ffprobe",
    run_func: Callable[..., Any] = subprocess.run,
) -> bool:
    """Return whether the selected media exposes a usable first audio stream."""
    result = run_func(
        [
            ffprobe_executable,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            str(media_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return bool(str(getattr(result, "stdout", "") or "").strip())


def _run_ffmpeg(command: list[str], *, run_func: Callable[..., Any]) -> Any:
    logger.info("Running FFmpeg command: %s", " ".join(command))
    return run_func(command, check=True, capture_output=True, text=True)


def mix_audio_tracks_with_result(
    video_path: str | os.PathLike[str],
    synced_audio_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    *,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
) -> AudioMixResult:
    session_path = Path(session_dir)
    original_audio_path = session_path / "original_audio.wav"
    amplified_dubbed_audio_path = session_path / "amplified_dubbed_audio.wav"
    mixed_audio_path = session_path / "mixed_audio.wav"
    output_path = session_path / "final_output.mp4"

    _run_ffmpeg(
        build_extract_original_audio_command(
            video_path,
            original_audio_path,
            ffmpeg_executable=ffmpeg_executable,
        ),
        run_func=run_func,
    )
    _run_ffmpeg(
        build_normalize_dubbed_audio_command(
            synced_audio_path,
            amplified_dubbed_audio_path,
            voice_lufs=voice_lufs,
            voice_gain_db=voice_gain_db,
            ffmpeg_executable=ffmpeg_executable,
        ),
        run_func=run_func,
    )
    _run_ffmpeg(
        build_mix_audio_command(
            original_audio_path,
            amplified_dubbed_audio_path,
            mixed_audio_path,
            source_gain_db=source_gain_db,
            voice_gain_db=0.0,
            voice_lufs=voice_lufs,
            ducking=ducking,
            attack_ms=attack_ms,
            release_ms=release_ms,
            normalize_voice=False,
            ffmpeg_executable=ffmpeg_executable,
        ),
        run_func=run_func,
    )
    _run_ffmpeg(
        build_mux_mixed_audio_command(
            video_path,
            mixed_audio_path,
            output_path,
            ffmpeg_executable=ffmpeg_executable,
        ),
        run_func=run_func,
    )
    return AudioMixResult(
        output_video_path=str(output_path),
        original_audio_path=str(original_audio_path),
        amplified_dubbed_audio_path=str(amplified_dubbed_audio_path),
        mixed_audio_path=str(mixed_audio_path),
    )


def mix_audio_tracks(
    video_path: str | os.PathLike[str],
    synced_audio_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    *,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
) -> str:
    return mix_audio_tracks_with_result(
        video_path,
        synced_audio_path,
        session_dir,
        source_gain_db=source_gain_db,
        voice_gain_db=voice_gain_db,
        voice_lufs=voice_lufs,
        ducking=ducking,
        attack_ms=attack_ms,
        release_ms=release_ms,
        ffmpeg_executable=ffmpeg_executable,
        run_func=run_func,
    ).output_video_path


def synchronize_audio_video_with_result(
    session_dir: str | os.PathLike[str],
    video_file: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    speech_blocks_file: str | os.PathLike[str],
    *,
    sentence_wavs_dir: str | os.PathLike[str] = "",
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
    sentence_gap_ms: int = 100,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    ffmpeg_executable: str = "ffmpeg",
    align_func: Callable[..., str] = align_audio_blocks,
    mix_func: Callable[..., str | AudioMixResult] = mix_audio_tracks_with_result,
) -> AudioSyncResult:
    sentence_wavs_path = Path(sentence_wavs_dir) if sentence_wavs_dir else Path(session_dir) / "Sentence_wavs"
    alignment_blocks = create_alignment_blocks(
        srt_file=srt_file,
        speech_blocks_file=speech_blocks_file,
        sentence_wavs_dir=sentence_wavs_path,
    )
    aligned_audio_path = align_func(
        alignment_blocks,
        session_dir,
        delay_start_ms=delay_start_ms,
        speed_up_percent=speed_up_percent,
        sentence_gap_ms=sentence_gap_ms,
        ffmpeg_executable=ffmpeg_executable,
    )
    mix_result = mix_func(
        video_file,
        aligned_audio_path,
        session_dir,
        source_gain_db=source_gain_db,
        voice_gain_db=voice_gain_db,
        voice_lufs=voice_lufs,
        ducking=ducking,
        attack_ms=attack_ms,
        release_ms=release_ms,
        ffmpeg_executable=ffmpeg_executable,
    )
    if isinstance(mix_result, AudioMixResult):
        return AudioSyncResult(
            output_video_path=mix_result.output_video_path,
            aligned_audio_path=str(aligned_audio_path),
            original_audio_path=mix_result.original_audio_path,
            amplified_dubbed_audio_path=mix_result.amplified_dubbed_audio_path,
            mixed_audio_path=mix_result.mixed_audio_path,
        )
    return AudioSyncResult(
        output_video_path=str(mix_result),
        aligned_audio_path=str(aligned_audio_path),
    )


def synchronize_audio_video(
    session_dir: str | os.PathLike[str],
    video_file: str | os.PathLike[str],
    srt_file: str | os.PathLike[str],
    speech_blocks_file: str | os.PathLike[str],
    *,
    sentence_wavs_dir: str | os.PathLike[str] = "",
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
    sentence_gap_ms: int = 100,
    source_gain_db: float = 0.0,
    voice_gain_db: float = 0.0,
    voice_lufs: float = -16.0,
    ducking: str = "strong",
    attack_ms: int = 25,
    release_ms: int = 350,
    ffmpeg_executable: str = "ffmpeg",
    align_func: Callable[..., str] = align_audio_blocks,
    mix_func: Callable[..., str | AudioMixResult] = mix_audio_tracks_with_result,
) -> str:
    return synchronize_audio_video_with_result(
        session_dir,
        video_file,
        srt_file,
        speech_blocks_file,
        sentence_wavs_dir=sentence_wavs_dir,
        delay_start_ms=delay_start_ms,
        speed_up_percent=speed_up_percent,
        sentence_gap_ms=sentence_gap_ms,
        source_gain_db=source_gain_db,
        voice_gain_db=voice_gain_db,
        voice_lufs=voice_lufs,
        ducking=ducking,
        attack_ms=attack_ms,
        release_ms=release_ms,
        ffmpeg_executable=ffmpeg_executable,
        align_func=align_func,
        mix_func=mix_func,
    ).output_video_path
