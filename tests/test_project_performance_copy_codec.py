"""In-memory current performance intent, strict closure and independent remapping."""

from __future__ import annotations

from collections import Counter
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash, validate_annotation
from pandrator.web.generation_cast_runtime import remap_markup
from pandrator.web.generation_control_schemas import VoiceBinding
from pandrator.web.project_speech_references import speech_reference_selector_hash
from pandrator.web.projects import copy_performance as codec
from pandrator.web.projects.copy_performance import (
    MAX_PERFORMANCE_PLANS,
    MAX_PERFORMANCE_UNITS,
    PERFORMANCE_FAMILIES,
    PLAN_FIELDS,
    freeze_performance_plan,
    freeze_performance_plan_rows,
    remap_performance_plan_rows,
    validate_performance_copy_graph,
)
from pandrator.web.projects.copy_speech import remap_segment_reference, segment_copy_references
from pandrator.web.projects.copy_speech_plan import speech_plan_signature
from pandrator.web.speech_annotation_records import normalized_record


class Discarded:
    def __deepcopy__(self, _memo):
        raise AssertionError("Discarded source values must not be traversed")

    def __iter__(self):
        raise AssertionError("Discarded source values must not be traversed")


def graph() -> dict[str, Any]:
    segments = []
    for ordinal, text in enumerate(("First.", "Second.", "Removed.", "   ")):
        segments.append(
            {
                "id": f"segment-{ordinal}",
                "plan_revision_id": "revision",
                "ordinal": ordinal,
                "text": text,
                "optimized_text": text if ordinal == 0 else None,
                "node_kind": "paragraph",
                "speaker": "source-session",
                "language": "en",
                "voice": None,
                "voice_id": None,
                "silence_after_ms": 0,
                "removed": ordinal == 2,
                "source_segment_ids_json": [],
                "speech_block_provenance_json": {
                    "section_id": "source-session",
                    "start_ms": ordinal * 1000,
                },
                "speech_plan_json": {},
                "paragraph_break_after": True,
            }
        )
    return {
        "sessions": [
            {"id": "source-session", "project_id": "project", "workflow_kind": "audiobook"}
        ],
        "projects": [{"id": "project", "workspace_id": "workspace"}],
        "generation_plans": [
            {"id": "speech-plan", "session_id": "source-session", "active_revision_id": "revision"}
        ],
        "generation_plan_revisions": [{"id": "revision", "plan_id": "speech-plan"}],
        "generation_segments": segments,
        "project_speech_references": [],
    }


def raw_plan(rows, *, identifier="performance", status="draft", purpose="delivery", xml=False):
    units = []
    for segment in rows["generation_segments"][:2]:
        spoken = segment["optimized_text"] or segment["text"]
        unit = {
            "id": segment["id"],
            "text": segment["text"],
            "speaker": segment["speaker"],
            "language": segment["language"],
            "node_kind": segment["node_kind"],
            "section_id": "source-session",
            "ordinal": segment["ordinal"],
            "spoken_text": spoken,
            "text_hash": content_hash(spoken),
            "voice": "old-library-label",
            "paragraph_break_after": True,
            "timing": {"start_ms": segment["ordinal"] * 1000},
        }
        if xml:
            unit["speech_xml"] = f'<segment id="{segment["id"]}">{spoken}</segment>'
        units.append(unit)
    return {
        "id": identifier,
        "session_id": "source-session",
        "plan_revision_id": "revision",
        "status": status,
        "settings_json": {"purpose": purpose, "annotation_format": "xml" if xml else "pssml"},
        "units_json": units,
        "base_signature": "parent-qualified-raw-signature",
        "manual_annotations_json": {"discarded": "parent merges annotations"},
        "job_id": "source-analysis-job",
        "version": 9,
    }


def freeze(rows, raw=None, annotations=None, baseline=None):
    return freeze_performance_plan(
        raw or raw_plan(rows),
        rows=rows,
        annotations=annotations or {},
        base_adopted_plan_id=baseline,
    )


def frozen_graph():
    rows = graph()
    rows["performance_plans"] = [
        freeze(rows, annotations={"segment-0": {"reason": "Reviewed manually"}})
    ]
    return rows


def test_partial_manual_draft_is_detached_and_drops_analysis_without_traversal():
    rows = graph()
    original = deepcopy(rows)
    raw = raw_plan(rows)
    unknown = Discarded()
    raw["settings_json"].update(
        provider_configs=unknown,
        effective_model_name=unknown,
        model_name=unknown,
        instructions=unknown,
        copy_from_id=unknown,
    )
    raw.update(job_id=unknown, created_at=unknown, response_hash=unknown, batches=unknown)
    raw["units_json"][0].update(speaker=unknown, timing=unknown, voice=unknown)
    annotations = {"segment-0": {"reason": "Manual override", "confidence": "high", "locked": True}}
    before = deepcopy(annotations)
    result = freeze(rows, raw, annotations)
    assert set(result) == PLAN_FIELDS
    assert result["status"] == "draft" and result["version"] == 1 and result["job_id"] is None
    assert set(result["manual_annotations_json"]) == {"segment-0"}
    assert result["manual_annotations_json"]["segment-0"]["reason"] == "Manual override"
    assert result["manual_annotations_json"]["segment-0"]["confidence"] == "high"
    settings = result["settings_json"]
    assert (
        settings["mode"],
        settings["model_name"],
        settings["instructions"],
        settings["copy_from_id"],
    ) == ("manual", "", "", None)
    assert settings["capabilities"] == {} and settings["general_direction"] == ""
    assert settings["character_dictionary"] == [] and settings["character_dictionary_revision"] == 1
    assert result["units_json"][0]["speaker"] == "source-session"
    assert result["units_json"][0]["voice"] == ""
    assert result["units_json"][0]["timing"] == {"start_ms": 0}
    assert "provider_configs" not in settings and "planner_version" not in settings
    assert rows == original and annotations == before
    assert raw["job_id"] is unknown and raw["units_json"][0]["speaker"] is unknown
    result["manual_annotations_json"]["segment-0"]["reason"] = "Destination edit"
    assert annotations == before


def test_complete_adopted_and_multiple_purpose_drafts_keep_exact_baselines():
    rows = graph()
    adopted = freeze(
        rows,
        raw_plan(rows, identifier="adopted", status="adopted"),
        {"segment-0": {}, "segment-1": {}},
    )
    drafts = [
        freeze(
            rows, raw_plan(rows, identifier=purpose, purpose=purpose, xml=True), baseline="adopted"
        )
        for purpose in ("speakers", "combined")
    ]
    rows["performance_plans"] = [adopted, *drafts]
    validate_performance_copy_graph(rows, allow_performance=True)
    assert [row["status"] for row in rows["performance_plans"]] == ["adopted", "draft", "draft"]
    assert all(row["settings_json"]["base_adopted_plan_id"] == "adopted" for row in drafts)
    assert all(row["settings_json"]["base_adopted_plan_version"] == 1 for row in drafts)
    assert all(row["manual_annotations_json"] == {} for row in drafts)
    assert adopted["settings_json"]["base_adopted_plan_id"] is None


def test_completed_then_manual_annotation_map_is_the_only_copied_annotation_input():
    rows = graph()
    completed = {
        "segment-0": {"reason": "Completed batch"},
        "segment-1": {"reason": "Completed second"},
    }
    manual = {"segment-0": {"reason": "Manual wins", "locked": True}}
    raw = raw_plan(rows)
    raw["manual_annotations_json"] = {"foreign": Discarded()}
    result = freeze(rows, raw, {**completed, **manual})
    assert result["manual_annotations_json"]["segment-0"]["reason"] == "Manual wins"
    assert result["manual_annotations_json"]["segment-1"]["reason"] == "Completed second"


@pytest.mark.parametrize(
    "purpose,format_xml", [("delivery", False), ("delivery", True), ("speakers", True)]
)
def test_xml_records_preserve_speaker_structure_controls_and_confidence(purpose, format_xml):
    rows = graph()
    raw = raw_plan(rows, purpose=purpose, xml=format_xml)
    characters = [{"id": "source-session", "display_name": "Narrator"}]
    raw["settings_json"]["character_dictionary"] = characters
    xml = '<segment id="segment-0"><ins>softly</ins><dialogue><speaker ref="source-session">First.</speaker></dialogue></segment>'
    record = normalized_record(
        "First.", "segment-0", {"speech_xml": xml, "locked": True, "reason": "Reviewed"}, characters
    )
    record["confidence"] = "high"
    frozen = freeze(rows, raw, {"segment-0": record})
    rows["performance_plans"] = [frozen]
    validate_performance_copy_graph(rows, allow_performance=True)
    copied = frozen["manual_annotations_json"]["segment-0"]
    assert (
        copied["confidence"] == "high"
        and copied["locked"] is True
        and copied["reason"] == "Reviewed"
    )
    assert '<speaker ref="source-session">' in copied["_speech_xml"]
    assert frozen["units_json"][0]["speech_xml"] == copied["_speech_xml"]
    parse_speech_markup(
        copied["_speech_xml"],
        expected_segment_id="segment-0",
        expected_text="First.",
        characters=frozen["settings_json"]["character_dictionary"],
    )


def test_xml_legacy_annotation_does_not_hide_controls_and_unreviewed_xml_remains():
    rows = graph()
    raw = raw_plan(rows, xml=True)
    raw["units_json"][0].pop("speech_xml")
    raw["units_json"][0]["legacy_annotation"] = True
    choice = {"decision": "steer", "delivery": {"pace": "slower"}, "reason": "Legacy choice"}
    result = freeze(rows, raw, {"segment-0": choice})
    assert result["units_json"][0]["legacy_annotation"] is True
    assert "speech_xml" not in result["units_json"][0]
    assert result["manual_annotations_json"]["segment-0"] == validate_annotation("First.", choice)
    assert "segment-1" not in result["manual_annotations_json"]
    assert result["units_json"][1]["speech_xml"] == '<segment id="segment-1">Second.</segment>'
    validate_performance_copy_graph({**rows, "performance_plans": [result]}, allow_performance=True)


def add_reference(rows):
    selector = VoiceBinding(voice="Saved narrator", service="audio_cpp", model="model").model_dump(
        mode="json"
    )
    rows["project_speech_references"] = [
        {
            "session_id": "source-session",
            "slot_key": "segment:" + rows["generation_segments"][0]["id"],
            "selector_json": selector,
            "selector_hash": speech_reference_selector_hash(selector),
            "binding_json": {
                "adapter": "audio_cpp",
                "model": "model",
                "voice": "Saved narrator",
                "display_name": "Saved narrator",
            },
            "input_artifact_id": None,
            "input_content_hash": None,
            "revision": 1,
        }
    ]


def test_remapping_rebinds_xml_ids_and_saved_signature_but_retains_semantic_labels():
    rows = graph()
    segment_id = "00000000-0000-0000-0000-000000000001"
    target_segment_id = "00000000-0000-0000-0000-000000000101"
    rows["generation_segments"][0]["id"] = segment_id
    add_reference(rows)
    rows["generation_segments"][0]["speech_plan_json"] = {
        "speech_xml": f'<segment id="{segment_id}">First.</segment>'
    }
    raw = raw_plan(rows, xml=True)
    raw["settings_json"]["character_dictionary"] = [
        {"id": "source-session", "display_name": "Narrator"}
    ]
    record = normalized_record(
        "First.",
        segment_id,
        {
            "speech_xml": f'<segment id="{segment_id}"><speaker ref="source-session">First.</speaker></segment>'
        },
        raw["settings_json"]["character_dictionary"],
    )
    adopted = freeze(
        rows,
        raw_plan(rows, identifier="adopted", status="adopted"),
        {segment_id: {}, "segment-1": {}},
    )
    rows["performance_plans"] = [
        freeze(rows, raw, {segment_id: record}, baseline="adopted"),
        adopted,
    ]
    before = deepcopy(rows)
    identities = {
        row["id"]: f"new-{row['id']}"
        for family in (
            "sessions",
            "projects",
            "generation_plans",
            "generation_plan_revisions",
            "generation_segments",
            "performance_plans",
        )
        for row in rows[family]
    }
    identities[segment_id] = target_segment_id
    mapped = deepcopy(rows)
    mapped.pop("performance_plans")
    for family in (
        "sessions",
        "projects",
        "generation_plans",
        "generation_plan_revisions",
        "generation_segments",
    ):
        for row in mapped[family]:
            for key in (
                "id",
                "session_id",
                "project_id",
                "active_revision_id",
                "plan_id",
                "plan_revision_id",
            ):
                if key in row:
                    row[key] = identities[row[key]]
    mapped["project_speech_references"] = [
        remap_segment_reference(row, identities, allow_null_inputs=True)
        for row in rows["project_speech_references"]
    ]
    mapped["generation_segments"][0]["speech_plan_json"]["speech_xml"] = remap_markup(
        f'<segment id="{segment_id}">First.</segment>', target_segment_id, "First.", []
    )
    mapped_before = deepcopy(mapped)
    result = remap_performance_plan_rows(rows, identities, mapped_rows=mapped)
    destination = result["performance_plans"][0]
    expected = speech_plan_signature(
        mapped["generation_segments"], references=segment_copy_references(mapped)
    )
    assert set(result) == PERFORMANCE_FAMILIES
    assert (
        destination["id"] == "new-performance" and destination["session_id"] == "new-source-session"
    )
    assert destination["plan_revision_id"] == "new-revision"
    assert destination["settings_json"]["expected_plan_revision_id"] == "new-revision"
    assert destination["settings_json"]["base_adopted_plan_id"] == "new-adopted"
    assert destination["settings_json"]["base_adopted_plan_version"] == 1
    assert result["performance_plans"][1]["status"] == "adopted"
    assert (
        destination["base_signature"] == expected
        and expected != rows["performance_plans"][0]["base_signature"]
    )
    assert set(destination["manual_annotations_json"]) == {target_segment_id}
    assert (
        f'id="{target_segment_id}"'
        in destination["manual_annotations_json"][target_segment_id]["_speech_xml"]
    )
    assert 'ref="source-session"' in destination["units_json"][0]["speech_xml"]
    assert destination["units_json"][0]["section_id"] == "source-session"
    assert destination["settings_json"]["character_dictionary"][0]["id"] == "source-session"
    assert rows == before and mapped == mapped_before


def test_empty_family_skips_unrelated_graph_and_old_formats_reject_nonempty():
    for rows in ({}, {"performance_plans": [], "sessions": Discarded()}):
        validate_performance_copy_graph(rows)
        untouched: Any = Discarded()
        assert remap_performance_plan_rows(rows, untouched, mapped_rows=untouched) == {
            "performance_plans": []
        }
    with pytest.raises(ValueError):
        validate_performance_copy_graph(frozen_graph())


@pytest.mark.parametrize(
    "defect",
    [
        "extra_row",
        "extra_settings",
        "extra_unit",
        "extra_annotation",
        "extra_timing",
        "stale_signature",
        "foreign_owner",
        "foreign_revision",
        "foreign_unit",
        "duplicate_plan",
        "duplicate_segment",
        "duplicate_revision",
        "bool_version",
        "job",
        "version",
        "settings_mode",
        "settings_model",
        "settings_context_bool",
        "settings_context_limit",
        "dictionary_duplicate",
        "dictionary_unknown",
        "dictionary_revision_bool",
        "incomplete_adopted",
        "unknown_status",
        "base_missing",
        "base_foreign",
        "base_version",
        "bool_ordinal",
        "bool_paragraph",
        "bool_timing",
        "nan_timing",
        "unit_order",
        "missing_unit",
        "text",
        "spoken_text",
        "text_hash",
        "annotation_foreign",
        "annotation_anchor",
        "annotation_nonfinite",
        "annotation_locked",
        "annotation_confidence",
        "legacy_without_choice",
        "xml_foreign_id",
        "xml_changed_text",
        "xml_dtd",
        "xml_conflict",
        "xml_unknown_character",
    ],
)
def test_frozen_graph_rejects_malformed_finite_contracts(defect):
    rows = frozen_graph()
    row = rows["performance_plans"][0]
    settings, unit, annotations = (
        row["settings_json"],
        row["units_json"][0],
        row["manual_annotations_json"],
    )
    if defect == "extra_row":
        row["response_hash"] = "discard me"
    elif defect == "extra_settings":
        settings["provider_configs"] = {}
    elif defect == "extra_unit":
        unit["editorial_tree"] = {}
    elif defect == "extra_annotation":
        annotations["segment-0"]["unknown"] = 1
    elif defect == "extra_timing":
        unit["timing"]["provider"] = "x"
    elif defect == "stale_signature":
        row["base_signature"] = "stale"
    elif defect == "foreign_owner":
        row["session_id"] = "missing"
    elif defect == "foreign_revision":
        row["plan_revision_id"] = "missing"
    elif defect == "foreign_unit":
        unit["id"] = "missing"
    elif defect == "duplicate_plan":
        rows["performance_plans"].append(deepcopy(row))
    elif defect == "duplicate_segment":
        rows["generation_segments"].append(deepcopy(rows["generation_segments"][0]))
    elif defect == "duplicate_revision":
        rows["generation_plan_revisions"].append(deepcopy(rows["generation_plan_revisions"][0]))
    elif defect == "bool_version":
        row["version"] = True
    elif defect == "job":
        row["job_id"] = "job"
    elif defect == "version":
        row["version"] = 2
    elif defect == "settings_mode":
        settings["mode"] = "passive"
    elif defect == "settings_model":
        settings["model_name"] = "provider-model"
    elif defect == "settings_context_bool":
        settings["context_before"] = True
    elif defect == "settings_context_limit":
        settings["batch_size"] = 33
    elif defect == "dictionary_duplicate":
        settings["character_dictionary"] = [{"id": "same", "display_name": "A"}] * 2
    elif defect == "dictionary_unknown":
        settings["character_dictionary"] = [{"id": "same", "display_name": "A", "provider": "x"}]
    elif defect == "dictionary_revision_bool":
        settings["character_dictionary_revision"] = True
    elif defect == "incomplete_adopted":
        row["status"] = "adopted"
    elif defect == "unknown_status":
        row["status"] = "superseded"
    elif defect == "base_missing":
        settings["base_adopted_plan_version"] = 1
    elif defect == "base_foreign":
        settings.update(base_adopted_plan_id="foreign", base_adopted_plan_version=1)
    elif defect == "base_version":
        settings["base_adopted_plan_version"] = True
    elif defect == "bool_ordinal":
        row["units_json"][1]["ordinal"] = True
    elif defect == "bool_paragraph":
        unit["paragraph_break_after"] = 1
    elif defect == "bool_timing":
        unit["timing"]["start_ms"] = False
    elif defect == "nan_timing":
        unit["timing"]["start_ms"] = float("nan")
    elif defect == "unit_order":
        row["units_json"].reverse()
    elif defect == "missing_unit":
        row["units_json"].pop()
    elif defect == "text":
        unit["text"] = "Changed."
    elif defect == "spoken_text":
        unit["spoken_text"] = "Changed."
    elif defect == "text_hash":
        unit["text_hash"] = "wrong"
    elif defect == "annotation_foreign":
        annotations["foreign"] = {}
    elif defect == "annotation_anchor":
        annotations["segment-0"] = {
            "decision": "steer",
            "spans": [{"anchor": {"quote": "absent"}, "delivery": {"pace": "slower"}}],
        }
    elif defect == "annotation_nonfinite":
        annotations["segment-0"] = {
            "decision": "steer",
            "delivery": {"emotion": "happy", "emotion_intensity": float("inf")},
        }
    elif defect == "annotation_locked":
        annotations["segment-0"]["locked"] = 1
    elif defect == "annotation_confidence":
        annotations["segment-0"]["confidence"] = "certain"
    elif defect == "legacy_without_choice":
        row["units_json"][1]["legacy_annotation"] = True
    else:
        xml = '<segment id="segment-0">First.</segment>'
        if defect == "xml_foreign_id":
            xml = '<segment id="foreign">First.</segment>'
        elif defect == "xml_changed_text":
            xml = '<segment id="segment-0">Changed.</segment>'
        elif defect == "xml_dtd":
            xml = '<!DOCTYPE segment [<!ENTITY x "First.">]><segment id="segment-0">&x;</segment>'
        elif defect == "xml_conflict":
            xml = '<segment id="segment-0"><pace>brisk</pace>First.</segment>'
        elif defect == "xml_unknown_character":
            xml = '<segment id="segment-0"><speaker ref="missing">First.</speaker></segment>'
        annotations["segment-0"]["_speech_xml"] = xml
    with pytest.raises(ValueError):
        validate_performance_copy_graph(rows, allow_performance=True)


@pytest.mark.parametrize("defect", ["missing", "invalid", "collision"])
def test_remapping_rejects_unavailable_or_colliding_identity(defect):
    rows = frozen_graph()
    identities: dict[str, Any] = {
        value: f"new-{value}"
        for value in ("performance", "source-session", "revision", "segment-0", "segment-1")
    }
    if defect == "missing":
        identities.pop("segment-0")
    elif defect == "invalid":
        identities["segment-0"] = True
    else:
        identities["segment-1"] = identities["segment-0"]
    with pytest.raises(ValueError):
        remap_performance_plan_rows(rows, identities, mapped_rows=graph())


def test_explicit_plan_and_unit_budgets_fail_before_projection():
    assert MAX_PERFORMANCE_PLANS == 1000 and MAX_PERFORMANCE_UNITS == 250_000
    with pytest.raises(ValueError):
        validate_performance_copy_graph(
            {"performance_plans": [{}] * (MAX_PERFORMANCE_PLANS + 1)}, allow_performance=True
        )
    rows = graph()
    raw = raw_plan(rows)
    raw["units_json"] = [{}] * (MAX_PERFORMANCE_UNITS + 1)
    with pytest.raises(ValueError):
        freeze(rows, raw)
    with pytest.raises(ValueError):
        validate_performance_copy_graph(
            {"performance_plans": [{"units_json": [{}] * (MAX_PERFORMANCE_UNITS // 2 + 1)}] * 2},
            allow_performance=True,
        )


def test_two_complete_adopted_plans_for_one_revision_are_rejected():
    rows = graph()
    rows["performance_plans"] = [
        freeze(
            rows,
            raw_plan(rows, identifier=identifier, status="adopted"),
            {"segment-0": {}, "segment-1": {}},
        )
        for identifier in ("adopted-one", "adopted-two")
    ]
    with pytest.raises(ValueError):
        validate_performance_copy_graph(rows, allow_performance=True)


def _bulk_inputs():
    rows, second = graph(), graph()
    families = (
        "sessions",
        "projects",
        "generation_plans",
        "generation_plan_revisions",
        "generation_segments",
    )
    identities = {row["id"]: f"second-{row['id']}" for name in families for row in second[name]}
    for name in families:
        for row in second[name]:
            for key in (
                "id",
                "project_id",
                "session_id",
                "active_revision_id",
                "plan_id",
                "plan_revision_id",
            ):
                if key in row:
                    row[key] = identities[row[key]]
        rows[name].extend(second[name])
    values = [raw_plan(rows, identifier=f"draft-{index}") for index in range(3)]
    for index in range(2):
        raw = raw_plan(second, identifier=f"second-draft-{index}")
        raw.update(session_id="second-source-session", plan_revision_id="second-revision")
        values.append(raw)
    annotations = {row["id"]: {row["units_json"][0]["id"]: {"reason": row["id"]}} for row in values}
    baselines = {row["id"]: None for row in values}
    return rows, values, annotations, baselines


def test_bulk_matches_single_outputs_without_sharing_input_or_plan_ownership(monkeypatch):
    rows, values, annotations, baselines = _bulk_inputs()
    before = deepcopy((rows, values, annotations, baselines))
    expected = [
        freeze_performance_plan(
            row,
            rows=rows,
            annotations=annotations[row["id"]],
            base_adopted_plan_id=baselines[row["id"]],
        )
        for row in values
    ]
    references = Mock(wraps=codec.segment_copy_references)
    signature = Mock(wraps=codec.speech_plan_signature)
    ownership = Mock(wraps=codec._graph)
    monkeypatch.setattr(codec, "segment_copy_references", references)
    monkeypatch.setattr(codec, "speech_plan_signature", signature)
    monkeypatch.setattr(codec, "_graph", ownership)
    result = freeze_performance_plan_rows(
        values, rows=rows, annotations=annotations, base_adopted_plan_ids=baselines
    )
    assert result == expected
    assert ownership.call_count == 1 and references.call_count == 1
    assert Counter(call.args[0][0]["plan_revision_id"] for call in signature.call_args_list) == {
        "revision": 1,
        "second-revision": 1,
    }
    assert (rows, values, annotations, baselines) == before
    assert result[0]["units_json"][0] is not result[1]["units_json"][0]
    assert result[0]["units_json"][0]["timing"] is not result[1]["units_json"][0]["timing"]
    result[0]["units_json"][0]["timing"]["start_ms"] = 7
    result[0]["settings_json"]["character_dictionary"].append({"id": "new"})
    assert result[1] == expected[1] and (rows, values, annotations, baselines) == before


def test_bulk_validation_and_remap_reference_work_is_once_per_graph_revision(monkeypatch):
    rows, values, annotations, baselines = _bulk_inputs()
    rows["performance_plans"] = freeze_performance_plan_rows(
        values,
        rows=rows,
        annotations=annotations,
        base_adopted_plan_ids=baselines,
    )
    references = Mock(wraps=codec.segment_copy_references)
    signature = Mock(wraps=codec.speech_plan_signature)
    ownership = Mock(wraps=codec._graph)
    monkeypatch.setattr(codec, "segment_copy_references", references)
    monkeypatch.setattr(codec, "speech_plan_signature", signature)
    monkeypatch.setattr(codec, "_graph", ownership)
    validate_performance_copy_graph(rows, allow_performance=True)
    assert ownership.call_count == 1 and references.call_count == 1
    assert Counter(call.args[0][0]["plan_revision_id"] for call in signature.call_args_list) == {
        "revision": 1,
        "second-revision": 1,
    }
    references.reset_mock()
    signature.reset_mock()
    ownership.reset_mock()
    families = (
        "sessions",
        "projects",
        "generation_plans",
        "generation_plan_revisions",
        "generation_segments",
        "performance_plans",
    )
    identities = {row["id"]: f"new-{row['id']}" for name in families for row in rows[name]}
    mapped = deepcopy(rows)
    mapped.pop("performance_plans")
    for name in families[:-1]:
        for row in mapped[name]:
            for key in (
                "id",
                "project_id",
                "session_id",
                "active_revision_id",
                "plan_id",
                "plan_revision_id",
            ):
                if key in row:
                    row[key] = identities[row[key]]
    result = remap_performance_plan_rows(rows, identities, mapped_rows=mapped)
    assert len(result["performance_plans"]) == 5
    # One source graph for validation; one destination graph reused for final validation.
    assert ownership.call_count == 2 and references.call_count == 2
    assert Counter(call.args[0][0]["plan_revision_id"] for call in signature.call_args_list) == {
        "revision": 1,
        "second-revision": 1,
        "new-revision": 1,
        "new-second-revision": 1,
    }


def test_bulk_empty_and_over_budget_inputs_do_not_construct_graph(monkeypatch):
    ownership = Mock(side_effect=AssertionError("No graph before finite family budget"))
    monkeypatch.setattr(codec, "_graph", ownership)
    untouched: Any = Discarded()
    assert (
        freeze_performance_plan_rows(
            [], rows=untouched, annotations=untouched, base_adopted_plan_ids=untouched
        )
        == []
    )
    with pytest.raises(ValueError):
        freeze_performance_plan_rows(
            [{}] * (MAX_PERFORMANCE_PLANS + 1),
            rows=untouched,
            annotations=untouched,
            base_adopted_plan_ids=untouched,
        )
    with pytest.raises(ValueError):
        freeze_performance_plan_rows(
            [{"units_json": [{}] * (MAX_PERFORMANCE_UNITS + 1)}],
            rows=untouched,
            annotations=untouched,
            base_adopted_plan_ids=untouched,
        )
    ownership.assert_not_called()
