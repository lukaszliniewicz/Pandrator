"""Pure validation and identity remapping for copied saved TTS references."""

from __future__ import annotations

import json
from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from ..generation_control_schemas import CastSettings, CharacterEntry, VoiceBinding
from ..generation_controls import iter_cast_voice_bindings
from ..project_cast_settings import _selected_settings
from ..project_speech_references import (
    StoredProjectSpeechReference,
    normalize_speech_reference_binding,
    normalize_speech_reference_slot,
    segment_speech_reference_identity,
    speech_reference_selector,
    speech_reference_selector_hash,
)
from ..project_tts_settings import _retained_selection
from ..speech_input_contracts import (
    AUDIO_LIMIT,
    JSON_LIMIT,
    SHA256,
    SpeechInputOwner,
    validate_speech_input_pin,
)

_REFERENCE_FIELDS = {
    "session_id",
    "slot_key",
    "selector_json",
    "selector_hash",
    "binding_json",
    "input_artifact_id",
    "input_content_hash",
    "revision",
}
_EDGE_FIELDS = {"parent_artifact_id", "child_artifact_id", "relation"}


def _invalid(message: str) -> ValueError:
    return ValueError(message)


def _reference_row(row: object, slots: frozenset[str] = frozenset({"tts"})) -> dict[str, Any]:
    if type(row) is not dict or set(row) != _REFERENCE_FIELDS:
        raise _invalid("Invalid saved TTS reference row.")
    if type(row["session_id"]) is not str or not row["session_id"]:
        raise _invalid("Invalid saved TTS reference owner.")
    if row["slot_key"] not in tuple(slots):
        raise _invalid("Unsupported saved speech reference slot.")
    if type(row["revision"]) is not int or row["revision"] < 1:
        raise _invalid("Invalid saved TTS reference revision.")
    return row


def segment_copy_references(
    rows: Mapping[str, Any],
) -> dict[str, StoredProjectSpeechReference]:
    """Read and validate only saved segment references in current copy rows."""
    if not isinstance(rows, Mapping):
        raise _invalid("Saved project copy rows are invalid.")
    raw_references = rows.get("project_speech_references", [])
    if type(raw_references) is not list:
        raise _invalid("Saved TTS reference rows are invalid.")
    reference_rows = [
        row for row in raw_references
        if type(row) is dict
        and type(row.get("slot_key")) is str
        and row["slot_key"].startswith("segment:")
    ]
    if not reference_rows:
        return {}

    def indexed(name: str) -> dict[str, dict[str, Any]]:
        values = rows.get(name)
        if type(values) is not list:
            raise _invalid("Saved segment reference ownership rows are incomplete.")
        result: dict[str, dict[str, Any]] = {}
        for value in values:
            if (type(value) is not dict or type(value.get("id")) is not str
                    or not value["id"] or value["id"] in result):
                raise _invalid("Saved segment reference ownership rows are invalid.")
            result[value["id"]] = value
        return result

    segments = indexed("generation_segments")
    revisions = indexed("generation_plan_revisions")
    plans = indexed("generation_plans")
    sessions = indexed("sessions")
    projects = indexed("projects")
    result: dict[str, StoredProjectSpeechReference] = {}
    for row in reference_rows:
        slot = normalize_speech_reference_slot(row["slot_key"])
        segment_id = slot[len("segment:"):]
        reference = _reference_row(row, frozenset({slot}))
        if segment_id in result:
            raise _invalid("Saved segment speech reference is duplicated.")
        segment = segments.get(segment_id)
        revision_id = segment.get("plan_revision_id") if segment is not None else None
        revision = revisions.get(revision_id) if type(revision_id) is str else None
        plan_id = revision.get("plan_id") if revision is not None else None
        plan = plans.get(plan_id) if type(plan_id) is str else None
        if (segment is None or revision is None or plan is None
                or plan.get("active_revision_id") != revision_id
                or plan.get("session_id") != reference["session_id"]):
            raise _invalid("Saved segment speech reference is not owned by the current plan.")

        session = sessions.get(reference["session_id"])
        project_id = session.get("project_id") if session is not None else None
        project = projects.get(project_id) if type(project_id) is str else None
        workspace_id = project.get("workspace_id") if project is not None else None
        if (type(project_id) is not str or not project_id
                or type(workspace_id) is not str or not workspace_id):
            raise _invalid("Saved segment speech reference owner is invalid.")
        owner = SpeechInputOwner(workspace_id, project_id, reference["session_id"])

        raw_selector = reference["selector_json"]
        if type(raw_selector) is not dict:
            raise _invalid("Saved segment speech selector is invalid.")
        selector = speech_reference_selector(raw_selector)
        if (selector != raw_selector or type(reference["selector_hash"]) is not str
                or speech_reference_selector_hash(selector) != reference["selector_hash"]):
            raise _invalid("Saved segment speech selector identity is invalid.")

        raw_binding = reference["binding_json"]
        binding = normalize_speech_reference_binding(raw_binding)
        if binding != raw_binding:
            raise _invalid("Saved segment speech binding is not canonical.")
        pin_value = None
        if reference["input_artifact_id"] is not None or reference["input_content_hash"] is not None:
            pin_value = {
                "version": 1,
                "artifact_id": reference["input_artifact_id"],
                "content_hash": reference["input_content_hash"],
            }
        pin = validate_speech_input_pin(pin_value)
        stored = StoredProjectSpeechReference(
            owner=owner,
            slot_key=slot,
            selector=deepcopy(selector),
            selector_hash=reference["selector_hash"],
            binding=deepcopy(binding),
            pin=deepcopy(pin),
            revision=reference["revision"],
        )
        segment_speech_reference_identity(stored)
        result[segment_id] = stored
    return result


def _artifact(
    artifacts: Mapping[str, dict[str, Any]], artifact_id: str
) -> dict[str, Any]:
    matches = [
        (key, value)
        for key, value in artifacts.items()
        if type(value) is dict and value.get("id") == artifact_id
    ]
    if len(matches) != 1 or matches[0][0] != artifact_id:
        raise _invalid("Saved speech input artifact is missing or duplicated.")
    key, value = matches[0]
    if artifacts.get(key) is not value:
        raise _invalid("Saved speech input artifact mapping is inconsistent.")
    return value


def _owned_artifact(
    artifact: dict[str, Any],
    artifact_id: str,
    owner: SpeechInputOwner,
    *,
    kind: str,
    role: str,
    mime_type: str,
    limit: int,
    expected_hash: str | None = None,
    require_hash: bool = False,
) -> None:
    if (
        artifact.get("id") != artifact_id
        or (
            artifact.get("workspace_id"),
            artifact.get("project_id"),
            artifact.get("session_id"),
        )
        != (owner.workspace_id, owner.project_id, owner.session_id)
        or artifact.get("state") != "current"
        or artifact.get("kind") != kind
        or artifact.get("role") != role
        or artifact.get("mime_type") != mime_type
    ):
        raise _invalid("Saved speech input artifact ownership or role is invalid.")
    size = artifact.get("size_bytes")
    if type(size) is not int or size < 0 or size > limit:
        raise _invalid("Saved speech input artifact size is invalid.")
    digest = artifact.get("content_hash")
    if require_hash and (type(digest) is not str or SHA256.fullmatch(digest) is None):
        raise _invalid("Saved speech input audio hash is invalid.")
    if expected_hash is not None and digest != expected_hash:
        raise _invalid("Saved speech input artifact hash disagrees with its pin.")


def _freeze_reference(
    row: object,
    effective_tts: object,
    *,
    owner: SpeechInputOwner,
    artifacts: Mapping[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    slots: frozenset[str],
    cast_binding: VoiceBinding | None = None,
) -> dict[str, Any]:
    """Validate the finite reference and its owned retained-input graph."""
    reference = _reference_row(row, slots)
    if not isinstance(owner, SpeechInputOwner):
        raise _invalid("Invalid saved TTS reference owner.")
    if (
        type(owner.workspace_id) is not str
        or not owner.workspace_id
        or type(owner.project_id) is not str
        or not owner.project_id
        or type(owner.session_id) is not str
        or reference["session_id"] != owner.session_id
    ):
        raise _invalid("Saved TTS reference owner disagrees with its session.")
    if type(effective_tts) is not dict:
        raise _invalid("Saved TTS settings are invalid.")

    selector_json = reference["selector_json"]
    if type(selector_json) is not dict:
        raise _invalid("Saved TTS selector is invalid.")
    selector = speech_reference_selector(selector_json)
    if selector != selector_json:
        raise _invalid("Saved TTS selector is not canonical.")
    if (
        type(reference["selector_hash"]) is not str
        or speech_reference_selector_hash(selector) != reference["selector_hash"]
    ):
        raise _invalid("Saved TTS selector hash disagrees with its selector.")

    binding_json = reference["binding_json"]
    if type(binding_json) is not dict:
        raise _invalid("Saved TTS binding is invalid.")
    binding = normalize_speech_reference_binding(binding_json)
    if binding != binding_json:
        raise _invalid("Saved TTS binding is not canonical.")

    selection = _retained_selection(effective_tts)
    if selection is None:
        raise _invalid("Saved TTS settings do not retain a speech selection.")
    selected_selector, selected = selection
    expected_selector = (cast_binding.model_dump(mode="json")
                         if cast_binding is not None else selected_selector)
    if speech_reference_selector(expected_selector) != selector:
        raise _invalid("Saved TTS selector disagrees with effective settings.")
    if binding["model"] != selected["model"]:
        raise _invalid("Saved TTS binding model disagrees with effective settings.")
    if selector["voice_id"] is None and binding["voice"] != selected["voice"]:
        raise _invalid("Saved TTS binding voice disagrees with effective settings.")

    pin_value = None
    if reference["input_artifact_id"] is not None or reference["input_content_hash"] is not None:
        pin_value = {
            "version": 1,
            "artifact_id": reference["input_artifact_id"],
            "content_hash": reference["input_content_hash"],
        }
    pin = validate_speech_input_pin(pin_value)
    frozen = deepcopy(reference)
    if pin is None:
        return frozen

    if not isinstance(artifacts, Mapping) or type(edges) is not list:
        raise _invalid("Saved speech input graph is invalid.")
    header = _artifact(artifacts, pin["artifact_id"])
    _owned_artifact(
        header,
        pin["artifact_id"],
        owner,
        kind="speech_voice_snapshot",
        role="speech_input",
        mime_type="application/json",
        limit=JSON_LIMIT,
        expected_hash=pin["content_hash"],
    )

    incoming: list[dict[str, Any]] = []
    for edge in edges:
        if type(edge) is not dict or set(edge) != _EDGE_FIELDS:
            raise _invalid("Saved speech input edge row is invalid.")
        if edge["child_artifact_id"] == pin["artifact_id"]:
            incoming.append(edge)
    if len(incoming) != 1 or incoming[0]["relation"] != "speech_reference":
        raise _invalid("Saved speech input must have one owned reference edge.")

    audio_id = incoming[0]["parent_artifact_id"]
    if type(audio_id) is not str or not audio_id:
        raise _invalid("Saved speech input audio artifact is missing.")
    audio = _artifact(artifacts, audio_id)
    _owned_artifact(
        audio,
        audio_id,
        owner,
        kind="audio",
        role="speech_reference_audio",
        mime_type="audio/wav",
        limit=AUDIO_LIMIT,
        require_hash=True,
    )
    return frozen


def _validate_copy_graph(
    rows: Mapping[str, Any], *, include_cast: bool, allow_null_inputs: bool = False,
    allow_segment_references: bool = False,
) -> None:
    """Validate copied TTS references against their source owner and graph."""
    if not isinstance(rows, Mapping):
        raise _invalid("Saved project copy rows are invalid.")
    references = rows.get("project_speech_references", [])
    if type(references) is not list:
        raise _invalid("Saved TTS reference rows are invalid.")
    controls = _controls_by_session(rows) if include_cast else {}
    segment_references = segment_copy_references(rows) if allow_segment_references else {}
    if not references and not controls and not segment_references:
        return

    sessions = rows.get("sessions")
    projects = rows.get("projects")
    settings_rows = rows.get("session_settings")
    artifact_rows = rows.get("artifacts")
    edges = rows.get("artifact_edges")
    if type(sessions) is not list:
        raise _invalid("Saved TTS reference sessions are invalid.")
    if type(projects) is not list:
        raise _invalid("Saved TTS reference projects are invalid.")
    if type(settings_rows) is not list:
        raise _invalid("Saved TTS settings rows are invalid.")
    if type(artifact_rows) is not list:
        raise _invalid("Saved TTS artifact rows are invalid.")
    if type(edges) is not list:
        raise _invalid("Saved TTS reference graph rows are incomplete.")

    settings_by_session: dict[str, list[dict[str, Any]]] = {}
    for settings_row in settings_rows:
        if type(settings_row) is dict and settings_row.get("section") == "tts":
            settings_session = settings_row.get("session_id")
            if type(settings_session) is str:
                settings_by_session.setdefault(settings_session, []).append(settings_row)

    artifact_map: dict[str, dict[str, Any]] = {}
    for artifact in artifact_rows:
        if type(artifact) is not dict or type(artifact.get("id")) is not str:
            raise _invalid("Saved TTS artifact row is invalid.")
        artifact_id = artifact["id"]
        if artifact_id in artifact_map:
            raise _invalid("Saved TTS artifact row is duplicated.")
        artifact_map[artifact_id] = artifact

    seen_references: set[tuple[str, str]] = set()
    segment_slots: dict[tuple[str, str], StoredProjectSpeechReference] = {}
    for stored in segment_references.values():
        session_id = stored.owner.session_id
        if type(session_id) is not str or not session_id:
            raise _invalid("Saved segment speech reference owner is invalid.")
        segment_slots[(session_id, stored.slot_key)] = stored
    for reference_row in references:
        sid = reference_row.get("session_id") if type(reference_row) is dict else None
        cast_bindings = controls.get(sid, {}) if type(sid) is str else {}
        segment_binding = None
        if allow_segment_references and type(reference_row) is dict and type(sid) is str:
            raw_slot = reference_row.get("slot_key")
            if type(raw_slot) is str and raw_slot.startswith("segment:"):
                segment_binding = segment_slots.get((sid, raw_slot))
        allowed_slots = {"tts", *cast_bindings}
        if segment_binding is not None:
            allowed_slots.add(segment_binding.slot_key)
        reference = _reference_row(reference_row, frozenset(allowed_slots))
        key = (reference["session_id"], reference["slot_key"])
        if key in seen_references:
            raise _invalid("Saved TTS reference row is duplicated.")
        seen_references.add(key)

        if segment_binding is not None:
            owner = segment_binding.owner
        else:
            matching_sessions = [
                row for row in sessions
                if type(row) is dict and row.get("id") == reference["session_id"]
            ]
            if len(matching_sessions) != 1:
                raise _invalid("Saved TTS reference session is missing or duplicated.")
            project_id = matching_sessions[0].get("project_id")
            if type(project_id) is not str or not project_id:
                raise _invalid("Saved TTS reference project owner is invalid.")
            matching_projects = [
                row for row in projects if type(row) is dict and row.get("id") == project_id
            ]
            if len(matching_projects) != 1:
                raise _invalid("Saved TTS reference project is missing or duplicated.")
            workspace_id = matching_projects[0].get("workspace_id")
            if type(workspace_id) is not str or not workspace_id:
                raise _invalid("Saved TTS reference workspace owner is invalid.")
            owner = SpeechInputOwner(workspace_id, project_id, reference["session_id"])

        matching_settings = settings_by_session.get(reference["session_id"], [])
        if len(matching_settings) != 1 or "value_json" not in matching_settings[0]:
            raise _invalid("Saved TTS settings row is missing or duplicated.")
        if reference["slot_key"] == "tts":
            frozen = freeze_tts_reference(
                reference,
                matching_settings[0]["value_json"],
                owner=owner,
                artifacts=artifact_map,
                edges=edges,
            )
        elif reference["slot_key"].startswith("cast:"):
            frozen = freeze_cast_reference(
                reference,
                matching_settings[0]["value_json"],
                binding=cast_bindings[reference["slot_key"]],
                owner=owner,
                artifacts=artifact_map,
                edges=edges,
                allow_null_inputs=allow_null_inputs,
            )
        else:
            if segment_binding is None:
                raise _invalid("Saved segment speech reference is missing its current owner.")
            frozen = freeze_segment_reference(
                reference,
                matching_settings[0]["value_json"],
                binding=VoiceBinding.model_validate(segment_binding.selector),
                owner=owner,
                artifacts=artifact_map,
                edges=edges,
                allow_null_inputs=allow_null_inputs,
            )
        if frozen["input_artifact_id"] is None and not allow_null_inputs:
            raise _invalid("Copied TTS references require a retained speech input.")

    expected = {(sid, slot) for sid, bindings in controls.items() for slot in bindings}
    expected.update(segment_slots)
    if {key for key in seen_references if key[1] != "tts"} != expected:
        raise _invalid("Saved cast references are incomplete.")


def _remap_reference(
    row: object,
    identities: Mapping[str, str],
    *,
    slots: frozenset[str],
    allow_null_inputs: bool = False,
) -> dict[str, Any]:
    """Remap only the owner and pin identities of a finite reference."""
    reference = _reference_row(row, slots)
    if not isinstance(identities, Mapping):
        raise _invalid("Saved TTS identity map is invalid.")
    pin = validate_speech_input_pin(
        None
        if reference["input_artifact_id"] is None and reference["input_content_hash"] is None
        else {
            "version": 1,
            "artifact_id": reference["input_artifact_id"],
            "content_hash": reference["input_content_hash"],
        }
    )
    if pin is None and not allow_null_inputs:
        raise _invalid("Copied TTS references require a retained speech input.")
    mapped: dict[str, str] = {}
    source_ids = (reference["session_id"],) if pin is None else (
        reference["session_id"], pin["artifact_id"]
    )
    for source_id in source_ids:
        if source_id not in identities:
            raise _invalid("Saved TTS identity mapping is missing.")
        target_id = identities[source_id]
        if type(target_id) is not str or not target_id:
            raise _invalid("Saved TTS identity mapping is invalid.")
        mapped[source_id] = target_id

    remapped = deepcopy(reference)
    remapped["session_id"] = mapped[reference["session_id"]]
    if pin is not None:
        remapped["input_artifact_id"] = mapped[pin["artifact_id"]]
    remapped["revision"] = 1
    return remapped


def freeze_generation_controls(row: Mapping[str, Any]) -> dict[str, Any]:
    """Canonicalize detached controls without consulting voices or providers."""
    if (row.get("kind") != "generation_controls"
            or row.get("source_language") != "auto" or row.get("target_language") != ""):
        raise _invalid("Invalid generation controls scope.")
    payload = row.get("payload_json")
    if type(payload) is not dict or set(payload) != {"characters", "cast"}:
        raise _invalid("Invalid generation controls fields.")
    raw = payload["characters"]
    if type(raw) is not list or len(raw) > 2000:
        raise _invalid("Invalid character dictionary.")
    characters = [CharacterEntry.model_validate(deepcopy(value)).model_dump(mode="json") for value in raw]
    ids = [value["id"] for value in characters]
    if any(not value for value in ids) or len(set(ids)) != len(ids):
        raise _invalid("Invalid character identities.")
    cast = CastSettings.model_validate(deepcopy(payload["cast"]))
    if set(cast.characters) - set(ids):
        raise _invalid("Cast references undeclared characters.")
    for slot, _binding in iter_cast_voice_bindings(cast):
        normalize_speech_reference_slot(slot)
    return {"characters": characters, "cast": cast.model_dump(mode="json")}


def _controls_by_session(rows: Mapping[str, Any]) -> dict[str, dict[str, VoiceBinding]]:
    controls: dict[str, dict[str, VoiceBinding]] = {}
    for row in rows.get("knowledge_ledgers", []):
        if row.get("kind") != "generation_controls":
            continue
        sid = row.get("session_id")
        if type(sid) is not str or not sid or sid in controls:
            raise _invalid("Invalid generation controls owner.")
        payload = freeze_generation_controls(row)
        controls[sid] = dict(iter_cast_voice_bindings(CastSettings.model_validate(payload["cast"])))
    return controls


def freeze_tts_reference(row: object, effective_tts: object, *, owner: SpeechInputOwner,
                         artifacts: Mapping[str, dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate one retained TTS reference with its legacy semantics."""
    return _freeze_reference(row, effective_tts, owner=owner, artifacts=artifacts,
                             edges=edges, slots=frozenset({"tts"}))


def freeze_cast_reference(row: object, effective_tts: object, *, binding: VoiceBinding,
                          owner: SpeechInputOwner, artifacts: Mapping[str, dict[str, Any]],
                          edges: list[dict[str, Any]],
                          allow_null_inputs: bool = False) -> dict[str, Any]:
    """Validate a cast binding; require retained input unless nulls are opted in."""
    if type(effective_tts) is not dict:
        raise _invalid("Invalid cast speech settings.")
    binding = VoiceBinding.model_validate(binding.model_dump(mode="json"))
    selected = _selected_settings(deepcopy(effective_tts), binding)
    if selected is None:
        raise _invalid("Unsupported cast speech selection.")
    selected["voice_id"] = binding.voice_id
    slot = row.get("slot_key") if type(row) is dict else None
    slot = _cast_slot(slot)
    if not slot.startswith("cast:"):
        raise _invalid("Unsupported cast speech slot.")
    frozen = _freeze_reference(row, selected, owner=owner, artifacts=artifacts,
                              edges=edges, slots=frozenset({slot}), cast_binding=binding)
    if frozen["input_artifact_id"] is None and not allow_null_inputs:
        raise _invalid("Copied cast references require a retained speech input.")
    return frozen


def freeze_segment_reference(
    row: object,
    effective_tts: object,
    *,
    binding: VoiceBinding,
    owner: SpeechInputOwner,
    artifacts: Mapping[str, dict[str, Any]],
    edges: list[dict[str, Any]],
    allow_null_inputs: bool = False,
) -> dict[str, Any]:
    """Validate one current segment binding and its retained-input graph."""
    if type(effective_tts) is not dict:
        raise _invalid("Invalid segment speech settings.")
    binding = VoiceBinding.model_validate(binding.model_dump(mode="json"))
    selected = _selected_settings(deepcopy(effective_tts), binding)
    if selected is None:
        raise _invalid("Unsupported segment speech selection.")
    selected["voice_id"] = binding.voice_id
    slot = row.get("slot_key") if type(row) is dict else None
    slot = _segment_slot(slot)
    frozen = _freeze_reference(
        row,
        selected,
        owner=owner,
        artifacts=artifacts,
        edges=edges,
        slots=frozenset({slot}),
        cast_binding=binding,
    )
    if frozen["input_artifact_id"] is None and not allow_null_inputs:
        raise _invalid("Copied segment references require a retained speech input.")
    return frozen


def validate_tts_copy_graph(
    rows: Mapping[str, Any], *, allow_null_inputs: bool = False
) -> None:
    """Validate the legacy tts-only graph."""
    _validate_copy_graph(rows, include_cast=False, allow_null_inputs=allow_null_inputs)


def validate_speech_copy_graph(
    rows: Mapping[str, Any], *, allow_null_inputs: bool = False,
    allow_segment_references: bool = False,
) -> None:
    """Validate every saved narrator and complete controls-bound cast graph."""
    _validate_copy_graph(
        rows,
        include_cast=True,
        allow_null_inputs=allow_null_inputs,
        allow_segment_references=allow_segment_references,
    )


def remap_tts_reference(
    row: object, identities: Mapping[str, str], *, allow_null_inputs: bool = False
) -> dict[str, Any]:
    """Remap the legacy tts-only reference."""
    return _remap_reference(
        row, identities, slots=frozenset({"tts"}), allow_null_inputs=allow_null_inputs
    )


def remap_cast_reference(
    row: object, identities: Mapping[str, str], *, allow_null_inputs: bool = False
) -> dict[str, Any]:
    """Preserve semantic cast slots and selectors while remapping owner/pin."""
    slot = row.get("slot_key") if type(row) is dict else None
    slot = _cast_slot(slot)
    if not slot.startswith("cast:"):
        raise _invalid("Unsupported cast speech slot.")
    return _remap_reference(
        row, identities, slots=frozenset({slot}), allow_null_inputs=allow_null_inputs
    )


def remap_segment_reference(
    row: object,
    identities: Mapping[str, str],
    *,
    allow_null_inputs: bool = False,
) -> dict[str, Any]:
    """Remap a segment reference owner, slot, and optional retained pin."""
    raw_slot = row.get("slot_key") if type(row) is dict else None
    slot = _segment_slot(raw_slot)
    reference = _reference_row(row, frozenset({slot}))
    segment_id = slot[len("segment:"):]
    if not isinstance(identities, Mapping) or segment_id not in identities:
        raise _invalid("Saved segment identity mapping is missing.")
    mapped_segment_id = identities[segment_id]
    if type(mapped_segment_id) is not str or not mapped_segment_id:
        raise _invalid("Saved segment identity mapping is invalid.")
    mapped_slot = _segment_slot(f"segment:{mapped_segment_id}")
    remapped = _remap_reference(
        reference,
        identities,
        slots=frozenset({slot}),
        allow_null_inputs=allow_null_inputs,
    )
    remapped["slot_key"] = mapped_slot
    return remapped


def _cast_slot(value: object) -> str:
    slot = normalize_speech_reference_slot(value)
    if slot == "cast:narrator":
        return slot
    if not slot.startswith("cast:"):
        raise _invalid("Unsupported cast speech slot.")
    bucket, member = json.loads(slot[5:])
    if bucket == "categories" and member not in {"male", "female", "androgynous", "unspecified"}:
        raise _invalid("Unsupported cast category slot.")
    return slot


def _segment_slot(value: object) -> str:
    slot = normalize_speech_reference_slot(value)
    if not slot.startswith("segment:"):
        raise _invalid("Unsupported segment speech slot.")
    return slot
