"""Pure planning and in-memory assembly for internally cast speech parts.

This module keeps a logical segment's accepted text intact while preparing
ordered provider requests for spans that resolve to different voices.  It does
not resolve managed voices itself; callers can provide ``apply_binding`` for
that backend-specific step.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass, replace
from typing import Any
import unicodedata

from pydub import AudioSegment

from pandrator.logic.speech_markup import (
    ParsedSpeechMarkup,
    SpeechMarkupSpan,
    markup_to_annotation,
    parse_speech_markup,
)
from pandrator.logic.speech_performance import validate_annotation

_EMPTY_DELIVERY = {
    "instruction": "",
    "emotion": "",
    "pace": "",
    "cadence": "",
    "emphasis": "",
}
_VOICE_KEYS = (
    "voice",
    "speaker",
    "voice_id",
    "voice_description",
    "voice_name",
    "_cast_voice_id",
    "audio_cpp_voice_ref",
    "audio_cpp_voice_ref_hash",
    "audio_cpp_reference_text",
    "service",
    "model",
)


@dataclass
class _PlannedSpan:
    span: SpeechMarkupSpan
    settings: dict[str, Any]
    voice_key: tuple[tuple[str, str], ...]
    voice_source: str
    fallback: bool


@dataclass
class _Group:
    items: list[_PlannedSpan]

    @property
    def start(self) -> int:
        return self.items[0].span.start

    @property
    def end(self) -> int:
        return self.items[-1].span.end

    @property
    def text(self) -> str:
        return "".join(item.span.text for item in self.items)

    @property
    def anchor(self) -> _PlannedSpan:
        for item in self.items:
            if _is_speakable_text(item.span.text):
                return item
        return self.items[0]

    @property
    def voice_key(self) -> tuple[tuple[str, str], ...]:
        return self.anchor.voice_key


def _is_speakable_text(text: str) -> bool:
    """Whether text contains a Unicode letter or number for a provider."""

    return any(unicodedata.category(character)[0] in {"L", "N"} for character in text)


def _control_values(
    settings: Mapping[str, Any], controls: Mapping[str, Any] | None
) -> dict[str, Any]:
    if controls is not None:
        return deepcopy(dict(controls))
    for key in ("generation_controls", "_generation_controls", "controls"):
        value = settings.get(key)
        if isinstance(value, Mapping):
            return deepcopy(dict(value))
    if "characters" in settings or "cast" in settings:
        return {
            "characters": deepcopy(settings.get("characters") or []),
            "cast": deepcopy(settings.get("cast") or {}),
        }
    return {"characters": [], "cast": {}}


def _voice_key(
    settings: Mapping[str, Any], binding: Mapping[str, Any] | None
) -> tuple[tuple[str, str], ...]:
    """Create a stable identity from voice settings, excluding delivery."""

    values: list[tuple[str, str]] = []
    for key in _VOICE_KEYS:
        value = settings.get(key)
        if value is not None and value != "":
            values.append((key, repr(value)))
    if binding and binding.get("voice_description"):
        # A backend callback may put this description into generation_prompt;
        # retain it in the grouping identity without splitting on directions.
        values.append(("binding_voice_description", repr(binding["voice_description"])))
    return tuple(values)


def _default_apply_binding(
    binding: dict[str, Any] | None, settings: dict[str, Any]
) -> dict[str, Any]:
    if not binding:
        return settings
    voice_id = binding.get("voice_id")
    voice = binding.get("voice")
    if voice_id and not voice:
        raise ValueError(
            "A managed voice_id needs an apply_binding callback for backend resolution."
        )
    if voice_id:
        raise ValueError(
            "The default binding adapter cannot safely apply a managed voice_id; "
            "provide apply_binding."
        )
    if voice:
        settings["voice"] = voice
        settings["speaker"] = voice
    description = binding.get("voice_description")
    if description:
        settings["generation_prompt"] = description
    return settings


def _binding_result(
    settings: dict[str, Any],
    *,
    controls: dict[str, Any],
    span: SpeechMarkupSpan,
    source_speaker: str | None,
    xml_present: bool,
    apply_binding: Callable[[dict[str, Any] | None, dict[str, Any]], dict[str, Any]]
    | None,
) -> tuple[dict[str, Any], str, bool, dict[str, Any] | None]:
    from .generation_controls import resolve_cast_voice

    effective_source = None
    if not xml_present or span.speaker_id is None and span.dialogue:
        effective_source = source_speaker
    speaker_id = span.speaker_id
    voice_category = span.voice_category
    dialogue = span.dialogue
    span_voice = span.voice
    if span.narrator:
        # An authored narrator scope is explicit even when nested in a
        # dialogue container.  It must not inherit the source speaker or a
        # dialogue category cast by accident.
        speaker_id = None
        voice_category = "unspecified"
        dialogue = False
        effective_source = None
    resolution = resolve_cast_voice(
        controls,
        speaker_id=speaker_id,
        voice_category=voice_category,
        dialogue=dialogue,
        span_voice=span_voice,
        source_speaker=effective_source,
    )
    binding = resolution.get("binding")
    if binding is not None and not isinstance(binding, dict):
        raise ValueError("Cast resolver returned an invalid binding.")
    if apply_binding is None:
        prepared = _default_apply_binding(binding, settings)
    else:
        result = apply_binding(deepcopy(binding), settings)
        if not isinstance(result, dict):
            raise TypeError("apply_binding must return a settings dictionary")
        prepared = result
    return (
        prepared,
        str(resolution.get("source") or "inherited"),
        bool(resolution.get("fallback")),
        deepcopy(binding),
    )


def _synthetic_markup(
    text: str, *, source_speaker: str | None, settings: Mapping[str, Any]
) -> ParsedSpeechMarkup:
    category = str(settings.get("voice_category") or "unspecified")
    dialogue = bool(settings.get("dialogue")) or bool(source_speaker)
    span = SpeechMarkupSpan(
        start=0,
        end=len(text),
        text=text,
        speaker_id=None,
        voice_category=category,
        voice=None,
        dialogue=dialogue,
        delivery=dict(_EMPTY_DELIVERY),
        narrator=False,
    )
    spans = (span,) if text else ()
    return ParsedSpeechMarkup(
        segment_id="",
        transcript=text,
        spans=spans,
        events=(),
        xml="",
        boundary_after=None,
    )


def _groups(planned: list[_PlannedSpan]) -> list[_Group]:
    initial: list[_Group] = []
    for item in planned:
        if initial and initial[-1].voice_key == item.voice_key:
            initial[-1].items.append(item)
        else:
            initial.append(_Group([item]))
    if not initial:
        return []
    if not any(_is_speakable_text(group.text) for group in initial):
        raise ValueError(
            "Speech segment contains no speakable letter or number text; "
            "punctuation-only input cannot be synthesized."
        )

    folded: list[_Group] = []
    pending: list[_PlannedSpan] = []
    for group in initial:
        if _is_speakable_text(group.text):
            if pending:
                group.items = pending + group.items
                pending = []
            folded.append(group)
        elif folded:
            folded[-1].items.extend(group.items)
        else:
            pending.extend(group.items)
    if pending:
        if folded:
            folded[-1].items.extend(pending)
        else:
            folded.append(_Group(pending))

    merged: list[_Group] = []
    for group in folded:
        if merged and merged[-1].voice_key == group.voice_key:
            merged[-1].items.extend(group.items)
        else:
            merged.append(group)
    return merged


def _speaker_ids(
    items: list[_PlannedSpan], *, source_speaker: str | None, xml_present: bool
) -> list[str]:
    result: list[str] = []
    for item in items:
        speaker = item.span.speaker_id
        if (
            speaker is None
            and source_speaker
            and (not xml_present or item.span.dialogue)
        ):
            speaker = source_speaker
        if speaker and speaker not in result:
            result.append(speaker)
    return result


def _subset(parsed: ParsedSpeechMarkup, start: int, end: int) -> ParsedSpeechMarkup:
    spans: list[SpeechMarkupSpan] = []
    for span in parsed.spans:
        left = max(start, span.start)
        right = min(end, span.end)
        if left >= right:
            continue
        spans.append(
            replace(
                span,
                start=left - start,
                end=right - start,
                text=parsed.transcript[left:right],
            )
        )
    events = tuple(
        replace(event, offset=event.offset - start)
        for event in parsed.events
        if event.offset >= start
        and (
            event.offset < end
            or (end == len(parsed.transcript) and event.offset == end)
        )
    )
    return replace(
        parsed,
        transcript=parsed.transcript[start:end],
        spans=tuple(spans),
        events=events,
    )


def _project_performance(
    settings: dict[str, Any],
    *,
    parsed: ParsedSpeechMarkup,
    group: _Group,
    performance_enabled: bool,
    xml_present: bool,
) -> None:
    if not performance_enabled:
        settings.pop("_performance", None)
        settings["performance_enabled"] = False
        return
    if xml_present:
        settings["_performance"] = markup_to_annotation(
            _subset(parsed, group.start, group.end)
        )
        return
    existing = settings.get("_performance")
    if existing is not None:
        # Validate the legacy full-segment record, but retain its supplied JSON
        # shape because no XML projection is needed for the one synthetic part.
        validate_annotation(parsed.transcript, existing)


def _merge_context(
    settings: dict[str, Any],
    *,
    groups: list[_Group],
    index: int,
    mode: str,
    source_speaker: str | None,
    xml_present: bool,
) -> None:
    raw = settings.get("_semantic_context")
    context: dict[str, Any]
    if isinstance(raw, Mapping):
        context = deepcopy(dict(raw))
    elif raw is None:
        context = {}
    else:
        context = {"before": raw}
    if mode not in {"off", "before", "both"}:
        raise ValueError("tts_context_mode must be off, before, or both")
    if mode == "off":
        if raw is not None:
            settings["_semantic_context"] = context
        return

    def label(group: _Group) -> str:
        ids = _speaker_ids(
            group.items,
            source_speaker=source_speaker,
            xml_present=xml_present,
        )
        return ids[0] if ids else "other voice"

    def sibling(group: _Group) -> str:
        text = group.text
        return f"[Other speaker: {label(group)}] {text}"

    def append(name: str, value: str) -> None:
        if not value:
            return
        existing = context.get(name)
        if existing:
            context[name] = f"{existing}\n{value}"
        else:
            context[name] = value

    if index > 0:
        append("before", sibling(groups[index - 1]))
    if mode == "both" and index + 1 < len(groups):
        append("after", sibling(groups[index + 1]))
    if context or raw is not None:
        settings["_semantic_context"] = context


def build_render_parts(
    text: str,
    settings: dict[str, Any],
    *,
    speech_xml: str | None = None,
    segment_id: str = "",
    controls: dict[str, Any] | None = None,
    source_speaker: str | None = None,
    apply_binding: Callable[[dict[str, Any] | None, dict[str, Any]], dict[str, Any]]
    | None = None,
) -> list[dict[str, Any]]:
    """Plan ordered provider requests for one logical segment."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if not isinstance(settings, dict):
        raise TypeError("settings must be a dictionary")
    control_values = _control_values(settings, controls)
    xml_present = speech_xml is not None
    if xml_present:
        parsed = parse_speech_markup(
            speech_xml,
            expected_segment_id=segment_id,
            expected_text=text,
            characters=control_values.get("characters"),
        )
    else:
        parsed = _synthetic_markup(
            text, source_speaker=source_speaker, settings=settings
        )
    if not any(_is_speakable_text(span.text) for span in parsed.spans):
        if parsed.events:
            raise ValueError(
                "Speech segment contains vocal events but no speakable letter or "
                "number text; event-only rendering is unsupported."
            )
        raise ValueError(
            "Speech segment contains no speakable letter or number text; "
            "punctuation-only input cannot be synthesized."
        )
    performance_enabled = bool(settings.get("performance_enabled", True))
    casting_enabled = bool(settings.get("casting_enabled", False))
    planned: list[_PlannedSpan] = []
    for span in parsed.spans:
        prepared = deepcopy(settings)
        if casting_enabled:
            prepared, source, fallback, binding = _binding_result(
                prepared,
                controls=control_values,
                span=span,
                source_speaker=source_speaker,
                xml_present=xml_present,
                apply_binding=apply_binding,
            )
        else:
            source, fallback, binding = "base", False, None
        planned.append(
            _PlannedSpan(
                span=span,
                settings=prepared,
                voice_key=_voice_key(prepared, binding),
                voice_source=source,
                fallback=fallback,
            )
        )
    groups = _groups(planned)
    if len(groups) > 64:
        raise ValueError("A segment cannot contain more than 64 render parts.")
    mode = str(settings.get("tts_context_mode") or "off")
    if mode not in {"off", "before", "both"}:
        raise ValueError("tts_context_mode must be off, before, or both")
    result: list[dict[str, Any]] = []
    for index, group in enumerate(groups):
        anchor = group.anchor
        group_settings = deepcopy(anchor.settings)
        _project_performance(
            group_settings,
            parsed=parsed,
            group=group,
            performance_enabled=performance_enabled,
            xml_present=xml_present,
        )
        _merge_context(
            group_settings,
            groups=groups,
            index=index,
            mode=mode,
            source_speaker=source_speaker,
            xml_present=xml_present,
        )
        speaker_ids = _speaker_ids(
            group.items,
            source_speaker=source_speaker,
            xml_present=xml_present,
        )
        result.append(
            {
                "start": group.start,
                "end": group.end,
                "text": group.text,
                "settings": group_settings,
                "voice_source": anchor.voice_source,
                "fallback": all(item.fallback for item in group.items),
                "speaker_ids": speaker_ids,
                "index": index,
            }
        )
    return result


def execute_render_parts(
    parts: list[dict[str, Any]],
    *,
    synthesize: Callable[[str, dict[str, Any]], AudioSegment],
    cancelled: Callable[[], bool],
) -> tuple[AudioSegment, list[dict[str, Any]]]:
    """Synthesize and concatenate all planned parts without partial results."""

    if not parts:
        raise RuntimeError("No renderable speech parts were supplied.")
    combined: AudioSegment | None = None
    manifest: list[dict[str, Any]] = []
    for index, part in enumerate(parts):
        if cancelled():
            raise RuntimeError("Speech rendering was cancelled.")
        audio = synthesize(
            str(part.get("text") or ""), deepcopy(part.get("settings") or {})
        )
        if cancelled():
            raise RuntimeError("Speech rendering was cancelled.")
        if audio is None:
            raise RuntimeError(f"Speech part {index} produced no audio.")
        try:
            duration_ms = len(audio)
        except Exception as exc:
            raise RuntimeError(f"Speech part {index} returned invalid audio.") from exc
        if duration_ms <= 0:
            raise RuntimeError(f"Speech part {index} produced no audio.")
        combined = audio if combined is None else combined + audio
        manifest.append(
            {
                "index": int(part.get("index", index)),
                "range": [int(part.get("start", 0)), int(part.get("end", 0))],
                "voice": (part.get("settings") or {}).get("voice")
                or (part.get("settings") or {}).get("speaker")
                or (part.get("settings") or {}).get("voice_id"),
                "source": part.get("voice_source", "base"),
                "fallback": bool(part.get("fallback")),
                "speaker_ids": list(part.get("speaker_ids") or []),
                "duration_ms": duration_ms,
            }
        )
    if combined is None:
        raise RuntimeError("Speech rendering produced no audio.")
    return combined, manifest


__all__ = ["build_render_parts", "execute_render_parts"]
