"""Concrete book and soundtrack boundaries propagate authority denial."""

from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock

import pytest

from pandrator.logic import book_video
from pandrator.web import soundtrack_export


@pytest.mark.parametrize("deny_at", [1, 3, 5])
def test_book_render_denial_prevents_the_next_file_or_process_boundary(
    tmp_path, monkeypatch, deny_at
):
    font = tmp_path / "synthetic-font.ttf"
    font.write_bytes(b"Synthetic callback fixture, no font parsing")
    layout = cast(book_video.BookLayout, SimpleNamespace(width=320, height=200, font_path=font))
    command = Mock(side_effect=AssertionError("Denied book render launched a process"))
    monkeypatch.setattr(
        "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles", lambda: "ffmpeg"
    )
    monkeypatch.setattr(book_video, "run_cancellable", command)
    monkeypatch.setattr(book_video, "write_book_ass", lambda _c, path, *_a: path.write_text("ASS"))
    denied = PermissionError("Synthetic book authority loss")
    calls = []

    def guard():
        calls.append(True)
        if len(calls) == deny_at:
            raise denied

    with pytest.raises(PermissionError) as caught:
        book_video.render_book_video(
            tmp_path / "audio.wav",
            [],
            tmp_path / "book.mp4",
            {},
            layout=layout,
            title="Book",
            start_ms=0,
            duration_ms=1000,
            progress=lambda *_a: None,
            cancel_event=threading.Event(),
            before_io=guard,
        )
    assert caught.value is denied and len(calls) == deny_at
    command.assert_not_called()
    assert not list(tmp_path.glob(".book-video-*"))
    assert not (tmp_path / "book.mp4").exists()
    assert font.is_file()


@pytest.mark.parametrize("operation", ["probe", "render"])
def test_soundtrack_denial_precedes_native_launch_and_keeps_exception_identity(
    tmp_path: Path, monkeypatch, operation
):
    denied = OSError("Synthetic soundtrack authority loss")

    def guard():
        raise denied

    launch = Mock(side_effect=AssertionError("Denied soundtrack launched a process"))
    monkeypatch.setattr(soundtrack_export.subprocess, "run", launch)
    monkeypatch.setattr(soundtrack_export.subprocess, "Popen", launch)
    with pytest.raises(OSError) as caught:
        if operation == "probe":
            soundtrack_export.probe_soundtrack_media(tmp_path / "audio.wav", before_io=guard)
        else:
            soundtrack_export.run_audio_command(["ffmpeg"], threading.Event(), before_io=guard)
    assert caught.value is denied
    launch.assert_not_called()
