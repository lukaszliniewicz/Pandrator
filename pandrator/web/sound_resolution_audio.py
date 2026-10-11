"""Exact take audio and conservative word-boundary measurements."""

from __future__ import annotations

import threading
import wave
from pathlib import Path
from typing import Any

import numpy as np

from pandrator.logic.audio_cpp_execution import native_audio_cpp_guard
from pandrator.logic.book_timing import AlignedBookText, align_book_text
from pandrator.logic.dubbing import crispasr
from pandrator.logic.dubbing.caption_alignment import parse_vad_export
from pandrator.logic.dubbing.qwen_alignment import alignment_key
from pandrator.logic.sound_boundary_refinement import locate_sound_anchor, refine_sound_boundary

from .audio_assembly import AudioAssemblyPart, AudioEncodingSettings, _normalize_part
from .sound_resolution_snapshot import REFINEMENT_SETTINGS, WorkItem


def _wav_window(path: Path, estimated_frame: int) -> tuple[int, np.ndarray, int, int]:
    with wave.open(str(path), "rb") as source:
        if (
            source.getnchannels(),
            source.getsampwidth(),
            source.getframerate(),
            source.getcomptype(),
        ) != (1, 2, 16000, "NONE"):
            raise ValueError("Sound resolution requires mono PCM16 at 16 kHz.")
        rate = source.getframerate()
        total_frames = source.getnframes()
        if not 0 <= estimated_frame <= total_frames:
            raise ValueError("Aligned word end lies outside the selected take.")
        first = max(0, estimated_frame - round(0.3 * rate))
        last = min(total_frames, estimated_frame + round(2.5 * rate))
        if last - first > 6 * rate:
            raise ValueError("Sound analysis crop exceeds six seconds.")
        source.setpos(first)
        data = source.readframes(last - first)
        if len(data) != 2 * (last - first):
            raise ValueError("Sound analysis crop is incomplete.")
    samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
    return first, samples, rate, total_frames


def _original_rate(path: Path) -> tuple[int, int]:
    with wave.open(str(path), "rb") as source:
        if (
            source.getcomptype() != "NONE"
            or source.getnframes() <= 0
            or not 8000 <= source.getframerate() <= 192000
        ):
            raise ValueError("The selected take must be a current PCM WAV.")
        return source.getframerate(), source.getnframes()


def normalize_take(
    item: WorkItem, destination: Path, settings: dict[str, Any], cancel_event: threading.Event
) -> None:
    assert item.path is not None
    _normalize_part(
        AudioAssemblyPart(item.path, 0),
        destination,
        AudioEncodingSettings(sample_rate_hz=16000, channels=1),
        ffmpeg_executable=str(settings.get("ffmpeg_executable") or "ffmpeg"),
        cancel_event=cancel_event,
    )


def align_take(
    item: WorkItem, settings: dict[str, Any], workdir: Path, cancel_event: threading.Event
) -> AlignedBookText:
    assert item.path is not None and item.transcript is not None and item.identity is not None
    runtime = {**settings, "original_language": item.language} if item.language else settings
    if item.identity["engine"] == "crispasr":
        executable = crispasr.resolve_executable(str(runtime.get("crispasr_executable") or ""))
        backend = str(runtime.get("stt_compute_backend") or "auto")
        with native_audio_cpp_guard([executable, "--backend", backend], cancel_event):
            return align_book_text(item.path, item.transcript, runtime, workdir, cancel_event)
    # Qwen's native invocation owns the reentrant audio.cpp guard.
    return align_book_text(item.path, item.transcript, runtime, workdir, cancel_event)


def resolve_boundary(
    item: WorkItem,
    aligned: AlignedBookText,
    normalized: Path,
    workdir: Path,
    settings: dict[str, Any],
    cancel_event: threading.Event,
) -> dict[str, Any]:
    assert item.transcript is not None and item.path is not None
    anchor = item.anchor
    try:
        _, quote_end = locate_sound_anchor(
            item.transcript,
            anchor["quote"],
            anchor["occurrence"],
            start=anchor.get("start"),
            end=anchor.get("end"),
        )
    except (KeyError, TypeError, ValueError):
        return {"status": "needs_placement", "reason": "spoken_quote_mismatch"}
    words = aligned.words
    owners = [
        index for index, word in enumerate(words) if word.start_char < quote_end <= word.end_char
    ]
    if len(owners) != 1 or alignment_key(item.transcript[quote_end : words[owners[0]].end_char]):
        return {"status": "needs_placement", "reason": "quote_word_boundary_ambiguous"}
    owner = owners[0]
    original_rate, original_frames = _original_rate(item.path)
    if anchor["kind"] == "after_word" and item.playback.get("mode") == "overlay":
        take_frame = min(original_frames, (words[owner].end_ms * original_rate + 500) // 1000)
        return {
            "status": "ready",
            "take_frame": take_frame,
            "take_sample_rate": original_rate,
            "evidence": {"alignment_only_overlay": True},
            "reason": "alignment_only_overlay",
        }
    rate = 16000
    estimate = round(words[owner].end_ms * rate / 1000)
    next_start = round(words[owner + 1].start_ms * rate / 1000) if owner + 1 < len(words) else None
    first, crop, rate, total_frames = _wav_window(normalized, estimate)
    if next_start is None:
        next_start = total_frames
    if crop.size == 0:
        return {"status": "needs_placement", "reason": "empty_analysis_crop"}
    crop_path = workdir / f"crop-{item.cue_id}.wav"
    with wave.open(str(crop_path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(rate)
        output.writeframes(np.rint(np.clip(crop, -1, 1) * 32767).astype("<i2").tobytes())
    vad_settings = {
        **settings,
        "stt_compute_backend": "cpu",
        "crispasr_vad_threshold": 0.5,
        "crispasr_vad_min_speech_ms": 40,
        "crispasr_vad_min_silence_ms": 80,
        "crispasr_vad_speech_pad_ms": 30,
    }
    executable = crispasr.resolve_executable(str(vad_settings.get("crispasr_executable") or ""))
    vad_path = workdir / f"vad-{item.cue_id}.json"
    with native_audio_cpp_guard([executable, "--backend", "cpu"], cancel_event):
        result_path = crispasr.run_vad_export(
            crop_path, vad_path, vad_settings, executable=executable, cancel_event=cancel_event
        )
    spans = parse_vad_export(result_path, duration_ms=round(len(crop) * 1000 / rate))
    speech = [
        (
            max(0, round(span.start_ms * rate / 1000)),
            min(len(crop), round(span.end_ms * rate / 1000)),
        )
        for span in spans
    ]
    speech = [(start, end) for start, end in speech if start < end]
    lexical = [
        (
            max(0, round(word.start_ms * rate / 1000) - first),
            min(len(crop), round(word.end_ms * rate / 1000) - first),
        )
        for word in words
    ]
    lexical = [(start, end) for start, end in lexical if start < end]
    owner_start = max(0, round(words[owner].start_ms * rate / 1000) - first)
    owner_end = min(len(crop), estimate - first)
    reliable_vad = any(start < owner_end and end > owner_start for start, end in speech)
    refined = refine_sound_boundary(
        crop,
        sample_rate=rate,
        estimated_end_frame=estimate - first,
        next_word_start_frame=max(0, min(len(crop), next_start - first)),
        word_intervals=lexical,
        speech_intervals=speech if reliable_vad else None,
        kind=anchor["kind"],
        quiet_dbfs=REFINEMENT_SETTINGS["quiet_dbfs"],
        min_quiet_ms=REFINEMENT_SETTINGS["min_quiet_ms"],
        guard_ms=REFINEMENT_SETTINGS["guard_ms"],
        max_search_ms=REFINEMENT_SETTINGS["max_search_ms"],
    )
    if refined["status"] != "ready":
        return {
            "status": "needs_placement",
            "reason": refined["reason"],
            "evidence": refined.get("evidence", {}),
        }
    chosen = first + int(refined["chosen_frame"])
    take_frame = min(original_frames, (chosen * original_rate + rate // 2) // rate)
    result: dict[str, Any] = {
        "status": "ready",
        "take_frame": take_frame,
        "take_sample_rate": original_rate,
        "evidence": refined.get("evidence", {}),
        "reason": refined.get("reason"),
    }
    for key in ("gap_start_frame", "gap_end_frame", "shift_frames"):
        if key in refined:
            result[key] = int(refined[key])
    return result
