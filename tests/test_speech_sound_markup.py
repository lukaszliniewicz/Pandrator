"""Bounded XML sound marker behavior."""

from __future__ import annotations

import uuid

import pytest

from pandrator.logic.speech_sound_markup import (
    SoundMarker,
    insert_sound_markers,
    preceding_lexical_anchor,
    split_sound_markup,
)

CUE = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
VARIANT = str(uuid.UUID("22222222-2222-4222-8222-222222222222"))


def marker(placement="after", **attributes):
    values = {"cue": CUE, "variant": VARIANT, "placement": placement, **attributes}
    return "<sound " + " ".join(f'{key}="{value}"' for key, value in values.items()) + "/>"


@pytest.mark.parametrize("bad", [
    "<sound/>", marker(extra="x"), marker(cue="AAAAAAAA-1111-4111-8111-111111111111"),
    marker(placement="middle"), marker().replace("/>", "></sound>"),
    marker().replace("/>", ">text</sound>"),
    "<sound cue='x' variant='y' placement='after'><event kind='pause'/></sound>",
])
def test_rejects_invalid_marker_contract(bad):
    with pytest.raises(ValueError):
        split_sound_markup(f'<segment id="s">Hi{bad}</segment>')


def test_rejects_dtd_grapheme_and_inside_word():
    with pytest.raises(ValueError):
        split_sound_markup(f'<!DOCTYPE segment><segment id="s">Hi{marker()}</segment>')
    with pytest.raises(ValueError, match="lexical word"):
        split_sound_markup(f'<segment id="s">Hel{marker()}lo</segment>')
    with pytest.raises(ValueError, match="grapheme"):
        split_sound_markup(f'<segment id="s">e{marker()}\u0301</segment>')


def test_roundtrip_preserves_unicode_tails_controls_and_events():
    speech = (
        '<segment id="s"><ins>calm</ins><speaker ref="a">'
        'Cafe\u0301 👋</speaker><event kind="pause"/> again</segment>'
    )
    derived = insert_sound_markers(speech, [
        SoundMarker(CUE, VARIANT, "after", 5),
        SoundMarker(str(uuid.uuid4()), VARIANT, "next_pause", len("Cafe\u0301 👋")),
    ])
    stripped, found = split_sound_markup(derived)
    assert stripped == speech
    assert [item.offset for item in found] == [5, len("Cafe\u0301 👋")]
    assert "<event kind=\"pause\"/>" in derived


def test_repeated_token_occurrence_and_same_offset_markers():
    assert preceding_lexical_anchor("hi hi", 5) == {
        "quote": "hi", "occurrence": 2, "start": 3, "end": 5,
    }
    speech = '<segment id="s">hi hi</segment>'
    two = [SoundMarker(str(uuid.uuid4()), VARIANT, "after", 2) for _ in range(2)]
    stripped, found = split_sound_markup(insert_sound_markers(speech, two))
    assert stripped == speech
    assert len(found) == 2 and all(item.offset == 2 for item in found)


def test_unicode_word_boundaries_preserve_complete_lexical_tokens():
    assert preceding_lexical_anchor("你好世界", 1) == {
        "quote": "你", "occurrence": 1, "start": 0, "end": 1,
    }
    assert preceding_lexical_anchor("안녕하세요 세계", 5)["quote"] == "안녕하세요"
    assert preceding_lexical_anchor("e\u0301cole", 6)["quote"] == "e\u0301cole"
    with pytest.raises(ValueError, match="lexical word"):
        preceding_lexical_anchor("hello", 2)
    with pytest.raises(ValueError, match="grapheme"):
        preceding_lexical_anchor("e\u0301cole", 1)
    with pytest.raises(ValueError, match="complete lexical token"):
        preceding_lexical_anchor("hello!", 6)
    speech = f'<segment id="s">你{marker()}好世界</segment>'
    stripped, found = split_sound_markup(speech)
    assert stripped == '<segment id="s">你好世界</segment>'
    assert found[0].offset == 1
