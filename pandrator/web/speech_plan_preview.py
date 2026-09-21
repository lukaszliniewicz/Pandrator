"""Read-only compilation of one speech-plan segment.

The preview deliberately follows the same render and performance compilation
paths as generation.  It never creates a plan, freezes a snapshot, refreshes
provider inventory, or synthesizes audio.
"""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from pandrator.logic.speech_markup import (
    ParsedSpeechMarkup,
    SpeechMarkupSpan,
    parse_speech_markup,
    plain_speech_markup,
)
from pandrator.logic.speech_performance import (
    PerformanceAnnotation,
    anchor_range,
    compile_performance,
    content_hash,
    resolve_capabilities,
)

from . import models as m
from .generation_controls import get_generation_controls
from .performance_plans import (
    _annotations,
    _assert_current,
    speech_markup_for_segment,
)
from .speech_annotation_records import record_annotation, record_markup
from .speech_plan_workspace import (
    frozen_semantic_contexts,
    semantic_context_units,
    semantic_context_window,
    segment_performance_settings,
)
from .workspace import RevisionConflict, adapt_runtime_settings


def _get_services_value(services: Any, key: str) -> Any:
    if isinstance(services, Mapping):
        return services[key]
    return getattr(services, key)


def _plan_segment(
    session,
    session_id: str,
    revision_id: str,
    segment_id: str,
    *,
    frozen: bool,
) -> tuple[m.GenerationPlan, m.GenerationPlanRevision, m.GenerationSegment]:
    if session.get(m.SessionRecord, session_id) is None:
        raise KeyError(session_id)
    plan = session.scalar(
        select(m.GenerationPlan).where(m.GenerationPlan.session_id == session_id)
    )
    revision = session.get(m.GenerationPlanRevision, revision_id)
    if plan is None or revision is None or revision.plan_id != plan.id:
        raise KeyError(revision_id)
    if not frozen and plan.active_revision_id != revision_id:
        raise RevisionConflict(
            "The selected speech plan changed. Refresh the active revision before previewing."
        )
    segment = session.get(m.GenerationSegment, segment_id)
    if (
        segment is None
        or segment.plan_revision_id != revision_id
        or segment.removed
    ):
        raise KeyError(segment_id)
    return plan, revision, segment


def _assert_snapshot_revision(snapshot: Mapping[str, Any], revision_id: str) -> None:
    for key in (
        "speech_plan_revision_id",
        "generation_control_snapshot",
        "performance_snapshot",
        "semantic_context_snapshot",
    ):
        value = snapshot.get(key)
        if key == "speech_plan_revision_id":
            snapshot_revision = value
        elif isinstance(value, Mapping):
            snapshot_revision = value.get("plan_revision_id")
        else:
            snapshot_revision = None
        if snapshot_revision and str(snapshot_revision) != revision_id:
            raise RevisionConflict(
                "The frozen generation snapshot does not match the requested speech plan revision."
            )


def _segment_text(segment: m.GenerationSegment) -> str:
    return str(segment.optimized_text or segment.text or "")


def _runtime_settings(values: Mapping[str, Any]) -> dict[str, Any]:
    """Flatten only runtime sections while preserving legacy aliases."""

    audio = adapt_runtime_settings("audio", dict(values.get("audio") or {}))
    tts_values = dict(values.get("tts") or {})
    selected = values.get("selected_segment_override") or {}
    if isinstance(selected, Mapping) and isinstance(selected.get("tts"), Mapping):
        tts_values.update(deepcopy(dict(selected["tts"])))
    tts = adapt_runtime_settings("tts", tts_values)
    return {**audio, **tts}


def _segment_overrides(settings: dict[str, Any], segment: m.GenerationSegment) -> None:
    if segment.language:
        settings.update(
            language=segment.language,
            target_language=segment.language,
        )
    if segment.voice:
        settings.update(voice=segment.voice, speaker=segment.voice)


def _character_dictionary(
    controls: Mapping[str, Any] | None,
    snapshot: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    if snapshot:
        generation = snapshot.get("generation_control_snapshot") or {}
        frozen_controls = generation.get("controls") or {}
        frozen_characters = frozen_controls.get("characters")
        if isinstance(frozen_characters, list):
            return deepcopy(frozen_characters)
        performance = snapshot.get("performance_snapshot") or {}
        frozen_characters = performance.get("character_dictionary")
        if isinstance(frozen_characters, list):
            return deepcopy(frozen_characters)
    characters = (controls or {}).get("characters")
    return deepcopy(characters) if isinstance(characters, list) else []


def _speaker_names(characters: list[dict[str, Any]]) -> dict[str, str]:
    result: dict[str, str] = {}
    for character in characters:
        identifier = str(character.get("id") or "")
        if not identifier:
            continue
        name = character.get("display_name") or character.get("name")
        result[identifier] = str(name or identifier)
    return result


def _span_role(span: SpeechMarkupSpan) -> str:
    if span.narrator:
        return "narrator"
    if span.speaker_id:
        return "speaker"
    if span.dialogue:
        return "dialogue"
    return "narrator"


def _part_for_range(
    parts: list[dict[str, Any]], start: int, end: int
) -> dict[str, Any] | None:
    candidates = [
        part
        for part in parts
        if int(part.get("end", 0)) > start and int(part.get("start", 0)) < end
    ]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda part: min(end, int(part.get("end", 0)))
        - max(start, int(part.get("start", 0))),
    )


def _plain_span(start: int, end: int) -> dict[str, Any]:
    return {
        "start": start,
        "end": end,
        "speaker_id": None,
        "speaker_name": None,
        "role": "narrator",
        "delivery": {
            "instruction": "",
            "emotion": "",
            "pace": "",
            "cadence": "",
            "emphasis": "",
        },
        "voice": "",
        "voice_source": "base",
        "fallback": False,
    }


def _public_spans(
    parsed: ParsedSpeechMarkup,
    parts: list[dict[str, Any]],
    characters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    names = _speaker_names(characters)
    result: list[dict[str, Any]] = []
    cursor = 0
    for span in parsed.spans:
        start = max(cursor, int(span.start))
        end = min(len(parsed.transcript), int(span.end))
        if start > cursor:
            result.append(_plain_span(cursor, start))
        if end <= start:
            continue
        part = _part_for_range(parts, start, end)
        settings = (part or {}).get("settings") or {}
        result.append(
            {
                "start": start,
                "end": end,
                "speaker_id": span.speaker_id,
                "speaker_name": names.get(span.speaker_id) if span.speaker_id else None,
                "role": _span_role(span),
                "delivery": deepcopy(span.delivery),
                "voice": str(
                    settings.get("voice")
                    or settings.get("speaker")
                    or settings.get("voice_id")
                    or ""
                ),
                "voice_source": str((part or {}).get("voice_source") or "base"),
                "fallback": bool((part or {}).get("fallback")),
            }
        )
        cursor = end
    if cursor < len(parsed.transcript):
        result.append(_plain_span(cursor, len(parsed.transcript)))
    if not result and parsed.transcript:
        result.append(_plain_span(0, len(parsed.transcript)))
    return result


def _compile_parts(
    parts: list[dict[str, Any]],
    *,
    include_request: bool,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for part in parts:
        compiled = compile_performance(str(part.get("text") or ""), part["settings"])
        public = {
            "start": int(part.get("start", 0)),
            "end": int(part.get("end", 0)),
            "voice": str(
                part["settings"].get("voice")
                or part["settings"].get("speaker")
                or part["settings"].get("voice_id")
                or ""
            ),
            "voice_source": str(part.get("voice_source") or "base"),
            "fallback": bool(part.get("fallback")),
            "report": deepcopy(compiled.report),
            "instructions": compiled.instructions,
        }
        if include_request:
            public.update(
                text=compiled.transcript,
                input=compiled.input,
                request_options=deepcopy(compiled.request_options),
            )
        result.append(public)
    return result


def _public_events(
    parsed: ParsedSpeechMarkup,
    text: str,
    annotation: Mapping[str, Any] | None,
) -> list[dict[str, Any]]:
    """Expose markup events, or pSSML events when no XML carries them."""

    if parsed.events or not annotation:
        return [event.public() for event in parsed.events]
    events: list[dict[str, Any]] = []
    validated = PerformanceAnnotation.model_validate(record_annotation(annotation) or {})
    for event in validated.events:
        if event.anchor is None:
            offset = 0 if event.position == "before" else len(text)
        else:
            start, end = anchor_range(text, event.anchor)
            offset = start if event.position == "before" else end
        events.append(
            {
                "offset": offset,
                "kind": event.kind,
                "duration_ms": event.duration_ms,
            }
        )
    return events


def _current_parts(
    session,
    session_id: str,
    segment: m.GenerationSegment,
    settings: dict[str, Any],
    *,
    controls: dict[str, Any],
    adopted_xml: str | None,
) -> tuple[ParsedSpeechMarkup, list[dict[str, Any]]]:
    text = _segment_text(segment)
    xml = adopted_xml or (segment.speech_plan_json or {}).get("speech_xml")
    parsed = parse_speech_markup(
        xml
        if isinstance(xml, str)
        else plain_speech_markup(segment.id, text),
        expected_segment_id=segment.id,
        expected_text=text,
        characters=_character_dictionary(controls),
    )
    from .generation_cast_runtime import preview_render_parts

    parts = preview_render_parts(
        session,
        session_id,
        text,
        segment.id,
        settings,
        xml if isinstance(xml, str) else None,
        segment.speaker,
    )
    return parsed, parts


def _frozen_parts(
    segment: m.GenerationSegment,
    settings: dict[str, Any],
    snapshot: dict[str, Any],
) -> tuple[ParsedSpeechMarkup, list[dict[str, Any]]]:
    text = _segment_text(segment)
    generation = snapshot.get("generation_control_snapshot") or {}
    generation_entry = (generation.get("segments") or {}).get(segment.id) or {}
    if generation_entry.get("text_hash") and generation_entry["text_hash"] != content_hash(text):
        raise ValueError(
            "Spoken text changed after the cast was frozen. Review a new speech plan."
        )
    performance = snapshot.get("performance_snapshot") or {}
    performance_entry = (performance.get("annotations") or {}).get(segment.id) or {}
    if settings.get("performance_enabled") and performance_entry.get("text_hash") and performance_entry["text_hash"] != content_hash(text):
        raise ValueError(
            "Spoken text changed after performance adoption. Review the performance plan again."
        )
    contexts = frozen_semantic_contexts(snapshot)
    effective = segment_performance_settings(
        settings,
        snapshot,
        segment.id,
        text,
        contexts=contexts,
    )
    from .generation_cast_runtime import segment_render_parts

    parts = segment_render_parts(effective, snapshot, segment.id, text)
    xml = generation_entry.get("speech_xml")
    if not isinstance(xml, str) and settings.get("performance_enabled"):
        xml = speech_markup_for_segment(snapshot, segment.id, text)
    if not isinstance(xml, str):
        xml = (segment.speech_plan_json or {}).get("speech_xml")
    parsed = parse_speech_markup(
        xml
        if isinstance(xml, str)
        else plain_speech_markup(segment.id, text),
        expected_segment_id=segment.id,
        expected_text=text,
        characters=_character_dictionary(None, snapshot),
    )
    return parsed, parts


def preview_speech_segment(
    services: Any,
    session_id: str,
    *,
    revision_id: str,
    segment_id: str,
    generation_run_id: str | None = None,
    include_request: bool = False,
) -> dict[str, Any]:
    """Compile one accepted segment for inspection without starting synthesis."""

    database = _get_services_value(services, "database")
    with database.session() as session:
        frozen = generation_run_id is not None
        _plan, _revision, segment = _plan_segment(
            session,
            session_id,
            revision_id,
            segment_id,
            frozen=frozen,
        )
        snapshot: dict[str, Any] = {}
        run = None
        if generation_run_id is not None:
            run = session.get(m.GenerationRun, generation_run_id)
            if run is None or run.session_id != session_id:
                raise KeyError(generation_run_id)
            if run.plan_revision_id != revision_id:
                raise RevisionConflict(
                    "The selected generation run belongs to a different speech plan revision."
                )
            snapshot = deepcopy(run.settings_snapshot_json or {})
            _assert_snapshot_revision(snapshot, revision_id)
            settings = _runtime_settings(snapshot)
            controls = (snapshot.get("generation_control_snapshot") or {}).get(
                "controls"
            ) or {}
            _segment_overrides(settings, segment)
            parsed, parts = _frozen_parts(segment, settings, snapshot)
            annotation = (
                ((snapshot.get("performance_snapshot") or {}).get("annotations") or {})
                .get(segment.id, {})
                .get("annotation")
                if settings.get("performance_enabled")
                else None
            )
            source = "run_snapshot"
        else:
            workspace = _get_services_value(services, "workspace_settings")
            resolved, _ = workspace.resolve(session_id, sections=["audio", "tts"])
            settings = _runtime_settings(resolved)
            controls = get_generation_controls(session, session_id)
            adopted_xml = None
            annotation_record = None
            if settings.get("performance_enabled") or settings.get("casting_enabled"):
                adopted = session.scalar(
                    select(m.PerformancePlan).where(
                        m.PerformancePlan.plan_revision_id == revision_id,
                        m.PerformancePlan.status == "adopted",
                    )
                )
                if adopted is None:
                    if settings.get("performance_enabled"):
                        raise ValueError(
                            "Performance is enabled, but this speech plan has no adopted performance plan. Adopt one or disable performance."
                        )
                else:
                    _assert_current(session, adopted)
                    annotation_record = _annotations(session, adopted).get(segment_id)
                    adopted_xml = record_markup(annotation_record)
                if settings.get("performance_enabled") and annotation_record is not None:
                    settings["_performance"] = record_annotation(annotation_record) or {}
            _segment_overrides(settings, segment)
            units = semantic_context_units(session, revision_id)
            contexts = semantic_context_window(
                units,
                settings,
                target_ids={segment_id},
            )
            settings["_semantic_context"] = contexts.get(segment_id, {})
            parsed, parts = _current_parts(
                session,
                session_id,
                segment,
                settings,
                controls=controls,
                adopted_xml=adopted_xml,
            )
            if not (settings.get("casting_enabled") or settings.get("performance_enabled")):
                # Saved directions remain inspectable while disabled. Actual
                # compiler parts above still use the generation settings.
                from .speech_annotation_view import annotation_xml_by_segment

                display_xml = annotation_xml_by_segment(
                    session, revision_id, [segment]
                ).get(segment_id)
                if display_xml:
                    parsed = parse_speech_markup(
                        display_xml,
                        expected_segment_id=segment_id,
                        expected_text=_segment_text(segment),
                        characters=_character_dictionary(controls),
                    )
            annotation = settings.get("_performance") if settings.get("performance_enabled") else None
            source = "current_plan"

        capabilities = resolve_capabilities(settings)
        compiled_parts = _compile_parts(parts, include_request=include_request)
        result: dict[str, Any] = {
            "session_id": session_id,
            "revision_id": revision_id,
            "segment_id": segment_id,
            "source": source,
            "text": parsed.transcript,
            "service": str(
                settings.get("service") or settings.get("tts_service") or ""
            ),
            "model": str(capabilities.get("model") or ""),
            "casting_enabled": bool(settings.get("casting_enabled")),
            "performance_enabled": bool(settings.get("performance_enabled")),
            "capabilities": capabilities,
            "spans": _public_spans(
                parsed,
                parts,
                _character_dictionary(controls, snapshot if run else None),
            ),
            "parts": compiled_parts,
            "events": _public_events(parsed, parsed.transcript, annotation),
            "boundary_after": parsed.boundary_after,
            "compilation_only": True,
        }
        if generation_run_id is not None:
            result["generation_run_id"] = generation_run_id
        return result


__all__ = ["preview_speech_segment"]
