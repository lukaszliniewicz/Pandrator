"""Focused tests for offset-safe speech-markup editing."""

import pytest

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_markup_edits import (
    join_speech_markup,
    slice_speech_markup,
)


CHARACTERS = [{"id": "c-alice", "voice_category": "female"}]


def test_slice_preserves_nested_controls_narrator_and_unicode_text():
    source = (
        '<segment id="source"><dialogue><speaker ref="c-alice" voice="alice">'
        '<emphasis>strong</emphasis>A😀</speaker></dialogue>'
        '<narrator voice="narrator"> aside </narrator></segment>'
    )
    result = slice_speech_markup(
        source,
        source_id="source",
        source_text="A😀 aside ",
        segment_id="slice",
        start=1,
        end=9,
        characters=CHARACTERS,
        boundary_after="continuation",
    )
    parsed = parse_speech_markup(
        result,
        expected_segment_id="slice",
        expected_text="😀 aside ",
        characters=CHARACTERS,
    )
    assert parsed.transcript == "😀 aside "
    assert parsed.spans[0].speaker_id == "c-alice"
    assert parsed.spans[0].voice == "alice"
    assert parsed.spans[0].delivery["emphasis"] == "strong"
    assert parsed.spans[-1].narrator is True
    assert parsed.boundary_after == "continuation"


def test_slice_event_at_end_is_selected_once_only_when_requested():
    source = '<segment id="source"><speaker ref="c-alice">Hi<event kind="pause" duration_ms="100"/></speaker></segment>'
    without_end = slice_speech_markup(
        source,
        source_id="source",
        source_text="Hi",
        segment_id="left",
        start=0,
        end=2,
        characters=CHARACTERS,
    )
    with_end = slice_speech_markup(
        source,
        source_id="source",
        source_text="Hi",
        segment_id="right",
        start=0,
        end=2,
        characters=CHARACTERS,
        include_end_events=True,
    )
    assert parse_speech_markup(
        without_end,
        expected_segment_id="left",
        expected_text="Hi",
        characters=CHARACTERS,
    ).events == ()
    assert [event.offset for event in parse_speech_markup(
        with_end,
        expected_segment_id="right",
        expected_text="Hi",
        characters=CHARACTERS,
    ).events] == [2]


def test_join_keeps_separators_outside_speaker_and_uses_last_boundary():
    first = '<segment id="first" boundary_after="continuation"><speaker ref="c-alice">One</speaker></segment>'
    last = '<segment id="last" boundary_after="paragraph"><narrator>Two</narrator></segment>'
    result = join_speech_markup(
        [("first", "One", first), ("plain", "& three", None), ("last", "Two", last)],
        segment_id="joined",
        separator=" | ",
        characters=CHARACTERS,
    )
    parsed = parse_speech_markup(
        result,
        expected_segment_id="joined",
        expected_text="One | & three | Two",
        characters=CHARACTERS,
    )
    assert parsed.transcript == "One | & three | Two"
    assert parsed.boundary_after == "paragraph"
    assert [span.speaker_id for span in parsed.spans if span.text == "One"] == ["c-alice"]
    separator_span = next(span for span in parsed.spans if " | & three" in span.text)
    assert separator_span.speaker_id is None
    assert separator_span.dialogue is False


@pytest.mark.parametrize(
    "source, source_text",
    [
        ('<!DOCTYPE segment [<!ENTITY x "boom">]><segment id="s">&x;</segment>', "boom"),
        ('<segment id="s"><speaker ref="missing">Text</speaker></segment>', "Text"),
    ],
)
def test_slice_reuses_parser_guards_for_entities_and_unknown_characters(source, source_text):
    with pytest.raises(ValueError):
        slice_speech_markup(
            source,
            source_id="s",
            source_text=source_text,
            segment_id="slice",
            start=0,
            end=len(source_text),
            characters=CHARACTERS,
        )


def test_slice_rejects_mismatched_text_and_ranges_without_rewriting():
    source = '<segment id="s">Exact text</segment>'
    with pytest.raises(ValueError, match="expected_text"):
        slice_speech_markup(
            source,
            source_id="s",
            source_text="Other text",
            segment_id="slice",
            start=0,
            end=5,
        )
    with pytest.raises(ValueError, match="range"):
        slice_speech_markup(
            source,
            source_id="s",
            source_text="Exact text",
            segment_id="slice",
            start=0,
            end=99,
        )
