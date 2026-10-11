"""Sound-aware facade for direct speech-selection XML editing."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pydantic import BaseModel

from pandrator.logic.speech_sound_markup import insert_sound_markers, split_sound_markup

from . import speech_selection as core
from .settings_policy import RevisionConflict, stable_hash
from .sound_project_commands import write_version
from .sound_project_schemas import SoundAnchor
from .speech_selection_schemas import SpeechSelectionApplyRequest, SpeechSelectionRequest
from .speech_sound_cues import SoundProposal, propose, visible_markers


def _values(request: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(request, BaseModel):
        return request.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return dict(request)


def _source(session, session_id: str, values: dict[str, Any]):
    model = SpeechSelectionRequest.model_validate({
        key: value for key, value in values.items() if key != "expected_preview_revision"
    })
    plan, revision, segment = core._active_segment(
        session, session_id, model.revision_id, model.segment_id,
        model.expected_segment_revision,
    )
    text = str(segment.optimized_text or segment.text or "")
    return model, plan, revision, segment, text


def _prepare(session, session_id: str, values: dict[str, Any]):
    model, _plan, revision, segment, text = _source(session, session_id, values)
    core_values = deepcopy(values)
    core_values.pop("expected_sound_plan_revision", None)
    core_values.pop("expected_preview_revision", None)
    markers = None
    if model.speech_xml is not None:
        core_values["speech_xml"], markers = split_sound_markup(model.speech_xml)
        if not markers and model.expected_sound_plan_revision is None:
            markers = None
    proposal = propose(
        session, session_id, segment.id, revision.id, text, markers,
        values.get("expected_sound_plan_revision"),
    )
    return core_values, proposal, segment.id, text


def _guard(core_guard: str, proposal: SoundProposal) -> str:
    return stable_hash({
        "core_guard": core_guard,
        "sound_plan_revision": proposal.revision,
        "sound_plan_id": proposal.plan.id if proposal.plan is not None else None,
        "speech_revision_id": proposal.active_revision_id,
        "current_sound_cues": proposal.current,
        "candidate_sound_cues": proposal.candidate,
    })


def _derived(speech_xml: str, proposal: SoundProposal, segment_id: str, text: str) -> str:
    return insert_sound_markers(
        speech_xml, visible_markers(proposal.candidate, segment_id, text)
    )


def _concise(
    proposal: SoundProposal, segment_id: str, text: str
) -> tuple[list[dict[str, Any]], int, int]:
    represented = {marker.cue for marker in visible_markers(proposal.current, segment_id, text)}
    cues = sorted(
        (cue for cue in proposal.current if cue["anchor_json"].get("segment_id") == segment_id),
        key=lambda cue: cue["logical_id"],
    )
    public_anchor_fields = SoundAnchor.model_fields.keys()
    concise = [{
        "cue": cue["logical_id"], "variant": cue["variant_id"],
        "anchor": {
            key: deepcopy(value) for key, value in cue["anchor_json"].items()
            if key in public_anchor_fields
        },
        "playback": deepcopy(cue["playback_json"]),
        "included": cue["included"], "placement_status": cue["placement_status"],
        "represented_in_xml": cue["logical_id"] in represented,
    } for cue in cues[:40]]
    return concise, len(cues), sum(cue["logical_id"] not in represented for cue in cues)


def get_speech_selection_context(
    services: Any, session, session_id: str, request: Mapping[str, Any] | Any
) -> dict[str, Any]:
    result = core.get_speech_selection_context(services, session, session_id, request)
    segment_id = result["segment_id"]
    # The context selection may be a subrange; use the complete accepted text.
    segment = session.get(core.m.GenerationSegment, segment_id)
    text = str(segment.optimized_text or segment.text or "")
    proposal = propose(
        session, session_id, segment_id, result["revision_id"], text, None, None
    )
    if result["speech_xml"] is not None:
        result["speech_xml"] = _derived(result["speech_xml"], proposal, segment_id, text)
    reference = deepcopy(result["markup_reference"])
    reference["element_tags"] = sorted([*reference["element_tags"], "sound"])
    reference["element_attributes"]["sound"] = ["cue", "placement", "variant"]
    reference["limits"]["sound_markers"] = 32
    reference["sound"] = {
        "empty": True,
        "required_attributes": ["cue", "variant", "placement"],
        "canonical_uuid_attributes": ["cue", "variant"],
        "placements": ["before", "after", "next_pause"],
        "before": "At the start of the segment only.",
        "after": "After a complete word or at the end of the segment.",
        "next_pause": "After a complete word; playback remains pending.",
    }
    result["markup_reference"] = reference
    result["sound_plan_revision"] = proposal.revision
    result["sound_cues"], result["sound_cue_count"], result["unrepresented_sound_cue_count"] = (
        _concise(proposal, segment_id, text)
    )
    result["sound_cues_truncated"] = result["sound_cue_count"] > len(result["sound_cues"])
    return result


def preview_speech_selection(
    services: Any, session, session_id: str, request: Mapping[str, Any] | Any
) -> dict[str, Any]:
    values = _values(request)
    core_values, proposal, segment_id, text = _prepare(session, session_id, values)
    result = core.preview_speech_selection(services, session, session_id, core_values)
    result["preview_revision"] = _guard(result["preview_revision"], proposal)
    result["speech_xml"] = _derived(result["speech_xml"], proposal, segment_id, text)
    result["sound_plan_revision"] = proposal.revision
    result["sound_changes"] = proposal.changes
    return result


def apply_speech_selection(
    services: Any, session, session_id: str, request: Mapping[str, Any] | Any
) -> dict[str, Any]:
    values = _values(request)
    SpeechSelectionApplyRequest.model_validate(values)
    core_values, proposal, segment_id, text = _prepare(session, session_id, values)
    preview = core.preview_speech_selection(services, session, session_id, core_values)
    if values["expected_preview_revision"].lower() != _guard(preview["preview_revision"], proposal):
        raise RevisionConflict("The speech and sound preview is stale. Review it again.")
    core_model, _ = core._validated_request(core_values, apply=False)
    state = core._resolve_state(services, session, session_id, core_model, core_values)
    speech_changed = (
        state.candidate_xml != state.source_xml
        or core_values.get("speaker", "unchanged") != "unchanged"
        or any(
            core_values.get(request_key) and not state.tts.get(setting_key)
            for request_key, setting_key in (
                ("enable_casting", "casting_enabled"),
                ("enable_performance", "performance_enabled"),
                ("enable_vocalizations", "performance_allow_vocalizations"),
            )
        )
    )
    if speech_changed:
        result = core.apply_speech_selection(
            services, session, session_id,
            {**core_values, "expected_preview_revision": preview["preview_revision"]},
        )
    else:
        from .source_management import assert_session_idle

        assert_session_idle(session, session_id)
        result = {
            "adopted": (
                {"id": state.adopted.id, "version": state.adopted.version,
                 "plan_revision_id": state.adopted.plan_revision_id,
                 "status": state.adopted.status}
                if state.adopted else None
            ),
            "selection": core._selection_response(state),
            "speech_xml": state.source_xml,
            "source_performance_plan_id": state.adopted.id if state.adopted else None,
            "current_plan_revision": state.revision.id,
            "current_plan_revision_id": state.revision.id,
            "performance_plan": (
                {"id": state.adopted.id, "version": state.adopted.version,
                 "plan_revision_id": state.adopted.plan_revision_id,
                 "status": state.adopted.status}
                if state.adopted else None
            ),
            "current_flags": {
                "casting_enabled": bool(state.tts.get("casting_enabled")),
                "performance_enabled": bool(state.tts.get("performance_enabled")),
                "allow_vocalizations": bool(state.tts.get("performance_allow_vocalizations")),
            },
        }
    revision = proposal.revision
    if proposal.changed:
        plan = write_version(
            session, session_id, proposal.plan, proposal.active_revision_id,
            proposal.candidate,
        )
        revision = plan.revision
    result["speech_xml"] = _derived(result["speech_xml"], proposal, segment_id, text)
    result["sound_plan_revision"] = revision
    result["sound_changes"] = proposal.changes
    return result


__all__ = ["apply_speech_selection", "get_speech_selection_context", "preview_speech_selection"]
