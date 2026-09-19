"""Freeze casting intent and resolve it into requests within one logical segment."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from copy import deepcopy
from typing import Any

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup
from pandrator.logic.speech_performance import content_hash, resolve_capabilities

from . import models as m
from .generation_control_schemas import VoiceBinding
from .generation_controls import get_generation_controls
from .tts_providers import TtsProviderRegistry


def remap_markup(xml: str, segment_id: str, text: str, characters: list[dict]) -> str:
    # Validate before ElementTree touches the input (DTD/entity guards and bounds).
    import re

    match = re.search(r'<segment\s[^>]*\bid\s*=\s*["\']([^"\']+)', xml)
    if not match:
        raise ValueError("Speech XML must have a segment ID.")
    parsed = parse_speech_markup(
        xml,
        expected_segment_id=match.group(1),
        expected_text=text,
        characters=characters,
    )
    root = ET.fromstring(parsed.xml)
    root.set("id", segment_id)
    return parse_speech_markup(
        ET.tostring(root, encoding="unicode"),
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    ).xml


def _binding_key(binding: dict) -> str:
    return content_hash(VoiceBinding.model_validate(binding).model_dump(mode="json"))


def resolve_binding(session, binding: dict, settings: dict) -> dict:
    """Resolve a managed reference once; never treat a character name as a voice."""
    binding = VoiceBinding.model_validate(binding).model_dump(mode="json")
    service = TtsProviderRegistry().service_id_for_settings(settings)
    requested_service = binding.get("service")
    actual_service = str(
        settings.get("service") or settings.get("tts_service") or service
    )
    if requested_service and requested_service.casefold() not in {
        service.casefold(),
        actual_service.casefold(),
    }:
        raise ValueError(
            f"Cast voice is assigned to {requested_service}; this run uses {actual_service}. Reassign the cast for the selected service."
        )
    model = str(
        resolve_capabilities(settings).get("model")
        or settings.get("xtts_model")
        or settings.get("model")
        or ""
    )
    if binding.get("model") and model and binding["model"] != model:
        raise ValueError(
            f"Cast voice is assigned to model {binding['model']}; this run uses {model}."
        )
    voice_name = binding.get("voice") or ""
    managed_id = binding.get("voice_id")
    if managed_id:
        voice = session.get(m.Voice, managed_id)
        if voice is None:
            raise ValueError(f"Managed cast voice no longer exists: {managed_id}")
        providers = (voice.metadata_json or {}).get("providers") or {}
        registrations = [
            (key, value)
            for key, value in providers.items()
            if isinstance(value, dict)
            and key.casefold().replace("-", "_")
            in {
                service.casefold().replace("-", "_"),
                actual_service.casefold().replace("-", "_"),
            }
        ]
        ready = next(
            (value for _, value in registrations if value.get("status") == "ready"),
            None,
        )
        if ready is None:
            raise ValueError(
                f"Publish or link '{voice.name}' to {actual_service} before casting it."
            )
        voice_name = str(
            ready.get("provider_voice_id") or ready.get("voice_id") or voice.name
        )
    # Voice-design models have a stable voice-description channel; other models
    # must not turn a voice identity description into a local emotion.
    if binding.get("voice_description") and not resolve_capabilities(settings).get(
        "voice_design"
    ):
        raise ValueError(
            "A cast voice description requires a voice-design model. Choose a provider voice or managed reference for this model."
        )
    return {**binding, "voice": voice_name}


def apply_resolved_binding(
    binding: dict | None, settings: dict, resolutions: dict[str, dict]
) -> dict:
    result = dict(settings)
    if not binding:
        return result
    resolved = resolutions.get(_binding_key(binding))
    if resolved is None:
        raise ValueError(
            "The cast assignment is absent from this run snapshot. Start a new generation run."
        )
    # Do not reuse a narrator reference waveform for a newly selected character.
    for key in (
        "audio_cpp_voice_ref",
        "audio_cpp_voice_ref_hash",
        "audio_cpp_reference_text",
    ):
        result.pop(key, None)
    if resolved.get("voice"):
        result["voice"] = result["speaker"] = resolved["voice"]
        # The native ElevenLabs adapter reads this legacy key before voice.
        if "elevenlabs_voice_id" in result:
            result["elevenlabs_voice_id"] = resolved["voice"]
    if resolved.get("voice_id"):
        result["_cast_voice_id"] = resolved["voice_id"]
    if resolved.get("voice_description"):
        general = str(
            result.get("generation_prompt")
            or result.get("openai_audio_instructions")
            or ""
        ).strip()
        description = resolved["voice_description"]
        combined = "\n".join(
            dict.fromkeys(part for part in (description, general) if part)
        )
        result["generation_prompt"] = combined
        result["openai_audio_instructions"] = combined
    return result


def freeze_cast_snapshot(
    session, revision_id: str, snapshot: dict, settings: dict
) -> None:
    from .generation_rendering import build_render_parts

    revision = session.get(m.GenerationPlanRevision, revision_id)
    plan = session.get(m.GenerationPlan, revision.plan_id) if revision else None
    if plan is None:
        raise ValueError("The speech plan is unavailable.")
    controls = get_generation_controls(session, plan.session_id)
    entries, resolutions = {}, {}
    performance = (snapshot.get("performance_snapshot") or {}).get("annotations") or {}

    def apply(binding, base):
        if binding:
            resolutions.setdefault(
                _binding_key(binding), resolve_binding(session, binding, base)
            )
        return apply_resolved_binding(binding, base, resolutions)

    for segment in session.scalars(
        select(m.GenerationSegment).where(
            m.GenerationSegment.plan_revision_id == revision_id,
            m.GenerationSegment.removed.is_(False),
        )
    ):
        text = segment.optimized_text or segment.text
        record = (performance.get(segment.id) or {}).get("annotation") or {}
        xml = record.get("_speech_xml") or (segment.speech_plan_json or {}).get(
            "speech_xml"
        )
        if xml:
            xml = remap_markup(xml, segment.id, text, controls["characters"])
        entry = {
            "text_hash": content_hash(text),
            "speech_xml": xml,
            "source_speaker": segment.speaker,
        }
        entries[segment.id] = entry
        base = dict(settings)
        if segment.voice:
            base["voice"] = base["speaker"] = segment.voice
        if segment.language:
            base["language"] = segment.language
        build_render_parts(
            text,
            base,
            speech_xml=xml,
            segment_id=segment.id,
            controls=controls,
            source_speaker=segment.speaker,
            apply_binding=apply,
        )
    snapshot["generation_control_snapshot"] = {
        "schema_version": 1,
        "plan_revision_id": revision_id,
        "controls": deepcopy(controls),
        "segments": entries,
        "resolved_bindings": resolutions,
    }


def segment_render_parts(
    settings: dict, snapshot: dict, segment_id: str, text: str
) -> list[dict[str, Any]]:
    from .generation_rendering import build_render_parts

    frozen = snapshot.get("generation_control_snapshot") or {}
    if not frozen:
        if settings.get("casting_enabled"):
            raise ValueError(
                "Casting requires a frozen character/cast snapshot. Start a new run."
            )
        return build_render_parts(text, settings)
    if frozen.get("schema_version") != 1:
        raise ValueError("Unsupported generation-control snapshot.")
    entry = (frozen.get("segments") or {}).get(segment_id)
    if not entry or entry.get("text_hash") != content_hash(text):
        raise ValueError(
            "Spoken text changed after the cast was frozen. Review a new speech plan."
        )
    return build_render_parts(
        text,
        settings,
        speech_xml=entry.get("speech_xml"),
        segment_id=segment_id,
        controls=frozen["controls"],
        source_speaker=entry.get("source_speaker"),
        apply_binding=lambda binding, base: apply_resolved_binding(
            binding, base, frozen["resolved_bindings"]
        ),
    )


def preview_render_parts(
    session,
    session_id: str,
    text: str,
    segment_id: str,
    settings: dict,
    xml: str | None,
    source_speaker: str | None = None,
) -> list[dict]:
    from .generation_rendering import build_render_parts

    controls = get_generation_controls(session, session_id)
    resolutions = {}

    def apply(binding, base):
        if binding:
            resolutions.setdefault(
                _binding_key(binding), resolve_binding(session, binding, base)
            )
        return apply_resolved_binding(binding, base, resolutions)

    return build_render_parts(
        text,
        settings,
        speech_xml=xml,
        segment_id=segment_id,
        controls=controls,
        source_speaker=source_speaker,
        apply_binding=apply,
    )


def combine_source_markup(
    units: list[tuple[str, str | None]],
    segment_id: str,
    text: str,
    characters: list[dict],
) -> str:
    """Reconcile whole source units with an accepted block, never fabricate offsets."""
    from pandrator.logic.speech_markup import plain_speech_markup

    root = ET.Element("segment", {"id": segment_id})
    cursor = 0
    for index, (source_text, xml) in enumerate(units):
        spoken = source_text.strip()
        position = text.find(spoken, cursor)
        if position < 0 or text[cursor:position].strip():
            raise ValueError(
                "Speech-block wording no longer matches its dialogue annotation. Keep annotated units integral or reannotate the accepted text."
            )
        separator = text[cursor:position]
        if len(root):
            root[-1].tail = (root[-1].tail or "") + separator
        else:
            root.text = (root.text or "") + separator
        canonical = (
            remap_markup(xml, str(index + 1), spoken, characters)
            if xml
            else plain_speech_markup(str(index + 1), spoken)
        )
        child = ET.fromstring(canonical)
        boundary = child.attrib.get("boundary_after")
        child.tag = "span"
        child.attrib.clear()
        root.append(child)
        if boundary:
            root.set("boundary_after", boundary)
        cursor = position + len(spoken)
    if text[cursor:].strip():
        raise ValueError(
            "Speech-block text contains words absent from its source dialogue annotations."
        )
    if len(root):
        root[-1].tail = (root[-1].tail or "") + text[cursor:]
    return parse_speech_markup(
        ET.tostring(root, encoding="unicode"),
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    ).xml
