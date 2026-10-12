from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import event, select

from pandrator.web import models as m
from pandrator.web import performance_plans as plans
from pandrator.web.api import create_app
from pandrator.web.auth import BootstrapTokenStore
from pandrator.web.performance_read_projections import capability_profile_id
from pandrator_mcp.schemas.performance import (
    GetPerformancePlanInput,
    ListPerformancePlansInput,
)
from pandrator_mcp.tools.performance import performance_action


@pytest.fixture
def read_case(tmp_path):
    bootstrap = BootstrapTokenStore()
    token = bootstrap.issue()
    app = create_app(data_root=tmp_path, testing=True, bootstrap_tokens=bootstrap)
    client = app.test_client()
    headers = {
        "X-CSRF-Token": client.post(
            "/api/v1/auth/bootstrap", json={"token": token}
        ).get_json()["csrf_token"]
    }
    services = app.extensions["pandrator"]
    response = client.post(
        "/api/v1/sessions",
        json={"name": "Performance read projections", "workflow_kind": "audiobook"},
        headers=headers,
    )
    assert response.status_code == 201, response.get_json()
    session_id = response.get_json()["id"]
    generation_plan = services["generation"].create_plan(
        session_id,
        source_revision_id=None,
        settings={},
        segments=[
            {"text": "The request was rejected.", "speaker": "A"},
            {"text": "Yet this defeat would be temporary.", "speaker": "A"},
            {"text": "Membership doubled soon afterwards.", "speaker": "A"},
        ],
    )
    revision_id = generation_plan["active_revision_id"]
    with services["database"].session() as session:
        segment_ids = list(
            session.scalars(
                select(m.GenerationSegment.id)
                .where(m.GenerationSegment.plan_revision_id == revision_id)
                .order_by(m.GenerationSegment.ordinal)
            )
        )
    case = {
        "app": app,
        "client": client,
        "headers": headers,
        "services": services,
        "session_id": session_id,
        "revision_id": revision_id,
        "segment_ids": segment_ids,
        "base": f"/api/v1/sessions/{session_id}/performance-plans",
    }
    yield case
    services["database"].dispose()


def create_plan(case, **options):
    response = case["client"].post(
        case["base"],
        json={"expected_plan_revision_id": case["revision_id"], **options},
        headers={
            **case["headers"],
            "Idempotency-Key": "projection-" + uuid.uuid4().hex,
        },
    )
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def get_plan(case, plan_id, query=""):
    return case["client"].get(case["base"] + "/" + plan_id + query)


def test_api_default_full_view_preserves_legacy_payload(read_case):
    plan = create_plan(read_case, annotation_format="xml")
    default = get_plan(read_case, plan["id"]).get_json()
    explicit = get_plan(
        read_case, plan["id"], "?view=full&include_units=true"
    ).get_json()

    assert default == explicit
    assert "base_signature" in default
    assert "character_dictionary" in default["settings"]
    assert default["items"][0]["speech_xml"]
    assert default["items"][0]["speech_structure"]["transcript"] == default[
        "items"
    ][0]["spoken_text"]
    assert default["items"][0]["text"] == default["items"][0]["spoken_text"]


def test_summary_skips_markup_parsing_and_limits_settings(read_case, monkeypatch):
    plan = create_plan(
        read_case,
        annotation_format="xml",
        instructions="Keep this private planning input out of summaries.",
    )

    def unexpected_parse(*_args, **_kwargs):
        raise AssertionError("summary view parsed speech markup")

    monkeypatch.setattr(plans, "parse_speech_markup", unexpected_parse)
    response = get_plan(read_case, plan["id"], "?view=summary")

    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert "items" not in result
    assert set(result["settings"]) <= {
        "mode",
        "purpose",
        "annotation_format",
        "model_name",
        "effective_model_name",
        "workflow_kind",
        "context_before",
        "context_after",
        "context_max_chars",
        "batch_size",
        "allow_vocalizations",
    }
    assert "instructions" not in result["settings"]
    assert "general_direction" not in result["settings"]
    assert "character_dictionary" not in result["settings"]
    assert result["capabilities"]
    assert result["capability_profile_id"] == capability_profile_id(
        result["capabilities"]
    )


@pytest.mark.parametrize("view", ["summary", "annotations", "full"])
def test_include_units_false_skips_unit_projection(read_case, monkeypatch, view):
    plan = create_plan(read_case, annotation_format="xml")

    def unexpected_projection(*_args, **_kwargs):
        raise AssertionError("read without units built a public unit")

    monkeypatch.setattr(plans, "_public_unit", unexpected_projection)
    monkeypatch.setattr(plans, "annotation_unit", unexpected_projection)
    response = get_plan(
        read_case, plan["id"], f"?view={view}&include_units=false"
    )

    assert response.status_code == 200, response.get_json()
    assert "items" not in response.get_json()


def test_annotations_projection_keeps_semantics_without_duplicate_text(read_case):
    plan = create_plan(read_case, annotation_format="xml")
    first_id = read_case["segment_ids"][0]
    spoken_text = "The request was rejected."
    markup = (
        f'<segment id="{first_id}" boundary_after="dialogue_turn">'
        '<dialogue id="d"><speaker ref="speaker-a">'
        f"{spoken_text}"
        "</speaker></dialogue>"
        '<event kind="laugh" affinity="previous"/>'
        "</segment>"
    )
    with read_case["services"]["database"].immediate_session() as session:
        plan_row = session.get(m.PerformancePlan, plan["id"])
        units = [dict(unit) for unit in plan_row.units_json]
        units[0]["speech_xml"] = markup
        plan_row.units_json = units
        settings = dict(plan_row.settings_json)
        settings["character_dictionary"] = [
            {"id": "speaker-a", "display_name": "Alice", "voice_category": "female"}
        ]
        plan_row.settings_json = settings

    response = get_plan(
        read_case,
        plan["id"],
        "?view=annotations&segment_ids=" + first_id,
    )
    assert response.status_code == 200, response.get_json()
    item = response.get_json()["items"][0]

    assert item["text"] == spoken_text
    assert item["text_layer"] == "speech"
    assert "spoken_text" not in item and "transcript" not in item
    assert "speech_xml" not in item and "xml" not in item["speech_structure"]
    assert set(item["speech_structure"]) == {
        "segment_id",
        "spans",
        "events",
        "boundary_after",
    }
    assert item["speech_structure"]["boundary_after"] == "dialogue_turn"
    span = item["speech_structure"]["spans"][0]
    assert "text" not in span
    assert span["speaker_id"] == "speaker-a"
    assert span["dialogue"] is True
    assert span["voice_category"] == "female"
    assert item["speech_structure"]["events"] == [
        {
            "offset": len(spoken_text),
            "kind": "laugh",
            "duration_ms": None,
            "affinity": "previous",
        }
    ]


def test_targeted_ids_filter_before_pagination_and_reject_unknown(read_case):
    plan = create_plan(read_case, annotation_format="xml")
    ids = read_case["segment_ids"]
    response = get_plan(
        read_case,
        plan["id"],
        f"?view=annotations&segment_ids={ids[2]},{ids[0]}&limit=1",
    )
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["filtered_total"] == 2
    assert [item["id"] for item in response.get_json()["items"]] == [ids[0]]

    unknown = get_plan(
        read_case, plan["id"], "?view=annotations&segment_ids=missing-segment"
    )
    duplicate = get_plan(
        read_case,
        plan["id"],
        f"?view=annotations&segment_ids={ids[0]},{ids[0]}",
    )
    too_many = get_plan(
        read_case,
        plan["id"],
        "?view=annotations&segment_ids=" + ",".join(f"unknown-{i}" for i in range(101)),
    )
    assert unknown.status_code == duplicate.status_code == too_many.status_code == 422


def test_unit_parse_work_is_page_bounded_and_queries_are_page_constant(
    read_case, monkeypatch
):
    plan = create_plan(read_case, annotation_format="xml")
    original_parse = plans.parse_speech_markup
    parse_count = 0

    def counted_parse(*args, **kwargs):
        nonlocal parse_count
        parse_count += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(plans, "parse_speech_markup", counted_parse)
    engine = read_case["services"]["database"].engine
    statement_count = 0

    def counted_statement(*_args, **_kwargs):
        nonlocal statement_count
        statement_count += 1

    event.listen(engine, "before_cursor_execute", counted_statement)
    try:
        first = get_plan(read_case, plan["id"], "?offset=0&limit=1")
        first_statements = statement_count
        first_parses = parse_count
        statement_count = 0
        parse_count = 0
        second = get_plan(read_case, plan["id"], "?offset=1&limit=2")
        second_statements = statement_count
        second_parses = parse_count
    finally:
        event.remove(engine, "before_cursor_execute", counted_statement)

    assert first.status_code == second.status_code == 200
    assert first_parses == 1
    assert second_parses == 2
    assert first_statements == second_statements
    assert [item["id"] for item in second.get_json()["items"]] == read_case[
        "segment_ids"
    ][1:]


def test_read_projection_uses_one_batch_load_for_annotations_and_summary(
    read_case, monkeypatch
):
    plan = create_plan(read_case, annotation_format="xml")
    original_batches = plans._batches
    batch_loads = 0

    def counted_batches(*args, **kwargs):
        nonlocal batch_loads
        batch_loads += 1
        return original_batches(*args, **kwargs)

    def unexpected_annotations(*_args, **_kwargs):
        raise AssertionError("read loaded the batch annotations a second time")

    monkeypatch.setattr(plans, "_batches", counted_batches)
    monkeypatch.setattr(plans, "_annotations", unexpected_annotations)
    response = get_plan(read_case, plan["id"], "?view=annotations")

    assert response.status_code == 200, response.get_json()
    assert batch_loads == 1


def test_structural_filter_parses_each_candidate_once(read_case, monkeypatch):
    plan = create_plan(read_case, annotation_format="xml")
    first_id = read_case["segment_ids"][0]
    with read_case["services"]["database"].immediate_session() as session:
        row = session.get(m.PerformancePlan, plan["id"])
        units = [dict(unit) for unit in row.units_json]
        units[0]["speech_xml"] = (
            f'<segment id="{first_id}"><dialogue id="d">'
            f'{units[0]["spoken_text"]}'
            "</dialogue></segment>"
        )
        row.units_json = units
    original_parse = plans.parse_speech_markup
    parse_count = 0

    def counted_parse(*args, **kwargs):
        nonlocal parse_count
        parse_count += 1
        return original_parse(*args, **kwargs)

    monkeypatch.setattr(plans, "parse_speech_markup", counted_parse)
    response = get_plan(read_case, plan["id"], "?filter=dialogue&limit=1")
    assert response.status_code == 200, response.get_json()
    assert parse_count == len(read_case["segment_ids"])
    assert len(response.get_json()["items"]) == 1
    assert response.get_json()["items"][0]["id"] == first_id


def test_list_compacts_frozen_capabilities_internally(read_case):
    first = create_plan(read_case)
    second = create_plan(read_case)
    frozen = [{"delivery": ["emotion"]}, {"delivery": ["emotion", "pace"]}]
    with read_case["services"]["database"].immediate_session() as session:
        for plan_id, capabilities in zip((first["id"], second["id"]), frozen, strict=True):
            row = session.get(m.PerformancePlan, plan_id)
            settings = dict(row.settings_json)
            settings["capabilities"] = capabilities
            settings["instructions"] = "private instructions"
            settings["character_dictionary"] = [{"id": "private"}]
            row.settings_json = settings

    response = read_case["client"].get(read_case["base"] + "?view=summary")
    assert response.status_code == 200, response.get_json()
    result = response.get_json()
    assert "capabilities" not in result
    assert len(result["capability_profiles"]) == 2
    items = {item["id"]: item for item in result["items"]}
    assert items[first["id"]]["capability_profile_id"] == capability_profile_id(frozen[0])
    assert items[second["id"]]["capability_profile_id"] == capability_profile_id(frozen[1])
    assert result["capability_profiles"] == {
        capability_profile_id(item): item for item in frozen
    }
    assert all("capabilities" not in item for item in result["items"])
    assert all("instructions" not in item["settings"] for item in result["items"])
    assert all("character_dictionary" not in item["settings"] for item in result["items"])


def test_mcp_read_defaults_forward_compact_projection_fields():
    application = Mock()
    application.performance_plan_request.return_value = {"id": "plan", "items": []}
    runtime = SimpleNamespace(require_application=lambda: application)

    listed = ListPerformancePlansInput(session_id="session")
    performance_action(runtime, "list", listed)
    assert application.performance_plan_request.call_args.args[1]["view"] == "summary"
    assert application.performance_plan_request.call_args.args[1]["include_units"] is False

    application.performance_plan_request.reset_mock()
    requested = GetPerformancePlanInput(
        session_id="session", plan_id="plan", segment_ids=["segment-a", "segment-b"]
    )
    result = performance_action(runtime, "get", requested).result
    forwarded = application.performance_plan_request.call_args.args[1]
    assert forwarded["view"] == "annotations"
    assert forwarded["include_units"] is False
    assert forwarded["segment_ids"] == ["segment-a", "segment-b"]
    assert result == {"schema_version": "1", "id": "plan"}

    application.performance_plan_request.reset_mock()
    application.performance_plan_request.return_value = {
        "id": "plan",
        "items": [{"id": "segment-a"}],
    }
    requested = GetPerformancePlanInput(
        session_id="session",
        plan_id="plan",
        include_units=True,
        view="full",
    )
    result = performance_action(runtime, "get", requested).result
    forwarded = application.performance_plan_request.call_args.args[1]
    assert forwarded["include_units"] is True
    assert forwarded["view"] == "full"
    assert result["items"] == [{"id": "segment-a"}]


def test_edit_next_action_reads_only_affected_annotation_units():
    application = Mock()
    application.performance_plan_request.return_value = {"id": "plan", "version": 2}
    runtime = SimpleNamespace(require_application=lambda: application)
    from pandrator_mcp.schemas.performance import EditPerformancePlanInput

    arguments = EditPerformancePlanInput(
        session_id="session",
        plan_id="plan",
        idempotency_key="performance-edit",
        expected_version=1,
        items=[
            {"segment_id": "segment-a", "annotation": {"decision": "none"}},
            {"segment_id": "segment-b", "annotation": {"decision": "steer"}},
        ],
    )
    result = performance_action(runtime, "edit", arguments)
    followup = result.next_actions[0].arguments
    assert followup == {
        "session_id": "session",
        "plan_id": "plan",
        "view": "annotations",
        "include_units": True,
        "offset": 0,
        "limit": 2,
        "segment_ids": ["segment-a", "segment-b"],
    }


def test_preview_metadata_uses_cached_catalogue_without_refresh(read_case, monkeypatch):
    plan = create_plan(read_case)
    catalogue = read_case["services"]["tts_catalogue"]
    snapshot = Mock(return_value=({"services": []}, None))
    monkeypatch.setattr(catalogue, "snapshot", snapshot)

    def compiled_preview(*_args, **_kwargs):
        return {
            "capabilities": {"model": "model-a", "backend": "provider-a"},
            "speech_structure": {"boundary_after": None},
        }

    monkeypatch.setattr(plans, "preview_segment", compiled_preview)
    response = read_case["client"].post(
        f"{read_case['base']}/{plan['id']}/preview",
        json={"segment_id": read_case["segment_ids"][0]},
        headers=read_case["headers"],
    )

    assert response.status_code == 200, response.get_json()
    snapshot.assert_called_once_with(refresh=False)
