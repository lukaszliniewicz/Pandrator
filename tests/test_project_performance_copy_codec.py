"""In-memory current performance intent, strict closure and independent remapping."""

from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from typing import Any
from unittest.mock import Mock

import pytest

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash, validate_annotation
from pandrator.web.generation_cast_runtime import remap_markup
from pandrator.web.generation_control_schemas import VoiceBinding
from pandrator.web.performance_validity import STRUCTURE_FIELDS
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
from pandrator.web.settings_policy import stable_hash
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


# A stale adopted plan is judged per segment: kept, rebased or left for review.
CHARACTERS = [{"id": "source-session", "display_name": "Narrator"}]
_FAMILIES = (
    "sessions",
    "projects",
    "generation_plans",
    "generation_plan_revisions",
    "generation_segments",
)
_OWNERS = ("id", "session_id", "project_id", "active_revision_id", "plan_id", "plan_revision_id")


def judged_rows():
    """Five segments: a dialogue entry, a plain entry, a removed, a blank and a last one."""
    rows = graph()
    extra = deepcopy(rows["generation_segments"][0])
    extra["id"], extra["ordinal"] = "segment-4", 4
    extra["speech_block_provenance_json"] = {"section_id": "source-session", "start_ms": 4000}
    rows["generation_segments"].append(extra)
    texts = ("Go on now.", "Second line here.", "Removed.", "   ", "Last one.")
    for segment, text in zip(rows["generation_segments"], texts, strict=True):
        segment.update(text=text, optimized_text=None)
    return rows


def stale_adopted(rows):
    """The raw adopted row and its annotations as stored before any edit."""
    units, annotations = [], {}
    for segment in rows["generation_segments"]:
        text, key = segment["text"], segment["id"]
        if segment["removed"] or not text.strip():
            continue
        inner = (
            f'<ins>calm</ins><dialogue><speaker ref="source-session">{text}</speaker></dialogue>'
            if key == "segment-0"
            else text
        )
        annotations[key] = normalized_record(
            text,
            key,
            {"speech_xml": f'<segment id="{key}">{inner}</segment>', "locked": key == "segment-0"},
            CHARACTERS,
        )
        units.append(
            {
                "id": key,
                "text": text,
                "speaker": segment["speaker"],
                "language": "en",
                "node_kind": "paragraph",
                "section_id": "source-session",
                "ordinal": segment["ordinal"],
                "spoken_text": text,
                "text_hash": content_hash(text),
                "voice": "",
                "paragraph_break_after": True,
                "timing": {"start_ms": segment["ordinal"] * 1000},
                "speech_xml": annotations[key]["_speech_xml"],
            }
        )
    raw = raw_plan(rows, identifier="adopted", status="adopted", xml=True)
    raw.update(units_json=units, base_signature="signature-before-the-edit")
    raw["settings_json"]["character_dictionary"] = CHARACTERS
    return raw, annotations


def edit(rows, **texts):
    for key, text in texts.items():
        rows["generation_segments"][int(key.removeprefix("s"))]["text"] = text


def current_signature(rows):
    (signature,) = codec._graph(rows)[3].values()
    return signature


def review_signature(signature):
    return stable_hash({"performance_review": signature})


def judged(rows, **texts):
    raw, annotations = stale_adopted(rows)
    edit(rows, **texts)
    return freeze(rows, raw, annotations)


def unit_ids(frozen):
    return [unit["id"] for unit in frozen["units_json"]]


def round_trip(rows, frozen):
    rows["performance_plans"] = [frozen]
    validate_performance_copy_graph(rows, allow_performance=True)


def mapped_copy(rows):
    identities = {
        row["id"]: f"new-{row['id']}"
        for family in (*_FAMILIES, "performance_plans")
        for row in rows[family]
    }
    mapped = deepcopy(rows)
    mapped.pop("performance_plans")
    for family in _FAMILIES:
        for row in mapped[family]:
            for key in _OWNERS:
                if key in row:
                    row[key] = identities[row[key]]
    # The destination's own signature differs from the source's.
    mapped["generation_segments"][4]["silence_after_ms"] = 250
    return identities, mapped


def test_signature_only_change_keeps_every_entry_and_is_fresh():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    before = deepcopy((raw, annotations, rows))
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == ["segment-0", "segment-1", "segment-4"]
    assert frozen["base_signature"] == current_signature(rows) != raw["base_signature"]
    assert frozen["manual_annotations_json"] == annotations
    assert (raw, annotations, rows) == before
    round_trip(rows, frozen)


def test_fitted_entries_are_rebased_inside_a_plain_entry_and_the_dialogue_quote():
    rows = judged_rows()
    frozen = judged(rows, s0="Go on right now.", s1="Second line there.")
    assert unit_ids(frozen) == ["segment-0", "segment-1", "segment-4"]
    assert frozen["base_signature"] == current_signature(rows)
    by_id = {unit["id"]: unit for unit in frozen["units_json"]}
    assert by_id["segment-0"]["spoken_text"] == "Go on right now."
    assert by_id["segment-0"]["text_hash"] == content_hash("Go on right now.")
    record = frozen["manual_annotations_json"]["segment-0"]
    assert record["locked"] is True
    assert '<speaker ref="source-session">Go on right now.</speaker>' in record["_speech_xml"]
    assert by_id["segment-0"]["speech_xml"] == record["_speech_xml"]
    assert "<ins>calm</ins>" in record["_speech_xml"]
    assert frozen["manual_annotations_json"]["segment-1"]["_speech_xml"] == (
        '<segment id="segment-1">Second line there.</segment>'
    )
    dumped = json.dumps(frozen)
    assert "Go on now." not in dumped and "Second line here." not in dumped
    round_trip(rows, frozen)


def test_a_change_reaching_the_dialogue_edge_is_left_for_review():
    rows = judged_rows()
    frozen = judged(rows, s0="Go on now!", s1="Second line there.")
    assert unit_ids(frozen) == ["segment-1", "segment-4"]
    assert set(frozen["manual_annotations_json"]) == {"segment-1", "segment-4"}
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    assert frozen["base_signature"] != current_signature(rows)
    assert len(frozen["base_signature"]) <= 128
    dumped = json.dumps(frozen)
    assert "Go on now." not in dumped and "Second line here." not in dumped
    round_trip(rows, frozen)


def test_removed_and_blanked_segments_lose_their_unit_without_review():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    rows["generation_segments"][1]["removed"] = True
    edit(rows, s4="   ")
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == ["segment-0"]
    assert frozen["base_signature"] == current_signature(rows)
    assert "Second line here." not in json.dumps(frozen)
    round_trip(rows, frozen)


def test_a_spoken_segment_without_a_unit_is_left_for_review():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    raw["units_json"].pop()
    annotations.pop("segment-4")
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == ["segment-0", "segment-1"]
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    round_trip(rows, frozen)


def test_judged_units_are_matched_by_id_not_position():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    raw["units_json"].reverse()
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == ["segment-0", "segment-1", "segment-4"]
    assert frozen == freeze(rows, *stale_adopted(rows))


@pytest.mark.parametrize("missing", [("section_id",), ("language",), STRUCTURE_FIELDS])
def test_judged_units_made_before_structure_fields_were_recorded_need_review(missing):
    # The copy rebuilds each unit from its current block, so an entry kept for
    # a unit that never recorded a field would hide a change to that field (the
    # language below) and look current there. Such units need review, as a
    # plan read in the app judges them; the copy itself still succeeds.
    def changed(rows):
        edit(rows, s0="Go on right now.")
        rows["generation_segments"][1]["language"] = "fr"

    recorded = judged_rows()
    raw, annotations = stale_adopted(recorded)
    changed(recorded)
    assert unit_ids(freeze(recorded, raw, annotations)) == ["segment-0", "segment-4"]
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    for unit in raw["units_json"]:
        for field in missing:
            del unit[field]
    changed(rows)
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == []
    assert frozen["manual_annotations_json"] == {}
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    round_trip(rows, frozen)


@pytest.mark.parametrize("missing", ["voice", "text_hash", "spoken_text"])
def test_judged_units_still_need_their_other_fields(missing):
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    del raw["units_json"][1][missing]
    edit(rows, s0="Go on right now.")
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


def test_a_fresh_adopted_unit_still_needs_every_field():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    raw["base_signature"] = current_signature(rows)
    freeze(rows, deepcopy(raw), deepcopy(annotations))
    del raw["units_json"][1]["section_id"]
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


@pytest.mark.parametrize("edited", [{"s0": "Go on right now."}, {"s0": "Go on now!"}])
def test_judged_rows_remap_with_ids_and_the_review_signature_on_the_destination(edited):
    rows = judged_rows()
    frozen = judged(rows, **edited)
    rows["performance_plans"] = [frozen]
    validate_performance_copy_graph(rows, allow_performance=True)
    identities, mapped = mapped_copy(rows)
    before = deepcopy((rows, mapped))
    destination = remap_performance_plan_rows(rows, identities, mapped_rows=mapped)[
        "performance_plans"
    ][0]
    assert (rows, mapped) == before
    signature = current_signature(mapped)
    review = edited["s0"].endswith("!")
    assert signature != current_signature(rows)
    assert destination["base_signature"] == (review_signature(signature) if review else signature)
    assert destination["base_signature"] != frozen["base_signature"]
    expected = (
        ["new-segment-1", "new-segment-4"]
        if review
        else ["new-segment-0", "new-segment-1", "new-segment-4"]
    )
    assert unit_ids(destination) == expected
    assert set(destination["manual_annotations_json"]) == set(expected)
    for key, record in destination["manual_annotations_json"].items():
        assert f'id="{key}"' in record["_speech_xml"]
    for record in destination["manual_annotations_json"].values():
        assert 'id="segment-' not in record["_speech_xml"]
    mapped["performance_plans"] = [destination]
    validate_performance_copy_graph(mapped, allow_performance=True)


@pytest.mark.parametrize(
    "defect",
    [
        "stale_signature_full_units",
        "review_value_without_review",
        "review_with_plain_signature",
        "annotation_for_dropped_segment",
        "unit_on_old_text",
        "unit_hash_only_old",
        "record_on_old_text",
        "foreign_unit",
        "fitted_text_changed",
    ],
)
def test_forged_judged_rows_fail_their_own_round_trip(defect):
    rows = judged_rows()
    review = defect in {"review_with_plain_signature", "annotation_for_dropped_segment"}
    frozen = judged(rows, s0="Go on now!" if review else "Go on right now.")
    rows["performance_plans"] = [frozen]
    signature = current_signature(rows)
    unit = frozen["units_json"][0]
    if defect == "stale_signature_full_units":
        frozen["base_signature"] = "arbitrary-stale-signature"
    elif defect == "review_value_without_review":
        frozen["base_signature"] = review_signature(signature)
    elif defect == "review_with_plain_signature":
        frozen["base_signature"] = signature
    elif defect == "annotation_for_dropped_segment":
        frozen["manual_annotations_json"]["segment-0"] = normalized_record(
            "Go on now!",
            "segment-0",
            {"speech_xml": '<segment id="segment-0">Go on now!</segment>'},
            CHARACTERS,
        )
    elif defect == "unit_on_old_text":
        unit.update(text="Go on now.", spoken_text="Go on now.", text_hash=content_hash("Go on now."))
    elif defect == "unit_hash_only_old":
        unit["text_hash"] = content_hash("Go on now.")
    elif defect == "record_on_old_text":
        record = frozen["manual_annotations_json"]["segment-0"]
        record["_speech_xml"] = record["_speech_xml"].replace("Go on right now.", "Go on now.")
    elif defect == "foreign_unit":
        unit["id"] = "segment-9"
    elif defect == "fitted_text_changed":
        unit["text"] = "Go on right now, really."
    with pytest.raises(ValueError):
        validate_performance_copy_graph(rows, allow_performance=True)


@pytest.mark.parametrize(
    "defect", ["duplicate_unit", "foreign_unit", "foreign_annotation", "missing_record"]
)
def test_judged_source_rows_with_a_broken_shape_are_rejected(defect):
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    if defect == "duplicate_unit":
        raw["units_json"].append(deepcopy(raw["units_json"][0]))
    elif defect == "foreign_unit":
        raw["units_json"][0]["id"] = "missing"
    elif defect == "foreign_annotation":
        annotations["missing"] = {}
    else:
        annotations.pop("segment-1")
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


def test_a_draft_on_a_changed_revision_is_never_judged():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    raw.update(status="draft", id="draft")
    edit(rows, s1="Second line there.")
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    raw.update(status="draft", id="draft")
    raw["units_json"].pop()
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


def test_an_adopted_row_with_the_current_signature_freezes_as_before():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    differing = freeze(rows, raw, annotations)
    raw["base_signature"] = current_signature(rows)
    fresh = freeze(rows, raw, annotations)
    assert unit_ids(fresh) == ["segment-0", "segment-1", "segment-4"]
    assert fresh["base_signature"] == current_signature(rows)
    assert fresh == differing
    round_trip(rows, fresh)


def test_a_plan_with_every_spoken_segment_needing_review_freezes_empty():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    # Segment 0 no longer fits; segments 1 and 4 have no unit at all.
    raw["units_json"] = raw["units_json"][:1]
    annotations = {"segment-0": annotations["segment-0"]}
    edit(rows, s0="Go on now!")
    frozen = freeze(rows, raw, annotations)
    assert frozen["status"] == "adopted"
    assert frozen["units_json"] == [] and frozen["manual_annotations_json"] == {}
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    round_trip(rows, frozen)


def test_a_plan_whose_segments_are_all_removed_or_blank_freezes_empty_and_fresh():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    rows["generation_segments"][0]["removed"] = True
    edit(rows, s1="  ", s4="")
    frozen = freeze(rows, raw, annotations)
    assert frozen["status"] == "adopted"
    assert frozen["units_json"] == [] and frozen["manual_annotations_json"] == {}
    assert frozen["base_signature"] == current_signature(rows)
    assert "Go on now." not in json.dumps(frozen)
    round_trip(rows, frozen)
    # A draft still needs a spoken segment.
    raw.update(status="draft", id="draft", base_signature=current_signature(rows))
    raw["units_json"] = []
    with pytest.raises(ValueError):
        freeze(rows, raw, {})


LEGACY_CHOICE = {
    "decision": "steer",
    "delivery": {"pace": "slower"},
    "spans": [{"anchor": {"quote": "Second"}, "delivery": {"emphasis": "light"}}],
    "reason": "Legacy choice",
}


def legacy_adopted(rows, *, xml):
    """A stale adopted plan whose segment-1 entry is a legacy (non-XML) choice."""
    raw, annotations = stale_adopted(rows)
    raw["units_json"][1].pop("speech_xml")
    if xml:
        raw["units_json"][1]["legacy_annotation"] = True
        annotations["segment-1"] = validate_annotation("Second line here.", LEGACY_CHOICE)
        return raw, annotations
    raw["settings_json"]["annotation_format"] = "pssml"
    for unit in raw["units_json"]:
        unit.pop("speech_xml", None)
    annotations = {
        unit["id"]: validate_annotation(
            unit["spoken_text"], LEGACY_CHOICE if unit["id"] == "segment-1" else {}
        )
        for unit in raw["units_json"]
    }
    return raw, annotations


@pytest.mark.parametrize("xml", [True, False], ids=["xml", "pssml"])
def test_a_legacy_entry_is_fitted_when_its_anchors_survive_the_edit(xml):
    rows = judged_rows()
    raw, annotations = legacy_adopted(rows, xml=xml)
    edit(rows, s1="Second line there.")
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == ["segment-0", "segment-1", "segment-4"]
    assert frozen["base_signature"] == current_signature(rows)
    unit = frozen["units_json"][1]
    assert unit["spoken_text"] == "Second line there."
    assert "speech_xml" not in unit
    assert unit.get("legacy_annotation", False) is xml
    assert frozen["manual_annotations_json"]["segment-1"] == validate_annotation(
        "Second line there.", LEGACY_CHOICE
    )
    assert "Second line here." not in json.dumps(frozen)
    round_trip(rows, frozen)


@pytest.mark.parametrize("xml", [True, False], ids=["xml", "pssml"])
def test_a_legacy_entry_whose_anchor_is_gone_is_left_for_review(xml):
    rows = judged_rows()
    raw, annotations = legacy_adopted(rows, xml=xml)
    edit(rows, s1="The line here.")
    frozen = freeze(rows, raw, annotations)
    assert unit_ids(frozen) == ["segment-0", "segment-4"]
    assert "segment-1" not in frozen["manual_annotations_json"]
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    round_trip(rows, frozen)


def test_a_judged_entry_follows_the_spoken_text_not_the_display_text():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    # The display text changes while the spoken (optimized) text stays the same.
    rows["generation_segments"][1].update(
        text="Second line, as displayed.", optimized_text="Second line here."
    )
    frozen = freeze(rows, raw, annotations)
    unit = frozen["units_json"][1]
    assert unit["text"] == "Second line, as displayed."
    assert unit["spoken_text"] == "Second line here."
    assert frozen["manual_annotations_json"]["segment-1"] == annotations["segment-1"]
    assert frozen["base_signature"] == current_signature(rows)
    round_trip(rows, frozen)


def test_a_judged_entry_is_fitted_to_a_changed_optimized_text():
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    rows["generation_segments"][1]["optimized_text"] = "Second line there."
    frozen = freeze(rows, raw, annotations)
    unit = frozen["units_json"][1]
    assert unit["text"] == "Second line here."
    assert unit["spoken_text"] == "Second line there."
    assert frozen["manual_annotations_json"]["segment-1"]["_speech_xml"] == (
        '<segment id="segment-1">Second line there.</segment>'
    )
    assert frozen["base_signature"] == current_signature(rows)
    round_trip(rows, frozen)


@pytest.mark.parametrize(
    "field",
    ["speaker", "language", "node_kind", "section_id"],
)
def test_a_structure_change_leaves_the_block_for_review(field):
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    segment = rows["generation_segments"][1]
    if field == "section_id":
        segment["speech_block_provenance_json"]["section_id"] = "another-section"
    else:
        segment[field] = {"speaker": "another-speaker", "language": "fr"}.get(field, "heading")
    frozen = freeze(rows, raw, annotations)
    # The text is unchanged, but the planner read another block.
    assert unit_ids(frozen) == ["segment-0", "segment-4"]
    assert "segment-1" not in frozen["manual_annotations_json"]
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    round_trip(rows, frozen)


def with_source_hashes(raw):
    for unit in raw["units_json"]:
        unit["source_hash"] = f"source-block-hash-of-{unit['id']}"
    return raw


@pytest.mark.parametrize("path", ["judged", "fresh", "draft"])
def test_a_source_hash_is_accepted_and_read_past_on_every_path(path):
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    if path == "judged":
        edit(rows, s1="Second line there.")
    elif path == "fresh":
        raw["base_signature"] = current_signature(rows)
    else:
        raw.update(status="draft", id="draft")
    plain = freeze(rows, deepcopy(raw), annotations)
    frozen = freeze(rows, with_source_hashes(raw), annotations)
    assert frozen == plain
    assert "source_hash" not in json.dumps(frozen)
    assert "source-block-hash-of-" not in json.dumps(frozen)
    round_trip(rows, frozen)
    raw["units_json"][1]["source_hash"] = 1
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


def test_a_frozen_row_never_carries_a_source_hash():
    rows = judged_rows()
    frozen = judged(rows, s1="Second line there.")
    frozen["units_json"][0]["source_hash"] = "source-block-hash"
    rows["performance_plans"] = [frozen]
    with pytest.raises(ValueError):
        validate_performance_copy_graph(rows, allow_performance=True)


def restored(value):
    """The same value on a restored revision: every block under a new id."""
    return json.loads(json.dumps(value).replace("segment-", "restored-"))


def test_a_plan_carried_stale_by_a_restore_is_judged_as_its_source():
    # The source plan, judged after a fitting edit to segment 1 and one that
    # reaches the dialogue edge in segment 0.
    source_rows = judged_rows()
    raw, annotations = stale_adopted(source_rows)
    edit(source_rows, s0="Go on now!", s1="Second line there.")
    source = freeze(source_rows, with_source_hashes(deepcopy(raw)), annotations)
    # The restore's carry: each unit and its markup move to the new block's id,
    # keep their old text, and the edited blocks keep their old block hash.
    carried = restored(raw)
    for unit in carried["units_json"]:
        unit["source_hash"] = (
            f"old-block-hash-of-{unit['id']}"
            if unit["id"] in {"restored-0", "restored-1"}
            else f"new-block-hash-of-{unit['id']}"
        )
    rows = restored(source_rows)
    frozen = freeze(rows, carried, restored(annotations))
    assert unit_ids(frozen) == ["restored-1", "restored-4"]
    assert frozen["units_json"][0]["spoken_text"] == "Second line there."
    assert frozen["base_signature"] == review_signature(current_signature(rows))
    assert {key: value for key, value in frozen.items() if key != "base_signature"} == {
        key: value for key, value in restored(source).items() if key != "base_signature"
    }
    dumped = json.dumps(frozen)
    assert "block-hash" not in dumped and "Second line here." not in dumped
    round_trip(rows, frozen)


@pytest.mark.parametrize("path", ["fresh", "unchanged", "fitted", "draft"])
def test_a_pssml_unit_marked_as_a_legacy_choice_is_read_past(path):
    # A pSSML plan made from another plan's entries marks its seeded units as
    # legacy choices; only an XML plan reads that flag.
    rows = judged_rows()
    raw, annotations = legacy_adopted(rows, xml=False)
    if path == "fresh":
        raw["base_signature"] = current_signature(rows)
    elif path == "fitted":
        edit(rows, s1="Second line there.")
    elif path == "draft":
        raw.update(status="draft", id="draft")
    plain = deepcopy(raw)
    for unit in raw["units_json"]:
        unit["legacy_annotation"] = True
    frozen = freeze(rows, raw, annotations)
    assert frozen == freeze(rows, plain, annotations)
    assert "legacy_annotation" not in json.dumps(frozen)
    round_trip(rows, frozen)
    raw["units_json"][1]["legacy_annotation"] = "yes"
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


@pytest.mark.parametrize("path", ["unchanged", "fitted"])
def test_an_xml_unit_marked_as_a_legacy_choice_needs_a_legacy_entry(path):
    rows = judged_rows()
    raw, annotations = stale_adopted(rows)
    # Segment 1's entry is markup, not a legacy choice.
    raw["units_json"][1]["legacy_annotation"] = True
    if path == "fitted":
        edit(rows, s1="Second line there.")
    with pytest.raises(ValueError):
        freeze(rows, raw, annotations)


def test_a_review_row_without_one_of_its_kept_entries_still_round_trips():
    # The limit ``_review_signature`` states: under the review signature a block
    # with no unit is one more block to review, so dropping a kept entry leaves
    # a consistent row rather than a forged one.
    rows = judged_rows()
    frozen = judged(rows, s0="Go on now!", s1="Second line there.")
    assert unit_ids(frozen) == ["segment-1", "segment-4"]
    frozen["units_json"].pop()
    frozen["manual_annotations_json"].pop("segment-4")
    round_trip(rows, frozen)
