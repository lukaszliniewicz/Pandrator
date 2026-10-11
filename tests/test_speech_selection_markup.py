from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest
from sqlalchemy import select

from pandrator.logic import speech_markup
from pandrator.logic.speech_markup import parse_speech_markup, speech_markup_reference
from pandrator.logic.speech_performance import Delivery, VocalEvent
from pandrator.web import models as m
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.settings_policy import RevisionConflict
from pandrator.web.speech_selection import (
    apply_speech_selection,
    get_speech_selection_context,
    preview_speech_selection,
)
from pandrator.web.speech_selection_schemas import SpeechSelectionRequest
from tests.test_performance_plans import adopt, create
from tests.test_performance_plans import case as case  # noqa: PLC0414


def _setup(case):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    text = "A 👋 B"
    with services["database"].session() as session:
        save_generation_controls(
            session, case["session_id"], expected_revision=0,
            characters=[{"id": "alice", "display_name": "Alice"}],
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.speech_plan_json = {
            "speech_xml": f'<segment id="{segment_id}" boundary_after="paragraph">{text}</segment>'
        }
        revision = segment.revision
    candidate = (
        f'<segment id="{segment_id}" boundary_after="paragraph">'
        '<speaker ref="alice"><ins>Softly</ins>A 👋</speaker>'
        '<event kind="pause" duration_ms="100"/>'
        '<narrator> B</narrator></segment>'
    )
    request = {
        "revision_id": case["revision_id"], "segment_id": segment_id,
        "expected_segment_revision": revision, "start": 0, "end": len(text),
        "speech_xml": candidate, "enable_performance": True,
    }
    return request, text


def test_whole_markup_preview_apply_preserves_text_speaker_and_event(case):
    request, text = _setup(case)
    services = case["services"]
    with services["database"].session() as session:
        before = list(session.scalars(select(m.PerformancePlan.id)))
        preview = preview_speech_selection(services, session, case["session_id"], request)
        after = list(session.scalars(select(m.PerformancePlan.id)))
    assert before == after == []
    parsed = parse_speech_markup(
        preview["speech_xml"], expected_segment_id=request["segment_id"],
        expected_text=text, characters=[{"id": "alice", "display_name": "Alice"}],
    )
    assert parsed.boundary_after == "paragraph"
    assert parsed.spans[0].speaker_id == "alice"
    assert parsed.spans[-1].narrator is True
    assert [(event.kind, event.offset) for event in parsed.events] == [("pause", 3)]
    assert preview["preview"]["capabilities"]["instructions"] == "none"
    assert preview["preview"]["parts"][0]["instructions"] == ""
    assert "Softly" not in preview["preview"]["parts"][0]["input"]
    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        saved = session.get(m.PerformancePlan, applied["performance_plan"]["id"])
        assert saved.manual_annotations_json[request["segment_id"]]["_speech_xml"] == parsed.xml
    assert applied["selection"]["text"] == text


def _adopted_modern_markup_setup(case):
    services = case["services"]
    segment_id, other_segment_id = case["segment_ids"][:2]
    text = "The clock stopped."
    source_ids = [17]
    provenance = {
        "source_cues": [{"reference": 17, "start_ms": 900, "end_ms": 2100}],
        "start_ms": 900,
        "end_ms": 2100,
        "duration_ms": 1200,
        "target_duration_ms": 1200,
        "anchor_start_ms": 900,
        "anchor_end_ms": 2100,
    }
    with services["database"].session() as session:
        save_generation_controls(
            session, case["session_id"], expected_revision=0,
            characters=[{"id": "alice", "display_name": "Alice"}],
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.speech_plan_json = {
            "speech_xml": f'<segment id="{segment_id}">{text}</segment>'
        }
        segment.source_segment_ids_json = source_ids
        segment.speech_block_provenance_json = provenance
        expected_segment_revision = segment.revision

    source_markup = (
        f'<segment id="{segment_id}" boundary_after="paragraph">'
        '<ins>Read warmly</ins>'
        '<speaker ref="alice">The clock '
        '<span><ins>Emphasize this</ins>stopped.</span></speaker>'
        '<event kind="pause" duration_ms="100"/>'
        '</segment>'
    )
    other_markup = (
        f'<segment id="{other_segment_id}" boundary_after="scene">'
        'Yet <ins>Preserve this direction</ins>this defeat would be temporary.'
        '</segment>'
    )
    plan = create(case, annotation_format="xml")
    response = case["post"](
        "/" + plan["id"],
        {
            "expected_version": plan["version"],
            "items": [
                {"segment_id": segment_id, "speech_xml": source_markup},
                {"segment_id": other_segment_id, "speech_xml": other_markup},
            ],
        },
        method="patch",
    )
    assert response.status_code == 200, response.get_json()
    adopt(case, response.get_json())
    return {
        "segment_id": segment_id,
        "other_segment_id": other_segment_id,
        "text": text,
        "source_ids": source_ids,
        "provenance": provenance,
        "expected_segment_revision": expected_segment_revision,
    }


@pytest.mark.parametrize("root_instruction", ["Read coolly", None])
def test_root_instruction_update_and_clear_preserve_adopted_markup_and_provenance(
    case, root_instruction,
):
    prepared = _adopted_modern_markup_setup(case)
    services = case["services"]
    segment_id = prepared["segment_id"]
    text = prepared["text"]
    context_request = {
        "revision_id": case["revision_id"],
        "segment_id": segment_id,
        "expected_segment_revision": prepared["expected_segment_revision"],
        "start": 0,
        "end": len(text),
    }
    with services["database"].session() as session:
        context = get_speech_selection_context(
            services, session, case["session_id"], context_request,
        )
    assert context["speech_xml"]
    assert {row["id"] for row in context["inherited"]["characters"]} == {"alice"}
    assert context["inherited"]["characters"][0]["display_name"] == "Alice"

    root = ET.fromstring(context["speech_xml"])
    root_control = next(child for child in root if child.tag == "ins")
    if root_instruction is None:
        root.remove(root_control)
    else:
        root_control.text = root_instruction
    candidate_xml = ET.tostring(root, encoding="unicode")
    request = {**context_request, "speech_xml": candidate_xml}

    with services["database"].session() as session:
        adopted = session.scalar(
            select(m.PerformancePlan).where(
                m.PerformancePlan.plan_revision_id == case["revision_id"],
                m.PerformancePlan.status == "adopted",
            )
        )
        other_record_before = dict(
            adopted.manual_annotations_json[prepared["other_segment_id"]]
        )
        preview = preview_speech_selection(
            services, session, case["session_id"], request,
        )

    assert preview["selection"] == {"start": 0, "end": len(text), "text": text}
    parsed = parse_speech_markup(
        preview["speech_xml"], expected_segment_id=segment_id,
        expected_text=text, characters=[{"id": "alice", "display_name": "Alice"}],
    )
    assert parsed.segment_id == segment_id
    assert parsed.transcript == text
    assert parsed.boundary_after == "paragraph"
    assert {span.speaker_id for span in parsed.spans} == {"alice"}
    stopped = next(span for span in parsed.spans if span.text == "stopped.")
    assert "Emphasize this" in stopped.delivery["instruction"]
    assert (root_instruction or "") in stopped.delivery["instruction"]
    assert [(event.kind, event.offset, event.duration_ms) for event in parsed.events] == [
        ("pause", len(text), 100),
    ]
    parsed_root = ET.fromstring(parsed.xml)
    assert parsed_root.attrib["id"] == segment_id
    assert parsed_root.attrib["boundary_after"] == "paragraph"
    direct_root_instruction = parsed_root.find("./ins")
    if root_instruction is None:
        assert direct_root_instruction is None
    else:
        assert direct_root_instruction is not None
        assert direct_root_instruction.text == root_instruction
    speaker = parsed_root.find("./speaker")
    assert speaker is not None and speaker.attrib["ref"] == "alice"
    phrase = speaker.find("./span")
    assert phrase is not None
    assert phrase.find("./ins").text == "Emphasize this"

    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "expected_preview_revision": preview["preview_revision"]},
        )
        saved_plan = session.get(m.PerformancePlan, applied["performance_plan"]["id"])
        saved_target = saved_plan.manual_annotations_json[segment_id]
        saved_other = saved_plan.manual_annotations_json[prepared["other_segment_id"]]
        saved_segment = session.get(m.GenerationSegment, segment_id)
        assert saved_target["_speech_xml"] == parsed.xml
        assert saved_other["_speech_xml"] == other_record_before["_speech_xml"]
        assert saved_other.get("locked") == other_record_before.get("locked")
        assert saved_segment.source_segment_ids_json == prepared["source_ids"]
        assert saved_segment.speech_block_provenance_json == prepared["provenance"]

    assert applied["selection"] == {"start": 0, "end": len(text), "text": text}
    assert applied["speech_xml"] == parsed.xml


@pytest.mark.parametrize("change", [
    lambda xml, sid: xml.replace("A 👋", "Changed 👋"),
    lambda xml, sid: xml.replace(f'id="{sid}"', 'id="other"', 1),
    lambda xml, sid: '<!DOCTYPE segment>' + xml,
    lambda xml, sid: xml.replace("<narrator>", "<unknown>"),
    lambda xml, sid: xml.replace('ref="alice"', 'ref="unknown"'),
    lambda xml, sid: xml.replace('boundary_after="paragraph"', 'boundary_after="scene"'),
])
def test_invalid_whole_markup_rejected_without_new_plan(case, change):
    request, _text = _setup(case)
    request["speech_xml"] = change(request["speech_xml"], request["segment_id"])
    with case["services"]["database"].session() as session:
        with pytest.raises(ValueError):
            preview_speech_selection(case["services"], session, case["session_id"], request)
        with pytest.raises(ValueError):
            apply_speech_selection(
                case["services"], session, case["session_id"],
                {**request, "expected_preview_revision": "0" * 64},
            )
        assert session.scalar(select(m.PerformancePlan.id)) is None


def test_whole_markup_rejects_partial_range_and_patch_combination(case):
    request, _text = _setup(case)
    with pytest.raises(ValueError, match="must be a string"):
        SpeechSelectionRequest.model_validate({**request, "speech_xml": None})
    with case["services"]["database"].session() as session:
        with pytest.raises(ValueError, match="complete speech block"):
            preview_speech_selection(
                case["services"], session, case["session_id"], {**request, "end": 2},
            )
    for patch in ({"speaker": "narrator"}, {"voice": None},
                  {"delivery": {"emotion": "sad"}}, {"character_id": None},
                  {"voice_category": None}):
        with pytest.raises(ValueError, match="cannot be combined"):
            SpeechSelectionRequest.model_validate({**request, **patch})


def test_whole_markup_stale_controls_and_locked_adoption_still_guarded(case):
    request, _text = _setup(case)
    services = case["services"]
    with services["database"].session() as session:
        preview = preview_speech_selection(services, session, case["session_id"], request)
    with services["database"].session() as session:
        save_generation_controls(
            session, case["session_id"], expected_revision=1,
            characters=[{"id": "alice", "display_name": "Alice", "notes": "changed"}],
        )
    with services["database"].session() as session:
        with pytest.raises(RevisionConflict, match="stale"):
            apply_speech_selection(
                services, session, case["session_id"],
                {**request, "expected_preview_revision": preview["preview_revision"]},
            )
    plan = create(case, annotation_format="xml")
    response = case["post"](
        "/" + plan["id"],
        {"expected_version": plan["version"], "items": [{
            "segment_id": request["segment_id"],
            "speech_xml": request["speech_xml"], "locked": True,
        }]}, method="patch",
    )
    assert response.status_code == 200, response.get_json()
    adopt(case, response.get_json())
    with services["database"].session() as session:
        locked_preview = preview_speech_selection(services, session, case["session_id"], request)
    with services["database"].session() as session:
        with pytest.raises(RevisionConflict, match="unlock"):
            apply_speech_selection(
                services, session, case["session_id"],
                {**request, "expected_preview_revision": locked_preview["preview_revision"]},
            )
    with services["database"].immediate_session() as session:
        applied = apply_speech_selection(
            services, session, case["session_id"],
            {**request, "unlock_locked": True,
             "expected_preview_revision": locked_preview["preview_revision"]},
        )
    assert applied["adopted"]["status"] == "adopted"


def test_markup_reference_tracks_authoritative_parser_contract():
    reference = speech_markup_reference()
    assert reference["root_tag"] == "segment"
    assert set(reference["element_tags"]) == speech_markup._ELEMENT_TAGS
    assert {
        tag: set(attributes) for tag, attributes in reference["element_attributes"].items()
    } == speech_markup._ELEMENT_ATTRIBUTES
    assert reference["control_tags"] == speech_markup._CONTROL_FIELDS
    assert reference["delivery_schema"] == Delivery.model_json_schema()
    assert reference["vocal_event_schema"] == VocalEvent.model_json_schema()
    assert reference["limits"] == {
        "xml_bytes": speech_markup._MAX_XML_BYTES,
        "nodes": speech_markup._MAX_NODES,
        "depth": speech_markup._MAX_DEPTH,
        "delivery_spans": 32,
        "events": 16,
    }
