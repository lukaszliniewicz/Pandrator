"""Boundary ownership and scope preservation for selected vocal cues."""

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from pandrator.logic.speech_markup import (
    assert_authored_markup_preserved,
    markup_to_annotation,
    parse_speech_markup,
    speech_markup_reference,
)
from pandrator.logic.speech_markup_edits import (
    edit_speech_markup_range,
    insert_speech_markup_event,
    slice_speech_markup,
)
from pandrator.web import models as m
from pandrator.web.generation_rendering import _subset
from pandrator.web.speech_selection import (
    _candidate_markup,
    apply_speech_selection,
    get_speech_selection_context,
    preview_speech_selection,
)
from pandrator.web.speech_selection_schemas import SpeechSelectionRequest
from tests.test_performance_plans import case as case  # noqa: PLC0414

CHARACTERS = [
    {"id": "a", "voice_category": "female"},
    {"id": "b", "voice_category": "male"},
]
SOURCE = (
    '<segment id="s" boundary_after="dialogue_turn">'
    '<ins>root direction</ins><dialogue id="d">'
    '<speaker ref="a">A</speaker><speaker ref="b">B</speaker>'
    '</dialogue></segment>'
)


def parsed(xml: str):
    return parse_speech_markup(
        xml, expected_segment_id="s", expected_text="AB", characters=CHARACTERS
    )


@pytest.mark.parametrize("affinity,expected_part,position", [
    ("previous", 0, "after"),
    ("following", 1, "before"),
])
def test_insertion_preserves_scopes_and_affinity_controls_render_part(
    affinity, expected_part, position
):
    result = insert_speech_markup_event(
        SOURCE, segment_id="s", text="AB", characters=CHARACTERS,
        offset=1, kind="pause", duration_ms=250, affinity=affinity,
    )
    original = parsed(SOURCE)
    edited = parsed(result)
    assert '<dialogue id="d">' in result
    assert '<ins>root direction</ins>' in result
    assert edited.spans == original.spans
    assert edited.boundary_after == original.boundary_after
    assert edited.events[0].affinity == affinity
    assert "affinity" in speech_markup_reference()["element_attributes"]["event"]
    assert_authored_markup_preserved(original, edited)
    first = _subset(edited, 0, 1)
    second = _subset(edited, 1, 2)
    assert [len(first.events), len(second.events)] == (
        [1, 0] if expected_part == 0 else [0, 1]
    )
    annotation = markup_to_annotation((first, second)[expected_part])
    assert annotation["events"][0]["position"] == position


def test_previous_and_following_at_one_boundary_slice_once_each():
    xml = insert_speech_markup_event(
        SOURCE, segment_id="s", text="AB", characters=CHARACTERS,
        offset=1, kind="laugh", affinity="following",
    )
    xml = insert_speech_markup_event(
        xml, segment_id="s", text="AB", characters=CHARACTERS,
        offset=1, kind="sigh", affinity="previous",
    )
    left = slice_speech_markup(
        xml, source_id="s", source_text="AB", segment_id="left",
        start=0, end=1, characters=CHARACTERS,
    )
    right = slice_speech_markup(
        xml, source_id="s", source_text="AB", segment_id="right",
        start=1, end=2, characters=CHARACTERS,
    )
    assert [event.kind for event in parse_speech_markup(
        left, expected_segment_id="left", expected_text="A", characters=CHARACTERS,
    ).events] == ["sigh"]
    assert [event.kind for event in parse_speech_markup(
        right, expected_segment_id="right", expected_text="B", characters=CHARACTERS,
    ).events] == ["laugh"]
    assert [(event.kind, event.affinity) for event in parsed(xml).events] == [
        ("sigh", "previous"), ("laugh", "following"),
    ]


def test_boundary_defaults_and_authored_affinity_are_preserved():
    original = parsed(SOURCE)
    first = insert_speech_markup_event(
        SOURCE, segment_id="s", text="AB", characters=CHARACTERS,
        offset=0, kind="gasp",
    )
    final = insert_speech_markup_event(
        first, segment_id="s", text="AB", characters=CHARACTERS,
        offset=2, kind="exhale", affinity="previous",
    )
    result = parsed(final)
    assert_authored_markup_preserved(original, result)
    assert [event.kind for event in _subset(result, 0, 1).events] == ["gasp"]
    assert [event.kind for event in _subset(result, 1, 2).events] == ["exhale"]
    changed = parsed(final.replace('affinity="previous"', 'affinity="following"'))
    with pytest.raises(ValueError, match="event was removed or changed"):
        assert_authored_markup_preserved(result, changed)


def test_selection_cue_composes_after_range_edit_and_rejects_invalid_inputs():
    segment = SimpleNamespace(id="s")
    request = SpeechSelectionRequest.model_validate({
        "revision_id": "r", "segment_id": "s", "expected_segment_revision": 1,
        "start": 0, "end": 1, "speaker": "narrator",
        "vocal_event": {"kind": "pause", "position": "after", "duration_ms": 100},
    })
    values = request.model_dump(exclude_unset=True)
    xml = _candidate_markup(
        source_xml=SOURCE, segment=segment, text="AB",
        characters=CHARACTERS, values=values,
    )
    result = parsed(xml)
    assert result.spans[0].narrator
    assert (result.events[0].offset, result.events[0].affinity) == (1, "previous")
    for bad in (
        {"vocal_event": None},
        {"vocal_event": {"kind": "sigh", "duration_ms": 100}},
        {"vocal_event": {"kind": "pause", "duration_ms": 49}},
        {"vocal_event": {"kind": "pause", "duration_ms": True}},
        {"speech_xml": SOURCE, "vocal_event": {"kind": "laugh"}},
    ):
        with pytest.raises(ValidationError):
            SpeechSelectionRequest.model_validate({
                "revision_id": "r", "segment_id": "s", "expected_segment_revision": 1,
                "start": 0, "end": 1, **bad,
            })
    with pytest.raises(ValidationError):
        insert_speech_markup_event(
            SOURCE, segment_id="s", text="AB", characters=CHARACTERS,
            offset=1, kind="laugh", duration_ms=100,
        )
    with pytest.raises(ValueError, match="grapheme"):
        insert_speech_markup_event(
            '<segment id="s">A👩🏽‍🚀B</segment>', segment_id="s",
            text="A👩🏽‍🚀B", characters=None, offset=2, kind="laugh",
        )


def test_existing_range_edit_keeps_event_affinity():
    xml = insert_speech_markup_event(
        SOURCE, segment_id="s", text="AB", characters=CHARACTERS,
        offset=1, kind="sigh", affinity="previous",
    )
    changed = edit_speech_markup_range(
        xml, segment_id="s", text="AB", characters=CHARACTERS,
        start=1, end=2, speaker="narrator",
    )
    assert [(event.offset, event.kind, event.affinity) for event in parsed(changed).events] == [
        (1, "sigh", "previous")
    ]


def test_range_edit_keeps_order_of_opposite_affinities_at_same_offset():
    xml = (
        '<segment id="s">A<event kind="laugh" affinity="following"/>'
        '<event kind="sigh" affinity="previous"/>B</segment>'
    )
    changed = edit_speech_markup_range(
        xml, segment_id="s", text="AB", characters=None,
        start=0, end=1, delivery={"pace": "brisk"},
    )
    assert [(event.kind, event.affinity) for event in parsed(changed).events] == [
        ("laugh", "following"), ("sigh", "previous"),
    ]
    assert_authored_markup_preserved(parsed(xml), parsed(changed))


def test_preview_opt_in_and_atomic_apply_vocalization_flag(case):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    with services["database"].session() as session:
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = "AB"
        segment.speech_plan_json = {"speech_xml": f'<segment id="{segment_id}">AB</segment>'}
        revision = segment.revision
    request = {
        "revision_id": case["revision_id"], "segment_id": segment_id,
        "expected_segment_revision": revision, "start": 0, "end": 1,
        "vocal_event": {"kind": "laugh"}, "enable_performance": True,
    }
    with services["database"].session() as session:
        before = preview_speech_selection(services, session, case["session_id"], request)
        enabled = preview_speech_selection(
            services, session, case["session_id"],
            {**request, "enable_vocalizations": True},
        )
    assert before["current_flags"]["allow_vocalizations"] is False
    assert enabled["current_flags"]["allow_vocalizations"] is False
    assert before["preview_revision"] != enabled["preview_revision"]
    assert any("Vocalizations are disabled" in item["message"] for item in before["preview"]["report"])
    assert not any("Vocalizations are disabled" in item["message"] for item in enabled["preview"]["report"])
    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "enable_vocalizations": True,
             "expected_preview_revision": enabled["preview_revision"]},
        )
        assert applied["current_flags"]["allow_vocalizations"] is True
    with services["database"].session() as session:
        context = get_speech_selection_context(
            services, session, case["session_id"],
            {key: request[key] for key in (
                "revision_id", "segment_id", "expected_segment_revision", "start", "end"
            )},
        )
        assert context["current_flags"]["allow_vocalizations"] is True
