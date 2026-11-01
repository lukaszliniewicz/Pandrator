"""Pure projection, validation, dependency, and identity helpers for copied evidence."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

EVIDENCE_ROUTES = frozenset(
    {
        "whisper",
        "parakeet",
        "moss",
        "qwen3",
        "azure_mai_transcribe_1_5",
        "azure_mai_transcribe_2",
        "audio_llm",
    }
)
EVIDENCE_STATUSES = frozenset({"completed", "failed", "resolved", "uncertain", "dismissed"})
ACTIVE_EVIDENCE_STATUSES = frozenset({"queued", "running"})
RESOLUTION_ACTIONS = frozenset({"accepted", "edited", "deleted", "uncertain", "dismissed"})
EVIDENCE_FIELDS = frozenset(
    {
        "id",
        "session_id",
        "source_artifact_id",
        "source_media_artifact_id",
        "source_revision_id",
        "source_segment_id",
        "cue_id",
        "start_ms",
        "end_ms",
        "clip_start_ms",
        "clip_end_ms",
        "reason",
        "routes_json",
        "status",
        "clip_artifact_id",
        "candidates_json",
        "resolution_json",
    }
)
_CANDIDATE_FIELDS = frozenset(
    {
        "id",
        "route",
        "status",
        "text",
        "context_text",
        "selection_method",
        "language",
        "timing_kind",
        "timing_method",
        "provider",
        "model",
        "engine",
        "compute_backend",
        "segments",
        "words",
        "transport",
        "transcript_artifact_id",
        "error",
    }
)
_DISPLAY_FIELDS = frozenset(
    {
        "text",
        "context_text",
        "selection_method",
        "language",
        "timing_kind",
        "timing_method",
        "provider",
        "model",
        "engine",
        "compute_backend",
    }
)
_TRANSPORT_FIELDS = frozenset(
    {"input_contract", "provider_wire_mapping", "audio_consumption"}
)
_RESOLUTION_FIELDS = frozenset({"action", "candidate_id", "text", "note"})
_SEGMENT_FIELDS = frozenset({"id", "start_ms", "end_ms", "text", "speaker", "words"})
_WORD_FIELDS = frozenset({"text", "start_ms", "end_ms", "speaker", "confidence"})
_ARTIFACT_ROLES = {"subtitle_evidence_audio", "subtitle_evidence_transcript"}
_FIXED_FAILURE = "Evidence route failed."


def _invalid() -> ValueError:
    return ValueError("Invalid retained subtitle evidence graph.")


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value)


def _finite(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except (OverflowError, TypeError, ValueError):
        return False


def _number(value: Any) -> None:
    if not _finite(value):
        raise _invalid()


def _evidence_ids(value: Any, target: list[str]) -> None:
    if type(value) is not list or any(not _identifier(item) for item in value):
        raise _invalid()
    target.extend(value)


def _logical_passage_items(metadata: Mapping[str, Any]) -> list[dict[str, Any]] | None:
    packet = metadata.get("logical_passages")
    if packet is None:
        return None
    if type(packet) is not dict:
        raise _invalid()
    if "items" not in packet:
        return None
    items = packet["items"]
    if type(items) is not list or any(type(item) is not dict for item in items):
        raise _invalid()
    return items


def _metadata_references(metadata: Any) -> list[str]:
    if type(metadata) is not dict:
        raise _invalid()
    result: list[str] = []
    if "evidence_ids" in metadata:
        _evidence_ids(metadata["evidence_ids"], result)
    for item in _logical_passage_items(metadata) or []:
        if "evidence_ids" in item:
            _evidence_ids(item["evidence_ids"], result)
    return result


def referenced_evidence_ids(
    segments: Iterable[Mapping[str, Any]], artifacts: Iterable[Mapping[str, Any]]
) -> tuple[str, ...]:
    """Return first-seen evidence IDs referenced by retained editorial metadata."""

    found: dict[str, None] = {}
    for row in segments:
        for evidence_id in _metadata_references(row.get("metadata_json")):
            found.setdefault(evidence_id, None)
    for row in artifacts:
        metadata = row.get("metadata_json")
        if type(metadata) is not dict:
            raise _invalid()
        for item in _logical_passage_items(metadata) or []:
            if "evidence_ids" in item:
                values: list[str] = []
                _evidence_ids(item["evidence_ids"], values)
                for evidence_id in values:
                    found.setdefault(evidence_id, None)
    return tuple(found)


def evidence_reference_sessions(
    segments: Iterable[Mapping[str, Any]],
    artifacts: Iterable[Mapping[str, Any]],
    revision_sessions: Mapping[str, str],
) -> dict[str, set[str | None]]:
    """Return metadata owners for each reference without loading evidence rows."""

    owners: dict[str, set[str | None]] = {}
    for row in segments:
        revision_id = row.get("revision_id")
        if type(revision_id) is not str or revision_id not in revision_sessions:
            raise _invalid()
        session_id = revision_sessions[revision_id]
        for evidence_id in _metadata_references(row.get("metadata_json")):
            owners.setdefault(evidence_id, set()).add(session_id)
    for row in artifacts:
        metadata = row.get("metadata_json")
        if type(metadata) is not dict:
            raise _invalid()
        session_id = row.get("session_id")
        for item in _logical_passage_items(metadata) or []:
            if "evidence_ids" not in item:
                continue
            values: list[str] = []
            _evidence_ids(item["evidence_ids"], values)
            for evidence_id in values:
                owners.setdefault(evidence_id, set()).add(session_id)
    return owners


def _project_word(value: Any, *, strict: bool) -> dict[str, Any]:
    if type(value) is not dict or (strict and set(value) != _WORD_FIELDS):
        raise _invalid()
    if not {"text", "start_ms", "end_ms"} <= set(value):
        raise _invalid()
    word = {key: value.get(key) for key in _WORD_FIELDS}
    if type(word["text"]) is not str:
        raise _invalid()
    _number(word["start_ms"])
    _number(word["end_ms"])
    if word["speaker"] is not None and type(word["speaker"]) is not str:
        raise _invalid()
    if word["confidence"] is not None:
        _number(word["confidence"])
    return word


def _project_segment(value: Any, *, strict: bool) -> dict[str, Any]:
    if type(value) is not dict or (strict and set(value) != _SEGMENT_FIELDS):
        raise _invalid()
    if not {"text", "start_ms", "end_ms"} <= set(value):
        raise _invalid()
    segment = {key: value.get(key) for key in _SEGMENT_FIELDS}
    if segment["words"] is None and "words" not in value:
        segment["words"] = []
    if segment["id"] is not None and not _identifier(segment["id"]):
        raise _invalid()
    _number(segment["start_ms"])
    _number(segment["end_ms"])
    if type(segment["text"]) is not str:
        raise _invalid()
    if segment["speaker"] is not None and type(segment["speaker"]) is not str:
        raise _invalid()
    if type(segment["words"]) is not list:
        raise _invalid()
    segment["words"] = [_project_word(item, strict=strict) for item in segment["words"]]
    return segment


def _project_transport(value: Any, *, strict: bool) -> dict[str, str]:
    if type(value) is not dict or (strict and set(value) != _TRANSPORT_FIELDS):
        raise _invalid()
    if not _TRANSPORT_FIELDS <= set(value):
        raise _invalid()
    result = {key: value[key] for key in _TRANSPORT_FIELDS}
    if any(type(item) is not str for item in result.values()):
        raise _invalid()
    return result


def _project_candidate(value: Any, *, strict: bool) -> dict[str, Any]:
    if type(value) is not dict or (strict and not set(value) <= _CANDIDATE_FIELDS):
        raise _invalid()
    if not {"id", "route", "status"} <= set(value):
        raise _invalid()
    candidate: dict[str, Any] = {
        key: item for key, item in value.items() if key in _CANDIDATE_FIELDS
    }
    if not _identifier(candidate["id"]):
        raise _invalid()
    route, status = candidate["route"], candidate["status"]
    if type(route) is not str or route not in EVIDENCE_ROUTES:
        raise _invalid()
    if type(status) is not str or status not in {"success", "failed"}:
        raise _invalid()
    for key in _DISPLAY_FIELDS & set(candidate):
        item = candidate[key]
        if item is not None and type(item) is not str:
            raise _invalid()
    if "transcript_artifact_id" in candidate:
        item = candidate["transcript_artifact_id"]
        if item is not None and not _identifier(item):
            raise _invalid()
    for key in ("segments", "words"):
        if key in candidate:
            if type(candidate[key]) is not list:
                raise _invalid()
            if key == "segments":
                candidate[key] = [
                    _project_segment(item, strict=strict) for item in candidate[key]
                ]
            else:
                candidate[key] = [_project_word(item, strict=strict) for item in candidate[key]]
    if "transport" in candidate:
        candidate["transport"] = _project_transport(candidate["transport"], strict=strict)
    if status == "success":
        candidate.pop("error", None)
        if type(candidate.get("text")) is not str:
            raise _invalid()
    else:
        if "error" in value and strict and value["error"] != _FIXED_FAILURE:
            raise _invalid()
        candidate["error"] = _FIXED_FAILURE
    return candidate


def _project_resolution(value: Any, *, strict: bool) -> dict[str, Any]:
    if type(value) is not dict or (strict and not set(value) <= _RESOLUTION_FIELDS):
        raise _invalid()
    result = {key: item for key, item in value.items() if key in _RESOLUTION_FIELDS}
    if not result:
        return result
    action = result.get("action")
    if type(action) is not str or action not in RESOLUTION_ACTIONS:
        raise _invalid()
    if "candidate_id" in result and not _identifier(result["candidate_id"]):
        raise _invalid()
    if "text" in result and type(result["text"]) is not str:
        raise _invalid()
    if "note" in result and type(result["note"]) is not str:
        raise _invalid()
    return result


def _validate_resolution(status: str, resolution: Mapping[str, Any], candidates: list[dict[str, Any]]) -> None:
    if not resolution:
        if status not in {"completed", "failed"}:
            raise _invalid()
        return
    action = resolution["action"]
    candidate_id = resolution.get("candidate_id")
    text = resolution.get("text")
    note = resolution.get("note")
    if action == "accepted":
        selected = next((item for item in candidates if item["id"] == candidate_id), None)
        if selected is None or selected["status"] != "success" or text is not None:
            raise _invalid()
    elif candidate_id is not None:
        raise _invalid()
    if action == "edited":
        if type(text) is not str or not text.strip():
            raise _invalid()
    elif text is not None:
        raise _invalid()
    if action == "uncertain" and (type(note) is not str or not note.strip()):
        raise _invalid()
    expected = {
        "resolved": {"accepted", "edited", "deleted"},
        "uncertain": {"uncertain"},
        "dismissed": {"dismissed"},
    }
    if status not in expected or action not in expected[status]:
        raise _invalid()


def sanitize_evidence(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project one persisted evidence row into its bounded editorial form."""

    if not isinstance(row, Mapping) or not EVIDENCE_FIELDS <= set(row):
        raise _invalid()
    result = {key: row[key] for key in EVIDENCE_FIELDS}
    for key in (
        "id",
        "session_id",
        "source_artifact_id",
        "source_revision_id",
    ):
        if not _identifier(result[key]):
            raise _invalid()
    for key in ("source_media_artifact_id", "source_segment_id", "clip_artifact_id"):
        if result[key] is not None and not _identifier(result[key]):
            raise _invalid()
    if type(result["cue_id"]) is not int or result["cue_id"] < 1:
        raise _invalid()
    for key in ("start_ms", "end_ms", "clip_start_ms", "clip_end_ms"):
        if type(result[key]) is not int:
            raise _invalid()
    if not (
        0 <= result["clip_start_ms"] <= result["start_ms"] < result["end_ms"]
        <= result["clip_end_ms"]
    ):
        raise _invalid()
    if type(result["reason"]) is not str or not result["reason"].strip() or len(result["reason"]) > 4000:
        raise _invalid()
    routes = result["routes_json"]
    if (
        type(routes) is not list
        or not routes
        or len(routes) > len(EVIDENCE_ROUTES)
        or any(type(route) is not str or route not in EVIDENCE_ROUTES for route in routes)
        or len(set(routes)) != len(routes)
    ):
        raise _invalid()
    status = result["status"]
    if type(status) is not str or status not in EVIDENCE_STATUSES:
        raise _invalid()
    if type(result["candidates_json"]) is not list or type(result["resolution_json"]) is not dict:
        raise _invalid()
    candidates = [
        _project_candidate(candidate, strict=False) for candidate in result["candidates_json"]
    ]
    candidate_ids = [candidate["id"] for candidate in candidates]
    if len(candidate_ids) != len(set(candidate_ids)) or any(
        candidate["route"] not in routes for candidate in candidates
    ):
        raise _invalid()
    resolution = _project_resolution(result["resolution_json"], strict=False)
    _validate_resolution(status, resolution, candidates)
    result["routes_json"] = list(routes)
    result["candidates_json"] = candidates
    result["resolution_json"] = resolution
    return result


def evidence_dependencies(rows: Iterable[Mapping[str, Any]]) -> tuple[set[str], set[str]]:
    """Return required artifact and pinned revision IDs for projected rows."""

    artifacts: set[str] = set()
    revisions: set[str] = set()
    for row in rows:
        projected = sanitize_evidence(row)
        artifacts.add(projected["source_artifact_id"])
        for key in ("source_media_artifact_id", "clip_artifact_id"):
            if projected[key] is not None:
                artifacts.add(projected[key])
        revisions.add(projected["source_revision_id"])
        artifacts.update(
            candidate["transcript_artifact_id"]
            for candidate in projected["candidates_json"]
            if candidate.get("transcript_artifact_id") is not None
        )
    return artifacts, revisions


def _artifact_metadata_without_reuse(value: Any) -> Any:
    if type(value) is dict:
        return {
            key: _artifact_metadata_without_reuse(item)
            for key, item in value.items()
            if not key.lower().replace("-", "_").startswith("reused_from")
        }
    if type(value) is list:
        return [_artifact_metadata_without_reuse(item) for item in value]
    return value


def sanitize_evidence_artifact_metadata(
    artifacts: Iterable[dict[str, Any]], evidence_artifact_ids: set[str]
) -> None:
    """Discard operational reuse history on retained evidence artifacts in place."""

    for artifact in artifacts:
        metadata = artifact.get("metadata_json")
        if (
            artifact.get("role") in _ARTIFACT_ROLES
            and artifact.get("id") in evidence_artifact_ids
            and type(metadata) is dict
        ):
            artifact["metadata_json"] = _artifact_metadata_without_reuse(metadata)


def validate_evidence_graph(rows: Mapping[str, list[dict[str, Any]]]) -> None:
    """Independently validate references, session links, pins, and row content."""

    evidence_rows = rows.get("subtitle_evidence")
    if type(evidence_rows) is not list:
        raise _invalid()
    evidence_by_id: dict[str, dict[str, Any]] = {}
    for row in evidence_rows:
        if type(row) is not dict or set(row) != EVIDENCE_FIELDS:
            raise _invalid()
        projected = sanitize_evidence(row)
        if projected != row or row["id"] in evidence_by_id:
            raise _invalid()
        evidence_by_id[row["id"]] = row

    sessions = {row["id"] for row in rows.get("sessions", [])}
    documents = {row["id"]: row for row in rows.get("documents", [])}
    revisions = {row["id"]: row for row in rows.get("document_revisions", [])}
    segments = {row["id"]: row for row in rows.get("segments", [])}
    artifacts = {row["id"]: row for row in rows.get("artifacts", [])}
    assets = {row["id"]: row for row in rows.get("source_assets", [])}
    attached_artifacts: dict[str, set[str]] = {}
    for row in rows.get("session_sources", []):
        if row.get("is_current") is True:
            asset = assets.get(row["source_asset_id"])
            if asset is not None:
                attached_artifacts.setdefault(row["session_id"], set()).add(
                    asset["artifact_id"]
                )

    def accessible(artifact: Mapping[str, Any], session_id: str) -> bool:
        if artifact.get("state") == "deleted":
            return False
        if artifact.get("session_id") == session_id:
            return True
        return artifact.get("id") in attached_artifacts.get(session_id, set())

    referenced: list[str] = []
    for segment in rows.get("segments", []):
        revision = revisions.get(segment.get("revision_id"))
        document = documents.get(revision.get("document_id")) if revision else None
        if document is None:
            raise _invalid()
        for evidence_id in _metadata_references(segment.get("metadata_json")):
            item = evidence_by_id.get(evidence_id)
            if item is None or item["session_id"] != document["session_id"]:
                raise _invalid()
            referenced.append(evidence_id)
    for artifact in rows.get("artifacts", []):
        metadata = artifact.get("metadata_json")
        if type(metadata) is not dict:
            raise _invalid()
        for passage in _logical_passage_items(metadata) or []:
            if "evidence_ids" not in passage:
                continue
            passage_session = artifact.get("session_id")
            for evidence_id in _metadata_references({"evidence_ids": passage["evidence_ids"]}):
                item = evidence_by_id.get(evidence_id)
                if item is None or item["session_id"] != passage_session:
                    raise _invalid()
                referenced.append(evidence_id)
        if artifact.get("role") in _ARTIFACT_ROLES and metadata.get("evidence_id") is not None:
            evidence_id = metadata.get("evidence_id")
            if type(evidence_id) is not str or not evidence_id:
                raise _invalid()
            item = evidence_by_id.get(evidence_id)
            if item is None or item["session_id"] != artifact.get("session_id"):
                raise _invalid()
            if artifact["role"] == "subtitle_evidence_audio":
                if any(
                    item[key] == artifact["id"]
                    for item in evidence_by_id.values()
                    for key in ("clip_artifact_id",)
                ) is False:
                    raise _invalid()
            if artifact["role"] == "subtitle_evidence_transcript":
                if not any(
                    candidate.get("transcript_artifact_id") == artifact["id"]
                    for row in evidence_by_id.values()
                    for candidate in row["candidates_json"]
                ):
                    raise _invalid()

    if set(referenced) != set(evidence_by_id):
        raise _invalid()
    if any(row["session_id"] not in sessions for row in evidence_by_id.values()):
        raise _invalid()

    for item in evidence_by_id.values():
        revision = revisions.get(item["source_revision_id"])
        document = documents.get(revision.get("document_id")) if revision else None
        source = artifacts.get(item["source_artifact_id"])
        if (
            document is None
            or document["session_id"] != item["session_id"]
            or source is None
            or not accessible(source, item["session_id"])
            or (source.get("metadata_json") or {}).get("revision_id")
            != item["source_revision_id"]
        ):
            raise _invalid()
        if item["source_segment_id"] is not None:
            segment = segments.get(item["source_segment_id"])
            if segment is None or segment["revision_id"] != item["source_revision_id"]:
                raise _invalid()
        media_id = item["source_media_artifact_id"]
        if media_id is not None:
            media = artifacts.get(media_id)
            if media is None or not accessible(media, item["session_id"]):
                raise _invalid()
        if item["clip_artifact_id"] is not None:
            clip = artifacts.get(item["clip_artifact_id"])
            if (
                clip is None
                or clip.get("role") != "subtitle_evidence_audio"
                or clip.get("session_id") != item["session_id"]
                or (clip.get("metadata_json") or {}).get("evidence_id")
                not in (None, item["id"])
            ):
                raise _invalid()
        for candidate in item["candidates_json"]:
            transcript_id = candidate.get("transcript_artifact_id")
            if transcript_id is None:
                continue
            transcript = artifacts.get(transcript_id)
            if (
                transcript is None
                or transcript.get("role") != "subtitle_evidence_transcript"
                or transcript.get("session_id") != item["session_id"]
                or (transcript.get("metadata_json") or {}).get("evidence_id")
                not in (None, item["id"])
            ):
                raise _invalid()


def remap_evidence(
    row: Mapping[str, Any],
    identities: Mapping[str, str],
    proofs: Mapping[str, Any],
    *,
    copy_id: str,
) -> dict[str, Any]:
    """Remap one validated evidence row and its row-scoped candidate identities."""

    projected = sanitize_evidence(row)
    old_evidence_id = projected["id"]
    result = dict(projected)
    for key in (
        "id",
        "session_id",
        "source_artifact_id",
        "source_media_artifact_id",
        "source_revision_id",
        "source_segment_id",
        "clip_artifact_id",
    ):
        old_id = result[key]
        if old_id is not None:
            if old_id not in identities:
                raise _invalid()
            result[key] = identities[old_id]
    candidates: list[dict[str, Any]] = []
    candidate_ids: dict[str, str] = {}
    for candidate in projected["candidates_json"]:
        old_candidate_id = candidate["id"]
        canonical = json.dumps(
            [old_evidence_id, old_candidate_id],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        new_candidate_id = str(
            uuid.uuid5(uuid.UUID(copy_id), "subtitle_evidence.candidate:" + canonical)
        )
        candidate_ids[old_candidate_id] = new_candidate_id
        mapped = dict(candidate)
        mapped["id"] = new_candidate_id
        transcript_id = mapped.get("transcript_artifact_id")
        if transcript_id is not None:
            if transcript_id not in identities or transcript_id not in proofs:
                raise _invalid()
            mapped["transcript_artifact_id"] = identities[transcript_id]
        candidates.append(mapped)
    resolution = dict(projected["resolution_json"])
    old_candidate_id = resolution.get("candidate_id")
    if old_candidate_id is not None:
        if old_candidate_id not in candidate_ids:
            raise _invalid()
        resolution["candidate_id"] = candidate_ids[old_candidate_id]
    result["candidates_json"] = candidates
    result["resolution_json"] = resolution
    result.update(job_id=None, audio_model_ids_json=[], error_message=None)
    return result
