from __future__ import annotations

import hashlib
import math
import os
import shutil
import struct
import threading
import wave
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from pandrator.logic.media_input_policy import LOCAL_MEDIA_INPUT_OPTIONS
from pandrator.runtime import DataPaths
from pandrator.web import (
    audio_assembly,
    media_process,
    sound_mix_manifest,
    video_previews,
    voice_reference_trim,
    waveform,
    workflow_audio_preview,
)
from pandrator.web.artifacts import ArtifactService
from pandrator.web.audio_assembly import AudioAssemblyPart, AudioEncodingSettings
from pandrator.web.database import Database as WebDatabase
from pandrator.web.jobs import JobQueue
from pandrator.web.media_process import MediaProcessError, MediaProcessResult
from pandrator.web.models import Artifact
from pandrator.web.video_previews import VideoInfo, VideoPreviewUnsupported

NEEDS_NATIVE_MEDIA = pytest.mark.skipif(
    not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
    reason="Local media qualification requires ffmpeg and ffprobe.",
)


@dataclass(frozen=True)
class _PreparedRegistration:
    relative_path: str


def _write_local_wav(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_rate = 16000
    samples = [0] * sample_rate
    samples.extend(
        round(math.sin(2 * math.pi * 440 * index / sample_rate) * 12000)
        for index in range(sample_rate // 2)
    )
    with wave.open(str(path), "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(sample_rate)
        writer.writeframes(struct.pack(f"<{len(samples)}h", *samples))


def _recording_runner(monkeypatch, *modules):
    real_runner = media_process.run_media_process
    commands: list[tuple[str, ...]] = []

    def record(command, *args, **kwargs):
        commands.append(tuple(os.fspath(argument) for argument in command))
        return real_runner(command, *args, **kwargs)

    for module in modules:
        monkeypatch.setattr(module, "run_media_process", record)
    return commands


def _assert_local_file_options(command: tuple[str, ...], path: Path, *, ffmpeg: bool) -> None:
    path_index = command.index(str(path))
    policy_size = len(LOCAL_MEDIA_INPUT_OPTIONS)
    if ffmpeg:
        assert command[path_index - 1] == "-i"
        assert command[path_index - policy_size - 1 : path_index - 1] == (LOCAL_MEDIA_INPUT_OPTIONS)
        return

    option_index = command.index(LOCAL_MEDIA_INPUT_OPTIONS[0])
    assert command[option_index : option_index + policy_size] == LOCAL_MEDIA_INPUT_OPTIONS
    assert option_index + policy_size <= path_index


@NEEDS_NATIVE_MEDIA
def test_local_wav_probe_silence_waveform_and_preview_use_restricted_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    source = root / "sessions" / "fixture" / "source audio.wav"
    _write_local_wav(source)
    commands = _recording_runner(monkeypatch, media_process, waveform)

    info = media_process.probe_audio_stream(source)
    assert info.sample_rate_hz == 16000
    assert info.channels == 1

    audible_start = media_process.find_first_audible_seconds(source, pre_roll_seconds=0.5)
    assert 0.4 < audible_start < 0.7

    peaks = waveform.generate_waveform_peaks(source, max_points=128)
    assert peaks.channels == 1
    assert 0.3 < max(peaks.points) < 0.5

    source_artifact = SimpleNamespace(
        id="source-audio",
        relative_path="sessions/fixture/source audio.wav",
        session_id="fixture",
        workspace_id="workspace",
        project_id=None,
        content_hash=hashlib.sha256(source.read_bytes()).hexdigest(),
        size_bytes=source.stat().st_size,
    )
    monkeypatch.setattr(
        workflow_audio_preview,
        "require_media_execution_artifact",
        lambda *_args, **_kwargs: source_artifact,
    )
    monkeypatch.setattr(
        workflow_audio_preview.MediaSourceSnapshot,
        "from_artifact",
        classmethod(lambda _cls, _artifact: object()),
    )
    database = SimpleNamespace(
        snapshot_session=lambda: nullcontext(object()),
        immediate_session=lambda: nullcontext(object()),
    )
    artifacts = SimpleNamespace(
        database=database,
        prepare_registration=lambda _path, *, settings: _PreparedRegistration(
            relative_path="temporary.mp3"
        ),
        register_in_session=lambda *_args, **_kwargs: SimpleNamespace(id="preview-audio"),
    )
    identifiers = iter(("final", "temporary"))
    context = workflow_audio_preview.AudioPreviewContext(
        paths=DataPaths(root),
        artifacts=cast(ArtifactService, artifacts),
        resolve_input=cast(
            Callable[[str], tuple[Artifact, Path]],
            lambda _artifact_id: (source_artifact, source),
        ),
        session_dir=lambda _session_id: root / "sessions" / "fixture" / "previews",
        new_id=lambda: next(identifiers),
    )
    result = workflow_audio_preview.generate_audio_preview(
        context,
        {"source_artifact_id": source_artifact.id},
        lambda *_args: None,
        threading.Event(),
    )

    assert result == {"artifact_id": "preview-audio", "source_artifact_id": "source-audio"}
    preview_path = (
        root / "sessions" / "fixture" / "previews" / ("audio-preview-v1-source-audio-final.mp3")
    )
    assert preview_path.is_file()

    probe_commands = [command for command in commands if str(source) in command]
    ffprobe_commands = [command for command in probe_commands if "-select_streams" in command]
    ffmpeg_commands = [command for command in probe_commands if "-i" in command]
    assert len(ffprobe_commands) == 2
    assert len(ffmpeg_commands) == 3
    for command in ffprobe_commands:
        _assert_local_file_options(command, source, ffmpeg=False)
    for command in ffmpeg_commands:
        _assert_local_file_options(command, source, ffmpeg=True)


@NEEDS_NATIVE_MEDIA
def test_other_shared_native_file_inputs_use_restricted_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.wav"
    _write_local_wav(source)
    commands = _recording_runner(
        monkeypatch,
        media_process,
        voice_reference_trim,
        sound_mix_manifest,
        video_previews,
        audio_assembly,
    )

    voice_info = voice_reference_trim.probe_voice_reference_audio(source)
    trim = voice_reference_trim.validate_voice_reference_trim(0.1, 0.5, voice_info)
    trimmed = tmp_path / "trimmed.wav"
    voice_reference_trim.trim_voice_reference_audio(source, trimmed, trim)
    assert trimmed.is_file()

    rate, channels, frames = sound_mix_manifest._probe_audio_geometry(source)
    assert (rate, channels) == (16000, 1)
    assert frames > 0

    encoding = AudioEncodingSettings(sample_rate_hz=16000, channels=1)
    normalized = tmp_path / "normalized.wav"
    audio_assembly._normalize_part(
        AudioAssemblyPart(source, expected_duration_ms=1500),
        normalized,
        encoding,
        ffmpeg_executable=shutil.which("ffmpeg") or "ffmpeg",
        cancel_event=None,
    )
    assert normalized.is_file()
    encoding_command = audio_assembly._encoding_command(
        shutil.which("ffmpeg") or "ffmpeg", normalized, tmp_path / "encoded.wav", encoding
    )
    _assert_local_file_options(tuple(encoding_command), normalized, ffmpeg=True)

    with pytest.raises(VideoPreviewUnsupported):
        video_previews.probe_video(source)

    ffprobe_commands = [
        command for command in commands if command[-1] == str(source) and "-i" not in command
    ]
    ffmpeg_commands = [
        command for command in commands if "-i" in command and str(source) in command
    ]
    assert len(ffprobe_commands) == 3
    assert len(ffmpeg_commands) == 2
    for command in ffprobe_commands:
        _assert_local_file_options(command, source, ffmpeg=False)
    for command in ffmpeg_commands:
        _assert_local_file_options(command, source, ffmpeg=True)


@NEEDS_NATIVE_MEDIA
def test_hls_and_concat_inputs_are_rejected_by_the_audio_probe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    control = tmp_path / "control.wav"
    _write_local_wav(control)
    child = tmp_path / "private.ts"
    media_process.run_media_process(
        [
            media_process.resolve_ffmpeg_executable(),
            "-nostdin",
            "-v",
            "error",
            *LOCAL_MEDIA_INPUT_OPTIONS,
            "-i",
            str(control),
            "-c:a",
            "aac",
            "-f",
            "mpegts",
            str(child),
        ]
    )
    playlist = tmp_path / "remote.m3u8"
    playlist.write_text(
        "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:2\n"
        "#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:1.5,\nprivate.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    concat = tmp_path / "nested.ffconcat"
    concat.write_text("ffconcat version 1.0\nfile 'private.ts'\n", encoding="utf-8")
    commands = _recording_runner(monkeypatch, media_process)

    for path in (playlist, concat):
        with pytest.raises(MediaProcessError):
            media_process.probe_audio_stream(path)

    assert len(commands) == 2
    for command, path in zip(commands, (playlist, concat), strict=True):
        _assert_local_file_options(command, path, ffmpeg=False)


def test_video_preview_generation_restricts_its_source_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "data"
    root.mkdir()
    source_path = root / "source media.mkv"
    source_path.write_bytes(b"synthetic local preview source")
    source_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
    source = SimpleNamespace(
        id="source-video",
        relative_path="source media.mkv",
        session_id=None,
        workspace_id="workspace",
        project_id=None,
        content_hash=source_hash,
        state="current",
    )

    class Session:
        def expunge(self, _artifact) -> None:
            pass

        def get(self, _model, _identifier):
            return source

        def scalars(self, _statement):
            return SimpleNamespace(all=lambda: [])

    class FakeDatabase:
        def snapshot_session(self):
            return nullcontext(Session())

        def immediate_session(self):
            return nullcontext(Session())

    commands: list[tuple[str, ...]] = []

    def render(command, **_kwargs):
        arguments = tuple(os.fspath(argument) for argument in command)
        commands.append(arguments)
        Path(arguments[-1]).write_bytes(b"synthetic preview output")
        return MediaProcessResult(returncode=0)

    info = VideoInfo(
        duration_seconds=1.0,
        width=320,
        height=240,
        frame_rate=24.0,
        has_audio=True,
        video_codec="h264",
        pixel_format="yuv420p",
        audio_codec="aac",
    )
    monkeypatch.setattr(video_previews, "run_media_process", render)
    monkeypatch.setattr(video_previews, "probe_video", lambda *_args, **_kwargs: info)
    monkeypatch.setattr(
        video_previews,
        "require_media_execution_artifact",
        lambda *_args, **_kwargs: source,
    )
    monkeypatch.setattr(
        video_previews.MediaSourceSnapshot,
        "from_artifact",
        classmethod(lambda _cls, _artifact: object()),
    )
    artifacts = SimpleNamespace(
        prepare_registration=lambda _path: _PreparedRegistration(relative_path="temporary.mp4"),
        register_in_session=lambda *_args, **_kwargs: SimpleNamespace(id="preview-video"),
    )
    service = video_previews.VideoPreviewService(
        cast(WebDatabase, FakeDatabase()),
        DataPaths(root),
        cast(ArtifactService, artifacts),
        cast(JobQueue, object()),
    )

    result = service.generate(
        {"source_content_hash": source_hash, "preview_version": video_previews.PREVIEW_VERSION},
        lambda *_args: None,
        threading.Event(),
    )

    assert result == {"artifact_id": "preview-video", "source_artifact_id": "source-video"}
    assert len(commands) == 1
    _assert_local_file_options(commands[0], source_path, ffmpeg=True)
