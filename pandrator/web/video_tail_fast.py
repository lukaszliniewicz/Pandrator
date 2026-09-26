"""Guarded production FFmpeg frozen-video-tail STREAM COPY fast path.

The caller (export integration) handles source audio separately. This module
produces a *video-only* MP4 whose body is the original encoded video stream
(bit-for-bit stream copy) followed by a short frozen-frame tail encoded from
the actual last displayed frame.

Strategy (mirrors the validated prototype)::

    body.mp4  = stream copy of ``0:v:0`` with matched ``-video_track_timescale``
    last.yuv  = actual last displayed frame via bounded absolute-seek decode
    tail.mp4  = short still clip (libx264, ``bf=0``) matched to source metadata
    output    = ffconcat ``body + tail`` with ``-c:v copy``

``try_fast_video_tail`` returns True only when a safe, verified video-only
extended MP4 exists at ``destination``. It returns False for unsupported
sources or failed fast attempts so the caller falls back to the existing full
encode. Cancellation (a set ``cancel_event`` or ``InterruptedError`` from the
supplied runner) always propagates and never degrades into ``False``.

All FFmpeg invocations go through the caller-supplied cancellable
``run_command``. FFprobe reads are header/packet-window bounded with timeouts
and cancel checks, and use an ffprobe binary matching the configured ffmpeg
(sibling binary, ``PANDRATOR_FFPROBE_EXE``, then ``PATH``).

Only H.264 SDR ``yuv420p`` baseline/main/high sources with constant,
known frame rate, ``1:1`` SAR, no rotation/HDR and (near-)zero start time are
eligible. Everything else conservatively returns False.
"""

from __future__ import annotations

import json
import logging
import math
import os
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)

_FF_PREFIX = ["-nostdin", "-hide_banner", "-v", "warning", "-y"]

# Short still clips only; anything longer falls back to the full encode.
MAX_TAIL_SECONDS = 30.0
# Bounded near-end decode window for last-frame extraction / seam checks.
_MIN_SEEK_WINDOW = 1.0
_SEEK_FRAMES = 10
_MAX_SEEK_WINDOW = 5.0
_PROBE_TIMEOUT = 25
_PACKET_WINDOW_BEFORE = 1.0
_PACKET_WINDOW_AFTER_PAD = 0.5
_STRICT_WINDOW_BEFORE = 0.5
_STRICT_WINDOW_AFTER_PAD = 0.3
_FROZEN_WINDOW_SECONDS = 1.0

_ALLOWED_PROFILES = {
    "baseline": "baseline",
    "constrained baseline": "baseline",
    "main": "main",
    "high": "high",
}
_ALLOWED_COLOR_TRANSFERS = {
    None,
    "unknown",
    "bt709",
    "bt470m",
    "bt470bg",
    "smpte170m",
    "smpte240m",
    "bt1361e",
    "iec61966-2-1",
    "iec61966-2-4",
}
_HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}


class _Ineligible(Exception):
    """Raised when the source is valid media but not fast-path eligible."""


def _check_cancelled(cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Video tail fast path canceled.")


def _ffprobe_for_ffmpeg(ffmpeg_executable: str) -> str:
    """Resolve the ffprobe binary matching the configured ffmpeg."""
    directory = os.path.dirname(os.path.abspath(ffmpeg_executable))
    if directory and os.path.basename(ffmpeg_executable).startswith("ffmpeg"):
        sibling = os.path.join(directory, "ffprobe")
        windows_sibling = sibling + ".exe"
        for candidate in (sibling, windows_sibling):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return str(os.environ.get("PANDRATOR_FFPROBE_EXE") or shutil.which("ffprobe") or "ffprobe")


def _parse_ratio(raw: object) -> tuple[int, int] | None:
    """Parse an FFmpeg rational like ``"30000/1001"`` into (num, den)."""
    text = str(raw or "").strip()
    if "/" not in text:
        return None
    numerator, _, denominator = text.partition("/")
    try:
        num, den = int(numerator), int(denominator)
    except (TypeError, ValueError):
        return None
    if num <= 0 or den <= 0:
        return None
    return num, den


def _ratio_value(ratio: tuple[int, int] | None) -> float | None:
    if ratio is None:
        return None
    value = ratio[0] / ratio[1]
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _parse_float(raw: object) -> float | None:
    try:
        value = float(str(raw))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value):
        return None
    return value


def _level_name(raw: object) -> str | None:
    """Normalize an ffprobe H.264 level (``31`` or ``"3.1"``) to ``"3.1"``."""
    text = str(raw or "").strip()
    if not text or text.lower() == "unknown":
        return None
    if "." in text:
        try:
            major, _, minor = text.partition(".")
            return f"{int(major)}.{int(minor)}"
        except (TypeError, ValueError):
            return None
    try:
        level = int(text)
    except (TypeError, ValueError):
        return None
    if level < 10 or level > 62:
        return None
    return f"{level // 10}.{level % 10}"


def _run_probe(ffprobe: str, args: list[str], cancel_event: threading.Event) -> dict:
    """Run a bounded ffprobe read and return parsed JSON."""
    _check_cancelled(cancel_event)
    try:
        completed = subprocess.run(
            [ffprobe, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT,
        )
    except subprocess.TimeoutExpired as error:
        raise _Ineligible(f"ffprobe read timed out after {_PROBE_TIMEOUT}s.") from error
    except OSError as error:
        raise _Ineligible(f"could not start ffprobe: {error}") from error
    _check_cancelled(cancel_event)
    if completed.returncode != 0:
        detail = (completed.stderr or "").strip().splitlines()
        raise _Ineligible(
            "ffprobe read failed: " + (detail[-1][-200:] if detail else "non-zero exit status.")
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise _Ineligible("ffprobe returned invalid JSON.") from error
    if not isinstance(payload, dict):
        raise _Ineligible("ffprobe returned an unexpected payload.")
    return payload


def _probe_streams(ffprobe: str, source: Path, cancel_event: threading.Event) -> tuple[dict, dict]:
    payload = _run_probe(
        ffprobe,
        [
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(source),
        ],
        cancel_event,
    )
    streams = payload.get("streams") or []
    if not isinstance(streams, list):
        raise _Ineligible("ffprobe returned malformed streams.")
    video = [
        item for item in streams if isinstance(item, dict) and item.get("codec_type") == "video"
    ]
    if not video:
        raise _Ineligible("no video stream found.")
    if len(video) > 1:
        raise _Ineligible("multiple video streams are not supported.")
    stream = video[0]
    fmt = payload.get("format")
    return stream, fmt if isinstance(fmt, dict) else {}


def _rotation_degrees(stream: dict) -> float:
    tags = stream.get("tags")
    if isinstance(tags, dict):
        for key in ("rotate", "rotation"):
            value = _parse_float(tags.get(key))
            if value is not None:
                return value
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("side_data_type") or "").lower()
            if "rotation" in kind or "displaymatrix" in kind:
                value = _parse_float(entry.get("rotation"))
                return value if value is not None else 90.0
    return 0.0


def _has_hdr_signals(stream: dict) -> bool:
    transfer = str(stream.get("color_transfer") or "unknown").lower()
    primaries = str(stream.get("color_primaries") or "unknown").lower()
    space = str(stream.get("color_space") or "unknown").lower()
    if transfer in _HDR_TRANSFERS:
        return True
    if primaries == "bt2020" or space in {"bt2020nc", "bt2020c", "bt2020_ncl"}:
        return True
    side_data = stream.get("side_data_list")
    if isinstance(side_data, list):
        for entry in side_data:
            if not isinstance(entry, dict):
                continue
            kind = str(entry.get("side_data_type") or "").lower()
            if any(
                marker in kind
                for marker in (
                    "mastering display",
                    "content light level",
                    "dolby vision",
                    "dovi",
                )
            ):
                return True
    return False


def _eligible_info(stream: dict) -> dict:
    """Validate a source video stream; return normalized info or raise."""
    codec = str(stream.get("codec_name") or "").lower()
    if codec not in {"h264", "avc"}:
        raise _Ineligible(f"unsupported video codec {codec or 'unknown'}.")
    profile = str(stream.get("profile") or "unknown").strip().lower()
    encoder_profile = _ALLOWED_PROFILES.get(profile)
    if encoder_profile is None:
        raise _Ineligible(f"unsupported H.264 profile {profile}.")
    pix_fmt = str(stream.get("pix_fmt") or "unknown").lower()
    if pix_fmt != "yuv420p":
        raise _Ineligible(f"unsupported pixel format {pix_fmt}.")
    try:
        width = int(str(stream.get("width") or "0"))
        height = int(str(stream.get("height") or "0"))
    except (TypeError, ValueError) as error:
        raise _Ineligible("video dimensions are unknown.") from error
    if width <= 0 or height <= 0 or width % 2 or height % 2:
        raise _Ineligible(f"unsupported video dimensions {width}x{height}.")
    sar = str(stream.get("sample_aspect_ratio") or "1:1")
    if sar not in {"1:1"}:
        raise _Ineligible(f"unsupported sample aspect ratio {sar}.")
    rotation = _rotation_degrees(stream)
    if abs(rotation) > 0.01:
        raise _Ineligible(f"rotated video ({rotation} deg) is not supported.")
    if _has_hdr_signals(stream):
        raise _Ineligible("HDR signalling is not supported by the fast path.")
    transfer = str(stream.get("color_transfer") or "unknown").lower()
    if transfer not in _ALLOWED_COLOR_TRANSFERS:
        raise _Ineligible(f"unsupported colour transfer {transfer}.")
    level = _level_name(stream.get("level"))
    if level is None:
        raise _Ineligible("unknown H.264 level; cannot match the tail clip.")
    r_rate = _parse_ratio(stream.get("r_frame_rate"))
    avg_rate = _parse_ratio(stream.get("avg_frame_rate"))
    r_fps = _ratio_value(r_rate)
    avg_fps = _ratio_value(avg_rate)
    if r_rate is None or avg_rate is None or r_fps is None or avg_fps is None:
        raise _Ineligible("variable or unknown frame rate is not supported.")
    if r_fps > 120 or avg_fps > 120:
        raise _Ineligible("implausible frame rate.")
    if abs(r_fps - avg_fps) / max(avg_fps, 1e-9) > 1e-3:
        raise _Ineligible("variable frame rate is not supported.")
    fps = avg_fps
    timebase = _parse_ratio(stream.get("time_base"))
    if timebase is None:
        raise _Ineligible("unknown video time base.")
    timescale_exact = timebase[1] / timebase[0]
    timescale = int(round(timescale_exact))
    if timescale < 1 or timescale > 90000:
        raise _Ineligible("unsupported video time base.")
    if abs(timescale * timebase[0] - timebase[1]) > 0:
        raise _Ineligible("non-integer video timescale is not supported.")
    start = _parse_float(stream.get("start_time", 0.0))
    if start is None:
        start = 0.0
    if abs(start) > 0.5 / fps + 1e-6:
        raise _Ineligible(f"nonzero start time {start}s is not supported.")
    # Video-stream-derived end only: the container/format duration may track a
    # longer audio track and must never steer the near-end seek.
    end = _parse_float(stream.get("duration"))
    if end is None or end <= 0:
        nb_frames = None
        try:
            nb_frames = int(str(stream.get("nb_frames") or "0"))
        except (TypeError, ValueError):
            nb_frames = None
        if nb_frames and nb_frames > 0:
            end = nb_frames / fps
    if end is None or not math.isfinite(end) or end <= 0:
        raise _Ineligible("video stream end could not be determined.")
    nb_frames_known = None
    try:
        candidate = int(str(stream.get("nb_frames") or "0"))
        nb_frames_known = candidate if candidate > 0 else None
    except (TypeError, ValueError):
        nb_frames_known = None
    return {
        "fps": fps,
        "fps_ratio": f"{avg_rate[0]}/{avg_rate[1]}",
        "width": width,
        "height": height,
        "profile": encoder_profile,
        "level": level,
        "timescale": timescale,
        "video_end": end,
        "nb_frames": nb_frames_known,
        "color_space": str(stream.get("color_space") or ""),
        "color_primaries": str(stream.get("color_primaries") or ""),
        "color_transfer": str(stream.get("color_transfer") or ""),
        "color_range": str(stream.get("color_range") or ""),
    }


def _tail_plan(extra_seconds: float, fps: float) -> tuple[int, float]:
    """Cover ``extra_seconds`` with whole frames, without adding a frame.

    The caller passes the shared frozen-tail extension computed by
    ``resolve_video_tail_extension_ms``, which already rounds up to whole
    frames plus one safety frame. Adding another frame here would make the
    video longer than the soundtrack master and the exported metadata, so
    this uses a minimal ceiling. The small epsilon only absorbs binary
    floating-point dust on exact frame multiples (e.g. 0.52s at 25fps is
    exactly 13 frames); any genuine fraction still rounds up.
    """
    if not math.isfinite(extra_seconds) or extra_seconds <= 0:
        raise _Ineligible("tail extension requires a positive duration.")
    if extra_seconds > MAX_TAIL_SECONDS:
        raise _Ineligible("tail extension exceeds the fast-path maximum.")
    frames = max(1, math.ceil(extra_seconds * fps - 1e-6))
    if frames > 9000:
        raise _Ineligible("tail frame count is out of range.")
    return frames, frames / fps


def _seek_window(fps: float) -> float:
    return min(_MAX_SEEK_WINDOW, max(_MIN_SEEK_WINDOW, _SEEK_FRAMES / fps))


def _remove_quietly(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _color_args(info: dict) -> list[str]:
    """Forward known SDR colour metadata to the tail encoder, verbatim."""
    args: list[str] = []
    mapping = (
        ("color_space", "-colorspace"),
        ("color_primaries", "-color_primaries"),
        ("color_transfer", "-color_trc"),
        ("color_range", "-color_range"),
    )
    for key, flag in mapping:
        value = str(info.get(key) or "").strip().lower()
        if value and value != "unknown":
            args.extend([flag, value])
    return args


def _framemd5_rows(path: Path) -> list[tuple[str, str]]:
    """Parse ``framemd5`` output into (pts, hash) pairs, skipping comments.

    Only presentation timestamps and decoded hashes are compared: the
    concat muxer may legitimately extend the final body packet duration to
    bridge the seam, while pixels and timestamps must stay bit-identical.
    """
    rows: list[tuple[str, str]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [item.strip() for item in line.split(",")]
        if len(parts) < 3:
            continue
        rows.append((parts[2], parts[-1]))
    return rows


def try_fast_video_tail(
    source: Path,
    destination: Path,
    *,
    scratch_dir: Path,
    extra_seconds: float,
    ffmpeg_executable: str,
    cancel_event: threading.Event,
    run_command: Callable[[list[str], threading.Event], None],
) -> bool:
    """Extend ``source`` video with a frozen tail via stream copy.

    Returns True only when a verified video-only extended MP4 exists at
    ``destination``. Returns False when the source is unsupported or the fast
    attempt failed, so the caller falls back to the existing full encode.
    Cancellation (``cancel_event`` set or ``InterruptedError``) propagates.
    """
    source = Path(source)
    destination = Path(destination)
    scratch_dir = Path(scratch_dir)
    intermediates: list[Path] = []

    def track(path: Path) -> Path:
        intermediates.append(path)
        return path

    def cleanup() -> None:
        for path in intermediates:
            _remove_quietly(path)

    def fail(reason: str) -> bool:
        logger.warning("Video tail fast path skipped: %s", reason[:200])
        cleanup()
        return False

    try:
        _check_cancelled(cancel_event)
        if not source.is_file():
            return fail(f"source not found: {source.name}")
        if not scratch_dir.is_dir():
            return fail("scratch directory is unavailable.")
        try:
            if destination.resolve() == source.resolve():
                return fail("destination must not overwrite the source.")
        except OSError:
            return fail("destination path could not be resolved.")
        try:
            fps_hint = float(extra_seconds)
        except (TypeError, ValueError):
            return fail("tail extension is not a number.")
        if not math.isfinite(fps_hint) or fps_hint <= 0:
            return fail("tail extension requires a positive duration.")

        ffprobe = _ffprobe_for_ffmpeg(ffmpeg_executable)
        try:
            stream, _fmt = _probe_streams(ffprobe, source, cancel_event)
            info = _eligible_info(stream)
            tail_frames, tail_duration = _tail_plan(fps_hint, info["fps"])
        except _Ineligible as error:
            return fail(str(error))

        fps = info["fps"]
        width = info["width"]
        height = info["height"]
        timescale = info["timescale"]
        video_end = info["video_end"]
        window = _seek_window(fps)

        body_path = track(scratch_dir / ".tailfast-body.mp4")
        frame_path = track(scratch_dir / ".tailfast-last.yuv")
        tail_path = track(scratch_dir / ".tailfast-clip.mp4")
        list_path = track(scratch_dir / ".tailfast-join.ffconcat")
        output_tmp = track(scratch_dir / (destination.name + ".tailfast-partial.mp4"))

        # 1. Stream-copy the original encoded video body (no audio; the
        #    caller muxes audio separately).
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-video_track_timescale",
                str(timescale),
                str(body_path),
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)

        # 2. Extract the actual last displayed frame with a bounded near-end
        #    decode anchored to the VIDEO stream end. Absolute ``-ss``/``-t``
        #    input seeking is used (never ``-sseof``): the container EOF may
        #    track a longer trailing audio track, which must not shift the
        #    window. ``reverse`` only ever buffers this small window, never
        #    the full recording.
        extract_start = max(0.0, video_end - window)
        extract_len = (video_end - extract_start) + 0.25
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-threads",
                "2",
                "-ss",
                f"{extract_start:.6f}",
                "-t",
                f"{extract_len:.6f}",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-vf",
                "reverse",
                "-frames:v",
                "1",
                "-pix_fmt",
                "yuv420p",
                "-f",
                "rawvideo",
                str(frame_path),
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)
        expected_raw = width * height * 3 // 2
        try:
            actual_raw = frame_path.stat().st_size
        except OSError:
            return fail("last-frame extraction produced no output.")
        if actual_raw != expected_raw:
            return fail("last-frame raw size does not match source dimensions.")

        # 3. Encode the short still clip with source-compatible metadata and
        #    no B-frames so the stream-copy seam cannot reorder.
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-stream_loop",
                "-1",
                "-f",
                "rawvideo",
                "-pixel_format",
                "yuv420p",
                "-video_size",
                f"{width}x{height}",
                "-framerate",
                info["fps_ratio"],
                "-i",
                str(frame_path),
                "-t",
                f"{tail_duration:.6f}",
                "-an",
                "-c:v",
                "libx264",
                "-threads:v",
                "2",
                "-preset",
                "medium",
                "-crf",
                "18",
                "-profile:v",
                info["profile"],
                "-level:v",
                info["level"],
                "-bf",
                "0",
                "-pix_fmt",
                "yuv420p",
                *_color_args(info),
                "-video_track_timescale",
                str(timescale),
                str(tail_path),
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)

        # 4. Concatenate body + tail with matched time base (list args only).
        #    The list references the fixed relative filenames alongside it,
        #    so user paths with spaces/quotes/backslashes never enter the
        #    concat script at all; every other path travels as argv.
        list_path.write_text(
            f"ffconcat version 1.0\nfile {body_path.name}\nfile {tail_path.name}\n",
            encoding="utf-8",
        )
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(list_path),
                "-map",
                "0:v:0",
                "-c:v",
                "copy",
                "-an",
                "-movflags",
                "+faststart",
                str(output_tmp),
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)

        # 5. Efficient validation (never a full decode of a long recording).
        try:
            out_stream, _out_fmt = _probe_streams(ffprobe, output_tmp, cancel_event)
        except _Ineligible as error:
            return fail(f"joined output unreadable: {error}")
        if str(out_stream.get("codec_name") or "").lower() not in {"h264", "avc"}:
            return fail("joined output codec mismatch.")
        if (
            str(out_stream.get("profile") or "").strip().lower()
            != str(stream.get("profile") or "").strip().lower()
        ):
            return fail("joined output profile mismatch.")
        if _level_name(out_stream.get("level")) != info["level"]:
            return fail("joined output level mismatch.")
        try:
            out_w = int(str(out_stream.get("width") or "0"))
            out_h = int(str(out_stream.get("height") or "0"))
        except (TypeError, ValueError):
            return fail("joined output dimensions unreadable.")
        if (out_w, out_h) != (width, height):
            return fail("joined output dimensions changed.")
        if str(out_stream.get("pix_fmt") or "").lower() != "yuv420p":
            return fail("joined output pixel format changed.")
        if _parse_ratio(out_stream.get("time_base")) != _parse_ratio(stream.get("time_base")):
            return fail("joined output time base changed.")
        out_duration = _parse_float(out_stream.get("duration"))
        expected_duration = video_end + tail_duration
        frame_dur = 1.0 / fps
        out_start = _parse_float(out_stream.get("start_time", 0.0))
        if out_start is None:
            out_start = 0.0
        if abs(out_start) > 0.5 / fps + 1e-6:
            return fail("joined output starts at a nonzero timestamp.")
        if out_duration is None or (abs(out_duration - expected_duration) > frame_dur * 1.5 + 0.05):
            return fail("joined output duration is outside tolerance.")
        if info["nb_frames"] is not None:
            try:
                out_frames = int(str(out_stream.get("nb_frames") or "0"))
            except (TypeError, ValueError):
                out_frames = 0
            if out_frames and abs(out_frames - (info["nb_frames"] + tail_frames)) > 1:
                return fail("joined output frame count is outside tolerance.")

        # 5b. Guard the seam with packet timestamps in a small window: DTS
        #     strictly increasing, final body DTS before first tail DTS, and
        #     display steps of exactly one frame across the boundary.
        seam_start = max(0.0, video_end - _PACKET_WINDOW_BEFORE)
        seam_end = video_end + tail_duration + _PACKET_WINDOW_AFTER_PAD
        try:
            packets_payload = _run_probe(
                ffprobe,
                [
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_packets",
                    "-show_entries",
                    "packet=pts_time,dts_time,duration_time,flags",
                    "-read_intervals",
                    f"{seam_start:.6f}%{seam_end:.6f}",
                    "-of",
                    "json",
                    str(output_tmp),
                ],
                cancel_event,
            )
        except _Ineligible as error:
            return fail(f"seam packet probe failed: {error}")
        raw_packets = packets_payload.get("packets")
        packets = raw_packets if isinstance(raw_packets, list) else []
        if len(packets) < 2:
            return fail("seam packet probe returned too few packets.")
        dts_values: list[float] = []
        pts_values: list[float] = []
        for entry in packets:
            if not isinstance(entry, dict):
                continue
            dts = _parse_float(entry.get("dts_time"))
            pts = _parse_float(entry.get("pts_time"))
            if dts is None or pts is None:
                return fail("seam packets carry unreadable timestamps.")
            dts_values.append(dts)
            pts_values.append(pts)
        if any(b <= a for a, b in zip(dts_values, dts_values[1:], strict=False)):
            return fail("DTS is not strictly increasing across the seam.")
        tolerance = max(5e-6, frame_dur * 1e-3)
        ordered_pts = sorted(pts_values)
        if any(
            abs((b - a) - frame_dur) > tolerance
            for a, b in zip(ordered_pts, ordered_pts[1:], strict=False)
        ):
            return fail("display intervals are not exactly one frame.")
        body_pts = [value for value in ordered_pts if value < video_end - frame_dur / 2]
        tail_pts = [value for value in ordered_pts if value >= video_end - frame_dur / 2]
        if not body_pts or not tail_pts:
            return fail("seam boundary packets not found.")
        if abs((tail_pts[0] - body_pts[-1]) - frame_dur) > tolerance:
            return fail("first tail frame is not continuous with the body.")
        if abs(ordered_pts[-1] - (expected_duration - frame_dur)) > (frame_dur * 1.5 + 0.05):
            return fail("final display endpoint is outside tolerance.")

        # 5c. Preserve original decoded frames: compare framemd5 hashes and
        #     presentation timestamps over a bounded pre-seam window. The
        #     window ends a full frame before the video end so tail packets
        #     (which exist only in the joined output) can never leak into
        #     the comparison through boundary-inclusive reads.
        frozen_start = max(0.0, video_end - _FROZEN_WINDOW_SECONDS)
        frozen_len = video_end - frame_dur - frozen_start
        if frozen_len < frame_dur * 0.5:
            return fail("source is too short for frame preservation check.")
        src_md5 = track(scratch_dir / ".tailfast-src.md5")
        out_md5 = track(scratch_dir / ".tailfast-out.md5")
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-threads",
                "2",
                "-ss",
                f"{frozen_start:.6f}",
                "-t",
                f"{frozen_len:.6f}",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-fps_mode",
                "passthrough",
                "-f",
                "framemd5",
                str(src_md5),
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-threads",
                "2",
                "-ss",
                f"{frozen_start:.6f}",
                "-t",
                f"{frozen_len:.6f}",
                "-i",
                str(output_tmp),
                "-map",
                "0:v:0",
                "-an",
                "-fps_mode",
                "passthrough",
                "-f",
                "framemd5",
                str(out_md5),
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)
        try:
            src_rows = _framemd5_rows(src_md5)
            out_rows = _framemd5_rows(out_md5)
        except OSError:
            return fail("frame hash comparison produced no output.")
        if not src_rows or len(src_rows) != len(out_rows):
            return fail("original frame window changed across the join.")
        if any(a != b for a, b in zip(src_rows, out_rows, strict=False)):
            return fail("original decoded frames/timestamps not preserved.")

        # 5d. Strict local decode around the seam only.
        strict_start = max(0.0, video_end - _STRICT_WINDOW_BEFORE)
        strict_len = (video_end - strict_start) + tail_duration + (_STRICT_WINDOW_AFTER_PAD)
        run_command(
            [
                ffmpeg_executable,
                *_FF_PREFIX,
                "-threads",
                "2",
                "-ss",
                f"{strict_start:.6f}",
                "-t",
                f"{strict_len:.6f}",
                "-err_detect",
                "explode",
                "-xerror",
                "-i",
                str(output_tmp),
                "-map",
                "0:v:0",
                "-an",
                "-f",
                "null",
                "-",
            ],
            cancel_event,
        )
        _check_cancelled(cancel_event)

        _remove_quietly(destination)
        os.replace(output_tmp, destination)
        intermediates = [path for path in intermediates if path != output_tmp]
        cleanup()
        logger.info(
            "Video tail fast path extended %s by %d frames (%.3fs).",
            source.name,
            tail_frames,
            tail_duration,
        )
        return True
    except InterruptedError:
        cleanup()
        raise
    except _Ineligible as error:
        return fail(str(error))
    except Exception as error:  # noqa: BLE001 - any FFmpeg failure means fallback
        cleanup()
        if cancel_event.is_set():
            raise InterruptedError("Video tail fast path canceled.") from error
        logger.warning(
            "Video tail fast path failed, using full encode: %s",
            str(error)[:200],
        )
        return False
