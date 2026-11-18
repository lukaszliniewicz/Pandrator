"""Optional cue-boundary refinement after a complete voiceover generation run.

Replacements are generated against an inactive immutable revision. Selection
changes only after synthesis and forward-timing validation both succeed.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.audio_sync import (
    NATURAL_OVERRUN_TOLERANCE_MS,
    NATURAL_OVERRUN_TOLERANCE_RATIO,
    _speed_up_wav_streaming,
    _streaming_audio_duration_ms,
    _trim_block_wav,
    alignment_adjustment,
    min_block_gap_ms_from_settings,
    sentence_gap_ms_from_settings,
    slowdown_enabled_from_settings,
    trim_silence_enabled_from_settings,
)
from pandrator.logic.dubbing.early_repair import find_repair_boundary
from pandrator.logic.dubbing.repair_markers import EARLY_REPAIR_MARKER_KEY, EARLY_REPAIR_REASON
from pandrator.logic.dubbing.timing_drift import PlacedSpeechBlock, late_cue_metrics

from .audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    preferred_pcm_format,
)
from .generation_audio_identity import plan_audio_identities
from .generation_start_preparation import snapshot_guard
from .generation_topology import GenerationTopologyService
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    Segment,
    utcnow,
)
from .output_assembly_lifecycle import mark_output_assemblies_stale
from .settings_policy import stable_hash
from .speech_plan_workspace import freeze_speech_snapshot, plan_signature

logger = logging.getLogger(__name__)


class _RepairNotApplied(Exception):
    """Reject only this optional repair; keep the original plan and takes."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _record_repair_outcome(
    session: Session, revision_id: str, status: str, reason: str | None = None
) -> None:
    revision = session.get(GenerationPlanRevision, revision_id)
    if revision is not None:
        revision.operation_json = {
            **dict(revision.operation_json or {}),
            "repair_status": status,
            "repair_reason": reason,
        }


@dataclass
class TimingGroup:
    segments: list[GenerationSegment]
    paths: list[Path]
    start_ms: int
    end_ms: int
    references: list[int]
    cues: list[tuple[int, int]] = field(default_factory=list)


def _selection_state(session: Session, revision_id: str) -> tuple[dict[str, str], str]:
    """Fence take choices as well as text when replacing a live plan."""
    rows = list(
        session.execute(
            select(
                GenerationSegment.id,
                AudioTake.id,
                AudioTake.revision,
                AudioTake.status,
                AudioTake.artifact_id,
                Artifact.state,
            )
            .outerjoin(
                AudioTake,
                (AudioTake.generation_segment_id == GenerationSegment.id)
                & AudioTake.is_active.is_(True),
            )
            .outerjoin(Artifact, Artifact.id == AudioTake.artifact_id)
            .where(
                GenerationSegment.plan_revision_id == revision_id,
                GenerationSegment.removed.is_(False),
            )
            .order_by(GenerationSegment.ordinal, AudioTake.id)
        )
    )
    choices = {
        segment_id: take_id
        for segment_id, take_id, _revision, status, _artifact, state in rows
        if take_id and status == "completed" and state == "current"
    }
    if len(choices) != len(rows):
        choices = {}
    return choices, stable_hash([list(row) for row in rows])


def _load_groups(handler: Any, run_id: str) -> tuple[list[TimingGroup], dict[str, str]]:
    """Use the same source revision, take ownership and grouping as assembly."""
    with handler.database.session() as session:
        run = session.get(GenerationRun, run_id)
        revision = session.get(GenerationPlanRevision, run.plan_revision_id)
        timings = {}
        for cue in session.scalars(
            select(Segment).where(Segment.revision_id == revision.source_revision_id)
        ):
            if cue.start_ms is not None and cue.end_ms is not None:
                timing = (int(cue.start_ms), int(cue.end_ms), cue.ordinal + 1)
                timings[str(cue.id)] = timing
                timings[str(cue.ordinal + 1)] = timing
        if not timings:
            return [], {}
        rows = session.execute(
            select(GenerationSegment, AudioTake, Artifact)
            .join(AudioTake, AudioTake.generation_segment_id == GenerationSegment.id)
            .join(Artifact, Artifact.id == AudioTake.artifact_id)
            .where(
                GenerationSegment.plan_revision_id == run.plan_revision_id,
                GenerationSegment.removed.is_(False),
                AudioTake.generation_run_id == run_id,
                AudioTake.status == "completed",
                Artifact.state == "current",
            )
            .order_by(
                GenerationSegment.ordinal,
                AudioTake.created_at.desc(),
                AudioTake.id.desc(),
            )
        ).all()
        count = session.scalar(
            select(func.count())
            .select_from(GenerationSegment)
            .where(
                GenerationSegment.plan_revision_id == run.plan_revision_id,
                GenerationSegment.removed.is_(False),
            )
        )
        groups: list[TimingGroup] = []
        takes: dict[str, str] = {}
        previous_group = None
        for segment, take, artifact in rows:
            if segment.id in takes:
                continue
            refs = sorted(
                {
                    timings[str(ref)]
                    for ref in segment.source_segment_ids_json
                    if str(ref) in timings
                }
            )
            if not refs or segment.node_kind != "subtitle_cue":
                return [], {}
            path = handler.paths.managed_path(artifact.relative_path)
            if not path.is_file():
                return [], {}
            takes[segment.id] = take.id
            explicit_group = str(segment.alignment_group or "").strip() or None
            same_group = bool(
                groups
                and (
                    (explicit_group and explicit_group == previous_group)
                    or (
                        not explicit_group
                        and not previous_group
                        and groups[-1].references[-1:] == [refs[0][2]]
                    )
                )
            )
            if same_group:
                groups[-1].segments.append(segment)
                groups[-1].paths.append(path)
                groups[-1].end_ms = max(groups[-1].end_ms, refs[-1][1])
                groups[-1].references = sorted(
                    {*groups[-1].references, *(ref[2] for ref in refs)}
                )
                groups[-1].cues = sorted({*groups[-1].cues, *((ref[0], ref[1]) for ref in refs)})
            else:
                groups.append(
                    TimingGroup(
                        [segment],
                        [path],
                        refs[0][0],
                        refs[-1][1],
                        [ref[2] for ref in refs],
                        sorted((ref[0], ref[1]) for ref in refs),
                    )
                )
            previous_group = explicit_group
        if len(takes) != count:
            return [], {}
        groups.sort(
            key=lambda group: (group.start_ms, group.end_ms, group.segments[0].ordinal)
        )
        return groups, takes


def advance_timing(
    group: TimingGroup,
    cursor: int,
    slot_end: int,
    settings: dict[str, Any],
    work: Path,
    cancel_event: Any,
    *,
    has_next_block: bool = False,
    diagnostics: dict[str, int] | None = None,
) -> tuple[int, int, int]:
    """Measure forward placement, including FFmpeg rounding, without a full mix.

    This preview uses the fitter's shared adjustment and tempo implementation.
    It retains the next-start catch-up window and the own-cue slowdown window.
    """
    sample_rate, channels = preferred_pcm_format(
        group.paths[0], cancel_event=cancel_event
    )
    gap = sentence_gap_ms_from_settings(settings)
    parts = [
        AudioAssemblyPart(
            path=path,
            expected_duration_ms=_streaming_audio_duration_ms(path),
            silence_after_ms=gap if index < len(group.paths) - 1 else 0,
        )
        for index, path in enumerate(group.paths)
    ]
    plan = build_audio_assembly_plan(
        parts, output_format="wav", sample_rate_hz=sample_rate, channels=channels
    )
    source = work / "timing-input.wav"
    duration = assemble_audio_plan(
        plan, source, backend="streaming", work_dir=work, cancel_event=cancel_event
    ).duration_ms
    if trim_silence_enabled_from_settings(settings):
        # Assembly fits the trimmed block, so the preview measures that one.
        trimmed_source, trimmed_lead_ms, trimmed_tail_ms = _trim_block_wav(
            source, work / "timing-input-trimmed.wav"
        )
        if trimmed_lead_ms or trimmed_tail_ms:
            source = trimmed_source
            duration = _streaming_audio_duration_ms(source)
    start = max(0, group.start_ms)
    cursor = max(cursor, start)
    slot_end = max(start + 1, slot_end)
    effective_gap = min_block_gap_ms_from_settings(settings) if has_next_block else 0
    speed = float(settings.get("synchronization_speed") or 1.0)
    maximum = min(4.0, max(1.0, round(speed * 100 if speed <= 10 else speed) / 100))
    adjustment = alignment_adjustment(
        duration,
        slot_end - start,
        cursor - start,
        delay_start_ms=max(0, int(settings.get("synchronization_delay_ms") or 0)),
        max_speed_factor=maximum,
        allow_slowdown=slowdown_enabled_from_settings(settings),
        speech_window_duration_ms=max(1, group.end_ms - start),
        min_block_gap_ms=effective_gap,
    )
    processed = duration
    factor = adjustment.speed_factor
    if abs(factor - 1.0) > 0.0001:
        for attempt in range(2):
            destination = work / f"timing-tempo-{attempt}.wav"
            _speed_up_wav_streaming(
                source,
                destination,
                factor,
                sample_rate_hz=sample_rate,
                channels=channels,
                ffmpeg_executable="ffmpeg",
                cancel_event=cancel_event,
            )
            processed = _streaming_audio_duration_ms(destination)
            if factor < 1:
                if processed + adjustment.start_delay_ms > min(
                    adjustment.available_ms,
                    max(0, group.end_ms - start - adjustment.drift_ms),
                ):
                    processed = duration
                break
            if processed <= adjustment.available_ms or factor >= maximum - 0.0001:
                break
            factor = min(
                maximum, factor * processed / max(1, adjustment.available_ms - 1)
            )
    if diagnostics is not None:
        diagnostics.update(original_audio_ms=duration, processed_audio_ms=processed)
    return (
        max(slot_end, cursor + adjustment.start_delay_ms + processed + effective_gap),
        duration,
        adjustment.start_delay_ms,
    )


@dataclass(frozen=True)
class GroupInspection:
    """One block's measured placement and its optional repair boundary."""

    boundary: Any | None
    baseline_cursor: int
    speech_start_ms: int
    speech_end_ms: int
    original_audio_ms: int
    start_delay_ms: int


def _grown_refreshed_neighbours(
    handler: Any,
    refreshed_context_parents: dict[str, str],
    take_ids: dict[str, str],
    staged_run_id: str,
) -> set[str]:
    """Parents whose refreshed take is longer than the take it replaced.

    A shorter take cannot worsen its original timing slot; a longer one is not
    rejected on its length any more, but it must be measured.  Missing
    durations stay fatal for the attempt, as before.
    """

    grown: set[str] = set()
    with handler.database.session() as session:
        for descendant_id, old_id in refreshed_context_parents.items():
            original_take = session.get(AudioTake, take_ids.get(old_id) or "")
            refreshed_take = session.scalar(
                select(AudioTake)
                .where(
                    AudioTake.generation_segment_id == descendant_id,
                    AudioTake.generation_run_id == staged_run_id,
                    AudioTake.status == "completed",
                )
                .order_by(AudioTake.created_at.desc(), AudioTake.id.desc())
                .limit(1)
            )
            if (
                original_take is None
                or refreshed_take is None
                or original_take.duration_ms is None
                or refreshed_take.duration_ms is None
            ):
                raise _RepairNotApplied("context_neighbour_unmeasurable")
            if refreshed_take.duration_ms > original_take.duration_ms:
                grown.add(old_id)
    return grown


def _refreshed_neighbour_groups(
    groups: list[TimingGroup],
    current_ids: dict[str, str],
    refreshed_context_parents: dict[str, str],
    staged_groups: list[TimingGroup],
) -> dict[str, tuple[int, TimingGroup]]:
    """Locate each refreshed neighbour as a loop index plus its staged group.

    ``refreshed_context_parents`` is keyed by the staged descendant and names
    the parent in the current revision; ``current_ids`` maps the loop's source
    blocks to that revision.  The staged group carries the take that was just
    synthesised, so forward placement can measure it instead of the old audio.
    """

    descendant_by_parent = {
        old_id: descendant_id
        for descendant_id, old_id in refreshed_context_parents.items()
    }
    index_by_current = {
        current_ids.get(segment.id, segment.id): index
        for index, group in enumerate(groups)
        for segment in group.segments
    }
    staged_by_segment = {
        segment.id: group for group in staged_groups for segment in group.segments
    }
    located: dict[str, tuple[int, TimingGroup]] = {}
    for old_id, descendant_id in descendant_by_parent.items():
        index = index_by_current.get(old_id)
        staged_group = staged_by_segment.get(descendant_id)
        if index is None or staged_group is None:
            # A parent can be a block an earlier repair split; the loop keeps
            # its original block list, so such a neighbour has no loop index.
            # It is reported by the caller, which only needs it for growth.
            continue
        located[old_id] = (index, staged_group)
    return located


def _adopt_refreshed_takes(
    groups: list[TimingGroup],
    staged_neighbours: dict[int, TimingGroup],
) -> None:
    """Measure a refreshed neighbour's new take in later loop iterations.

    The loop keeps the source run's block list, so its placement would keep
    reading the old audio after a repair re-synthesised a neighbour. Swapping
    the paths keeps block identity (and ordinals) while the measured duration
    follows the take the plan now holds. A group whose segment count disagrees
    is left untouched; it cannot be aligned safely.
    """

    for block_index, staged_group in staged_neighbours.items():
        if block_index < 0 or block_index >= len(groups):
            continue
        measured = groups[block_index]
        if len(staged_group.paths) == len(measured.paths):
            measured.paths = list(staged_group.paths)


def _place_group_run(
    groups: list[TimingGroup],
    first: int,
    last: int,
    cursor: int,
    settings: dict[str, Any],
    work: Path,
    cancel_event: Any,
    *,
    overrides: dict[int, TimingGroup] | None = None,
) -> int:
    """Forward-place groups ``first``..``last`` and return the cursor after them.

    Slots and next-block gaps mirror ``_inspect_group``; an override supplies
    the staged group (and therefore the refreshed take) for one index.
    """

    overrides = overrides or {}
    for index in range(first, last + 1):
        group = overrides.get(index, groups[index])
        slot_end = (
            groups[index + 1].start_ms if index + 1 < len(groups) else group.end_ms
        )
        cursor, _duration, _delay = advance_timing(
            group,
            cursor,
            slot_end,
            settings,
            work,
            cancel_event,
            has_next_block=index + 1 < len(groups),
        )
    return cursor


_NEIGHBOUR_GROWTH_PRECHECK = "neighbour_growth_precheck"


def neighbour_growth_precheck(
    *,
    estimated_advance_ms: int,
    sentence_gap_ms: int,
) -> str | None:
    """Refuse a candidate whose modelled gain cannot survive the split's own pause.

    A split only helps when the second half starts earlier than the original
    block did.  ``find_repair_boundary`` already requires a modelled advance,
    but it does not subtract the mandatory pause the two halves need between
    them: when that pause consumes the whole advance, placing the second half
    cannot gain anything and the pass rejects the attempt as added delay.  That
    outcome is certain without synthesising anything, so a candidate in that
    state is skipped with ``neighbour_growth_precheck`` before any call.

    Cue-window slack is deliberately not part of this rule.  The pass no longer
    rejects a refreshed neighbour for growing; it verifies the forward
    placement instead, and window slack does not predict whether a fresh take
    comes out longer.  Skipping on it could therefore lose a repair that would
    have been kept.
    """

    if estimated_advance_ms - sentence_gap_ms <= 0:
        return _NEIGHBOUR_GROWTH_PRECHECK
    return None


def _context_neighbour_count(
    groups: list[TimingGroup],
    index: int,
    tts_settings: dict[str, Any],
) -> int:
    """Count the blocks whose window a split at ``index`` can refresh.

    A block's semantic window holds whole neighbouring block texts, so only
    blocks within the configured context counts around the split can see
    different text.  A split replaces one block's text with two, so this is the
    candidate set the pass considers for re-synthesis; it then compares the
    frozen windows and keeps only those that actually changed.  Counting blocks
    (not context units) can only widen the set, so a preview estimate never
    under-counts the neighbours the pass would pay for.
    """

    before = tts_settings.get("performance_context_before", 2)
    after = tts_settings.get("performance_context_after", 1)
    before = before if isinstance(before, int) and not isinstance(before, bool) else 2
    after = after if isinstance(after, int) and not isinstance(after, bool) else 1
    return len(range(max(0, index - after), index)) + len(
        range(index + 1, min(len(groups), index + 1 + max(0, before)))
    )


def _inspect_group(
    group: TimingGroup,
    index: int,
    groups: list[TimingGroup],
    cursor: int,
    audio_settings: dict[str, Any],
    tts_settings: dict[str, Any],
    thresholds: dict[str, int],
    work: Path,
    cancel_event: Any,
) -> GroupInspection:
    """Measure one block exactly as the repair pass does, then look for a split."""

    slot_end = groups[index + 1].start_ms if index + 1 < len(groups) else group.end_ms
    diagnostics: dict[str, int] = {}
    baseline_cursor, duration, start_delay = advance_timing(
        group,
        cursor,
        slot_end,
        audio_settings,
        work,
        cancel_event,
        has_next_block=index + 1 < len(groups),
        diagnostics=diagnostics,
    )
    placed_start = max(cursor, max(0, group.start_ms))
    processed_ms = diagnostics.get("processed_audio_ms", duration)
    segment = group.segments[0]
    boundary = None
    provenance = dict(segment.speech_block_provenance_json or {})
    if (
        len(group.segments) == 1
        and not {
            "estimated_internal_timing",
            "shared_passage_timing",
        }.intersection(provenance.get("risk_flags") or [])
    ):
        boundary = find_repair_boundary(
            segment.text,
            segment.optimized_text or segment.text,
            provenance,
            # Judge the rendered take after gentle tempo adjustment,
            # not the shorter unprocessed source audio.
            processed_ms,
            incoming_delay_ms=max(0, cursor - group.start_ms),
            start_delay_ms=start_delay,
            language_code=str(segment.language or tts_settings.get("language") or "en"),
            **thresholds,
        )
    if boundary is not None and (boundary.start_ms, boundary.end_ms) != (
        group.start_ms,
        group.end_ms,
    ):
        boundary = None
    return GroupInspection(
        boundary=boundary,
        baseline_cursor=baseline_cursor,
        speech_start_ms=placed_start + start_delay,
        speech_end_ms=placed_start + start_delay + processed_ms,
        original_audio_ms=processed_ms,
        start_delay_ms=start_delay,
    )


def _source_context_available(
    snapshot: dict[str, Any], revision_id: str, take_ids: dict[str, str]
) -> bool:
    """Return whether the frozen semantic neighbours prove the source takes.

    Older or interrupted voiceovers may predate immutable semantic windows.
    Reconstructing their context from today's settings would not prove that
    existing audio was spoken with that same context, so both the automatic
    pass and an explicit preview fail closed instead.
    """

    from .generation_performance_snapshot import (
        frozen_semantic_contexts,
        performance_runtime_settings,
    )

    if str(performance_runtime_settings(snapshot).get("tts_context_mode") or "off") == "off":
        return True
    frozen = snapshot.get("semantic_context_snapshot") or {}
    try:
        context_units = frozen_semantic_contexts(snapshot)
    except (ValueError, TypeError, KeyError):
        # A future/unknown schema is not evidence of the original TTS
        # neighbours. Optional repair must refuse it without failing the
        # completed source generation job.
        context_units = {}
    return (
        frozen.get("plan_revision_id") == revision_id
        and set(take_ids).issubset(context_units)
    )


def _coerced_thresholds(value: Any) -> dict[str, int] | None:
    """Read the four repair thresholds carried by an explicit request payload."""

    if not isinstance(value, dict):
        return None
    from .settings_policy import repair_thresholds

    prefixed = {
        f"speech_block_early_repair_{name}": number
        for name, number in value.items()
        if isinstance(name, str)
    }
    return repair_thresholds(prefixed)


class RepairCancellation:
    def __init__(self, handler: Any, source_run_id: str, event: Any):
        self.handler = handler
        self.source_run_id = source_run_id
        self.event = event

    def is_set(self) -> bool:
        if self.event.is_set():
            return True
        with self.handler.database.session() as session:
            run = session.get(GenerationRun, self.source_run_id)
            return run is None or run.cancel_requested or run.pause_requested

    def wait(self, timeout: float | None = None) -> bool:
        return self.is_set() or self.event.wait(timeout) or self.is_set()


def _skipped_repair_result(
    reason: str, run_id: str, plan_revision_id: str
) -> dict[str, Any]:
    """Return the same durable envelope for every fail-closed skip path."""

    return {
        "repair_checked_blocks": 0,
        "repair_candidate_blocks": 0,
        "repair_attempted_blocks": 0,
        "repair_rejection_reasons": {reason: 1},
        "repaired_blocks": 0,
        "repaired_generation_run_id": run_id,
        "repaired_plan_revision_id": plan_revision_id,
        "repair_effect": {
            "late_cues_before": 0,
            "late_cues_after": 0,
            "worst_lag_ms_before": 0,
            "worst_lag_ms_after": 0,
            "blocks": 0,
        },
    }



def repair_early_blocks(
    handler: Any,
    run_id: str,
    progress: Any,
    cancel_event: Any,
    *,
    stage_job_id: str | None = None,
    thresholds: dict[str, int] | None = None,
) -> dict[str, Any]:
    """One pass over original blocks; never recursively repair new children.

    ``thresholds`` carries the current repair thresholds of an explicit
    request.  The automatic second pass of a full run passes none and uses the
    thresholds frozen with that run, so its replay stays reproducible.
    """
    from .repair_batches import GUARD_KEY, capture_repair_base, record_accepted_repair
    from .settings_policy import repair_thresholds as _resolve_thresholds

    with handler.database.session() as session:
        source_run = session.get(GenerationRun, run_id)
        snapshot = deepcopy(source_run.settings_snapshot_json or {})
        source_revision_id = source_run.plan_revision_id
        session_id = source_run.session_id
        active = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        if active is None or active.active_revision_id != source_revision_id:
            return _skipped_repair_result(
                "active_plan_changed", run_id, source_revision_id
            )
        batch_snapshot = capture_repair_base(session, source_revision_id)
    groups, take_ids = _load_groups(handler, run_id)
    if not groups:
        return _skipped_repair_result(
            "no_speech_blocks", run_id, source_revision_id
        )
    service = GenerationTopologyService(handler.database, handler.jobs)
    stop = RepairCancellation(handler, run_id, cancel_event)
    current_ids = {
        segment.id: segment.id for group in groups for segment in group.segments
    }
    current_run_id = run_id
    current_revision_id = source_revision_id
    repaired = 0
    repair_candidates = 0
    repair_attempts = 0
    rejection_reasons: Counter[str] = Counter()
    effect: dict[str, int] = {
        "late_cues_before": 0,
        "late_cues_after": 0,
        "worst_lag_ms_before": 0,
        "worst_lag_ms_after": 0,
        "blocks": 0,
    }
    cursor = 0
    from .generation_performance_snapshot import performance_runtime_settings

    audio_settings = dict(snapshot.get("audio") or {})
    # Resolve semantic context and repair parameters exactly as the frozen
    # synthesis snapshot does, including the selected segment override.
    tts_settings = performance_runtime_settings(snapshot)
    context_mode = str(tts_settings.get("tts_context_mode") or "off")
    if not _source_context_available(snapshot, source_revision_id, take_ids):
        return _skipped_repair_result(
            "missing_source_semantic_context", run_id, source_revision_id
        )
    frozen_thresholds = _resolve_thresholds(tts_settings)
    thresholds = thresholds or frozen_thresholds
    with tempfile.TemporaryDirectory(prefix="voiceover-repair-") as directory:
        work = Path(directory)
        checked = 0
        inspection_failed = False
        pass_completed = False
        # The cursor that entered each block in this pass's own placement. A
        # later attempt needs it to compare a refreshed preceding neighbour
        # against the placement the pass actually used.
        entry_cursors: dict[int, int] = {}
        # Source blocks whose take was re-synthesised by an applied repair.
        # Their recorded entry cursor was measured with the old audio, so a
        # preceding-neighbour comparison through them cannot be trusted.
        refreshed_by_earlier_repairs: set[str] = set()
        for index, group in enumerate(groups):
            entry_cursors[index] = cursor
            if stop.is_set():
                rejection_reasons["generation_stopped"] += 1
                break
            progress(
                index / len(groups),
                f"Repairing timing: {index + 1} of {len(groups)} blocks",
            )
            try:
                inspection = _inspect_group(
                    group,
                    index,
                    groups,
                    cursor,
                    audio_settings,
                    tts_settings,
                    thresholds,
                    work,
                    stop,
                )
            except Exception:
                logger.warning(
                    "Voiceover timing inspection stopped; selected audio is unchanged.",
                    exc_info=True,
                )
                rejection_reasons["timing_inspection_failed"] += 1
                inspection_failed = True
                break
            checked += 1
            segment = group.segments[0]
            slot_end = (
                groups[index + 1].start_ms if index + 1 < len(groups) else group.end_ms
            )
            baseline_cursor = inspection.baseline_cursor
            boundary = inspection.boundary
            if boundary is None:
                cursor = baseline_cursor
                continue
            repair_candidates += 1
            precheck = neighbour_growth_precheck(
                estimated_advance_ms=boundary.estimated_advance_ms,
                sentence_gap_ms=sentence_gap_ms_from_settings(audio_settings),
            )
            if precheck is not None:
                # The split cannot move the second half even with no neighbour
                # growth, so the pass would reject it as added delay.
                rejection_reasons[precheck] += 1
                cursor = baseline_cursor
                continue
            before_effect = late_cue_metrics(
                [
                    PlacedSpeechBlock(
                        inspection.speech_start_ms,
                        inspection.speech_end_ms,
                        group.cues,
                    )
                ]
            )
            staged_run_id = None
            staged_revision_id = None
            repair_status = "failed"
            repair_reason: str | None = "generation_failed"
            try:
                with handler.database.immediate_session() as session:
                    choices, source_take_signature = _selection_state(
                        session, current_revision_id
                    )
                    if choices != take_ids:
                        # A choice made before staging also wins over the run
                        # audio used to measure the proposed repair. Preserve
                        # its provenance as a user selection, not a TTS error.
                        repair_status, repair_reason = (
                            "not_applied", "selection_changed"
                        )
                        break
                    # The public split semantics are reused, but activation waits for audio.
                    proposal = service.revise_topology_in_session(
                        session,
                        session_id,
                        current_revision_id,
                        {
                            "action": "split",
                            "segment_id": current_ids[segment.id],
                            "text_layer": "display",
                            "cursor": boundary.display_cursor,
                            "reason": EARLY_REPAIR_REASON,
                            "source_generation_run_id": run_id,
                            GUARD_KEY: deepcopy(batch_snapshot),
                            "source_block_ordinal": segment.ordinal,
                            "repair_status": "pending",
                        },
                        activate=False,
                    )
                    staged_revision_id = proposal["plan_revision_id"]
                    children = [
                        session.get(GenerationSegment, child_id)
                        for child_id in proposal["affected_segment_ids"]
                    ]
                    expected_speech = [
                        (segment.optimized_text or segment.text)[
                            : boundary.speech_cursor
                        ].strip(),
                        (segment.optimized_text or segment.text)[
                            boundary.speech_cursor :
                        ].strip(),
                    ]
                    if (
                        len(children) != 2
                        or [(child.optimized_text or child.text) for child in children]
                        != expected_speech
                    ):
                        raise ValueError(
                            "Cue boundary no longer maps to the spoken text."
                        )
                    source_signature = plan_signature(session, current_revision_id)
                # The staged split is committed; if anything below fails it stays
                # in history unapplied, like a repair whose audio adds delay.
                # Freezing it and its audio identities reads the whole plan, which
                # can take minutes on a long cast plan, so it runs outside the
                # writer. The guard is taken first, and the writer below rechecks
                # it with the take choices and the source plan.
                with handler.database.session() as session:
                    guard = snapshot_guard(session, session_id, staged_revision_id)
                    child_snapshot = deepcopy(snapshot)
                    child_snapshot["speech_plan_revision_id"] = proposal[
                        "plan_revision_id"
                    ]
                    child_snapshot[EARLY_REPAIR_MARKER_KEY] = run_id
                    freeze_speech_snapshot(
                        session,
                        proposal["plan_revision_id"],
                        child_snapshot,
                        explicit=True,
                    )
                    # The parent run's semantic neighbours are keyed by its
                    # original block IDs. A split must rebind context to the
                    # two newly authored blocks, never silently omit context.
                    from .generation_performance_snapshot import (
                        frozen_semantic_contexts,
                        performance_runtime_settings,
                    )

                    # Match exactly the effective settings used by synthesis:
                    # audio, TTS and the supported selected-segment override.
                    # The TTS-only value can disagree with the frozen context
                    # and would otherwise silently reuse stale neighbour takes.
                    context_mode = str(
                        performance_runtime_settings(child_snapshot).get(
                            "tts_context_mode"
                        ) or "off"
                    )
                    if context_mode != "off":
                        frozen = child_snapshot.get("semantic_context_snapshot") or {}
                        child_ids = set(proposal["affected_segment_ids"])
                        frozen_ids = {
                            str(item.get("id"))
                            for item in frozen.get("units") or []
                            if isinstance(item, dict)
                        }
                        if (
                            frozen.get("plan_revision_id")
                            != proposal["plan_revision_id"]
                            or not child_ids.issubset(frozen_ids)
                            or not child_ids.issubset(
                                frozen_semantic_contexts(child_snapshot)
                            )
                        ):
                            raise ValueError(
                                "Split speech blocks have no revision-matched "
                                "semantic context; preserving original audio."
                            )
                    # Semantic windows use neighbouring block text.  A
                    # split changes the context of nearby unchanged blocks,
                    # so their old takes cannot be carried over implicitly.
                    # Re-synthesise only those neighbours whose actual
                    # before/after context differs on the staged revision.
                    changed_context_clones: set[str] = set()
                    # descendant -> parent block.  The parent take is
                    # independently retained so a neighbour that grows in
                    # duration cannot silently worsen downstream timing.
                    refreshed_context_parents: dict[str, str] = {}
                    if context_mode != "off":
                        # Compare with the semantic evidence actually frozen
                        # for the selected source takes, not merely a window
                        # recalculated from the new settings.  Later repairs
                        # derive from the latest accepted child run.
                        current_run = session.get(GenerationRun, current_run_id)
                        if current_run is None:
                            raise ValueError("The source generation run is unavailable.")
                        current_snapshot = current_run.settings_snapshot_json or {}
                        prior_frozen = current_snapshot.get("semantic_context_snapshot") or {}
                        if prior_frozen.get("plan_revision_id") != current_revision_id:
                            raise _RepairNotApplied("missing_source_semantic_context")
                        old_contexts = frozen_semantic_contexts(current_snapshot)
                        if not set(proposal["lineage"]).issubset(old_contexts):
                            raise _RepairNotApplied("missing_source_semantic_context")
                        new_contexts = frozen_semantic_contexts(child_snapshot)
                        for old_id, descendants in proposal["lineage"].items():
                            if (
                                len(descendants) == 1
                                and old_contexts.get(old_id)
                                != new_contexts.get(descendants[0])
                            ):
                                changed_context_clones.add(descendants[0])
                                refreshed_context_parents[descendants[0]] = old_id
                        # The active-take check above (choices != take_ids)
                        # is the source-of-truth guard for manually selected
                        # alternatives. Historical inactive takes may coexist
                        # with valid accepted repairs and do not block them.
                    child_snapshot["generation_audio_identities"] = (
                        plan_audio_identities(
                            session, proposal["plan_revision_id"], child_snapshot
                        )
                    )
                if stop.is_set():
                    repair_status, repair_reason = "stopped", "generation_stopped"
                    break
                with handler.database.immediate_session() as session:
                    plan = session.scalar(
                        select(GenerationPlan).where(
                            GenerationPlan.session_id == session_id
                        )
                    )
                    if (
                        # Staging checked the active revision; recheck it, since
                        # the pass above ran outside the writer.
                        plan is None
                        or plan.active_revision_id != current_revision_id
                        or _selection_state(session, current_revision_id)
                        != (choices, source_take_signature)
                        or plan_signature(session, current_revision_id) != source_signature
                        or snapshot_guard(session, session_id, staged_revision_id) != guard
                    ):
                        repair_status, repair_reason = (
                            "not_applied", "selection_changed"
                        )
                        break
                    sequence = (
                        int(
                            session.scalar(
                                select(func.max(GenerationRun.sequence_number)).where(
                                    GenerationRun.session_id == session_id
                                )
                            )
                            or 0
                        )
                        + 1
                    )
                    staged = GenerationRun(
                        session_id=session_id,
                        plan_revision_id=proposal["plan_revision_id"],
                        source_generation_run_id=current_run_id,
                        job_id=stage_job_id or source_run.job_id,
                        sequence_number=sequence,
                        operation="generate",
                        status="queued",
                        settings_snapshot_json=child_snapshot,
                        settings_hash=stable_hash(child_snapshot),
                    )
                    session.add(staged)
                    session.flush()
                    staged_run_id = staged.id
                    preserved_clones = {
                        (take.generation_segment_id, take.parent_take_id): take
                        for take in session.scalars(
                            select(AudioTake).join(
                                GenerationSegment,
                                GenerationSegment.id == AudioTake.generation_segment_id,
                            ).where(GenerationSegment.plan_revision_id == staged_revision_id)
                        )
                    }
                    for old_id, descendants in proposal["lineage"].items():
                        if len(descendants) != 1:
                            continue
                        original_take_id = take_ids.get(old_id)
                        if original_take_id is None:
                            raise ValueError(
                                "The original audio selection changed during repair."
                            )
                        clone = preserved_clones.get((descendants[0], original_take_id))
                        if clone is None:
                            raise ValueError(
                                "The original audio take cannot be preserved."
                            )
                        if descendants[0] in changed_context_clones:
                            # The cloned audio was generated with a different
                            # semantic window.  Leave it outside this run so
                            # its new version is synthesised with fresh 1/1
                            # (or wider) neighbour context.
                            clone.generation_run_id = None
                        else:
                            clone.generation_run_id = staged.id
                repair_attempts += 1
                progress(
                    index / len(groups),
                    f"Repairing timing: {index + 1} of {len(groups)} blocks "
                    f"(regenerating the split halves of block {segment.ordinal})",
                )
                generated = handler.run_generation(
                    {
                        "generation_run_id": staged_run_id,
                        "segment_ids": [],
                        "operation": "resume",
                    },
                    lambda _value, _detail=None: None,
                    stop,
                )
                if generated.get("status") != "completed" or stop.is_set():
                    if stop.is_set() or generated.get("status") in {
                        "paused",
                        "canceled",
                    }:
                        repair_status, repair_reason = "stopped", "generation_stopped"
                    break
                # A contextual neighbour is re-synthesised because its
                # before/after window changed, so its take may also change
                # length.  Shorter audio cannot worsen its original timing
                # slot.  A longer take is accepted only when a forward
                # placement proves it does not push later speech: window
                # slack alone proves nothing, because a fresh take may come
                # out longer regardless of the room it had.
                staged_groups, staged_takes = _load_groups(handler, staged_run_id)
                child_ids = set(proposal["affected_segment_ids"])
                replacement = [
                    item
                    for item in staged_groups
                    if any(child.id in child_ids for child in item.segments)
                ]
                if (
                    len(replacement) != 2
                    or replacement[1].start_ms != boundary.boundary_ms
                ):
                    raise ValueError(
                        "Repair did not preserve separate cue timing anchors."
                    )
                staged_neighbours: dict[int, TimingGroup] = {}
                following_growth: list[int] = []
                if refreshed_context_parents:
                    located = _refreshed_neighbour_groups(
                        groups,
                        current_ids,
                        refreshed_context_parents,
                        staged_groups,
                    )
                    staged_neighbours = {
                        block_index: staged_group
                        for block_index, staged_group in located.values()
                    }
                    grown = _grown_refreshed_neighbours(
                        handler, refreshed_context_parents, take_ids, staged_run_id
                    )
                    if grown - set(located):
                        # A grown neighbour that cannot be placed is one an
                        # earlier repair split away from the original block
                        # list. Its entry cursor is no longer trustworthy, so
                        # growth is rejected exactly as it was before.
                        raise _RepairNotApplied("context_neighbour_added_delay")
                    if grown:
                        grown_blocks = {
                            block_index
                            for old_id, (block_index, _group) in located.items()
                            if old_id in grown
                        }
                        preceding_blocks = sorted(
                            block_index
                            for block_index in grown_blocks
                            if block_index < index
                        )
                        if preceding_blocks:
                            # The recorded entry cursor was measured with the
                            # old takes. It stays trustworthy only while no
                            # earlier repair has replaced a take in this
                            # range; otherwise reject exactly as before.
                            refreshed_range = {
                                segment.id
                                for block_index in range(preceding_blocks[0], index)
                                for segment in groups[block_index].segments
                            }
                            if refreshed_range & refreshed_by_earlier_repairs:
                                raise _RepairNotApplied(
                                    "context_neighbour_added_delay"
                                )
                            new_cursor = _place_group_run(
                                groups,
                                preceding_blocks[0],
                                index - 1,
                                entry_cursors[preceding_blocks[0]],
                                audio_settings,
                                work,
                                stop,
                                overrides=staged_neighbours,
                            )
                            candidate_start = max(0, group.start_ms)
                            if max(new_cursor, candidate_start) > max(
                                entry_cursors[index], candidate_start
                            ):
                                raise _RepairNotApplied(
                                    "context_neighbour_added_delay"
                                )
                        following_growth = sorted(
                            block_index
                            for block_index in grown_blocks
                            if block_index > index
                        )
                first_diagnostics: dict[str, int] = {}
                child_cursor, first_duration, first_delay = advance_timing(
                    replacement[0],
                    cursor,
                    replacement[1].start_ms,
                    audio_settings,
                    work,
                    stop,
                    has_next_block=True,
                    diagnostics=first_diagnostics,
                )
                first_child_drift = max(0, child_cursor - replacement[1].start_ms)
                second_diagnostics: dict[str, int] = {}
                second_cursor_start = child_cursor
                child_cursor, second_duration, second_delay = advance_timing(
                    replacement[1],
                    child_cursor,
                    slot_end,
                    audio_settings,
                    work,
                    stop,
                    has_next_block=index + 1 < len(groups),
                    diagnostics=second_diagnostics,
                )
                if child_cursor > baseline_cursor:
                    # A repair must not create new delay for subsequent original blocks.
                    repair_status, repair_reason = "not_applied", "added_delay"
                    cursor = baseline_cursor
                    continue
                if following_growth:
                    # Walk the whole run from the block right after the
                    # candidate, not from the first grown one: an un-grown
                    # neighbour that already overruns its slot supplies drift
                    # the grown block must inherit, and starting later would
                    # hide it behind a roomier window.
                    new_cursor = _place_group_run(
                        groups,
                        index + 1,
                        following_growth[-1],
                        child_cursor,
                        audio_settings,
                        work,
                        stop,
                        overrides=staged_neighbours,
                    )
                    old_cursor = _place_group_run(
                        groups,
                        index + 1,
                        following_growth[-1],
                        baseline_cursor,
                        audio_settings,
                        work,
                        stop,
                    )
                    if new_cursor > old_cursor:
                        raise _RepairNotApplied("context_neighbour_added_delay")
                anchor_tolerance = min(
                    NATURAL_OVERRUN_TOLERANCE_MS,
                    int((replacement[1].start_ms - replacement[0].start_ms)
                        * NATURAL_OVERRUN_TOLERANCE_RATIO),
                )
                if first_child_drift > anchor_tolerance:
                    # Final catch-up is not enough: the new internal cue
                    # anchor must not itself be substantially missed.
                    repair_status, repair_reason = "not_applied", "missed_repair_anchor"
                    cursor = baseline_cursor
                    continue
                with handler.database.immediate_session() as session:
                    active = session.scalar(
                        select(GenerationPlan).where(
                            GenerationPlan.session_id == session_id
                        )
                    )
                    if (
                        stop.is_set()
                        or active.active_revision_id != current_revision_id
                        or plan_signature(session, current_revision_id)
                        != source_signature
                        or _selection_state(session, current_revision_id)[1]
                        != source_take_signature
                    ):
                        repair_status, repair_reason = (
                            ("stopped", "generation_stopped")
                            if stop.is_set()
                            else ("not_applied", "selection_changed")
                        )
                        break
                    active.active_revision_id = proposal["plan_revision_id"]
                    active.updated_at = utcnow()
                    mark_output_assemblies_stale(
                        session, session_id, cancel_active=True, jobs=handler.jobs
                    )
                    _record_repair_outcome(session, staged_revision_id, "applied")
                    record_accepted_repair(session, staged_revision_id)
                repair_status, repair_reason = "applied", None
                after_effect = late_cue_metrics(
                    [
                        PlacedSpeechBlock(
                            max(cursor, max(0, replacement[0].start_ms))
                            + first_delay,
                            max(cursor, max(0, replacement[0].start_ms))
                            + first_delay
                            + first_diagnostics.get("processed_audio_ms", first_duration),
                            replacement[0].cues,
                        ),
                        PlacedSpeechBlock(
                            max(second_cursor_start, max(0, replacement[1].start_ms))
                            + second_delay,
                            max(second_cursor_start, max(0, replacement[1].start_ms))
                            + second_delay
                            + second_diagnostics.get(
                                "processed_audio_ms", second_duration
                            ),
                            replacement[1].cues,
                        ),
                    ]
                )
                effect["late_cues_before"] += before_effect["late_cues"]
                effect["late_cues_after"] += after_effect["late_cues"]
                effect["worst_lag_ms_before"] = max(
                    effect["worst_lag_ms_before"], before_effect["worst_lag_ms"]
                )
                effect["worst_lag_ms_after"] = max(
                    effect["worst_lag_ms_after"], after_effect["worst_lag_ms"]
                )
                effect["blocks"] += 1
                # The refreshed neighbours now have longer or shorter audio in
                # this revision. Replace their measured paths so later
                # iterations place them from the takes the plan now holds.
                original_by_current = {
                    current: original for original, current in current_ids.items()
                }
                for block_index, staged_group in staged_neighbours.items():
                    if block_index < 0 or block_index >= len(groups):
                        continue
                    groups[block_index].paths = list(staged_group.paths)
                refreshed_by_earlier_repairs.update(
                    original_by_current.get(old_id, old_id)
                    for old_id in refreshed_context_parents.values()
                )
                # A later candidate measures the audio this pass's plan now
                # holds.  Unchanged neighbours reuse their cloned take, but a
                # refreshed neighbour has a new one, so its measured path must
                # follow; otherwise its grown duration would be invisible to
                # every following placement.
                original_by_current = {
                    current: original for original, current in current_ids.items()
                }
                refreshed_by_earlier_repairs.update(
                    original_by_current[old_id]
                    for old_id in refreshed_context_parents.values()
                    if old_id in original_by_current
                )
                _adopt_refreshed_takes(groups, staged_neighbours)
                current_ids = {
                    original: proposal["lineage"][current][0]
                    for original, current in current_ids.items()
                    if current in proposal["lineage"]
                }
                current_revision_id = proposal["plan_revision_id"]
                current_run_id = staged_run_id
                take_ids = staged_takes
                cursor = child_cursor
                repaired += 1
            except _RepairNotApplied as skipped:
                repair_status, repair_reason = "not_applied", skipped.reason
                cursor = baseline_cursor
                continue
            except Exception:
                if stop.is_set():
                    repair_status, repair_reason = "stopped", "generation_stopped"
                logger.warning(
                    "Optional voiceover repair stopped; the last selected plan and audio remain usable.",
                    exc_info=True,
                )
                if staged_run_id:
                    with handler.database.session() as session:
                        staged = session.get(GenerationRun, staged_run_id)
                        if staged is not None and staged.status not in {
                            "completed",
                            "canceled",
                        }:
                            staged.status = "failed"
                break
            finally:
                if repair_status != "applied":
                    rejection_reasons[repair_reason or repair_status] += 1
                if staged_revision_id and repair_status != "applied":
                    with handler.database.session() as session:
                        _record_repair_outcome(
                            session, staged_revision_id, repair_status, repair_reason
                        )
        else:
            pass_completed = not stop.is_set()
    return {
        "repair_checked_blocks": checked,
        "repair_candidate_blocks": repair_candidates,
        "repair_attempted_blocks": repair_attempts,
        "repair_rejection_reasons": dict(sorted(rejection_reasons.items())),
        "repaired_blocks": repaired,
        "repaired_generation_run_id": current_run_id,
        "repaired_plan_revision_id": current_revision_id,
        # A stopped or interrupted inspection is not evidence that the run has
        # nothing left to repair. Only a pass that reached every source block
        # may be reused as a completed result by the API.
        "repair_completed": pass_completed,
        "repair_inspection_failed": inspection_failed,
        "repair_planned_blocks": len(groups),
        # Late-cue evidence for the blocks this pass touched, measured on the
        # same placement data the pass itself judges. Present even when no
        # repair was applied, where blocks is zero.
        "repair_effect": dict(effect),
    }


@dataclass(frozen=True)
class _PreviewAccess:
    """The two capabilities group loading needs, without a worker handler."""

    database: Any
    paths: Any


def _preview_envelope(
    run_id: str,
    session_id: str,
    revision_id: str,
    thresholds: dict[str, int],
    *,
    skipped_reason: str | None = None,
) -> dict[str, Any]:
    return {
        "preview": True,
        "generation_run_id": run_id,
        "session_id": session_id,
        "speech_plan_revision_id": revision_id,
        "thresholds": thresholds,
        "skipped_reason": skipped_reason,
        "planned_blocks": 0,
        "checked_count": 0,
        "candidate_count": 0,
        "prechecked_count": 0,
        "estimated_synthesis_calls": 0,
        "candidates": [],
    }


def preview_early_blocks(
    database: Any,
    paths: Any,
    run_id: str,
    *,
    thresholds: dict[str, int] | None = None,
    cancel_event: Any = None,
) -> dict[str, Any]:
    """List the blocks an explicit timing repair would attempt, without writing.

    Candidate selection is the pass's own: the same take grouping, the same
    forward placement measurement and the same boundary function, with the
    requested thresholds instead of the frozen ones.  Nothing is staged,
    queued or synthesised, and no job or plan row changes.

    The call count follows the pass: two takes for the halves, plus one for
    each neighbouring block whose window a split can refresh.  That neighbour
    count is an upper bound, because the pass re-synthesises only the
    neighbours whose frozen before/after text actually changed.  Candidates
    the neighbour growth pre-check would skip are reported with that reason and
    count no calls.
    """

    from .generation_performance_snapshot import performance_runtime_settings
    from .settings_policy import repair_thresholds as _resolve_thresholds

    requested = _coerced_thresholds(thresholds) if thresholds is not None else None
    with database.session() as session:
        source_run = session.get(GenerationRun, run_id)
        if source_run is None:
            raise KeyError(run_id)
        snapshot = deepcopy(source_run.settings_snapshot_json or {})
        source_revision_id = source_run.plan_revision_id
        session_id = source_run.session_id
        tts_settings = performance_runtime_settings(snapshot)
        resolved_thresholds = requested or _resolve_thresholds(tts_settings)
        if source_run.audio_version_json is not None:
            return _preview_envelope(
                run_id,
                session_id,
                source_revision_id,
                resolved_thresholds,
                skipped_reason="audio_version",
            )
        plan = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        if plan is None or plan.active_revision_id != source_revision_id:
            return _preview_envelope(
                run_id,
                session_id,
                source_revision_id,
                resolved_thresholds,
                skipped_reason="active_plan_changed",
            )
    loader = _PreviewAccess(database=database, paths=paths)
    groups, take_ids = _load_groups(loader, run_id)
    if not groups:
        return _preview_envelope(
            run_id,
            session_id,
            source_revision_id,
            resolved_thresholds,
            skipped_reason="no_speech_blocks",
        )
    if not _source_context_available(snapshot, source_revision_id, take_ids):
        return _preview_envelope(
            run_id,
            session_id,
            source_revision_id,
            resolved_thresholds,
            skipped_reason="missing_source_semantic_context",
        )
    audio_settings = dict(snapshot.get("audio") or {})
    context_mode = str(tts_settings.get("tts_context_mode") or "off")
    gap_ms = sentence_gap_ms_from_settings(audio_settings)
    event = cancel_event if cancel_event is not None else threading.Event()
    checked = 0
    cursor = 0
    estimated_calls = 0
    candidates: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="voiceover-repair-preview-") as directory:
        work = Path(directory)
        with database.session() as session:
            for index, group in enumerate(groups):
                if event.is_set():
                    break
                inspection = _inspect_group(
                    group,
                    index,
                    groups,
                    cursor,
                    audio_settings,
                    tts_settings,
                    resolved_thresholds,
                    work,
                    event,
                )
                checked += 1
                boundary = inspection.boundary
                if boundary is None:
                    cursor = inspection.baseline_cursor
                    continue
                skipped = neighbour_growth_precheck(
                    estimated_advance_ms=boundary.estimated_advance_ms,
                    sentence_gap_ms=gap_ms,
                )
                neighbours = (
                    _context_neighbour_count(groups, index, tts_settings)
                    if context_mode != "off"
                    else 0
                )
                calls = 0 if skipped else 2 + neighbours
                estimated_calls += calls
                candidates.append(
                    {
                        "ordinal": group.segments[0].ordinal,
                        "segment_id": group.segments[0].id,
                        "shortfall_ms": max(
                            0,
                            (group.end_ms - group.start_ms)
                            - inspection.original_audio_ms
                            - inspection.start_delay_ms,
                        ),
                        "proposed_split": {
                            "start_ms": boundary.start_ms,
                            "boundary_ms": boundary.boundary_ms,
                            "end_ms": boundary.end_ms,
                            "estimated_advance_ms": boundary.estimated_advance_ms,
                        },
                        "estimated_synthesis_calls": calls,
                        "skipped_reason": skipped,
                    }
                )
                cursor = inspection.baseline_cursor
    return {
        "preview": True,
        "generation_run_id": run_id,
        "session_id": session_id,
        "speech_plan_revision_id": source_revision_id,
        "thresholds": resolved_thresholds,
        "skipped_reason": None,
        "planned_blocks": len(groups),
        "checked_count": checked,
        "candidate_count": len(candidates),
        "prechecked_count": sum(
            1 for item in candidates if item["skipped_reason"] == _NEIGHBOUR_GROWTH_PRECHECK
        ),
        "estimated_synthesis_calls": estimated_calls,
        "candidates": candidates,
    }
