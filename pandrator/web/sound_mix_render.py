"""Bounded-memory PCM16 rendering for frame-exact sound-mix plans."""

from __future__ import annotations

import math
import os
import stat
import threading
import wave
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
)
from .media_process import MediaProcessCancelled

MAX_RENDER_BLOCK_FRAMES = 64 * 1024
PCM_SAMPLE_WIDTH_BYTES = 2


def normalize_mix_audio(
    source: Path,
    destination: Path,
    *,
    rate: int,
    channels: int,
    cancel_event: threading.Event,
) -> int:
    """Normalize one sound source through the existing streaming assembler."""

    source = Path(source)
    destination = Path(destination)
    _valid_target_rate(rate, channels)
    if cancel_event.is_set():
        raise MediaProcessCancelled("Sound-mix audio normalization was canceled.")
    if os.path.lexists(destination):
        raise FileExistsError(f"Normalization output already exists: {destination.name}")
    if not _is_regular_non_symlink(source):
        raise ValueError("Normalization source must be a regular, non-symlink file.")

    assembly_plan = build_audio_assembly_plan(
        [AudioAssemblyPart(path=source, expected_duration_ms=0)],
        output_format="wav",
        sample_rate_hz=rate,
        channels=channels,
    )
    result = assemble_audio_plan(
        assembly_plan,
        destination,
        cancel_event=cancel_event,
    )
    frames = _wav_info(
        destination,
        label="normalized audio",
        expected_rate=rate,
        expected_channels=channels,
        expected_frames=result.total_frames,
        require_nonempty=True,
        cancel_event=cancel_event,
    )
    return frames


def render_sound_mix(
    base_path: Path,
    clip_paths: dict[str, Path],
    plan: dict[str, Any],
    output_path: Path,
    *,
    cancel_event: threading.Event,
    progress: Callable[[float, str], None] | None = None,
) -> dict[str, Any]:
    """Render a PCM16 WAV using fixed-size blocks and active clip readers."""

    base_path = Path(base_path)
    output_path = Path(output_path)
    mix = _validate_plan(plan)
    if not isinstance(clip_paths, dict):
        raise ValueError("clip_paths must be a dictionary keyed by cue_id.")
    if any(not isinstance(key, str) for key in clip_paths):
        raise ValueError("clip_paths keys must be cue_id strings.")
    expected_ids = {clip["cue_id"] for clip in mix["clips"]}
    if set(clip_paths) != expected_ids:
        raise ValueError("clip_paths must contain exactly one path for every planned clip.")
    if cancel_event.is_set():
        raise MediaProcessCancelled("Sound-mix rendering was canceled.")

    _wav_info(
        base_path,
        label="base audio",
        expected_rate=mix["rate"],
        expected_channels=mix["channels"],
        expected_frames=mix["base_frames"],
        require_nonempty=True,
        cancel_event=cancel_event,
    )
    clip_paths = {cue_id: Path(path) for cue_id, path in clip_paths.items()}
    for clip in mix["clips"]:
        _wav_info(
            clip_paths[clip["cue_id"]],
            label=f"clip {clip['cue_id']}",
            expected_rate=mix["rate"],
            expected_channels=mix["channels"],
            expected_frames=clip["source_frames"],
            require_nonempty=True,
            cancel_event=cancel_event,
        )

    if os.path.lexists(output_path):
        raise FileExistsError(f"Sound-mix output already exists: {output_path.name}")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    bytes_per_frame = mix["channels"] * PCM_SAMPLE_WIDTH_BYTES
    pieces = mix["pieces"]
    clips = sorted(mix["clips"], key=lambda item: (item["start_frame"], item["_order"]))
    active_readers: dict[str, wave.Wave_read] = {}
    output_identity: tuple[int, int] | None = None
    clipped_samples = 0
    succeeded = False

    try:
        with wave.open(str(base_path), "rb") as base_reader:
            with output_path.open("xb") as output_stream:
                created = os.fstat(output_stream.fileno())
                output_identity = (created.st_dev, created.st_ino)
                writer = wave.open(output_stream, "wb")
                try:
                    writer.setnchannels(mix["channels"])
                    writer.setsampwidth(PCM_SAMPLE_WIDTH_BYTES)
                    writer.setframerate(mix["rate"])
                    piece_index = 0
                    next_clip_index = 0
                    active_clips: dict[str, dict[str, Any]] = {}
                    output_start = 0
                    while output_start < mix["total_frames"]:
                        if cancel_event.is_set():
                            raise MediaProcessCancelled("Sound-mix rendering was canceled.")
                        output_end = min(
                            mix["total_frames"], output_start + MAX_RENDER_BLOCK_FRAMES
                        )
                        block_frames = output_end - output_start
                        samples = np.zeros((block_frames, mix["channels"]), dtype=np.float64)

                        while (
                            piece_index < len(pieces)
                            and pieces[piece_index]["output_end_frame"] <= output_start
                        ):
                            piece_index += 1
                        current_piece = piece_index
                        while (
                            current_piece < len(pieces)
                            and pieces[current_piece]["output_start_frame"] < output_end
                        ):
                            piece = pieces[current_piece]
                            overlap_start = max(output_start, piece["output_start_frame"])
                            overlap_end = min(output_end, piece["output_end_frame"])
                            source_start = piece["source_start_frame"]
                            source_end = piece["source_end_frame"]
                            if source_start is not None and source_end is not None:
                                source_frame = source_start + (
                                    overlap_start - piece["output_start_frame"]
                                )
                                count = overlap_end - overlap_start
                                base_reader.setpos(source_frame)
                                raw = base_reader.readframes(count)
                                _require_pcm_bytes(raw, count, bytes_per_frame, "base audio")
                                source_samples = np.frombuffer(raw, dtype="<i2").reshape(
                                    count, mix["channels"]
                                )
                                target_start = overlap_start - output_start
                                target_end = target_start + count
                                samples[target_start:target_end] += source_samples
                            current_piece += 1

                        while (
                            next_clip_index < len(clips)
                            and clips[next_clip_index]["start_frame"] < output_end
                        ):
                            clip = clips[next_clip_index]
                            active_clips[clip["cue_id"]] = clip
                            next_clip_index += 1
                        for cue_id in tuple(active_readers):
                            clip = active_clips.get(cue_id)
                            if clip is None or clip["end_frame"] <= output_start:
                                active_readers.pop(cue_id).close()
                                active_clips.pop(cue_id, None)

                        for cue_id, clip in active_clips.items():
                            overlap_start = max(output_start, clip["start_frame"])
                            overlap_end = min(output_end, clip["end_frame"])
                            if overlap_start >= overlap_end:
                                continue
                            reader = active_readers.get(cue_id)
                            if reader is None:
                                reader = wave.open(str(clip_paths[cue_id]), "rb")
                                active_readers[cue_id] = reader
                            count = overlap_end - overlap_start
                            clip_source_start = clip["trim_start_frame"] + (
                                overlap_start - clip["start_frame"]
                            )
                            reader.setpos(clip_source_start)
                            raw = reader.readframes(count)
                            _require_pcm_bytes(raw, count, bytes_per_frame, f"clip {cue_id}")
                            source_samples = (
                                np.frombuffer(raw, dtype="<i2")
                                .reshape(count, mix["channels"])
                                .astype(np.float64)
                            )
                            local_start = clip_source_start - clip["trim_start_frame"]
                            local_frames = np.arange(local_start, local_start + count)
                            envelope = _fade_envelope(local_frames, clip)
                            envelope *= 10.0 ** (clip["gain_db"] / 20.0)
                            target_start = overlap_start - output_start
                            target_end = target_start + count
                            samples[target_start:target_end] += (
                                source_samples * envelope[:, np.newaxis]
                            )

                        clipped_samples += int(
                            np.count_nonzero((samples < -32768) | (samples > 32767))
                        )
                        pcm = np.rint(np.clip(samples, -32768, 32767)).astype("<i2")
                        writer.writeframesraw(pcm.tobytes(order="C"))
                        output_start = output_end
                        if progress is not None:
                            progress(
                                output_end / mix["total_frames"],
                                f"Rendered {output_end} of {mix['total_frames']} sound-mix frames",
                            )

                    if cancel_event.is_set():
                        raise MediaProcessCancelled("Sound-mix rendering was canceled.")
                finally:
                    try:
                        writer.close()
                    finally:
                        for reader in active_readers.values():
                            reader.close()
                        active_readers.clear()
        _wav_info(
            output_path,
            label="sound-mix output",
            expected_rate=mix["rate"],
            expected_channels=mix["channels"],
            expected_frames=mix["total_frames"],
            require_nonempty=True,
            cancel_event=cancel_event,
        )
        succeeded = True
    except BaseException:
        if output_identity is not None and not succeeded:
            try:
                current = output_path.lstat()
            except FileNotFoundError:
                current = None
            if (
                current is not None
                and stat.S_ISREG(current.st_mode)
                and (current.st_dev, current.st_ino) == output_identity
            ):
                output_path.unlink(missing_ok=True)
        raise

    return {
        "frames": mix["total_frames"],
        "sample_rate": mix["rate"],
        "channels": mix["channels"],
        "duration_seconds": mix["total_frames"] / mix["rate"],
        "clipped_samples": clipped_samples,
        "mix_signature": mix["mix_signature"],
    }


def _valid_target_rate(rate: int, channels: int) -> None:
    if type(rate) is not int or not 8000 <= rate <= 192000:
        raise ValueError("rate must be an integer between 8000 and 192000 Hz.")
    if type(channels) is not int or channels not in {1, 2}:
        raise ValueError("channels must be one or two.")


def _is_regular_non_symlink(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    return stat.S_ISREG(info.st_mode)


def _wav_info(
    path: Path,
    *,
    label: str,
    expected_rate: int,
    expected_channels: int,
    expected_frames: int,
    require_nonempty: bool,
    cancel_event: threading.Event | None = None,
) -> int:
    if not _is_regular_non_symlink(path):
        raise ValueError(f"{label} must be a regular, non-symlink WAV file.")
    try:
        with wave.open(str(path), "rb") as reader:
            if reader.getcomptype() != "NONE" or reader.getsampwidth() != PCM_SAMPLE_WIDTH_BYTES:
                raise ValueError(f"{label} must contain uncompressed PCM16 samples.")
            if reader.getframerate() != expected_rate or reader.getnchannels() != expected_channels:
                raise ValueError(f"{label} rate or channel count does not match the mix plan.")
            frames = reader.getnframes()
            if require_nonempty and frames <= 0:
                raise ValueError(f"{label} must not be empty.")
            if frames != expected_frames:
                raise ValueError(f"{label} frame count does not match the mix plan.")
            remaining = frames
            bytes_per_frame = expected_channels * PCM_SAMPLE_WIDTH_BYTES
            while remaining:
                if cancel_event is not None and cancel_event.is_set():
                    raise MediaProcessCancelled("Sound-mix WAV validation was canceled.")
                count = min(MAX_RENDER_BLOCK_FRAMES, remaining)
                raw = reader.readframes(count)
                _require_pcm_bytes(raw, count, bytes_per_frame, label)
                remaining -= count
    except (EOFError, OSError, wave.Error) as error:
        raise ValueError(f"{label} is not a valid PCM16 WAV file.") from error
    return frames


def _require_pcm_bytes(raw: bytes, frames: int, bytes_per_frame: int, label: str) -> None:
    if len(raw) != frames * bytes_per_frame:
        raise ValueError(f"{label} ended before its declared PCM frame count.")


def _fade_envelope(local_frames: np.ndarray, clip: dict[str, Any]) -> np.ndarray:
    duration = clip["end_frame"] - clip["start_frame"]
    envelope = np.ones(local_frames.shape, dtype=np.float64)
    fade_in = clip["fade_in_frames"]
    if fade_in:
        active = local_frames < fade_in
        if fade_in == 1:
            envelope[active] = 0.0
        else:
            envelope[active] *= local_frames[active] / (fade_in - 1)
    fade_out = clip["fade_out_frames"]
    if fade_out:
        remaining = duration - 1 - local_frames
        active = remaining < fade_out
        if fade_out == 1:
            envelope[active] = 0.0
        else:
            envelope[active] *= remaining[active] / (fade_out - 1)
    return envelope


def _validate_plan(plan: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        raise ValueError("plan must be an object.")
    total_frames = _plan_integer(plan, "total_frames", minimum=1)
    base_frames = _plan_integer(plan, "base_frames", minimum=1)
    rate = _plan_integer(plan, "rate", minimum=8000)
    if rate > 192000:
        raise ValueError("plan.rate must not exceed 192000 Hz.")
    channels = plan.get("channels")
    if type(channels) is not int or channels not in {1, 2}:
        raise ValueError("plan.channels must be one or two.")
    signature = plan.get("mix_signature")
    if not isinstance(signature, str) or not signature:
        raise ValueError("plan.mix_signature must be a non-empty string.")
    insertions = plan.get("insertions")
    if not isinstance(insertions, list):
        raise ValueError("plan.insertions must be a list.")
    inserted_frames = 0
    for item in insertions:
        if not isinstance(item, dict):
            raise ValueError("plan.insertions entries must be objects.")
        anchor = _plan_integer(item, "base_frame")
        added = _plan_integer(item, "added_frames", minimum=1)
        if anchor > base_frames:
            raise ValueError("plan insertion is outside the base audio.")
        inserted_frames += added
    if total_frames != base_frames + inserted_frames:
        raise ValueError("plan.total_frames does not match its insertions.")
    fixed_timeline = plan.get("fixed_timeline", False)
    if type(fixed_timeline) is not bool:
        raise ValueError("plan.fixed_timeline must be a boolean.")
    if fixed_timeline and (insertions or total_frames != base_frames):
        raise ValueError("A fixed-timeline plan cannot contain insertions.")

    pieces = plan.get("pieces")
    if not isinstance(pieces, list) or not pieces:
        raise ValueError("plan.pieces must cover the output timeline.")
    piece_cursor = 0
    source_cursor = 0
    for piece in pieces:
        if not isinstance(piece, dict):
            raise ValueError("plan.pieces entries must be objects.")
        start = _plan_integer(piece, "output_start_frame")
        end = _plan_integer(piece, "output_end_frame", minimum=1)
        if start != piece_cursor or end <= start or end > total_frames:
            raise ValueError("plan.pieces must be contiguous and within total_frames.")
        source_start = piece.get("source_start_frame")
        source_end = piece.get("source_end_frame")
        if source_start is None or source_end is None:
            if source_start is not None or source_end is not None:
                raise ValueError("plan piece source coordinates must both be set or both be null.")
        else:
            source_start = _strict_integer(source_start, "piece.source_start_frame")
            source_end = _strict_integer(source_end, "piece.source_end_frame")
            if source_start != source_cursor or source_end - source_start != end - start:
                raise ValueError("plan speech pieces must preserve ordered base frames.")
            if source_end > base_frames:
                raise ValueError("plan speech piece is outside the base audio.")
            source_cursor = source_end
        piece_cursor = end
    if piece_cursor != total_frames or source_cursor != base_frames:
        raise ValueError("plan.pieces must cover all output and base speech frames.")

    raw_clips = plan.get("clips")
    if not isinstance(raw_clips, list):
        raise ValueError("plan.clips must be a list.")
    clips: list[dict[str, Any]] = []
    cue_ids: set[str] = set()
    for index, raw_clip in enumerate(raw_clips):
        if not isinstance(raw_clip, dict):
            raise ValueError("plan.clips entries must be objects.")
        cue_id = raw_clip.get("cue_id")
        if not isinstance(cue_id, str) or not cue_id or cue_id in cue_ids:
            raise ValueError("plan clip cue_id values must be unique non-empty strings.")
        cue_ids.add(cue_id)
        start = _strict_integer(raw_clip.get("start_frame"), f"clips[{index}].start_frame")
        end = _strict_integer(raw_clip.get("end_frame"), f"clips[{index}].end_frame")
        trim_start = _strict_integer(
            raw_clip.get("trim_start_frame"), f"clips[{index}].trim_start_frame"
        )
        trim_end = _strict_integer(raw_clip.get("trim_end_frame"), f"clips[{index}].trim_end_frame")
        if start >= end or end > total_frames or trim_start >= trim_end:
            raise ValueError("plan clip positions and trims must describe non-empty ranges.")
        if end - start != trim_end - trim_start:
            raise ValueError("plan clip duration does not match its trimmed source range.")
        gain_db = raw_clip.get("gain_db")
        if (
            isinstance(gain_db, bool)
            or not isinstance(gain_db, (int, float))
            or not math.isfinite(float(gain_db))
            or not -60 <= gain_db <= 12
        ):
            raise ValueError("plan clip gain_db must be finite and between -60 and 12.")
        fade_in = _strict_integer(raw_clip.get("fade_in_frames"), f"clips[{index}].fade_in_frames")
        fade_out = _strict_integer(
            raw_clip.get("fade_out_frames"), f"clips[{index}].fade_out_frames"
        )
        if fade_in > end - start or fade_out > end - start:
            raise ValueError("plan clip fades exceed their trimmed duration.")
        mode = raw_clip.get("mode")
        if not isinstance(mode, str) or mode not in {"insert", "overlay"}:
            raise ValueError("plan clip mode is invalid.")
        clips.append(
            {
                "cue_id": cue_id,
                "variant_id": raw_clip.get("variant_id"),
                "content_hash": raw_clip.get("content_hash"),
                "start_frame": start,
                "end_frame": end,
                "trim_start_frame": trim_start,
                "trim_end_frame": trim_end,
                "source_frames": _strict_integer(
                    raw_clip.get("source_frames"), f"clips[{index}].source_frames"
                ),
                "gain_db": float(gain_db),
                "fade_in_frames": fade_in,
                "fade_out_frames": fade_out,
                "mode": mode,
                "_order": index,
            }
        )
        if clips[-1]["trim_end_frame"] > clips[-1]["source_frames"]:
            raise ValueError("plan clip trim exceeds source_frames.")
        if mode == "overlay" and fixed_timeline and end > base_frames:
            raise ValueError("A fixed-timeline overlay exceeds the base audio duration.")
    return {
        "total_frames": total_frames,
        "base_frames": base_frames,
        "rate": rate,
        "channels": channels,
        "mix_signature": signature,
        "pieces": pieces,
        "clips": clips,
    }


def _strict_integer(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer.")
    return value


def _plan_integer(plan: dict[str, Any], name: str, *, minimum: int = 0) -> int:
    value = plan.get(name)
    if type(value) is not int or value < minimum:
        raise ValueError(f"plan.{name} must be an integer greater than or equal to {minimum}.")
    return value
