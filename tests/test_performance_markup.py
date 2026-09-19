"""XML speech annotation compatibility and lifecycle contracts."""

import pytest
from pydantic import ValidationError

from tests.test_performance_plans import case as performance_case
from pandrator.logic.speech_markup import plain_speech_markup
from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web.speech_annotation_records import (
    normalized_record,
    record_annotation,
    record_markup,
)
from pandrator_mcp.schemas.performance import (
    GetPerformancePlanInput,
    PreviewPerformancePlanInput,
)
from pandrator.web.performance_schemas import (
    PerformanceItem,
    PerformancePreviewRequest,
)

case = performance_case


def _xml(segment_id: str, text: str, *, speaker: str | None = None) -> str:
    if speaker:
        return f'<segment id="{segment_id}"><dialogue><speaker ref="{speaker}">{text}</speaker></dialogue></segment>'
    return plain_speech_markup(segment_id, text)


def test_normalized_records_keep_xml_private_and_public_pure():
    record = normalized_record(
        "Hello.",
        "segment",
        {
            "speech_xml": _xml("segment", "Hello."),
            "locked": True,
            "reason": "Reviewed source structure.",
        },
        [],
    )
    assert record_markup(record).startswith('<segment id="segment">')
    assert "_speech_xml" in record
    public = record_annotation(record)
    assert "_speech_xml" not in public
    assert public["locked"] is True
    with pytest.raises(ValueError, match="exactly one"):
        normalized_record(
            "Hello.",
            "segment",
            {
                "annotation": {"decision": "none"},
                "speech_xml": _xml("segment", "Hello."),
            },
            [],
        )


@pytest.mark.parametrize(
    "model,values",
    [
        (PerformanceItem, {"segment_id": "s", "annotation": {"decision": "none"}}),
        (
            PerformancePreviewRequest,
            {"segment_id": "s"},
        ),
        (
            PreviewPerformancePlanInput,
            {"session_id": "s", "plan_id": "p", "segment_id": "s"},
        ),
    ],
)
def test_xml_schema_is_backward_compatible_and_xor_validated(model, values):
    assert model.model_validate(values)
    if model is not PerformancePreviewRequest:
        with pytest.raises(ValidationError):
            model.model_validate(
                {
                    **values,
                    "annotation": {"decision": "none"},
                    "speech_xml": _xml("s", "Hello."),
                }
            )


def test_get_performance_plan_filter_is_bounded():
    value = GetPerformancePlanInput(session_id="s", plan_id="p", filter="dialogue")
    assert value.filter == "dialogue"
    with pytest.raises(ValidationError):
        GetPerformancePlanInput(session_id="s", plan_id="p", filter="unknown")


@pytest.mark.parametrize(
    "model,values",
    [
        (PerformancePreviewRequest, {"segment_id": "s"}),
        (
            PreviewPerformancePlanInput,
            {"session_id": "s", "plan_id": "p", "segment_id": "s"},
        ),
    ],
)
def test_preview_accepts_optional_casting_override(model, values):
    assert (
        model.model_validate({**values, "casting_enabled": True}).casting_enabled
        is True
    )


def test_xml_plan_seeds_dictionary_structure_and_preserves_analysis(case):
    controls_path = f"/api/v1/sessions/{case['session_id']}/generation-controls"
    saved = case["client"].put(
        controls_path,
        json={
            "expected_revision": 0,
            "characters": [
                {"id": "c1", "display_name": "Alice", "voice_category": "female"},
                {"id": "c2", "display_name": "Bob", "voice_category": "male"},
            ],
        },
        headers={**case["headers"], "Idempotency-Key": "markup-controls-1"},
    )
    assert saved.status_code == 200, saved.get_json()

    with case["services"]["database"].session() as session:
        first = session.get(m.GenerationSegment, case["segment_ids"][0])
        first.speech_plan_json = {
            "speech_xml": _xml(first.id, first.text, speaker="c1")
        }

    plan_response = case["post"](
        "",
        {
            "expected_plan_revision_id": case["revision_id"],
            "annotation_format": "xml",
            "mode": "passive",
        },
    )
    assert plan_response.status_code == 201, plan_response.get_json()
    plan = plan_response.get_json()
    assert plan["settings"]["annotation_format"] == "xml"
    assert plan["settings"]["character_dictionary_revision"] == 1
    first_item = plan["items"][0]
    assert first_item["speech_xml"]
    assert first_item["speech_structure"]["spans"][0]["speaker_id"] == "c1"
    assert "_speech_xml" not in (first_item["annotation"] or {})

    batch = case["post"]("/" + plan["id"] + "/claim").get_json()
    assert "speech_xml" in batch["batch"]["items"][0]
    assert "Preserve each current XML" in batch["batch"]["instructions"]
    source_items = [
        {"segment_id": item["segment_id"], "speech_xml": item["speech_xml"]}
        for item in batch["batch"]["items"]
    ]
    submitted = case["post"](
        f"/{plan['id']}/batches/{batch['batch_id']}/submit",
        {"lease_token": batch["lease_token"], "items": source_items},
    )
    assert submitted.status_code == 200, submitted.get_json()

    current = case["client"].get(f"{case['base']}/{plan['id']}").get_json()
    assert current["analysed_count"] == 3
    assert current["items"][0]["speech_structure"]["spans"][0]["speaker_id"] == "c1"

    changed = source_items[0]["speech_xml"].replace(
        '<speaker ref="c1">', '<speaker ref="c2">'
    )
    bad = case["post"](
        f"/{plan['id']}/batches/{batch['batch_id']}/submit",
        {
            "lease_token": batch["lease_token"],
            "items": [
                {"segment_id": source_items[0]["segment_id"], "speech_xml": changed}
            ],
        },
    )
    assert bad.status_code in {409, 422}


def test_xml_snapshot_readback_and_filters(case):
    plan_response = case["post"](
        "",
        {
            "expected_plan_revision_id": case["revision_id"],
            "annotation_format": "xml",
            "mode": "manual",
        },
    )
    assert plan_response.status_code == 201
    plan = plan_response.get_json()
    dialogue = (
        case["client"].get(f"{case['base']}/{plan['id']}?filter=dialogue").get_json()
    )
    assert dialogue["filtered_total"] == 0

    adopted = case["post"](
        f"/{plan['id']}/adopt",
        {"expected_version": plan["version"], "accept_unanalysed": True},
    )
    assert adopted.status_code == 200
    with case["services"]["database"].session() as session:
        snapshot = {}
        plans.freeze_performance_snapshot(session, case["revision_id"], snapshot)
        text = plan["items"][0]["spoken_text"]
        assert plans.speech_markup_for_segment(
            snapshot, plan["items"][0]["id"], text
        ).startswith("<segment")


def test_preview_accepts_new_dictionary_character_without_saving_draft(case):
    plan = case["post"](
        "",
        {
            "expected_plan_revision_id": case["revision_id"],
            "annotation_format": "xml",
            "mode": "manual",
        },
    ).get_json()
    controls = case["client"].put(
        f"/api/v1/sessions/{case['session_id']}/generation-controls",
        json={
            "expected_revision": 0,
            "characters": [{"id": "new-character", "display_name": "Alice"}],
            "cast": {"characters": {"new-character": {"voice": "Puck"}}},
        },
        headers={**case["headers"], "Idempotency-Key": "preview-new-character"},
    )
    assert controls.status_code == 200
    unit = plan["items"][0]
    with case["services"]["database"].session() as session:
        draft = plans.get_plan(session, case["session_id"], plan["id"])
        preview = plans.preview_segment(
            session,
            draft,
            unit["id"],
            {
                "service": "gemini",
                "model": "gemini-2.5-flash-tts",
                "voice": "Kore",
                "casting_enabled": True,
            },
            speech_xml=_xml(unit["id"], unit["spoken_text"], speaker="new-character"),
        )
        assert preview["parts"][0]["voice"] == "Puck"
        assert draft.version == plan["version"]
        assert draft.settings_json["character_dictionary"] == []
