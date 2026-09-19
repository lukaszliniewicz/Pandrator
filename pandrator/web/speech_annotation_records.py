"""Normalize legacy pSSML and structured speech-markup annotation records."""

# Input validation deliberately raises ValueError: API callers translate it to 422.
# ruff: noqa: TRY004

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from pandrator.logic.speech_markup import markup_to_annotation, parse_speech_markup
from pandrator.logic.speech_performance import validate_annotation

SPEECH_XML_KEY = "_speech_xml"


def _values(item: Any) -> dict[str, Any]:
    if hasattr(item, "model_dump"):
        return dict(item.model_dump(mode="json", by_alias=True, exclude_none=True))
    if isinstance(item, Mapping):
        return dict(item)
    raise TypeError("Annotation items must be mappings or validated models.")


def _xml_record(
    text: str,
    segment_id: str,
    xml: str,
    characters: list[dict[str, Any]] | None,
    *,
    locked: bool,
    reason: str,
) -> dict[str, Any]:
    parsed = parse_speech_markup(
        xml,
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    )
    record = markup_to_annotation(parsed)
    record[SPEECH_XML_KEY] = parsed.xml
    record["locked"] = locked
    if reason:
        record["reason"] = reason
    return record


def normalized_record(
    text: str,
    segment_id: str,
    item: Any,
    characters: list[dict[str, Any]] | None,
    automatic: bool = False,
) -> dict[str, Any]:
    """Return a validated stored record for exactly one supplied format."""

    values = _values(item)
    annotation = values.get("annotation")
    speech_xml = values.get("speech_xml")
    if (annotation is None) == (speech_xml is None):
        raise ValueError("Supply exactly one of annotation or speech_xml.")
    if annotation is not None:
        if values.get("locked") or values.get("reason"):
            raise ValueError("locked and reason apply only to speech_xml items.")
        record = validate_annotation(text, annotation)
        if automatic and record.get("locked"):
            raise ValueError("Automatic workers cannot lock annotations.")
        return record
    if not isinstance(speech_xml, str):
        raise ValueError("speech_xml must be a string.")
    locked = values.get("locked", False)
    reason = values.get("reason", "")
    if not isinstance(locked, bool):
        raise ValueError("speech_xml locked must be a boolean.")
    if not isinstance(reason, str):
        raise ValueError("speech_xml reason must be a string.")
    if automatic and locked:
        raise ValueError("Automatic workers cannot lock speech markup.")
    return _xml_record(
        text,
        segment_id,
        speech_xml,
        characters,
        locked=locked,
        reason=reason,
    )


def record_annotation(record: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """Return pure pSSML annotation data for public/provider-facing use."""

    if record is None:
        return None
    result = deepcopy(dict(record))
    result.pop(SPEECH_XML_KEY, None)
    return result


def record_markup(record: Mapping[str, Any] | None) -> str | None:
    if record is None:
        return None
    value = record.get(SPEECH_XML_KEY)
    return value if isinstance(value, str) else None


def markup_structure(
    record: Mapping[str, Any] | None,
    text: str,
    segment_id: str,
    characters: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    xml = record_markup(record)
    if xml is None:
        return None
    return parse_speech_markup(
        xml,
        expected_segment_id=segment_id,
        expected_text=text,
        characters=characters,
    ).public()


__all__ = [
    "SPEECH_XML_KEY",
    "markup_structure",
    "normalized_record",
    "record_annotation",
    "record_markup",
]
