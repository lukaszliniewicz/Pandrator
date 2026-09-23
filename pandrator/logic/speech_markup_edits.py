"""Offset-safe edits for the bounded speech-markup dialect.

The parser is the authority for source validation and for the effective
metadata carried by each text span.  Edits rebuild a small canonical document
from that parsed representation and run the rebuilt document through the same
guarded parser before returning it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from typing import Any

from .speech_markup import (
    ParsedSpeechMarkup,
    SpeechMarkupEvent,
    SpeechMarkupSpan,
    _escape_attribute,
    _escape_text,
    parse_speech_markup,
)

_CONTROL_TAGS = (
    ("instruction", "ins"),
    ("emotion", "em"),
    ("pace", "pace"),
    ("cadence", "cadence"),
    ("emphasis", "emphasis"),
)

_VOICE_UNCHANGED = object()
_DELIVERY_FIELDS = frozenset(field for field, _tag in _CONTROL_TAGS)


def _event_xml(event: SpeechMarkupEvent) -> str:
    attributes = [f'kind="{_escape_attribute(event.kind)}"']
    if event.duration_ms is not None:
        attributes.append(f'duration_ms="{event.duration_ms}"')
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


def _scope_xml(span: SpeechMarkupSpan, content: str) -> str:
    opening, closing = _span_opening(span)
    controls = "".join(
        f"<{tag}>{_escape_text(str(span.delivery.get(field) or ''))}</{tag}>"
        for field, tag in _CONTROL_TAGS
        if span.delivery.get(field)
    )
    # A raw root span is not needed for the default scope.  Keeping it as text
    # avoids introducing a needless span boundary around plain separators.
    if (
        not span.narrator
        and not span.dialogue
        and not span.voice
        and not any(span.delivery.get(field) for field, _ in _CONTROL_TAGS)
    ):
        return content
    return opening + ">" + controls + content + closing


def _events_by_offset(events: Iterable[SpeechMarkupEvent]) -> dict[int, list[str]]:
    grouped: dict[int, list[str]] = {}
    for event in events:
        grouped.setdefault(event.offset, []).append(_event_xml(event))
    return grouped


def _span_xml(
    parsed: ParsedSpeechMarkup,
    span: SpeechMarkupSpan,
    events: dict[int, list[str]],
) -> str:
    """Serialize one span, inserting events at their exact offsets."""

    pieces: list[str] = []
    cursor = span.start
    boundaries = sorted(
        offset for offset in events if span.start <= offset < span.end
    )
    for offset in boundaries:
        pieces.append(_escape_text(parsed.transcript[cursor:offset]))
        pieces.extend(events[offset])
        cursor = offset
    pieces.append(_escape_text(parsed.transcript[cursor:span.end]))
    return _scope_xml(span, "".join(pieces))


def _serialize_body(
    parsed: ParsedSpeechMarkup,
    *,
    start: int = 0,
    end: int | None = None,
    include_end_events: bool = True,
) -> str:
    """Serialize a parsed transcript range without a segment root."""

    if end is None:
        end = len(parsed.transcript)
    events = _events_by_offset(
        event
        for event in parsed.events
        if event.offset >= start
        and (event.offset < end or (include_end_events and event.offset == end))
    )
    body: list[str] = []
    spans = [span for span in parsed.spans if span.end > start and span.start < end]
    cursor = start
    for span in spans:
        left = max(start, span.start)
        right = min(end, span.end)
        if left > cursor:
            body.append(_escape_text(parsed.transcript[cursor:left]))
        if left == span.start and right == span.end:
            body.append(_span_xml(parsed, span, events))
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
            body.append(_span_xml(parsed, clipped, events))
        cursor = right
    if cursor < end:
        body.append(_escape_text(parsed.transcript[cursor:end]))
    # End events are outside the final text scope.  This also gives event-only
    # documents a stable representation and prevents a boundary event from
    # being emitted a second time by a neighboring span.
    for offset in sorted(events):
        if offset == end:
            body.extend(events[offset])
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


def _validate_range(source_text: str, start: int, end: int) -> None:
    if isinstance(start, bool) or isinstance(end, bool):
        raise TypeError("start and end must be integer code-point offsets")
    if not isinstance(start, int) or not isinstance(end, int):
        raise TypeError("start and end must be integer code-point offsets")
    if start < 0 or end < start or end > len(source_text):
        raise ValueError(
            f"invalid speech markup range {start}:{end} for text of length {len(source_text)}"
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
    if speaker not in {"unchanged", "narrator", "character"}:
        raise ValueError("speaker must be unchanged, narrator, or character")
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
            if value is not None and not isinstance(value, str):
                raise TypeError(f"delivery.{field} must be a string or null")
            if isinstance(value, str) and len(value) > 2000:
                raise ValueError(f"delivery.{field} exceeds the 2000 character limit")

    parsed = parse_speech_markup(
        xml,
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    )
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
                if voice is not _VOICE_UNCHANGED:
                    values["voice"] = voice
                next_delivery = dict(fragment.delivery)
                for field, value in (delivery or {}).items():
                    next_delivery[field] = value or ""
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
        body=_serialize_body(
            replace(parsed, spans=tuple(updated)),
            include_end_events=True,
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
    "join_speech_markup",
    "slice_speech_markup",
]
