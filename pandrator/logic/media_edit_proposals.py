"""Pure policy preparation for media-edit proposal ranges and evidence."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from .media_edit import (
    BoundaryEvidence,
    KeepRange,
    MediaCue,
    MediaWord,
    keep_ranges_from_cuts,
    normalize_keep_ranges,
    refine_boundary,
)


@dataclass(frozen=True, slots=True)
class ProposedMediaEdit:
    """A shallowly frozen proposal container with mutable payload contents."""

    instructions: str
    keep_ranges: list[dict[str, Any]]
    evidence: dict[str, Any]
    operation: dict[str, Any]


def keep_range_payload(item: Any) -> dict[str, Any]:
    return {
        "id": str(getattr(item, "id", "")),
        "start_ms": int(getattr(item, "start_ms", 0)),
        "end_ms": int(getattr(item, "end_ms", 0)),
        "label": getattr(item, "label", None),
    }


def boundary_payload(evidence: BoundaryEvidence) -> dict[str, Any]:
    return {
        "original_ms": evidence.original_ms,
        "refined_ms": evidence.refined_ms,
        "confidence": evidence.confidence,
        "method": evidence.method,
        "warnings": list(evidence.warnings),
    }


def _retain_previous_ranges(
    proposed: tuple[KeepRange, ...],
    previous: list[dict[str, Any]],
    duration_ms: int,
) -> tuple[KeepRange, ...]:
    """Intersect a proposal with the retained timeline, preserving its labels."""
    previous_ranges = normalize_keep_ranges(
        [
            KeepRange(
                str(item["id"]), int(item["start_ms"]), int(item["end_ms"]),
                item.get("label"),
            )
            for item in previous
        ],
        duration_ms,
    )
    retained = [
        KeepRange(
            "pending", max(prior.start_ms, new.start_ms),
            min(prior.end_ms, new.end_ms), prior.label,
        )
        for prior in previous_ranges
        for new in proposed
        if min(prior.end_ms, new.end_ms) > max(prior.start_ms, new.start_ms)
    ]
    if not retained:
        raise ValueError("The proposal would remove the entire recording.")
    return normalize_keep_ranges(retained, duration_ms)


def prepare_media_edit_proposal(
    *,
    cues: list[MediaCue],
    duration_ms: int,
    previous_keep_ranges: list[dict[str, Any]],
    previous_instructions: str,
    previous_evidence: dict[str, Any],
    cuts: list[dict[str, Any]],
    provenance: dict[str, Any] | None = None,
    proposal_instructions: str | None = None,
    reject_duplicate_pairs: bool = False,
) -> ProposedMediaEdit:
    """Add removal spans to existing cuts without restoring removed material."""
    normalized_cuts: list[
        tuple[int, int, str, str, str, tuple[BoundaryEvidence, BoundaryEvidence], bool, bool]
    ] = []
    cue_by_id = {cue.id: (index, cue) for index, cue in enumerate(cues)}
    trustworthy_words: list[MediaWord] = []
    word_keys: set[tuple[str, int, int, float | None]] = set()
    for cue in cues:
        if (
            cue.timing_source not in {"asr_alignment", "ctc_alignment", "qwen3_alignment"}
            or cue.timing_confidence is None
            or cue.timing_confidence < 0.5
        ):
            continue
        for word in cue.words:
            key = (word.text, word.start_ms, word.end_ms, word.confidence)
            if key not in word_keys:
                word_keys.add(key)
                trustworthy_words.append(word)

    def refine_cue_boundary(
        cue: MediaCue, boundary_ms: int, *, side: Literal["start", "end"]
    ) -> BoundaryEvidence:
        if (
            cue.timing_source in {"asr_alignment", "ctc_alignment", "qwen3_alignment"}
            and cue.timing_confidence is not None
            and cue.timing_confidence >= 0.5
            and cue.words
        ):
            return refine_boundary(boundary_ms, trustworthy_words, side=side)
        return BoundaryEvidence(
            original_ms=boundary_ms,
            refined_ms=boundary_ms,
            confidence=0.0,
            method="caption_boundary",
            warnings=(
                (
                    "caption boundary preserved because no reliable ASR word "
                    "alignment was available"
                ),
            ),
        )

    seen: set[tuple[str, str]] = set()
    for item in cuts:
        if not isinstance(item, dict):
            raise TypeError("Each proposal cut must be an object.")
        start_at_media_start = bool(item.get("start_at_media_start", False))
        end_at_media_end = bool(item.get("end_at_media_end", False))
        start_value = item.get("start_cue_id")
        end_value = item.get("end_cue_id")
        if (start_value is not None) == start_at_media_start:
            raise ValueError(
                "Exactly one of start_cue_id or start_at_media_start=true is required."
            )
        if (end_value is not None) == end_at_media_end:
            raise ValueError(
                "Exactly one of end_cue_id or end_at_media_end=true is required."
            )
        start_id = str(start_value or "")
        end_id = str(end_value or "")
        reason_value = item.get("reason")
        if not isinstance(reason_value, str):
            raise TypeError("Proposal cut reason must be a string.")
        reason = reason_value.strip()
        if not start_at_media_start and start_id not in cue_by_id:
            raise ValueError("Proposal cut references an unknown cue ID.")
        if not end_at_media_end and end_id not in cue_by_id:
            raise ValueError("Proposal cut references an unknown cue ID.")
        start_index, start_cue = (
            cue_by_id[start_id] if not start_at_media_start else (-1, None)
        )
        end_index, end_cue = (
            cue_by_id[end_id] if not end_at_media_end else (len(cues), None)
        )
        if end_index < start_index:
            raise ValueError("Proposal cut cue IDs are out of order.")
        if not reason:
            raise ValueError("Proposal cut reasons must not be empty.")
        if len(reason) > 500:
            raise ValueError("Proposal cut reasons must be at most 500 characters.")
        key = (
            "__media_start__" if start_at_media_start else start_id,
            "__media_end__" if end_at_media_end else end_id,
        )
        if key in seen:
            if reject_duplicate_pairs:
                raise ValueError("Proposal cut cue IDs must be unique.")
            continue
        seen.add(key)
        start_evidence = (
            BoundaryEvidence(0, 0, 1.0, "media_start")
            if start_cue is None
            else refine_cue_boundary(start_cue, start_cue.start_ms, side="start")
        )
        end_evidence = (
            BoundaryEvidence(
                duration_ms, duration_ms, 1.0, "media_end"
            )
            if end_cue is None
            else refine_cue_boundary(end_cue, end_cue.end_ms, side="end")
        )
        if end_evidence.refined_ms <= start_evidence.refined_ms:
            raise ValueError(
                "Proposal cut boundaries do not form a positive interval."
            )
        normalized_cuts.append(
            (
                start_evidence.refined_ms,
                end_evidence.refined_ms,
                start_id,
                end_id,
                reason,
                (start_evidence, end_evidence),
                start_at_media_start,
                end_at_media_end,
            )
        )

    if normalized_cuts:
        keep_ranges = keep_ranges_from_cuts(
            [(item[0], item[1]) for item in normalized_cuts], duration_ms
        )
        if not keep_ranges:
            raise ValueError("The proposal would remove the entire recording.")
        keep_ranges = _retain_previous_ranges(keep_ranges, previous_keep_ranges, duration_ms)
        normalized_ranges = [keep_range_payload(item) for item in keep_ranges]
    else:
        normalized_ranges = list(previous_keep_ranges)

    next_instructions = (
        previous_instructions
        if proposal_instructions is None
        else proposal_instructions.strip()
    )
    if proposal_instructions is not None and not next_instructions:
        raise ValueError("Proposal instructions must not be empty.")

    evidence = dict(previous_evidence)
    warnings = list(evidence.get("warnings") or [])
    boundary_records: list[dict[str, Any]] = []
    for (
        start_ms,
        end_ms,
        start_id,
        end_id,
        reason,
        boundaries,
        start_at_media_start,
        end_at_media_end,
    ) in normalized_cuts:
        start_evidence, end_evidence = boundaries
        boundary_records.append(
            {
                "start_cue_id": None if start_at_media_start else start_id,
                "start_at_media_start": start_at_media_start,
                "end_cue_id": None if end_at_media_end else end_id,
                "end_at_media_end": end_at_media_end,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "reason": reason,
                "start": boundary_payload(start_evidence),
                "end": boundary_payload(end_evidence),
            }
        )
        warnings.extend(start_evidence.warnings)
        warnings.extend(end_evidence.warnings)
    for cue in cues:
        if cue.timing_confidence is not None and cue.timing_confidence < 0.5:
            warnings.append(f"Low-confidence timing for cue {cue.id}.")
    evidence["warnings"] = list(dict.fromkeys(str(item) for item in warnings))
    previous_proposal = evidence.get("agent_proposal")
    retained_records = (
        [item for item in previous_proposal.get("cuts") or [] if isinstance(item, dict)]
        if isinstance(previous_proposal, dict)
        else []
    )
    for record in boundary_records:
        if record not in retained_records:
            retained_records.append(record)
    evidence["agent_proposal"] = {"cuts": retained_records}
    operation = {
        "type": "agent_proposal",
        "cuts": boundary_records,
    }
    if provenance:
        operation = {
            "type": "passive_dispatch",
            **provenance,
            "cuts": boundary_records,
        }
        evidence["passive_dispatch"] = dict(provenance)
    return ProposedMediaEdit(next_instructions, normalized_ranges, evidence, operation)
