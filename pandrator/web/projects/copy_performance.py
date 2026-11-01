"""Finite current performance intent, detached from analysis execution history."""

from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash, validate_annotation

from ..generation_cast_runtime import remap_markup
from ..generation_control_schemas import CharacterEntry
from ..performance_schemas import PerformancePlanCreateRequest
from ..speech_annotation_records import SPEECH_XML_KEY, normalized_record
from .copy_speech import segment_copy_references
from .copy_speech_plan import speech_plan_signature

PERFORMANCE_FAMILIES = frozenset({"performance_plans"})
MAX_PERFORMANCE_PLANS = 1000
MAX_PERFORMANCE_UNITS = 250_000
PLAN_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "plan_revision_id",
        "base_signature",
        "settings_json",
        "units_json",
        "manual_annotations_json",
        "status",
        "version",
        "job_id",
    }
)
_UNIT_FIELDS = frozenset(
    {
        "id",
        "text",
        "speaker",
        "language",
        "node_kind",
        "section_id",
        "ordinal",
        "spoken_text",
        "text_hash",
        "voice",
        "paragraph_break_after",
        "timing",
    }
)
_UNIT_OPTIONAL = frozenset({"speech_xml", "legacy_annotation"})
_CONTEXT_FIELDS = (
    "context_before",
    "context_after",
    "context_max_chars",
    "batch_size",
    "allow_vocalizations",
)
_TIMING_FIELDS = (
    "start_ms",
    "end_ms",
    "target_duration_ms",
    "anchor_start_ms",
    "anchor_end_ms",
    "duration_ms",
    "start",
    "end",
    "budget_ms",
)
_SETTINGS_FIELDS = frozenset(PerformancePlanCreateRequest.model_fields) | {
    "workflow_kind",
    "general_direction",
    "capabilities",
    "base_adopted_plan_id",
    "base_adopted_plan_version",
    "character_dictionary",
    "character_dictionary_revision",
}


def _invalid() -> ValueError:
    return ValueError("Invalid current performance-plan copy graph.")


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= 36


def _finite(value: Any) -> None:
    if value is None or type(value) in {str, int, bool}:
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid()
        return
    if type(value) is list:
        for item in value:
            _finite(item)
        return
    if type(value) is dict and all(type(key) is str for key in value):
        for item in value.values():
            _finite(item)
        return
    raise _invalid()


def _index(values: Any) -> dict[str, dict[str, Any]]:
    if type(values) is not list:
        raise _invalid()
    result = {}
    for row in values:
        if type(row) is not dict or not _identifier(row.get("id")) or row["id"] in result:
            raise _invalid()
        result[row["id"]] = row
    return result


def _graph(
    rows: Mapping[str, Any],
) -> tuple[
    dict[str, dict[str, Any]],
    dict[str, str],
    dict[str, list[dict[str, Any]]],
    dict[str, str],
]:
    sessions = _index(rows["sessions"])
    plans = _index(rows["generation_plans"])
    revisions = _index(rows["generation_plan_revisions"])
    segments = _index(rows["generation_segments"])
    owners: dict[str, str] = {}
    seen_sessions: set[str] = set()
    for plan in plans.values():
        sid, rid = plan["session_id"], plan["active_revision_id"]
        if (
            not _identifier(sid)
            or not _identifier(rid)
            or sid not in sessions
            or sid in seen_sessions
            or rid not in revisions
            or revisions[rid]["plan_id"] != plan["id"]
            or rid in owners
        ):
            raise _invalid()
        seen_sessions.add(sid)
        owners[rid] = sid
    if set(owners) != set(revisions):
        raise _invalid()
    by_revision: dict[str, list[dict[str, Any]]] = {rid: [] for rid in revisions}
    ordinals: dict[str, set[int]] = {rid: set() for rid in revisions}
    for segment in segments.values():
        rid, ordinal = segment["plan_revision_id"], segment["ordinal"]
        if (
            not _identifier(rid)
            or rid not in by_revision
            or type(ordinal) is not int
            or ordinal < 0
            or ordinal in ordinals[rid]
        ):
            raise _invalid()
        ordinals[rid].add(ordinal)
        by_revision[rid].append(segment)
    for selected in by_revision.values():
        selected.sort(key=lambda value: value["ordinal"])
    references = segment_copy_references(rows)
    signatures = {
        rid: speech_plan_signature(selected, references=references)
        for rid, selected in by_revision.items()
    }
    return sessions, owners, by_revision, signatures


def _settings(
    raw: Any,
    *,
    revision_id: str,
    workflow_kind: str,
    status: str,
    baseline: Any,
) -> dict[str, Any]:
    if type(raw) is not dict or type(workflow_kind) is not str or not workflow_kind:
        raise _invalid()
    if baseline is not None and (status != "draft" or not _identifier(baseline)):
        raise _invalid()
    # Read only retained fields: discarded provider/history values are never walked.
    values = {key: raw[key] for key in _CONTEXT_FIELDS if key in raw}
    values.update(
        expected_plan_revision_id=revision_id,
        purpose=raw.get("purpose", "delivery"),
        annotation_format=raw.get("annotation_format", "pssml"),
        mode="manual",
        model_name="",
        instructions="",
        copy_from_id=None,
    )
    result = PerformancePlanCreateRequest.model_validate(values).model_dump(mode="json")
    dictionary = raw.get("character_dictionary", [])
    if type(dictionary) is not list or len(dictionary) > 2000:
        raise _invalid()
    characters = [
        CharacterEntry.model_validate(value).model_dump(mode="json") for value in dictionary
    ]
    identifiers = [character["id"] for character in characters]
    if any(not value.strip() for value in identifiers) or len(set(identifiers)) != len(identifiers):
        raise _invalid()
    result.update(
        workflow_kind=workflow_kind,
        general_direction="",
        capabilities={},
        base_adopted_plan_id=baseline,
        base_adopted_plan_version=1 if baseline is not None else None,
        character_dictionary=characters,
        character_dictionary_revision=1,
    )
    return result


def _unit(segment: dict[str, Any]) -> dict[str, Any] | None:
    text, optimized, removed = segment["text"], segment["optimized_text"], segment["removed"]
    if (
        type(text) is not str
        or optimized is not None
        and type(optimized) is not str
        or type(removed) is not bool
    ):
        raise _invalid()
    spoken = optimized or text
    if removed or not spoken.strip():
        return None
    if len(spoken) > 24000:
        raise _invalid()
    provenance = segment["speech_block_provenance_json"]
    if type(provenance) is not dict:
        raise _invalid()
    if any(
        value is not None and type(value) is not str
        for value in (provenance.get("section_id"), segment["speaker"], segment["language"])
    ):
        raise _invalid()
    section = provenance.get("section_id") or ""
    speaker, language = segment["speaker"] or "", segment["language"] or ""
    if (
        any(type(value) is not str for value in (section, speaker, language, segment["node_kind"]))
        or type(segment["paragraph_break_after"]) is not bool
    ):
        raise _invalid()
    timing = {key: provenance[key] for key in _TIMING_FIELDS if key in provenance}
    if any(
        type(value) not in {int, float} or not math.isfinite(value) for value in timing.values()
    ):
        raise _invalid()
    return {
        "id": segment["id"],
        "text": text,
        "speaker": speaker,
        "language": language,
        "node_kind": segment["node_kind"],
        "section_id": section,
        "ordinal": segment["ordinal"],
        "spoken_text": spoken,
        "text_hash": content_hash(spoken),
        "voice": "",
        "paragraph_break_after": segment["paragraph_break_after"],
        "timing": timing,
    }


def _record(value: Any, unit: dict[str, Any], characters: list[dict[str, Any]]) -> dict[str, Any]:
    if type(value) is not dict:
        raise _invalid()
    pure = {key: item for key, item in value.items() if key != SPEECH_XML_KEY}
    validated = validate_annotation(unit["spoken_text"], pure)
    if SPEECH_XML_KEY not in value:
        return validated
    xml = value[SPEECH_XML_KEY]
    if type(xml) is not str:
        raise _invalid()
    result = normalized_record(
        unit["spoken_text"],
        unit["id"],
        {"speech_xml": xml, "locked": validated["locked"], "reason": validated["reason"]},
        characters,
    )
    derived = {
        key: item for key, item in result.items() if key not in {SPEECH_XML_KEY, "confidence"}
    }
    if derived != {key: item for key, item in validated.items() if key != "confidence"}:
        raise _invalid()
    result["confidence"] = validated["confidence"]
    return result


def _freeze(
    row: Mapping[str, Any],
    annotations: Any,
    baseline: Any,
    graph: tuple[
        dict[str, dict[str, Any]], dict[str, str], dict[str, list[dict[str, Any]]], dict[str, str]
    ],
) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise _invalid()
    identifier, sid, rid = row["id"], row["session_id"], row["plan_revision_id"]
    status = row["status"]
    sessions, owners, segments, signatures = graph
    if (
        not all(_identifier(value) for value in (identifier, sid, rid))
        or type(status) is not str
        or status not in {"draft", "adopted"}
        or sid not in sessions
        or owners.get(rid) != sid
        or identifier == baseline
    ):
        raise _invalid()
    settings = _settings(
        row["settings_json"],
        revision_id=rid,
        workflow_kind=sessions[sid]["workflow_kind"],
        status=status,
        baseline=baseline,
    )
    raw_units = row["units_json"]
    if type(raw_units) is not list or len(raw_units) > MAX_PERFORMANCE_UNITS:
        raise _invalid()
    canonical = [unit for segment in segments[rid] if (unit := _unit(segment)) is not None]
    if not canonical or len(raw_units) != len(canonical):
        raise _invalid()
    if type(annotations) is not dict or any(type(key) is not str for key in annotations):
        raise _invalid()
    if set(annotations) - {unit["id"] for unit in canonical}:
        raise _invalid()
    if status == "adopted" and len(annotations) != len(canonical):
        raise _invalid()
    records = {}
    characters = settings["character_dictionary"]
    for raw, unit in zip(raw_units, canonical, strict=True):
        if (
            type(raw) is not dict
            or not _UNIT_FIELDS <= set(raw)
            or set(raw) - _UNIT_FIELDS - _UNIT_OPTIONAL
            or any(raw[key] != unit[key] for key in ("id", "text", "spoken_text", "text_hash"))
        ):
            raise _invalid()
        source_xml = None
        if "speech_xml" in raw:
            if type(raw["speech_xml"]) is not str:
                raise _invalid()
            source_xml = parse_speech_markup(
                raw["speech_xml"],
                expected_segment_id=unit["id"],
                expected_text=unit["spoken_text"],
                characters=characters,
            ).xml
        record = (
            _record(annotations[unit["id"]], unit, characters)
            if unit["id"] in annotations
            else None
        )
        if record is not None:
            records[unit["id"]] = record
        legacy = raw.get("legacy_annotation", False)
        if (
            type(legacy) is not bool
            or legacy
            and (
                settings["annotation_format"] != "xml" or record is None or SPEECH_XML_KEY in record
            )
        ):
            raise _invalid()
        if (
            settings["annotation_format"] == "xml"
            and record is not None
            and SPEECH_XML_KEY not in record
        ):
            unit["legacy_annotation"] = True
        else:
            effective = record.get(SPEECH_XML_KEY) if record is not None else None
            effective = effective or source_xml
            if effective is not None:
                unit["speech_xml"] = effective
            elif settings["annotation_format"] == "xml":
                raise _invalid()
    result = {
        "id": identifier,
        "session_id": sid,
        "plan_revision_id": rid,
        "base_signature": signatures[rid],
        "settings_json": settings,
        "units_json": canonical,
        "manual_annotations_json": records,
        "status": status,
        "version": 1,
        "job_id": None,
    }
    _finite(result)
    json.dumps(result, allow_nan=False, ensure_ascii=False).encode("utf-8")
    return result


def _raw_unit_budget(values: Any) -> None:
    if type(values) is not list or len(values) > MAX_PERFORMANCE_PLANS:
        raise _invalid()
    total_units = 0
    for row in values:
        if not isinstance(row, Mapping) or type(row.get("units_json")) is not list:
            raise _invalid()
        total_units += len(row["units_json"])
        if total_units > MAX_PERFORMANCE_UNITS:
            raise _invalid()


def freeze_performance_plan_rows(
    values: Sequence[Mapping[str, Any]],
    *,
    rows: Mapping[str, Any],
    annotations: Mapping[str, Any],
    base_adopted_plan_ids: Mapping[str, str | None],
) -> list[dict[str, Any]]:
    """Freeze the included family with one call-local ownership/signature context."""
    try:
        _raw_unit_budget(values)
        if not values:
            return []
        if (
            not isinstance(rows, Mapping)
            or not isinstance(annotations, Mapping)
            or not isinstance(base_adopted_plan_ids, Mapping)
        ):
            raise _invalid()
        identifiers = [row["id"] for row in values]
        if any(not _identifier(value) for value in identifiers) or len(set(identifiers)) != len(
            identifiers
        ):
            raise _invalid()
        graph = _graph(rows)
        return [
            _freeze(row, annotations[row["id"]], base_adopted_plan_ids[row["id"]], graph)
            for row in values
        ]
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError) as error:
        raise _invalid() from error


def freeze_performance_plan(
    row: Mapping[str, Any],
    *,
    rows: Mapping[str, Any],
    annotations: Any,
    base_adopted_plan_id: str | None,
) -> dict[str, Any]:
    """Freeze one already-qualified current source row without walking discarded fields."""
    try:
        if not isinstance(rows, Mapping):
            raise _invalid()
        return _freeze(row, annotations, base_adopted_plan_id, _graph(rows))
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError) as error:
        raise _invalid() from error


def _validate_performance_rows(
    values: list[dict[str, Any]],
    graph: tuple[
        dict[str, dict[str, Any]], dict[str, str], dict[str, list[dict[str, Any]]], dict[str, str]
    ],
) -> None:
    plans = _index(values)
    adopted: dict[str, str] = {}
    for row in plans.values():
        if set(row) != PLAN_FIELDS:
            raise _invalid()
        _finite(row)
        settings = row["settings_json"]
        if (
            type(settings) is not dict
            or set(settings) != _SETTINGS_FIELDS
            or type(row["version"]) is not int
            or row["version"] != 1
            or row["job_id"] is not None
            or type(settings["character_dictionary_revision"]) is not int
            or settings["character_dictionary_revision"] != 1
            or settings["base_adopted_plan_version"] is not None
            and type(settings["base_adopted_plan_version"]) is not int
        ):
            raise _invalid()
        for unit in row["units_json"]:
            if (
                type(unit) is not dict
                or not _UNIT_FIELDS <= set(unit)
                or set(unit) - _UNIT_FIELDS - _UNIT_OPTIONAL
                or any(
                    type(unit[key]) is not str
                    for key in _UNIT_FIELDS - {"ordinal", "paragraph_break_after", "timing"}
                )
                or type(unit["ordinal"]) is not int
                or type(unit["paragraph_break_after"]) is not bool
                or type(unit["timing"]) is not dict
                or set(unit["timing"]) - set(_TIMING_FIELDS)
                or any(
                    type(value) not in {int, float} or not math.isfinite(value)
                    for value in unit["timing"].values()
                )
                or "legacy_annotation" in unit
                and type(unit["legacy_annotation"]) is not bool
                or "speech_xml" in unit
                and type(unit["speech_xml"]) is not str
            ):
                raise _invalid()
        baseline = settings["base_adopted_plan_id"]
        canonical = _freeze(row, row["manual_annotations_json"], baseline, graph)
        if json.dumps(canonical, sort_keys=True) != json.dumps(row, sort_keys=True):
            raise _invalid()
        rid = row["plan_revision_id"]
        if row["status"] == "adopted":
            if rid in adopted:
                raise _invalid()
            adopted[rid] = row["id"]
    for row in plans.values():
        baseline = row["settings_json"]["base_adopted_plan_id"]
        if row["status"] == "draft" and baseline != adopted.get(row["plan_revision_id"]):
            raise _invalid()


def validate_performance_copy_graph(
    rows: Mapping[str, Any],
    *,
    allow_performance: bool = False,
) -> None:
    """Validate finite performance schemas and their current speech ownership closure."""
    try:
        if not isinstance(rows, Mapping):
            raise _invalid()
        values = rows.get("performance_plans", [])
        if type(values) is not list:
            raise _invalid()
        if not values:
            return
        if not allow_performance or len(values) > MAX_PERFORMANCE_PLANS:
            raise _invalid()
        _raw_unit_budget(values)
        _validate_performance_rows(values, _graph(rows))
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError) as error:
        raise _invalid() from error


def remap_performance_plan_rows(
    rows: Mapping[str, Any],
    identities: Mapping[str, str],
    *,
    mapped_rows: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Rebind managed identities, canonical XML roots and the mapped speech signature."""
    try:
        validate_performance_copy_graph(rows, allow_performance=True)
        values = rows.get("performance_plans", [])
        if not values:
            return {"performance_plans": []}
        required = {
            value
            for row in values
            for value in (
                row["id"],
                row["session_id"],
                row["plan_revision_id"],
                *(unit["id"] for unit in row["units_json"]),
            )
        }
        if (
            not isinstance(identities, Mapping)
            or any(
                value not in identities or not _identifier(identities[value]) for value in required
            )
            or len({identities[value] for value in required}) != len(required)
        ):
            raise _invalid()
        result = []
        graph = _graph(mapped_rows)
        for row in values:
            mapped = deepcopy(row)
            mapped.update(
                id=identities[row["id"]],
                session_id=identities[row["session_id"]],
                plan_revision_id=identities[row["plan_revision_id"]],
            )
            dictionary = mapped["settings_json"]["character_dictionary"]
            annotations = {}
            for unit in mapped["units_json"]:
                source_id = unit["id"]
                unit["id"] = identities[source_id]
                if "speech_xml" in unit:
                    unit["speech_xml"] = remap_markup(
                        unit["speech_xml"], unit["id"], unit["spoken_text"], dictionary
                    )
                if source_id in mapped["manual_annotations_json"]:
                    record = mapped["manual_annotations_json"][source_id]
                    if SPEECH_XML_KEY in record:
                        record[SPEECH_XML_KEY] = remap_markup(
                            record[SPEECH_XML_KEY], unit["id"], unit["spoken_text"], dictionary
                        )
                    annotations[unit["id"]] = record
            baseline = mapped["settings_json"]["base_adopted_plan_id"]
            result.append(
                _freeze(
                    mapped,
                    annotations,
                    identities[baseline] if baseline is not None else None,
                    graph,
                )
            )
        _raw_unit_budget(result)
        _validate_performance_rows(result, graph)
        return {"performance_plans": result}
    except (TypeError, ValueError, KeyError, RecursionError, OverflowError) as error:
        raise _invalid() from error
