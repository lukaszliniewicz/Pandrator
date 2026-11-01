"""Pure finite codec for one current editable speech revision per session."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pandrator.logic.speech_markup import parse_speech_markup

from ..generation_cast_runtime import remap_markup
from ..project_speech_references import (
    StoredProjectSpeechReference,
    segment_speech_reference_identity,
)
from ..settings_policy import _secret_free, stable_hash
from ..speech_plan_context import SIGNATURE_FIELDS
from .copy_connections import connection_key
from .copy_speech import segment_copy_references

PLAN_FIELDS = frozenset({"id", "session_id", "active_revision_id"})
REVISION_FIELDS = frozenset({
    "id", "plan_id", "source_revision_id", "parent_revision_id", "revision_number",
    "settings_json", "operation_json", "content_hash",
})
SEGMENT_FIELDS = frozenset({
    "id", "plan_revision_id", "ordinal", "source_segment_ids_json",
    "speech_block_provenance_json", "alignment_group", "node_kind", "paragraph_break_after",
    "speaker", "text", "optimized_text", "speech_plan_json", "optimization_status",
    "optimization_source_hash", "optimization_reviewed", "optimization_model", "voice_id",
    "voice", "language", "silence_after_ms", "marked", "removed", "status", "revision",
})
REVIEW_FIELDS = frozenset({"revision_id", "content_hash"})
SPEECH_FAMILIES = frozenset({
    "generation_plans", "generation_plan_revisions", "generation_segments", "speech_plan_reviews",
})
_HISTORY = {"parent_segment_id", "parent_segment_ids", "parent_revision_id", "parent_revision_ids"}
_SEMANTIC = {"turn_id", "section_id", "character_id", "character_ids", "speaker_id", "speaker_ids"}
_UPSTREAM = {"id", "plan_id", "speech_plan_id", "parent_speech_plan_ids"}
_MANAGED = re.compile(r"(?:^|_)(?:artifact|session|document|revision|segment|source_asset|evidence|plan|voice|sound|provider|connection|job|run)(?:_|$)")
_NULLABLE_TEXT = {
    "alignment_group", "speaker", "optimized_text", "optimization_source_hash",
    "optimization_model", "voice_id", "voice", "language",
}
_BOOLS = {"paragraph_break_after", "optimization_reviewed", "marked", "removed"}
_INTS = {"ordinal", "silence_after_ms", "revision"}


def _invalid() -> ValueError:
    return ValueError("Invalid current speech-plan copy graph.")


def _finite(value: Any) -> None:
    def walk(item: Any) -> None:
        if item is None or type(item) in {str, int, float, bool}:
            return
        if type(item) is list:
            for child in item:
                walk(child)
            return
        if type(item) is dict and all(type(key) is str for key in item):
            for child in item.values():
                walk(child)
            return
        raise _invalid()
    walk(value)
    try:
        json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (ValueError, TypeError, UnicodeError) as error:
        raise _invalid() from error


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value) and len(value) <= 36


def _fields(row: Any, fields: frozenset[str]) -> None:
    if type(row) is not dict or set(row) != fields:
        raise _invalid()
    _finite(row)


def _index(values: Any, fields: frozenset[str], key: str = "id") -> dict[str, dict[str, Any]]:
    if type(values) is not list:
        raise _invalid()
    result = {}
    for row in values:
        _fields(row, fields)
        identifier = row[key]
        if not _identifier(identifier) or identifier in result:
            raise _invalid()
        result[identifier] = row
    return result


def speech_plan_signature(
    segments: list[dict[str, Any]],
    *,
    references: Mapping[str, StoredProjectSpeechReference] | None = None,
) -> str:
    """Match plan_signature, including saved per-segment choices when present."""
    if not references:
        return stable_hash([
            {key: row[key] for key in SIGNATURE_FIELDS}
            for row in sorted(segments, key=lambda row: row["ordinal"])
        ])
    values = []
    for row in sorted(segments, key=lambda row: row["ordinal"]):
        payload = {key: row[key] for key in SIGNATURE_FIELDS}
        stored = references.get(row["id"])
        if stored is not None:
            payload["_speech_reference"] = segment_speech_reference_identity(stored)
            payload["voice_id"] = stored.selector.get("voice_id")
            payload["voice"] = stored.selector.get("voice") or None
        values.append(payload)
    return stable_hash(values)


def speech_revision_digest(revision: dict[str, Any], segments: list[dict[str, Any]]) -> str:
    """Hash the finite copy root, independently from the editable review signature."""
    return stable_hash({
        "source_revision_id": revision["source_revision_id"],
        "settings_json": revision["settings_json"],
        "operation_json": revision["operation_json"],
        "segments": [
            {key: value for key, value in row.items()
             if key not in {"id", "plan_revision_id", "status", "revision"}}
            for row in sorted(segments, key=lambda row: row["ordinal"])
        ],
    })


def _known(rows: Mapping[str, Any]) -> dict[str, set[str]]:
    return {name: {row["id"] for row in values if "id" in row}
            for name, values in rows.items() if type(values) is list}


def _references(key: str, known: Mapping[str, set[str]]) -> set[str]:
    if "artifact" in key:
        families = {"artifacts"}
    elif "source_asset" in key:
        families = {"source_assets"}
    elif "session" in key:
        families = {"sessions"}
    elif "revision" in key:
        families = {"document_revisions", "generation_plan_revisions", "media_edit_plan_revisions"}
    elif "document" in key:
        families = {"documents"}
    elif "segment" in key:
        families = {"segments", "generation_segments"}
    elif "evidence" in key:
        families = {"subtitle_evidence"}
    elif "plan" in key:
        families = {"generation_plans", "media_edit_plans"}
    else:
        families = set()
    return set().union(*(known.get(name, set()) for name in families))


def _json(
    value: Any, *, area: str, known: Mapping[str, set[str]],
    identities: Mapping[str, str] | None = None, path: tuple[str, ...] = (),
) -> Any:
    _finite(value)
    if type(value) is list:
        return [_json(item, area=area, known=known, identities=identities, path=path) for item in value]
    if type(value) is not dict:
        return value
    if area == "settings_json" and not path and "provider_configs" in value:
        raise _invalid()
    if _secret_free(value) != value:
        raise _invalid()
    result = {}
    for key, item in value.items():
        if (area == "speech_plan_json" and not path and key in _HISTORY
                or area == "speech_block_provenance_json" and path == ("manual_topology",) and key in _HISTORY
                or area == "speech_block_provenance_json" and path == ("resegmentation",)
                and key in {"source_revision_id", "source_spans"}
                or path and path[-1] == "formation_events"
                and key in {"left_segment_id", "right_segment_id", "left_revision_id", "right_revision_id"}):
            continue
        semantic = key in _SEMANTIC or area == "speech_plan_json" and key in _UPSTREAM
        if semantic:
            result[key] = deepcopy(item)
            continue
        if (connection_key(key) or key in {"path", "paths", "job_id", "job_ids", "run_id", "run_ids"}
                or key.endswith(("_path", "_paths", "_job_id", "_job_ids", "_run_id", "_run_ids"))):
            if item not in (None, "", [], {}):
                raise _invalid()
        if _MANAGED.search(key) and key.endswith(("_id", "_ids")) and item not in (None, "", []):
            values = item if type(item) is list else [item]
            allowed = _references(key, known)
            if any(type(identifier) is not str or identifier not in allowed for identifier in values):
                raise _invalid()
            mapped = [identities[identifier] if identities is not None else identifier for identifier in values]
            result[key] = mapped if type(item) is list else mapped[0]
        else:
            result[key] = _json(item, area=area, known=known, identities=identities, path=(*path, key))
    return result


def _characters(rows: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    return {row["session_id"]: row["payload_json"]["characters"]
            for row in rows.get("knowledge_ledgers", []) if row["kind"] == "generation_controls"}


def _segment(row: dict[str, Any], *, known: Mapping[str, set[str]], characters: list[dict[str, Any]],
             identities: Mapping[str, str] | None = None, completed: set[str] | None = None,
             reference: StoredProjectSpeechReference | None = None) -> dict[str, Any]:
    _fields(row, SEGMENT_FIELDS)
    for key, value in row.items():
        if key in _BOOLS:
            valid = type(value) is bool
        elif key in _INTS:
            valid = type(value) is int and value >= (1 if key == "revision" else 0)
        elif key in _NULLABLE_TEXT:
            valid = value is None or type(value) is str
        elif key == "source_segment_ids_json":
            valid = type(value) is list and all(type(item) in {str, int} for item in value)
        elif key.endswith("_json"):
            valid = type(value) is dict
        else:
            valid = type(value) is str
        if not valid:
            raise _invalid()
    if not _identifier(row["id"]) or not _identifier(row["plan_revision_id"]):
        raise _invalid()
    if ((row["voice"] not in (None, "") or row["voice_id"] not in (None, ""))
            and reference is None):
        raise _invalid()
    if reference is not None and reference.slot_key != f"segment:{row['id']}":
        raise _invalid()
    limits = {"alignment_group": 64, "node_kind": 40, "speaker": 160,
              "optimization_status": 32, "optimization_source_hash": 128,
              "optimization_model": 255, "voice_id": 36, "voice": 255,
              "language": 40, "status": 32}
    if any(row[key] is not None and len(row[key]) > limit for key, limit in limits.items()):
        raise _invalid()
    result = deepcopy(row)
    if reference is not None:
        result["voice"] = None
        result["voice_id"] = None
    if identities is not None:
        result["id"] = identities[row["id"]]
        result["plan_revision_id"] = identities[row["plan_revision_id"]]
    for key in ("speech_block_provenance_json", "speech_plan_json"):
        result[key] = _json(row[key], area=key, known=known, identities=identities)
    xml = result["speech_plan_json"].get("speech_xml")
    if xml is not None:
        if type(xml) is not str:
            raise _invalid()
        parse_speech_markup(xml, expected_segment_id=row["id"],
                            expected_text=result["optimized_text"] or result["text"], characters=characters)
        result["speech_plan_json"]["speech_xml"] = remap_markup(
            xml, result["id"], result["optimized_text"] or result["text"], characters,
        )
    result.update(status="completed" if completed is not None and row["id"] in completed else "ready", revision=1)
    return result



def editorial_revision_settings(value: object) -> dict[str, Any]:
    """Detach editorial settings, excluding the known root routing snapshot."""
    if type(value) is not dict:
        raise _invalid()
    result = deepcopy(value)
    result.pop("provider_configs", None)
    return result


def freeze_speech_plan_rows(
    rows: Mapping[str, Any],
    settings: Mapping[str, dict[str, Any]],
    *,
    allow_segment_references: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Detach eligible current rows into one canonical copy root per plan."""
    result: dict[str, list[dict[str, Any]]] = {key: [] for key in SPEECH_FAMILIES}
    references = segment_copy_references(rows) if allow_segment_references else {}
    if not rows["generation_plans"]:
        return result
    known, characters = _known(rows), _characters(rows)
    completed = {row["generation_segment_id"] for row in rows.get("audio_takes", [])}
    _index(rows["generation_plans"], PLAN_FIELDS)
    revisions = _index(rows["generation_plan_revisions"], REVISION_FIELDS)
    _index(rows["generation_segments"], SEGMENT_FIELDS)
    reviews = _index(rows["speech_plan_reviews"], REVIEW_FIELDS, "revision_id")
    for revision in revisions.values():
        if (type(revision["settings_json"]) is not dict or type(revision["operation_json"]) is not dict
                or type(revision["revision_number"]) is not int or revision["revision_number"] < 1
                or revision["parent_revision_id"] is not None and not _identifier(revision["parent_revision_id"])):
            raise _invalid()
    for plan in rows["generation_plans"]:
        revision = revisions[plan["active_revision_id"]]
        segments = [row for row in rows["generation_segments"] if row["plan_revision_id"] == revision["id"]]
        source_hash = revision["content_hash"]
        if type(source_hash) is not str or not source_hash or len(source_hash) > 128:
            raise _invalid()
        _finite(source_hash)
        frozen = {**deepcopy(revision), "parent_revision_id": None, "revision_number": 1,
                  "settings_json": _json(settings[revision["id"]], area="settings_json", known=known),
                  "operation_json": {"action": "copy_current", "version": 1, "source_content_hash": source_hash}}
        canonical = [
            _segment(
                row,
                known=known,
                characters=characters.get(plan["session_id"], []),
                completed=completed,
                reference=references.get(row["id"]),
            )
            for row in segments
        ]
        frozen["content_hash"] = speech_revision_digest(frozen, canonical)
        result["generation_plans"].append(deepcopy(plan))
        result["generation_plan_revisions"].append(frozen)
        result["generation_segments"].extend(canonical)
        review = reviews.get(revision["id"])
        if review is not None and review["content_hash"] == speech_plan_signature(segments, references=references):
            result["speech_plan_reviews"].append({
                "revision_id": revision["id"],
                "content_hash": speech_plan_signature(canonical, references=references),
            })
    return result


def validate_speech_plan_graph(
    rows: Mapping[str, Any], *, allow_segment_references: bool = False
) -> None:
    """Prove closure, finite scalar/JSON types, ownership, XML and both digests."""
    references = segment_copy_references(rows) if allow_segment_references else {}
    plans = _index(rows.get("generation_plans", []), PLAN_FIELDS)
    revisions = _index(rows.get("generation_plan_revisions", []), REVISION_FIELDS)
    segments = _index(rows.get("generation_segments", []), SEGMENT_FIELDS)
    reviews = _index(rows.get("speech_plan_reviews", []), REVIEW_FIELDS, "revision_id")
    if not plans and not revisions and not segments and not reviews:
        return
    sessions = {row["id"]: row for row in rows["sessions"]}
    projects = {row["id"]: row for row in rows["projects"]}
    documents = {row["id"]: row for row in rows["documents"]}
    document_revisions = {row["id"]: row for row in rows["document_revisions"]}
    artifacts = {row["id"]: row for row in rows["artifacts"]}
    known, characters = _known(rows), _characters(rows)
    completed = {row["generation_segment_id"] for row in rows.get("audio_takes", [])}
    for plan in plans.values():
        if not all(_identifier(plan[key]) for key in PLAN_FIELDS):
            raise _invalid()
    for revision in revisions.values():
        if (not _identifier(revision["plan_id"])
                or revision["source_revision_id"] is not None and not _identifier(revision["source_revision_id"])):
            raise _invalid()
    for review in reviews.values():
        if type(review["content_hash"]) is not str or len(review["content_hash"]) > 128:
            raise _invalid()
    seen_sessions = set()
    if len(plans) != len(revisions) or set(revisions) != {plan["active_revision_id"] for plan in plans.values()}:
        raise _invalid()
    for plan in plans.values():
        sid = plan["session_id"]
        if sid not in sessions or sid in seen_sessions:
            raise _invalid()
        seen_sessions.add(sid)
        revision = revisions[plan["active_revision_id"]]
        if (revision["plan_id"] != plan["id"] or revision["parent_revision_id"] is not None
                or type(revision["revision_number"]) is not int or revision["revision_number"] != 1
                or type(revision["settings_json"]) is not dict or type(revision["operation_json"]) is not dict):
            raise _invalid()
        operation = revision["operation_json"]
        if (set(operation) != {"action", "version", "source_content_hash"}
                or operation["action"] != "copy_current" or type(operation["version"]) is not int
                or operation["version"] != 1 or type(operation["source_content_hash"]) is not str
                or not operation["source_content_hash"] or len(operation["source_content_hash"]) > 128):
            raise _invalid()
        source = revision["source_revision_id"]
        if source is not None and (source not in document_revisions or
                documents[document_revisions[source]["document_id"]]["session_id"] != sid):
            raise _invalid()
        settings = _json(revision["settings_json"], area="settings_json", known=known)
        if settings != revision["settings_json"]:
            raise _invalid()
        source_artifact = settings.get("_source_artifact_id")
        if source_artifact:
            artifact = artifacts[source_artifact]
            project = projects[sessions[sid]["project_id"]]
            if (artifact["workspace_id"], artifact["project_id"], artifact["session_id"]) != (project["workspace_id"], project["id"], sid):
                raise _invalid()
        selected = [row for row in segments.values() if row["plan_revision_id"] == revision["id"]]
        ordinals = set()
        for row in selected:
            if (_segment(
                    row,
                    known=known,
                    characters=characters.get(sid, []),
                    completed=completed,
                    reference=references.get(row["id"]),
            ) != row or row["ordinal"] in ordinals):
                raise _invalid()
            ordinals.add(row["ordinal"])
        if (type(revision["content_hash"]) is not str
                or revision["content_hash"] != speech_revision_digest(revision, selected)):
            raise _invalid()
        review = reviews.get(revision["id"])
        if review is not None and review["content_hash"] != speech_plan_signature(selected, references=references):
            raise _invalid()
    if any(row["plan_revision_id"] not in revisions for row in segments.values()) or not set(reviews) <= set(revisions):
        raise _invalid()


def remap_speech_plan_rows(
    rows: Mapping[str, Any],
    identities: Mapping[str, str],
    *,
    allow_segment_references: bool = False,
) -> dict[str, list[dict[str, Any]]]:
    """Rebind only managed graph identities; semantic labels and history roots stay finite."""
    result: dict[str, list[dict[str, Any]]] = {key: [] for key in SPEECH_FAMILIES}
    references = segment_copy_references(rows) if allow_segment_references else {}
    if not rows["generation_plans"]:
        return result
    known, characters = _known(rows), _characters(rows)
    completed = {row["generation_segment_id"] for row in rows.get("audio_takes", [])}
    session_by_revision = {plan["active_revision_id"]: plan["session_id"] for plan in rows["generation_plans"]}
    mapped_references: dict[str, StoredProjectSpeechReference] = {}
    for segment_id, reference in references.items():
        if segment_id not in identities:
            raise _invalid()
        mapped_id = identities[segment_id]
        if type(mapped_id) is not str or not mapped_id or mapped_id in mapped_references:
            raise _invalid()
        mapped_references[mapped_id] = reference
    for plan in rows["generation_plans"]:
        result["generation_plans"].append({key: identities[value] for key, value in plan.items()})
    for row in rows["generation_segments"]:
        result["generation_segments"].append(_segment(row, known=known, identities=identities,
            characters=characters.get(session_by_revision[row["plan_revision_id"]], []), completed=completed,
            reference=references.get(row["id"])))
    for row in rows["generation_plan_revisions"]:
        revision = {**deepcopy(row), "id": identities[row["id"]], "plan_id": identities[row["plan_id"]],
                    "source_revision_id": identities[row["source_revision_id"]] if row["source_revision_id"] is not None else None,
                    "settings_json": _json(row["settings_json"], area="settings_json", known=known, identities=identities)}
        selected = [segment for segment in result["generation_segments"] if segment["plan_revision_id"] == revision["id"]]
        revision["content_hash"] = speech_revision_digest(revision, selected)
        result["generation_plan_revisions"].append(revision)
    for row in rows["speech_plan_reviews"]:
        revision_id = identities[row["revision_id"]]
        selected = [segment for segment in result["generation_segments"] if segment["plan_revision_id"] == revision_id]
        result["speech_plan_reviews"].append({
            "revision_id": revision_id,
            "content_hash": speech_plan_signature(selected, references=mapped_references),
        })
    return result
