"""Audio alignment and mix helpers for Pandrator-native dubbing."""

from __future__ import annotations

import json
import logging
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

        if alignment_blocks and alignment_blocks[-1].subtitles[-1:] == new_block.subtitles[:1]:
            previous = alignment_blocks[-1]
            alignment_blocks[-1] = AudioAlignmentBlock(
                number=f"{previous.number}-{new_block.number}",
                text=f"{previous.text} {new_block.text}".strip(),
                start_ms=previous.start_ms,
                end_ms=new_block.end_ms,
                audio_files=[*previous.audio_files, *new_block.audio_files],
                subtitles=sorted(set([*previous.subtitles, *new_block.subtitles])),
            )
        else:
            alignment_blocks.append(new_block)

    if invalid_blocks:
        raise AudioSyncError("Cannot synchronize incomplete speech blocks: " + ", ".join(invalid_blocks))
    if not alignment_blocks:
        raise ValueError("No alignment blocks could be created from the selected subtitles and speech blocks.")
    return alignment_blocks


def _atempo_filter_chain(factor: float) -> str:
    filters: list[str] = []
    remaining = float(factor)
    while remaining > 2.0:
        filters.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5:
        filters.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.01:
        filters.append(f"atempo={remaining:.3f}")
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


def align_audio_blocks(
    alignment_blocks: list[AudioAlignmentBlock],
    session_dir: str | os.PathLike[str],
    *,
    delay_start_ms: int = 2000,
    speed_up_percent: int = 115,
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
    output_path: str | os.PathLike[str] | None = None,
) -> str:
    from pydub import AudioSegment

    session_path = Path(session_dir)
    session_path.mkdir(parents=True, exist_ok=True)
    final_audio = AudioSegment.silent(duration=0)
    current_time = 0
    total_shift = 0

    for index, block in enumerate(alignment_blocks):
        block_start_ms = block.start_ms
        if index < len(alignment_blocks) - 1:
            block_duration = max(1, alignment_blocks[index + 1].start_ms - block_start_ms)
        else:
            block_duration = max(1, block.end_ms - block.start_ms)

        adjusted_block_start = block_start_ms + total_shift
        if adjusted_block_start > current_time:
            final_audio += AudioSegment.silent(duration=adjusted_block_start - current_time)
            current_time = adjusted_block_start

        block_audio = AudioSegment.silent(duration=0)
        for wav_path in block.audio_files:
            if not wav_path.exists():
                logger.warning("Skipping missing sentence WAV: %s", wav_path)
                continue
            if len(block_audio) > 0:
                block_audio += AudioSegment.silent(duration=100)
            block_audio += AudioSegment.from_wav(wav_path)

        original_audio_duration = len(block_audio)
        processed_audio = block_audio
        audio_delay = 0

        if original_audio_duration < block_duration and total_shift <= 0:
            available_time = block_duration - original_audio_duration
            audio_delay = min(max(0, int(delay_start_ms)), int(available_time * 0.7))

        should_speed_up = False
        actual_speedup_factor = 1.0
        if total_shift > 0 and speed_up_percent > 100 and original_audio_duration > 0:
            should_speed_up = True
            needed = (original_audio_duration + total_shift) / original_audio_duration
            actual_speedup_factor = min(needed, speed_up_percent / 100.0)
        elif total_shift <= 0 and original_audio_duration > block_duration and speed_up_percent > 100:
            should_speed_up = True
            needed = original_audio_duration / block_duration
            actual_speedup_factor = min(needed, speed_up_percent / 100.0)

        if should_speed_up and actual_speedup_factor > 1.01:
            with tempfile.TemporaryDirectory(prefix=".sync-", dir=session_path) as temporary:
                processed_audio = _speed_up_audio_segment(
                    block_audio,
                    actual_speedup_factor,
                    Path(temporary),
                    ffmpeg_executable=ffmpeg_executable,
                    run_func=run_func,
                )

        if audio_delay > 0:
            final_audio += AudioSegment.silent(duration=audio_delay)
            current_time += audio_delay

        final_audio += processed_audio
        current_time += len(processed_audio)
        actual_audio_duration = len(processed_audio) + audio_delay

        if actual_audio_duration > block_duration:
            total_shift += actual_audio_duration - block_duration
        else:
            silence_needed = block_duration - actual_audio_duration
            if silence_needed >= total_shift:
                silence_to_add = silence_needed - total_shift
                final_audio += AudioSegment.silent(duration=silence_to_add)
                current_time += silence_to_add
                total_shift = 0
            else:
                total_shift -= silence_needed

    destination = Path(output_path) if output_path else session_path / "aligned_audio.wav"
    destination.parent.mkdir(parents=True, exist_ok=True)
    final_audio.export(destination, format="wav")
    return str(destination)


def parse_ffmpeg_max_volume(stderr_text: str) -> float:
    match = re.search(r"max_volume:\s*(-?\d+(?:\.\d+)?)\s*dB", str(stderr_text or ""))
    if not match:
        raise ValueError("FFmpeg volume analysis did not contain max_volume.")
    return float(match.group(1))


MIX_FILTER_COMPLEX = (
    "[1]silencedetect=n=-30dB:d=2[silence];"
    "[silence]aformat=sample_fmts=u8:sample_rates=44100:channel_layouts=mono,"
    "aresample=async=1000,pan=1c|c0=c0,"
    "aformat=sample_fmts=s16:sample_rates=44100:channel_layouts=mono[silence_mono];"
    "[0][silence_mono]sidechaincompress=threshold=0.01:ratio=20:attack=100:release=500:makeup=1[gated];"
    "[1]volume=2[subtitles];"
    "[gated][subtitles]amix=inputs=2[mixed]"
)


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


def build_mix_audio_command(
    original_audio_path: str | os.PathLike[str],
    amplified_dubbed_audio_path: str | os.PathLike[str],
    mixed_audio_path: str | os.PathLike[str],
    *,
    ffmpeg_executable: str = "ffmpeg",
) -> list[str]:
    return [
        ffmpeg_executable,
        "-y",
        "-i",
        str(original_audio_path),
        "-i",
        str(amplified_dubbed_audio_path),
        "-filter_complex",
        MIX_FILTER_COMPLEX,
        "-map",
        "[mixed]",
        str(mixed_audio_path),
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
        "0:v",
        "-map",
        "1:a",
        str(output_video_path),
    ]


def _run_ffmpeg(command: list[str], *, run_func: Callable[..., Any]) -> Any:
    logger.info("Running FFmpeg command: %s", " ".join(command))
    return run_func(command, check=True, capture_output=True, text=True)


def mix_audio_tracks_with_result(
    video_path: str | os.PathLike[str],
    synced_audio_path: str | os.PathLike[str],
    session_dir: str | os.PathLike[str],
    *,
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
    analysis = _run_ffmpeg(
        build_volume_analysis_command(
            synced_audio_path,
            ffmpeg_executable=ffmpeg_executable,
        ),
        run_func=run_func,
    )
    amplification = -parse_ffmpeg_max_volume(str(getattr(analysis, "stderr", "") or ""))
    _run_ffmpeg(
        build_amplify_audio_command(
            synced_audio_path,
            amplified_dubbed_audio_path,
            amplification,
            ffmpeg_executable=ffmpeg_executable,
        ),
        run_func=run_func,
    )
    _run_ffmpeg(
        build_mix_audio_command(
            original_audio_path,
            amplified_dubbed_audio_path,
            mixed_audio_path,
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
    ffmpeg_executable: str = "ffmpeg",
    run_func: Callable[..., Any] = subprocess.run,
) -> str:
    return mix_audio_tracks_with_result(
        video_path,
        synced_audio_path,
        session_dir,
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
        ffmpeg_executable=ffmpeg_executable,
    )
    mix_result = mix_func(
        video_file,
        aligned_audio_path,
        session_dir,
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
        ffmpeg_executable=ffmpeg_executable,
        align_func=align_func,
        mix_func=mix_func,
    ).output_video_path
