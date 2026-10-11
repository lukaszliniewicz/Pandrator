from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_markup_edits import edit_speech_markup_range

CHARACTERS = [
    {"id": "alice", "display_name": "Alice", "voice_category": "female"},
    {"id": "bob", "display_name": "Bob", "voice_category": "male"},
]
_CONTROL_TAGS = {"ins", "em", "pace", "cadence", "emphasis"}


def _span_at(parsed, offset):
    return next(span for span in parsed.spans if span.start <= offset < span.end)


def _event_signature(parsed):
    return tuple(
        (event.offset, event.kind, event.duration_ms, event.affinity)
        for event in parsed.events
    )


def _root_controls(xml):
    root = ET.fromstring(xml)
    return tuple(
        (child.tag, child.text or "")
        for child in root
        if child.tag in _CONTROL_TAGS
    )


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


def test_voice_and_character_edits_preserve_root_delivery_and_events():
    text = " A👩🏽‍🚀, okay."
    xml = (
        '<segment id="s" boundary_after="dialogue_turn">'
        "<ins>Root instruction</ins><em>calm</em><pace>slower</pace> "
        '<speaker ref="alice" voice="AliceVoice">'
        "<ins>Local direction</ins><em>urgent</em>A👩🏽‍🚀</speaker>"
        '<event kind="pause" duration_ms="120"/>, '
        '<event kind="pause" duration_ms="240"/>'
        '<speaker ref="bob">okay</speaker>.</segment>'
    )
    original = parse_speech_markup(
        xml, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )
    expected_roots = (
        ("ins", "Root instruction"),
        ("em", "calm"),
        ("pace", "slower"),
    )
    assert _root_controls(original.xml) == expected_roots
    assert _span_at(original, 1).delivery == {
        "instruction": "Root instruction\nLocal direction",
        "emotion": "urgent",
        "pace": "slower",
        "cadence": "",
        "emphasis": "",
    }

    edited_voice = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=1,
        end=len(" A👩🏽‍🚀"),
        voice="UpdatedVoice",
    )
    edited_character = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=1,
        end=len(" A👩🏽‍🚀"),
        speaker="character",
        character_id="bob",
        voice="BobVoice",
    )

    for edited, expected_voice, expected_speaker in (
        (edited_voice, "UpdatedVoice", "alice"),
        (edited_character, "BobVoice", "bob"),
    ):
        parsed = parse_speech_markup(
            edited,
            expected_segment_id="s",
            expected_text=text,
            characters=CHARACTERS,
        )
        assert parsed.transcript == text
        assert parsed.boundary_after == "dialogue_turn"
        assert _root_controls(parsed.xml) == expected_roots
        assert _event_signature(parsed) == _event_signature(original)
        assert _event_signature(parsed) == (
            (len(" A👩🏽‍🚀"), "pause", 120, "following"),
            (len(" A👩🏽‍🚀, "), "pause", 240, "following"),
        )
        for offset in range(len(text)):
            assert _span_at(parsed, offset).delivery == _span_at(original, offset).delivery
        selected = _span_at(parsed, 1)
        assert selected.voice == expected_voice
        assert selected.speaker_id == expected_speaker
        assert selected.dialogue and not selected.narrator


@pytest.mark.parametrize(
    ("instruction", "expected_instruction"),
    [("Lower voice", "Lower voice"), ("", "")],
)
def test_instruction_replacement_and_clear_mask_retained_root(
    instruction, expected_instruction
):
    text = "Keep change after."
    xml = (
        '<segment id="s"><ins>Root direction</ins>'
        "<em>calm</em><pace>slower</pace>Keep <speaker ref=\"alice\">"
        "change</speaker> after.</segment>"
    )
    original = parse_speech_markup(
        xml, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )
    start = text.index("change")
    end = start + len("change")
    edited = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=start,
        end=end,
        delivery={"instruction": instruction},
    )
    parsed = parse_speech_markup(
        edited, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )

    assert parsed.transcript == text
    assert _root_controls(parsed.xml) == (
        ("ins", "Root direction"),
        ("em", "calm"),
        ("pace", "slower"),
    )
    assert all(
        _span_at(parsed, offset).delivery["instruction"] == expected_instruction
        for offset in range(start, end)
    )
    root = ET.fromstring(parsed.xml)
    selected_speaker = next(
        element
        for element in root.iter("speaker")
        if element.attrib.get("ref") == "alice"
        and "change" in "".join(element.itertext())
    )
    assert any(
        child.tag == "ins" and not (child.text or "")
        for child in selected_speaker
    )
    if expected_instruction:
        nested_span = next(
            child for child in selected_speaker if child.tag == "span"
        )
        assert nested_span.find("ins").text == expected_instruction
    else:
        assert all(child.tag != "span" for child in selected_speaker)
    for offset in [*range(start), *range(end, len(text))]:
        assert _span_at(parsed, offset).delivery == _span_at(original, offset).delivery
    assert _span_at(parsed, start).delivery["emotion"] == "calm"
    assert _span_at(parsed, start).delivery["pace"] == "slower"


def test_instruction_clear_masks_root_in_a_plain_scope():
    text = "Keep change after."
    xml = '<segment id="s"><ins>Root direction</ins>Keep change after.</segment>'
    start = text.index("change")
    end = start + len("change")
    edited = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=start,
        end=end,
        delivery={"instruction": ""},
    )
    parsed = parse_speech_markup(
        edited, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )

    assert _root_controls(parsed.xml) == (("ins", "Root direction"),)
    assert all(
        _span_at(parsed, offset).delivery["instruction"] == ""
        for offset in range(start, end)
    )
    assert _span_at(parsed, start - 1).delivery["instruction"] == "Root direction"
    root = ET.fromstring(parsed.xml)
    selected_scope = next(
        element
        for element in root.iter("span")
        if "change" in "".join(element.itertext())
    )
    mask = selected_scope.find("ins")
    assert mask is not None and not (mask.text or "")


@pytest.mark.parametrize(("tag", "field", "root_value"), [("em", "emotion", "calm"), ("pace", "pace", "slower")])
def test_empty_noninstruction_override_masks_plain_scope_root(tag, field, root_value):
    text = "Keep change after."
    xml = f'<segment id="s"><{tag}>{root_value}</{tag}>Keep change after.</segment>'
    start = text.index("change")
    end = start + len("change")
    edited = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=start,
        end=end,
        delivery={field: ""},
    )
    parsed = parse_speech_markup(
        edited, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )

    assert _root_controls(parsed.xml) == ((tag, root_value),)
    assert all(
        _span_at(parsed, offset).delivery[field] == ""
        for offset in range(start, end)
    )
    assert _span_at(parsed, start - 1).delivery[field] == root_value
    root = ET.fromstring(parsed.xml)
    selected_scope = next(
        element
        for element in root.iter("span")
        if "change" in "".join(element.itertext())
    )
    mask = selected_scope.find(tag)
    assert mask is not None and not (mask.text or "")


def test_partial_range_crossing_scopes_preserves_each_effective_control():
    text = "One two three"
    xml = (
        '<segment id="s"><ins>Root direction</ins><em>steady</em>'
        '<pace>natural</pace><speaker ref="alice" voice="AliceVoice">'
        "<ins>Local direction</ins>One </speaker>"
        '<narrator><em>soft</em>two </narrator>'
        '<speaker ref="bob">three</speaker></segment>'
    )
    original = parse_speech_markup(
        xml, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )
    start, end = 2, 10
    edited = edit_speech_markup_range(
        xml,
        segment_id="s",
        text=text,
        characters=CHARACTERS,
        start=start,
        end=end,
        voice="SelectedVoice",
    )
    parsed = parse_speech_markup(
        edited, expected_segment_id="s", expected_text=text, characters=CHARACTERS
    )

    assert parsed.transcript == text
    assert _root_controls(parsed.xml) == (
        ("ins", "Root direction"),
        ("em", "steady"),
        ("pace", "natural"),
    )
    for offset in range(len(text)):
        before = _span_at(original, offset)
        after = _span_at(parsed, offset)
        assert after.delivery == before.delivery
        if start <= offset < end:
            assert after.voice == "SelectedVoice"
        else:
            assert after.voice == before.voice
            assert after.speaker_id == before.speaker_id
            assert after.narrator == before.narrator
