"""Passage-first planning mode and optional second-pass regroup selection.

Default voiceover/dubbing generation plans independent meaningful TIMED
PASSAGES. The character target (``speech_block_max_chars``) is a maximum, not
an encouragement to combine adjacent timed passages: overlong passages split
only at natural boundaries, and capacity splits inside one source window share
that window's timing envelope (``shared_passage_timing``) instead of being
fabricated as independently anchored passages. The legacy cue-packing behavior
remains available as ``speech_block_generation_mode = "legacy"``.

The optional second pass (``speech_block_regroup_enabled``, OFF by default)
regenerates selected adjacent-passage groups with the SAME TTS provider/model.
It never performs word alignment and never introduces a new model. Selection is
pure and deterministic so it can be unit-tested without a database or TTS:

- candidates are adjacent in source order, share speaker/voice/language, have
  no overlap/cut/user-forced boundary and carry no uncertain shared-timing
  artifacts;
- no candidate group spans a true edit cut in the CURRENT output timeline:
  cut positions are derived from the persisted media-edit keep_ranges (the
  cumulative output offsets where consecutive kept ranges are NOT contiguous
  in source time), never from cue IDs or renumbered ordinals. A passage that
  itself spans a cut is excluded, and a group whose combined span contains a
  cut is refused; contiguous keep ranges produce no cut. When the source
  lineage is a known edited (cut-derived) input but its keep_ranges mapping
  is missing or uncertain, selection conservatively yields no groups at all.
  Ordinary unedited inputs (no cut-derived ancestors) are unaffected;
- combined spoken AND display characters stay under ``speech_block_max_chars``;
- every original unpadded/unmodified take duration fits its source span within
  ``min(max_mismatch_ms, max_mismatch_percent of span)``, symmetrically;
- every internal source gap fits ``max_gap_ms``;
- at most 8 passages per group (hard cap matching the coordinated
  ``speech_block_regroup_max_passages`` setting bounds);
- cumulative internal boundary displacement estimated from first-pass durations
  against gap-inclusive source positions must stay within
  ``max_boundary_shift_ms`` at EVERY internal boundary, so an early violation
  cannot be hidden by later drift cancellation;
- exactly one generation per selected group, disjoint groups chosen solely
  from first-pass measurements (no recursive regrouping of regenerated audio);
- the regenerated group total must itself fit its group span within the same
  mismatch allowance, otherwise the originals are retained.

There is no guarantee of internal synchronization inside a regenerated group:
its internal sentence boundaries are not independently timed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping


GENERATION_MODES = ("passage", "legacy")
DEFAULT_GENERATION_MODE = "passage"

#: Uncertain timing artifacts: chunks that share one source timing window (or
#: carry overlap evidence) must never be treated as independently timed units.
UNCERTAIN_TIMING_FLAGS = frozenset(
    {
        "shared_passage_timing",
        "estimated_internal_timing",
        "timing_overlap",
    }
)

#: Absolute ceiling on regroup group size, regardless of configuration.
MAX_REGROUP_PASSAGES_HARD_CAP = 8

REGROUP_SPEC: dict[str, dict[str, Any]] = {
    "speech_block_generation_mode": {
        "default": "passage",
        "choices": ("passage", "legacy"),
    },
    "speech_block_regroup_enabled": {"default": False, "type": bool},
    "speech_block_regroup_max_mismatch_ms": {
        "default": 500,
        "type": int,
        "minimum": 0,
        "maximum": 2000,
    },
    "speech_block_regroup_max_mismatch_percent": {
        "default": 15,
        "type": int,
        "minimum": 0,
        "maximum": 50,
    },
    "speech_block_regroup_max_gap_ms": {
        "default": 300,
        "type": int,
        "minimum": 0,
        "maximum": 2000,
    },
    "speech_block_regroup_max_passages": {
        "default": 3,
        "type": int,
        "minimum": 2,
        "maximum": 8,
    },
    "speech_block_regroup_max_boundary_shift_ms": {
        "default": 500,
        "type": int,
        "minimum": 0,
        "maximum": 2000,
    },
}


def normalize_generation_mode(raw_value: Any) -> str:
    """Return the supported speech-block generation mode (default passage)."""

    normalized = str(raw_value if raw_value is not None else "").strip().lower()
    if not normalized:
        return DEFAULT_GENERATION_MODE
    if normalized not in GENERATION_MODES:
        raise ValueError(
            f"speech_block_generation_mode must be one of {list(GENERATION_MODES)}."
        )
    return normalized


def _check_int(key: str, value: Any, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{key} must be an integer from {minimum} to {maximum}.")
    if not minimum <= value <= maximum:
        raise ValueError(f"{key} must be an integer from {minimum} to {maximum}.")
    return value


def validate_regroup_settings(value: Mapping[str, Any]) -> None:
    """Validate only the regroup keys present in a sparse settings update."""

    if not isinstance(value, Mapping):
        raise ValueError("Regroup settings must be an object.")
    for key, spec in REGROUP_SPEC.items():
        if key not in value:
            continue
        candidate = value[key]
        if key == "speech_block_generation_mode":
            normalize_generation_mode(candidate)
        elif key == "speech_block_regroup_enabled":
            if not isinstance(candidate, bool):
                raise ValueError("speech_block_regroup_enabled must be a boolean.")
        else:
            assert spec["type"] is int
            _check_int(key, candidate, minimum=spec["minimum"], maximum=spec["maximum"])


def normalize_regroup_settings(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return effective regroup settings with coordinated defaults applied."""

    source = dict(raw or {})
    normalized: dict[str, Any] = {}
    for key, spec in REGROUP_SPEC.items():
        if key == "speech_block_generation_mode":
            normalized[key] = normalize_generation_mode(
                source.get(key, spec["default"])
            )
        elif key == "speech_block_regroup_enabled":
            candidate = source.get(key, spec["default"])
            if not isinstance(candidate, bool):
                raise ValueError("speech_block_regroup_enabled must be a boolean.")
            normalized[key] = candidate
        else:
            assert spec["type"] is int
            candidate = source.get(key, spec["default"])
            normalized[key] = _check_int(
                key, candidate, minimum=spec["minimum"], maximum=spec["maximum"]
            )
    return normalized


def mismatch_allowance_ms(
    source_span_ms: int, *, max_mismatch_ms: int, max_mismatch_percent: int
) -> int:
    """Symmetric fit allowance: ``min(max_ms, percent of span)``.

    The source span is an end-minus-start difference, so it already includes
    internal gaps. A non-positive span can never fit and yields zero allowance.
    """

    if source_span_ms <= 0:
        return 0
    return min(
        int(max_mismatch_ms), (int(source_span_ms) * int(max_mismatch_percent)) // 100
    )


@dataclass(frozen=True)
class RegroupPassage:
    """First-pass evidence for one independently timed passage."""

    key: str
    ordinal: int
    speaker: str = ""
    voice: str = ""
    voice_id: str = ""
    language: str = ""
    start_ms: int = 0
    end_ms: int = 0
    take_duration_ms: int = -1
    display_chars: int = 0
    speech_chars: int = 0
    risk_flags: tuple[str, ...] = ()
    user_locked: bool = False
    boundary_after_cut: bool = False
    # Raw source references (subtitle/passage ordinals) backing this passage.
    # Integer references let selection refuse to bridge an actual cut (a
    # removed source unit shows up as skipped numbering); non-integer
    # references cannot be verified and fall back to timestamp guards.
    source_refs: tuple = ()

    @property
    def span_ms(self) -> int:
        return int(self.end_ms) - int(self.start_ms)


@dataclass
class RegroupSelection:
    """Disjoint candidate groups plus per-start rejection diagnostics."""

    groups: list[tuple[str, ...]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)


def select_second_pass(
    settings_snapshot: Mapping[str, Any] | None,
    *,
    operation: str,
    has_selected_ids: bool,
    workflow_kind: str,
) -> str | None:
    """Select the optional post-generation pass: repair, regroup, or neither.

    Single wiring point shared by the generation runner and run-status
    reporting. Legacy planning keeps the old early split repair;
    passage-first planning uses the optional regroup second pass instead. The
    two never run together, and staged child runs (marked with either parent
    id) never trigger another pass, so at most two passes exist per
    first-pass run.
    """

    snapshot = dict(settings_snapshot or {})
    if (
        operation not in {"generate", "resume"}
        or has_selected_ids
        or workflow_kind != "voiceover"
        or snapshot.get("early_repair_parent_run_id")
        or snapshot.get("regroup_parent_run_id")
    ):
        return None
    tts = snapshot.get("tts") or {}
    try:
        mode = normalize_generation_mode(tts.get("speech_block_generation_mode"))
    except ValueError:
        mode = DEFAULT_GENERATION_MODE
    if mode == "passage":
        return "regroup" if tts.get("speech_block_regroup_enabled") is True else None
    return "repair" if tts.get("speech_block_early_repair_enabled") is True else None


def _int_source_refs(passage: RegroupPassage) -> list[int] | None:
    """Verifiable integer source references, or None when unverifiable."""

    refs: list[int] = []
    for ref in passage.source_refs:
        if isinstance(ref, bool):
            return None
        if isinstance(ref, float) and not ref.is_integer():
            return None
        try:
            refs.append(int(ref))
        except (TypeError, ValueError):
            return None
    return refs


def output_cut_positions_ms(
    keep_ranges: list[tuple[int, int]],
) -> list[int]:
    """Return true edit-cut positions in the CURRENT output timeline.

    Each entry is the cumulative output offset where one kept range ends and
    the next kept range resumes from a NON-contiguous source position, i.e.
    the exact join the renderer produced in the retimed output. Overlapping
    or contiguous ranges (``next.start <= previous.end``) are coalesced and
    produce no cut, so merely adjacent ranges never fake a cut. Source cue
    IDs, ordinals, and renumbering play no role here: after a rendered edit
    the surviving cues are renumbered contiguously, so integer reference gaps
    cannot be trusted to reveal cuts.
    """

    ordered = sorted(
        (int(start), int(end))
        for start, end in keep_ranges
        if int(end) > int(start)
    )
    if not ordered:
        return []
    coalesced: list[list[int]] = []
    for start, end in ordered:
        if coalesced and start <= coalesced[-1][1]:
            coalesced[-1][1] = max(coalesced[-1][1], end)
        else:
            coalesced.append([start, end])
    cuts: list[int] = []
    cursor = 0
    for position, (start, end) in enumerate(coalesced):
        if position > 0:
            # A true cut: the previous kept range ends at ``cursor`` in the
            # output timeline while the source jumps from the previous end
            # to this non-contiguous start.
            cuts.append(cursor)
        cursor += end - start
    return cuts


def _span_contains_cut(start_ms: int, end_ms: int, cuts: list[int]) -> bool:
    """Whether a cut lies strictly inside ``(start_ms, end_ms)``."""

    return any(start_ms < cut < end_ms for cut in cuts)


def _delivery_key(passage: RegroupPassage) -> tuple[str, str, str, str]:
    return (
        str(passage.speaker or ""),
        str(passage.voice or ""),
        str(passage.voice_id or ""),
        str(passage.language or ""),
    )


def _pair_rejection(
    left: RegroupPassage,
    right: RegroupPassage,
    *,
    max_chars: int,
    max_gap_ms: int,
) -> str | None:
    if right.ordinal != left.ordinal + 1:
        return "not_adjacent_in_plan_order"
    if left.boundary_after_cut:
        return "cut_boundary_between_passages"
    if right.start_ms < left.end_ms:
        return "source_windows_overlap"
    if right.start_ms - left.end_ms > max_gap_ms:
        return "source_gap_too_long"
    if _delivery_key(left) != _delivery_key(right):
        return "speaker_voice_language_mismatch"
    if set(left.risk_flags) & set(UNCERTAIN_TIMING_FLAGS):
        return "uncertain_shared_timing"
    if set(right.risk_flags) & set(UNCERTAIN_TIMING_FLAGS):
        return "uncertain_shared_timing"
    if left.user_locked or right.user_locked:
        return "user_locked_boundary"
    combined_display = int(left.display_chars) + 1 + int(right.display_chars)
    combined_speech = int(left.speech_chars) + 1 + int(right.speech_chars)
    if combined_display > max_chars or combined_speech > max_chars:
        return "combined_chars_exceed_cap"
    return None


def select_regroup_candidates(
    passages: list[RegroupPassage],
    *,
    max_chars: int,
    max_mismatch_ms: int = 500,
    max_mismatch_percent: int = 15,
    max_gap_ms: int = 300,
    max_passages: int = 3,
    max_boundary_shift_ms: int = 500,
    source_cut_positions: list[int] | tuple[int, ...] | None = None,
    edited_cuts_unknown: bool = False,
) -> RegroupSelection:
    """Select disjoint regroup candidates solely from first-pass evidence.

    The scan is left-to-right and greedy: at each ungrouped passage the
    longest valid extension (up to ``max_passages``, hard-capped at 8) wins.
    Every internal boundary of a candidate group is drift-checked, so an
    early violation cannot be hidden by later cancellation.

    ``source_cut_positions`` carries the true edit cuts in the CURRENT
    output timeline (see :func:`output_cut_positions_ms`): a passage whose
    own span contains a cut is rejected, and a group whose combined span
    contains a cut is refused at the crossing boundary. Per-cut exclusion
    keeps the rest of an edited recording eligible. When the input is a
    known edited (cut-derived) source but its cut mapping is missing or
    uncertain, pass ``edited_cuts_unknown=True`` (with no positions) and
    selection conservatively yields no groups. ``None`` positions with a
    false flag means an ordinary unedited input: fully eligible.
    """

    ordered = sorted(passages, key=lambda item: (item.ordinal, item.key))
    group_limit = max(2, min(int(max_passages), MAX_REGROUP_PASSAGES_HARD_CAP))
    allowances = {
        passage.key: mismatch_allowance_ms(
            passage.span_ms,
            max_mismatch_ms=max_mismatch_ms,
            max_mismatch_percent=max_mismatch_percent,
        )
        for passage in ordered
    }
    verifiable_refs = {passage.key: _int_source_refs(passage) for passage in ordered}

    def fits_own_span(passage: RegroupPassage) -> bool:
        if passage.span_ms <= 0 or passage.take_duration_ms < 0:
            return False
        return (
            abs(int(passage.take_duration_ms) - passage.span_ms)
            <= allowances[passage.key]
        )

    def extend_refs(
        seen: set[int] | None, candidate: RegroupPassage
    ) -> tuple[bool, set[int] | None]:
        """Extend verified references, rejecting bridged cuts.

        A removed source unit shows up as skipped reference numbering, and a
        reference claimed by two members means overlapping windows. Both
        reject the extension. Unverifiable (non-integer) references keep the
        caller's state and fall back to the timestamp guards.
        """

        fresh = verifiable_refs[candidate.key]
        if fresh is None:
            return True, seen
        fresh_set = set(fresh)
        if not fresh_set:
            return True, seen
        combined = fresh_set if seen is None else (seen | fresh_set)
        if seen is not None and not fresh_set.isdisjoint(seen):
            return False, seen
        if max(combined) - min(combined) + 1 != len(combined):
            return False, seen
        return True, combined

    selection = RegroupSelection()
    cuts = sorted({int(cut) for cut in (source_cut_positions or ())})
    if edited_cuts_unknown and not cuts:
        # Known cut-derived input, unrecoverable cut mapping: regrouping
        # anywhere could bridge a removed section, so select nothing.
        for passage in ordered:
            selection.rejected.append(
                {"key": passage.key, "reason": "edited_source_cuts_unknown"}
            )
        return selection
    index = 0
    while index < len(ordered):
        first = ordered[index]
        if not fits_own_span(first):
            selection.rejected.append(
                {"key": first.key, "reason": "take_duration_misfit"}
            )
            index += 1
            continue
        if cuts and _span_contains_cut(first.start_ms, first.end_ms, cuts):
            # The passage itself straddles a rendered edit join.
            selection.rejected.append(
                {"key": first.key, "reason": "source_cut_inside_passage"}
            )
            index += 1
            continue
        members: list[RegroupPassage] = [first]
        display_total = int(first.display_chars)
        speech_total = int(first.speech_chars)
        cumulative_duration = int(first.take_duration_ms)
        refs_ok, seen_refs = extend_refs(None, first)
        if not refs_ok:
            # The passage itself spans a cut in its own references.
            selection.rejected.append(
                {"key": first.key, "reason": "source_reference_cut"}
            )
            index += 1
            continue
        cursor = index + 1
        while cursor < len(ordered) and len(members) < group_limit:
            candidate = ordered[cursor]
            previous = members[-1]
            if not fits_own_span(candidate):
                selection.rejected.append(
                    {"key": candidate.key, "reason": "take_duration_misfit"}
                )
                break
            rejection = _pair_rejection(
                previous,
                candidate,
                max_chars=int(max_chars),
                max_gap_ms=int(max_gap_ms),
            )
            if rejection is not None:
                selection.rejected.append({"key": candidate.key, "reason": rejection})
                break
            if cuts and _span_contains_cut(first.start_ms, candidate.end_ms, cuts):
                # The combined group span crosses a rendered edit join,
                # even when the retimed output times look contiguous and
                # the renumbered source references show no gap.
                selection.rejected.append(
                    {"key": candidate.key, "reason": "source_cut_inside_group"}
                )
                break
            extended_ok, extended_refs = extend_refs(seen_refs, candidate)
            if not extended_ok:
                selection.rejected.append(
                    {"key": candidate.key, "reason": "source_reference_cut"}
                )
                break
            display_total += 1 + int(candidate.display_chars)
            speech_total += 1 + int(candidate.speech_chars)
            if display_total > int(max_chars) or speech_total > int(max_chars):
                selection.rejected.append(
                    {"key": candidate.key, "reason": "combined_chars_exceed_cap"}
                )
                break
            # Gap-inclusive displacement of the NEW internal boundary (between
            # the current members and this candidate). The regenerated group
            # is one continuous utterance, so the predicted boundary position
            # is the cumulative first-pass audio; the source boundary position
            # is the candidate start minus the group start, which includes
            # every source gap by construction. Absorbed gaps therefore show
            # up as systematic early drift instead of cancelling out.
            # Every internal boundary is checked, so an early violation
            # cannot be hidden by later drift cancellation.
            displacement = cumulative_duration - (candidate.start_ms - first.start_ms)
            if abs(displacement) > int(max_boundary_shift_ms):
                selection.rejected.append(
                    {"key": candidate.key, "reason": "boundary_shift_too_large"}
                )
                break
            cumulative_duration += int(candidate.take_duration_ms)
            seen_refs = extended_refs
            members.append(candidate)
            cursor += 1
        if len(members) >= 2:
            selection.groups.append(tuple(member.key for member in members))
            index += len(members)
        else:
            index += 1
    return selection


def regenerated_group_fits(
    total_duration_ms: int,
    group_span_ms: int,
    *,
    max_mismatch_ms: int = 500,
    max_mismatch_percent: int = 15,
) -> bool:
    """Validate regenerated audio against its group span (symmetric)."""

    if group_span_ms <= 0 or total_duration_ms < 0:
        return False
    return abs(int(total_duration_ms) - int(group_span_ms)) <= mismatch_allowance_ms(
        int(group_span_ms),
        max_mismatch_ms=max_mismatch_ms,
        max_mismatch_percent=max_mismatch_percent,
    )
