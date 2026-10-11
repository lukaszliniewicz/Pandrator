from __future__ import annotations

from unittest.mock import patch

from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web import speech_selection
from pandrator.web.generation_controls import save_generation_controls
from pandrator.web.speech_plan_preview import preview_speech_segment
from pandrator.web.speech_selection import get_speech_selection_context
from tests.test_performance_plans import adopt, create
from tests.test_performance_plans import case as case  # noqa: PLC0414


def _request(case, segment_id, *, start=0, end=1):
    with case["services"]["database"].session() as session:
        revision = session.get(m.GenerationSegment, segment_id).revision
    return {
        "revision_id": case["revision_id"],
        "segment_id": segment_id,
        "expected_segment_revision": revision,
        "start": start,
        "end": end,
    }


def test_context_reads_adopted_xml_mixed_values_and_leaves_rows_unchanged(case, record_property):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    text = "A👋B"
    with services["database"].session() as session:
        save_generation_controls(
            session, case["session_id"], expected_revision=0,
            characters=[
                {"id": "alice", "display_name": "Alice", "delivery": {"emotion": "calm"}},
                {"id": "bob", "display_name": "Bob"},
            ],
        )
        segment = session.get(m.GenerationSegment, segment_id)
        segment.text = text
        segment.speech_plan_json = {"speech_xml": f'<segment id="{segment_id}"><narrator>{text}</narrator></segment>'}
    adopted_xml = (
        f'<segment id="{segment_id}">'
        '<speaker ref="alice" voice="one"><ins>Soft</ins>A</speaker>'
        '<speaker ref="bob" voice="two"><ins>Firm</ins>👋B</speaker></segment>'
    )
    plan = create(case, annotation_format="xml")
    response = case["post"](
        "/" + plan["id"],
        {"expected_version": plan["version"], "items": [
            {"segment_id": segment_id, "speech_xml": adopted_xml}
        ]},
        method="patch",
    )
    assert response.status_code == 200, response.get_json()
    changed = response.get_json()
    adopt(case, changed)
    request = _request(case, segment_id, end=2)
    statements = []

    def count_selects(_conn, _cursor, statement, _parameters, _context, _executemany):
        if statement.lstrip().upper().startswith("SELECT"):
            statements.append(statement)

    with services["database"].session() as session:
        before = list(session.scalars(select(m.PerformancePlan.id)))
        event.listen(services["database"].engine, "before_cursor_execute", count_selects)
        try:
            with patch.object(services["database"], "session", side_effect=AssertionError("nested session")):
                result = get_speech_selection_context(
                    services, session, case["session_id"], request
                )
        finally:
            event.remove(services["database"].engine, "before_cursor_execute", count_selects)
        after = list(session.scalars(select(m.PerformancePlan.id)))
    record_property("scoped_selects", len(statements))
    assert len(statements) <= 30, f"selection context used {len(statements)} SELECTs"
    assert before == after
    assert result["preview"] == preview_speech_segment(
        services, case["session_id"], revision_id=case["revision_id"],
        segment_id=segment_id,
    )
    assert result["selection"] == {"start": 0, "end": 2, "text": "A👋"}
    assert result["editable"] is True
    assert result["speech_xml"] == adopted_xml
    assert result["markup_reference"]["root_tag"] == "segment"
    assert result["current"]["speaker"] == "mixed"
    assert result["current"]["voice"] is None
    assert result["current"]["delivery"]["instruction"] is None
    assert result["current"]["mixed_fields"] == ["speaker", "voice", "instruction"]
    assert result["inherited"]["characters"][0]["delivery"]["emotion"] == "calm"
    assert result["preview"] is not None or result["preview_error"]
    assert result["capabilities"]


def test_context_http_read_scope_without_idempotency_and_legacy_read_only(case):
    services = case["services"]
    segment_id = case["segment_ids"][0]
    plan = create(case)
    response = case["post"](
        "/" + plan["id"],
        {"expected_version": plan["version"], "items": [{
            "segment_id": segment_id,
            "annotation": {
                "decision": "steer", "delivery": {"instruction": "legacy"}, "locked": True,
            },
        }]},
        method="patch",
    )
    assert response.status_code == 200, response.get_json()
    changed = response.get_json()
    adopt(case, changed)
    body = _request(case, segment_id)
    _token, raw = services["auth"].create_api_token("context read", scopes=["app.read"])
    response = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection-context',
        json=body, headers={"Authorization": f"Bearer {raw}"},
    )
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert result["editable"] is False
    assert result["speech_xml"] is None
    assert result["markup_reference"]["root_tag"] == "segment"
    assert result["locked"] is True
    assert "legacy structured" in result["edit_blocked_reason"]
    assert result["current"]["delivery"]["instruction"] == ""
    assert result["compilation_only"] is True
    assert result["preview"] is not None or result["preview_error"]
    with services["database"].session() as session:
        session.get(m.GenerationSegment, segment_id).revision += 1
    stale = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection-context',
        json=body, headers={"Authorization": f"Bearer {raw}"},
    )
    assert stale.status_code == 409


def test_context_reports_compile_error_but_preserves_authored_metadata(case, monkeypatch):
    segment_id = case["segment_ids"][0]
    request = _request(case, segment_id)

    def cannot_compile(*_args, **_kwargs):
        raise ValueError("Shorten the combined direction before rendering.")

    monkeypatch.setattr(speech_selection, "preview_speech_segment_in_session", cannot_compile)
    with case["services"]["database"].session() as session:
        result = speech_selection.get_speech_selection_context(
            case["services"], session, case["session_id"], request,
        )
    assert result["preview"] is None
    assert "Shorten" in result["preview_error"]
    assert result["selection"]["text"]
    assert result["capabilities"]
