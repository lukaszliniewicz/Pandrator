from __future__ import annotations

import pytest
from sqlalchemy import select

from pandrator.web import models as m
from pandrator.web import speech_selection
from pandrator.web.generation_controls import save_generation_controls
from tests.test_performance_plans import case as case  # noqa: PLC0414
from tests.test_performance_plans import create


def _plans(case):
    with case["services"]["database"].session() as session:
        return list(
            session.scalars(
                select(m.PerformancePlan).where(
                    m.PerformancePlan.plan_revision_id == case["revision_id"]
                )
            )
        )


def _idempotency_ids(case):
    with case["services"]["database"].session() as session:
        return set(session.scalars(select(m.ApiIdempotency.id)))


def _request(case, *, segment_id=None, **values):
    segment_id = segment_id or case["segment_ids"][0]
    with case["services"]["database"].session() as session:
        segment = session.get(m.GenerationSegment, segment_id)
        expected_revision = segment.revision
    return {
        "revision_id": case["revision_id"],
        "segment_id": segment_id,
        "expected_segment_revision": expected_revision,
        "start": 0,
        "end": 5,
        "speaker": "narrator",
        **values,
    }


def _token_headers(case, *scopes):
    _record, raw = case["services"]["auth"].create_api_token(
        "speech selection test token", scopes=scopes
    )
    return {"Authorization": f"Bearer {raw}"}


def _set_tts(case, **values):
    settings = case["services"]["workspace_settings"]
    current = settings.get(case["session_id"], "tts")
    settings.update(
        case["session_id"],
        "tts",
        current["revision"],
        {**current["effective"], **values},
    )


def _prepare_locked_xml_plan(case):
    services = case["services"]
    session_id = case["session_id"]
    first_id, second_id = case["segment_ids"][:2]
    with services["database"].session() as session:
        save_generation_controls(
            session,
            session_id,
            expected_revision=0,
            characters=[
                {"id": "alice", "display_name": "Alice", "voice_category": "female"}
            ],
        )
    first_text = "The request was rejected."
    second_text = "Yet this defeat would be temporary."
    first_xml = (
        f'<segment id="{first_id}"><speaker ref="alice">{first_text}</speaker></segment>'
    )
    second_xml = f'<segment id="{second_id}"><narrator>{second_text}</narrator></segment>'
    plan = create(case, annotation_format="xml")
    edited = case["post"](
        f'/{plan["id"]}',
        {
            "expected_version": plan["version"],
            "items": [
                {"segment_id": first_id, "speech_xml": first_xml, "locked": True},
                {"segment_id": second_id, "speech_xml": second_xml, "locked": True},
            ],
        },
        method="patch",
    )
    assert edited.status_code == 200, edited.get_json()
    adopted = case["post"](
        f'/{plan["id"]}/adopt',
        {"expected_version": edited.get_json()["version"], "accept_unanalysed": True},
    )
    assert adopted.status_code == 200, adopted.get_json()
    _set_tts(case, casting_enabled=False, performance_enabled=False)
    with services["database"].session() as session:
        take = m.AudioTake(
            generation_segment_id=first_id,
            kind="tts",
            status="completed",
            is_active=True,
        )
        session.add(take)
        session.flush()
        take_id = take.id
    return {
        "plan_id": plan["id"],
        "first_id": first_id,
        "second_id": second_id,
        "second_xml": second_xml,
        "take_id": take_id,
    }


def test_selection_http_scopes_preview_without_idempotency_and_apply_replays_once(case):
    body = _request(case)
    read_headers = _token_headers(case, "app.read")
    before_idempotency = _idempotency_ids(case)
    preview = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection-preview',
        json=body,
        headers=read_headers,
    )
    assert preview.status_code == 200, preview.get_json()
    assert _plans(case) == []
    assert _idempotency_ids(case) == before_idempotency

    apply_body = {**body, "expected_preview_revision": preview.get_json()["preview_revision"]}
    denied = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json=apply_body,
        headers=read_headers,
    )
    assert denied.status_code == 403
    assert denied.get_json()["error"]["code"] == "scope_denied"

    missing_key = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json=apply_body,
        headers=case["headers"],
    )
    assert missing_key.status_code == 422
    assert _plans(case) == []

    headers = {**case["headers"], "Idempotency-Key": "speech-selection-replay"}
    applied = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json=apply_body,
        headers=headers,
    )
    assert applied.status_code == 200, applied.get_json()
    replayed = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json=apply_body,
        headers=headers,
    )
    assert replayed.status_code == 200, replayed.get_json()
    assert replayed.headers["Idempotency-Replayed"] == "true"
    assert len(_plans(case)) == 1


@pytest.mark.parametrize("mutation", ["segment", "cast", "tts", "adopted", "active"])
def test_selection_preview_guard_rejects_changed_state_without_new_plan(case, mutation):
    if mutation == "adopted":
        existing = create(case, annotation_format="xml")
        adopted = case["post"](
            f'/{existing["id"]}/adopt',
            {"expected_version": existing["version"], "accept_unanalysed": True},
        )
        assert adopted.status_code == 200, adopted.get_json()

    body = _request(case)
    with case["services"]["database"].session() as session:
        preview = speech_selection.preview_speech_selection(
            case["services"], session, case["session_id"], body
        )
    before_ids = {plan.id for plan in _plans(case)}

    if mutation == "segment":
        with case["services"]["database"].session() as session:
            session.get(m.GenerationSegment, body["segment_id"]).revision += 1
    elif mutation == "cast":
        with case["services"]["database"].session() as session:
            controls = save_generation_controls(
                session,
                case["session_id"],
                expected_revision=0,
                characters=[
                    {"id": "cast-change", "display_name": "Cast change", "voice_category": "male"}
                ],
            )
            assert controls["revision"] == 1
    elif mutation == "tts":
        _set_tts(case, model="guard-change-model")
    elif mutation == "adopted":
        with case["services"]["database"].session() as session:
            plan = session.get(m.PerformancePlan, existing["id"])
            plan.version += 1
    else:
        with case["services"]["database"].session() as session:
            plan = session.scalar(
                select(m.GenerationPlan).where(
                    m.GenerationPlan.session_id == case["session_id"]
                )
            )
            plan.active_revision_id = "selected-revision-changed"

    apply_body = {**body, "expected_preview_revision": preview["preview_revision"]}
    result = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json=apply_body,
        headers={
            **case["headers"],
            "Idempotency-Key": f"selection-guard-{mutation}",
        },
    )
    assert result.status_code == 409, result.get_json()
    assert result.get_json()["error"]["code"] == "revision_conflict"
    assert {plan.id for plan in _plans(case)} == before_ids


def test_locked_selection_requires_unlock_then_preserves_plan_audio_and_other_markup(case):
    prepared = _prepare_locked_xml_plan(case)
    body = _request(
        case,
        segment_id=prepared["first_id"],
        start=0,
        end=3,
        speaker="narrator",
        delivery={"emotion": "new"},
    )
    with case["services"]["database"].session() as session:
        preview = speech_selection.preview_speech_selection(
            case["services"], session, case["session_id"], body
        )
    assert preview["locked"] is True
    assert preview["source_performance_plan_id"] == prepared["plan_id"]
    assert preview["current_flags"] == {
        "casting_enabled": False,
        "performance_enabled": False,
    }
    assert preview["preview"]["parts"][0]["instructions"] == ""

    apply_body = {**body, "expected_preview_revision": preview["preview_revision"]}
    rejected = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json=apply_body,
        headers={**case["headers"], "Idempotency-Key": "locked-without-unlock"},
    )
    assert rejected.status_code == 409, rejected.get_json()
    assert rejected.get_json()["error"]["code"] == "revision_conflict"
    assert {plan.id for plan in _plans(case)} == {prepared["plan_id"]}

    applied = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json={**apply_body, "unlock_locked": True},
        headers={**case["headers"], "Idempotency-Key": "locked-with-unlock"},
    )
    assert applied.status_code == 200, applied.get_json()
    payload = applied.get_json()
    assert payload["current_flags"] == {
        "casting_enabled": False,
        "performance_enabled": False,
    }
    assert payload["source_performance_plan_id"] == prepared["plan_id"]

    with case["services"]["database"].session() as session:
        old = session.get(m.PerformancePlan, prepared["plan_id"])
        new = session.get(m.PerformancePlan, payload["performance_plan"]["id"])
        take = session.get(m.AudioTake, prepared["take_id"])
        assert old.status == "superseded"
        assert old.manual_annotations_json[prepared["second_id"]]["_speech_xml"] == prepared["second_xml"]
        assert old.manual_annotations_json[prepared["second_id"]]["locked"] is True
        assert new.manual_annotations_json[prepared["second_id"]]["_speech_xml"] == prepared["second_xml"]
        assert new.manual_annotations_json[prepared["second_id"]]["locked"] is True
        assert take is not None and take.status == "completed"


def test_selection_failed_apply_rolls_back_supersession(case, monkeypatch):
    prepared = _prepare_locked_xml_plan(case)
    body = _request(case, segment_id=prepared["first_id"], start=0, end=3)
    with case["services"]["database"].session() as session:
        preview = speech_selection.preview_speech_selection(
            case["services"], session, case["session_id"], body
        )
    request = {
        **body,
        "unlock_locked": True,
        "expected_preview_revision": preview["preview_revision"],
    }
    original_adopt = speech_selection.plans.adopt_plan

    def fail_after_adopt(*args, **kwargs):
        original_adopt(*args, **kwargs)
        raise RuntimeError("injected post-adoption failure")

    monkeypatch.setattr(speech_selection.plans, "adopt_plan", fail_after_adopt)
    with pytest.raises(RuntimeError, match="injected post-adoption failure"), case[
        "services"
    ]["database"].immediate_session() as session:
        speech_selection.apply_speech_selection(
            case["services"], session, case["session_id"], request
        )

    with case["services"]["database"].session() as session:
        plans = list(
            session.scalars(
                select(m.PerformancePlan).where(
                    m.PerformancePlan.plan_revision_id == case["revision_id"]
                )
            )
        )
        old = session.get(m.PerformancePlan, prepared["plan_id"])
        assert len(plans) == 1
        assert old.status == "adopted"


def test_legacy_delivery_spans_and_events_are_not_silently_converted(case):
    plan = create(case)
    segment_id = case["segment_ids"][0]
    annotation = {
        "decision": "steer",
        "delivery": {"instruction": "legacy delivery"},
        "spans": [
            {
                "anchor": {"quote": "request"},
                "delivery": {"emotion": "tense"},
            }
        ],
        "events": [{"kind": "sigh", "anchor": {"quote": "rejected"}}],
        "locked": True,
    }
    edited = case["post"](
        f'/{plan["id"]}',
        {
            "expected_version": plan["version"],
            "items": [{"segment_id": segment_id, "annotation": annotation}],
        },
        method="patch",
    )
    assert edited.status_code == 200, edited.get_json()
    adopted = case["post"](
        f'/{plan["id"]}/adopt',
        {"expected_version": edited.get_json()["version"], "accept_unanalysed": True},
    )
    assert adopted.status_code == 200, adopted.get_json()
    before = _plans(case)[0].manual_annotations_json[segment_id]
    body = _request(case, segment_id=segment_id)
    preview = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection-preview',
        json=body,
        headers=case["headers"],
    )
    assert preview.status_code == 422, preview.get_json()
    assert "legacy structured directions" in preview.get_json()["error"]["message"]

    apply = case["client"].post(
        f'/api/v1/sessions/{case["session_id"]}/speech-plan/selection',
        json={**body, "expected_preview_revision": "0" * 64},
        headers={**case["headers"], "Idempotency-Key": "legacy-no-conversion"},
    )
    assert apply.status_code == 422, apply.get_json()
    plans = _plans(case)
    assert len(plans) == 1
    assert plans[0].id == plan["id"]
    assert plans[0].status == "adopted"
    assert plans[0].manual_annotations_json[segment_id] == before
