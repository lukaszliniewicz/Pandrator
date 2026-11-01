"""Pure finite codec for the current project sound-plan copy graph."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable, Mapping
from copy import deepcopy
from typing import Any, TypeVar

from ..sound_project_schemas import SoundAnchor, SoundPlayback

PLAN_FIELDS = frozenset({"id", "session_id", "revision", "speech_plan_revision_id"})
CUE_FIELDS = frozenset(
    {
        "id",
        "logical_id",
        "plan_id",
        "variant_id",
        "anchor_json",
        "playback_json",
        "included",
        "placement_status",
    }
)
VARIANT_FIELDS = frozenset(
    {
        "id",
        "workspace_id",
        "item_id",
        "job_id",
        "artifact_id",
        "state",
        "fingerprint",
        "provenance_json",
        "sample_rate",
        "channels",
        "frame_count",
        "duration_seconds",
    }
)
SOUND_FAMILIES = frozenset({"project_sound_plans", "project_sound_cues", "sound_variants"})
COPIED_SOUND_KEY = "copied_sound"

_ARTIFACT_FIELDS = frozenset(
    {
        "id",
        "workspace_id",
        "project_id",
        "session_id",
        "kind",
        "role",
        "relative_path",
        "mime_type",
        "size_bytes",
        "content_hash",
        "state",
        "settings_hash",
        "metadata_json",
    }
)
_ARTIFACT_MARKER = {COPIED_SOUND_KEY: {"version": 1}}
_MAX_EXACT_INTEGER = 2**53 - 1
_CONTENT_HASH = re.compile(r"[0-9a-fA-F]{64}\Z")
_ERROR = "Invalid current sound-plan copy graph."
_T = TypeVar("_T")


def _invalid() -> ValueError:
    return ValueError(_ERROR)


def _guard(action: Callable[[], _T]) -> _T:
    try:
        return action()
    except (ValueError, TypeError, KeyError, RecursionError, OverflowError) as error:
        if isinstance(error, ValueError) and error.args == (_ERROR,):
            raise
        raise _invalid() from None


def _identifier(value: Any) -> bool:
    return type(value) is str and bool(value.strip()) and len(value) <= 36


def _finite(value: Any) -> None:
    def walk(item: Any) -> None:
        if item is None or type(item) in {str, int, bool}:
            return
        if type(item) is float:
            if not math.isfinite(item):
                raise _invalid()
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
    except (TypeError, ValueError, OverflowError, UnicodeError, RecursionError):
        raise _invalid() from None


def _mapping(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise _invalid()
    return value


def _required(row: Mapping[str, Any], key: str) -> Any:
    if key not in row:
        raise _invalid()
    return row[key]


def _positive_integer(value: Any, *, maximum: int | None = None) -> bool:
    return type(value) is int and value > 0 and (maximum is None or value <= maximum)


def _audio_facts(row: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in ("sample_rate", "channels", "frame_count"):
        value = _required(row, key)
        if value is not None and not _positive_integer(value, maximum=_MAX_EXACT_INTEGER):
            raise _invalid()
        result[key] = value

    duration = _required(row, "duration_seconds")
    if duration is not None:
        if type(duration) is int:
            if duration <= 0:
                raise _invalid()
        elif type(duration) is float:
            if not math.isfinite(duration) or duration <= 0:
                raise _invalid()
        else:
            raise _invalid()
    result["duration_seconds"] = duration
    _finite(result)
    return result


def _freeze_sound_variant(row: Mapping[str, Any], display_name: str | None) -> dict[str, Any]:
    row = _mapping(row)
    identifier = _required(row, "id")
    workspace_id = _required(row, "workspace_id")
    artifact_id = _required(row, "artifact_id")
    state = _required(row, "state")
    if (
        not _identifier(identifier)
        or not _identifier(workspace_id)
        or not _identifier(artifact_id)
        or type(state) is not str
        or state != "ready"
    ):
        raise _invalid()
    if display_name is not None and type(display_name) is not str:
        raise _invalid()
    name = display_name if display_name is not None and display_name.strip() else "Sound"
    result = {
        "id": identifier,
        "workspace_id": workspace_id,
        "item_id": None,
        "job_id": None,
        "artifact_id": artifact_id,
        "state": "ready",
        "fingerprint": None,
        "provenance_json": {COPIED_SOUND_KEY: {"version": 1}, "display_name": name[:255]},
        **_audio_facts(row),
    }
    _finite(result)
    return result


def freeze_sound_variant(row: Mapping[str, Any], *, display_name: str | None) -> dict[str, Any]:
    """Project one ready variant into a finite reusable copy root."""
    return _guard(lambda: _freeze_sound_variant(row, display_name))


def _freeze_sound_artifact(row: Mapping[str, Any]) -> dict[str, Any]:
    row = _mapping(row)
    identifier = _required(row, "id")
    workspace_id = _required(row, "workspace_id")
    project_id = _required(row, "project_id")
    session_id = _required(row, "session_id")
    kind = _required(row, "kind")
    role = _required(row, "role")
    relative_path = _required(row, "relative_path")
    mime_type = _required(row, "mime_type")
    size_bytes = _required(row, "size_bytes")
    content_hash = _required(row, "content_hash")
    state = _required(row, "state")
    if (
        not _identifier(identifier)
        or not _identifier(workspace_id)
        or project_id is not None
        and not _identifier(project_id)
        or session_id is not None
        or type(kind) is not str
        or kind != "audio"
        or type(role) is not str
        or role != "sound.original"
        or type(relative_path) is not str
        or not relative_path.strip()
        or type(mime_type) is not str
        or not mime_type.startswith("audio/")
        or not mime_type[6:].strip()
        or type(size_bytes) is not int
        or size_bytes <= 0
        or type(content_hash) is not str
        or _CONTENT_HASH.fullmatch(content_hash) is None
        or type(state) is not str
        or state != "current"
    ):
        raise _invalid()
    result = {
        "id": identifier,
        "workspace_id": workspace_id,
        "project_id": project_id,
        "session_id": None,
        "kind": "audio",
        "role": "sound.original",
        "relative_path": relative_path,
        "mime_type": mime_type,
        "size_bytes": size_bytes,
        "content_hash": content_hash,
        "state": "current",
        "settings_hash": None,
        "metadata_json": deepcopy(_ARTIFACT_MARKER),
    }
    _finite(result)
    return result


def freeze_sound_artifact(row: Mapping[str, Any]) -> dict[str, Any]:
    """Project a current global sound artifact without traversing discarded metadata."""
    return _guard(lambda: _freeze_sound_artifact(row))


def _clean_anchor(value: Any) -> dict[str, Any]:
    if type(value) is not dict:
        raise _invalid()
    cleaned: dict[str, Any] = {}
    for key, item in value.items():
        if type(key) is not str:
            raise _invalid()
        if key == "resolution" or key.startswith("origin_"):
            continue
        cleaned[key] = item
    _finite(cleaned)
    return cleaned


def _freeze_sound_cue(row: Mapping[str, Any], workflow_kind: str) -> dict[str, Any]:
    row = _mapping(row)
    if set(row) != CUE_FIELDS:
        raise _invalid()
    if type(workflow_kind) is not str or not workflow_kind.strip():
        raise _invalid()
    identifier = _required(row, "id")
    logical_id = _required(row, "logical_id")
    plan_id = _required(row, "plan_id")
    variant_id = _required(row, "variant_id")
    included = _required(row, "included")
    placement_status = _required(row, "placement_status")
    if (
        not all(_identifier(value) for value in (identifier, logical_id, plan_id, variant_id))
        or type(included) is not bool
        or placement_status not in {"ready", "needs_placement"}
        or type(placement_status) is not str
    ):
        raise _invalid()

    anchor_data = _clean_anchor(_required(row, "anchor_json"))
    playback_data = _required(row, "playback_json")
    if type(playback_data) is not dict:
        raise _invalid()
    _finite(playback_data)
    try:
        anchor = SoundAnchor.model_validate(anchor_data, strict=True)
        playback = SoundPlayback.model_validate(playback_data, strict=True)
    except ValueError:
        raise _invalid() from None

    mode = playback.mode or ("insert" if workflow_kind == "audiobook" else "overlay")
    if workflow_kind in {"voiceover", "subtitles"} and mode == "insert":
        raise _invalid()
    if anchor.kind == "source_time":
        if workflow_kind not in {"voiceover", "subtitles", "media_edit"} or mode != "overlay":
            raise _invalid()
    if mode == "insert" and playback.offset_ms < 0:
        raise _invalid()

    anchor_json = anchor.model_dump(exclude_none=True)
    playback_json = {**playback.model_dump(exclude_none=True), "mode": mode}
    status = "needs_placement" if anchor.kind in {"after_word", "next_pause"} else placement_status
    result = {
        "id": identifier,
        "logical_id": logical_id,
        "plan_id": plan_id,
        "variant_id": variant_id,
        "anchor_json": anchor_json,
        "playback_json": playback_json,
        "included": included,
        "placement_status": status,
    }
    _finite(result)
    return result


def freeze_sound_cue(row: Mapping[str, Any], *, workflow_kind: str) -> dict[str, Any]:
    """Detach one cue, removing source resolution and refreshing placement state."""
    return _guard(lambda: _freeze_sound_cue(row, workflow_kind))


def _exact_rows(value: Any, fields: frozenset[str]) -> dict[str, dict[str, Any]]:
    if type(value) is not list:
        raise _invalid()
    result: dict[str, dict[str, Any]] = {}
    for row in value:
        if type(row) is not dict or set(row) != fields:
            raise _invalid()
        _finite(row)
        identifier = row.get("id")
        if type(identifier) is not str or not _identifier(identifier) or identifier in result:
            raise _invalid()
        result[identifier] = row
    return result


def _core_index(value: Any) -> dict[str, dict[str, Any]]:
    if type(value) is not list:
        raise _invalid()
    result: dict[str, dict[str, Any]] = {}
    for row in value:
        if type(row) is not dict:
            raise _invalid()
        identifier = row.get("id")
        if type(identifier) is not str or not _identifier(identifier) or identifier in result:
            raise _invalid()
        result[identifier] = row
    return result


def _has_sound_marker(row: Any) -> bool:
    if type(row) is not dict:
        return False
    metadata = row.get("metadata_json")
    return type(metadata) is dict and COPIED_SOUND_KEY in metadata


def _marker_name(value: Any) -> str:
    if (
        type(value) is not dict
        or set(value) != {COPIED_SOUND_KEY, "display_name"}
        or type(value[COPIED_SOUND_KEY]) is not dict
        or set(value[COPIED_SOUND_KEY]) != {"version"}
        or type(value[COPIED_SOUND_KEY]["version"]) is not int
        or value[COPIED_SOUND_KEY]["version"] != 1
        or type(value["display_name"]) is not str
        or not value["display_name"].strip()
        or len(value["display_name"]) > 255
    ):
        raise _invalid()
    _finite(value)
    return value["display_name"]


def _has_exact_artifact_marker(value: Any) -> bool:
    return (
        type(value) is dict
        and set(value) == {COPIED_SOUND_KEY}
        and type(value[COPIED_SOUND_KEY]) is dict
        and set(value[COPIED_SOUND_KEY]) == {"version"}
        and type(value[COPIED_SOUND_KEY]["version"]) is int
        and value[COPIED_SOUND_KEY]["version"] == 1
    )


def _validate_legacy_graph(rows: Mapping[str, Any]) -> None:
    for family in SOUND_FAMILIES:
        if family not in rows:
            continue
        value = rows[family]
        if type(value) is not list or value:
            raise _invalid()
    artifacts = rows.get("artifacts", [])
    if type(artifacts) is not list:
        raise _invalid()
    if any(_has_sound_marker(row) for row in artifacts):
        raise _invalid()


def _validate_sound_copy_graph(rows: Mapping[str, Any]) -> None:
    if not isinstance(rows, Mapping):
        raise _invalid()
    sound_rows = {family: _required(rows, family) for family in SOUND_FAMILIES}
    if any(type(value) is not list for value in sound_rows.values()):
        raise _invalid()
    if all(not value for value in sound_rows.values()):
        _validate_legacy_graph(rows)
        return

    projects = _core_index(_required(rows, "projects"))
    sessions = _core_index(_required(rows, "sessions"))
    generation_plans = _core_index(_required(rows, "generation_plans"))
    generation_revisions = _core_index(_required(rows, "generation_plan_revisions"))
    generation_segments = _core_index(_required(rows, "generation_segments"))
    artifacts = _core_index(_required(rows, "artifacts"))
    artifact_edges = _required(rows, "artifact_edges")
    if type(artifact_edges) is not list or len(projects) != 1:
        raise _invalid()
    project_id, project = next(iter(projects.items()))
    workspace_id = project.get("workspace_id")
    if not _identifier(workspace_id):
        raise _invalid()

    plans = _exact_rows(sound_rows["project_sound_plans"], PLAN_FIELDS)
    variants = _exact_rows(sound_rows["sound_variants"], VARIANT_FIELDS)
    cue_rows = sound_rows["project_sound_cues"]

    entity_ids: set[str] = set()
    for family in (plans, variants):
        if entity_ids.intersection(family):
            raise _invalid()
        entity_ids.update(family)

    generation_plan_by_session: dict[str, dict[str, Any]] = {}
    for generation_plan in generation_plans.values():
        session_id = generation_plan.get("session_id")
        active_revision_id = generation_plan.get("active_revision_id")
        if (
            not _identifier(session_id)
            or session_id not in sessions
            or active_revision_id is not None
            and not _identifier(active_revision_id)
            or session_id in generation_plan_by_session
        ):
            raise _invalid()
        generation_plan_by_session[session_id] = generation_plan

    for revision in generation_revisions.values():
        if not _identifier(revision.get("plan_id")) or revision["plan_id"] not in generation_plans:
            raise _invalid()
    for generation_plan in generation_plans.values():
        active_revision_id = generation_plan.get("active_revision_id")
        if active_revision_id is not None:
            revision = generation_revisions.get(active_revision_id)
            if revision is None or revision.get("plan_id") != generation_plan["id"]:
                raise _invalid()
    for segment in generation_segments.values():
        revision_id = segment.get("plan_revision_id")
        if not _identifier(revision_id) or revision_id not in generation_revisions:
            raise _invalid()

    plan_by_session: dict[str, dict[str, Any]] = {}
    for plan in plans.values():
        session_id = plan["session_id"]
        revision = plan["revision"]
        speech_revision_id = plan["speech_plan_revision_id"]
        session = sessions.get(session_id)
        if (
            not _identifier(session_id)
            or session is None
            or session.get("project_id") != project_id
            or type(session.get("workflow_kind")) is not str
            or not session["workflow_kind"].strip()
            or type(revision) is not int
            or revision <= 0
            or speech_revision_id is not None
            and not _identifier(speech_revision_id)
            or session_id in plan_by_session
        ):
            raise _invalid()
        generation_plan = generation_plan_by_session.get(session_id)
        active_revision_id = generation_plan.get("active_revision_id") if generation_plan else None
        if speech_revision_id != active_revision_id:
            raise _invalid()
        if speech_revision_id is not None:
            if generation_plan is None:
                raise _invalid()
            pinned_revision = generation_revisions.get(speech_revision_id)
            if pinned_revision is None or pinned_revision.get("plan_id") != generation_plan["id"]:
                raise _invalid()
        plan_by_session[session_id] = plan

    cues: dict[str, dict[str, Any]] = {}
    logical_ids: dict[str, set[str]] = {}
    if len(cue_rows) > 1000 * max(1, len(plans)):
        raise _invalid()
    for row in cue_rows:
        if type(row) is not dict or set(row) != CUE_FIELDS:
            raise _invalid()
        identifier = row.get("id")
        if (
            type(identifier) is not str
            or not _identifier(identifier)
            or identifier in cues
            or identifier in entity_ids
        ):
            raise _invalid()
        plan_id = row.get("plan_id")
        if type(plan_id) is not str or not _identifier(plan_id):
            raise _invalid()
        plan = plans.get(plan_id)
        if plan is None:
            raise _invalid()
        session = sessions[plan["session_id"]]
        workflow_kind = session["workflow_kind"]
        frozen = _freeze_sound_cue(row, workflow_kind)
        if frozen != row:
            raise _invalid()
        logical_id = frozen["logical_id"]
        seen_logical = logical_ids.setdefault(plan_id, set())
        if logical_id in seen_logical or len(seen_logical) >= 1000:
            raise _invalid()
        seen_logical.add(logical_id)
        entity_ids.add(identifier)
        cues[identifier] = frozen

    variant_references: set[str] = set()
    for cue in cues.values():
        variant_id = cue["variant_id"]
        if variant_id not in variants:
            raise _invalid()
        variant_references.add(variant_id)
        plan = plans[cue["plan_id"]]
        anchor = cue["anchor_json"]
        segment_id = anchor.get("segment_id")
        pinned_revision_id = plan["speech_plan_revision_id"]
        if segment_id is not None:
            if not _identifier(segment_id) or pinned_revision_id is None:
                raise _invalid()
            segment = generation_segments.get(segment_id)
            if segment is None or segment.get("plan_revision_id") != pinned_revision_id:
                raise _invalid()
    if variant_references != set(variants):
        raise _invalid()

    selected_artifact_ids: set[str] = set()
    for variant in variants.values():
        display_name = _marker_name(variant["provenance_json"])
        _finite(variant)
        if _freeze_sound_variant(variant, display_name) != variant:
            raise _invalid()
        if variant["workspace_id"] != workspace_id:
            raise _invalid()
        artifact_id = variant["artifact_id"]
        if artifact_id not in artifacts:
            raise _invalid()
        selected_artifact_ids.add(artifact_id)

    for artifact_id in selected_artifact_ids:
        artifact = artifacts[artifact_id]
        if type(artifact) is not dict or set(artifact) != _ARTIFACT_FIELDS:
            raise _invalid()
        if not _has_exact_artifact_marker(artifact["metadata_json"]):
            raise _invalid()
        if _freeze_sound_artifact(artifact) != artifact:
            raise _invalid()
        if (
            artifact["workspace_id"] != workspace_id
            or artifact["session_id"] is not None
            or artifact["project_id"] not in {None, project_id}
        ):
            raise _invalid()

    for artifact in artifacts.values():
        if _has_sound_marker(artifact) and artifact["id"] not in selected_artifact_ids:
            raise _invalid()

    for edge in artifact_edges:
        if type(edge) is not dict:
            raise _invalid()
        child_id = edge.get("child_artifact_id")
        if child_id in selected_artifact_ids:
            raise _invalid()


def validate_sound_copy_graph(rows: Mapping[str, Any], *, allow_sounds: bool = False) -> None:
    """Validate closure and current ownership for copied sound-plan rows."""

    def validate() -> None:
        if not isinstance(rows, Mapping) or type(allow_sounds) is not bool:
            raise _invalid()
        if allow_sounds:
            _validate_sound_copy_graph(rows)
        else:
            _validate_legacy_graph(rows)

    _guard(validate)


def _required_identity(identities: Mapping[str, str], source_id: str) -> str:
    if source_id not in identities:
        raise _invalid()
    destination_id = identities[source_id]
    if not _identifier(destination_id):
        raise _invalid()
    return destination_id


def _remap_sound_copy_rows(
    rows: Mapping[str, Any],
    identities: Mapping[str, str],
    destination_workspace_id: str,
) -> dict[str, list[dict[str, Any]]]:
    _mapping(identities)
    if not _identifier(destination_workspace_id):
        raise _invalid()
    _validate_sound_copy_graph(rows)
    plans = rows["project_sound_plans"]
    cues = rows["project_sound_cues"]
    variants = rows["sound_variants"]

    required_source_ids: set[str] = set()
    for plan in plans:
        required_source_ids.update({plan["id"], plan["session_id"]})
        if plan["speech_plan_revision_id"] is not None:
            required_source_ids.add(plan["speech_plan_revision_id"])
    for cue in cues:
        required_source_ids.update({cue["id"], cue["plan_id"], cue["variant_id"]})
        segment_id = cue["anchor_json"].get("segment_id")
        if segment_id is not None:
            required_source_ids.add(segment_id)
    for variant in variants:
        required_source_ids.update({variant["id"], variant["artifact_id"]})

    mapped_ids: dict[str, str] = {}
    mapped_targets: set[str] = set()
    for source_id in required_source_ids:
        target_id = _required_identity(identities, source_id)
        _finite(target_id)
        if target_id in required_source_ids or target_id in mapped_targets:
            raise _invalid()
        mapped_targets.add(target_id)
        mapped_ids[source_id] = target_id

    result: dict[str, list[dict[str, Any]]] = {
        "project_sound_plans": [],
        "project_sound_cues": [],
        "sound_variants": [],
    }
    for plan in plans:
        copied = deepcopy(plan)
        copied["id"] = mapped_ids[plan["id"]]
        copied["session_id"] = mapped_ids[plan["session_id"]]
        copied["speech_plan_revision_id"] = (
            mapped_ids[plan["speech_plan_revision_id"]]
            if plan["speech_plan_revision_id"] is not None
            else None
        )
        copied["revision"] = 1
        result["project_sound_plans"].append(copied)

    for cue in cues:
        copied = deepcopy(cue)
        copied["id"] = mapped_ids[cue["id"]]
        copied["logical_id"] = mapped_ids[cue["id"]]
        copied["plan_id"] = mapped_ids[cue["plan_id"]]
        copied["variant_id"] = mapped_ids[cue["variant_id"]]
        segment_id = cue["anchor_json"].get("segment_id")
        if segment_id is not None:
            copied["anchor_json"]["segment_id"] = mapped_ids[segment_id]
        result["project_sound_cues"].append(copied)

    for variant in variants:
        copied = deepcopy(variant)
        copied["id"] = mapped_ids[variant["id"]]
        copied["workspace_id"] = destination_workspace_id
        copied["artifact_id"] = mapped_ids[variant["artifact_id"]]
        result["sound_variants"].append(copied)
    _finite(result)
    return result


def remap_sound_copy_rows(
    rows: Mapping[str, Any],
    identities: Mapping[str, str],
    *,
    destination_workspace_id: str,
) -> dict[str, list[dict[str, Any]]]:
    """Remap current sound rows into a destination session/workspace graph."""
    return _guard(lambda: _remap_sound_copy_rows(rows, identities, destination_workspace_id))
