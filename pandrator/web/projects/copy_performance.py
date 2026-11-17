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
from ..performance_validity import FITTED, STRUCTURE_FIELDS, UNCHANGED, judge_block
from ..settings_policy import stable_hash
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
# A block hash is tied to the source project's identities, so a copy reads
# past it and the copied plan compares blocks field by field instead.
_SOURCE_UNIT_OPTIONAL = frozenset({"source_hash"})
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


def _source_unit(raw: Any, *, judged: bool = False) -> bool:
    """A raw source unit's shape; its block's ``source_hash`` is read past.

    A judged unit may lack the structure fields ``judge_block`` skips, as units
    made before those fields were recorded do. Its copy is built from the
    current segment, so nothing missing reaches the destination.
    """
    required = _UNIT_FIELDS - set(STRUCTURE_FIELDS) if judged else _UNIT_FIELDS
    return (
        type(raw) is dict
        and required <= set(raw)
        and not set(raw) - _UNIT_FIELDS - _UNIT_OPTIONAL - _SOURCE_UNIT_OPTIONAL
        and type(raw.get("source_hash", "")) is str
    )


def _review_signature(signature: str) -> str:
    """Derived stand-in for a signature that cannot match: some block needs review.

    A row carrying it is judged again on every read, and a block with no unit
    there is simply one more block to review. So a row with this signature
    that lacks a kept entry still survives its own round trip: the check
    proves the row is consistent, not that no entry was dropped.
    """
    return stable_hash({"performance_review": signature})


def _judged_entries(
    raw_units: list[Any],
    canonical: list[dict[str, Any]],
    segment_ids: set[str],
    annotations: dict[str, Any],
    characters: list[dict[str, Any]],
) -> tuple[list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None, bool]], bool]:
    """Judge a stale adopted plan per block with ``judge_block``, as the destination reads it.

    Each canonical unit is kept as it is (unchanged), rebased onto its current
    text (fitted), or left out because it needs review. Returns the kept
    ``(unit, raw, record, fitted)`` entries in ordinal order and whether any
    spoken block is left without one.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for raw in raw_units:
        if (
            not _source_unit(raw, judged=True)
            or type(raw["id"]) is not str
            or raw["id"] in by_id
            or raw["id"] not in segment_ids
            or type(raw["spoken_text"]) is not str
            or raw["text_hash"] != content_hash(raw["spoken_text"])
        ):
            raise _invalid()
        by_id[raw["id"]] = raw
    if set(annotations) - segment_ids:
        raise _invalid()
    entries = []
    review = False
    for unit in canonical:
        raw = by_id.get(unit["id"])
        stored = annotations.get(unit["id"])
        if stored is not None and type(stored) is not dict:
            raise _invalid()
        try:
            state, entry = judge_block(
                raw,
                stored,
                spoken_text=unit["spoken_text"],
                removed=False,
                structure={field: unit[field] for field in STRUCTURE_FIELDS},
                segment_id=unit["id"],
                characters=characters,
            )
        except (AttributeError, KeyError, TypeError, ValueError) as error:
            raise _invalid() from error
        if raw is None or state not in {UNCHANGED, FITTED}:
            review = True
            continue
        if entry is None:
            # An adopted plan holds an entry for every block it planned.
            raise _invalid()
        entries.append((unit, raw, _record(entry, unit, characters), state == FITTED))
    return entries, review


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
    # An adopted plan outlives an edit that removes or blanks every block; it
    # then carries no unit, as each removed or blank block carries none.
    if not canonical and status != "adopted":
        raise _invalid()
    if type(annotations) is not dict or any(type(key) is not str for key in annotations):
        raise _invalid()
    characters = settings["character_dictionary"]
    # An adopted plan whose signature no longer matches is judged per block,
    # as the destination judges it; a draft or a fresh plan is never judged.
    judged = status == "adopted" and row.get("base_signature") != signatures[rid]
    review = False
    if judged:
        entries, review = _judged_entries(
            raw_units,
            canonical,
            {segment["id"] for segment in segments[rid]},
            annotations,
            characters,
        )
    else:
        if len(raw_units) != len(canonical):
            raise _invalid()
        if set(annotations) - {unit["id"] for unit in canonical}:
            raise _invalid()
        if status == "adopted" and len(annotations) != len(canonical):
            raise _invalid()
        entries = []
        for raw, unit in zip(raw_units, canonical, strict=True):
            if not _source_unit(raw) or any(
                raw[key] != unit[key] for key in ("id", "text", "spoken_text", "text_hash")
            ):
                raise _invalid()
            record = (
                _record(annotations[unit["id"]], unit, characters)
                if unit["id"] in annotations
                else None
            )
            entries.append((unit, raw, record, False))
    records = {}
    kept = []
    for unit, raw, record, fitted in entries:
        source_xml = None
        if "speech_xml" in raw:
            if type(raw["speech_xml"]) is not str:
                raise _invalid()
            # A fitted unit's own markup was parsed against its earlier text;
            # its effective markup comes from the fitted record instead.
            if not fitted:
                source_xml = parse_speech_markup(
                    raw["speech_xml"],
                    expected_segment_id=unit["id"],
                    expected_text=unit["spoken_text"],
                    characters=characters,
                ).xml
        if record is not None:
            records[unit["id"]] = record
        # A plan made from another plan's entries marks each seeded unit without
        # markup as a legacy choice, whatever its format. Only an XML plan reads
        # the flag, so a pSSML plan's is read past and never emitted. A fitted
        # record keeps its entry's kind, so it is checked like any other.
        legacy = raw.get("legacy_annotation", False)
        if type(legacy) is not bool or (
            legacy
            and settings["annotation_format"] == "xml"
            and (record is None or SPEECH_XML_KEY in record)
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
        kept.append(unit)
    result = {
        "id": identifier,
        "session_id": sid,
        "plan_revision_id": rid,
        # A plan with a block still to review keeps a signature that differs
        # from the current one, so the destination judges it per block again.
        "base_signature": _review_signature(signatures[rid]) if review else signatures[rid],
        "settings_json": settings,
        "units_json": kept,
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
