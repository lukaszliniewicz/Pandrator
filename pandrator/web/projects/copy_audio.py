"""Bounded selected-take capture and finite owned copied-audio graph codec."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from ..assembly_silences import render_part_silences
from ..generation_audio_identity import IDENTITY_KEY
from ..recorded_speech_contracts import (
    PROVENANCE_KEY,
    finite_json,
    project_recorded_snapshot,
    recorded_input_pins,
    recorded_provenance_version,
    remap_recorded_snapshot,
    validate_recorded_snapshot,
    validate_speech_provenance,
)
from ..speech_input_contracts import AUDIO_LIMIT, JSON_LIMIT, SHA256, SpeechInputOwner
from .copy_speech import _artifact, _owned_artifact

TAKE_FIELDS = frozenset({"id", "generation_segment_id", "generation_run_id", "artifact_id", "parent_take_id",
    "kind", "status", "settings_hash", "duration_ms", "is_active", "revision"})
_METADATA_FIELDS = {IDENTITY_KEY, "kind", "speaker", "source_text", "synthesized_text", "render_parts",
    "speech_timing", "llm_optimized", "llm_model", "audio_verification", PROVENANCE_KEY}
_OPERATIONAL = {"generation_segment_id", "generation_task_run_id", "generation_run_id"}
_PART_FIELDS = {"index", "range", "voice", "source", "fallback", "speaker_ids", "speaker_labels",
    "duration_ms", "silence_before_ms", "silence_policy", "start_frame", "end_frame", "sample_rate_hz", "speech_timing"}
_IDENTITY_FIELDS = {"schema_version", "settings_hash", "voice_reference_hash", "source_voice_reference_hash",
                    "legacy_voice_reference_hash", "performance_request_hash"}
_METRICS = {"duration_ms", "sample_rate_hz", "channels", "sample_width_bits", "sample_count", "speech_units",
    "rms", "rms_dbfs", "peak", "peak_dbfs", "crest_factor", "clipped_sample_fraction", "dc_offset", "tail_rms_dbfs",
    "run_median_rms_dbfs", "run_rms_delta_db", "run_rms_robust_z", "expected_duration_range_ms"}


class AudioGraphInvalid(ValueError):
    """An owned scalar ancestry pointer is foreign or contradictory."""


class UnsupportedAudio(ValueError):
    """Owned current material has no finite selected-recording adapter."""


def _invalid() -> ValueError:
    return ValueError("Invalid selected current audio copy graph.")


def _uuid(value: Any) -> bool:
    try:
        return type(value) is str and str(UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def selected_take_headers(session: Session, revision_ids: set[str], limit: int) -> list[dict[str, Any]]:
    if not revision_ids:
        return []
    rows = [dict(row) for row in session.execute(select(
        *(getattr(m.AudioTake, key) for key in sorted(TAKE_FIELDS)),
        m.GenerationSegment.plan_revision_id.label("_revision_id"),
        m.GenerationSegment.status.label("_segment_status"),
        m.Artifact.id.label("_artifact_exists"), m.Artifact.kind.label("_artifact_kind"),
        m.Artifact.role.label("_artifact_role"), m.Artifact.state.label("_artifact_state"),
        m.Artifact.session_id.label("_artifact_session_id"),
        m.Artifact.project_id.label("_artifact_project_id"),
        m.Artifact.workspace_id.label("_artifact_workspace_id"),
        m.GenerationPlan.session_id.label("_owner_session_id"),
        m.SessionRecord.project_id.label("_owner_project_id"),
        m.Project.workspace_id.label("_owner_workspace_id"),
    ).join(m.GenerationSegment, m.GenerationSegment.id == m.AudioTake.generation_segment_id)
      .join(m.GenerationPlanRevision, m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id)
      .join(m.GenerationPlan, m.GenerationPlan.id == m.GenerationPlanRevision.plan_id)
      .join(m.SessionRecord, m.SessionRecord.id == m.GenerationPlan.session_id)
      .join(m.Project, m.Project.id == m.SessionRecord.project_id)
      .outerjoin(m.Artifact, m.Artifact.id == m.AudioTake.artifact_id)
      .where(m.GenerationSegment.plan_revision_id.in_(revision_ids), m.GenerationSegment.removed.is_(False),
             m.AudioTake.is_active.is_(True)).limit(limit + 1)).mappings()]
    if len(rows) > limit:
        raise OverflowError("Selected take headers exceed the editorial budget.")
    for row in rows:
        if row["artifact_id"] is not None and (row["_artifact_exists"] is None or
                (row["_artifact_session_id"], row["_artifact_project_id"], row["_artifact_workspace_id"]) !=
                (row["_owner_session_id"], row["_owner_project_id"], row["_owner_workspace_id"])):
            raise _invalid()
    if len({row["generation_segment_id"] for row in rows}) != len(rows):
        raise _invalid()
    return rows


def supported_take_header(row: Mapping[str, Any]) -> bool:
    return (row["_artifact_kind"] == "audio" and row["_artifact_role"] == "generation_take" and row["_artifact_state"] == "current"
            and row["status"] == "completed" and row["kind"] == "tts" and row["_segment_status"] == "completed"
            and _uuid(row["artifact_id"]) and type(row["duration_ms"]) is int and row["duration_ms"] > 0
            and (row["settings_hash"] is None or type(row["settings_hash"]) is str and 0 < len(row["settings_hash"]) <= 128))


def _timing(value: Any) -> None:
    if (type(value) is not dict or set(value) != {"version", "text", "words", "engine", "diagnostics"}
            or type(value["version"]) is not int or value["version"] != 1
            or type(value["text"]) is not str or type(value["engine"]) is not str
            or type(value["words"]) is not list or type(value["diagnostics"]) is not dict
            or set(value["diagnostics"]) != {"complete", "character_count", "normalized_match"}
            or type(value["diagnostics"]["complete"]) is not bool
            or type(value["diagnostics"]["character_count"]) is not int
            or type(value["diagnostics"]["normalized_match"]) is not bool):
        raise _invalid()
    for word in value["words"]:
        if (type(word) is not dict or set(word) != {"text", "start_ms", "end_ms", "start_char", "end_char"}
                or type(word["text"]) is not str or any(type(word[key]) is not int or word[key] < 0
                    for key in ("start_ms", "end_ms", "start_char", "end_char"))
                or word["end_ms"] < word["start_ms"] or word["end_char"] < word["start_char"]):
            raise _invalid()


def audio_metadata(value: Any, text: str) -> dict[str, Any]:
    finite_json(value)
    if (type(value) is not dict or set(value) - _METADATA_FIELDS - _OPERATIONAL
            or value.get("kind") != "tts" or value.get("synthesized_text") != text
            or type(value.get("source_text")) is not str):
        raise _invalid()
    identity = value.get(IDENTITY_KEY)
    if (type(identity) is not dict or not {"schema_version", "settings_hash", "voice_reference_hash"} <= identity.keys()
            or set(identity) - _IDENTITY_FIELDS or type(identity["schema_version"]) is not int or identity["schema_version"] != 1
            or any(type(item) is not str or SHA256.fullmatch(item) is None for key, item in identity.items() if key != "schema_version")):
        raise _invalid()
    for key in ("speaker", "llm_model"):
        if key in value and value[key] is not None and type(value[key]) is not str:
            raise _invalid()
    if "llm_optimized" in value and type(value["llm_optimized"]) is not bool:
        raise _invalid()
    if "render_parts" in value:
        if type(value["render_parts"]) is not list:
            raise _invalid()
        for part in value["render_parts"]:
            if type(part) is not dict or set(part) - _PART_FIELDS:
                raise _invalid()
            for key in ("index", "duration_ms", "silence_before_ms", "start_frame", "end_frame", "sample_rate_hz"):
                if key in part and (type(part[key]) is not int or part[key] < (1 if key == "sample_rate_hz" else 0)):
                    raise _invalid()
            if "range" in part and (type(part["range"]) is not list or len(part["range"]) != 2
                    or any(type(item) is not int or item < 0 for item in part["range"]) or part["range"][1] < part["range"][0]):
                raise _invalid()
            for key in ("speaker_ids", "speaker_labels"):
                if key in part and (type(part[key]) is not list or any(type(item) is not str for item in part[key])):
                    raise _invalid()
            if "fallback" in part and type(part["fallback"]) is not bool:
                raise _invalid()
            for key in ("voice", "source", "silence_policy"):
                if key in part and part[key] is not None and type(part[key]) is not str:
                    raise _invalid()
            if "speech_timing" in part:
                _timing(part["speech_timing"])
        if any(part.get("silence_policy") is not None for part in value["render_parts"]):
            if any(part.get("silence_policy") != "assembly-v1" for part in value["render_parts"]):
                raise _invalid()
            render_part_silences(value)
    if "speech_timing" in value:
        _timing(value["speech_timing"])
    if "audio_verification" in value:
        verification = value["audio_verification"]
        if (type(verification) is not dict or set(verification) != {"schema_version", "mode", "status", "issues", "metrics", "scope"}
                or type(verification["schema_version"]) is not int or verification["schema_version"] != 1
                or type(verification["metrics"]) is not dict or set(verification["metrics"]) - _METRICS
                or any(item is not None and type(item) not in {int, float} for key, item in verification["metrics"].items() if key != "expected_duration_range_ms")
                or type(verification["issues"]) is not list):
            raise _invalid()
        if "expected_duration_range_ms" in verification["metrics"]:
            bounds = verification["metrics"]["expected_duration_range_ms"]
            if (type(bounds) is not list or len(bounds) != 2
                    or any(type(bound) is not int or bound < 0 for bound in bounds)
                    or bounds[1] < bounds[0]):
                raise _invalid()
        for issue in verification["issues"]:
            if type(issue) is not dict or set(issue) != {"code", "severity", "message"} or any(type(item) is not str for item in issue.values()):
                raise _invalid()
        if any(type(verification[key]) is not str for key in ("mode", "status", "scope")):
            raise _invalid()
    return {key: deepcopy(item) for key, item in value.items() if key not in _OPERATIONAL}


def _header_forest(session: Session, model: Any, ids: set[str], *, parent: str,
                   segment_join: bool, owner_sessions: set[str], budget: int) -> dict[str, dict[str, Any]]:
    result = {}
    pending = set(ids)
    for _pass in range(32):
        if not pending:
            break
        if len(result) + len(pending) > budget:
            raise OverflowError("Audio ancestry exceeds the editorial budget.")
        found = {}
        for start in range(0, len(pending), 500):
            chunk = sorted(pending)[start:start + 500]
            columns = [model.id, getattr(model, parent)]
            if segment_join:
                columns += [model.generation_segment_id, model.generation_run_id, model.artifact_id,
                            m.GenerationSegment.plan_revision_id]
                statement = select(*columns, m.GenerationPlan.session_id).join(
                    m.GenerationSegment, m.GenerationSegment.id == model.generation_segment_id)
                statement = statement.join(m.GenerationPlanRevision, m.GenerationPlanRevision.id == m.GenerationSegment.plan_revision_id)
            else:
                columns += [model.plan_revision_id, model.session_id, m.GenerationPlan.session_id.label("_owner_session_id")]
                statement = select(*columns).join(m.GenerationPlanRevision, m.GenerationPlanRevision.id == model.plan_revision_id)
            statement = statement.join(m.GenerationPlan, m.GenerationPlan.id == m.GenerationPlanRevision.plan_id).where(model.id.in_(chunk))
            for row in session.execute(statement).mappings():
                value = dict(row)
                if value["session_id"] not in owner_sessions:
                    raise _invalid()
                if not segment_join and value["_owner_session_id"] != value["session_id"]:
                    raise _invalid()
                found[value["id"]] = value
        if set(found) != pending:
            raise _invalid()
        result.update(found)
        pending = {row[parent] for row in found.values() if row[parent] is not None} - result.keys()
    if pending:
        raise OverflowError("Audio ancestry exceeds 32 frontiers.")
    for origin in result:
        seen = set()
        current = origin
        while current is not None:
            if current in seen:
                raise _invalid()
            seen.add(current)
            row = result[current]
            following = row[parent]
            if following is not None and result[following]["session_id"] != row["session_id"]:
                raise _invalid()
            current = following
    return result


def capture_audio_evidence(session: Session, headers: list[dict[str, Any]], artifacts: Mapping[str, dict[str, Any]],
                           segments: list[dict[str, Any]], plans: list[dict[str, Any]], budget: int,
                           owner_by_session: Mapping[str, SpeechInputOwner], *,
                           allow_saved_selections: bool = False):
    """Hydrate only the bounded same-session executor forest for selected roots."""
    if not headers:
        return [], {}, set()
    plan_sessions = {row["active_revision_id"]: row["session_id"] for row in plans}
    segment_map = {row["id"]: row for row in segments}
    owners = {plan_sessions[row["_revision_id"]] for row in headers}
    take_headers = _header_forest(session, m.AudioTake, {row["id"] for row in headers}, parent="parent_take_id",
                                  segment_join=True, owner_sessions=owners, budget=budget)
    artifact_ids = {row["artifact_id"] for row in take_headers.values() if row["artifact_id"] is not None}
    if len(take_headers) + len(artifact_ids) > budget:
        raise OverflowError("Audio ownership headers exceed the editorial budget.")
    artifact_headers = {}
    for start in range(0, len(artifact_ids), 500):
        for row in session.execute(select(m.Artifact.id, m.Artifact.session_id, m.Artifact.project_id,
                m.Artifact.workspace_id, m.Artifact.metadata_json["generation_task_run_id"].as_string().label("executor_id"))
                .where(m.Artifact.id.in_(sorted(artifact_ids)[start:start + 500]))).mappings():
            artifact_headers[row["id"]] = dict(row)
    if set(artifact_headers) != artifact_ids:
        raise _invalid()
    for take in take_headers.values():
        artifact = artifact_headers.get(take["artifact_id"])
        owner = owner_by_session[take["session_id"]]
        if artifact is not None and (artifact["workspace_id"], artifact["project_id"], artifact["session_id"]) != (owner.workspace_id, owner.project_id, owner.session_id):
            raise _invalid()
    executor_ids = {row["id"]: (artifact_headers.get(row["artifact_id"], {}).get("executor_id") or row["generation_run_id"])
                    for row in take_headers.values()}
    run_ids = {value for value in executor_ids.values() if value is not None}
    run_ids.update(row["generation_run_id"] for row in take_headers.values() if row["generation_run_id"] is not None)
    run_headers = _header_forest(session, m.GenerationRun, run_ids, parent="source_generation_run_id",
        segment_join=False, owner_sessions=owners, budget=budget - len(take_headers) - len(artifact_headers)) if run_ids else {}
    for take_id, run_id in executor_ids.items():
        for pointer in (run_id, take_headers[take_id]["generation_run_id"]):
            if pointer is not None and run_headers[pointer]["session_id"] != take_headers[take_id]["session_id"]:
                raise _invalid()
    snapshots = {}
    for start in range(0, len(run_headers), 500):
        for row in session.execute(select(m.GenerationRun.id, m.GenerationRun.settings_snapshot_json)
                .where(m.GenerationRun.id.in_(sorted(run_headers)[start:start + 500]))):
            snapshots[row.id] = row.settings_snapshot_json
    canonical, evidence, unsupported = [], {}, set()
    for header in headers:
        segment = segment_map[header["generation_segment_id"]]
        text = segment["optimized_text"] or segment["text"]
        sid = plan_sessions[header["_revision_id"]]
        artifact = artifacts.get(header["artifact_id"])
        if artifact is None:
            raise _invalid()
        try:
            metadata = audio_metadata(artifact["metadata_json"], text)
            snapshot = None
            take = take_headers[header["id"]]
            while take is not None and snapshot is None:
                run_id = executor_ids[take["id"]]
                while run_id is not None and snapshot is None:
                    raw = snapshots[run_id]
                    if type(raw) is not dict:
                        raise UnsupportedAudio()
                    frozen = raw.get("generation_control_snapshot")
                    if frozen is None:
                        try:
                            snapshot = project_recorded_snapshot(raw, entry_id=take["generation_segment_id"],
                                segment_id=segment["id"], revision_id=segment["plan_revision_id"], text=text,
                                allow_saved_selections=allow_saved_selections)
                        except ValueError:
                            pass
                    elif type(frozen) is dict:
                        entries = frozen.get("segments", {})
                        entry_id = segment["id"] if segment["id"] in entries else take["generation_segment_id"]
                        if entry_id in entries:
                            if take["session_id"] != sid or frozen.get("plan_revision_id") != run_headers[run_id]["plan_revision_id"]:
                                raise AudioGraphInvalid()
                            snapshot = project_recorded_snapshot(raw, entry_id=entry_id, segment_id=segment["id"],
                                revision_id=segment["plan_revision_id"], text=text,
                                allow_saved_selections=allow_saved_selections)
                    run_id = run_headers[run_id]["source_generation_run_id"]
                if snapshot is None and take["generation_run_id"] is None:
                    copied = artifacts.get(take["artifact_id"])
                    stored = (copied or {}).get("metadata_json", {}).get(PROVENANCE_KEY)
                    if stored is not None:
                        wrapper = validate_speech_provenance(stored, allow_saved_selections=allow_saved_selections)
                        entry = wrapper["segments"].get(take["generation_segment_id"])
                        if entry is not None:
                            original_revision = take["plan_revision_id"]
                            expanded = wrapper["version"] == 2
                            validate_recorded_snapshot(entry, segment_id=take["generation_segment_id"],
                                revision_id=original_revision, text=text, allow_saved_selections=expanded)
                            snapshot = remap_recorded_snapshot(entry, {pin["artifact_id"]: pin["artifact_id"]
                                for pin in recorded_input_pins(entry, allow_saved_selections=expanded)},
                                segment_id=segment["id"], revision_id=segment["plan_revision_id"], text=text,
                                allow_saved_selections=expanded)
                take = take_headers.get(take["parent_take_id"])
            if snapshot is None:
                raise UnsupportedAudio()
            wrapper = evidence.setdefault(header["artifact_id"], {"version": 1, "segments": {}})
            wrapper["version"] = max(wrapper["version"], recorded_provenance_version(snapshot))
            wrapper["segments"][segment["id"]] = snapshot
            canonical.append({key: (None if key in {"generation_run_id", "parent_take_id"} else
                1 if key == "revision" else header[key]) for key in TAKE_FIELDS})
            metadata[PROVENANCE_KEY] = evidence[header["artifact_id"]]
        except AudioGraphInvalid:
            raise
        except (ValueError, TypeError, KeyError):
            unsupported.add(header["_revision_id"])
    return canonical, evidence, unsupported


def validate_audio_copy_graph(rows: Mapping[str, Any], *, allow_saved_selections: bool = False) -> None:
    takes = rows.get("audio_takes", [])
    if type(takes) is not list:
        raise _invalid()
    segments = {row["id"]: row for row in rows.get("generation_segments", [])}
    revisions = {row["id"]: row for row in rows.get("generation_plan_revisions", [])}
    plans = {row["id"]: row for row in rows.get("generation_plans", [])}
    sessions = {row["id"]: row for row in rows.get("sessions", [])}
    projects = {row["id"]: row for row in rows.get("projects", [])}
    artifacts = {row["id"]: row for row in rows.get("artifacts", [])}
    edges = rows.get("artifact_edges", [])
    selected = set()
    evidence_keys = {}
    for take in takes:
        finite_json(take)
        if (type(take) is not dict or set(take) != TAKE_FIELDS or not all(_uuid(take[key]) for key in ("id", "generation_segment_id", "artifact_id"))
                or take["generation_segment_id"] in selected or take["generation_run_id"] is not None or take["parent_take_id"] is not None
                or take["kind"] != "tts" or take["status"] != "completed" or take["is_active"] is not True
                or type(take["revision"]) is not int or take["revision"] != 1
                or type(take["duration_ms"]) is not int or take["duration_ms"] <= 0
                or take["settings_hash"] is not None and (type(take["settings_hash"]) is not str or not 0 < len(take["settings_hash"]) <= 128)):
            raise _invalid()
        segment = segments.get(take["generation_segment_id"])
        if segment is None or segment["removed"] or segment["status"] != "completed":
            raise _invalid()
        selected.add(segment["id"])
        sid = plans[revisions[segment["plan_revision_id"]]["plan_id"]]["session_id"]
        project = projects[sessions[sid]["project_id"]]
        owner = SpeechInputOwner(project["workspace_id"], project["id"], sid)
        artifact = artifacts.get(take["artifact_id"])
        if (artifact is None or artifact["kind"] != "audio" or artifact["role"] != "generation_take" or artifact["state"] != "current"
                or (artifact["workspace_id"], artifact["project_id"], artifact["session_id"]) != (owner.workspace_id, owner.project_id, owner.session_id)):
            raise _invalid()
        metadata = audio_metadata(artifact["metadata_json"], segment["optimized_text"] or segment["text"])
        if metadata != artifact["metadata_json"]:
            raise _invalid()
        wrapper = validate_speech_provenance(metadata.get(PROVENANCE_KEY),
                                             allow_saved_selections=allow_saved_selections)
        snapshot = wrapper["segments"].get(segment["id"])
        expanded = wrapper["version"] == 2
        validate_recorded_snapshot(snapshot, segment_id=segment["id"], revision_id=segment["plan_revision_id"],
            text=segment["optimized_text"] or segment["text"], allow_saved_selections=expanded)
        evidence_keys.setdefault(artifact["id"], set()).add(segment["id"])
        for pin in recorded_input_pins(snapshot, allow_saved_selections=expanded):
            pinned = _artifact(artifacts, pin["artifact_id"])
            _owned_artifact(pinned, pin["artifact_id"], owner, kind="speech_voice_snapshot", role="speech_input",
                mime_type="application/json", limit=JSON_LIMIT, expected_hash=pin["content_hash"])
            parents = [edge for edge in edges if edge["child_artifact_id"] == pin["artifact_id"]]
            if len(parents) != 1 or parents[0]["relation"] != "speech_reference":
                raise _invalid()
            audio = _artifact(artifacts, parents[0]["parent_artifact_id"])
            _owned_artifact(audio, audio["id"], owner, kind="audio", role="speech_reference_audio", mime_type="audio/wav", limit=AUDIO_LIMIT, require_hash=True)
            if not any(edge["parent_artifact_id"] == pin["artifact_id"] and edge["child_artifact_id"] == artifact["id"] for edge in edges):
                raise _invalid()
    for artifact in artifacts.values():
        metadata = artifact.get("metadata_json")
        if type(metadata) is dict and PROVENANCE_KEY in metadata:
            wrapper = validate_speech_provenance(metadata[PROVENANCE_KEY],
                                                 allow_saved_selections=allow_saved_selections)
            if set(wrapper["segments"]) != evidence_keys.get(artifact["id"], set()):
                raise _invalid()
    if any(row["status"] != ("completed" if row["id"] in selected else "ready") for row in segments.values()):
        raise _invalid()


def remap_audio_rows(rows: Mapping[str, Any], identities: dict[str, str]) -> list[dict[str, Any]]:
    return [{**row, "id": identities[row["id"]], "generation_segment_id": identities[row["generation_segment_id"]],
             "artifact_id": identities[row["artifact_id"]]} for row in rows.get("audio_takes", [])]


def remap_audio_metadata(rows: Mapping[str, Any], identities: dict[str, str], *,
                         allow_saved_selections: bool = False) -> dict[str, dict[str, Any]]:
    segments = {row["id"]: row for row in rows.get("generation_segments", [])}
    result = {}
    for artifact in rows.get("artifacts", []):
        metadata = artifact.get("metadata_json")
        if artifact["role"] != "generation_take" or PROVENANCE_KEY not in metadata:
            continue
        copied = {key: deepcopy(value) for key, value in metadata.items() if key not in _OPERATIONAL | {PROVENANCE_KEY}}
        if PROVENANCE_KEY in metadata:
            wrapper = validate_speech_provenance(metadata[PROVENANCE_KEY],
                                                 allow_saved_selections=allow_saved_selections)
            entries = {}
            for identifier, snapshot in wrapper["segments"].items():
                segment = segments[identifier]
                entries[identities[identifier]] = remap_recorded_snapshot(snapshot, identities,
                    segment_id=identities[identifier], revision_id=identities[segment["plan_revision_id"]],
                    text=segment["optimized_text"] or segment["text"],
                    allow_saved_selections=wrapper["version"] == 2)
            copied[PROVENANCE_KEY] = {"version": wrapper["version"], "segments": entries}
        result[artifact["id"]] = copied
    return result
