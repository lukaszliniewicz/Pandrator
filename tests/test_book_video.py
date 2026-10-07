"""Measured fitting, safe ASS data and owned, cancellable video command contracts."""

import threading
from pathlib import Path
from unittest import mock

import pytest

from pandrator.logic import book_video
from pandrator.logic.book_cues import BookCue
from pandrator.logic.cancellable_process import ProcessCancelled


@pytest.fixture
def layout():
    try:
        return book_video.prepare_book_layout({}, "A short passage.", "en")
    except ValueError as error:
        pytest.skip(f"A system font is required: {error}")


def test_layout_measures_real_glyph_advances_and_whole_words(layout):
    lines = layout.fit_lines("A long passage with a comfortable amount of text. " * 2)
    assert lines and all(layout._font.getlength(line) <= layout.content_width for line in lines)
    assert layout.fit_lines("oversized" * 100) is None
    assert layout.fit_lines("One.\nTwo.") == ("One.", "Two.")


def test_ass_has_fixed_resolution_alignment_and_escaped_text(tmp_path, layout):
    text = r"Data {\pos(1,1)} is not an override."
    path = tmp_path / "book.ass"
    book_video.write_book_ass(
        [BookCue(0, 5000, text, (text,), "A chapter")], path, {}, layout, "Book"
    )
    content = path.read_text()
    assert "PlayResX: 1920" in content and "WrapStyle: 2" in content
    assert r"\{\\pos(1,1)\}" in content
    assert "Heading" in content and "A chapter" in content


@pytest.mark.parametrize(
    ("settings", "body_size", "heading_size"),
    [
        ({}, 80, 44),
        ({"book_resolution": "720p"}, 53, 29),
        ({"book_font_size": 60, "book_heading_font_size": 38}, 60, 38),
    ],
)
def test_heading_size_is_independent_and_scales_with_resolution(
    tmp_path, layout, settings, body_size, heading_size
):
    measured = book_video.prepare_book_layout(
        {**settings, "book_font_path": str(layout.font_path)}, "Text", "en"
    )
    path = tmp_path / "sized.ass"
    book_video.write_book_ass(
        [BookCue(0, 5000, "Text", ("Text",))], path, settings, measured, "Book title"
    )
    styles = {
        fields[0].removeprefix("Style: "): int(fields[2])
        for line in path.read_text().splitlines()
        if line.startswith("Style: ")
        for fields in [line.split(",")]
    }
    assert styles == {"Reading": body_size, "Heading": heading_size}


def test_render_uses_audio_range_synthetic_background_and_same_ass(tmp_path, layout):
    audio, destination = tmp_path / "audio.wav", tmp_path / "book.mp4"
    command = []

    def capture(arguments, **options):
        command.extend(arguments)
        assert options["cancel_event"] is event
        subtitle_filter = arguments[arguments.index("-vf") + 1]
        assert "subtitles=filename=" in subtitle_filter and "fontsdir=" in subtitle_filter
        assert any(path.name == "passages.ass" for path in tmp_path.rglob("*.ass"))

    event = threading.Event()
    with (
        mock.patch(
            "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles",
            return_value="ffmpeg",
        ),
        mock.patch.object(book_video, "run_cancellable", side_effect=capture),
    ):
        book_video.render_book_video(
            audio,
            [BookCue(0, 1000, "Text", ("Text",))],
            destination,
            {},
            layout=layout,
            title="Book",
            start_ms=2500,
            duration_ms=1000,
            progress=lambda *_: None,
            cancel_event=event,
        )
    assert command[command.index("-ss") + 1] == "2.500"
    assert command[command.index("-t") + 1] == "1.000"
    assert command[command.index("-pix_fmt") + 1] == "yuv420p"
    assert "libx264" in command and "aac" in command
    assert not list(tmp_path.glob(".book-video-*"))


def test_render_cancellation_reaps_only_owned_process(tmp_path, layout):
    with (
        mock.patch(
            "pandrator.logic.dubbing_handler.resolve_ffmpeg_for_burned_subtitles",
            return_value="ffmpeg",
        ),
        mock.patch.object(book_video, "run_cancellable", side_effect=ProcessCancelled("cancel")),
    ):
        with pytest.raises(InterruptedError):
            book_video.render_book_video(
                Path("audio.wav"),
                [BookCue(0, 1000, "Text")],
                tmp_path / "book.mp4",
                {},
                layout=layout,
                title="",
                start_ms=0,
                duration_ms=1000,
                progress=lambda *_: None,
                cancel_event=threading.Event(),
            )
    assert not list(tmp_path.glob(".book-video-*"))
