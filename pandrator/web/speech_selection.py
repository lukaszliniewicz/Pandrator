"""Preview and atomically apply direct speech-markup range edits."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from pandrator.logic.speech_markup import (
    parse_speech_markup,
    plain_speech_markup,
    speech_markup_reference,
)
from pandrator.logic.speech_markup_edits import (
    edit_speech_markup_range,
    insert_speech_markup_event,
)
from pandrator.logic.speech_performance import resolve_capabilities

from . import models as m
from . import performance_plans as plans
from .generation_controls import get_generation_controls
from .performance_schemas import PerformancePlanCreateRequest
from .settings_policy import RevisionConflict, stable_hash
from .source_management import assert_session_idle
from .speech_annotation_records import record_markup
from .speech_plan_preview import _runtime_settings, preview_speech_segment_in_session
from .speech_plan_workspace import plan_signature
from .speech_selection_schemas import (
    SpeechSelectionApplyRequest,
    SpeechSelectionContextRequest,
    SpeechSelectionRequest,
)

_STRING_DELIVERY_FIELDS = ("instruction", "emotion", "pace", "cadence", "emphasis")
_DELIVERY_FIELDS = (*_STRING_DELIVERY_FIELDS, "emotion_intensity")
_MIXED_FIELDS = ("speaker", "voice", "voice_category", *_DELIVERY_FIELDS)


def _empty_delivery() -> dict[str, Any]:
    return {
        **dict.fromkeys(_STRING_DELIVERY_FIELDS, ""),
        "emotion_intensity": None,
    }


def _service(services: Any, key: str) -> Any:
    if isinstance(services, Mapping):
        return services[key]
    return getattr(services, key)


@dataclass(frozen=True, slots=True)
class _SelectionState:
    plan: m.GenerationPlan
    revision: m.GenerationPlanRevision
    segment: m.GenerationSegment
    text: str
    characters: list[dict[str, Any]]
    controls_revision: int
    source_xml: str
    candidate_xml: str
    adopted: m.PerformancePlan | None
    adopted_record: dict[str, Any] | None
    signature: str
    tts: dict[str, Any]
    audio: dict[str, Any]
    runtime: dict[str, Any]
    values: dict[str, Any]
    guard: str

    @property
    def locked(self) -> bool:
        return bool((self.adopted_record or {}).get("locked"))


def _validated_request(request: Mapping[str, Any], *, apply: bool):
    model = (
        request
        if isinstance(request, SpeechSelectionApplyRequest if apply else SpeechSelectionRequest)
        else (
            SpeechSelectionApplyRequest.model_validate(request)
            if apply
            else SpeechSelectionRequest.model_validate(request)
        )
    )
    values = model.model_dump(mode="json", by_alias=True, exclude_unset=True)
    return model, values


def _active_segment(
    session, session_id: str, revision_id: str, segment_id: str, expected_revision: int
) -> tuple[m.GenerationPlan, m.GenerationPlanRevision, m.GenerationSegment]:
    if session.get(m.SessionRecord, session_id) is None:
        raise KeyError(session_id)
    plan = session.scalar(
        select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id)
    )
    revision = session.get(m.GenerationPlanRevision, revision_id)
    if (
        plan is None
        or revision is None
        or revision.plan_id != plan.id
        or plan.active_revision_id != revision_id
    ):
        raise RevisionConflict(
            "The selected speech plan changed. Refresh the active revision before editing."
        )
    segment = session.get(m.GenerationSegment, segment_id)
    if (
        segment is None
        or segment.plan_revision_id != revision_id
        or segment.removed
    ):
        raise KeyError(segment_id)
    if segment.revision != expected_revision:
        raise RevisionConflict(
            "The selected speech block changed. Refresh it before editing."
        )
    return plan, revision, segment


def _current_adopted(
    session, revision_id: str, signature: str
) -> m.PerformancePlan | None:
    adopted = session.scalar(
        select(m.PerformancePlan).where(
            m.PerformancePlan.plan_revision_id == revision_id,
            m.PerformancePlan.status == "adopted",
        )
    )
    if adopted is not None:
        # This check is required even when both runtime flags are disabled:
        # authored adopted markup remains the source for a direct edit.
        plans._assert_current(session, adopted)
        if adopted.base_signature != signature:
            raise RevisionConflict(
                "The adopted performance plan is stale after a speech edit."
            )
    return adopted


def _source_markup(
    segment: m.GenerationSegment,
    adopted_record: Mapping[str, Any] | None,
    characters: list[dict[str, Any]],
    text: str,
) -> str:
    xml = record_markup(adopted_record)
    if not isinstance(xml, str) or not xml:
        if adopted_record and (
            any((adopted_record.get("delivery") or {}).values())
            or adopted_record.get("spans")
            or adopted_record.get("events")
        ):
            raise ValueError(
                "This block has legacy structured directions. Use the Directions "
                "editor to review them; phrase editing cannot safely convert "
                "these directions to speech XML. No changes were made."
            )
        raw = (segment.speech_plan_json or {}).get("speech_xml")
        xml = raw if isinstance(raw, str) and raw else plain_speech_markup(segment.id, text)
    return parse_speech_markup(
        xml,
        expected_segment_id=segment.id,
        expected_text=text,
        characters=characters,
    ).xml


def _request_changes(values: Mapping[str, Any]) -> dict[str, Any]:
    # ``unlock_locked`` changes permission, not the proposed markup.  It must
    # therefore not invalidate a preview that the user is reviewing.
    return {
        key: deepcopy(value)
        for key, value in values.items()
        if key not in {"expected_preview_revision", "unlock_locked"}
    }


def _selection_guard(
    *,
    revision_id: str,
    segment_id: str,
    segment_revision: int,
    source_xml: str,
    signature: str,
    adopted: m.PerformancePlan | None,
    controls_revision: int,
    tts: Mapping[str, Any],
    audio: Mapping[str, Any],
    values: Mapping[str, Any],
) -> str:
    return stable_hash(
        {
            "revision_id": revision_id,
            "segment_id": segment_id,
            "segment_revision": segment_revision,
            "source_xml": source_xml,
            "base_signature": signature,
            "source_performance_plan_id": adopted.id if adopted else None,
            "source_performance_plan_version": adopted.version if adopted else None,
            "character_controls_revision": controls_revision,
            "tts": dict(tts),
            "audio": dict(audio),
            "changes": _request_changes(values),
        }
    )


def _candidate_markup(
    *,
    source_xml: str,
    segment: m.GenerationSegment,
    text: str,
    characters: list[dict[str, Any]],
    values: Mapping[str, Any],
) -> str:
    if values.get("speech_xml") is not None:
        if values["start"] != 0 or values["end"] != len(text):
            raise ValueError("speech_xml requires the complete speech block range.")
        source = parse_speech_markup(
            source_xml, expected_segment_id=segment.id,
            expected_text=text, characters=characters,
        )
        candidate = parse_speech_markup(
            values["speech_xml"], expected_segment_id=segment.id,
            expected_text=text, characters=characters,
        )
        if candidate.boundary_after != source.boundary_after:
            raise ValueError(
                "speech_xml cannot change boundary_after; use block boundary controls."
            )
        return candidate.xml
    kwargs: dict[str, Any] = {
        "segment_id": segment.id,
        "text": text,
        "characters": characters,
        "start": values["start"],
        "end": values["end"],
        "speaker": values.get("speaker", "unchanged"),
        "character_id": values.get("character_id"),
        "voice_category": values.get("voice_category"),
        "delivery": values.get("delivery"),
    }
    if "voice" in values:
        kwargs["voice"] = values["voice"]
    candidate_xml = (
        edit_speech_markup_range(source_xml, **kwargs)
        if values.get("speaker", "unchanged") != "unchanged"
        or "voice" in values
        or values.get("delivery") is not None
        else source_xml
    )
    cue = values.get("vocal_event")
    if cue is not None:
        return insert_speech_markup_event(
            candidate_xml,
            segment_id=segment.id,
            text=text,
            characters=characters,
            offset=values["start"] if cue.get("position", "before") == "before" else values["end"],
            kind=cue["kind"],
            duration_ms=cue.get("duration_ms"),
            affinity="following" if cue.get("position", "before") == "before" else "previous",
        )
    return candidate_xml


def _transient_plan(
    session,
    session_id: str,
    revision_id: str,
    target_segment_id: str,
    candidate_xml: str,
    characters: list[dict[str, Any]],
    controls_revision: int,
    signature: str,
    runtime: Mapping[str, Any],
) -> m.PerformancePlan:
    units: list[dict[str, Any]] = []
    rows = session.scalars(
        select(m.GenerationSegment)
        .where(
            m.GenerationSegment.plan_revision_id == revision_id,
            m.GenerationSegment.removed.is_(False),
        )
        .order_by(m.GenerationSegment.ordinal)
    )
    for row in rows:
        spoken = str(row.optimized_text or row.text or "")
        provenance = row.speech_block_provenance_json or {}
        unit = {
            "id": row.id,
            "text": spoken,
            "spoken_text": spoken,
            "speaker": row.speaker or "",
            "language": row.language or "",
            "node_kind": row.node_kind,
            "section_id": str(provenance.get("section_id") or ""),
            "ordinal": row.ordinal,
            "voice": row.voice or "",
            "paragraph_break_after": row.paragraph_break_after,
            "timing": {
                key: provenance[key]
                for key in (
                    "start_ms", "end_ms", "target_duration_ms", "anchor_start_ms",
                    "anchor_end_ms", "duration_ms", "start", "end", "budget_ms",
                )
                if key in provenance
            },
        }
        raw_xml = (row.speech_plan_json or {}).get("speech_xml")
        unit["speech_xml"] = (
            raw_xml
            if isinstance(raw_xml, str) and raw_xml
            else plain_speech_markup(row.id, spoken)
        )
        if row.id == target_segment_id:
            unit["speech_xml"] = candidate_xml
        units.append(unit)
    context_before = runtime.get("performance_context_before", 2)
    context_after = runtime.get("performance_context_after", 1)
    context_max = runtime.get("performance_context_max_chars", 4000)
    settings = {
        "annotation_format": "xml",
        "character_dictionary": deepcopy(characters),
        "character_dictionary_revision": controls_revision,
        "context_before": context_before,
        "context_after": context_after,
        "context_max_chars": context_max,
        "workflow_kind": "selection_preview",
    }
    return m.PerformancePlan(
        id=f"selection-preview-{uuid.uuid4().hex}",
        session_id=session_id,
        plan_revision_id=revision_id,
        base_signature=signature,
        settings_json=settings,
        units_json=units,
        manual_annotations_json={},
        status="draft",
    )


def _preview_compilation(
    services: Any,
    session,
    state: _SelectionState,
) -> dict[str, Any]:
    transient = _transient_plan(
        session,
        state.plan.session_id,
        state.revision.id,
        state.segment.id,
        state.candidate_xml,
        state.characters,
        state.controls_revision,
        state.signature,
        state.runtime,
    )
    return plans.preview_segment(
        session,
        transient,
        state.segment.id,
        dict(state.runtime),
        speech_xml=state.candidate_xml,
    )


def _resolve_state(
    services: Any,
    session,
    session_id: str,
    model: SpeechSelectionRequest | SpeechSelectionApplyRequest,
    values: dict[str, Any],
) -> _SelectionState:
    plan, revision, segment = _active_segment(
        session,
        session_id,
        model.revision_id,
        model.segment_id,
        model.expected_segment_revision,
    )
    text = str(segment.optimized_text or segment.text or "")
    if not text:
        raise ValueError("The selected speech block is empty.")
    if model.end > len(text):
        raise ValueError("The selected speech range exceeds the accepted text.")
    controls = get_generation_controls(session, session_id)
    characters = deepcopy(controls.get("characters") or [])
    signature = plan_signature(session, revision.id)
    adopted = _current_adopted(session, revision.id, signature)
    adopted_record = (
        plans._annotations(session, adopted).get(segment.id) if adopted else None
    )
    source_xml = _source_markup(segment, adopted_record, characters, text)
    candidate_xml = _candidate_markup(
        source_xml=source_xml,
        segment=segment,
        text=text,
        characters=characters,
        values=values,
    )
    workspace = _service(services, "workspace_settings")
    tts_snapshot = workspace.get_in_session(session, session_id, "tts")
    audio_snapshot = workspace.get_in_session(session, session_id, "audio")
    tts = deepcopy(tts_snapshot["effective"])
    audio = deepcopy(audio_snapshot["effective"])
    runtime = _runtime_settings({"audio": audio, "tts": tts})
    runtime["casting_enabled"] = bool(
        runtime.get("casting_enabled") or values.get("enable_casting", False)
    )
    runtime["_preview_performance_enabled"] = bool(
        runtime.get("performance_enabled") or values.get("enable_performance", False)
    )
    runtime["performance_allow_vocalizations"] = bool(
        runtime.get("performance_allow_vocalizations")
        or values.get("enable_vocalizations", False)
    )
    guard = _selection_guard(
        revision_id=revision.id,
        segment_id=segment.id,
        segment_revision=segment.revision,
        source_xml=source_xml,
        signature=signature,
        adopted=adopted,
        controls_revision=int(controls.get("revision") or 0),
        tts=tts,
        audio=audio,
        values=values,
    )
    return _SelectionState(
        plan=plan,
        revision=revision,
        segment=segment,
        text=text,
        characters=characters,
        controls_revision=int(controls.get("revision") or 0),
        source_xml=source_xml,
        candidate_xml=candidate_xml,
        adopted=adopted,
        adopted_record=adopted_record,
        signature=signature,
        tts=tts,
        audio=audio,
        runtime=runtime,
        values=values,
        guard=guard,
    )


def _selection_response(state: _SelectionState) -> dict[str, Any]:
    return {
        "start": state.values["start"],
        "end": state.values["end"],
        "text": state.text[state.values["start"] : state.values["end"]],
    }


def _selection_spans(parsed, start: int, end: int) -> list[Any]:
    if start == end:
        if not parsed.transcript:
            raise ValueError("The selected speech block is empty.")
        if start == len(parsed.transcript):
            selected = [span for span in parsed.spans if span.start < start <= span.end]
        else:
            selected = [span for span in parsed.spans if span.start <= start < span.end]
    else:
        selected = [
            span for span in parsed.spans if span.start < end and span.end > start
        ]
    if not selected:
        raise ValueError("The selected speech position does not overlap accepted text.")
    return selected


def _selection_current(parsed, start: int, end: int) -> dict[str, Any]:
    selected = _selection_spans(parsed, start, end)
    fields: dict[str, list[Any]] = {key: [] for key in _MIXED_FIELDS}
    for span in selected:
        speaker = (
            "character" if span.speaker_id else
            "narrator" if span.narrator or not span.dialogue else "dialogue"
        )
        fields["speaker"].append((speaker, span.speaker_id))
        fields["voice"].append(span.voice)
        fields["voice_category"].append(
            span.voice_category if speaker == "dialogue" else None
        )
        for field in _DELIVERY_FIELDS:
            value = span.delivery.get(field)
            fields[field].append(
                value if field == "emotion_intensity" else value or ""
            )
    mixed = [key for key in _MIXED_FIELDS if len(set(fields[key])) > 1]
    common_speaker = fields["speaker"][0] if "speaker" not in mixed else ("mixed", None)
    return {
        "speaker": common_speaker[0],
        "character_id": common_speaker[1],
        "voice_category": fields["voice_category"][0] if "voice_category" not in mixed else None,
        "voice": fields["voice"][0] if "voice" not in mixed else None,
        "delivery": {
            field: fields[field][0] if field not in mixed else None
            for field in _DELIVERY_FIELDS
        },
        "mixed_fields": mixed,
    }


def get_speech_selection_context(
    services: Any,
    session,
    session_id: str,
    request: Mapping[str, Any] | SpeechSelectionContextRequest,
) -> dict[str, Any]:
    """Inspect effective authored directions without creating an edit candidate."""

    model = (
        request if isinstance(request, SpeechSelectionContextRequest)
        else SpeechSelectionContextRequest.model_validate(request)
    )
    _plan, revision, segment = _active_segment(
        session, session_id, model.revision_id, model.segment_id,
        model.expected_segment_revision,
    )
    text = str(segment.optimized_text or segment.text or "")
    if not text:
        raise ValueError("The selected speech block is empty.")
    if model.end > len(text):
        raise ValueError("The selected speech range exceeds the accepted text.")
    controls = get_generation_controls(session, session_id)
    characters = deepcopy(controls.get("characters") or [])
    adopted = _current_adopted(session, revision.id, plan_signature(session, revision.id))
    adopted_record = (
        plans._annotations(session, adopted).get(segment.id) if adopted else None
    )
    legacy = bool(
        adopted_record and not record_markup(adopted_record) and (
            any((adopted_record.get("delivery") or {}).values())
            or adopted_record.get("spans") or adopted_record.get("events")
        )
    )
    if legacy:
        raw_xml = (segment.speech_plan_json or {}).get("speech_xml")
        source_xml = raw_xml if isinstance(raw_xml, str) and raw_xml else plain_speech_markup(segment.id, text)
    else:
        source_xml = _source_markup(segment, adopted_record, characters, text)
    parsed = parse_speech_markup(
        source_xml, expected_segment_id=segment.id, expected_text=text,
        characters=characters,
    )
    current = _selection_current(parsed, model.start, model.end)
    selected_ids = {
        span.speaker_id for span in _selection_spans(parsed, model.start, model.end)
        if span.speaker_id
    }
    inherited_characters = []
    for item in characters:
        if item.get("id") not in selected_ids:
            continue
        delivery = deepcopy(item.get("delivery") or _empty_delivery())
        delivery.setdefault("emotion_intensity", None)
        inherited_characters.append(
            {
                "id": item["id"],
                "display_name": item["display_name"],
                "delivery": delivery,
            }
        )
    workspace = _service(services, "workspace_settings")
    resolved, _ = workspace.resolve_in_session(
        session, session_id, sections=["audio", "tts"]
    )
    runtime = _runtime_settings(resolved)
    try:
        preview = preview_speech_segment_in_session(
            services, session, session_id,
            revision_id=revision.id, segment_id=segment.id,
            _resolved_settings=resolved, _controls=controls,
        )
        preview_error = None
    except RevisionConflict:
        raise
    except ValueError as error:
        if str(error).startswith("Invalid speech markup:"):
            raise
        preview = None
        preview_error = str(error)
    capabilities = resolve_capabilities(runtime)
    return {
        "revision_id": revision.id,
        "segment_id": segment.id,
        "segment_revision": segment.revision,
        "selection": {"start": model.start, "end": model.end,
                      "text": text[model.start:model.end]},
        "locked": bool((adopted_record or {}).get("locked")),
        "editable": not legacy,
        "edit_blocked_reason": (
            "This block has legacy structured directions that cannot be edited as speech XML."
            if legacy else None
        ),
        "current": current,
        "speech_xml": None if legacy else parsed.xml,
        "markup_reference": speech_markup_reference(),
        "inherited": {
            "general_direction": str(
                runtime.get("generation_prompt")
                or runtime.get("openai_audio_instructions") or ""
            ).strip(),
            "characters": inherited_characters,
        },
        "current_flags": {
            "casting_enabled": bool(runtime.get("casting_enabled")),
            "performance_enabled": bool(runtime.get("performance_enabled")),
            "allow_vocalizations": bool(runtime.get("performance_allow_vocalizations")),
        },
        "preview": preview,
        "preview_error": preview_error,
        "service": str(runtime.get("service_id") or runtime.get("service") or ""),
        "model": str(capabilities.get("model") or ""),
        "capabilities": capabilities,
        "compilation_only": True,
    }


def preview_speech_selection(
    services: Any, session, session_id: str, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Return a deterministic compilation preview without creating rows."""

    model, values = _validated_request(request, apply=False)
    state = _resolve_state(services, session, session_id, model, values)
    preview = _preview_compilation(services, session, state)
    return {
        "preview_revision": state.guard,
        "selection": _selection_response(state),
        "speech_xml": state.candidate_xml,
        "locked": state.locked,
        "source_performance_plan_id": state.adopted.id if state.adopted else None,
        "current_flags": {
            "casting_enabled": bool(state.tts.get("casting_enabled")),
            "performance_enabled": bool(state.tts.get("performance_enabled")),
            "allow_vocalizations": bool(state.tts.get("performance_allow_vocalizations")),
        },
        "preview": preview,
        "compilation_only": True,
    }


def apply_speech_selection(
    services: Any, session, session_id: str, request: Mapping[str, Any]
) -> dict[str, Any]:
    """Create, edit, and adopt one manual XML plan in the caller transaction."""

    model, values = _validated_request(request, apply=True)
    state = _resolve_state(services, session, session_id, model, values)
    if values["expected_preview_revision"].lower() != state.guard:
        raise RevisionConflict(
            "The speech selection preview is stale. Review the current selection again."
        )
    assert_session_idle(session, session_id)
    if state.locked and not values.get("unlock_locked", False):
        raise RevisionConflict(
            "Explicitly unlock the protected manual annotation before changing it."
        )

    create_request = PerformancePlanCreateRequest(
        expected_plan_revision_id=state.revision.id,
        mode="manual",
        annotation_format="xml",
        context_before=int(state.runtime.get("performance_context_before", 2)),
        context_after=int(state.runtime.get("performance_context_after", 1)),
        context_max_chars=int(state.runtime.get("performance_context_max_chars", 4000)),
        copy_from_id=state.adopted.id if state.adopted else None,
    )
    new_plan = plans.create_plan(
        session,
        session_id,
        create_request,
        state.tts,
    )
    edited = plans.edit_annotations(
        session,
        new_plan,
        expected_version=new_plan.version,
        items=[
            {
                "segment_id": state.segment.id,
                "speech_xml": state.candidate_xml,
                "locked": True,
            }
        ],
        unlock_locked=bool(values.get("unlock_locked", False)),
    )
    adopted = plans.adopt_plan(
        session,
        new_plan,
        expected_version=edited["version"],
        accept_unanalysed=True,
    )

    patch: dict[str, Any] = {}
    if values.get("enable_casting"):
        patch["casting_enabled"] = True
    if values.get("enable_performance"):
        patch["performance_enabled"] = True
    if values.get("enable_vocalizations"):
        patch["performance_allow_vocalizations"] = True
    if patch:
        workspace = _service(services, "workspace_settings")
        current = workspace.get_in_session(session, session_id, "tts")
        needed = {
            key: value
            for key, value in patch.items()
            if current["effective"].get(key) is not True
        }
        if needed:
            workspace.patch_in_session(
                session,
                session_id,
                "tts",
                int(current["revision"]),
                needed,
            )

    workspace = _service(services, "workspace_settings")
    flags = workspace.get_in_session(session, session_id, "tts")["effective"]
    return {
        **adopted,
        "adopted": adopted,
        "selection": _selection_response(state),
        "speech_xml": state.candidate_xml,
        "source_performance_plan_id": state.adopted.id if state.adopted else None,
        "current_plan_revision": state.revision.id,
        "current_plan_revision_id": state.revision.id,
        "performance_plan": {
            "id": new_plan.id,
            "version": new_plan.version,
            "plan_revision_id": new_plan.plan_revision_id,
            "status": new_plan.status,
        },
        "current_flags": {
            "casting_enabled": bool(flags.get("casting_enabled")),
            "performance_enabled": bool(flags.get("performance_enabled")),
            "allow_vocalizations": bool(flags.get("performance_allow_vocalizations")),
        },
    }


__all__ = ["apply_speech_selection", "get_speech_selection_context", "preview_speech_selection"]
