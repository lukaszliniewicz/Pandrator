"""Audiobook repair preserves speech semantics and requires draft adoption."""

import uuid

import pytest
from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.web import models as m
from pandrator.web.generation_cast_runtime import remap_markup
from pandrator.web.speech_boundaries import assembly_pause, freeze_boundaries
from tests.test_performance_plans import adopt, case as case, create, edit


def topology(case, revision, **operation):
    if operation["action"] == "split":
        operation.setdefault("text_layer", "display")
    return case["client"].post(
        f"/api/v1/sessions/{case['session_id']}/generation-plan/topology",
        json={"expected_revision_id": revision, **operation},
        headers={
            **case["headers"],
            "If-Match": f'"{revision}"',
            "Idempotency-Key": str(uuid.uuid4()),
        },
    )


def rows(case, revision):
    with case["services"]["database"].session() as session:
        return list(
            session.scalars(
                select(m.GenerationSegment)
                .where(
                    m.GenerationSegment.plan_revision_id == revision,
                )
                .order_by(m.GenerationSegment.ordinal)
            )
        )


def test_draft_resegment_then_adopt_keeps_text_lineage_and_historical_plan(case):
    before = rows(case, case["revision_id"])
    response = topology(
        case,
        case["revision_id"],
        action="resegment",
        segment_ids=case["segment_ids"][:2],
        boundaries=[11],
    )
    assert response.status_code == 201, response.get_json()
    draft = response.get_json()
    assert draft["is_draft"] and draft["active_plan_revision_id"] == case["revision_id"]
    after = rows(case, draft["plan_revision_id"])
    assert " ".join(row.text for row in before) == " ".join(row.text for row in after)
    assert len(draft["lineage"][before[0].id]) == 2
    assert draft["lineage"][before[1].id] == [after[1].id]
    assert after[0].silence_after_ms == 0
    adopted = topology(
        case,
        case["revision_id"],
        action="restore",
        target_revision_id=draft["plan_revision_id"],
    )
    assert adopted.status_code == 201, adopted.get_json()
    assert not adopted.get_json()["is_draft"]
    assert [row.text for row in rows(case, case["revision_id"])] == [
        row.text for row in before
    ]


def test_draft_rejects_same_revision_text_change(case):
    response = topology(
        case,
        case["revision_id"],
        action="resegment",
        segment_ids=case["segment_ids"][:1],
        boundaries=[11],
    )
    draft = response.get_json()
    with case["services"]["database"].session() as session:
        session.get(m.GenerationSegment, case["segment_ids"][0]).text += " Changed."
    response = topology(
        case,
        case["revision_id"],
        action="restore",
        target_revision_id=draft["plan_revision_id"],
    )
    assert response.status_code == 409, response.get_json()


@pytest.mark.parametrize(
    "changes",
    [
        {"segment_ids": [2, 0]},
        {"boundaries": [11, 11]},
        {"boundaries": [True]},
        {"boundaries": [0]},
        {"max_chars": 20},
        {"boundaries": [11], "max_chars": 80},
    ],
)
def test_invalid_resegmentation_is_rejected(case, changes):
    operation = {
        "action": "resegment",
        "segment_ids": case["segment_ids"][:2],
        "boundaries": [11],
    }
    changes = dict(changes)
    if "segment_ids" in changes:
        changes["segment_ids"] = [
            case["segment_ids"][index] for index in changes["segment_ids"]
        ]
    operation.update(changes)
    response = topology(case, case["revision_id"], **operation)
    assert response.status_code == 422, response.get_json()


def test_split_merge_preserves_narration_dialogue_delivery_and_boundary(case):
    sid = case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, sid)
        row.speaker = None
        row.speech_plan_json = {
            "speech_xml": f'<segment id="{sid}" boundary_after="paragraph"><dialogue><em>sad</em>The request</dialogue><narrator> was rejected.</narrator></segment>'
        }
    response = topology(
        case, case["revision_id"], action="split", segment_id=sid, cursor=11
    )
    assert response.status_code == 201, response.get_json()
    split = response.get_json()
    parts = rows(case, split["plan_revision_id"])
    parsed = [
        parse_speech_markup(
            remap_markup(row.speech_plan_json["speech_xml"], row.id, row.text, []),
            expected_segment_id=row.id,
            expected_text=row.text,
        )
        for row in parts[:2]
    ]
    assert parsed[0].boundary_after == "continuation"
    assert (
        parsed[0].spans[0].dialogue and parsed[0].spans[0].delivery["emotion"] == "sad"
    )
    assert parsed[1].spans[0].narrator and parsed[1].boundary_after == "paragraph"
    merged = topology(
        case,
        split["plan_revision_id"],
        action="merge",
        left_segment_id=parts[0].id,
        right_segment_id=parts[1].id,
    )
    assert merged.status_code == 201, merged.get_json()
    row = rows(case, merged.get_json()["plan_revision_id"])[0]
    assert row.text == "The request was rejected."
    parsed = parse_speech_markup(
        remap_markup(row.speech_plan_json["speech_xml"], row.id, row.text, []),
        expected_segment_id=row.id,
        expected_text=row.text,
    )
    assert parsed.boundary_after == "paragraph"
    assert any(
        span.dialogue and span.delivery["emotion"] == "sad" for span in parsed.spans
    )
    assert any(span.narrator for span in parsed.spans)


def test_copy_performance_retains_only_unchanged_descendants(case):
    plan = adopt(case, edit(case, create(case), index=2))
    response = topology(
        case,
        case["revision_id"],
        action="split",
        segment_id=case["segment_ids"][0],
        cursor=11,
    )
    assert response.status_code == 201, response.get_json()
    case["revision_id"] = response.get_json()["plan_revision_id"]
    copied = create(case, copy_from_id=plan["id"])
    assert copied["locked_count"] == 1
    assert (
        copied["items"][-1]["annotation"]["delivery"]["instruction"]
        == "Mark a restrained contrast"
    )
    assert copied["items"][0]["annotation"] is None


def test_frozen_continuation_overrides_stored_pause_and_detects_text_drift(case):
    sid = case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, sid)
        row.silence_after_ms = 700
        row.speech_plan_json = {
            "speech_xml": f'<segment id="{sid}" boundary_after="continuation">{row.text}</segment>'
        }
        snapshot = {"audio": {"sentence_silence_ms": 250, "paragraph_silence_ms": 700}}
        freeze_boundaries(session, case["revision_id"], snapshot)
        assert assembly_pause(row, snapshot) == 0
        row.silence_after_ms = 999
        assert assembly_pause(row, snapshot) == 0
        row.text += " Changed."
        with pytest.raises(ValueError, match="changed"):
            assembly_pause(row, snapshot)


def test_draft_cannot_be_selected_or_generated_without_adoption(case):
    from pandrator.web.speech_plan_workspace import (
        freeze_speech_snapshot,
        select_speech_plan,
    )
    from pandrator.web.workspace import RevisionConflict

    result = topology(
        case,
        case["revision_id"],
        action="resegment",
        segment_ids=case["segment_ids"][:1],
        boundaries=[11],
    ).get_json()
    with case["services"]["database"].session() as session:
        with pytest.raises(RevisionConflict, match="Adopt"):
            select_speech_plan(
                session,
                case["session_id"],
                revision_id=result["plan_revision_id"],
                expected_plan_revision_id=case["revision_id"],
            )
        with pytest.raises(RevisionConflict, match="Adopt"):
            freeze_speech_snapshot(session, result["plan_revision_id"], {})


def test_split_refuses_to_drop_event_in_trimmed_whitespace(case):
    sid = case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, sid)
        row.speech_plan_json = {
            "speech_xml": f'<segment id="{sid}">The request<event kind="sigh"/> was rejected.</segment>'
        }
    response = topology(
        case, case["revision_id"], action="split", segment_id=sid, cursor=11
    )
    assert response.status_code == 422, response.get_json()
    assert "vocal event" in str(response.get_json())


def test_rejected_resume_keeps_run_paused(case, monkeypatch):
    service = case["services"]["generation"]
    with case["services"]["database"].session() as session:
        run = m.GenerationRun(
            session_id=case["session_id"],
            plan_revision_id=case["revision_id"],
            sequence_number=1,
            status="paused",
        )
        session.add(run)
        session.flush()
        run_id = run.id

    def reject(*_args, **_kwargs):
        raise ValueError("audio.cpp model maintenance is active")

    monkeypatch.setattr(service.jobs, "enqueue_in_session", reject)
    with pytest.raises(ValueError, match="maintenance"):
        service.resume(run_id)
    with case["services"]["database"].session() as session:
        assert session.get(m.GenerationRun, run_id).status == "paused"


@pytest.mark.parametrize("change", ["markup", "pause", "voice_id"])
def test_copy_does_not_reuse_annotation_after_structural_change(case, change):
    plan = adopt(case, edit(case, create(case), index=2))
    response = topology(
        case,
        case["revision_id"],
        action="split",
        segment_id=case["segment_ids"][0],
        cursor=11,
    )
    case["revision_id"] = response.get_json()["plan_revision_id"]
    last = rows(case, case["revision_id"])[-1]
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, last.id)
        if change == "markup":
            row.speech_plan_json = {
                "speech_xml": f'<segment id="{row.id}"><narrator>{row.text}</narrator></segment>'
            }
        elif change == "pause":
            row.silence_after_ms += 100
        else:
            voice = m.Voice(name="New reference")
            session.add(voice)
            session.flush()
            row.voice_id = voice.id
    copied = create(case, copy_from_id=plan["id"])
    assert copied["locked_count"] == 0
    assert copied["items"][-1]["annotation"] is None


def test_plain_pauses_survive_generation_time_text_optimization(case):
    with case["services"]["database"].session() as session:
        row = session.get(m.GenerationSegment, case["segment_ids"][0])
        row.silence_after_ms = 250
        snapshot = {}
        freeze_boundaries(session, row.plan_revision_id, snapshot)
        row.optimized_text = "The request had been rejected."
        assert assembly_pause(row, snapshot) == 250
