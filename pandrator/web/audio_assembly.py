"""Bounded-memory assembly of immutable generation takes."""

from __future__ import annotations

import math
import os
import tempfile
import threading
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from pydub import AudioSegment

from .media_process import (
    MediaProcessCancelled,
    MediaProcessError,
    probe_audio_stream,
    resolve_ffmpeg_executable,
    run_media_process,
)


OUTPUT_FORMATS = {"wav", "mp3", "m4b", "opus", "flac"}
STREAMING_BACKEND = "streaming"
PYDUB_BACKEND = "pydub"
PCM_SAMPLE_WIDTH_BYTES = 2
PCM_COPY_FRAMES = 64 * 1024

# Public compatibility name for callers and tests.
AudioAssemblyCancelled = MediaProcessCancelled


@dataclass(frozen=True)
class AudioAssemblyPart:
    """One immutable source in an assembly plan.

    The plan carries paths and timing instructions only. Decoded audio must
    never be retained here.
    """

    path: Path
    expected_duration_ms: int
    silence_before_ms: int = 0
    silence_after_ms: int = 0
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    crossfade_after_ms: int = 0
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", Path(self.path))
        for field_name in (
            "expected_duration_ms",
            "silence_before_ms",
            "silence_after_ms",
            "fade_in_ms",
            "fade_out_ms",
            "crossfade_after_ms",
        ):
            value = int(getattr(self, field_name) or 0)
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative.")
            object.__setattr__(self, field_name, value)


@dataclass(frozen=True)
class AudioChapter:
    part_index: int
    title: str
    expected_start_ms: int

    def __post_init__(self) -> None:
        if int(self.part_index) < 0:
            raise ValueError("Chapter part_index cannot be negative.")
        object.__setattr__(self, "part_index", int(self.part_index))
        object.__setattr__(self, "expected_start_ms", max(0, int(self.expected_start_ms or 0)))
        object.__setattr__(self, "title", str(self.title or "").strip())


@dataclass(frozen=True)
class AudioEncodingSettings:
    output_format: str = "wav"
    bitrate: str = "192k"
    sample_rate_hz: int = 24000
    channels: int = 1
    sample_width_bytes: int = PCM_SAMPLE_WIDTH_BYTES

    def __post_init__(self) -> None:
        normalized = str(self.output_format or "wav").strip().lower()
        if normalized not in OUTPUT_FORMATS:
            raise ValueError(f"Unsupported audio output format: {self.output_format}")
        sample_rate = int(self.sample_rate_hz or 0)
        channels = int(self.channels or 0)
        if sample_rate < 8000 or sample_rate > 192000:
            raise ValueError("Assembly sample rate must be between 8000 and 192000 Hz.")
        if channels not in {1, 2}:
            raise ValueError("Assembly output must be mono or stereo.")
        if int(self.sample_width_bytes) != PCM_SAMPLE_WIDTH_BYTES:
            raise ValueError("Streaming assembly currently requires 16-bit PCM.")
        object.__setattr__(self, "output_format", normalized)
        object.__setattr__(self, "bitrate", str(self.bitrate or "192k"))
        object.__setattr__(self, "sample_rate_hz", sample_rate)
        object.__setattr__(self, "channels", channels)
        object.__setattr__(self, "sample_width_bytes", PCM_SAMPLE_WIDTH_BYTES)


@dataclass(frozen=True)
class AudioAssemblyPlan:
    parts: tuple[AudioAssemblyPart, ...]
    chapters: tuple[AudioChapter, ...]
    expected_duration_ms: int
    encoding: AudioEncodingSettings

    def __post_init__(self) -> None:
        object.__setattr__(self, "parts", tuple(self.parts))
        object.__setattr__(self, "chapters", tuple(self.chapters))
        if not self.parts:
            raise ValueError("At least one audio take is required for assembly.")
        expected = int(self.expected_duration_ms or 0)
        if expected < 0:
            raise ValueError("Expected assembly duration cannot be negative.")
        object.__setattr__(self, "expected_duration_ms", expected)
        for chapter in self.chapters:
            if chapter.part_index >= len(self.parts):
                raise ValueError("Chapter part_index is outside the assembly plan.")
        if any(part.crossfade_after_ms for part in self.parts):
            raise ValueError(
                "Crossfade instructions are reserved in AudioAssemblyPlan but "
                "are not enabled by the current workspace settings."
            )


@dataclass(frozen=True)
class AudioAssemblyResult:
    duration_ms: int
    part_duration_ms: tuple[int, ...]
    chapter_starts_ms: tuple[int, ...]
    backend: str


def resolve_assembly_backend(explicit: str | None = None) -> str:
    value = str(explicit or os.environ.get("PANDRATOR_AUDIO_ASSEMBLER") or STREAMING_BACKEND)
    normalized = value.strip().lower().replace("_", "-")
    if normalized in {"streaming", "ffmpeg", "bounded"}:
        return STREAMING_BACKEND
    if normalized in {"pydub", "legacy", "legacy-pydub"}:
        return PYDUB_BACKEND
    raise ValueError(
        "PANDRATOR_AUDIO_ASSEMBLER must be 'streaming' (default) or 'pydub'."
    )


def preferred_pcm_format(
    path: str | os.PathLike[str],
    *,
    ffprobe_executable: str | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[int, int]:
    """Choose a stable PCM target from the first take without decoding it."""

    source = Path(path)
    try:
        with wave.open(str(source), "rb") as reader:
            if reader.getcomptype() == "NONE":
                sample_rate = int(reader.getframerate())
                channels = int(reader.getnchannels())
                if 8000 <= sample_rate <= 192000 and channels in {1, 2}:
                    return sample_rate, channels
    except (EOFError, OSError, wave.Error):
        pass
    info = probe_audio_stream(
        source,
        ffprobe_executable=ffprobe_executable,
        cancel_event=cancel_event,
    )
    return (
        min(192000, max(8000, info.sample_rate_hz)),
        1 if info.channels <= 1 else 2,
    )


def build_audio_assembly_plan(
    parts: Iterable[AudioAssemblyPart],
    *,
    output_format: str,
    bitrate: str = "192k",
    sample_rate_hz: int = 24000,
    channels: int = 1,
    chapters: Iterable[tuple[int, str]] = (),
) -> AudioAssemblyPlan:
    """Build an immutable metadata-only plan and its expected timeline."""

    ordered = tuple(parts)
    timeline_ms = 0
    starts: list[int] = []
    for part in ordered:
        timeline_ms += part.silence_before_ms
        starts.append(timeline_ms)
        timeline_ms += part.expected_duration_ms + part.silence_after_ms
    chapter_values = tuple(
        AudioChapter(
            part_index=int(part_index),
            title=str(title),
            expected_start_ms=starts[int(part_index)],
        )
        for part_index, title in chapters
    )
    return AudioAssemblyPlan(
        parts=ordered,
        chapters=chapter_values,
        expected_duration_ms=timeline_ms,
        encoding=AudioEncodingSettings(
            output_format=output_format,
            bitrate=bitrate,
            sample_rate_hz=sample_rate_hz,
            channels=channels,
        ),
    )


def compose_audio(
    parts: Iterable[tuple[AudioSegment, int]],
    settings: dict[str, Any] | None = None,
) -> AudioSegment:
    """Legacy in-memory composition retained for rollback compatibility."""

    values = list(parts)
    if not values:
        raise ValueError("At least one audio take is required for assembly.")
    options = settings or {}
    fade_enabled = bool(options.get("fade_enabled", options.get("enable_fade", False)))
    fade_in = max(0, int(options.get("fade_in_ms", options.get("fade_in_duration", 0)) or 0))
    fade_out = max(0, int(options.get("fade_out_ms", options.get("fade_out_duration", 0)) or 0))
    combined = AudioSegment.empty()
    for index, (source, silence_after_ms) in enumerate(values):
        audio = source
        if fade_enabled:
            if fade_in:
                audio = audio.fade_in(min(fade_in, len(audio)))
            if fade_out:
                audio = audio.fade_out(min(fade_out, len(audio)))
        combined += audio
        if index < len(values) - 1 and int(silence_after_ms or 0) > 0:
            combined += AudioSegment.silent(
                duration=max(0, int(silence_after_ms)),
                frame_rate=max(8000, int(audio.frame_rate or 24000)),
            )
    return combined


def export_audio(
    audio: AudioSegment,
    destination: Path,
    output_format: str,
    bitrate: str = "192k",
) -> None:
    """Legacy Pydub export using Pandrator's supported containers/codecs."""

    normalized = str(output_format or "wav").strip().lower()
    if normalized not in OUTPUT_FORMATS:
        raise ValueError(f"Unsupported audio output format: {output_format}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {}
    ffmpeg_format = normalized
    if normalized == "wav":
        options["codec"] = "pcm_s16le"
    elif normalized == "mp3":
        options.update(codec="libmp3lame", bitrate=bitrate)
    elif normalized == "m4b":
        ffmpeg_format = "mp4"
        options.update(codec="aac", bitrate=bitrate)
    elif normalized == "opus":
        options.update(codec="libopus", bitrate=bitrate)
    exported = audio.export(destination, format=ffmpeg_format, **options)
    exported.close()


def _fade_filter(part: AudioAssemblyPart) -> str:
    filters: list[str] = []
    if part.fade_in_ms:
        duration = min(part.fade_in_ms, part.expected_duration_ms) / 1000
        if duration > 0:
            filters.append(f"afade=t=in:st=0:d={duration:.6f}")
    if part.fade_out_ms:
        duration_ms = min(part.fade_out_ms, part.expected_duration_ms)
        start_ms = max(0, part.expected_duration_ms - duration_ms)
        if duration_ms > 0:
            filters.append(
                f"afade=t=out:st={start_ms / 1000:.6f}:d={duration_ms / 1000:.6f}"
            )
    return ",".join(filters)


def _normalize_part(
    part: AudioAssemblyPart,
    destination: Path,
    encoding: AudioEncodingSettings,
    *,
    ffmpeg_executable: str,
    cancel_event: threading.Event | None,
) -> None:
    command: list[str] = [
        ffmpeg_executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(part.path),
        "-map",
        "0:a:0",
        "-vn",
        "-sn",
        "-dn",
    ]
    fade_filter = _fade_filter(part)
    if fade_filter:
        command.extend(["-af", fade_filter])
    command.extend(
        [
            "-ar",
            str(encoding.sample_rate_hz),
            "-ac",
            str(encoding.channels),
            "-c:a",
            "pcm_s16le",
            "-f",
            "wav",
            str(destination),
        ]
    )
    run_media_process(command, cancel_event=cancel_event)


def _write_silence(
    writer: wave.Wave_write,
    frame_count: int,
    bytes_per_frame: int,
    *,
    cancel_event: threading.Event | None,
) -> None:
    remaining = max(0, int(frame_count))
    zero_chunk = b"\0" * (min(PCM_COPY_FRAMES, max(1, remaining)) * bytes_per_frame)
    while remaining:
        if cancel_event is not None and cancel_event.is_set():
            raise MediaProcessCancelled("Audio assembly was canceled.")
        count = min(PCM_COPY_FRAMES, remaining)
        writer.writeframesraw(zero_chunk[: count * bytes_per_frame])
        remaining -= count


def _copy_direct_pcm_wav(
    source: Path,
    writer: wave.Wave_write,
    *,
    encoding: AudioEncodingSettings,
    cancel_event: threading.Event | None,
) -> int | None:
    copied = 0
    try:
        reader_context = wave.open(str(source), "rb")
    except (EOFError, OSError, wave.Error):
        return None
    with reader_context as reader:
        if (
            reader.getcomptype() != "NONE"
            or reader.getsampwidth() != encoding.sample_width_bytes
            or reader.getframerate() != encoding.sample_rate_hz
            or reader.getnchannels() != encoding.channels
        ):
            return None
        while True:
            if cancel_event is not None and cancel_event.is_set():
                raise MediaProcessCancelled("Audio assembly was canceled.")
            frames = reader.readframes(PCM_COPY_FRAMES)
            if not frames:
                break
            writer.writeframesraw(frames)
            copied += len(frames) // (
                encoding.sample_width_bytes * encoding.channels
            )
    return copied


def _copy_normalized_wav(
    source: Path,
    writer: wave.Wave_write,
    *,
    encoding: AudioEncodingSettings,
    cancel_event: threading.Event | None,
) -> int:
    copied = _copy_direct_pcm_wav(
        source,
        writer,
        encoding=encoding,
        cancel_event=cancel_event,
    )
    if copied is None:
        raise MediaProcessError(
            f"Normalized PCM parameters do not match for {source.name}."
        )
    return copied


def _encoding_command(
    ffmpeg_executable: str,
    timeline: Path,
    destination: Path,
    encoding: AudioEncodingSettings,
) -> list[str]:
    command = [
        ffmpeg_executable,
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(timeline),
        "-map",
        "0:a:0",
        "-vn",
    ]
    if encoding.output_format == "mp3":
        command.extend(["-c:a", "libmp3lame", "-b:a", encoding.bitrate])
    elif encoding.output_format == "m4b":
        command.extend(
            ["-c:a", "aac", "-b:a", encoding.bitrate, "-movflags", "+faststart", "-f", "mp4"]
        )
    elif encoding.output_format == "opus":
        command.extend(["-c:a", "libopus", "-b:a", encoding.bitrate])
    elif encoding.output_format == "flac":
        command.extend(["-c:a", "flac"])
    else:
        command.extend(["-c:a", "pcm_s16le", "-f", "wav"])
    command.append(str(destination))
    return command


def _assemble_streaming(
    plan: AudioAssemblyPlan,
    destination: Path,
    *,
    work_dir: Path,
    ffmpeg_executable: str,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
) -> AudioAssemblyResult:
    destination.parent.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    encoding = plan.encoding
    bytes_per_frame = encoding.sample_width_bytes * encoding.channels
    chapters_by_part: dict[int, list[int]] = {}
    for chapter_index, chapter in enumerate(plan.chapters):
        chapters_by_part.setdefault(chapter.part_index, []).append(chapter_index)
    chapter_starts = [0] * len(plan.chapters)
    part_durations: list[int] = []
    total_frames = 0

    with tempfile.TemporaryDirectory(prefix=".assembly-stream-", dir=work_dir) as temporary:
        temporary_path = Path(temporary)
        timeline = temporary_path / "timeline.wav"
        with wave.open(str(timeline), "wb") as writer:
            writer.setnchannels(encoding.channels)
            writer.setsampwidth(encoding.sample_width_bytes)
            writer.setframerate(encoding.sample_rate_hz)
            for index, part in enumerate(plan.parts):
                if cancel_event is not None and cancel_event.is_set():
                    raise MediaProcessCancelled("Audio assembly was canceled.")
                if not part.path.is_file():
                    raise FileNotFoundError(f"Audio source is missing: {part.path.name}")
                before_frames = int(round(encoding.sample_rate_hz * part.silence_before_ms / 1000))
                _write_silence(
                    writer,
                    before_frames,
                    bytes_per_frame,
                    cancel_event=cancel_event,
                )
                total_frames += before_frames
                actual_start_ms = int(round(total_frames * 1000 / encoding.sample_rate_hz))
                for chapter_index in chapters_by_part.get(index, ()):
                    chapter_starts[chapter_index] = actual_start_ms

                copied_frames = None
                if not part.fade_in_ms and not part.fade_out_ms:
                    copied_frames = _copy_direct_pcm_wav(
                        part.path,
                        writer,
                        encoding=encoding,
                        cancel_event=cancel_event,
                    )
                if copied_frames is None:
                    normalized = temporary_path / f"part-{index + 1:06d}.wav"
                    _normalize_part(
                        part,
                        normalized,
                        encoding,
                        ffmpeg_executable=ffmpeg_executable,
                        cancel_event=cancel_event,
                    )
                    copied_frames = _copy_normalized_wav(
                        normalized,
                        writer,
                        encoding=encoding,
                        cancel_event=cancel_event,
                    )
                    normalized.unlink(missing_ok=True)
                part_durations.append(
                    int(round(copied_frames * 1000 / encoding.sample_rate_hz))
                )
                total_frames += copied_frames

                after_frames = int(round(encoding.sample_rate_hz * part.silence_after_ms / 1000))
                _write_silence(
                    writer,
                    after_frames,
                    bytes_per_frame,
                    cancel_event=cancel_event,
                )
                total_frames += after_frames
                if progress is not None:
                    progress(
                        0.75 * ((index + 1) / len(plan.parts)),
                        f"Streamed audio segment {index + 1} of {len(plan.parts)}",
                    )

        if cancel_event is not None and cancel_event.is_set():
            raise MediaProcessCancelled("Audio assembly was canceled.")
        encoded = temporary_path / f"encoded.{encoding.output_format}"
        if encoding.output_format == "wav":
            os.replace(timeline, encoded)
        else:
            if progress is not None:
                progress(0.8, f"Encoding {encoding.output_format.upper()} output")
            run_media_process(
                _encoding_command(ffmpeg_executable, timeline, encoded, encoding),
                cancel_event=cancel_event,
            )
        if progress is not None:
            progress(0.95, "Finalizing assembled audio")
        os.replace(encoded, destination)

    return AudioAssemblyResult(
        duration_ms=int(round(total_frames * 1000 / encoding.sample_rate_hz)),
        part_duration_ms=tuple(part_durations),
        chapter_starts_ms=tuple(chapter_starts),
        backend=STREAMING_BACKEND,
    )


def _assemble_pydub(
    plan: AudioAssemblyPlan,
    destination: Path,
    *,
    cancel_event: threading.Event | None,
    progress: Callable[[float, str], None] | None,
) -> AudioAssemblyResult:
    """Compatibility path matching the pre-Phase-3 in-memory behavior."""

    encoding = plan.encoding
    combined = AudioSegment.silent(duration=0, frame_rate=encoding.sample_rate_hz)
    combined = combined.set_channels(encoding.channels).set_sample_width(
        encoding.sample_width_bytes
    )
    chapters_by_part: dict[int, list[int]] = {}
    for chapter_index, chapter in enumerate(plan.chapters):
        chapters_by_part.setdefault(chapter.part_index, []).append(chapter_index)
    chapter_starts = [0] * len(plan.chapters)
    part_durations: list[int] = []
    for index, part in enumerate(plan.parts):
        if cancel_event is not None and cancel_event.is_set():
            raise MediaProcessCancelled("Audio assembly was canceled.")
        if part.silence_before_ms:
            combined += AudioSegment.silent(
                duration=part.silence_before_ms,
                frame_rate=encoding.sample_rate_hz,
            ).set_channels(encoding.channels)
        for chapter_index in chapters_by_part.get(index, ()):
            chapter_starts[chapter_index] = len(combined)
        audio = (
            AudioSegment.from_file(part.path)
            .set_frame_rate(encoding.sample_rate_hz)
            .set_channels(encoding.channels)
            .set_sample_width(encoding.sample_width_bytes)
        )
        if part.fade_in_ms:
            audio = audio.fade_in(min(part.fade_in_ms, len(audio)))
        if part.fade_out_ms:
            audio = audio.fade_out(min(part.fade_out_ms, len(audio)))
        part_durations.append(len(audio))
        combined += audio
        if part.silence_after_ms:
            combined += AudioSegment.silent(
                duration=part.silence_after_ms,
                frame_rate=encoding.sample_rate_hz,
            ).set_channels(encoding.channels)
        if progress is not None:
            progress(
                0.75 * ((index + 1) / len(plan.parts)),
                f"Loaded audio segment {index + 1} of {len(plan.parts)}",
            )
    if cancel_event is not None and cancel_event.is_set():
        raise MediaProcessCancelled("Audio assembly was canceled.")
    with tempfile.TemporaryDirectory(prefix=".assembly-pydub-", dir=destination.parent) as temporary:
        encoded = Path(temporary) / f"encoded.{encoding.output_format}"
        export_audio(combined, encoded, encoding.output_format, encoding.bitrate)
        os.replace(encoded, destination)
    return AudioAssemblyResult(
        duration_ms=len(combined),
        part_duration_ms=tuple(part_durations),
        chapter_starts_ms=tuple(chapter_starts),
        backend=PYDUB_BACKEND,
    )


def assemble_audio_plan(
    plan: AudioAssemblyPlan,
    destination: str | os.PathLike[str],
    *,
    backend: str | None = None,
    work_dir: str | os.PathLike[str] | None = None,
    ffmpeg_executable: str | None = None,
    cancel_event: threading.Event | None = None,
    progress: Callable[[float, str], None] | None = None,
) -> AudioAssemblyResult:
    """Render a metadata-only plan with bounded memory and atomic replacement."""

    output = Path(destination)
    output.parent.mkdir(parents=True, exist_ok=True)
    selected_backend = resolve_assembly_backend(backend)
    if selected_backend == PYDUB_BACKEND:
        return _assemble_pydub(
            plan,
            output,
            cancel_event=cancel_event,
            progress=progress,
        )
    return _assemble_streaming(
        plan,
        output,
        work_dir=Path(work_dir) if work_dir is not None else output.parent,
        ffmpeg_executable=resolve_ffmpeg_executable(ffmpeg_executable),
        cancel_event=cancel_event,
        progress=progress,
    )


def pcm_duration_ms(path: str | os.PathLike[str]) -> int:
    """Return a PCM WAV duration from its header without decoding samples."""

    with wave.open(str(path), "rb") as reader:
        return int(
            math.ceil(reader.getnframes() * 1000 / max(1, reader.getframerate()))
        )
