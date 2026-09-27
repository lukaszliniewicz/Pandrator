"""Independent XML speaker and delivery pass contracts."""

import threading
from types import SimpleNamespace
from unittest.mock import patch

from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web.generation_controls import get_generation_controls
from tests.test_performance_plans import _llm_response
from tests.test_performance_plans import case as performance_case

case = performance_case


def draft(case, purpose):
    response = case["post"](
        "",
        {
            "expected_plan_revision_id": case["revision_id"],
            "annotation_format": "xml",
            "mode": "passive",
            "purpose": purpose,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def claim(case, plan):
    response = case["post"](f"/{plan['id']}/claim")
    assert response.status_code == 200, response.get_json()
    return response.get_json()


def submit(case, plan, lease, items, proposals=None):
    return case["post"](
        f"/{plan['id']}/batches/{lease['batch_id']}/submit",
        {
            "lease_token": lease["lease_token"],
            "items": items,
            "character_proposals": proposals or [],
        },
    )


def source_items(lease):
    return [
        {"segment_id": item["segment_id"], "speech_xml": item["speech_xml"]}
        for item in lease["batch"]["items"]
    ]


def speaker_xml(item, *, instruction=False, voice=False, text=None):
    segment_id = item["segment_id"]
    original = item["speech_xml"]
    words = text or original.split(">", 1)[1].rsplit("<", 1)[0]
    direction = "<ins>bright</ins>" if instruction else ""
    voice_attr = ' voice="Invented"' if voice else ""
    return (
        f'<segment id="{segment_id}">{direction}<dialogue>'
        f'<speaker ref="c-alice"{voice_attr}>{words}</speaker>'
        "</dialogue></segment>"
    )


def test_speakers_then_delivery_keeps_cumulative_xml_and_independent_flags(case):
    speaker_plan = draft(case, "speakers")
    assert speaker_plan["settings"]["purpose"] == "speakers"
    lease = claim(case, speaker_plan)
    items = source_items(lease)
    items[0]["speech_xml"] = speaker_xml(items[0])
    proposal = {"id": "c-alice", "display_name": "Alice"}
    response = submit(case, speaker_plan, lease, items, [proposal])
    assert response.status_code == 200, response.get_json()
    version = response.get_json()["version"]
    adopted = case["post"](
        f"/{speaker_plan['id']}/adopt",
        {"expected_version": version, "enable": True},
    )
    assert adopted.status_code == 200, adopted.get_json()
    assert adopted.get_json()["enabled"] is False
    with case["services"]["database"].session() as session:
        assert [
            entry["id"]
            for entry in get_generation_controls(session, case["session_id"])["characters"]
        ] == ["c-alice"]

    delivery_plan = draft(case, "delivery")
    assert "c-alice" in delivery_plan["items"][0]["speech_xml"]
    delivery_lease = claim(case, delivery_plan)
    delivery_items = source_items(delivery_lease)
    delivery_items[0]["speech_xml"] = delivery_items[0]["speech_xml"].replace(
        ">", "><ins>bright</ins>", 1
    )
    response = submit(case, delivery_plan, delivery_lease, delivery_items)
    assert response.status_code == 200, response.get_json()
    adopted = case["post"](
        f"/{delivery_plan['id']}/adopt",
        {"expected_version": response.get_json()["version"], "enable": False},
    )
    assert adopted.status_code == 200, adopted.get_json()
    next_speaker_plan = draft(case, "speakers")
    assert "c-alice" in next_speaker_plan["items"][0]["speech_xml"]
    assert "<ins>bright</ins>" in next_speaker_plan["items"][0]["speech_xml"]


def test_adoption_rejects_draft_based_on_superseded_xml(case):
    speaker_plan = draft(case, "speakers")
    delivery_plan = draft(case, "delivery")
    assert speaker_plan["settings"]["base_adopted_plan_id"] is None
    assert delivery_plan["settings"]["base_adopted_plan_version"] is None

    lease = claim(case, speaker_plan)
    items = source_items(lease)
    items[0]["speech_xml"] = speaker_xml(items[0])
    submitted = submit(
        case,
        speaker_plan,
        lease,
        items,
        [{"id": "c-alice", "display_name": "Alice"}],
    )
    assert submitted.status_code == 200, submitted.get_json()
    adopted = case["post"](
        f"/{speaker_plan['id']}/adopt",
        {"expected_version": submitted.get_json()["version"], "enable": False},
    )
    assert adopted.status_code == 200, adopted.get_json()

    stale = case["post"](
        f"/{delivery_plan['id']}/adopt",
        {"expected_version": delivery_plan["version"], "accept_unanalysed": True},
    )
    assert stale.status_code == 409, stale.get_json()
    assert "adopted speech annotations changed" in stale.get_json()["error"]["message"]

    successor = draft(case, "delivery")
    assert successor["settings"]["base_adopted_plan_id"] == speaker_plan["id"]
    assert successor["settings"]["base_adopted_plan_version"] == adopted.get_json()["version"]
    assert "c-alice" in successor["items"][0]["speech_xml"]


def test_pass_guards_and_proposal_rollback(case):
    plan = draft(case, "speakers")
    lease = claim(case, plan)
    original = source_items(lease)
    proposal = {"id": "c-alice", "display_name": "Alice"}
    for changed in (
        speaker_xml(original[0], instruction=True),
        speaker_xml(original[0], voice=True),
        speaker_xml(original[0], text="Different words."),
    ):
        items = source_items(lease)
        items[0]["speech_xml"] = changed
        response = submit(case, plan, lease, items, [proposal])
        assert response.status_code == 422, response.get_json()
        with case["services"]["database"].session() as session:
            assert get_generation_controls(session, case["session_id"])["characters"] == []
            assert session.get(m.PerformanceBatch, lease["batch_id"]).status == "leased"
    items = source_items(lease)
    items[0]["speech_xml"] = speaker_xml(items[0])
    bad_lease = dict(lease, lease_token="wrong-lease-token-0000")
    response = submit(case, plan, bad_lease, items, [proposal])
    assert response.status_code == 409
    with case["services"]["database"].session() as session:
        assert get_generation_controls(session, case["session_id"])["characters"] == []


def test_delivery_rejects_identity_and_combined_accepts_both(case):
    delivery = draft(case, "delivery")
    lease = claim(case, delivery)
    items = source_items(lease)
    items[0]["speech_xml"] = speaker_xml(items[0])
    assert submit(case, delivery, lease, items).status_code == 422
    assert (
        submit(
            case, delivery, lease, source_items(lease), [{"id": "c-alice", "display_name": "Alice"}]
        ).status_code
        == 422
    )

    combined = draft(case, "combined")
    combined_lease = claim(case, combined)
    items = source_items(combined_lease)
    items[0]["speech_xml"] = speaker_xml(items[0], instruction=True)
    response = submit(
        case,
        combined,
        combined_lease,
        items,
        [{"id": "c-alice", "display_name": "Alice"}],
    )
    assert response.status_code == 200, response.get_json()


def test_default_llm_model_is_frozen_before_batches_and_on_resume(case):
    response = case["post"](
        "",
        {
            "expected_plan_revision_id": case["revision_id"],
            "mode": "passive",
            "batch_size": 1,
        },
    )
    assert response.status_code == 201, response.get_json()
    plan = response.get_json()
    payload = {"session_id": case["session_id"], "performance_plan_id": plan["id"]}
    requested = []
    calls = []

    def resolve(_database, _paths, *, requested_model, **_kwargs):
        requested.append(requested_model)
        return SimpleNamespace(provider_configs=[]), "local/resolved"

    def interrupted(**kwargs):
        calls.append(kwargs["model_name"])
        if len(calls) == 2:
            raise RuntimeError("interrupted")
        return _llm_response(**kwargs)

    with (
        patch("pandrator.web.provider_settings.build_llm_settings", side_effect=resolve),
        patch("pandrator.logic.llm_handler.chat_completion_with_metadata", side_effect=interrupted),
    ):
        try:
            plans.run_analysis(
                case["services"]["workflow_handlers"],
                payload,
                lambda *_args: None,
                threading.Event(),
            )
        except RuntimeError as error:
            assert str(error) == "interrupted"
        else:
            raise AssertionError("Expected the simulated interruption")
    with case["services"]["database"].session() as session:
        saved = session.get(m.PerformancePlan, plan["id"])
        assert saved.settings_json["effective_model_name"] == "local/resolved"
    with (
        patch("pandrator.web.provider_settings.build_llm_settings", side_effect=resolve),
        patch(
            "pandrator.logic.llm_handler.chat_completion_with_metadata", side_effect=_llm_response
        ),
    ):
        plans.run_analysis(
            case["services"]["workflow_handlers"],
            payload,
            lambda *_args: None,
            threading.Event(),
        )
    assert requested == ["", "local/resolved"]
    assert calls == ["local/resolved", "local/resolved"]
