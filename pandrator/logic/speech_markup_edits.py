"""Offset-safe edits for the bounded speech-markup dialect.

The parser is the authority for source validation and for the effective
metadata carried by each text span.  Edits rebuild a small canonical document
from that parsed representation and run the rebuilt document through the same
guarded parser before returning it.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .speech_markup import (
    ParsedSpeechMarkup,
    SpeechMarkupEvent,
    SpeechMarkupSpan,
    _escape_attribute,
    _escape_text,
    _parse_xml,
    _serialize,
    event_in_range,
    parse_speech_markup,
)
from .speech_performance import Delivery, VocalEvent

_CONTROL_TAGS = (
    ("instruction", "ins"),
    ("emotion", "em"),
    ("pace", "pace"),
    ("cadence", "cadence"),
    ("emphasis", "emphasis"),
)

_VOICE_UNCHANGED = object()
_DELIVERY_FIELDS = frozenset(field for field, _tag in _CONTROL_TAGS) | {
    "emotion_intensity"
}


def _event_xml(event: SpeechMarkupEvent) -> str:
    attributes = [f'kind="{_escape_attribute(event.kind)}"']
    if event.duration_ms is not None:
        attributes.append(f'duration_ms="{event.duration_ms}"')
    if event.affinity != "following":
        attributes.append(f'affinity="{_escape_attribute(event.affinity)}"')
    return "<event " + " ".join(attributes) + "/>"


def _span_opening(span: SpeechMarkupSpan) -> tuple[str, str]:
    """Return the effective scope's opening and closing tags."""

    attributes: list[tuple[str, str]] = []
    if span.narrator:
        tag = "narrator"
        if span.voice:
            attributes.append(("voice", span.voice))
    elif span.dialogue:
        tag = "speaker" if (
            span.speaker_id or span.voice_category != "unspecified" or span.voice
        ) else "dialogue"
        if tag == "speaker":
            if span.speaker_id:
                attributes.append(("ref", span.speaker_id))
            elif span.voice_category != "unspecified":
                attributes.append(("g", span.voice_category))
            if span.voice:
                attributes.append(("voice", span.voice))
    else:
        tag = "span"
        if span.voice:
            attributes.append(("voice", span.voice))

    opening = "<" + tag + "".join(
        f' {key}="{_escape_attribute(value)}"' for key, value in attributes
    )
    closing = "</" + tag + ">"
    return opening, closing


def _emotion_control_xml(emotion: str, intensity: float | None) -> str:
    if not emotion:
        if intensity is not None:
            raise ValueError("emotion_intensity requires a non-empty emotion.")
        return "<em/>"
    attribute = (
        ""
        if intensity is None
        else f' intensity="{_escape_attribute(str(intensity))}"'
    )
    return f"<em{attribute}>{_escape_text(emotion)}</em>"


def _scope_xml(
    span: SpeechMarkupSpan,
    content: str,
    *,
    root_delivery: dict[str, Any] | None = None,
) -> str:
    opening, closing = _span_opening(span)
    nested_instruction: str | None = None
    if root_delivery is None:
        controls_list: list[str] = []
        for field, tag in _CONTROL_TAGS:
            if field == "emotion":
                emotion = str(span.delivery.get("emotion") or "")
                intensity = span.delivery.get("emotion_intensity")
                if emotion or intensity is not None:
                    controls_list.append(_emotion_control_xml(emotion, intensity))
                continue
            value = span.delivery.get(field)
            if value:
                controls_list.append(
                    f"<{tag}>{_escape_text(str(value))}</{tag}>"
                )
        controls = "".join(controls_list)
    else:
        local_controls: list[str] = []
        for field, tag in _CONTROL_TAGS:
            if field == "emotion":
                effective_emotion = str(span.delivery.get("emotion") or "")
                root_emotion = str(root_delivery.get("emotion") or "")
                effective_intensity = span.delivery.get("emotion_intensity")
                root_intensity = root_delivery.get("emotion_intensity")
                if (
                    effective_emotion == root_emotion
                    and effective_intensity == root_intensity
                ):
                    continue
                local_controls.append(
                    _emotion_control_xml(effective_emotion, effective_intensity)
                )
                continue
            effective_value = str(span.delivery.get(field) or "")
            root_value = str(root_delivery.get(field) or "")
            if effective_value == root_value:
                continue
            if field == "instruction":
                prefix = f"{root_value}\n" if root_value else "\n"
                if root_value and effective_value.startswith(prefix):
                    local_value = effective_value[len(prefix) :]
                    if local_value:
                        local_controls.append(
                            f"<ins>{_escape_text(local_value)}</ins>"
                        )
                else:
                    # Instructions append to their inherited value. Clear the
                    # retained root first when the effective value replaces it.
                    local_controls.append("<ins/>")
                    if effective_value:
                        nested_instruction = effective_value
                continue
            if effective_value:
                local_controls.append(
                    f"<{tag}>{_escape_text(effective_value)}</{tag}>"
                )
            else:
                # Empty local controls mask a non-empty inherited value.
                local_controls.append(f"<{tag}/>")
        controls = "".join(local_controls)
    if nested_instruction is not None:
        content = (
            "<span><ins>"
            + _escape_text(nested_instruction)
            + "</ins>"
            + content
            + "</span>"
        )
    # A raw root span is not needed for the default scope.  Keeping it as text
    # avoids introducing a needless span boundary around plain separators.
    if (
        not span.narrator
        and not span.dialogue
        and not span.voice
        and not controls
    ):
        return content
    return opening + ">" + controls + content + closing


def _events_by_offset(events: Iterable[SpeechMarkupEvent]) -> dict[int, list[SpeechMarkupEvent]]:
    grouped: dict[int, list[SpeechMarkupEvent]] = {}
    for event in events:
        grouped.setdefault(event.offset, []).append(event)
    return grouped


def _span_xml(
    parsed: ParsedSpeechMarkup,
    span: SpeechMarkupSpan,
    events: dict[int, list[SpeechMarkupEvent]],
    *,
    root_delivery: dict[str, Any] | None = None,
) -> str:
    """Serialize one span, inserting events at their exact offsets."""

    pieces: list[str] = []
    cursor = span.start
    boundaries = sorted(offset for offset in events if span.start <= offset < span.end)
    for offset in boundaries:
        pieces.append(_escape_text(parsed.transcript[cursor:offset]))
        pieces.extend(_event_xml(event) for event in events[offset])
        cursor = offset
    pieces.append(_escape_text(parsed.transcript[cursor:span.end]))
    pieces.extend(_event_xml(event) for event in events.get(span.end, ()))
    return _scope_xml(span, "".join(pieces), root_delivery=root_delivery)


def _serialize_body(
    parsed: ParsedSpeechMarkup,
    *,
    start: int = 0,
    end: int | None = None,
    include_end_events: bool = True,
    root_delivery: dict[str, Any] | None = None,
) -> str:
    """Serialize a parsed transcript range without a segment root."""

    if end is None:
        end = len(parsed.transcript)
    selected_events = tuple(
        event
        for event in parsed.events
        if (event_in_range(event, start, end, len(parsed.transcript))
            and (include_end_events or event.offset != len(parsed.transcript)))
        or (start == end == 0 and include_end_events and event.offset == 0)
    )
    body: list[str] = []
    spans = [span for span in parsed.spans if span.end > start and span.start < end]
    # Affinity decides which range owns an event. Within one serialized range,
    # keep every event at the same offset together so authored order survives
    # even when their affinities differ.
    span_events: list[list[SpeechMarkupEvent]] = [[] for _ in spans]
    for event in selected_events:
        for index, span in enumerate(spans):
            if max(start, span.start) <= event.offset < min(end, span.end):
                span_events[index].append(event)
                break
        else:
            if spans and event.offset == end:
                span_events[-1].append(event)
    cursor = start
    for index, span in enumerate(spans):
        left = max(start, span.start)
        right = min(end, span.end)
        if left > cursor:
            body.append(_escape_text(parsed.transcript[cursor:left]))
        if left == span.start and right == span.end:
            body.append(
                _span_xml(
                    parsed,
                    span,
                    _events_by_offset(span_events[index]),
                    root_delivery=root_delivery,
                )
            )
        else:
            clipped = SpeechMarkupSpan(
                start=left,
                end=right,
                text=parsed.transcript[left:right],
                speaker_id=span.speaker_id,
                voice_category=span.voice_category,
                voice=span.voice,
                dialogue=span.dialogue,
                delivery=dict(span.delivery),
                narrator=span.narrator,
            )
            body.append(
                _span_xml(
                    parsed,
                    clipped,
                    _events_by_offset(span_events[index]),
                    root_delivery=root_delivery,
                )
            )
        cursor = right
    if cursor < end:
        body.append(_escape_text(parsed.transcript[cursor:end]))
    if not spans:
        body.extend(_event_xml(event) for event in selected_events)
    return "".join(body)


def _serialize_segment(
    *,
    segment_id: str,
    body: str,
    boundary_after: str | None,
) -> str:
    attributes = [f'id="{_escape_attribute(segment_id)}"']
    if boundary_after is not None:
        attributes.append(f'boundary_after="{_escape_attribute(boundary_after)}"')
    return "<segment " + " ".join(attributes) + ">" + body + "</segment>"


def _parse_rebuilt(
    xml: str,
    *,
    segment_id: str,
    text: str,
    characters: list[dict[str, Any]] | None,
) -> str:
    return parse_speech_markup(
        xml,
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    ).xml


def _root_control_context(
    parsed: ParsedSpeechMarkup,
) -> tuple[str, dict[str, Any]]:
    """Return direct-root controls and their parser-resolved delivery values."""

    root = _parse_xml(parsed.xml)
    control_tags = {tag for _field, tag in _CONTROL_TAGS}
    root_controls = "".join(
        _serialize(child) for child in root if child.tag in control_tags
    )
    probe = _serialize_segment(
        segment_id=parsed.segment_id,
        body=root_controls + "x",
        boundary_after=None,
    )
    root_scope = parse_speech_markup(
        probe,
        expected_segment_id=parsed.segment_id,
        expected_text="x",
    )
    return root_controls, dict(root_scope.spans[0].delivery)


def _validate_range(source_text: str, start: int, end: int) -> None:
    if isinstance(start, bool) or isinstance(end, bool):
        raise TypeError("start and end must be integer code-point offsets")
    if not isinstance(start, int) or not isinstance(end, int):
        raise TypeError("start and end must be integer code-point offsets")
    if start < 0 or end < start or end > len(source_text):
        raise ValueError(
            f"invalid speech markup range {start}:{end} for text of length {len(source_text)}"
        )


def insert_speech_markup_event(
    xml: str,
    *,
    segment_id: str,
    text: str,
    characters: list[dict[str, Any]] | None,
    offset: int,
    kind: str,
    duration_ms: int | None = None,
    affinity: str = "following",
) -> str:
    """Insert one event into the validated source tree without rebuilding scopes."""

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    _validate_range(text, offset, offset)
    if affinity not in {"following", "previous"}:
        raise ValueError("affinity must be following or previous")
    event_model = VocalEvent.model_validate({"kind": kind, "duration_ms": duration_ms})
    parsed = parse_speech_markup(
        xml, expected_segment_id=segment_id, expected_text=text, characters=characters
    )
    root = _parse_xml(parsed.xml)
    event_attributes = {"kind": event_model.kind, "affinity": affinity}
    if event_model.duration_ms is not None:
        event_attributes["duration_ms"] = str(event_model.duration_ms)
    event = ET.Element("event", event_attributes)
    cursor = 0
    inserted = False

    def split_slot(parent: ET.Element, preceding: ET.Element | None, value: str) -> bool:
        nonlocal cursor, inserted
        end = cursor + len(value)
        if value and cursor <= offset <= end:
            prefix = value[: offset - cursor]
            event.tail = value[offset - cursor :]
            if preceding is None:
                parent.text = prefix
                parent.insert(0, event)
            else:
                preceding.tail = prefix
                parent.insert(list(parent).index(preceding) + 1, event)
            inserted = True
            return True
        cursor = end
        return False

    def walk(element: ET.Element) -> None:
        if inserted:
            return
        if element.tag not in {"event", "ins", "em", "pace", "cadence", "emphasis"}:
            if split_slot(element, None, element.text or ""):
                return
            for child in list(element):
                walk(child)
                if inserted:
                    return
                if split_slot(element, child, child.tail or ""):
                    return

    walk(root)
    if not inserted:
        # The authoritative parser permits an event-only, empty transcript.
        root.insert(0, event)
    return _parse_rebuilt(
        _serialize(root), segment_id=segment_id, text=text, characters=characters
    )


def slice_speech_markup(
    xml: str,
    *,
    source_id: str,
    source_text: str,
    segment_id: str,
    start: int,
    end: int,
    characters: list[dict[str, Any]] | None = None,
    boundary_after: str | None = None,
    include_end_events: bool = False,
) -> str:
    """Return a structurally faithful code-point slice of one segment."""

    if not isinstance(source_text, str):
        raise TypeError("source_text must be a string")
    _validate_range(source_text, start, end)
    parsed = parse_speech_markup(
        xml,
        expected_segment_id=source_id,
        expected_text=source_text,
        characters=characters,
    )
    body = _serialize_body(
        parsed,
        start=start,
        end=end,
        include_end_events=include_end_events,
    )
    rebuilt = _serialize_segment(
        segment_id=segment_id,
        body=body,
        boundary_after=boundary_after,
    )
    return _parse_rebuilt(
        rebuilt,
        segment_id=segment_id,
        text=source_text[start:end],
        characters=characters,
    )


def join_speech_markup(
    units: list[tuple[str, str, str | None]],
    *,
    segment_id: str,
    separator: str = " ",
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Join exact text units while keeping separators outside speaker scopes."""

    if not isinstance(units, list):
        raise TypeError("units must be a list")
    if not isinstance(separator, str):
        raise TypeError("separator must be a string")
    parsed_units: list[ParsedSpeechMarkup | None] = []
    texts: list[str] = []
    for unit in units:
        if not isinstance(unit, tuple) or len(unit) != 3:
            raise TypeError("each unit must be a (source_id, text, xml) tuple")
        source_id, text, xml = unit
        if not isinstance(source_id, str) or not source_id:
            raise ValueError("unit source_id must be a non-empty string")
        if not isinstance(text, str):
            raise TypeError("unit text must be a string")
        if xml is None:
            parsed_units.append(None)
        else:
            if not isinstance(xml, str):
                raise TypeError("unit xml must be a string or None")
            parsed_units.append(
                parse_speech_markup(
                    xml,
                    expected_segment_id=source_id,
                    expected_text=text,
                    characters=characters,
                )
            )
        texts.append(text)

    body: list[str] = []
    for index, (parsed, text) in enumerate(zip(parsed_units, texts, strict=True)):
        if index:
            body.append(_escape_text(separator))
        if parsed is None:
            body.append(_escape_text(text))
        else:
            body.append(_serialize_body(parsed))
    joined_text = separator.join(texts)
    last_parsed = parsed_units[-1] if parsed_units else None
    boundary_after = last_parsed.boundary_after if last_parsed is not None else None
    rebuilt = _serialize_segment(
        segment_id=segment_id,
        body="".join(body),
        boundary_after=boundary_after,
    )
    return _parse_rebuilt(
        rebuilt,
        segment_id=segment_id,
        text=joined_text,
        characters=characters,
    )


def edit_speech_markup_range(
    xml: str,
    *,
    segment_id: str,
    text: str,
    characters: list[dict[str, Any]] | None,
    start: int,
    end: int,
    speaker: str = "unchanged",
    character_id: str | None = None,
    voice_category: str | None = None,
    voice: str | None | object = _VOICE_UNCHANGED,
    delivery: dict[str, Any] | None = None,
) -> str:
    """Edit only the effective metadata intersecting a text range.

    Offsets are Python code-point offsets into the accepted transcript.  A
    selected span is split at both boundaries before applying the requested
    metadata, so adjacent source scopes, events, whitespace, and the segment
    boundary retain their parsed meaning.  The rebuilt document is always
    validated by the same guarded parser before it is returned.
    """

    if not isinstance(text, str):
        raise TypeError("text must be a string")
    _validate_range(text, start, end)
    if start == end:
        raise ValueError("The selected speech range must not be empty.")
    if speaker not in {"unchanged", "narrator", "dialogue", "character"}:
        raise ValueError("speaker must be unchanged, narrator, dialogue, or character")
    if voice_category is not None and (
        speaker != "dialogue"
        or voice_category not in {"male", "female", "androgynous", "unspecified"}
    ):
        raise ValueError("voice_category is only valid for a dialogue selection")
    character_category = "unspecified"
    if speaker == "character":
        if not isinstance(character_id, str) or not character_id:
            raise ValueError("character_id is required for a character selection")
        catalog = characters or []
        matches = [
            entry
            for entry in catalog
            if isinstance(entry, dict) and entry.get("id") == character_id
        ]
        if len(matches) != 1:
            raise ValueError(f"Unknown character_id: {character_id!r}")
        character_category = str(matches[0].get("voice_category") or "unspecified")
    elif character_id is not None:
        raise ValueError("character_id is only valid for a character selection")
    if voice is not _VOICE_UNCHANGED and voice is not None:
        if not isinstance(voice, str) or not voice:
            raise ValueError("voice must be a non-empty string or null")
    if delivery is not None:
        if not isinstance(delivery, dict):
            raise TypeError("delivery must be a mapping or None")
        unknown = set(delivery) - _DELIVERY_FIELDS
        if unknown:
            raise ValueError(f"Unknown delivery field(s): {sorted(unknown)}")
        for field, value in delivery.items():
            if field == "emotion_intensity":
                if value is not None and (
                    type(value) not in {int, float}
                    or not math.isfinite(value)
                    or not 0 <= value <= 2
                ):
                    raise ValueError(
                        "delivery.emotion_intensity must be a finite number from 0 to 2 or null"
                    )
            elif value is not None and not isinstance(value, str):
                raise TypeError(f"delivery.{field} must be a string or null")
            if isinstance(value, str) and len(value) > 2000:
                raise ValueError(f"delivery.{field} exceeds the 2000 character limit")

    parsed = parse_speech_markup(
        xml,
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    )
    root_controls, root_delivery = _root_control_context(parsed)
    updated: list[SpeechMarkupSpan] = []
    for span in parsed.spans:
        left = span.start
        right = span.end
        if right <= start or left >= end:
            updated.append(span)
            continue
        # Split at the selection boundaries first.  Each fragment gets a
        # copy of the source effective scope, preserving metadata outside the
        # selected range even when the range crosses existing scopes.
        cuts = sorted({left, right, max(left, start), min(right, end)})
        for fragment_start, fragment_end in zip(cuts, cuts[1:], strict=False):
            if fragment_end <= fragment_start:
                continue
            fragment = replace(
                span,
                start=fragment_start,
                end=fragment_end,
                text=text[fragment_start:fragment_end],
            )
            if fragment_start >= start and fragment_end <= end:
                values = {
                    "speaker_id": fragment.speaker_id,
                    "voice_category": fragment.voice_category,
                    "voice": fragment.voice,
                    "dialogue": fragment.dialogue,
                    "narrator": fragment.narrator,
                }
                if speaker == "narrator":
                    values.update(
                        speaker_id=None,
                        voice_category="unspecified",
                        dialogue=False,
                        narrator=True,
                    )
                elif speaker == "character":
                    values.update(
                        speaker_id=character_id,
                        voice_category=character_category,
                        dialogue=True,
                        narrator=False,
                    )
                elif speaker == "dialogue":
                    values.update(
                        speaker_id=None,
                        voice_category=voice_category or "unspecified",
                        dialogue=True,
                        narrator=False,
                    )
                if voice is not _VOICE_UNCHANGED:
                    values["voice"] = voice
                next_delivery = dict(fragment.delivery)
                for field, value in (delivery or {}).items():
                    next_delivery[field] = (
                        value
                        if field == "emotion_intensity"
                        else value or ""
                    )
                if delivery is not None and not str(delivery.get("emotion") or "").strip() and "emotion" in delivery:
                    # Clearing emotion also clears its coupled intensity even
                    # when a stale slider value was included in the same patch.
                    next_delivery["emotion_intensity"] = None
                next_delivery = Delivery.model_validate(next_delivery).model_dump(
                    mode="json"
                )
                fragment = replace(
                    fragment,
                    speaker_id=values["speaker_id"],
                    voice_category=values["voice_category"],
                    voice=values["voice"],
                    dialogue=values["dialogue"],
                    narrator=values["narrator"],
                    delivery=next_delivery,
                )
            updated.append(fragment)

    rebuilt = _serialize_segment(
        segment_id=parsed.segment_id,
        body=root_controls
        + _serialize_body(
            replace(parsed, spans=tuple(updated)),
            include_end_events=True,
            root_delivery=root_delivery,
        ),
        boundary_after=parsed.boundary_after,
    )
    return _parse_rebuilt(
        rebuilt,
        segment_id=segment_id,
        text=text,
        characters=characters,
    )


__all__ = [
    "edit_speech_markup_range",
    "insert_speech_markup_event",
    "join_speech_markup",
    "slice_speech_markup",
]
