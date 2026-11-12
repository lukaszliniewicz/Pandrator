"""Optional cue-boundary refinement after a complete voiceover generation run.

Replacements are generated against an inactive immutable revision. Selection
changes only after synthesis and forward-timing validation both succeed.
"""

from __future__ import annotations

import logging
import tempfile
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.audio_sync import (
    NATURAL_OVERRUN_TOLERANCE_MS,
    NATURAL_OVERRUN_TOLERANCE_RATIO,
    _speed_up_wav_streaming,
    _streaming_audio_duration_ms,
    alignment_adjustment,
    min_block_gap_ms_from_settings,
    sentence_gap_ms_from_settings,
    slowdown_enabled_from_settings,
)
from pandrator.logic.dubbing.early_repair import find_repair_boundary

from .audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    preferred_pcm_format,
)
from .generation_audio_identity import plan_audio_identities
from .generation_start_preparation import snapshot_guard
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
from .settings_policy import stable_hash
from .speech_plan_workspace import freeze_speech_snapshot, plan_signature
from .workspace import GenerationService, mark_output_assemblies_stale
from .workspace_settings import WorkspaceSettingsService

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
            else:
                groups.append(
                    TimingGroup(
                        [segment],
                        [path],
                        refs[0][0],
                        refs[-1][1],
                        [ref[2] for ref in refs],
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
    }



def repair_early_blocks(
    handler: Any,
    run_id: str,
    progress: Any,
    cancel_event: Any,
    *,
    stage_job_id: str | None = None,
) -> dict[str, Any]:
    """One pass over original blocks; never recursively repair new children."""
    from .repair_batches import GUARD_KEY, capture_repair_base, record_accepted_repair

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
    service = GenerationService(
        handler.database, handler.jobs, WorkspaceSettingsService(handler.database)
    )
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
    cursor = 0
    from .generation_performance_snapshot import performance_runtime_settings

    audio_settings = dict(snapshot.get("audio") or {})
    # Resolve semantic context and repair parameters exactly as the frozen
    # synthesis snapshot does, including the selected segment override.
    tts_settings = performance_runtime_settings(snapshot)
    if str(tts_settings.get("tts_context_mode") or "off") != "off":
        from .generation_performance_snapshot import frozen_semantic_contexts

        frozen = snapshot.get("semantic_context_snapshot") or {}
        try:
            context_units = frozen_semantic_contexts(snapshot)
        except (ValueError, TypeError, KeyError):
            # A future/unknown schema is not evidence of the original TTS
            # neighbours. Optional repair must refuse it without failing
            # the completed source generation job.
            context_units = {}
        # Older or interrupted voiceovers may predate immutable semantic
        # windows. Reconstructing their context from today's settings would
        # not prove that existing audio was spoken with that same context.
        # Fail closed, leaving every original take intact, rather than
        # invalidating all neighbours and synthesising the whole lecture.
        if (
            frozen.get("plan_revision_id") != source_revision_id
            or not set(take_ids).issubset(context_units)
        ):
            return _skipped_repair_result(
                "missing_source_semantic_context", run_id, source_revision_id
            )
    thresholds = {
        name: tts_settings.get(f"speech_block_early_repair_{name}", default)
        for name, default in (
            ("min_shortfall_ms", 1000),
            ("min_shortfall_percent", 20),
            ("min_advance_ms", 1000),
            ("min_child_span_ms", 1000),
        )
    }
    with tempfile.TemporaryDirectory(prefix="voiceover-repair-") as directory:
        work = Path(directory)
        checked = 0
        inspection_failed = False
        pass_completed = False
        for index, group in enumerate(groups):
            if stop.is_set():
                rejection_reasons["generation_stopped"] += 1
                break
            progress(
                index / len(groups),
                f"Checking voiceover timing {index + 1} of {len(groups)}",
            )
            slot_end = (
                groups[index + 1].start_ms if index + 1 < len(groups) else group.end_ms
            )
            timing_details: dict[str, int] = {}
            try:
                baseline_cursor, duration, start_delay = advance_timing(
                    group, cursor, slot_end, audio_settings, work, stop,
                    has_next_block=index + 1 < len(groups),
                    diagnostics=timing_details,
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
            boundary = None
            provenance = dict(segment.speech_block_provenance_json or {})
            if (
                len(group.segments) == 1
                and not {
                    "estimated_internal_timing", "shared_passage_timing"
                }.intersection(provenance.get("risk_flags") or [])
            ):
                boundary = find_repair_boundary(
                    segment.text,
                    segment.optimized_text or segment.text,
                    provenance,
                    # Judge the rendered take after gentle tempo adjustment,
                    # not the shorter unprocessed source audio.
                    timing_details.get("processed_audio_ms", duration),
                    incoming_delay_ms=max(0, cursor - group.start_ms),
                    start_delay_ms=start_delay,
                    language_code=str(
                        segment.language or tts_settings.get("language") or "en"
                    ),
                    **thresholds,
                )
            if boundary is None or (boundary.start_ms, boundary.end_ms) != (
                group.start_ms,
                group.end_ms,
            ):
                cursor = baseline_cursor
                continue
            repair_candidates += 1
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
                            "reason": "early_timing_repair",
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
                    child_snapshot["early_repair_parent_run_id"] = run_id
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
                    if (
                        _selection_state(session, current_revision_id)
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
                    f"Regenerating two cue groups for block {index + 1}",
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
                # before/after window changed.  It may also change duration.
                # Shorter audio cannot worsen its original timing slot;
                # reject the entire optional repair if any such neighbour
                # grows, rather than adopt an unverified downstream delay.
                # Raw take duration is deliberately used as a fail-closed
                # bound: the neighbour's true allowed slack depends on its
                # incoming cursor, the whole group's take totals and any tempo
                # adjustment.  Measuring that would require another forward
                # placement pass per neighbour, so any growth stays rejected
                # even if the cue window might absorb it.
                # This conservative comparison preserves the original
                # complete run and all selected takes on rejection.
                if refreshed_context_parents:
                    with handler.database.session() as session:
                        for descendant_id, old_id in refreshed_context_parents.items():
                            original_take = session.get(AudioTake, take_ids[old_id])
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
                                raise _RepairNotApplied(
                                    "context_neighbour_unmeasurable"
                                )
                            if refreshed_take.duration_ms > original_take.duration_ms:
                                repair_status, repair_reason = (
                                    "not_applied", "context_neighbour_added_duration"
                                )
                                break
                    if repair_status == "not_applied":
                        cursor = baseline_cursor
                        continue
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
                child_cursor, _, _ = advance_timing(
                    replacement[0],
                    cursor,
                    replacement[1].start_ms,
                    audio_settings,
                    work,
                    stop,
                    has_next_block=True,
                )
                first_child_drift = max(0, child_cursor - replacement[1].start_ms)
                child_cursor, _, _ = advance_timing(
                    replacement[1],
                    child_cursor,
                    slot_end,
                    audio_settings,
                    work,
                    stop,
                    has_next_block=index + 1 < len(groups),
                )
                if child_cursor > baseline_cursor:
                    # A repair must not create new delay for subsequent original blocks.
                    repair_status, repair_reason = "not_applied", "added_delay"
                    cursor = baseline_cursor
                    continue
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
    }
