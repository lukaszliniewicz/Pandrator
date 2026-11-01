from __future__ import annotations

import subprocess
import threading
import wave
from pathlib import Path
from unittest.mock import Mock

import pytest

from pandrator.logic import book_timing
from pandrator.web import export_video_commands
from pandrator.web.export_video_commands import VideoEncodingOptions

_WAVE_OPEN = wave.open


def _write_mono_pcm(path: Path, *, seconds: int = 1) -> Path:
    with _WAVE_OPEN(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\0\0" * 16000 * seconds)
    return path


@pytest.fixture
def book_case(tmp_path: Path):
    source = _write_mono_pcm(tmp_path / "source.wav")
    return source, tmp_path / "book-work", threading.Event()


def _video_options() -> VideoEncodingOptions:
    return VideoEncodingOptions("ffmpeg", "libx264", 19, "fast", "720p", "copy", "256k")


def _render_video_mode(mode: str, *, source: Path, cancel_event, progress, before_io):
    options = _video_options()
    destination = source.with_name(f"{mode}-output.mp4")
    if mode == "soft":
        return export_video_commands.render_soft_subtitle_video(
            source,
            [{"path": str(source.with_suffix(".srt")), "language": "en", "title": "English"}],
            destination,
            options,
            transcode_video=True,
            progress=progress,
            cancel_event=cancel_event,
            before_io=before_io,
        )
    if mode == "burned":
        return export_video_commands.render_burned_subtitle_video(
            source,
            source.with_suffix(".srt"),
            destination,
            options,
            language="en",
            progress=progress,
            cancel_event=cancel_event,
            before_io=before_io,
        )
    if mode == "transcoded":
        return export_video_commands.render_transcoded_video(
            source,
            destination,
            options,
            progress=progress,
            cancel_event=cancel_event,
            before_io=before_io,
        )
    return export_video_commands.render_web_video(
        source,
        destination,
        options,
        tail_extension_ms=0,
        progress=progress,
        cancel_event=cancel_event,
        before_io=before_io,
    )


@pytest.mark.parametrize("mode", ["soft", "burned", "transcoded", "web"])
def test_video_renderers_gate_each_actual_launch(mode, tmp_path, monkeypatch):
    source = _write_mono_pcm(tmp_path / "synthetic-source.wav")
    operations = []
    launch = Mock(side_effect=lambda *_args, **_kwargs: operations.append("launch"))
    monkeypatch.setattr(export_video_commands, "run_cancellable", launch)
    monkeypatch.setattr(
        "pandrator.web.capabilities.ffmpeg_video_encoder_ids",
        lambda _executable: {"libx264"},
    )
    monkeypatch.setattr(
        "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles",
        lambda: "ffmpeg-libass",
    )

    def before_io():
        operations.append("guard")

    _render_video_mode(
        mode,
        source=source,
        cancel_event=threading.Event(),
        progress=lambda _value, _detail: None,
        before_io=before_io,
    )

    assert operations == ["guard", "launch"]
    launch.assert_called_once()


def test_run_video_command_propagates_guard_exception_without_launch(monkeypatch):
    launch = Mock()
    monkeypatch.setattr(export_video_commands, "run_cancellable", launch)
    denial = subprocess.CalledProcessError(17, ["authority-check"], stderr="denied")

    def before_io():
        raise denial

    with pytest.raises(subprocess.CalledProcessError) as caught:
        export_video_commands.run_video_command(
            ["ffmpeg", "-i", "synthetic.wav"],
            threading.Event(),
            before_io=before_io,
        )

    assert caught.value is denial
    launch.assert_not_called()


def test_first_video_guard_denial_does_not_start_or_fallback(monkeypatch, tmp_path):
    source = _write_mono_pcm(tmp_path / "synthetic-source.wav")
    launch = Mock()
    encoder_probe = Mock(return_value={"libx264"})
    monkeypatch.setattr(export_video_commands, "run_cancellable", launch)
    monkeypatch.setattr("pandrator.web.capabilities.ffmpeg_video_encoder_ids", encoder_probe)
    denial = subprocess.CalledProcessError(18, ["authority-check"], stderr="denied")

    def before_io():
        raise denial

    with pytest.raises(subprocess.CalledProcessError) as caught:
        export_video_commands.render_web_video(
            source,
            source.with_name("web-output.mp4"),
            _video_options(),
            tail_extension_ms=0,
            progress=lambda _value, _detail: None,
            cancel_event=threading.Event(),
            before_io=before_io,
        )

    assert caught.value is denial
    launch.assert_not_called()
    encoder_probe.assert_not_called()


def test_late_video_guard_denial_prevents_fallback_and_preserves_exception(monkeypatch, tmp_path):
    source = _write_mono_pcm(tmp_path / "synthetic-source.wav")
    launch_error = subprocess.CalledProcessError(1, ["ffmpeg"], stderr="copy failed")
    denial = subprocess.CalledProcessError(19, ["authority-check"], stderr="revoked")
    launch = Mock(side_effect=launch_error)
    monkeypatch.setattr(export_video_commands, "run_cancellable", launch)
    monkeypatch.setattr(
        "pandrator.web.capabilities.ffmpeg_video_encoder_ids",
        lambda _executable: {"libx264"},
    )
    guard_calls = []

    def before_io():
        guard_calls.append(None)
        if len(guard_calls) == 2:
            raise denial

    with pytest.raises(subprocess.CalledProcessError) as caught:
        export_video_commands.render_web_video(
            source,
            source.with_name("web-output.mp4"),
            _video_options(),
            tail_extension_ms=0,
            progress=lambda _value, _detail: None,
            cancel_event=threading.Event(),
            before_io=before_io,
        )

    assert caught.value is denial
    assert len(guard_calls) == 2
    launch.assert_called_once()


def _mock_normalizer(event_log: list[str], cancel_event: threading.Event):
    def normalize(command, **kwargs):
        assert kwargs["cancel_event"] is cancel_event
        assert kwargs["check"] and kwargs["capture_output"]
        event_log.append("ffmpeg")
        _write_mono_pcm(Path(command[-1]))

    return normalize


def _runner(event_log: list[str], *, transcript: Path | None = None):
    def align(normalized, transcript_path, _output, _options, **_kwargs):
        event_log.append("ctc")
        assert normalized.is_file()
        assert transcript_path.read_text(encoding="utf-8") == "hello"
        if transcript is not None:
            assert transcript_path == transcript
        return [{"word": "hello", "start": 0.1, "end": 0.7}]

    return align


def test_book_alignment_guards_each_io_boundary_and_runner(book_case, monkeypatch):
    source, work, cancel_event = book_case
    events = []
    monkeypatch.setattr(
        book_timing,
        "run_cancellable",
        _mock_normalizer(events, cancel_event),
    )
    original_wave_open = book_timing.wave.open

    def observed_wave_open(*args, **kwargs):
        events.append("wav-open")
        return original_wave_open(*args, **kwargs)

    monkeypatch.setattr(book_timing.wave, "open", observed_wave_open)

    def before_io():
        events.append("guard")

    result = book_timing.align_book_text(
        source,
        "hello",
        {"language": "en"},
        work,
        cancel_event,
        runner=_runner(events, transcript=work / "book-alignment.txt"),
        before_io=before_io,
    )

    assert result.words[0].text == "hello"
    assert events == [
        "guard",
        "guard",
        "ffmpeg",
        "guard",
        "wav-open",
        "guard",
        "guard",
        "ctc",
    ]


def test_book_initial_guard_denial_prevents_work_directory_creation(book_case, monkeypatch):
    source, work, cancel_event = book_case
    normalize = Mock()
    runner = Mock()
    monkeypatch.setattr(book_timing, "run_cancellable", normalize)
    denial = PermissionError("initial book I/O denied")

    def before_io():
        raise denial

    with pytest.raises(PermissionError) as caught:
        book_timing.align_book_text(
            source,
            "hello",
            {"language": "en"},
            work,
            cancel_event,
            runner=runner,
            before_io=before_io,
        )

    assert caught.value is denial
    assert not work.exists()
    normalize.assert_not_called()
    runner.assert_not_called()


def test_book_normalization_guard_denial_prevents_ffmpeg_launch(book_case, monkeypatch):
    source, work, cancel_event = book_case
    normalize = Mock()
    runner = Mock()
    monkeypatch.setattr(book_timing, "run_cancellable", normalize)
    guard_calls = []
    denial = PermissionError("normalization denied")

    def before_io():
        guard_calls.append(None)
        if len(guard_calls) == 2:
            raise denial

    with pytest.raises(PermissionError) as caught:
        book_timing.align_book_text(
            source,
            "hello",
            {"language": "en"},
            work,
            cancel_event,
            runner=runner,
            before_io=before_io,
        )

    assert caught.value is denial
    assert len(guard_calls) == 2
    assert work.is_dir()
    assert not (work / "book-alignment.wav").exists()
    normalize.assert_not_called()
    runner.assert_not_called()


def test_book_wav_guard_denial_prevents_wav_open_and_runner(book_case, monkeypatch):
    source, work, cancel_event = book_case
    events = []
    monkeypatch.setattr(
        book_timing,
        "run_cancellable",
        _mock_normalizer(events, cancel_event),
    )
    wav_open = Mock(side_effect=_WAVE_OPEN)
    monkeypatch.setattr(book_timing.wave, "open", wav_open)
    runner = Mock()
    guard_calls = []
    denial = PermissionError("normalized WAV read denied")

    def before_io():
        guard_calls.append(None)
        if len(guard_calls) == 3:
            raise denial

    with pytest.raises(PermissionError) as caught:
        book_timing.align_book_text(
            source,
            "hello",
            {"language": "en"},
            work,
            cancel_event,
            runner=runner,
            before_io=before_io,
        )

    assert caught.value is denial
    assert len(guard_calls) == 3
    assert (work / "book-alignment.wav").is_file()
    wav_open.assert_not_called()
    runner.assert_not_called()


@pytest.mark.parametrize(("denial_call", "transcript_exists"), [(4, False), (5, True)])
def test_book_private_text_and_runner_are_guarded(
    book_case, monkeypatch, denial_call, transcript_exists
):
    source, work, cancel_event = book_case
    events = []
    monkeypatch.setattr(
        book_timing,
        "run_cancellable",
        _mock_normalizer(events, cancel_event),
    )
    runner = Mock(side_effect=_runner(events))
    guard_calls = []
    denial = PermissionError("transcript or aligner access denied")

    def before_io():
        guard_calls.append(None)
        if len(guard_calls) == denial_call:
            raise denial

    with pytest.raises(PermissionError) as caught:
        book_timing.align_book_text(
            source,
            "hello",
            {"language": "en"},
            work,
            cancel_event,
            runner=runner,
            before_io=before_io,
        )

    assert caught.value is denial
    assert len(guard_calls) == denial_call
    assert (work / "book-alignment.txt").exists() is transcript_exists
    runner.assert_not_called()
