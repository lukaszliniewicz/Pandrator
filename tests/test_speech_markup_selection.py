from __future__ import annotations

import pytest

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_markup_edits import edit_speech_markup_range

CHARACTERS = [
    {"id": "alice", "display_name": "Alice", "voice_category": "female"},
    {"id": "bob", "display_name": "Bob", "voice_category": "male"},
]


def test_range_edit_preserves_unicode_text_events_and_unselected_scopes():
    text = "Hi 👩🏽‍🚀, Bob!"
    xml = (
        '<segment id="s" boundary_after="dialogue_turn">'
        '<ins>Restrained narration</ins><em>somber</em>'
        '<speaker ref="alice" voice="AliceVoice"><pace>slower</pace>Hi 👩🏽‍🚀</speaker>, '
        '<event kind="pause" duration_ms="250"/>'
        '<speaker ref="bob"><cadence>contrast</cadence>Bob</speaker>!</segment>'
    )

    edited = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=3,
        end=7,
        speaker="narrator",
        voice=None,
        delivery={"instruction": "with care", "emotion": "", "pace": None},
    )
    parsed = parse_speech_markup(
        edited,
        expected_segment_id="s",
        expected_text=text,
        characters=CHARACTERS,
    )

    assert parsed.transcript == text
    assert parsed.boundary_after == "dialogue_turn"
    assert [(event.offset, event.kind, event.duration_ms) for event in parsed.events] == [
        (9, "pause", 250)
    ]
    assert parsed.spans[0].speaker_id == "alice"
    selected = [span for span in parsed.spans if span.start >= 3 and span.end <= 7]
    assert selected and all(span.narrator and not span.dialogue for span in selected)
    assert all(span.voice is None for span in selected)
    assert parsed.spans[-2].speaker_id == "bob"
    original = parse_speech_markup(
        xml, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )
    for offset in [*range(3), *range(7, len(text))]:
        before = next(span for span in original.spans if span.start <= offset < span.end)
        after = next(span for span in parsed.spans if span.start <= offset < span.end)
        assert after.delivery == before.delivery
        assert after.voice == before.voice
        assert after.speaker_id == before.speaker_id
        assert after.narrator == before.narrator


def test_range_edit_rejects_empty_ranges_and_unknown_characters():
    xml = '<segment id="s">Hello</segment>'
    with pytest.raises(ValueError, match="must not be empty"):
        edit_speech_markup_range(
            xml,
            segment_id="s",
            text="Hello",
            characters=CHARACTERS,
            start=2,
            end=2,
        )
    with pytest.raises(ValueError, match="Unknown character"):
        edit_speech_markup_range(
            xml,
            segment_id="s",
            text="Hello",
            characters=CHARACTERS,
            start=0,
            end=1,
            speaker="character",
            character_id="missing",
        )


@pytest.mark.parametrize("character_id, category", [("alice", "female"), ("bob", "male")])
def test_character_range_edit_retains_selected_category(character_id, category):
    edited = edit_speech_markup_range(
        '<segment id="s">Hello 👋!</segment>',
        segment_id="s", text="Hello 👋!", characters=CHARACTERS,
        start=6, end=7, speaker="character", character_id=character_id,
    )
    parsed = parse_speech_markup(
        edited, expected_segment_id="s", expected_text="Hello 👋!", characters=CHARACTERS,
    )
    assert parsed.transcript == "Hello 👋!"
    selected = next(span for span in parsed.spans if span.start == 6)
    assert selected.speaker_id == character_id
    assert selected.voice_category == category
    assert selected.dialogue and not selected.narrator
    assert parsed.spans[0].speaker_id is None
    assert parsed.spans[-1].speaker_id is None
