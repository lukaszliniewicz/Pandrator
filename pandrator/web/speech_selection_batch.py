"""Compile and atomically adopt a bounded group of speech selections."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from pandrator.logic.speech_sound_markup import insert_sound_markers, split_sound_markup

from . import models as m
from . import performance_plans as plans
from . import speech_selection as core
from .generation_controls import get_generation_controls
from .performance_schemas import PerformancePlanCreateRequest
from .settings_policy import RevisionConflict, stable_hash
from .sound_project_commands import write_version
from .source_management import assert_session_idle
from .speech_plan_preview import _runtime_settings
from .speech_plan_workspace import plan_signature
from .speech_selection_batch_schemas import (
    SpeechSelectionBatchApplyRequest,
    SpeechSelectionBatchRequest,
)
from .speech_sound_cues import load_source_state, propose, visible_markers


@dataclass(slots=True)
class _BatchState:
    revision_id: str
    adopted: m.PerformancePlan | None
    characters: list[dict[str, Any]]
    controls_revision: int
    signature: str
    tts: dict[str, Any]
    audio: dict[str, Any]
    runtime: dict[str, Any]
    segments: dict[str, m.GenerationSegment]
    texts: dict[str, str]
    sources: dict[str, str]
    candidates: dict[str, str]
    records: dict[str, dict[str, Any] | None]
    sound_plan: Any
    sound_revision: int
    sound_current: list[dict[str, Any]]
    sound_candidate: list[dict[str, Any]]
    sound_changes: dict[str, list[str]]
    guard: str


def _fields(change: Mapping[str, Any]) -> set[str]:
    if change.get("speech_xml") is not None:
        return {"xml"}
    result = set()
    if change.get("speaker", "unchanged") != "unchanged" or "voice" in change:
        result.add("voice_speaker")
    if "voice_category" in change:
        result.add("voice_speaker")
    result.update(f"delivery.{key}" for key in (change.get("delivery") or {}))
    if change.get("vocal_event") is not None:
        result.add("vocal_event")
    return result


def _validate_collisions(changes: list[dict[str, Any]]) -> None:
    by_segment: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for change in changes:
        by_segment[change["segment_id"]].append(change)
    for segment_changes in by_segment.values():
        if len(segment_changes) > 1 and any(
            item.get("speech_xml") is not None for item in segment_changes
        ):
            raise ValueError("speech_xml must be the only change for its segment.")
        revision = segment_changes[0]["expected_segment_revision"]
        if any(item["expected_segment_revision"] != revision for item in segment_changes):
            raise RevisionConflict("Repeated selections must use one segment revision.")
        previous: list[tuple[int, int, set[str], int | None]] = []
        for item in segment_changes:
            start, end = item["start"], item["end"]
            fields = _fields(item)
            cursor = (
                start if start == end and "vocal_event" in fields else None
            )
            for other_start, other_end, other_fields, other_cursor in previous:
                if cursor is not None and cursor == other_cursor:
                    raise ValueError("Duplicate vocal events at one cursor are ambiguous.")
                if (
                    cursor is None and other_cursor is None
                    and start < other_end and other_start < end
                    and fields & other_fields
                ):
                    raise ValueError("Overlapping speech selections change the same field.")
            previous.append((start, end, fields, cursor))


def _resolve(services: Any, session, session_id: str, values: dict[str, Any]) -> _BatchState:
    changes = values["changes"]
    _validate_collisions(changes)
    revision_id = changes[0]["revision_id"]
    if session.get(m.SessionRecord, session_id) is None:
        raise KeyError(session_id)
    plan = session.scalar(select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id))
    revision = session.get(m.GenerationPlanRevision, revision_id)
    if plan is None or revision is None or revision.plan_id != plan.id or plan.active_revision_id != revision_id:
        raise RevisionConflict("The selected speech plan changed. Refresh the active revision.")
    ids = list(dict.fromkeys(item["segment_id"] for item in changes))
    segments = {
        row.id: row for row in session.scalars(
            select(m.GenerationSegment).where(m.GenerationSegment.id.in_(ids))
        )
    }
    for item in changes:
        segment = segments.get(item["segment_id"])
        if segment is None or segment.plan_revision_id != revision_id or segment.removed:
            raise KeyError(item["segment_id"])
        if segment.revision != item["expected_segment_revision"]:
            raise RevisionConflict("The selected speech block changed. Refresh it before editing.")
    controls = get_generation_controls(session, session_id)
    characters = deepcopy(controls.get("characters") or [])
    controls_revision = int(controls.get("revision") or 0)
    signature = plan_signature(session, revision_id)
    adopted = core._current_adopted(session, revision_id, signature)
    annotations = plans._annotations(session, adopted) if adopted else {}
    workspace = core._service(services, "workspace_settings")
    tts = deepcopy(workspace.get_in_session(session, session_id, "tts")["effective"])
    audio = deepcopy(workspace.get_in_session(session, session_id, "audio")["effective"])
    runtime = _runtime_settings({"audio": audio, "tts": tts})
    runtime["casting_enabled"] = bool(
        runtime.get("casting_enabled") or any(item.get("enable_casting") for item in changes)
    )
    runtime["_preview_performance_enabled"] = bool(
        runtime.get("performance_enabled") or any(item.get("enable_performance") for item in changes)
    )
    runtime["performance_allow_vocalizations"] = bool(
        runtime.get("performance_allow_vocalizations")
        or any(item.get("enable_vocalizations") for item in changes)
    )
    texts: dict[str, str] = {}
    sources: dict[str, str] = {}
    candidates: dict[str, str] = {}
    records: dict[str, dict[str, Any] | None] = {}
    for segment_id in ids:
        segment = segments[segment_id]
        text = str(segment.optimized_text or segment.text or "")
        if not text:
            raise ValueError("The selected speech block is empty.")
        texts[segment_id] = text
        records[segment_id] = annotations.get(segment_id)
        sources[segment_id] = core._source_markup(segment, records[segment_id], characters, text)
        candidates[segment_id] = sources[segment_id]

    sound_source = load_source_state(session, session_id)
    base_sound = propose(
        session, session_id, ids[0], revision_id, texts[ids[0]], None, None,
        source_state=sound_source,
    )
    current_by_id = {cue["logical_id"]: cue for cue in base_sound.current}
    merged = deepcopy(current_by_id)
    touched_cues: set[str] = set()
    for item in changes:
        segment_id = item["segment_id"]
        text = texts[segment_id]
        if item["end"] > len(text):
            raise ValueError("The selected speech range exceeds the accepted text.")
        edit = deepcopy(item)
        edit.pop("expected_sound_plan_revision", None)
        edit.pop("expected_preview_revision", None)
        markers = None
        if item.get("speech_xml") is not None:
            edit["speech_xml"], markers = split_sound_markup(item["speech_xml"])
        candidates[segment_id] = core._candidate_markup(
            source_xml=candidates[segment_id], segment=segments[segment_id],
            text=text, characters=characters, values=edit,
        )
        if markers is not None:
            proposal = propose(
                session, session_id, segment_id, revision_id, text, markers,
                item.get("expected_sound_plan_revision"),
                source_state=sound_source,
            )
            changed_ids = set().union(*proposal.changes.values())
            if touched_cues & changed_ids:
                raise ValueError("Multiple speech selections change the same sound cue.")
            touched_cues.update(changed_ids)
            candidate_by_id = {cue["logical_id"]: cue for cue in proposal.candidate}
            for cue_id in changed_ids:
                if cue_id in candidate_by_id:
                    merged[cue_id] = deepcopy(candidate_by_id[cue_id])
                else:
                    merged.pop(cue_id, None)
    sound_candidate = sorted(merged.values(), key=lambda cue: cue["logical_id"])
    before, after = set(current_by_id), set(merged)
    sound_changes = {
        "added": sorted(after - before),
        "removed": sorted(before - after),
        "updated": sorted(key for key in before & after if current_by_id[key] != merged[key]),
    }
    guard = stable_hash({
        "revision_id": revision_id,
        "segment_revisions": {key: segments[key].revision for key in ids},
        "source_xml": sources,
        "candidate_xml": candidates,
        "base_signature": signature,
        "source_performance_plan": (
            (adopted.id, adopted.version) if adopted else None
        ),
        "character_controls_revision": controls_revision,
        "tts": tts, "audio": audio,
        "changes": [
            {key: value for key, value in item.items() if key != "unlock_locked"}
            for item in changes
        ],
        "sound_plan_id": base_sound.plan.id if base_sound.plan else None,
        "sound_plan_revision": base_sound.revision,
        "sound_current": base_sound.current,
        "sound_candidate": sound_candidate,
    })
    return _BatchState(
        revision_id, adopted, characters, controls_revision, signature, tts, audio,
        runtime, segments, texts, sources, candidates, records, base_sound.plan,
        base_sound.revision, base_sound.current, sound_candidate, sound_changes, guard,
    )


def _compile(services: Any, session, session_id: str, state: _BatchState, detailed: bool):
    ids = list(state.candidates)
    transient = core._transient_plan(
        session, session_id, state.revision_id, ids[0], state.candidates[ids[0]],
        state.characters, state.controls_revision, state.signature, state.runtime,
    )
    for unit in transient.units_json:
        if unit["id"] in state.candidates:
            unit["speech_xml"] = state.candidates[unit["id"]]
    summaries = []
    details = []
    profile = None
    prepared = plans._prepare_previews(session, transient, state.runtime, ids)
    for segment_id in ids:
        compiled = plans.preview_segment(
            session, transient, segment_id, dict(state.runtime),
            speech_xml=state.candidates[segment_id],
            _prepared=prepared,
        )
        if profile is None:
            profile = compiled.get("capabilities")
        summaries.append({
            "segment_id": segment_id,
            "fingerprint": compiled.get("fingerprint"),
            "part_count": len(compiled.get("parts") or []),
            "report": compiled.get("report") or [],
        })
        if detailed and len(details) < 10:
            details.append({
                **compiled,
                "speech_xml": insert_sound_markers(
                    state.candidates[segment_id],
                    visible_markers(state.sound_candidate, segment_id, state.texts[segment_id]),
                ),
            })
    return summaries, details, profile


def preview_speech_selection_batch(
    services: Any, session, session_id: str,
    request: Mapping[str, Any] | SpeechSelectionBatchRequest,
) -> dict[str, Any]:
    model = (
        request if isinstance(request, SpeechSelectionBatchRequest)
        else SpeechSelectionBatchRequest.model_validate(request)
    )
    values = model.model_dump(mode="json", by_alias=True, exclude_unset=True)
    state = _resolve(services, session, session_id, values)
    summaries, details, profile = _compile(
        services, session, session_id, state, model.detailed_preview
    )
    return {
        "preview_revision": state.guard,
        "revision_id": state.revision_id,
        "source_performance_plan_id": state.adopted.id if state.adopted else None,
        "sound_plan_revision": state.sound_revision,
        "sound_changes": state.sound_changes,
        "affected_segment_ids": list(state.candidates),
        "segments": [
            {"segment_id": segment_id,
             "speech_xml_hash": stable_hash(state.candidates[segment_id]),
             "selection_count": sum(
                 item["segment_id"] == segment_id for item in values["changes"]
             ),
             "locked": bool((state.records[segment_id] or {}).get("locked"))}
            for segment_id in state.candidates
        ],
        "current_flags": {
            "casting_enabled": bool(state.tts.get("casting_enabled")),
            "performance_enabled": bool(state.tts.get("performance_enabled")),
            "allow_vocalizations": bool(state.tts.get("performance_allow_vocalizations")),
        },
        "compilation": summaries,
        "capabilities": profile,
        "details": details,
        "details_truncated": model.detailed_preview and len(state.candidates) > 10,
        "compilation_only": True,
    }


def apply_speech_selection_batch(
    services: Any, session, session_id: str,
    request: Mapping[str, Any] | SpeechSelectionBatchApplyRequest,
) -> dict[str, Any]:
    model = (
        request if isinstance(request, SpeechSelectionBatchApplyRequest)
        else SpeechSelectionBatchApplyRequest.model_validate(request)
    )
    values = model.model_dump(mode="json", by_alias=True, exclude_unset=True)
    state = _resolve(services, session, session_id, values)
    if model.expected_preview_revision.lower() != state.guard:
        raise RevisionConflict("The speech selection batch preview is stale. Review it again.")
    assert_session_idle(session, session_id)
    for item in values["changes"]:
        if (state.records[item["segment_id"]] or {}).get("locked") and not item.get("unlock_locked"):
            raise RevisionConflict("Explicitly unlock the protected manual annotation before changing it.")
    request_plan = PerformancePlanCreateRequest(
        expected_plan_revision_id=state.revision_id,
        mode="manual", annotation_format="xml",
        context_before=int(state.runtime.get("performance_context_before", 2)),
        context_after=int(state.runtime.get("performance_context_after", 1)),
        context_max_chars=int(state.runtime.get("performance_context_max_chars", 4000)),
        copy_from_id=state.adopted.id if state.adopted else None,
    )
    new_plan = plans.create_plan(session, session_id, request_plan, state.tts)
    edited = plans.edit_annotations(
        session, new_plan, expected_version=new_plan.version,
        items=[
            {"segment_id": segment_id, "speech_xml": state.candidates[segment_id], "locked": True}
            for segment_id in state.candidates
        ],
        unlock_locked=True,
    )
    adopted = plans.adopt_plan(
        session, new_plan, expected_version=edited["version"], accept_unanalysed=True,
    )
    patch = {
        setting: True
        for request_key, setting in (
            ("enable_casting", "casting_enabled"),
            ("enable_performance", "performance_enabled"),
            ("enable_vocalizations", "performance_allow_vocalizations"),
        )
        if any(item.get(request_key) for item in values["changes"])
        and state.tts.get(setting) is not True
    }
    workspace = core._service(services, "workspace_settings")
    if patch:
        current = workspace.get_in_session(session, session_id, "tts")
        workspace.patch_in_session(session, session_id, "tts", int(current["revision"]), patch)
    flags = workspace.get_in_session(session, session_id, "tts")["effective"]
    sound_revision = state.sound_revision
    if state.sound_candidate != state.sound_current:
        sound_revision = write_version(
            session, session_id, state.sound_plan, state.revision_id, state.sound_candidate,
        ).revision
    return {
        "adopted": adopted,
        "performance_plan": {
            "id": new_plan.id, "version": new_plan.version,
            "plan_revision_id": new_plan.plan_revision_id, "status": new_plan.status,
        },
        "source_performance_plan_id": state.adopted.id if state.adopted else None,
        "current_plan_revision_id": state.revision_id,
        "affected_segment_ids": list(state.candidates),
        "sound_plan_revision": sound_revision,
        "sound_changes": state.sound_changes,
        "current_flags": {
            "casting_enabled": bool(flags.get("casting_enabled")),
            "performance_enabled": bool(flags.get("performance_enabled")),
            "allow_vocalizations": bool(flags.get("performance_allow_vocalizations")),
        },
    }
