from __future__ import annotations

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.web import models as m
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.speech_selection import (
    apply_speech_selection,
    preview_speech_selection,
)
from tests.test_performance_plans import case as case


def test_selection_preview_is_read_only_and_apply_preserves_other_units(case):
    services = case["services"]
    session_id = case["session_id"]
    revision_id = case["revision_id"]
    first_id, second_id = case["segment_ids"][:2]
    with services["database"].session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {"id": "alice", "display_name": "Alice", "voice_category": "female"},
                {"id": "bob", "display_name": "Bob", "voice_category": "male"},
            ],
        )
        first = session.get(m.GenerationSegment, first_id)
        first.text = "Hello 👋 world."
        first.speech_plan_json = {
            "speech_xml": (
                f'<segment id="{first_id}"><speaker ref="alice">Hello 👋</speaker>'
                ' world.</segment>'
            )
        }
        expected_segment_revision = first.revision

        request = {
            "revision_id": revision_id,
            "segment_id": first_id,
            "expected_segment_revision": expected_segment_revision,
            "start": 6,
            "end": 8,
            "speaker": "character",
            "character_id": "bob",
            "voice": None,
            "delivery": {"emotion": "serious"},
            "enable_casting": True,
        }
        before = session.scalar(select(m.PerformancePlan.id))
        preview = preview_speech_selection(services, session, session_id, request)
        assert before is None
        assert session.scalar(select(m.PerformancePlan.id)) is None
        assert preview["selection"]["text"] == "👋 "
        assert preview["compilation_only"] is True
        assert "serious" in preview["speech_xml"]

        result = apply_speech_selection(
            services,
            session,
            session_id,
            {
                **request,
                "expected_preview_revision": preview["preview_revision"],
            },
        )
        assert result["adopted"]["status"] == "adopted"
        assert result["selection"]["text"] == "👋 "
        assert result["current_flags"]["casting_enabled"] is True
        assert session.get(m.GenerationSegment, second_id).text == "Yet this defeat would be temporary."
        plans = list(
            session.scalars(
                select(m.PerformancePlan).where(
                    m.PerformancePlan.plan_revision_id == revision_id
                )
            )
        )
        assert len(plans) == 1
        assert plans[0].status == "adopted"


def test_unnamed_dialogue_range_keeps_adjacent_markup_and_events(case):
    services = case["services"]
    session_id = case["session_id"]
    segment_id = case["segment_ids"][0]
    with services["database"].session() as session:
        save_generation_controls(
            session, session_id, expected_revision=0,
            characters=[{"id": "alice", "display_name": "Alice"}],
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = "A 👋 B"
        segment.speech_plan_json = {"speech_xml": (
            f'<segment id="{segment_id}" boundary_after="paragraph">'
            '<speaker ref="alice">A </speaker>'
            '<speaker ref="alice"><em>bright</em>👋</speaker>'
            '<event kind="pause" duration_ms="100"/>'
            '<narrator> B</narrator></segment>'
        )}
        expected_revision = segment.revision
    request = {
        "revision_id": case["revision_id"], "segment_id": segment_id,
        "expected_segment_revision": expected_revision,
        "start": 2, "end": 3, "speaker": "dialogue",
        "voice_category": "female", "enable_casting": True,
    }
    with services["database"].session() as session:
        preview = preview_speech_selection(services, session, session_id, request)
        parsed = parse_speech_markup(
            preview["speech_xml"], expected_segment_id=segment_id,
            expected_text="A 👋 B",
            characters=[{"id": "alice", "display_name": "Alice"}],
        )
        assert parsed.transcript == "A 👋 B"
        assert parsed.boundary_after == "paragraph"
        assert len(parsed.events) == 1 and parsed.events[0].offset == 3
        assert parsed.spans[0].speaker_id == "alice"
        assert parsed.spans[1].speaker_id is None
        assert parsed.spans[1].dialogue is True
        assert parsed.spans[1].voice_category == "female"
        assert parsed.spans[1].delivery["emotion"] == "bright"
        assert parsed.spans[-1].narrator is True
        result = apply_speech_selection(
            services, session, session_id,
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        assert result["selection"]["text"] == "👋"
