"""Command-builder contracts for restricted local media inputs."""

from __future__ import annotations

import os
import threading
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from pydub import AudioSegment

from pandrator.logic import audio_cpp_processing, book_timing, book_video, dubbing_handler
from pandrator.logic.book_video import BookLayout
from pandrator.logic.dubbing import audio_sync, manual_timing, transcription, video_muxing
from pandrator.logic.media_input_policy import LOCAL_MEDIA_INPUT_OPTIONS

_MEDIA_A = "source audio 'one' (original).mov"
_MEDIA_B = "speech audio 'two' \"final\".wav"
_SUBTITLE_A = "English subtitles 'one'.srt"
_SUBTITLE_B = 'German subtitles "two".srt'
_OUTPUT = "rendered output 'final'.mp4"
_AUDIO_OUTPUT = "normalized output 'final'.wav"


def _assert_local_inputs(command: Sequence[str], inputs: Sequence[str]) -> None:
    """Assert options are attached to every expected argv input, in order."""
    assert command.count("-i") == len(inputs)
    cursor = 0
    options = list(LOCAL_MEDIA_INPUT_OPTIONS)
    for expected_path in inputs:
        input_index = command.index("-i", cursor)
        assert list(command[input_index - len(options) : input_index]) == options
        assert command[input_index + 1] == expected_path
        assert command.count(expected_path) == 1
        cursor = input_index + 2


def _assert_output_suffix(command: Sequence[str], expected: Sequence[str]) -> None:
    assert list(command[-len(expected) :]) == list(expected)


@pytest.mark.parametrize(
    ("command_factory", "inputs", "output_suffix"),
    [
        pytest.param(
            lambda: audio_sync.build_extract_original_audio_command(_MEDIA_A, _AUDIO_OUTPUT),
            [_MEDIA_A],
            ["-acodec", "pcm_s16le", "-ar", "44100", "-ac", "2", _AUDIO_OUTPUT],
            id="audio-sync-extract",
        ),
        pytest.param(
            lambda: audio_sync.build_volume_analysis_command(_MEDIA_A),
            [_MEDIA_A],
            ["-sn", "-dn", "-f", "null", os.devnull],
            id="audio-sync-analysis",
        ),
        pytest.param(
            lambda: audio_sync.build_amplify_audio_command(_MEDIA_A, _AUDIO_OUTPUT, 3.5),
            [_MEDIA_A],
            ["-af", "volume=3.5dB", _AUDIO_OUTPUT],
            id="audio-sync-amplify",
        ),
        pytest.param(
            lambda: audio_sync.build_normalize_dubbed_audio_command(_MEDIA_A, _AUDIO_OUTPUT),
            [_MEDIA_A],
            ["-ar", "48000", "-ac", "2", _AUDIO_OUTPUT],
            id="audio-sync-normalize",
        ),
        pytest.param(
            lambda: audio_sync.build_mix_audio_command(_MEDIA_A, _MEDIA_B, _AUDIO_OUTPUT),
            [_MEDIA_A, _MEDIA_B],
            ["-ar", "48000", "-ac", "2", _AUDIO_OUTPUT],
            id="audio-sync-mix-audio",
        ),
        pytest.param(
            lambda: audio_sync.build_mix_preview_command(_MEDIA_A, _MEDIA_B, _AUDIO_OUTPUT),
            [_MEDIA_A, _MEDIA_B],
            ["-c:a", "pcm_s16le", _AUDIO_OUTPUT],
            id="audio-sync-mix-preview",
        ),
        pytest.param(
            lambda: audio_sync.build_mix_video_audio_command(_MEDIA_A, _MEDIA_B, _OUTPUT),
            [_MEDIA_A, _MEDIA_B],
            ["-movflags", "+faststart", "-shortest", _OUTPUT],
            id="audio-sync-mix-video",
        ),
        pytest.param(
            lambda: audio_sync.build_mux_mixed_audio_command(_MEDIA_A, _MEDIA_B, _OUTPUT),
            [_MEDIA_A, _MEDIA_B],
            ["-movflags", "+faststart", "-shortest", _OUTPUT],
            id="audio-sync-mux",
        ),
        pytest.param(
            lambda: video_muxing.build_video_transcode_command(_MEDIA_A, _OUTPUT),
            [_MEDIA_A],
            ["-movflags", "+faststart", _OUTPUT],
            id="video-transcode",
        ),
        pytest.param(
            lambda: video_muxing.build_web_optimized_remux_command(_MEDIA_A, _OUTPUT),
            [_MEDIA_A],
            ["-movflags", "+faststart", _OUTPUT],
            id="video-remux",
        ),
        pytest.param(
            lambda: video_muxing.build_removal_only_video_command(
                _MEDIA_A, _OUTPUT, [(0, 1000)], has_audio=True
            ),
            [_MEDIA_A],
            ["-movflags", "+faststart", _OUTPUT],
            id="video-removal",
        ),
        pytest.param(
            lambda: video_muxing.build_add_subtitles_command(
                _MEDIA_A, _SUBTITLE_A, _OUTPUT, subtitle_mode="soft"
            ),
            [_MEDIA_A, _SUBTITLE_A],
            ["-movflags", "+faststart", _OUTPUT],
            id="soft-subtitle-inputs",
        ),
        pytest.param(
            lambda: video_muxing.build_add_subtitles_command(
                _MEDIA_A, _SUBTITLE_A, _OUTPUT, subtitle_mode="burned"
            ),
            [_MEDIA_A],
            ["-movflags", "+faststart", _OUTPUT],
            id="burned-subtitle-video-input",
        ),
        pytest.param(
            lambda: video_muxing.build_replace_video_audio_command(_MEDIA_A, _MEDIA_B, _OUTPUT),
            [_MEDIA_A, _MEDIA_B],
            ["-movflags", "+faststart", "-shortest", _OUTPUT],
            id="replace-video-audio",
        ),
        pytest.param(
            lambda: video_muxing.build_video_tail_extension_command(_MEDIA_A, _OUTPUT, 1.25),
            [_MEDIA_A],
            ["-movflags", "+faststart", _OUTPUT],
            id="video-tail-extension",
        ),
        pytest.param(
            lambda: video_muxing.build_multi_soft_subtitle_command(
                _MEDIA_A,
                [
                    {"path": _SUBTITLE_A, "language": "en", "title": "English"},
                    {"path": _SUBTITLE_B, "language": "de", "title": "German"},
                ],
                _OUTPUT,
            ),
            [_MEDIA_A, _SUBTITLE_A, _SUBTITLE_B],
            ["-movflags", "+faststart", _OUTPUT],
            id="multi-soft-subtitle-inputs",
        ),
        pytest.param(
            lambda: manual_timing.build_segment_preview_command(
                _MEDIA_A, _AUDIO_OUTPUT, start_ms=200, end_ms=1200
            ),
            [_MEDIA_A],
            ["-acodec", "pcm_s16le", _AUDIO_OUTPUT],
            id="manual-timing-preview",
        ),
    ],
)
def test_media_command_builders_restrict_each_file_input(
    command_factory: Callable[[], list[str]],
    inputs: list[str],
    output_suffix: list[str],
) -> None:
    command = command_factory()

    _assert_local_inputs(command, inputs)
    _assert_output_suffix(command, output_suffix)


def test_audio_sync_speedup_commands_restrict_inputs(tmp_path, monkeypatch):
    captured: list[list[str]] = []
    marker = object()

    class FakeAudioSegment:
        def export(self, _path: Path, *, format: str) -> None:
            assert format == "wav"

    monkeypatch.setattr(AudioSegment, "from_wav", lambda _path: marker)

    def capture_audio_segment(command, **_kwargs):
        captured.append(list(command))
        return SimpleNamespace()

    result = audio_sync._speed_up_audio_segment(
        FakeAudioSegment(),
        1.25,
        tmp_path,
        ffmpeg_executable="ffmpeg-test",
        run_func=capture_audio_segment,
    )
    assert result is marker
    _assert_local_inputs(captured[0], [str(tmp_path / "temp_speedup_input.wav")])
    assert captured[0][-1] == str(tmp_path / "temp_speedup_output.wav")

    from pandrator.web import media_process

    streaming_source = tmp_path / _MEDIA_A
    streaming_output = tmp_path / "speedup output.wav"

    def capture_streaming(command, **_kwargs):
        captured.append(list(command))

    monkeypatch.setattr(media_process, "run_media_process", capture_streaming)
    audio_sync._speed_up_wav_streaming(
        streaming_source,
        streaming_output,
        1.25,
        sample_rate_hz=16000,
        channels=1,
        ffmpeg_executable="ffmpeg-test",
        cancel_event=None,
    )
    _assert_local_inputs(captured[1], [str(streaming_source)])
    assert captured[1][-1] == str(streaming_output)


def test_audio_sync_ffprobe_restricts_positional_file_input():
    captured: list[list[str]] = []

    def capture(command, **_kwargs):
        captured.append(list(command))
        return SimpleNamespace(stdout="0\n")

    assert audio_sync.media_has_audio_stream(
        _MEDIA_A, ffprobe_executable="ffprobe-test", run_func=capture
    )
    command = captured[0]
    assert command[-len(LOCAL_MEDIA_INPUT_OPTIONS) - 1 : -1] == list(LOCAL_MEDIA_INPUT_OPTIONS)
    assert command[1:9] == [
        "-v",
        "error",
        "-select_streams",
        "a:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
    ]
    assert command[-1] == _MEDIA_A
    assert command.count(_MEDIA_A) == 1


def test_transcription_excerpt_and_vocal_isolation_commands_restrict_inputs(tmp_path, monkeypatch):
    captured: list[list[str]] = []

    def capture(command, **_kwargs):
        captured.append(list(command))
        return SimpleNamespace(stderr=None)

    transcription.extract_audio(_MEDIA_A, str(tmp_path), "extract 'one'", run_func=capture)
    _assert_local_inputs(captured[-1], [_MEDIA_A])
    _assert_output_suffix(
        captured[-1],
        [
            "-ac",
            "1",
            "-af",
            "aresample,loudnorm",
            "-y",
            str(tmp_path / "extract 'one'.wav"),
        ],
    )

    transcription.extract_audio_excerpt(
        _MEDIA_B,
        str(tmp_path),
        "excerpt 'two'",
        100,
        1100,
        run_func=capture,
    )
    _assert_local_inputs(captured[-1], [_MEDIA_B])
    _assert_output_suffix(
        captured[-1],
        [
            "-ac",
            "1",
            "-af",
            "aresample,loudnorm",
            "-y",
            str(tmp_path / "excerpt 'two'.wav"),
        ],
    )

    source = tmp_path / _MEDIA_A
    session = tmp_path / "isolation session"

    def fake_isolate(_source, raw_output, _settings, **_kwargs):
        Path(raw_output).write_bytes(b"synthetic wav placeholder")
        return {"status": "isolated"}

    monkeypatch.setattr(audio_cpp_processing, "isolate_vocals", fake_isolate)
    monkeypatch.setattr(transcription, "_vocal_isolation_choice", lambda _settings: "contract")
    transcription.apply_vocal_isolation(
        source,
        session,
        "take 'three'",
        {"transcription_vocal_isolation": "bs_roformer"},
        ffmpeg_executable="ffmpeg-test",
        run_func=capture,
    )
    expected_raw = session / "take 'three'_vocals_raw.wav"
    _assert_local_inputs(captured[-1], [str(expected_raw)])
    assert captured[-1][-1] == str(session / "take 'three'_vocals.wav")


def test_audio_cpp_normalization_command_restricts_input(tmp_path, monkeypatch):
    source = tmp_path / _MEDIA_A
    source.write_bytes(b"synthetic non-WAV media")
    temporary = tmp_path / "normalization"
    captured: list[list[str]] = []

    def fake_run_child(command, *_args, **_kwargs):
        captured.append(list(command))
        target = Path(command[-1])
        target.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(target), "wb") as normalized:
            normalized.setnchannels(1)
            normalized.setsampwidth(2)
            normalized.setframerate(16000)
            normalized.writeframes(b"\0\0")

    monkeypatch.setattr(audio_cpp_processing, "_require_ffmpeg", lambda: "ffmpeg-test")
    monkeypatch.setattr(audio_cpp_processing, "_run_child", fake_run_child)
    normalized, _info = audio_cpp_processing._normalize_wav(
        source,
        temporary,
        rate=16000,
        channels=1,
        settings={},
        cancel_event=None,
        run_func=None,
        label="contract-input",
    )

    _assert_local_inputs(captured[0], [str(source)])
    assert normalized == temporary / "contract-input-16000hz.wav"
    assert captured[0][-1] == str(normalized)


def test_book_alignment_normalization_command_restricts_input(tmp_path, monkeypatch):
    captured: list[list[str]] = []

    class StopAfterCapture(Exception):
        pass

    def capture(command, **_kwargs):
        captured.append(list(command))
        raise StopAfterCapture

    monkeypatch.setattr(book_timing, "run_cancellable", capture)
    source = tmp_path / _MEDIA_A
    with pytest.raises(StopAfterCapture):
        book_timing.align_book_text(
            source,
            "A short transcript.",
            {"language": "English"},
            tmp_path / "alignment work",
            threading.Event(),
        )

    _assert_local_inputs(captured[0], [str(source)])
    _assert_output_suffix(
        captured[0],
        [
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(tmp_path / "alignment work" / "book-alignment.wav"),
        ],
    )


def test_book_video_keeps_lavfi_literal_and_restricts_audio_input(tmp_path, monkeypatch):
    font_path = tmp_path / "font.ttf"
    font_path.write_bytes(b"font placeholder")
    captured: list[list[str]] = []

    def capture(command, **_kwargs):
        captured.append(list(command))
        return SimpleNamespace()

    monkeypatch.setattr(book_video, "write_book_ass", lambda *_args: None)
    monkeypatch.setattr(book_video, "run_cancellable", capture)
    monkeypatch.setattr(
        dubbing_handler, "resolve_ffmpeg_for_burned_subtitles", lambda: "ffmpeg-test"
    )
    book_video.render_book_video(
        tmp_path / _MEDIA_A,
        [],
        tmp_path / _OUTPUT,
        {},
        layout=cast(
            BookLayout,
            SimpleNamespace(width=800, height=450, font_path=font_path),
        ),
        title="Contract",
        start_ms=250,
        duration_ms=1500,
        progress=lambda *_args: None,
        cancel_event=threading.Event(),
    )

    command = captured[0]
    lavfi_index = command.index("-f")
    expected_color = f"color=c={book_video.BOOK_DEFAULTS['book_background']}:s=800x450:r=24:d=1.500"
    assert command[lavfi_index : lavfi_index + 4] == [
        "-f",
        "lavfi",
        "-i",
        expected_color,
    ]
    assert command.count("-i") == 2
    audio_input_index = command.index("-i", lavfi_index + 4)
    options = list(LOCAL_MEDIA_INPUT_OPTIONS)
    assert list(command[audio_input_index - len(options) : audio_input_index]) == options
    assert command[audio_input_index + 1] == str(tmp_path / _MEDIA_A)
    assert command[-1] == str(tmp_path / _OUTPUT)


def test_manual_correction_extraction_command_restricts_input(tmp_path, monkeypatch):
    video = tmp_path / _MEDIA_A
    video.write_bytes(b"synthetic video placeholder")
    session = tmp_path / "manual correction"
    captured: list[list[str]] = []

    class FakeProcess:
        stdout = None
        returncode = 0

        def wait(self):
            return 0

    def fake_popen(command, **_kwargs):
        captured.append(list(command))
        Path(command[-1]).write_bytes(b"synthetic audio placeholder")
        return FakeProcess()

    monkeypatch.setattr(dubbing_handler.subprocess, "Popen", fake_popen)
    output = dubbing_handler.extract_audio_for_manual_correction(str(video), str(session))

    _assert_local_inputs(captured[0], [str(video)])
    _assert_output_suffix(
        captured[0],
        [
            "-ac",
            "1",
            "-af",
            "aresample,loudnorm",
            str(output),
        ],
    )


def test_video_transcode_keeps_input_device_before_local_policy():
    command = video_muxing.build_video_transcode_command(
        _MEDIA_A,
        _OUTPUT,
        video_encoder="h264_vaapi",
        hardware_device="/dev/dri/renderD-test",
    )
    device_index = command.index("-vaapi_device")
    input_index = command.index("-i")
    options = list(LOCAL_MEDIA_INPUT_OPTIONS)

    assert command[device_index + 1] == "/dev/dri/renderD-test"
    assert device_index < input_index - len(options)
    assert list(command[input_index - len(options) : input_index]) == options
    assert command[input_index + 1] == _MEDIA_A
    assert command[-1] == _OUTPUT


def test_local_subtitle_demuxer_does_not_broaden_protocols_or_playlists():
    options = list(LOCAL_MEDIA_INPUT_OPTIONS)
    assert options[options.index("-protocol_whitelist") + 1] == "file,pipe"
    formats = set(options[options.index("-format_whitelist") + 1].split(","))
    assert {"srt", "subrip", "webvtt", "ass"} <= formats
    assert not {"concat", "hls", "dash"} & formats
