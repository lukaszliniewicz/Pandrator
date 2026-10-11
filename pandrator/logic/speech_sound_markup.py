"""Bounded sound-marker projection around canonical speech XML.

Sound markers are authoring metadata.  The returned speech XML is the only
document that may be passed to the speech compiler.
"""

from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import regex

from .speech_markup import _parse_xml, _serialize

MAX_SOUND_MARKERS = 32
_MAX_NODES = 2048
_MAX_DEPTH = 24
_ATTRIBUTES = {"cue", "variant", "placement"}
_PLACEMENTS = {"before", "after", "next_pause"}
_CONTROL = {"ins", "em", "pace", "cadence", "emphasis", "event"}


def _lexical_tokens(text: str) -> list[tuple[str, int, int]]:
    """Use Unicode word boundaries while excluding punctuation-only spans."""
    boundaries = [match.start() for match in regex.finditer(
        r"\b", text, flags=regex.WORD | regex.VERSION1
    )]
    return [
        (text[start:end], start, end)
        for start, end in zip(boundaries, boundaries[1:], strict=False)
        if regex.search(r"\w", text[start:end], flags=regex.VERSION1)
    ]


@dataclass(frozen=True, slots=True)
class SoundMarker:
    cue: str
    variant: str
    placement: str
    offset: int


def _uuid(value: str) -> bool:
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError):
        return False


def _spoken_length(element: ET.Element) -> int:
    if element.tag in _CONTROL or element.tag == "sound":
        return 0
    return len(element.text or "") + sum(
        _spoken_length(child) + len(child.tail or "") for child in element
    )


def split_sound_markup(xml: str) -> tuple[str, tuple[SoundMarker, ...]]:
    """Validate markers, remove them with their tails, and return speech XML."""
    if re.search(r"</\s*sound\s*>", xml):
        raise ValueError("Sound markers must use empty-element syntax.")
    root = _parse_xml(xml)
    markers: list[SoundMarker] = []
    seen: set[str] = set()
    nodes = 0

    def limits(element: ET.Element, depth: int) -> None:
        nonlocal nodes
        nodes += 1
        if nodes > _MAX_NODES or depth > _MAX_DEPTH:
            raise ValueError("Sound markup exceeds the speech XML node or depth limit.")
        for child in element:
            limits(child, depth + 1)

    limits(root, 1)

    def walk(element: ET.Element, offset: int, depth: int) -> int:
        if not isinstance(element.tag, str) or element.tag.startswith("{") or ":" in element.tag:
            raise ValueError("Invalid sound markup element.")
        if element.tag in _CONTROL:
            if any(child.tag == "sound" for child in element.iter()):
                raise ValueError("Sound markers cannot be nested inside controls or events.")
            return offset
        offset += len(element.text or "")
        index = 0
        while index < len(element):
            child = element[index]
            if child.tag == "sound":
                if set(child.attrib) != _ATTRIBUTES or len(child) or child.text:
                    raise ValueError("A sound marker requires only cue, variant and placement attributes.")
                cue, variant, placement = (child.attrib[key] for key in ("cue", "variant", "placement"))
                if not _uuid(cue) or not _uuid(variant) or placement not in _PLACEMENTS:
                    raise ValueError("Sound marker has an invalid UUID or placement.")
                if cue in seen:
                    raise ValueError("Duplicate sound cue ID in speech XML.")
                if len(markers) >= MAX_SOUND_MARKERS:
                    raise ValueError("A segment supports at most 32 sound markers.")
                if placement == "before" and element is not root:
                    raise ValueError("A before sound marker must be at the segment root.")
                seen.add(cue)
                markers.append(SoundMarker(cue, variant, placement, offset))
                tail = child.tail or ""
                element.remove(child)
                if index:
                    prior = element[index - 1]
                    prior.tail = (prior.tail or "") + tail
                else:
                    element.text = (element.text or "") + tail
                offset += len(tail)
                continue
            offset = walk(child, offset, depth + 1)
            offset += len(child.tail or "")
            index += 1
        return offset

    if root.tag != "segment":
        raise ValueError("Sound markup requires a segment root.")
    transcript_length = walk(root, 0, 1)
    def spoken(element: ET.Element) -> str:
        if element.tag in _CONTROL:
            return ""
        return (element.text or "") + "".join(
            spoken(child) + (child.tail or "") for child in element
        )

    text = spoken(root)
    boundaries = {0, *(match.end() for match in regex.finditer(r"\X", text))}
    for marker in markers:
        if marker.placement == "before" and marker.offset != 0:
            raise ValueError("A before sound marker must precede the segment text.")
        if marker.placement == "after" and marker.offset == 0:
            raise ValueError("An after sound marker needs preceding speech.")
        if marker.offset > transcript_length:
            raise ValueError("Sound marker is outside the segment text.")
        if marker.offset not in boundaries:
            raise ValueError("Sound marker splits a Unicode grapheme.")
        if any(start < marker.offset < end for _, start, end in _lexical_tokens(text)):
            raise ValueError("Sound marker splits a lexical word.")
    return _serialize(root), tuple(markers)


def _text_slots(root: ET.Element) -> list[tuple[ET.Element, str, int, int]]:
    slots: list[tuple[ET.Element, str, int, int]] = []

    def walk(element: ET.Element, offset: int) -> int:
        if element.tag in _CONTROL:
            return offset
        value = element.text or ""
        slots.append((element, "text", offset, offset + len(value)))
        offset += len(value)
        for child in element:
            offset = walk(child, offset)
            value = child.tail or ""
            slots.append((child, "tail", offset, offset + len(value)))
            offset += len(value)
        return offset

    walk(root, 0)
    return slots


def insert_sound_markers(speech_xml: str, markers: list[SoundMarker]) -> str:
    """Project representable cues without moving speech controls or text."""
    root = _parse_xml(speech_xml)
    if len(markers) > MAX_SOUND_MARKERS:
        raise ValueError("A segment supports at most 32 sound markers.")
    # Reverse insertion keeps offsets tied to the original speech document.
    for marker in sorted(markers, key=lambda item: (item.offset, item.cue), reverse=True):
        element = ET.Element("sound", {
            "cue": marker.cue, "variant": marker.variant, "placement": marker.placement,
        })
        if marker.placement == "before" and marker.offset == 0:
            element.tail = root.text
            root.text = None
            root.insert(0, element)
            continue
        if marker.placement == "after" and marker.offset == _spoken_length(root):
            root.append(element)
            continue
        slots = _text_slots(root)
        slot = next(
            ((owner, field, start, end) for owner, field, start, end in slots
             if start < marker.offset <= end),
            None,
        )
        if slot is None:
            raise ValueError("Sound marker cannot be placed at this speech position.")
        owner, field, start, _end = slot
        value = getattr(owner, field) or ""
        split = marker.offset - start
        setattr(owner, field, value[:split])
        element.tail = value[split:]
        if field == "text":
            owner.insert(0, element)
        else:
            parent = next((node for node in root.iter() if owner in node), None)
            if parent is None:
                raise ValueError("Sound marker has no speech parent.")
            parent.insert(list(parent).index(owner) + 1, element)
    result = _serialize(root)
    # This also enforces the XML byte, node and depth bounds after projection.
    split_sound_markup(result)
    return result


def preceding_lexical_anchor(text: str, offset: int) -> dict[str, Any]:
    """Anchor a marker to a complete Unicode lexical token ending at offset."""
    boundaries = {0, *(match.end() for match in regex.finditer(r"\X", text))}
    if offset not in boundaries:
        raise ValueError("Sound marker splits a Unicode grapheme.")
    tokens = _lexical_tokens(text)
    if any(start < offset < end for _, start, end in tokens):
        raise ValueError("Sound marker splits a lexical word.")
    token = next((item for item in reversed(tokens) if item[2] == offset), None)
    if token is None:
        raise ValueError("Sound marker must follow a complete lexical token.")
    quote, start, end = token
    occurrence = len(list(re.finditer(re.escape(quote), text[:start]))) + 1
    return {"quote": quote, "occurrence": occurrence, "start": start, "end": end}
