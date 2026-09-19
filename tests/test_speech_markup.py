"""Focused contract tests for bounded XML speech markup."""

import json

import pytest

from pandrator.logic.speech_markup import (
    VOICE_CATEGORIES,
    assert_authored_markup_preserved,
    markup_to_annotation,
    parse_speech_markup,
    plain_speech_markup,
)
from pandrator.logic.speech_performance import validate_annotation


CHARACTERS = [
    {
        "id": "c7",
        "name": "Scrooge",
        "display_name": "Ebenezer Scrooge",
        "aliases": ["old miser"],
        "voice_category": "male",
    },
    {"id": "c8", "name": "Belle", "voice_category": "female"},
]


def test_plain_markup_and_shorthand_canonicalization_preserve_transcript():
    source = '<segment id="s42"><dialogue><speaker g="male" n="Scrooge">“Come in,”</speaker> said Scrooge.</dialogue></segment>'
    parsed = parse_speech_markup(
        source,
        expected_segment_id="s42",
        expected_text="“Come in,” said Scrooge.",
        characters=CHARACTERS,
    )
    assert parsed.transcript == "“Come in,” said Scrooge."
    assert 'speaker ref="c7"' in parsed.xml
    assert ' n="Scrooge"' not in parsed.xml
    assert parsed.spans[0].speaker_id == "c7"
    assert parsed.spans[0].dialogue is True
    assert parsed.spans[-1].speaker_id is None
    assert parsed.spans[-1].dialogue is True
    assert plain_speech_markup("s", "2 < 3 & 4") == '<segment id="s">2 &lt; 3 &amp; 4</segment>'


def test_category_only_speaker_is_anonymous_but_dialogue_true():
    parsed = parse_speech_markup(
        '<segment id="s"><speaker g="female">Hello</speaker></segment>',
        expected_segment_id="s",
        expected_text="Hello",
    )
    span = parsed.spans[0]
    assert span.speaker_id is None
    assert span.voice_category == "female"
    assert span.dialogue is True
    assert span.narrator is False


def test_speaker_requires_identity_or_voice_metadata():
    for source in (
        '<segment id="s"><speaker>Text</speaker></segment>',
        '<segment id="s"><speaker ref="c7" n="Scrooge">Text</speaker></segment>',
        '<segment id="s"><speaker ref="missing">Text</speaker></segment>',
    ):
        with pytest.raises(ValueError):
            parse_speech_markup(
                source,
                expected_segment_id="s",
                expected_text="Text",
                characters=CHARACTERS,
            )


def test_scoped_controls_inherit_override_and_restore():
    parsed = parse_speech_markup(
        '<segment id="s"><ins>calm</ins>Before <span><em>sad</em>inside</span> after</segment>',
        expected_segment_id="s",
        expected_text="Before inside after",
    )
    assert [span.text for span in parsed.spans] == ["Before ", "inside", " after"]
    assert parsed.spans[0].delivery["instruction"] == "calm"
    assert parsed.spans[1].delivery["instruction"] == "calm"
    assert parsed.spans[1].delivery["emotion"] == "sad"
    assert parsed.spans[2].delivery["instruction"] == "calm"
    assert parsed.spans[2].delivery["emotion"] == ""


def test_repeated_quotes_get_exact_occurrences_and_speaker_differences_do_not_steer():
    parsed = parse_speech_markup(
        '<segment id="s"><dialogue><speaker ref="c7"><span><emphasis>strong</emphasis>yes</span></speaker><speaker ref="c8"> yes</speaker></dialogue></segment>',
        expected_segment_id="s",
        expected_text="yes yes",
        characters=CHARACTERS,
    )
    annotation = markup_to_annotation(parsed)
    assert annotation["decision"] == "steer"
    assert annotation["delivery"]["emphasis"] == "strong"
    assert annotation["spans"] == []

    parsed = parse_speech_markup(
        '<segment id="s"><speaker ref="c7">yes</speaker><speaker ref="c8"> yes</speaker></segment>',
        expected_segment_id="s",
        expected_text="yes yes",
        characters=CHARACTERS,
    )
    assert markup_to_annotation(parsed)["decision"] == "none"


def test_mixed_delivery_spans_use_anchors_and_adapter_is_valid():
    parsed = parse_speech_markup(
        '<segment id="s"><span><emphasis>light</emphasis>yes</span> <span><pace>brisk</pace>yes</span></segment>',
        expected_segment_id="s",
        expected_text="yes yes",
    )
    annotation = markup_to_annotation(parsed)
    assert len(annotation["spans"]) == 2
    assert annotation["spans"][0]["anchor"] == {"quote": "yes", "occurrence": 1}
    assert annotation["spans"][1]["anchor"] == {"quote": "yes", "occurrence": 2}
    assert validate_annotation(parsed.transcript, annotation) == annotation


def test_events_are_ordered_and_edge_positions_are_preserved():
    parsed = parse_speech_markup(
        '<segment id="s"><event kind="sigh"/>A<event kind="pause" duration_ms="200"/>B<event kind="laugh"/></segment>',
        expected_segment_id="s",
        expected_text="AB",
    )
    assert [event.offset for event in parsed.events] == [0, 1, 2]
    annotation = markup_to_annotation(parsed)
    assert [event["position"] for event in annotation["events"]] == ["before", "before", "after"]
    assert annotation["events"][1]["anchor"] == {"quote": "B", "occurrence": 1}
    assert annotation["events"][1]["duration_ms"] == 200


def test_public_values_are_json_safe():
    parsed = parse_speech_markup(
        '<segment id="s" boundary_after="paragraph">Text</segment>',
        expected_segment_id="s",
        expected_text="Text",
    )
    public = parsed.public()
    assert public["boundary_after"] == "paragraph"
    assert json.loads(json.dumps(public, ensure_ascii=False))["transcript"] == "Text"
    assert VOICE_CATEGORIES == ("male", "female", "androgynous", "unspecified")


def test_narrator_scope_resets_dialogue_and_public_includes_flag():
    parsed = parse_speech_markup(
        '<segment id="s"><dialogue><narrator>Aside</narrator></dialogue></segment>',
        expected_segment_id="s",
        expected_text="Aside",
    )
    span = parsed.spans[0]
    assert span.speaker_id is None
    assert span.voice_category == "unspecified"
    assert span.dialogue is False
    assert span.narrator is True
    assert parsed.public()["spans"][0]["narrator"] is True


def test_authored_metadata_survives_subdivision_and_rejects_removal():
    generic_source = parse_speech_markup(
        '<segment id="s"><span><ins>root direction</ins>root quote</span></segment>',
        expected_segment_id="s",
        expected_text="root quote",
    )
    subdivided = parse_speech_markup(
        '<segment id="s"><span><ins>root direction</ins>root <speaker ref="c7" voice="voiced">quote</speaker></span></segment>',
        expected_segment_id="s",
        expected_text="root quote",
        characters=CHARACTERS,
    )
    assert_authored_markup_preserved(generic_source, subdivided)
    assert "<ins>root direction</ins>root <speaker" in subdivided.xml

    source = parse_speech_markup(
        '<segment id="s" boundary_after="paragraph"><span><ins>root direction</ins><speaker ref="c7" voice="voiced">quote</speaker><event kind="pause" duration_ms="50"/></span></segment>',
        expected_segment_id="s",
        expected_text="quote",
        characters=CHARACTERS,
    )
    result = parse_speech_markup(
        '<segment id="s" boundary_after="paragraph"><span><ins>root direction</ins><speaker ref="c7" voice="voiced">quote</speaker><event kind="pause" duration_ms="50"/></span></segment>',
        expected_segment_id="s",
        expected_text="quote",
        characters=CHARACTERS,
    )
    assert_authored_markup_preserved(source, result)

    with pytest.raises(ValueError, match="voice"):
        assert_authored_markup_preserved(
            source,
            parse_speech_markup(
                '<segment id="s" boundary_after="paragraph"><span><ins>root direction</ins><speaker ref="c7">quote</speaker><event kind="pause" duration_ms="50"/></span></segment>',
                expected_segment_id="s",
                expected_text="quote",
                characters=CHARACTERS,
            ),
        )
    with pytest.raises(ValueError, match="event"):
        assert_authored_markup_preserved(
            source,
            parse_speech_markup(
                '<segment id="s" boundary_after="paragraph"><span><ins>root direction</ins><speaker ref="c7" voice="voiced">quote</speaker></span></segment>',
                expected_segment_id="s",
                expected_text="quote",
                characters=CHARACTERS,
            ),
        )
    with pytest.raises(ValueError, match="boundary_after"):
        assert_authored_markup_preserved(
            source,
            parse_speech_markup(
                '<segment id="s"><span><ins>root direction</ins><speaker ref="c7" voice="voiced">quote</speaker><event kind="pause" duration_ms="50"/></span></segment>',
                expected_segment_id="s",
                expected_text="quote",
                characters=CHARACTERS,
            ),
        )


@pytest.mark.parametrize(
    "source",
    [
        '<!DOCTYPE segment [<!ENTITY x "boom">]><segment id="s">&x;</segment>',
        '<segment id="s"><unknown>Text</unknown></segment>',
        '<segment id="s"><speaker ref="missing">Text</speaker></segment>',
        '<segment id="s"><emphasis>light</emphasis><emphasis>strong</emphasis>Text</segment>',
        '<segment id="s"><event kind="pause" duration_ms="10"/></segment>',
    ],
)
def test_invalid_markup_is_rejected(source):
    with pytest.raises(ValueError):
        parse_speech_markup(source, expected_segment_id="s", characters=CHARACTERS)


def test_grapheme_boundaries_are_not_split():
    with pytest.raises(ValueError, match="grapheme"):
        parse_speech_markup(
            '<segment id="s"><span voice="voice-a">e</span>\u0301</segment>',
            expected_segment_id="s",
            expected_text="e\u0301",
        )


def test_expected_text_and_literal_escaped_xml_are_exact():
    source = plain_speech_markup("s", "literal <tag> & text")
    parsed = parse_speech_markup(
        source,
        expected_segment_id="s",
        expected_text="literal <tag> & text",
    )
    assert parsed.transcript == "literal <tag> & text"
    with pytest.raises(ValueError):
        parse_speech_markup(source, expected_segment_id="s", expected_text="other")


def test_document_node_depth_and_size_bounds_are_enforced():
    too_deep = "<segment id=\"s\">" + "<span>" * 24 + "x" + "</span>" * 24 + "</segment>"
    with pytest.raises(ValueError, match="depth"):
        parse_speech_markup(too_deep, expected_segment_id="s")

    too_many_nodes = '<segment id="s">' + "<span/>" * 2048 + "</segment>"
    with pytest.raises(ValueError, match="node"):
        parse_speech_markup(too_many_nodes, expected_segment_id="s")

    too_large = '<segment id="s">' + ("x" * (256 * 1024)) + "</segment>"
    with pytest.raises(ValueError, match="256"):
        parse_speech_markup(too_large, expected_segment_id="s")
