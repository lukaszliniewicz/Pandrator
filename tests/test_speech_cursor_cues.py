from __future__ import annotations

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from pandrator.logic.speech_markup import (
    assert_authored_markup_preserved,
    parse_speech_markup,
)
from pandrator.web import models as m
from pandrator.web.generation_controls import get_generation_controls, save_generation_controls
from pandrator.web.speech_selection import (
    apply_speech_selection,
    get_speech_selection_context,
    preview_speech_selection,
)
from pandrator.web.speech_selection_schemas import (
    SpeechSelectionApplyRequest,
    SpeechSelectionContextRequest,
    SpeechSelectionRequest,
)
from tests.test_performance_plans import case as case  # noqa: PLC0414

TEXT = "A👋B"
CHARACTERS = [
    {"id": "alice", "display_name": "Alice"},
    {"id": "bob", "display_name": "Bob"},
]


def _base(segment_id: str, revision_id: str, revision: int, start: int, end: int):
    return {
        "revision_id": revision_id,
        "segment_id": segment_id,
        "expected_segment_revision": revision,
        "start": start,
        "end": end,
    }


def _set_segment(case, text: str = TEXT) -> tuple[str, int]:
    services = case["services"]
    segment_id = case["segment_ids"][0]
    if text == TEXT:
        xml = (
            f'<segment id="{segment_id}" boundary_after="dialogue_turn">'
            '<ins>Root direction</ins>'
            '<speaker ref="alice">A</speaker>'
            '<narrator>👋</narrator>'
            '<speaker ref="bob">B</speaker>'
            '</segment>'
        )
    else:
        xml = f'<segment id="{segment_id}">{text}</segment>'
    with services["database"].session() as session:
        controls = get_generation_controls(session, case["session_id"])
        save_generation_controls(
            session,
            case["session_id"],
            expected_revision=int(controls.get("revision") or 0),
            characters=CHARACTERS,
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.optimized_text = text
        segment.speech_plan_json = {"speech_xml": xml}
        revision = segment.revision
    return segment_id, revision


def test_cursor_context_uses_following_span_and_preceding_span_at_end(case):
    services = case["services"]
    segment_id, revision = _set_segment(case)
    expected = (
        (0, "character", "alice", ["alice"]),
        (1, "narrator", None, []),
        (2, "character", "bob", ["bob"]),
        (3, "character", "bob", ["bob"]),
    )

    for offset, speaker, character_id, inherited_ids in expected:
        request = _base(
            segment_id, case["revision_id"], revision, offset, offset
        )
        with services["database"].session() as session:
            result = get_speech_selection_context(
                services, session, case["session_id"], request
            )
        assert result["selection"] == {"start": offset, "end": offset, "text": ""}
        assert result["current"]["speaker"] == speaker
        assert result["current"]["character_id"] == character_id
        assert [item["id"] for item in result["inherited"]["characters"]] == inherited_ids


def test_cursor_request_schema_acceptance_and_patch_rejections():
    base = _base("segment", "revision", 1, 1, 1)
    with pytest.raises(ValidationError, match="requires a vocal_event"):
        SpeechSelectionRequest.model_validate(base)

    cue = {"vocal_event": {"kind": "laugh"}}
    assert SpeechSelectionRequest.model_validate({**base, **cue}).start == 1
    assert SpeechSelectionRequest.model_validate(
        {**base, **cue, "speaker": "unchanged"}
    ).end == 1
    assert SpeechSelectionApplyRequest.model_validate(
        {**base, **cue, "expected_preview_revision": "a" * 64}
    ).end == 1
    assert SpeechSelectionContextRequest.model_validate(base).end == 1

    for patch in (
        {"speaker": "narrator"},
        {"speaker": "character", "character_id": "alice"},
        {"voice": None},
        {"voice": ""},
        {"character_id": None},
        {"voice_category": None},
        {"delivery": None},
        {"delivery": {}},
        {"speech_xml": None},
        {"speech_xml": ""},
    ):
        with pytest.raises(ValidationError, match="cursor selection"):
            SpeechSelectionRequest.model_validate({**base, **cue, **patch})

    with pytest.raises(ValidationError, match="requires a vocal_event"):
        SpeechSelectionRequest.model_validate({**base, "vocal_event": None})
    with pytest.raises(ValidationError):
        SpeechSelectionRequest.model_validate({**base, **cue, "speaker": ""})
    with pytest.raises(ValidationError, match="reversed"):
        SpeechSelectionRequest.model_validate(
            {**base, "start": 2, "end": 1, **cue}
        )
    with pytest.raises(ValidationError, match="reversed"):
        SpeechSelectionContextRequest.model_validate(
            {**base, "start": 2, "end": 1}
        )


@pytest.mark.parametrize(
    ("offset", "position", "expected_affinity"),
    ((0, "before", "following"), (1, "before", "following"),
     (2, "after", "previous"), (3, "after", "previous")),
)
def test_cursor_preview_and_apply_preserve_markup_and_codepoint_positions(
    case, offset, position, expected_affinity
):
    services = case["services"]
    segment_id, revision = _set_segment(case)
    request = {
        **_base(segment_id, case["revision_id"], revision, offset, offset),
        "vocal_event": {"kind": "pause", "position": position, "duration_ms": 200},
    }
    with services["database"].session() as session:
        preview = preview_speech_selection(
            services, session, case["session_id"], request
        )

    assert preview["selection"] == {"start": offset, "end": offset, "text": ""}
    original = parse_speech_markup(
        f'<segment id="{segment_id}" boundary_after="dialogue_turn">'
        '<ins>Root direction</ins><speaker ref="alice">A</speaker>'
        '<narrator>👋</narrator><speaker ref="bob">B</speaker></segment>',
        expected_segment_id=segment_id,
        expected_text=TEXT,
        characters=CHARACTERS,
    )
    preview_markup = parse_speech_markup(
        preview["speech_xml"], expected_segment_id=segment_id,
        expected_text=TEXT, characters=CHARACTERS,
    )
    assert preview_markup.spans == original.spans
    assert preview_markup.boundary_after == original.boundary_after
    assert "<ins>Root direction</ins>" in preview["speech_xml"]
    assert [(event.offset, event.affinity, event.duration_ms)
            for event in preview_markup.events] == [(offset, expected_affinity, 200)]
    assert_authored_markup_preserved(original, preview_markup)

    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        saved_plan = session.get(m.PerformancePlan, applied["performance_plan"]["id"])
        saved_xml = saved_plan.manual_annotations_json[segment_id]["_speech_xml"]
        assert session.scalar(select(m.PerformancePlan.id)) is not None

    assert applied["selection"] == {"start": offset, "end": offset, "text": ""}
    assert saved_xml == preview["speech_xml"]


def test_cursor_rejects_empty_out_of_range_and_inside_grapheme_requests(case):
    services = case["services"]
    segment_id, revision = _set_segment(case)

    for offset in (4,):
        request = {
            **_base(segment_id, case["revision_id"], revision, offset, offset),
            "vocal_event": {"kind": "laugh"},
        }
        with services["database"].session() as session:
            with pytest.raises(ValueError, match="exceeds the accepted text"):
                preview_speech_selection(
                    services, session, case["session_id"], request
                )
            with pytest.raises(ValueError, match="exceeds the accepted text"):
                get_speech_selection_context(
                    services,
                    session,
                    case["session_id"],
                    {key: value for key, value in request.items() if key != "vocal_event"},
                )

    # The astronaut emoji uses multiple code points; this position splits it.
    astral_grapheme = "A👩🏽‍🚀B"
    segment_id, revision = _set_segment(case, astral_grapheme)
    request = {
        **_base(segment_id, case["revision_id"], revision, 2, 2),
        "vocal_event": {"kind": "laugh"},
    }
    with services["database"].session() as session:
        with pytest.raises(ValueError, match="grapheme"):
            preview_speech_selection(services, session, case["session_id"], request)

    segment_id, revision = _set_segment(case, "")
    empty_request = _base(segment_id, case["revision_id"], revision, 0, 0)
    with services["database"].session() as session:
        with pytest.raises(ValueError, match="block is empty"):
            get_speech_selection_context(
                services, session, case["session_id"], empty_request
            )
        with pytest.raises(ValueError, match="block is empty"):
            preview_speech_selection(
                services, session, case["session_id"], {
                    **empty_request, "vocal_event": {"kind": "laugh"}
                },
            )
