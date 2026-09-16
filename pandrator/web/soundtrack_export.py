"""Reusable, timeline-preserving soundtrack masters for audio and video export."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import threading
from pathlib import Path
from typing import Any

from sqlalchemy import select

from pandrator.logic.dubbing.audio_sync import build_mix_filter_complex
from .models import Artifact, new_id

SAMPLE_RATE = 48_000
VIDEO_TAIL_EXTENSION_DEFAULT_MS = 2000
VIDEO_TAIL_EXTENSION_MAX_MS = 30000
MIX_KEYS = (
    "mix_source_gain_db",
    "mix_voice_gain_db",
    "mix_voice_lufs",
    "mix_ducking",
    "mix_attack_ms",
    "mix_release_ms",
)


def _executable(name: str) -> str:
    executable = os.environ.get(f"PANDRATOR_{name.upper()}_EXE") or shutil.which(name)
    if not executable:
        raise ValueError(f"{name} is required for soundtrack export.")
    return executable


def probe_soundtrack_media(path: Path) -> dict[str, Any]:
    result = subprocess.run(
        [
            _executable("ffprobe"),
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    data = json.loads(result.stdout)
    streams = data.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    preferred = video or audio or {}
    try:
        duration = float(
            preferred.get("duration") or (data.get("format") or {}).get("duration") or 0
        )
    except (TypeError, ValueError):
        duration = 0
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError(
            "The recording duration could not be determined. Verify the media before exporting."
        )
    fps: float | None = None
    if video is not None:
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(video.get(key) or "")
            if "/" in raw:
                numerator, _, denominator = raw.partition("/")
                try:
                    value = float(numerator) / float(denominator)
                except (TypeError, ValueError, ZeroDivisionError):
                    continue
                if math.isfinite(value) and value > 0:
                    fps = value
                    break

    def _stream_duration(item: dict[str, Any] | None) -> float | None:
        if not item:
            return None
        try:
            value = float(item.get("duration") or 0)
        except (TypeError, ValueError):
            return None
        if math.isfinite(value) and value > 0:
            return value
        return None

    try:
        container_duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        container_duration = 0
    if not math.isfinite(container_duration) or container_duration <= 0:
        container_duration = None
    return {
        "duration": duration,
        "has_audio": audio is not None,
        "has_video": video is not None,
        "fps": fps,
        "video_duration": _stream_duration(video),
        "audio_duration": _stream_duration(audio),
        "container_duration": container_duration,
    }


def video_tail_extension_cap_ms(settings: dict[str, Any] | None) -> int:
    """Normalize the frozen-tail allowance for voiceover media export.

    Returns an integer from 0 through 30000; 0 keeps the strict timeline with
    no frame freeze. Unknown or malformed values raise instead of silently
    widening the export.
    """

    raw = (settings or {}).get(
        "video_tail_extension_max_ms", VIDEO_TAIL_EXTENSION_DEFAULT_MS
    )
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(
            "video_tail_extension_max_ms must be an integer from 0 to 30000."
        )
    number = raw
    if not 0 <= number <= VIDEO_TAIL_EXTENSION_MAX_MS:
        raise ValueError(
            "video_tail_extension_max_ms must be an integer from 0 to 30000."
        )
    return number


OVERRUN_TOLERANCE_SECONDS = 0.05


def ceil_tail_extension_seconds(overrun_seconds: float, fps: float | None) -> float:
    """Ceil a positive overrun to whole video frames so the tail covers audio.

    Canonical frame math lives in video_muxing.video_tail_extension_seconds;
    this wrapper keeps audio-master durations consistent with the frozen video
    tail (frame rounding plus one extra frame, no fractional-second trust).
    """

    from pandrator.logic.dubbing.video_muxing import video_tail_extension_seconds

    return video_tail_extension_seconds(overrun_seconds, fps)


def resolve_video_tail_extension_ms(
    *,
    reference_duration: float,
    generated_duration: float,
    fps: float | None,
    settings: dict[str, Any] | None,
) -> int:
    """Return the frozen-tail extension in ms, or 0 when the timeline fits.

    Raises a clear actionable error when the terminal overrun exceeds the
    bounded allowance instead of silently clipping speech.
    """

    overrun = generated_duration - reference_duration
    if overrun <= OVERRUN_TOLERANCE_SECONDS:
        return 0
    extension_seconds = ceil_tail_extension_seconds(overrun, fps)
    allowance_ms = video_tail_extension_cap_ms(settings)
    extension_ms = round(extension_seconds * 1000)
    if allowance_ms <= 0 or extension_ms > allowance_ms:
        raise ValueError(
            f"Generated speech exceeds the recording by {overrun:.2f} seconds. "
            f"Cover up to {allowance_ms} ms by freezing the last video frame "
            "(output.video_tail_extension_max_ms), review synchronization, or turn "
            "off 'Match recording timeline'; speech will not be silently cut."
        )
    return extension_ms


def run_audio_command(command: list[str], cancel_event: threading.Event) -> None:
    if cancel_event.is_set():
        raise InterruptedError("Soundtrack export cancelled.")
    with subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    ) as process:
        while True:
            try:
                _stdout, stderr = process.communicate(timeout=0.25)
                break
            except subprocess.TimeoutExpired:
                if cancel_event.is_set():
                    process.terminate()
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise InterruptedError("Soundtrack export cancelled.")
        if process.returncode:
            raise ValueError(f"Soundtrack rendering failed: {stderr[-1800:]}")


def ensure_soundtrack_master(
    handler,
    *,
    session_id: str,
    source: Artifact | None,
    speech: Artifact | None,
    audio_mode: str,
    settings: dict[str, Any],
    cancel_event: threading.Event,
    tail_extension_ms: int = 0,
) -> Artifact:
    """Use the same canonical WAV master for a soundtrack file or a video mux.

    The reference timeline is never trimmed to detected speech. Short tracks are
    padded. A generated track exceeding the reference raises a useful error
    instead of silently clipping spoken words. Video callers pass the single
    shared frozen-tail extension (computed once with
    resolve_video_tail_extension_ms from the VIDEO stream duration) so the
    master covers the full generated speech; audio-only callers leave the
    default strict behavior.
    """
    if audio_mode not in {"mixed", "source", "dubbed"}:
        raise ValueError("Choose mixed, original or voiceover-only audio.")
    if audio_mode == "source":
        speech = None
    source_path = handler._resolve_input(source.id)[1] if source is not None else None
    speech_path = handler._resolve_input(speech.id)[1] if speech is not None else None
    if audio_mode in {"mixed", "source"} and source_path is None:
        raise ValueError("This soundtrack needs an associated original recording.")
    if audio_mode in {"mixed", "dubbed"} and speech_path is None:
        raise ValueError(
            "Assemble the selected generated speech before exporting its soundtrack."
        )
    reference = probe_soundtrack_media(source_path) if source_path is not None else None
    generated = probe_soundtrack_media(speech_path) if speech_path is not None else None
    if audio_mode in {"mixed", "source"} and not reference["has_audio"]:
        raise ValueError(
            "The original recording has no audio stream. Choose voiceover-only audio."
        )
    match_reference = (
        bool(settings.get("audio_match_source_duration", True))
        and reference is not None
    )
    tail_extension_ms = int(tail_extension_ms or 0)
    if tail_extension_ms < 0:
        raise ValueError("tail_extension_ms must not be negative.")
    reference_duration = (
        reference.get("video_duration") or reference["duration"]
        if reference is not None
        else 0
    )
    if (
        match_reference
        and audio_mode in {"mixed", "dubbed"}
        and reference is not None
        and generated is not None
    ):
        overrun = generated["duration"] - reference_duration
        if overrun > OVERRUN_TOLERANCE_SECONDS and not tail_extension_ms:
            raise ValueError(
                f"Generated speech exceeds the recording by {overrun:.2f} seconds. "
                "Review synchronization or turn off 'Match recording timeline'; speech will not be silently cut."
            )
        if tail_extension_ms and reference_duration + tail_extension_ms / 1000 < (
            generated["duration"] - OVERRUN_TOLERANCE_SECONDS
        ):
            raise ValueError(
                f"Generated speech exceeds the recording by {overrun:.2f} seconds, "
                "which the shared frozen-tail extension does not cover; "
                "speech will not be silently cut."
            )
    # A matched master never truncates speech: sub-tolerance positive overruns
    # are covered at full generated length (no freeze, no reencode); only true
    # shortfalls pad out to the reference timeline.
    duration = (
        reference_duration + tail_extension_ms / 1000
        if match_reference and tail_extension_ms
        else (
            max(reference_duration, generated["duration"])
            if match_reference
            else max(
                reference_duration if reference else 0,
                generated["duration"] if generated else 0,
            )
        )
    )
    if (
        match_reference
        and not tail_extension_ms
        and audio_mode in {"mixed", "dubbed"}
        and generated["duration"] > duration + OVERRUN_TOLERANCE_SECONDS
    ):
        raise ValueError(
            f"Generated speech exceeds the recording by {generated['duration'] - duration:.2f} seconds. "
            "Review synchronization or turn off 'Match recording timeline'; speech will not be silently cut."
        )
    samples = round(duration * SAMPLE_RATE)
    definition = {
        "version": 2,
        "source_id": source.id if source else None,
        "source_hash": source.content_hash if source else None,
        "speech_id": speech.id if speech else None,
        "speech_hash": speech.content_hash if speech else None,
        "audio_mode": audio_mode,
        "samples": samples,
        "sample_rate": SAMPLE_RATE,
        "match_reference": match_reference,
        "tail_extension_ms": tail_extension_ms,
        "mix": {key: settings.get(key) for key in MIX_KEYS},
    }
    key = hashlib.sha256(json.dumps(definition, sort_keys=True).encode()).hexdigest()
    with handler.database.session() as session:
        cached = session.scalar(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.role == "soundtrack_master",
                Artifact.state == "current",
                Artifact.metadata_json["soundtrack_key"].as_string() == key,
            )
            .order_by(Artifact.created_at.desc())
            .limit(1)
        )
        if (
            cached is not None
            and handler.paths.managed_path(cached.relative_path).is_file()
        ):
            session.expunge(cached)
            return cached
    destination = (
        handler._operation_dir(session_id, "soundtracks") / f"soundtrack-{new_id()}.wav"
    )
    temporary = destination.with_name(f".{destination.stem}.partial.wav")
    command = [_executable("ffmpeg"), "-nostdin", "-v", "error", "-y"]
    if audio_mode == "mixed":
        command += ["-i", str(source_path), "-i", str(speech_path)]
        graph = build_mix_filter_complex(
            source_gain_db=settings.get("mix_source_gain_db", 0.0),
            voice_gain_db=settings.get("mix_voice_gain_db", 0.0),
            voice_lufs=settings.get("mix_voice_lufs", -16.0),
            ducking=str(settings.get("mix_ducking") or "strong"),
            attack_ms=settings.get("mix_attack_ms", 25),
            release_ms=settings.get("mix_release_ms", 350),
        )
        if not match_reference:
            graph = graph.replace("duration=first", "duration=longest")
        else:
            # amix duration=first ends the mix with the source audio, dropping
            # generated speech past the reference; apad then backfills silence
            # so durations look right while words are lost. Whenever the master
            # extends past the reference to cover speech, mix longest and let
            # the final atrim cap the length exactly.
            reference_audio_duration = (
                (reference.get("audio_duration") or reference_duration)
                if reference is not None
                else 0
            )
            if samples > round(reference_audio_duration * SAMPLE_RATE):
                graph = graph.replace("duration=first", "duration=longest")
        graph += f";[mixed]aresample={SAMPLE_RATE},apad,atrim=end_sample={samples}[soundtrack]"
    else:
        command += ["-i", str(source_path if audio_mode == "source" else speech_path)]
        graph = f"[0:a:0]aresample={SAMPLE_RATE},apad,atrim=end_sample={samples}[soundtrack]"
    command += [
        "-filter_complex",
        graph,
        "-map",
        "[soundtrack]",
        "-vn",
        "-sn",
        "-dn",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "2",
        "-c:a",
        "pcm_s24le",
        str(temporary),
    ]
    try:
        run_audio_command(command, cancel_event)
        if cancel_event.is_set():
            raise InterruptedError("Soundtrack export cancelled.")
        temporary.replace(destination)
        return handler.artifacts.register(
            destination,
            kind="audio",
            role="soundtrack_master",
            session_id=session_id,
            parent_ids=[item.id for item in (source, speech) if item is not None],
            settings=definition,
            metadata={
                "soundtrack_key": key,
                "soundtrack": definition,
                "duration_ms": samples * 1000 / SAMPLE_RATE,
                "tail_extension_ms": tail_extension_ms,
                "language": settings.get("target_language") or settings.get("language"),
            },
        )
    finally:
        temporary.unlink(missing_ok=True)


def export_soundtrack_file(
    handler,
    *,
    session_id: str,
    master: Artifact,
    destination: Path,
    settings: dict[str, Any],
    cancel_event: threading.Event,
) -> Artifact:
    source_path = handler._resolve_input(master.id)[1]
    format_name = str(settings.get("format") or "wav").lower()
    codecs = {
        "wav": ["-c:a", "pcm_s24le"],
        "flac": ["-c:a", "flac"],
        "mp3": ["-c:a", "libmp3lame", "-b:a", "320k"],
        "opus": ["-c:a", "libopus", "-b:a", "160k"],
    }
    if format_name in {"mp3", "opus"}:
        bitrate = str(
            settings.get("bitrate") or ("320k" if format_name == "mp3" else "160k")
        ).lower()
        if (
            not re.fullmatch(r"[1-9][0-9]{0,2}k", bitrate)
            or not 8 <= int(bitrate[:-1]) <= 512
        ):
            raise ValueError("Audio bitrate must be between 8k and 512k.")
        codecs[format_name][-1] = bitrate
    if format_name not in codecs:
        raise ValueError("Audio-only export supports WAV, FLAC, MP3 or Opus.")
    destination = destination.with_suffix("." + format_name)
    temporary = destination.with_name(
        f".{destination.stem}-{new_id()}.partial.{format_name}"
    )
    metadata = dict(master.metadata_json or {})
    try:
        if format_name == "wav":
            if cancel_event.is_set():
                raise InterruptedError("Soundtrack export cancelled.")
            shutil.copyfile(source_path, temporary)
        else:
            command = [
                _executable("ffmpeg"),
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-i",
                str(source_path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                *codecs[format_name],
            ]
            language = metadata.get("language")
            if language:
                command += ["-metadata:s:a:0", f"language={language}"]
            command += [str(temporary)]
            run_audio_command(command, cancel_event)
        if cancel_event.is_set():
            raise InterruptedError("Soundtrack export cancelled.")
        temporary.replace(destination)
        mode = metadata.get("soundtrack", {}).get("audio_mode", "dubbed")
        return handler.artifacts.register(
            destination,
            kind="export",
            role=f"export_{mode}_audio",
            session_id=session_id,
            parent_ids=[master.id],
            settings=settings,
            metadata={
                **metadata,
                "audio_only": True,
                "soundtrack_master_id": master.id,
            },
        )
    finally:
        temporary.unlink(missing_ok=True)
