"""Bounded, provider-independent XML speech markup.

The markup in this module is deliberately smaller than SSML.  It describes
the accepted transcript, speaker structure, and delivery intent; it is never
passed to a synthesis provider unchanged.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import regex

from .speech_performance import Delivery, VocalEvent, validate_annotation

VOICE_CATEGORIES = ("male", "female", "androgynous", "unspecified")

_SCHEMA = "pandrator.performance/v1"
_MAX_XML_BYTES = 256 * 1024
_MAX_NODES = 2048
_MAX_DEPTH = 24
_BOUNDARIES = frozenset(
    {"continuation", "dialogue_turn", "paragraph", "scene", "chapter"}
)
_CONTROL_FIELDS = {
    "ins": "instruction",
    "em": "emotion",
    "pace": "pace",
    "cadence": "cadence",
    "emphasis": "emphasis",
}
_CONTROL_TAGS = frozenset(_CONTROL_FIELDS)
_ELEMENT_TAGS = frozenset(
    {"segment", "dialogue", "speaker", "narrator", "span", "event", *_CONTROL_TAGS}
)
_ELEMENT_ATTRIBUTES = {
    "segment": frozenset({"id", "version", "boundary_after"}),
    "dialogue": frozenset({"id"}),
    "speaker": frozenset({"ref", "n", "g", "voice"}),
    "narrator": frozenset({"voice"}),
    "span": frozenset({"id", "voice"}),
    "event": frozenset({"kind", "duration_ms"}),
    "ins": frozenset(),
    "em": frozenset(),
    "pace": frozenset(),
    "cadence": frozenset(),
    "emphasis": frozenset(),
}
_LEAF_TAGS = frozenset({"event", *_CONTROL_TAGS})


@dataclass(frozen=True, slots=True)
class SpeechMarkupSpan:
    start: int
    end: int
    text: str
    speaker_id: str | None
    voice_category: str
    voice: str | None
    dialogue: bool
    delivery: dict[str, Any]
    narrator: bool = False

    def public(self) -> dict[str, Any]:
        return {
            "start": self.start,
            "end": self.end,
            "text": self.text,
            "speaker_id": self.speaker_id,
            "voice_category": self.voice_category,
            "voice": self.voice,
            "dialogue": self.dialogue,
            "delivery": dict(self.delivery),
            "narrator": self.narrator,
        }


@dataclass(frozen=True, slots=True)
class SpeechMarkupEvent:
    offset: int
    kind: str
    duration_ms: int | None

    def public(self) -> dict[str, Any]:
        return {
            "offset": self.offset,
            "kind": self.kind,
            "duration_ms": self.duration_ms,
        }


@dataclass(frozen=True, slots=True)
class ParsedSpeechMarkup:
    segment_id: str
    transcript: str
    spans: tuple[SpeechMarkupSpan, ...]
    events: tuple[SpeechMarkupEvent, ...]
    xml: str
    boundary_after: str | None

    def public(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "transcript": self.transcript,
            "spans": [span.public() for span in self.spans],
            "events": [event.public() for event in self.events],
            "xml": self.xml,
            "boundary_after": self.boundary_after,
        }


@dataclass(frozen=True, slots=True)
class _Scope:
    speaker_id: str | None = None
    voice_category: str = "unspecified"
    voice: str | None = None
    dialogue: bool = False
    delivery: dict[str, Any] | None = None
    narrator: bool = False


@dataclass(frozen=True, slots=True)
class _Character:
    identifier: str
    voice_category: str
    names: tuple[str, ...]


def _error(message: str) -> ValueError:
    return ValueError(f"Invalid speech markup: {message}")


def _ensure_xml_characters(value: str) -> None:
    for character in value:
        codepoint = ord(character)
        if not (
            codepoint in {0x9, 0xA, 0xD}
            or 0x20 <= codepoint <= 0xD7FF
            or 0xE000 <= codepoint <= 0xFFFD
            or 0x10000 <= codepoint <= 0x10FFFF
        ):
            raise _error("text contains a character that XML 1.0 cannot represent")


def _escape_text(value: str) -> str:
    _ensure_xml_characters(value)
    # Explicitly encode carriage returns so the canonical document reparses to
    # the same ordinary text on every XML implementation.
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", "&#13;")
    )


def _escape_attribute(value: str) -> str:
    _ensure_xml_characters(value)
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace('"', "&quot;")
        .replace("\t", "&#9;")
        .replace("\n", "&#10;")
        .replace("\r", "&#13;")
    )


def plain_speech_markup(segment_id: str, text: str) -> str:
    """Return a minimal segment document containing exactly ``text``."""

    if not isinstance(segment_id, str) or not segment_id:
        raise ValueError("segment_id must be a non-empty string")
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    return (
        f'<segment id="{_escape_attribute(segment_id)}">{_escape_text(text)}</segment>'
    )


def _parse_xml(xml: str) -> ET.Element:
    if not isinstance(xml, str):
        raise TypeError("xml must be a string")
    try:
        size = len(xml.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _error("document is not valid UTF-8 text") from exc
    if size > _MAX_XML_BYTES:
        raise _error("document exceeds the 256 KiB limit")
    # ET's standard parser expands predefined entities, which is desirable for
    # escaped prose.  DTDs and declarations are rejected before parsing so no
    # external or user-defined entity can be consulted.
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", xml, flags=re.IGNORECASE):
        raise _error("DTD and entity declarations are not allowed")
    if "<![" in xml:
        raise _error("CDATA and declaration sections are not allowed")
    if "<!--" in xml or "-->" in xml:
        raise _error("comments are not allowed")
    if re.search(r"<\?(?!xml(?:\s|\?|>))", xml, flags=re.IGNORECASE):
        raise _error("processing instructions are not allowed")
    try:
        parser = ET.XMLParser(
            target=ET.TreeBuilder(insert_comments=True, insert_pis=True)
        )
        root = ET.fromstring(xml, parser=parser)
    except (ET.ParseError, ValueError) as exc:
        raise _error("malformed XML") from exc
    return root


def _local_tag(tag: Any) -> str:
    if not isinstance(tag, str):
        raise _error("comments and processing instructions are not allowed")
    if tag.startswith("{") or ":" in tag:
        raise _error("namespaces are not allowed")
    return tag


def _check_attributes(element: ET.Element, tag: str) -> None:
    for attribute in element.attrib:
        if attribute.startswith("{") or ":" in attribute:
            raise _error("namespaced attributes are not allowed")
    unknown = set(element.attrib) - _ELEMENT_ATTRIBUTES[tag]
    if unknown:
        names = ", ".join(sorted(unknown))
        raise _error(f"unknown attribute(s) on <{tag}>: {names}")


def _character_catalog(
    characters: list[dict[str, Any]] | None,
) -> tuple[_Character, ...]:
    if characters is None:
        return ()
    if not isinstance(characters, list):
        raise TypeError("characters must be a list of dictionaries")
    result: list[_Character] = []
    for entry in characters:
        if not isinstance(entry, dict):
            raise _error("each character entry must be an object")
        identifier = entry.get("id")
        if not isinstance(identifier, str) or not identifier:
            raise _error("each character needs a non-empty id")
        category = entry.get("voice_category", "unspecified")
        if category not in VOICE_CATEGORIES:
            raise _error(f"invalid character voice_category: {category!r}")
        names: list[str] = []
        for key in ("name", "display_name"):
            value = entry.get(key)
            if value is not None:
                if not isinstance(value, str):
                    raise _error(f"character {key} must be a string")
                names.append(value)
        aliases = entry.get("aliases", ())
        if isinstance(aliases, str):
            aliases = (aliases,)
        elif isinstance(aliases, (list, tuple)):
            aliases = tuple(aliases)
        else:
            raise _error("character aliases must be a string or list")
        if any(not isinstance(alias, str) for alias in aliases):
            raise _error("character aliases must be strings")
        names.extend(aliases)
        result.append(_Character(identifier, category, tuple(names)))
    return tuple(result)


def _resolve_speaker(element: ET.Element, catalog: tuple[_Character, ...]) -> None:
    has_ref = "ref" in element.attrib
    has_name = "n" in element.attrib
    if has_ref == has_name:
        raise _error("<speaker> requires exactly one of ref or n")
    query = element.attrib.get("ref") if has_ref else element.attrib.get("n")
    assert query is not None
    if has_ref:
        matches = [entry for entry in catalog if entry.identifier == query]
    else:
        matches = [entry for entry in catalog if query in entry.names]
    if not matches:
        kind = "reference" if has_ref else "name"
        raise _error(f"unresolved speaker {kind}: {query!r}")
    identifiers = {entry.identifier for entry in matches}
    if len(matches) != 1 or len(identifiers) != 1:
        kind = "reference" if has_ref else "name"
        raise _error(f"ambiguous speaker {kind}: {query!r}")
    match = matches[0]
    if has_name:
        # This mutates only the parsed tree, never the supplied character
        # dictionary.  Canonical output therefore has one stable spelling.
        del element.attrib["n"]
        element.attrib["ref"] = match.identifier


def _prepare_tree(root: ET.Element, catalog: tuple[_Character, ...]) -> None:
    count = 0

    def visit(element: ET.Element, depth: int) -> None:
        nonlocal count
        count += 1
        if count > _MAX_NODES:
            raise _error("document exceeds the 2048 node limit")
        if depth > _MAX_DEPTH:
            raise _error("document exceeds the maximum depth of 24")
        tag = _local_tag(element.tag)
        if tag not in _ELEMENT_TAGS:
            raise _error(f"unknown element <{tag}>")
        _check_attributes(element, tag)
        if tag == "segment" and element is not root:
            raise _error("<segment> is allowed only as the root")
        if tag == "speaker":
            has_ref = "ref" in element.attrib
            has_name = "n" in element.attrib
            if has_ref and has_name:
                raise _error("<speaker> accepts only one of ref or n")
            if has_ref or has_name:
                _resolve_speaker(element, catalog)
            elif not (element.attrib.get("g") or element.attrib.get("voice")):
                raise _error(
                    "<speaker> without ref or n requires a non-empty g or voice"
                )
            category = element.attrib.get("g")
            if category is not None and category not in VOICE_CATEGORIES:
                raise _error(f"invalid speaker voice category: {category!r}")
        if tag in {"speaker", "narrator", "span"}:
            voice = element.attrib.get("voice")
            if voice == "":
                raise _error(f"<{tag}> voice must not be empty")
        if (
            tag in {"dialogue", "span"}
            and "id" in element.attrib
            and not element.attrib["id"]
        ):
            raise _error(f"<{tag}> id must not be empty")
        if tag == "segment":
            if "id" not in element.attrib or not element.attrib["id"]:
                raise _error("<segment> requires a non-empty id")
            if element.attrib.get("version") not in (None, "1"):
                raise _error("segment version must be 1")
            boundary = element.attrib.get("boundary_after")
            if boundary is not None and boundary not in _BOUNDARIES:
                raise _error(f"invalid boundary_after: {boundary!r}")
        if tag == "event":
            if element.text not in (None, ""):
                raise _error("<event> cannot contain text")
            if list(element):
                raise _error("<event> cannot contain children")
            if "kind" not in element.attrib:
                raise _error("<event> requires kind")
            duration = element.attrib.get("duration_ms")
            if duration is not None:
                if not re.fullmatch(r"[0-9]+", duration):
                    raise _error("event duration_ms must be an integer")
                duration_value = int(duration)
            else:
                duration_value = None
            try:
                VocalEvent.model_validate(
                    {"kind": element.attrib["kind"], "duration_ms": duration_value}
                )
            except Exception as exc:
                raise _error(f"invalid event: {exc}") from exc
            if duration is not None:
                element.attrib["duration_ms"] = str(duration_value)
        if tag in _CONTROL_TAGS and list(element):
            raise _error(f"<{tag}> must be a leaf control")
        if tag in _LEAF_TAGS:
            # A comment/PI child is still a child even though ET exposes it as
            # a callable tag; visit it to produce the stable rejection below.
            if list(element):
                for child in element:
                    visit(child, depth + 1)
                raise _error(f"<{tag}> must be a leaf element")
            return
        for child in element:
            visit(child, depth + 1)

    if _local_tag(root.tag) != "segment":
        raise _error("root element must be <segment>")
    visit(root, 1)


def _control_delivery(parent: dict[str, Any], element: ET.Element) -> dict[str, Any]:
    controls: dict[str, str] = {}
    for child in element:
        tag = _local_tag(child.tag)
        if tag not in _CONTROL_TAGS:
            continue
        if tag in controls:
            raise _error(f"duplicate <{tag}> control in one scope")
        controls[tag] = child.text or ""
    if not controls:
        return dict(parent)
    values = dict(parent)
    for tag, value in controls.items():
        field = _CONTROL_FIELDS[tag]
        if field == "instruction":
            value = value.strip()
            if value:
                previous = str(values.get(field) or "").strip()
                value = f"{previous}\n{value}" if previous else value
        elif field == "emotion":
            value = value.strip()
        values[field] = value
    try:
        return Delivery.model_validate(values).model_dump(mode="json")
    except Exception as exc:
        raise _error(f"invalid delivery control: {exc}") from exc


def _initial_delivery() -> dict[str, Any]:
    return Delivery().model_dump(mode="json")


def _scope_for(
    element: ET.Element,
    inherited: _Scope,
    categories: dict[str, str],
) -> _Scope:
    tag = _local_tag(element.tag)
    delivery = _control_delivery(
        inherited.delivery if inherited.delivery is not None else _initial_delivery(),
        element,
    )
    speaker_id = inherited.speaker_id
    category = inherited.voice_category
    voice = inherited.voice
    dialogue = inherited.dialogue or tag == "dialogue"
    narrator = inherited.narrator
    if tag == "speaker":
        speaker_id = element.attrib.get("ref")
        category = element.attrib.get("g") or (
            categories.get(speaker_id, "unspecified")
            if speaker_id is not None
            else "unspecified"
        )
        voice = element.attrib.get("voice")
        dialogue = True
        narrator = False
    elif tag == "narrator":
        speaker_id = None
        category = "unspecified"
        voice = element.attrib.get("voice")
        dialogue = False
        narrator = True
    elif tag == "span" and "voice" in element.attrib:
        voice = element.attrib["voice"]
    return _Scope(
        speaker_id=speaker_id,
        voice_category=category,
        voice=voice,
        dialogue=dialogue,
        delivery=delivery,
        narrator=narrator,
    )


def _serialize(element: ET.Element) -> str:
    tag = _local_tag(element.tag)
    attributes: list[tuple[str, str]] = []
    for key in (
        "id",
        "version",
        "boundary_after",
        "ref",
        "g",
        "voice",
        "n",
        "kind",
        "duration_ms",
    ):
        if key in element.attrib:
            attributes.append((key, element.attrib[key]))
    # Attributes not in the schema are rejected earlier; this guard keeps this
    # serializer deterministic if the module is used directly in a debugger.
    for key in element.attrib:
        if key not in {name for name, _ in attributes}:
            attributes.append((key, element.attrib[key]))
    opening = (
        "<"
        + tag
        + "".join(f' {key}="{_escape_attribute(value)}"' for key, value in attributes)
    )
    children = list(element)
    text = element.text or ""
    if not children and not text:
        return opening + "/>"
    result = [opening, ">", _escape_text(text)]
    for child in children:
        result.append(_serialize(child))
        result.append(_escape_text(child.tail or ""))
    result.extend(("</", tag, ">"))
    return "".join(result)


def _append_text(
    value: str,
    scope: _Scope,
    transcript_parts: list[str],
    spans: list[SpeechMarkupSpan],
) -> None:
    if not value:
        return
    start = sum(len(part) for part in transcript_parts)
    transcript_parts.append(value)
    end = start + len(value)
    delivery = dict(scope.delivery or _initial_delivery())
    if (
        spans
        and spans[-1].end == start
        and (
            spans[-1].speaker_id,
            spans[-1].voice_category,
            spans[-1].voice,
            spans[-1].dialogue,
            spans[-1].delivery,
            spans[-1].narrator,
        )
        == (
            scope.speaker_id,
            scope.voice_category,
            scope.voice,
            scope.dialogue,
            delivery,
            scope.narrator,
        )
    ):
        previous = spans[-1]
        spans[-1] = SpeechMarkupSpan(
            previous.start,
            end,
            previous.text + value,
            previous.speaker_id,
            previous.voice_category,
            previous.voice,
            previous.dialogue,
            previous.delivery,
            previous.narrator,
        )
        return
    spans.append(
        SpeechMarkupSpan(
            start,
            end,
            value,
            scope.speaker_id,
            scope.voice_category,
            scope.voice,
            scope.dialogue,
            delivery,
            scope.narrator,
        )
    )


def _extract(
    root: ET.Element,
    categories: dict[str, str],
) -> tuple[str, tuple[SpeechMarkupSpan, ...], tuple[SpeechMarkupEvent, ...]]:
    transcript_parts: list[str] = []
    spans: list[SpeechMarkupSpan] = []
    events: list[SpeechMarkupEvent] = []
    default = _Scope(delivery=_initial_delivery())

    def walk(element: ET.Element, inherited: _Scope) -> None:
        tag = _local_tag(element.tag)
        if tag == "event":
            offset = sum(len(part) for part in transcript_parts)
            duration = element.attrib.get("duration_ms")
            events.append(
                SpeechMarkupEvent(
                    offset,
                    element.attrib["kind"],
                    int(duration) if duration is not None else None,
                )
            )
            return
        if tag in _CONTROL_TAGS:
            return
        scope = _scope_for(element, inherited, categories)
        _append_text(element.text or "", scope, transcript_parts, spans)
        for child in element:
            walk(child, scope)
            _append_text(child.tail or "", scope, transcript_parts, spans)

    walk(root, default)
    transcript = "".join(transcript_parts)
    return transcript, tuple(spans), tuple(events)


def _check_grapheme_boundaries(
    transcript: str,
    spans: Iterable[SpeechMarkupSpan],
    events: Iterable[SpeechMarkupEvent],
) -> None:
    boundaries = {0, *(match.end() for match in regex.finditer(r"\X", transcript))}
    for span in spans:
        if span.start not in boundaries or span.end not in boundaries:
            raise _error("a span boundary splits a Unicode grapheme")
    for event in events:
        if event.offset not in boundaries:
            raise _error("an event offset splits a Unicode grapheme")


def parse_speech_markup(
    xml: str,
    *,
    expected_segment_id: str,
    expected_text: str | None = None,
    characters: list[dict[str, Any]] | None = None,
) -> ParsedSpeechMarkup:
    """Parse, validate, and canonically serialize one annotated segment."""

    if not isinstance(expected_segment_id, str) or not expected_segment_id:
        raise ValueError("expected_segment_id must be a non-empty string")
    root = _parse_xml(xml)
    catalog = _character_catalog(characters)
    _prepare_tree(root, catalog)
    if root.attrib["id"] != expected_segment_id:
        raise _error("segment id does not match expected_segment_id")
    categories = {entry.identifier: entry.voice_category for entry in catalog}
    transcript, spans, events = _extract(root, categories)
    _check_grapheme_boundaries(transcript, spans, events)
    direction_count = sum(
        any(bool(value) for value in span.delivery.values()) for span in spans
    )
    if direction_count > 32:
        raise _error("document contains more than 32 delivery spans")
    if len(events) > 16:
        raise _error("document contains more than 16 events")
    if expected_text is not None and transcript != expected_text:
        raise _error("extracted transcript does not match expected_text")
    canonical = _serialize(root)
    return ParsedSpeechMarkup(
        segment_id=root.attrib["id"],
        transcript=transcript,
        spans=spans,
        events=events,
        xml=canonical,
        boundary_after=root.attrib.get("boundary_after"),
    )


def assert_authored_markup_preserved(
    source: ParsedSpeechMarkup, result: ParsedSpeechMarkup
) -> None:
    """Assert that an annotation pass retained authored speech metadata.

    Annotation passes may subdivide generic spans while adding attribution, so
    this compares metadata over each authored span's covered text rather than
    requiring the original span boundaries to survive unchanged.
    """

    if source.segment_id != result.segment_id:
        raise ValueError(
            "Authored speech markup segment id changed: "
            f"{source.segment_id!r} -> {result.segment_id!r}."
        )
    if source.transcript != result.transcript:
        raise ValueError("Authored speech markup transcript changed.")

    def nonempty(value: Any) -> bool:
        return value is not None and value != ""

    def protected(span: SpeechMarkupSpan) -> bool:
        return bool(
            span.speaker_id
            or span.voice
            or span.voice_category != "unspecified"
            or span.narrator
            or span.dialogue
            or any(nonempty(value) for value in span.delivery.values())
        )

    def compare(source_span: SpeechMarkupSpan, result_span: SpeechMarkupSpan) -> None:
        checks: list[tuple[str, Any, Any]] = []
        if source_span.speaker_id:
            checks.append(
                ("speaker_id", source_span.speaker_id, result_span.speaker_id)
            )
        if source_span.voice:
            checks.append(("voice", source_span.voice, result_span.voice))
        if source_span.voice_category != "unspecified":
            checks.append(
                (
                    "voice_category",
                    source_span.voice_category,
                    result_span.voice_category,
                )
            )
        if source_span.narrator:
            checks.append(("narrator", True, result_span.narrator))
        if source_span.dialogue:
            checks.append(("dialogue", True, result_span.dialogue))
        for field, value in source_span.delivery.items():
            if nonempty(value):
                checks.append(
                    (f"delivery.{field}", value, result_span.delivery.get(field))
                )
        for field, expected, actual in checks:
            if actual != expected:
                raise ValueError(
                    "Authored speech markup changed "
                    f"{field} over {source_span.start}:{source_span.end}: "
                    f"expected {expected!r}, got {actual!r}."
                )

    for source_span in source.spans:
        if not protected(source_span) or source_span.start >= source_span.end:
            continue
        cursor = source_span.start
        overlaps = [
            span
            for span in result.spans
            if span.end > source_span.start and span.start < source_span.end
        ]
        overlaps.sort(key=lambda span: (span.start, span.end))
        for result_span in overlaps:
            left = max(source_span.start, result_span.start)
            right = min(source_span.end, result_span.end)
            if right <= left:
                continue
            if left > cursor:
                raise ValueError(
                    f"Authored speech markup coverage was lost over {cursor}:{left}."
                )
            compare(source_span, result_span)
            cursor = max(cursor, right)
            if cursor >= source_span.end:
                break
        if cursor < source_span.end:
            raise ValueError(
                "Authored speech markup coverage was lost over "
                f"{cursor}:{source_span.end}."
            )

    result_index = 0
    for source_event in source.events:
        expected = (source_event.offset, source_event.kind, source_event.duration_ms)
        while result_index < len(result.events):
            result_event = result.events[result_index]
            result_index += 1
            actual = (result_event.offset, result_event.kind, result_event.duration_ms)
            if actual == expected:
                break
        else:
            raise ValueError(
                f"Authored speech markup event was removed or changed: {expected!r}."
            )
    if (
        source.boundary_after is not None
        and source.boundary_after != result.boundary_after
    ):
        raise ValueError(
            "Authored speech markup boundary_after changed: "
            f"{source.boundary_after!r} -> {result.boundary_after!r}."
        )


def _occurrence(text: str, quote: str, start: int) -> int:
    matches = [match.start() for match in re.finditer(re.escape(quote), text)]
    try:
        return matches.index(start) + 1
    except ValueError as exc:
        raise _error(
            "a delivery span cannot be represented as an exact anchor"
        ) from exc


def _anchor(text: str, quote: str, start: int) -> dict[str, Any]:
    return {"quote": quote, "occurrence": _occurrence(text, quote, start)}


def _event_annotation(event: SpeechMarkupEvent, text: str) -> dict[str, Any]:
    if event.offset == 0:
        anchor = None
        position = "before"
    elif event.offset == len(text):
        anchor = None
        position = "after"
    else:
        next_match = regex.search(r"\X", text[event.offset :])
        if next_match is None:
            anchor = None
            position = "after"
        else:
            start = event.offset + next_match.start()
            quote = next_match.group(0)
            anchor = _anchor(text, quote, start)
            position = "before"
    result: dict[str, Any] = {
        "kind": event.kind,
        "anchor": anchor,
        "position": position,
    }
    if event.duration_ms is not None:
        result["duration_ms"] = event.duration_ms
    return result


def markup_to_annotation(parsed: ParsedSpeechMarkup) -> dict[str, Any]:
    """Adapt parsed delivery/events to the existing performance annotation."""

    if not isinstance(parsed, ParsedSpeechMarkup):
        raise TypeError("parsed must be ParsedSpeechMarkup")
    directed = [
        span
        for span in parsed.spans
        if any(bool(value) for value in span.delivery.values())
    ]
    distinct = {
        tuple(Delivery.model_validate(span.delivery).model_dump(mode="json").items())
        for span in directed
    }
    annotation: dict[str, Any] = {
        "schema": _SCHEMA,
        "decision": "steer" if directed or parsed.events else "none",
    }
    if directed and len(distinct) == 1:
        annotation["delivery"] = dict(
            Delivery.model_validate(directed[0].delivery).model_dump(mode="json")
        )
    elif directed:
        annotation["delivery"] = Delivery().model_dump(mode="json")
        annotation["spans"] = [
            {
                "anchor": _anchor(parsed.transcript, span.text, span.start),
                "delivery": Delivery.model_validate(span.delivery).model_dump(
                    mode="json"
                ),
            }
            for span in directed
        ]
    if parsed.events:
        annotation["events"] = [
            _event_annotation(event, parsed.transcript) for event in parsed.events
        ]
    # Validate the adapter output against the authoritative schema.  Returning
    # its JSON dump also fills only the schema's provider-independent defaults.
    return validate_annotation(parsed.transcript, annotation)


__all__ = [
    "VOICE_CATEGORIES",
    "ParsedSpeechMarkup",
    "SpeechMarkupEvent",
    "SpeechMarkupSpan",
    "assert_authored_markup_preserved",
    "markup_to_annotation",
    "parse_speech_markup",
    "plain_speech_markup",
]
