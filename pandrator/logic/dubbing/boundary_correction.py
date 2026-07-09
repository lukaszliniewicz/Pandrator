"""Energy-based WhisperX boundary correction for native dubbing."""

from __future__ import annotations

import json
import logging
import os
import wave
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .models import SubtitleSegment
from .srt_utils import compose_srt

logger = logging.getLogger(__name__)


@dataclass
class BoundaryCorrectionConfig:
    min_gap_for_check: float = 0.1
    forward_window: float = 0.1
    backward_step: float = 0.025
    max_backward_steps: int = 120
    high_energy_threshold: float = 0.5
    low_energy_threshold: float = 0.15
    spike_threshold: float = 1.5
    contaminated_windows_skip: int = 6
    lookback_window: int = 2
    boundary_buffer_steps: int = 4
    overlap_buffer: float = 0.02
    sample_rate: int = 16000


@dataclass
class BoundaryCorrectionResult:
    srt_path: str
    corrections_path: str
    corrections: list[dict[str, Any]]


AudioLoader = Callable[[str | os.PathLike[str], int], np.ndarray]


def _as_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _json_safe(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    return value


def load_audio(audio_path: str | os.PathLike[str], sample_rate: int = 16000) -> np.ndarray:
    """Load mono floating-point audio. WAV uses stdlib; other formats fall back to pydub."""
    path = Path(audio_path)
    try:
        with wave.open(str(path), "rb") as wav_file:
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()
            frame_rate = wav_file.getframerate()
            frames = wav_file.readframes(wav_file.getnframes())
        if frame_rate != sample_rate:
            raise ValueError(f"WAV sample rate {frame_rate} does not match expected {sample_rate}.")
        if sample_width == 1:
            audio = (np.frombuffer(frames, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif sample_width == 2:
            audio = np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0
        elif sample_width == 4:
            audio = np.frombuffer(frames, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"Unsupported WAV sample width: {sample_width}")
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio
    except Exception:
        from pydub import AudioSegment

        segment = (
            AudioSegment.from_file(str(path))
            .set_frame_rate(sample_rate)
            .set_channels(1)
            .set_sample_width(2)
        )
        return np.array(segment.get_array_of_samples(), dtype=np.float32) / 32768.0


def calculate_energy(audio: np.ndarray, start_sample: int, end_sample: int) -> float:
    if start_sample >= len(audio) or end_sample > len(audio) or start_sample >= end_sample:
        return 0.0
    segment = audio[start_sample:end_sample]
    if len(segment) == 0:
        return 0.0
    return float(np.sqrt(np.mean(segment**2)))


def time_to_samples(time_seconds: float, sample_rate: int) -> int:
    return int(float(time_seconds) * sample_rate)


def check_and_correct_overlaps(
    segments: list[dict[str, Any]],
    config: BoundaryCorrectionConfig,
) -> list[dict[str, Any]]:
    corrections: list[dict[str, Any]] = []
    for index in range(len(segments) - 1):
        current = segments[index]
        next_segment = segments[index + 1]
        current_end = _as_float(current.get("end"))
        next_start = _as_float(next_segment.get("start"))
        if current_end <= next_start:
            continue

        old_end = current_end
        new_end = max(_as_float(current.get("start")), next_start - config.overlap_buffer)
        current["end"] = new_end
        corrections.append(
            {
                "type": "overlap",
                "segment_index": index,
                "old_end": old_end,
                "new_end": new_end,
                "reason": (
                    f"Segment {index} end ({old_end:.3f}s) was after segment "
                    f"{index + 1} start ({next_start:.3f}s)"
                ),
            }
        )
    return corrections


def _find_energy_boundary(
    *,
    end_time: float,
    start_limit: float,
    forward_energy: float,
    energies: list[float],
    times: list[float],
    config: BoundaryCorrectionConfig,
) -> tuple[int, int, int] | None:
    skip_windows = config.contaminated_windows_skip
    lookback_window = config.lookback_window
    min_windows_needed = skip_windows + lookback_window + 1
    if len(energies) < min_windows_needed:
        return None

    speech_onset_step = None
    for index in range(skip_windows + lookback_window, len(energies)):
        current_energy = energies[index]
        previous = energies[max(skip_windows, index - lookback_window) : index]
        if not previous:
            continue
        average_previous = float(np.mean(previous))
        max_previous = float(np.max(previous))
        spike_ratio = current_energy / (average_previous + 1e-10)
        absolute_ratio = current_energy / forward_energy
        if (
            spike_ratio > config.spike_threshold
            and absolute_ratio > config.low_energy_threshold
            and current_energy > max_previous * 1.2
        ):
            speech_onset_step = index
            break

    if speech_onset_step is None:
        return None

    buffered_step = max(0, speech_onset_step - config.boundary_buffer_steps)
    boundary_step = None
    for candidate_step in range(buffered_step, -1, -1):
        if energies[candidate_step] / forward_energy <= config.low_energy_threshold:
            boundary_step = candidate_step
            break

    if boundary_step is None:
        for candidate_step in range(buffered_step, -1, -1):
            if energies[candidate_step] / forward_energy < config.high_energy_threshold:
                boundary_step = candidate_step
                break

    if boundary_step is None:
        return None

    new_end = times[boundary_step]
    if abs(new_end - end_time) <= config.backward_step or new_end >= end_time or new_end < start_limit:
        return None
    return speech_onset_step, buffered_step, boundary_step


def correct_segment_boundary(
    segment: dict[str, Any],
    next_segment: dict[str, Any],
    audio: np.ndarray,
    config: BoundaryCorrectionConfig,
    segment_index: int,
) -> dict[str, Any] | None:
    sample_rate = config.sample_rate
    segment_start = _as_float(segment.get("start"))
    segment_end = _as_float(segment.get("end"))
    next_start = _as_float(next_segment.get("start"))
    gap = next_start - segment_end
    if gap >= config.min_gap_for_check:
        return None

    forward_energy = calculate_energy(
        audio,
        time_to_samples(segment_end, sample_rate),
        time_to_samples(segment_end + config.forward_window, sample_rate),
    )
    if forward_energy == 0.0:
        return None

    energies: list[float] = []
    times: list[float] = []
    for step in range(config.max_backward_steps):
        step_end = segment_end - step * config.backward_step
        step_start = step_end - config.backward_step
        if step_start < segment_start:
            break
        energies.append(
            calculate_energy(
                audio,
                time_to_samples(step_start, sample_rate),
                time_to_samples(step_end, sample_rate),
            )
        )
        times.append(step_start)

    boundary = _find_energy_boundary(
        end_time=segment_end,
        start_limit=segment_start,
        forward_energy=forward_energy,
        energies=energies,
        times=times,
        config=config,
    )
    if boundary is None:
        return None

    speech_onset_step, buffered_step, boundary_step = boundary
    new_end = times[boundary_step]
    previous = energies[
        max(config.contaminated_windows_skip, speech_onset_step - config.lookback_window) : speech_onset_step
    ]
    correction = {
        "type": "energy_boundary",
        "segment_index": segment_index,
        "old_end": segment_end,
        "new_end": new_end,
        "gap_size": gap,
        "forward_energy": forward_energy,
        "speech_onset_energy": energies[speech_onset_step],
        "boundary_energy": energies[boundary_step],
        "boundary_energy_ratio": energies[boundary_step] / forward_energy,
        "spike_ratio": energies[speech_onset_step] / (float(np.mean(previous)) + 1e-10),
        "speech_onset_step": speech_onset_step,
        "boundary_step": boundary_step,
        "buffered_step": buffered_step,
        "reason": f"Speech onset at step {speech_onset_step}; cut at {new_end:.3f}s.",
    }
    segment["end"] = new_end
    return _json_safe(correction)


def correct_word_boundary(
    word: dict[str, Any],
    next_word: dict[str, Any],
    audio: np.ndarray,
    config: BoundaryCorrectionConfig,
    word_index: int,
) -> dict[str, Any] | None:
    sample_rate = config.sample_rate
    word_start = _as_float(word.get("start"))
    word_end = _as_float(word.get("end"))
    next_start = _as_float(next_word.get("start"))
    gap = next_start - word_end
    if gap >= config.min_gap_for_check:
        return None

    forward_energy = calculate_energy(
        audio,
        time_to_samples(word_end, sample_rate),
        time_to_samples(word_end + config.forward_window, sample_rate),
    )
    if forward_energy == 0.0:
        return None

    energies: list[float] = []
    times: list[float] = []
    for step in range(config.max_backward_steps):
        step_end = word_end - step * config.backward_step
        step_start = step_end - config.backward_step
        if step_start < word_start:
            break
        energies.append(
            calculate_energy(
                audio,
                time_to_samples(step_start, sample_rate),
                time_to_samples(step_end, sample_rate),
            )
        )
        times.append(step_start)

    boundary = _find_energy_boundary(
        end_time=word_end,
        start_limit=word_start,
        forward_energy=forward_energy,
        energies=energies,
        times=times,
        config=config,
    )
    if boundary is None:
        return None

    _speech_onset_step, _buffered_step, boundary_step = boundary
    new_end = times[boundary_step]
    word["end"] = new_end
    return {
        "type": "word_boundary",
        "word_index": word_index,
        "old_end": word_end,
        "new_end": new_end,
        "reason": f"Corrected word '{word.get('word', '')}'",
    }


def process_whisperx_segments(
    segments: list[dict[str, Any]],
    audio: np.ndarray,
    config: BoundaryCorrectionConfig | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_config = config or BoundaryCorrectionConfig()
    corrected_segments = deepcopy(segments)
    corrections = check_and_correct_overlaps(corrected_segments, active_config)
    for index in range(len(corrected_segments) - 1):
        correction = correct_segment_boundary(
            corrected_segments[index],
            corrected_segments[index + 1],
            audio,
            active_config,
            index,
        )
        if correction:
            corrections.append(correction)
    return corrected_segments, corrections


def process_word_boundaries_at_segment_ends(
    segments: list[dict[str, Any]],
    audio: np.ndarray,
    config: BoundaryCorrectionConfig | None = None,
    words_to_check: int = 3,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    active_config = config or BoundaryCorrectionConfig()
    corrected_segments = deepcopy(segments)
    corrections: list[dict[str, Any]] = []

    for index in range(len(corrected_segments) - 1):
        current_segment = corrected_segments[index]
        next_segment = corrected_segments[index + 1]
        gap = _as_float(next_segment.get("start")) - _as_float(current_segment.get("end"))
        if gap >= active_config.min_gap_for_check:
            continue
        if not current_segment.get("words") or not next_segment.get("words"):
            continue

        first_word_of_next_segment = next_segment["words"][0]
        for word in current_segment["words"][-words_to_check:]:
            correction = correct_word_boundary(word, first_word_of_next_segment, audio, active_config, -1)
            if correction:
                corrections.append(correction)

    all_words: list[dict[str, Any]] = []
    for segment in corrected_segments:
        all_words.extend(segment.get("words") or [])

    for index in range(len(all_words) - 1):
        current_word = all_words[index]
        next_word = all_words[index + 1]
        if _as_float(current_word.get("end")) > _as_float(next_word.get("start")):
            current_word["end"] = _as_float(next_word.get("start"))

    return all_words, corrections


def segments_to_srt_content(segments: list[dict[str, Any]]) -> str:
    subtitle_segments: list[SubtitleSegment] = []
    for index, segment in enumerate(segments, start=1):
        start_ms = max(0, int(round(_as_float(segment.get("start")) * 1000)))
        end_ms = max(start_ms + 1, int(round(_as_float(segment.get("end")) * 1000)))
        subtitle_segments.append(
            SubtitleSegment(
                index=index,
                start_ms=start_ms,
                end_ms=end_ms,
                text=str(segment.get("text") or "").strip(),
            )
        )
    return compose_srt(subtitle_segments)


def correct_boundaries_from_json_file(
    json_path: str | os.PathLike[str],
    audio_path: str | os.PathLike[str],
    *,
    output_srt_path: str | os.PathLike[str] | None = None,
    corrections_path: str | os.PathLike[str] | None = None,
    config: BoundaryCorrectionConfig | None = None,
    audio_loader: AudioLoader = load_audio,
) -> BoundaryCorrectionResult:
    active_config = config or BoundaryCorrectionConfig()
    json_file = Path(json_path)
    with json_file.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    segments = payload.get("segments") or []
    if not isinstance(segments, list):
        raise ValueError("WhisperX JSON does not contain a valid 'segments' list.")

    audio = audio_loader(audio_path, active_config.sample_rate)
    corrected_segments, corrections = process_whisperx_segments(segments, audio, active_config)

    srt_file = Path(output_srt_path or json_file.with_name(f"{json_file.stem}_corrected.srt"))
    srt_file.write_text(segments_to_srt_content(corrected_segments), encoding="utf-8")

    corrections_file = Path(corrections_path or json_file.with_name(f"{json_file.stem}_boundary_corrections.json"))
    corrections_payload = {
        "segments": corrected_segments,
        "corrections": _json_safe(corrections),
    }
    corrections_file.write_text(json.dumps(corrections_payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return BoundaryCorrectionResult(
        srt_path=str(srt_file),
        corrections_path=str(corrections_file),
        corrections=_json_safe(corrections),
    )


def extract_words_from_json_file(
    json_path: str | os.PathLike[str],
    audio_path: str | os.PathLike[str],
    *,
    config: BoundaryCorrectionConfig | None = None,
    audio_loader: AudioLoader = load_audio,
) -> list[dict[str, Any]]:
    active_config = config or BoundaryCorrectionConfig()
    with Path(json_path).open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    segments = payload.get("segments") or []
    audio = audio_loader(audio_path, active_config.sample_rate)
    words, _corrections = process_word_boundaries_at_segment_ends(segments, audio, active_config)
    return [
        {
            "word": word.get("word"),
            "start": word.get("start"),
            "end": word.get("end"),
        }
        for word in words
    ]
