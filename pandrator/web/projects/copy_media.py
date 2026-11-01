"""Pure projection, validation, and rebinding for copied media-edit state."""

from __future__ import annotations

import hashlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..settings_policy import _secret_free

MAX_MEDIA_EDIT_PLANS = 101
MAX_MEDIA_EDIT_REVISIONS = 1_000
MAX_MEDIA_PAYLOAD_ROWS = 250_000

PLAN_FIELDS = frozenset({"id", "session_id", "active_revision_id"})
REVISION_FIELDS = frozenset(
    {
        "id",
        "plan_id",
        "parent_revision_id",
        "revision_number",
        "source_media_artifact_id",
        "editorial_transcript_artifact_id",
        "timing_artifact_id",
        "duration_ms",
        "instructions",
        "keep_ranges_json",
        "cues_json",
        "evidence_json",
        "operation_json",
        "reviewed",
        "content_hash",
    }
)
_RANGE_FIELDS = frozenset({"id", "start_ms", "end_ms", "label"})
_CUE_FIELDS = frozenset(
    {
        "id",
        "start_ms",
        "end_ms",
        "text",
        "speaker",
        "words",
        "timing_confidence",
        "timing_source",
    }
)
_WORD_FIELDS = frozenset({"text", "start_ms", "end_ms", "confidence"})
_HASH = re.compile(r"[0-9a-f]{64}\Z", re.ASCII)
_REFERENCE_FAMILIES = re.compile(
    r"(?:^|_)(?:artifact|session|document|revision|segment|source_asset|evidence|plan)(?:_|$)"
)
_EXCLUDED = frozenset(
    {
        "provider_id",
        "provider",
        "connection_id",
        "base_url",
        "api_base",
        "endpoint",
        "external_server_url",
        "secret_ref",
        "credential_ref",
        "services",
        "tts_services",
        "service_configs",
        "providers",
        "endpoints",
        "path",
        "paths",
        "job_id",
        "job_ids",
        "run_id",
        "run_ids",
    }
)
_LOCAL_REFERENCE_KEYS = frozenset(
    {
        "cue_id",
        "cue_ids",
        "start_cue_id",
        "end_cue_id",
        "source_cue_id",
        "adjacent_keep_range_id",
        "keep_range_id",
        "keep_range_ids",
        "range_id",
        "range_ids",
        "cut_id",
    }
)


def _invalid() -> ValueError:
    return ValueError("Invalid saved media-edit copy graph.")


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value)


def _integer(value: Any, *, minimum: int = 0) -> bool:
    return type(value) is int and value >= minimum


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _json_value(value: Any) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _invalid()
        return
    if type(value) is list:
        for item in value:
            _json_value(item)
        return
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise _invalid()
        for item in value.values():
            _json_value(item)
        return
    raise _invalid()


def revision_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    """Build MediaEditService's exact revision hash payload from row fields."""

    return {
        "source_media_artifact_id": row["source_media_artifact_id"],
        "editorial_transcript_artifact_id": row["editorial_transcript_artifact_id"],
        "timing_artifact_id": row["timing_artifact_id"],
        "duration_ms": row["duration_ms"],
        "instructions": row["instructions"],
        "keep_ranges": list(row["keep_ranges_json"]),
        "cues": list(row["cues_json"]),
        "evidence": dict(row["evidence_json"]),
        "operation": dict(row["operation_json"]),
        "reviewed": bool(row["reviewed"]),
    }


def revision_hash(row: Mapping[str, Any]) -> str:
    """Return the service-compatible canonical media-edit content hash."""

    payload = revision_payload(row)
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _validate_range(value: Any, duration_ms: int) -> None:
    if type(value) is not dict or set(value) != _RANGE_FIELDS:
        raise _invalid()
    if not _identifier(value["id"]):
        raise _invalid()
    if not _integer(value["start_ms"]) or not _integer(value["end_ms"]):
        raise _invalid()
    if not (value["start_ms"] < value["end_ms"] <= duration_ms):
        raise _invalid()
    if value["label"] is not None and type(value["label"]) is not str:
        raise _invalid()


def _validate_word(value: Any) -> None:
    if type(value) is not dict or set(value) != _WORD_FIELDS:
        raise _invalid()
    if type(value["text"]) is not str:
        raise _invalid()
    if not _integer(value["start_ms"]) or not _integer(value["end_ms"]):
        raise _invalid()
    if value["end_ms"] <= value["start_ms"]:
        raise _invalid()
    confidence = value["confidence"]
    if confidence is not None and (
        not _number(confidence) or not 0 <= confidence <= 1
    ):
        raise _invalid()


def _validate_cue(value: Any) -> int:
    if type(value) is not dict or set(value) != _CUE_FIELDS:
        raise _invalid()
    if not _identifier(value["id"]) or type(value["text"]) is not str:
        raise _invalid()
    if not _integer(value["start_ms"]) or not _integer(value["end_ms"]):
        raise _invalid()
    if value["end_ms"] <= value["start_ms"]:
        raise _invalid()
    if value["speaker"] is not None and type(value["speaker"]) is not str:
        raise _invalid()
    confidence = value["timing_confidence"]
    if confidence is not None and (
        not _number(confidence) or not 0 <= confidence <= 1
    ):
        raise _invalid()
    if type(value["timing_source"]) is not str or not value["timing_source"]:
        raise _invalid()
    words = value["words"]
    if type(words) is not list:
        raise _invalid()
    for word in words:
        _validate_word(word)
    return len(words)


def validate_revision(row: Mapping[str, Any], *, frozen: bool) -> int:
    """Validate one source or frozen revision and return its range/cue/word count."""

    if type(row) is not dict or set(row) != REVISION_FIELDS:
        raise _invalid()
    for key in ("id", "plan_id", "source_media_artifact_id", "editorial_transcript_artifact_id"):
        if not _identifier(row[key]):
            raise _invalid()
    for key in ("parent_revision_id", "timing_artifact_id"):
        if row[key] is not None and not _identifier(row[key]):
            raise _invalid()
    if not _integer(row["revision_number"], minimum=1):
        raise _invalid()
    if not _integer(row["duration_ms"], minimum=1):
        raise _invalid()
    if type(row["instructions"]) is not str or type(row["reviewed"]) is not bool:
        raise _invalid()
    ranges = row["keep_ranges_json"]
    cues = row["cues_json"]
    evidence = row["evidence_json"]
    operation = row["operation_json"]
    if type(ranges) is not list or not ranges:
        raise _invalid()
    if type(cues) is not list or type(evidence) is not dict or type(operation) is not dict:
        raise _invalid()
    previous_end = -1
    for item in ranges:
        _validate_range(item, row["duration_ms"])
        if item["start_ms"] < previous_end:
            raise _invalid()
        previous_end = item["end_ms"]
    count = len(ranges)
    for cue in cues:
        count += 1 + _validate_cue(cue)
    _json_value(ranges)
    _json_value(cues)
    _json_value(evidence)
    _json_value(operation)
    try:
        source_hash = revision_hash(row)
    except (KeyError, TypeError, ValueError, UnicodeError) as error:
        raise _invalid() from error
    if type(row["content_hash"]) is not str or _HASH.fullmatch(row["content_hash"]) is None:
        raise _invalid()
    if not frozen and source_hash != row["content_hash"]:
        raise _invalid()
    if frozen:
        if source_hash != row["content_hash"] or "passive_dispatch" in evidence:
            raise _invalid()
        if _secret_free(ranges) != ranges or _secret_free(cues) != cues:
            raise _invalid()
        if _secret_free(evidence) != evidence or _secret_free(operation) != operation:
            raise _invalid()
    return count


def freeze_revision(row: Mapping[str, Any], selected_revision_ids: set[str]) -> dict[str, Any]:
    """Project a verified source revision, dropping only passive dispatch state."""

    result = {key: deepcopy(row[key]) for key in REVISION_FIELDS}
    validate_revision(result, frozen=False)
    for key in ("keep_ranges_json", "cues_json", "operation_json"):
        if _secret_free(result[key]) != result[key]:
            raise _invalid()
    evidence = dict(result["evidence_json"])
    evidence.pop("passive_dispatch", None)
    if _secret_free(evidence) != evidence:
        raise _invalid()
    result["evidence_json"] = evidence
    if result["parent_revision_id"] not in selected_revision_ids:
        result["parent_revision_id"] = None
    result["content_hash"] = revision_hash(result)
    validate_revision(result, frozen=True)
    return result


def _uuid_like(value: str) -> bool:
    try:
        uuid.UUID(value)
    except (ValueError, AttributeError):
        return False
    return True


def _excluded(key: str) -> bool:
    normalized = key.lower()
    return (
        normalized in _EXCLUDED
        or normalized.endswith(("_secret_ref", "_credential_ref", "_base_url", "_endpoint"))
        or normalized.endswith(
            (
                "_path",
                "_paths",
                "_job_id",
                "_job_ids",
                "_run_id",
                "_run_ids",
            )
        )
    )


def _local_reference_key(key: str) -> bool:
    normalized = key.lower()
    return normalized in _LOCAL_REFERENCE_KEYS or (
        normalized.endswith(("_id", "_ids"))
        and any(part in normalized for part in ("cue", "range", "cut"))
    )


def _remap_json(
    value: Any,
    identities: Mapping[str, str],
    *,
    key: str = "",
    local_scope: bool = False,
) -> Any:
    if key in {"cues", "words", "keep_ranges", "ranges", "cuts"}:
        local_scope = True
    if isinstance(value, str):
        local = _local_reference_key(key) or (local_scope and key == "id")
        if local:
            return value
        reference = key.endswith(("_id", "_ids"))
        managed = _REFERENCE_FAMILIES.search(key) is not None
        if reference and value in identities:
            return identities[value]
        if reference and managed and value and _uuid_like(value):
            raise _invalid()
        return value
    if type(value) is list:
        return [
            _remap_json(item, identities, key=key, local_scope=local_scope)
            for item in value
        ]
    if type(value) is dict:
        result = {}
        for child_key, item in value.items():
            if _excluded(child_key) or (key == "evidence_json" and child_key == "passive_dispatch"):
                continue
            mapped_key = child_key
            if key.endswith("_id") and child_key in identities:
                mapped_key = identities[child_key]
            elif key.endswith("_id") and _REFERENCE_FAMILIES.search(key) and _uuid_like(child_key):
                raise _invalid()
            result[mapped_key] = _remap_json(
                item, identities, key=child_key, local_scope=local_scope
            )
        return result
    return value


def remap_revision(
    row: Mapping[str, Any],
    identities: Mapping[str, str],
    selected_revision_ids: set[str],
) -> dict[str, Any]:
    """Remap managed media row references without rewriting local timeline IDs."""

    validate_revision(row, frozen=True)
    result = {key: deepcopy(row[key]) for key in REVISION_FIELDS}
    for key in (
        "id",
        "plan_id",
        "source_media_artifact_id",
        "editorial_transcript_artifact_id",
        "timing_artifact_id",
    ):
        old_id = result[key]
        if old_id is not None:
            mapped = identities.get(old_id)
            if mapped is None:
                raise _invalid()
            result[key] = mapped
    parent_id = result["parent_revision_id"]
    if parent_id is not None:
        if parent_id not in selected_revision_ids or parent_id not in identities:
            result["parent_revision_id"] = None
        else:
            result["parent_revision_id"] = identities[parent_id]
    result["evidence_json"] = _remap_json(result["evidence_json"], identities, key="evidence_json")
    result["operation_json"] = _remap_json(result["operation_json"], identities, key="operation_json")
    if _secret_free(result["evidence_json"]) != result["evidence_json"]:
        raise _invalid()
    if _secret_free(result["operation_json"]) != result["operation_json"]:
        raise _invalid()
    result["content_hash"] = revision_hash(result)
    validate_revision(result, frozen=True)
    return result


def validate_media_graph(
    rows: Mapping[str, list[dict[str, Any]]],
    *,
    branch_source_sessions: Mapping[str, str],
    document_revision_ids: set[str] | None = None,
    source: bool = False,
) -> None:
    """Validate frozen plans, selected revisions, pins, hashes, and artifact pairs."""

    plans = rows.get("media_edit_plans")
    revisions = rows.get("media_edit_plan_revisions")
    sessions = {row["id"]: row for row in rows.get("sessions", [])}
    artifacts = {row["id"]: row for row in rows.get("artifacts", [])}
    translations = rows.get("translation_projects", [])
    branches = rows.get("translation_project_branches", [])
    if type(plans) is not list or type(revisions) is not list:
        raise _invalid()
    if len(plans) > MAX_MEDIA_EDIT_PLANS or len(revisions) > MAX_MEDIA_EDIT_REVISIONS:
        raise _invalid()

    plan_by_id: dict[str, dict[str, Any]] = {}
    plan_by_session: dict[str, dict[str, Any]] = {}
    for row in plans:
        if type(row) is not dict or set(row) != PLAN_FIELDS:
            raise _invalid()
        if not _identifier(row["id"]) or not _identifier(row["session_id"]):
            raise _invalid()
        if row["active_revision_id"] is not None and not _identifier(row["active_revision_id"]):
            raise _invalid()
        if row["id"] in plan_by_id or row["session_id"] in plan_by_session:
            raise _invalid()
        if row["session_id"] not in sessions:
            raise _invalid()
        if sessions[row["session_id"]].get("workflow_kind") not in {"subtitles", "media_edit"}:
            raise _invalid()
        plan_by_id[row["id"]] = row
        plan_by_session[row["session_id"]] = row

    revision_by_id: dict[str, dict[str, Any]] = {}
    numbers: set[tuple[str, int]] = set()
    total_rows = 0
    for row in revisions:
        total_rows += validate_revision(row, frozen=not source)
        if row["id"] in revision_by_id:
            raise _invalid()
        plan = plan_by_id.get(row["plan_id"])
        if plan is None:
            raise _invalid()
        number_key = (row["plan_id"], row["revision_number"])
        if number_key in numbers:
            raise _invalid()
        numbers.add(number_key)
        for key in (
            "source_media_artifact_id",
            "editorial_transcript_artifact_id",
            "timing_artifact_id",
        ):
            if row[key] is not None and row[key] not in artifacts:
                raise _invalid()
        revision_by_id[row["id"]] = row
    if total_rows > MAX_MEDIA_PAYLOAD_ROWS:
        raise _invalid()

    for row in revisions:
        parent_id = row["parent_revision_id"]
        if parent_id is not None and parent_id in revision_by_id:
            if revision_by_id[parent_id]["plan_id"] != row["plan_id"]:
                raise _invalid()
        elif parent_id is not None and not source:
            raise _invalid()
    for plan in plans:
        active_id = plan["active_revision_id"]
        if active_id is not None:
            active = revision_by_id.get(active_id)
            if active is None or active["plan_id"] != plan["id"]:
                raise _invalid()

    branch_sources: dict[str, str] = {}
    for branch in branches:
        if len(translations) != 1 or branch["session_id"] in branch_sources:
            raise _invalid()
        branch_sources[branch["session_id"]] = translations[0]["source_session_id"]
    if dict(branch_source_sessions) != branch_sources:
        raise _invalid()

    def resolve_pin(artifact: Mapping[str, Any], metadata: Mapping[str, Any]) -> dict[str, Any] | None:
        artifact_session = artifact.get("session_id")
        role = artifact.get("role")
        media_role = type(role) is str and role.startswith("media_edit_")
        candidates: list[dict[str, Any]] = []
        dedicated_id = metadata.get("media_edit_revision_id")
        if dedicated_id is not None:
            if not _identifier(dedicated_id) or dedicated_id not in revision_by_id:
                raise _invalid()
            candidates.append(revision_by_id[dedicated_id])
        generic_id = metadata.get("revision_id")
        if generic_id is not None:
            if not _identifier(generic_id):
                raise _invalid()
            if generic_id in revision_by_id:
                candidates.append(revision_by_id[generic_id])
            elif dedicated_id is not None and document_revision_ids is not None and generic_id not in document_revision_ids:
                raise _invalid()
        plan_id = metadata.get("plan_id")
        revision_number = metadata.get("revision", metadata.get("revision_number"))
        if media_role and (
            not _identifier(plan_id)
            or type(revision_number) is not int
            or type(metadata.get("content_hash")) is not str
        ):
            raise _invalid()
        if plan_id is not None and revision_number is not None and not candidates:
            if not _identifier(plan_id) or type(revision_number) is not int:
                raise _invalid()
            match = next(
                (
                    row
                    for row in revision_by_id.values()
                    if row["plan_id"] == plan_id and row["revision_number"] == revision_number
                ),
                None,
            )
            if match is not None:
                candidates.append(match)
            elif plan_id in plan_by_id:
                raise _invalid()
        if not candidates:
            if media_role and (plan_id is not None or dedicated_id is not None):
                raise _invalid()
            return None
        if any(item["id"] != candidates[0]["id"] for item in candidates[1:]):
            raise _invalid()
        revision = candidates[0]
        plan = plan_by_id[revision["plan_id"]]
        if type(artifact_session) is not str:
            raise _invalid()
        if (
            artifact_session != plan["session_id"]
            and branch_sources.get(artifact_session) != plan["session_id"]
        ):
            raise _invalid()
        if plan_id is not None and plan_id != revision["plan_id"]:
            raise _invalid()
        for key in ("revision", "revision_number"):
            if key in metadata and metadata[key] is not None:
                if type(metadata[key]) is not int or metadata[key] != revision["revision_number"]:
                    raise _invalid()
        content_hash = metadata.get("content_hash")
        if content_hash is not None and content_hash != revision["content_hash"]:
            raise _invalid()
        role = artifact.get("role")
        if (
            type(role) is str
            and role in {"media_edit_media", "media_edit_subtitles"}
            and "content_hash" not in metadata
        ):
            raise _invalid()
        return revision

    document_ids = document_revision_ids or set()
    for artifact in artifacts.values():
        metadata = artifact.get("metadata_json")
        if type(metadata) is not dict:
            raise _invalid()
        pin = resolve_pin(artifact, metadata)
        if pin is None:
            continue
        generic_id = metadata.get("revision_id")
        if (
            generic_id is not None
            and generic_id not in revision_by_id
            and generic_id not in document_ids
        ):
            raise _invalid()

    for translation in translations:
        revision_id = translation.get("source_media_edit_revision_id")
        content_hash = translation.get("source_media_edit_content_hash")
        if (revision_id is None) != (content_hash is None):
            raise _invalid()
        if revision_id is None:
            continue
        if not _identifier(revision_id) or not _identifier(translation.get("source_session_id")):
            raise _invalid()
        revision = revision_by_id.get(revision_id)
        plan = plan_by_id.get(revision["plan_id"]) if revision else None
        if (
            revision is None
            or plan is None
            or plan["session_id"] != translation["source_session_id"]
            or content_hash != revision["content_hash"]
        ):
            raise _invalid()


def rebind_capture_metadata(rows: Mapping[str, list[dict[str, Any]]]) -> None:
    """Update paired frozen registry/translation hashes after passive-data removal."""

    revisions = {row["id"]: row for row in rows["media_edit_plan_revisions"]}
    plans = {row["id"]: row for row in rows["media_edit_plans"]}
    numbers = {
        (row["plan_id"], row["revision_number"]): row for row in revisions.values()
    }
    for artifact in rows["artifacts"]:
        metadata = artifact.get("metadata_json")
        if type(metadata) is not dict:
            continue
        pin_id = metadata.get("media_edit_revision_id")
        if pin_id not in revisions:
            generic_id = metadata.get("revision_id")
            pin_id = generic_id if generic_id in revisions else None
        if pin_id is None and metadata.get("plan_id") in plans:
            number = metadata.get("revision", metadata.get("revision_number"))
            if type(number) is int:
                revision = numbers.get((metadata["plan_id"], number))
                pin_id = revision["id"] if revision is not None else None
        if pin_id is not None and "content_hash" in metadata:
            metadata["content_hash"] = revisions[pin_id]["content_hash"]
    for translation in rows["translation_projects"]:
        revision_id = translation.get("source_media_edit_revision_id")
        if revision_id in revisions:
            translation["source_media_edit_content_hash"] = revisions[revision_id]["content_hash"]


def rebind_published_metadata(
    source_rows: Mapping[str, list[dict[str, Any]]],
    prepared_rows: Mapping[str, list[dict[str, Any]]],
    identities: Mapping[str, str],
) -> None:
    """Rebind only paired media hashes after destination IDs and scrub fields change."""

    revisions = {row["id"]: row for row in prepared_rows["media_edit_plan_revisions"]}
    source_revision_ids = {
        row["id"]: row for row in source_rows["media_edit_plan_revisions"]
    }
    old_to_new_revision = {
        old_id: revisions[identities[old_id]] for old_id in source_revision_ids
    }
    source_artifacts = {row["id"]: row for row in source_rows["artifacts"]}
    prepared_artifacts = {row["id"]: row for row in prepared_rows["artifacts"]}
    for old_artifact_id, artifact in source_artifacts.items():
        prepared = prepared_artifacts.get(identities[old_artifact_id])
        if prepared is None:
            continue
        metadata = artifact.get("metadata_json")
        prepared_metadata = prepared.get("metadata_json")
        if type(metadata) is not dict or type(prepared_metadata) is not dict:
            continue
        pin_id = metadata.get("media_edit_revision_id")
        if pin_id not in old_to_new_revision:
            generic_id = metadata.get("revision_id")
            pin_id = generic_id if generic_id in old_to_new_revision else None
        if pin_id is None and metadata.get("plan_id") is not None:
            revision_number = metadata.get("revision", metadata.get("revision_number"))
            if type(revision_number) is int:
                pin_id = next(
                    (
                        old_id
                        for old_id, old_row in source_revision_ids.items()
                        if old_row["plan_id"] == metadata["plan_id"]
                        and old_row["revision_number"] == revision_number
                    ),
                    None,
                )
        if pin_id is not None and "content_hash" in prepared_metadata:
            prepared_metadata["content_hash"] = old_to_new_revision[pin_id]["content_hash"]
    prepared_translations = {row["id"]: row for row in prepared_rows["translation_projects"]}
    for source in source_rows["translation_projects"]:
        prepared = prepared_translations[identities[source["id"]]]
        pin_id = source.get("source_media_edit_revision_id")
        if pin_id in old_to_new_revision:
            prepared["source_media_edit_content_hash"] = old_to_new_revision[pin_id][
                "content_hash"
            ]
