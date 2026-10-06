"""Lossless dialogue span transport for passive speech annotation."""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from typing import Any

BOUNDARIES = frozenset({"continuation", "dialogue_turn", "paragraph", "scene", "chapter"})


def source_text_sha256(text: str) -> str:
    """Hash the exact UTF-8 source, without Unicode normalization."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dialogue_spans_to_xml(
    *,
    unit_id: int,
    source_text: str,
    source_sha256: object,
    annotations: object,
    boundary_after: object = None,
    characters: list[dict[str, Any]] | None = None,
) -> str:
    """Validate codepoint ranges and reconstruct markup from exact source slices."""
    if (
        not isinstance(source_sha256, str)
        or re.fullmatch(r"[a-f0-9]{64}", source_sha256) is None
        or source_sha256 != source_text_sha256(source_text)
    ):
        raise ValueError("source_sha256 must match the exact UTF-8 source text")
    if not isinstance(annotations, list) or len(annotations) > 500:
        raise ValueError("annotations must be a list of at most 500 dialogue spans")
    if boundary_after is not None and (
        not isinstance(boundary_after, str) or boundary_after not in BOUNDARIES
    ):
        raise ValueError("boundary_after must be a supported speech boundary")
    known_ids = {item["id"] for item in characters or [] if isinstance(item.get("id"), str)}
    root = ET.Element("segment", {"id": str(unit_id)})
    if boundary_after is not None:
        root.set("boundary_after", boundary_after)
    offset = 0
    for annotation in annotations:
        if not isinstance(annotation, dict) or set(annotation) - {"start", "end", "speaker_ref"}:
            raise ValueError("each annotation must contain only start, end, and speaker_ref")
        start, end = annotation.get("start"), annotation.get("end")
        if type(start) is not int or type(end) is not int:
            raise ValueError("annotation offsets must be strict integers in Unicode codepoints")
        if start < offset or end <= start or start < 0 or end > len(source_text):
            raise ValueError("annotations must be sorted, nonoverlapping, and within source text")
        speaker_ref = annotation.get("speaker_ref")
        if speaker_ref is not None and (
            not isinstance(speaker_ref, str) or speaker_ref not in known_ids
        ):
            raise ValueError(
                "speaker_ref must be a known character ID or null for unknown identity"
            )
        if start > offset:
            ET.SubElement(root, "narrator").text = source_text[offset:start]
        dialogue = ET.SubElement(root, "dialogue")
        spoken = (
            ET.SubElement(dialogue, "speaker", {"ref": speaker_ref})
            if speaker_ref is not None
            else dialogue
        )
        spoken.text = source_text[start:end]
        offset = end
    if offset < len(source_text):
        ET.SubElement(root, "narrator").text = source_text[offset:]
    # ElementTree escapes XML-sensitive prose; encode CR explicitly to prevent
    # XML newline normalization from changing the exact transcript on parse.
    return ET.tostring(root, encoding="unicode").replace("\r", "&#13;")
