"""Exact-text audiobook topology proposals; no synthesis or source rewriting."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections import Counter
from copy import deepcopy

from sqlalchemy import select

from pandrator.logic.speech_markup import parse_speech_markup, plain_speech_markup
from pandrator.logic.speech_markup_edits import join_speech_markup, slice_speech_markup

from . import models as m
from .generation_cast_runtime import remap_markup
from .generation_controls import get_generation_controls


def copy_unchanged_annotations(
    session, previous, revision_id: str, units: list[dict], characters: list[dict]
) -> dict:
    """Follow immutable lineage, retaining only exact one-to-one annotation targets."""
    from .performance_plans import _annotations
    from .speech_annotation_records import normalized_record, record_markup

    chain, visited = [], set()
    cursor = revision_id
    while cursor != previous.plan_revision_id:
        if cursor in visited or len(chain) >= 100:
            raise ValueError(
                "The performance source is not an ancestor of this speech plan."
            )
        visited.add(cursor)
        revision = session.get(m.GenerationPlanRevision, cursor)
        if revision is None:
            raise ValueError(
                "The performance source is not an ancestor of this speech plan."
            )
        operation = revision.operation_json or {}
        chain.append(operation.get("lineage") or {})
        cursor = (
            operation.get("target_revision_id")
            if operation.get("action") == "restore"
            else revision.parent_revision_id
        )
    mapping = {unit["id"]: [unit["id"]] for unit in previous.units_json}
    for lineage in reversed(chain):
        mapping = {
            old: list(
                dict.fromkeys(
                    child for parent in ids for child in lineage.get(parent, [])
                )
            )
            for old, ids in mapping.items()
        }
    source_units = {unit["id"]: unit for unit in previous.units_json}
    targets = {unit["id"]: unit for unit in units}
    annotations = _annotations(session, previous)
    segment_rows = {
        row.id: row
        for row in session.scalars(
            select(m.GenerationSegment).where(
                m.GenerationSegment.plan_revision_id.in_(
                    [previous.plan_revision_id, revision_id]
                ),
            )
        )
    }

    def structure_key(row):
        xml = (row.speech_plan_json or {}).get("speech_xml")
        return (
            row.voice_id,
            row.node_kind,
            row.silence_after_ms,
            row.paragraph_break_after,
            remap_markup(xml, "comparison", row.optimized_text or row.text, characters)
            if xml
            else None,
        )

    target_counts = Counter(child for ids in mapping.values() for child in ids)
    seed = {}
    for old, ids in mapping.items():
        if len(ids) != 1 or target_counts[ids[0]] != 1:
            continue
        source, target = source_units[old], targets.get(ids[0])
        if target is None or any(
            source.get(key) != target.get(key)
            for key in ("spoken_text", "speaker", "voice", "language")
        ):
            continue
        if structure_key(segment_rows[old]) != structure_key(segment_rows[ids[0]]):
            continue
        record = deepcopy(annotations.get(old))
        if record is None:
            continue
        xml = record_markup(record)
        if xml:
            projected = remap_markup(
                xml, target["id"], target["spoken_text"], characters
            )
            record = normalized_record(
                target["spoken_text"],
                target["id"],
                {"speech_xml": projected, "locked": record.get("locked", False)},
                characters,
            )
        seed[target["id"]] = record
    return seed


def _reviewed_annotations(session, revision_id: str) -> dict:
    """Read and validate one adopted plan once per topology operation."""
    from .performance_plans import _annotations
    from .speech_plan_workspace import plan_signature

    adopted = session.scalar(
        select(m.PerformancePlan).where(
            m.PerformancePlan.plan_revision_id == revision_id,
            m.PerformancePlan.status == "adopted",
        )
    )
    if adopted is not None and adopted.base_signature == plan_signature(
        session, revision_id
    ):
        return _annotations(session, adopted)
    return {}


def effective_markup(
    session, segment, characters: list[dict], annotations: dict | None = None
) -> str:
    """Use reviewed casting if present, otherwise the accepted source structure."""
    from .speech_annotation_records import record_markup

    if annotations is None:
        annotations = _reviewed_annotations(session, segment.plan_revision_id)
    text = segment.optimized_text or segment.text
    xml = record_markup(annotations.get(segment.id, {}))
    xml = xml or (segment.speech_plan_json or {}).get("speech_xml")
    if xml:
        return remap_markup(xml, segment.id, text, characters)
    plain = plain_speech_markup(segment.id, text)
    if segment.speaker:
        root = ET.fromstring(plain)
        root.text = None
        ET.SubElement(root, "dialogue").text = text
        plain = ET.tostring(root, encoding="unicode")
    return plain


def project_split_markup(
    session, segment, values: list[dict], speech_cursor: int, session_id: str
) -> None:
    characters = get_generation_controls(session, session_id)["characters"]
    text = segment.optimized_text or segment.text
    xml = effective_markup(session, segment, characters)
    parsed = parse_speech_markup(
        xml, expected_segment_id=segment.id, expected_text=text, characters=characters
    )
    for index, (left, right) in enumerate(
        ((0, speech_cursor), (speech_cursor, len(text)))
    ):
        while left < right and text[left].isspace():
            left += 1
        while right > left and text[right - 1].isspace():
            right -= 1
        values[index]["speech_plan_json"]["speech_xml"] = slice_speech_markup(
            xml,
            source_id=segment.id,
            source_text=text,
            segment_id=segment.id,
            start=left,
            end=right,
            characters=characters,
            boundary_after="continuation" if index == 0 else parsed.boundary_after,
            include_end_events=index == 1,
        )
    projected_events = sum(
        len(
            parse_speech_markup(
                value["speech_plan_json"]["speech_xml"],
                expected_segment_id=segment.id,
                expected_text=value["optimized_text"] or value["text"],
                characters=characters,
            ).events
        )
        for value in values
    )
    if projected_events != len(parsed.events):
        raise ValueError(
            "The split would remove a vocal event beside trimmed whitespace. Move the boundary or edit the event first."
        )


def project_merge_markup(session, left, right, values: dict, session_id: str) -> None:
    characters = get_generation_controls(session, session_id)["characters"]
    annotations = _reviewed_annotations(session, left.plan_revision_id)
    values["speech_plan_json"]["speech_xml"] = join_speech_markup(
        [
            (
                row.id,
                row.optimized_text or row.text,
                effective_markup(session, row, characters, annotations),
            )
            for row in (left, right)
        ],
        segment_id=left.id,
        characters=characters,
    )


def _automatic_boundaries(
    text: str, max_chars: int, paragraph_ends: set[int]
) -> list[int]:
    """Prefer sentence ends, then whitespace; retain every source character."""
    sentences = [
        match.end()
        for match in re.finditer(r"[.!?…。！？][\"\u201d\u2019\)\]]*(?:\s+|$)", text)
    ]
    whitespace = [match.end() for match in re.finditer(r"\s+", text)]
    result, start = [], 0
    while start < len(text):
        paragraph_end = min(
            (value for value in paragraph_ends if value > start), default=len(text)
        )
        ceiling = min(start + max_chars, paragraph_end)
        if paragraph_end <= ceiling:
            end = paragraph_end
        else:
            candidates = [value for value in sentences if start < value <= ceiling]
            candidates = candidates or [
                value for value in whitespace if start < value <= ceiling
            ]
            if not candidates:
                raise ValueError(
                    "A word exceeds max_chars; choose explicit character boundaries or a larger limit."
                )
            end = candidates[-1]
        if end < len(text):
            result.append(end)
        start = end
    return result


def prepare_resegmentation(
    service, session, session_id: str, current, rows: list, operation: dict
):
    record = session.get(m.SessionRecord, session_id)
    if record is None or record.workflow_kind != "audiobook":
        raise ValueError("Range resegmentation is available for audiobook sessions.")
    ids = operation.get("segment_ids") or []
    if not 1 <= len(ids) <= 100 or len(set(ids)) != len(ids):
        raise ValueError("Select 1–100 unique contiguous segments in reading order.")
    positions = {row.id: index for index, row in enumerate(rows)}
    if any(identifier not in positions for identifier in ids):
        raise ValueError("Every selected segment must belong to the active plan.")
    first = positions[ids[0]]
    selected = rows[first : first + len(ids)]
    if [row.id for row in selected] != ids:
        raise ValueError("Select contiguous segments in reading order.")
    if any(row.node_kind in {"chapter_marker", "heading"} for row in selected):
        raise ValueError("Resegment prose within a chapter; keep headings separate.")
    for field in ("voice", "voice_id", "language", "speaker"):
        if len({getattr(row, field) for row in selected}) > 1:
            raise ValueError(
                f"Range resegmentation cannot combine different {field} overrides."
            )
    if any(row.optimized_text and row.optimized_text != row.text for row in selected):
        raise ValueError(
            "Range resegmentation requires matching display and speech text; use mapped split/merge for optimized variants."
        )
    text = " ".join(row.text for row in selected)
    if len(text) > 24000:
        raise ValueError("Select at most 24,000 characters per resegmentation draft.")
    source_ranges, source_ends, paragraph_ends = [], {}, set()
    cursor = 0
    for row in selected:
        end = cursor + len(row.text)
        source_ranges.append((row, cursor, end))
        source_ends[end] = row
        if row.paragraph_break_after:
            paragraph_ends.add(end)
        cursor = end + 1
    boundaries = operation.get("boundaries")
    if boundaries is None:
        max_chars = operation.get("max_chars") or 300
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 40 <= max_chars <= 4000
        ):
            raise ValueError("max_chars must be an integer between 40 and 4000.")
        boundaries = _automatic_boundaries(text, max_chars, paragraph_ends)
    if (
        len(boundaries) > 199
        or any(
            type(value) is not int or not 0 < value < len(text) for value in boundaries
        )
        or boundaries != sorted(set(boundaries))
    ):
        raise ValueError(
            "Supply at most 199 strictly increasing character boundaries inside the range."
        )
    characters = get_generation_controls(session, session_id)["characters"]
    annotations = _reviewed_annotations(session, current.id)
    source_markup = {
        row.id: effective_markup(session, row, characters, annotations)
        for row in selected
    }
    xml = join_speech_markup(
        [(row.id, row.text, source_markup[row.id]) for row in selected],
        segment_id=selected[0].id,
        characters=characters,
    )
    source_boundaries = {
        row.id: parse_speech_markup(
            source_markup[row.id],
            expected_segment_id=row.id,
            expected_text=row.text,
            characters=characters,
        ).boundary_after
        for row in selected
    }
    replacements, preview = [], []
    cuts = [0, *boundaries, len(text)]
    for index, (start, end) in enumerate(zip(cuts, cuts[1:])):
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start == end:
            raise ValueError("Resegmentation cannot create an empty block.")
        source = next(row for row, a, b in source_ranges if a <= start < b)
        terminal_source = source_ends.get(end)
        paragraph = bool(terminal_source and terminal_source.paragraph_break_after)
        boundary = (
            "paragraph"
            if paragraph
            else (
                None
                if re.search(r"[.!?…。！？][\"\u201d\u2019\)\]]*$", text[start:end])
                else "continuation"
            )
        )
        if terminal_source and source_boundaries[terminal_source.id] is not None:
            boundary = source_boundaries[terminal_source.id]
        values = service._segment_copy_values(source)
        values.update(
            text=text[start:end],
            optimized_text=None,
            status="stale",
            optimization_status="stale",
            optimization_source_hash=None,
            optimization_reviewed=False,
            optimization_model=None,
            paragraph_break_after=paragraph,
            silence_after_ms=terminal_source.silence_after_ms
            if terminal_source
            else (0 if boundary == "continuation" else 250),
            source_segment_ids_json=list(
                dict.fromkeys(
                    ref
                    for row, a, b in source_ranges
                    if a < end and b > start
                    for ref in (row.source_segment_ids_json or [])
                )
            ),
            speech_block_provenance_json={
                "resegmentation": {
                    "source_revision_id": current.id,
                    "range_start": start,
                    "range_end": end,
                    "source_spans": [
                        {
                            "segment_id": row.id,
                            "start": max(start, a) - a,
                            "end": min(end, b) - a,
                        }
                        for row, a, b in source_ranges
                        if a < end and b > start
                    ],
                }
            },
            speech_plan_json={
                "speech_xml": slice_speech_markup(
                    xml,
                    source_id=selected[0].id,
                    source_text=text,
                    segment_id=source.id,
                    start=start,
                    end=end,
                    characters=characters,
                    boundary_after=boundary,
                    include_end_events=index == len(cuts) - 2,
                )
            },
        )
        replacements.append(values)
        preview.append(
            {
                "text": values["text"],
                "start": start,
                "end": end,
                "silence_after_ms": values["silence_after_ms"],
                "boundary_after": boundary,
            }
        )
    original_events = parse_speech_markup(
        xml,
        expected_segment_id=selected[0].id,
        expected_text=text,
        characters=characters,
    ).events
    projected_events = sum(
        len(
            parse_speech_markup(
                value["speech_plan_json"]["speech_xml"],
                expected_segment_id=next(
                    row.id for row, a, b in source_ranges if a <= item["start"] < b
                ),
                expected_text=value["text"],
                characters=characters,
            ).events
        )
        for value, item in zip(replacements, preview, strict=True)
    )
    if projected_events != len(original_events):
        raise ValueError(
            "These boundaries would remove a vocal event beside trimmed whitespace. Move the boundary or edit the event first."
        )
    mapping = {row.id: [] for row in selected}
    mapping[selected[0].id] = replacements
    return mapping, [
        {
            "source_segment_ids": ids,
            "source_text": text,
            "before": [
                {
                    "id": row.id,
                    "text": row.text,
                    "silence_after_ms": row.silence_after_ms,
                }
                for row in selected
            ],
            "after": preview,
        }
    ]
