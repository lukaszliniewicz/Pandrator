"""Reading displays retain exact text and speech timing across layout choices."""

import pytest
import regex

from pandrator.logic.book_cues import (
    BookPassage,
    clip_book_cues,
    compose_book_cues,
    write_book_subtitles,
)
from pandrator.logic.book_settings import validate_book_settings
from pandrator.logic.book_timing import BookWord


def passage(text, *, start=0, step=900, heading=""):
    spans = list(regex.finditer(r"\S+", text))
    words = tuple(
        BookWord(
            match.group(),
            start + index * step,
            start + (index + 1) * step - 50,
            match.start(),
            match.end(),
        )
        for index, match in enumerate(spans)
    )
    return BookPassage(text, words, start, start + len(words) * step, heading)


def test_reading_keeps_full_text_and_holds_through_paragraph_silence():
    first = passage("A short sentence. A second one.")
    second = passage("Another paragraph follows.", start=first.end_ms + 123)
    cues = compose_book_cues(
        [first, second], {"book_target_seconds": 2}, total_duration_ms=second.end_ms
    )
    assert " ".join(cue.text for cue in cues) == first.text + " " + second.text
    assert cues[0].start_ms == 0
    assert cues[-1].end_ms == second.end_ms
    assert all(a.end_ms == b.start_ms for a, b in zip(cues, cues[1:], strict=False))
    assert any(cue.end_ms == second.start_ms for cue in cues)


def test_fitted_cues_do_not_split_a_mapped_name_or_lose_punctuation():
    text = "“Sk Rooj,” said Marley."
    item = BookPassage(
        text,
        (
            BookWord("“Sk Rooj,”", 0, 700, 0, 10),
            BookWord("said", 800, 1200, 11, 15),
            BookWord("Marley.", 1300, 1900, 16, 23),
        ),
        0,
        2000,
    )
    cues = compose_book_cues(
        [item],
        {},
        total_duration_ms=2000,
        fit_lines=lambda value: (value,) if len(value) <= 15 else None,
    )
    assert [cue.text for cue in cues] == ["“Sk Rooj,”", "said Marley."]


def test_reading_capacity_uses_sentence_boundary_before_target_duration():
    item = passage("A complete sentence. Christmas among the rest.", step=300)
    cues = compose_book_cues(
        [item],
        {},
        total_duration_ms=item.end_ms,
        fit_lines=lambda text: (text,) if len(text) <= 40 else None,
    )
    assert [cue.text for cue in cues] == [
        "A complete sentence.",
        "Christmas among the rest.",
    ]
    assert cues[0].end_ms == cues[1].start_ms == item.words[3].start_ms


def test_whole_segment_subtitles_skip_word_splitting_and_line_cap():
    text = " ".join(["Word"] * 200)
    item = passage(text, step=100)
    cues = compose_book_cues([item], {"book_cue_mode": "segments"}, total_duration_ms=item.end_ms)
    assert len(cues) == 1 and cues[0].text == text
    with pytest.raises(ValueError, match="does not fit"):
        compose_book_cues(
            [item],
            {"book_cue_mode": "segments"},
            total_duration_ms=item.end_ms,
            fit_lines=lambda _: None,
        )


def test_caption_limits_keep_complete_cjk_and_use_real_word_boundaries():
    text = "春が来た。桜が咲いている。"
    words = tuple(
        BookWord(char, index * 700, index * 700 + 600, index, index + 1)
        for index, char in enumerate(text)
        if char.isalnum()
    )
    words = tuple(
        BookWord(
            text[
                word.start_char : words[index + 1].start_char
                if index + 1 < len(words)
                else len(text)
            ],
            word.start_ms,
            word.end_ms,
            word.start_char,
            words[index + 1].start_char if index + 1 < len(words) else len(text),
        )
        for index, word in enumerate(words)
    )
    item = BookPassage(text, words, 0, len(text) * 700)
    cues = compose_book_cues(
        [item],
        {"book_style": "captions", "language": "ja", "subtitle_language_defaults": True},
        total_duration_ms=item.end_ms,
    )
    assert "".join(cue.text for cue in cues) == text
    assert all(len(cue.lines) <= 2 for cue in cues)
    assert all(cue.end_ms - cue.start_ms <= 7000 for cue in cues)


def test_chapter_headings_prevent_short_fragment_merge():
    a = passage("One.", heading="First")
    b = passage("Two.", start=1000, heading="Second")
    cues = compose_book_cues([a, b], {}, total_duration_ms=b.end_ms)
    assert len(cues) == 2
    assert [cue.heading for cue in cues] == ["First", "Second"]


@pytest.mark.parametrize("style", ["reading", "captions"])
def test_short_passages_keep_their_paragraph_boundaries(style):
    first = passage('"No," she said.', step=300)
    second = passage("Another paragraph begins.", start=1000, step=300)
    cues = compose_book_cues(
        [first, second], {"book_style": style}, total_duration_ms=second.end_ms
    )
    assert [cue.text for cue in cues] == [first.text, second.text]
    assert cues[1].start_ms == second.start_ms


def test_preview_clips_bounds_without_estimating_word_durations(tmp_path):
    item = passage("A sentence for a preview.")
    original = compose_book_cues([item], {}, total_duration_ms=item.end_ms)
    clipped = clip_book_cues(original, 750, 2500)
    assert clipped[0].start_ms == 0 and clipped[-1].end_ms == 1750
    assert clipped[0].text == original[0].text
    for format in ("srt", "vtt"):
        path = tmp_path / f"preview.{format}"
        write_book_subtitles(clipped, path, format)
        content = path.read_text()
        assert "00:00:01" in content and item.text in content
        assert content.startswith("WEBVTT") == (format == "vtt")


@pytest.mark.parametrize("format", ["srt", "vtt"])
def test_whole_segment_subtitles_keep_blank_source_lines_inside_one_cue(tmp_path, format):
    item = passage("First.\n\n\n\n \t\nSecond.\x00\nThird.")
    cues = compose_book_cues([item], {"book_cue_mode": "segments"}, total_duration_ms=item.end_ms)
    path = tmp_path / f"whole-segment.{format}"
    write_book_subtitles(cues, path, format)

    content = path.read_text()
    blocks = content.strip().split("\n\n")
    if format == "vtt":
        assert blocks.pop(0) == "WEBVTT"
    assert len(blocks) == 1
    assert blocks[0].splitlines()[2:] == ["First.", "Second.", "Third."]


@pytest.mark.parametrize(
    "settings",
    [
        {"book_background": "red:filter=evil"},
        {"book_target_seconds": True},
        {"book_font_size": 500},
        {"book_heading_font_size": 0},
        {"book_heading_font_size": True},
        {"book_target_seconds": 21, "book_max_seconds": 20},
        {"book_preview_start_seconds": float("nan")},
        {"book_preview_duration_seconds": 31},
    ],
)
def test_invalid_settings_fail_before_alignment_or_render(settings):
    with pytest.raises(ValueError):
        validate_book_settings(settings)
