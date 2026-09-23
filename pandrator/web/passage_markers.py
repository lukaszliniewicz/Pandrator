"""Read-only, text-verified timed-passage markers and guarded split anchors.

Source passage timing is not generated-word timing. No marker is inferred from
source ID lists, proportional offsets, or an obsolete pre-merge word ledger.
"""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from pandrator.logic.dubbing.natural_boundaries import classify_boundary


@dataclass(frozen=True)
class _Passage:
    reference: str | int
    start_ms: int
    end_ms: int
    begin: int
    end: int


def _empty(status: str, message: str) -> dict[str, Any]:
    return {"status": status, "message": message, "boundaries": []}


def _map_layer(text: str, cues: list[Any], layer: str) -> tuple[list[_Passage], dict[str, Any]]:
    """Validate every stored code-point span, including full text coverage.

    Clipped ranges keep the full original cue text in legacy provenance. They
    must not be promoted back to a precisely timed whole passage here.
    """
    mapped: list[_Passage] = []
    seen: set[str | int] = set()
    consumed = 0
    for cue in cues:
        if not isinstance(cue, Mapping):
            return [], _empty("unavailable", "The passage provenance is incomplete.")
        reference = cue.get("reference")
        if (isinstance(reference, bool) or not isinstance(reference, (str, int))
                or reference in seen or not str(reference).strip()):
            return [], _empty("unavailable", "The passage references are ambiguous.")
        ranges = cue.get(f"{layer}_spans")
        evidence = cue.get(f"{layer}_text")
        if not isinstance(ranges, list) or len(ranges) != 1 or not isinstance(evidence, str):
            return [], _empty("unavailable", "Complete passage-to-text mapping is unavailable; no positions are guessed.")
        span = ranges[0]
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(value) is not int for value in span)):
            return [], _empty("unavailable", "The passage text ranges are invalid.")
        begin, end = span
        if not (consumed <= begin < end <= len(text)) or text[consumed:begin].strip():
            return [], _empty("stale", "Passage mapping no longer matches this text. Review the text before splitting.")
        if text[begin:end].strip() != evidence.strip():
            return [], _empty("stale", "Passage mapping no longer matches this text. Review the text before splitting.")
        start_ms, end_ms = cue.get("start_ms"), cue.get("end_ms")
        if (type(start_ms) is not int or type(end_ms) is not int
                or not 0 <= start_ms < end_ms):
            return [], _empty("unavailable", "This passage has no reliable source timing window.")
        if mapped and start_ms < mapped[-1].start_ms:
            return [], _empty("unavailable", "Reordered passage timing cannot be shown as precise split anchors.")
        seen.add(reference)
        mapped.append(_Passage(reference, start_ms, end_ms, begin, end))
        consumed = end
    if text[consumed:].strip():
        return [], _empty("stale", "Passage mapping no longer covers the current text. No positions are guessed.")
    return mapped, {"status": "mapped", "message": None, "boundaries": []}


def describe_passages(segment: Any, *, language: str | None = None) -> dict[str, Any]:
    """Describe visible markers without mutation, database queries, or model calls."""
    provenance = segment.speech_block_provenance_json or {}
    if not isinstance(provenance, Mapping):
        provenance = {}
    cues = provenance.get("source_cues")
    if not isinstance(cues, list) or not cues:
        empty = _empty("unavailable", "No timed passage provenance is available for this block.")
        return {"schema_version": 1, "layers": {"display": dict(empty), "speech": dict(empty)}}
    texts = {"display": segment.text or "", "speech": segment.optimized_text or segment.text or ""}
    layers: dict[str, Any] = {}
    maps: dict[str, list[_Passage]] = {}
    for layer in texts:
        maps[layer], layers[layer] = _map_layer(texts[layer], cues, layer)
    for layer, text in texts.items():
        layers[layer]["text"] = text  # Client can reject locally stale display metadata.
    # Without a separate spoken override, the spoken text is exactly the
    # display text. Legacy imports may have omitted the redundant speech spans.
    if texts["speech"] == texts["display"] and layers["display"]["status"] == "mapped":
        maps["speech"] = maps["display"]
        layers["speech"] = {"status": "mapped", "message": None, "boundaries": [], "text": texts["speech"]}
    fingerprint = hashlib.sha256(json.dumps({
        "id": segment.id, "revision": segment.revision, "texts": texts,
        "provenance": provenance,
    }, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()
    lang = segment.language or language or ""
    estimated = "estimated_internal_timing" in (provenance.get("risk_flags") or [])
    shared = "shared_passage_timing" in (provenance.get("risk_flags") or [])
    for layer, passages in maps.items():
        if layers[layer]["status"] != "mapped":
            continue
        companion = "speech" if layer == "display" else "display"
        other = maps[companion]
        correspondence = (layers[companion]["status"] == "mapped"
                          and [p.reference for p in passages] == [p.reference for p in other])
        for index, (left, right) in enumerate(
            zip(passages, passages[1:], strict=False)
        ):
            offset = left.end
            offsets = {layer: offset, companion: other[index].end if correspondence else None}
            kind = classify_boundary(texts[layer][:offset], texts[layer][offset:], language_code=lang)
            other_kind = (classify_boundary(texts[companion][:other[index].end],
                                           texts[companion][other[index].end:], language_code=lang)
                          if correspondence else None)
            natural = kind is not None and (not correspondence or other_kind is not None)
            left_window = [min(p.start_ms for p in passages[:index + 1]),
                           max(p.end_ms for p in passages[:index + 1])]
            right_window = [min(p.start_ms for p in passages[index + 1:]),
                            max(p.end_ms for p in passages[index + 1:])]
            gap = right.start_ms - left.end_ms
            blocked = None
            if not correspondence:
                blocked = "The other text layer cannot be split at a verified matching passage boundary."
            elif left_window[1] > right_window[0]:
                blocked = "Source windows overlap at this boundary; there is no independent timed split."
            elif estimated or shared:
                blocked = "These chunks share a source timing window; independent internal timestamps are not available."
            elif segment.removed:
                blocked = "Restore this removed block before splitting it."
            warning = None
            if not natural:
                warning = "This timed boundary is inside an unfinished phrase. Splitting here may produce unnatural speech."
            if gap > 0 and not natural:
                warning += " The source pause is a hesitation, not proof of a natural speech break."
            if gap < 0:
                warning = "The source passages overlap; the dot is informational, not a safe timed split."
            boundary_id = "pb-" + hashlib.sha256(f"{fingerprint}:{layer}:{index}:{offset}".encode()).hexdigest()[:24]
            layers[layer]["boundaries"].append({
                "id": boundary_id, "offset": offset,
                "display_offset": offsets.get("display"), "speech_offset": offsets.get("speech"),
                "left_reference": left.reference, "right_reference": right.reference,
                "left_end_ms": left.end_ms, "right_start_ms": right.start_ms, "gap_ms": gap,
                "left_window": left_window, "right_window": right_window,
                "natural": natural, "boundary_kind": ("sentence", "clause", "comma", "conjunction")[kind] if kind is not None else None,
                "warning": warning, "split_allowed": blocked is None, "split_blocked_reason": blocked,
            })
    return {"schema_version": 1, "layers": layers}


def validate_passage_split(segment: Any, boundary_id: str, layer: str, cursor: int) -> tuple[int, int]:
    """Recheck the exact marker at mutation time, before making new revisions."""
    description = describe_passages(segment)
    selected = next((value for value in description["layers"][layer]["boundaries"]
                     if value["id"] == boundary_id and value["offset"] == cursor), None)
    if selected is None:
        raise ValueError("This passage marker no longer matches the current text. Close the preview and refresh.")
    if not selected["split_allowed"]:
        raise ValueError(selected["split_blocked_reason"])
    return selected["display_offset"], selected["speech_offset"]
