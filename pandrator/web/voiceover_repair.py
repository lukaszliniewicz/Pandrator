"""Optional cue-boundary refinement after a complete voiceover generation run.

Replacements are generated against an inactive immutable revision. Selection
changes only after synthesis and forward-timing validation both succeed.
"""

from __future__ import annotations

import logging
import tempfile
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.audio_sync import (
    _speed_up_wav_streaming,
    _streaming_audio_duration_ms,
    alignment_adjustment,
)
from pandrator.logic.dubbing.early_repair import find_repair_boundary

from .audio_assembly import (
    AudioAssemblyPart,
    assemble_audio_plan,
    build_audio_assembly_plan,
    preferred_pcm_format,
)
from .generation_audio_identity import plan_audio_identities
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
from .speech_plan_workspace import freeze_speech_snapshot, plan_signature
from .workspace import (
    GenerationService,
    WorkspaceSettingsService,
    mark_output_assemblies_stale,
    stable_hash,
)

logger = logging.getLogger(__name__)


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
) -> tuple[int, int, int]:
    """Measure forward placement, including FFmpeg rounding, without a full mix.

    This preview uses the fitter's shared adjustment and tempo implementation.
    It retains the next-start catch-up window and the own-cue slowdown window.
    """
    sample_rate, channels = preferred_pcm_format(
        group.paths[0], cancel_event=cancel_event
    )
    gap = max(0, min(5000, int(settings.get("synchronization_sentence_gap_ms") or 100)))
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
    speed = float(settings.get("synchronization_speed") or 1.0)
    maximum = min(4.0, max(1.0, round(speed * 100 if speed <= 10 else speed) / 100))
    adjustment = alignment_adjustment(
        duration,
        slot_end - start,
        cursor - start,
        delay_start_ms=max(0, int(settings.get("synchronization_delay_ms") or 0)),
        max_speed_factor=maximum,
        allow_slowdown=bool(settings.get("synchronization_slowdown_enabled", False)),
        speech_window_duration_ms=max(1, group.end_ms - start),
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
                    adjustment.available_ms, group.end_ms - start
                ):
                    processed = duration
                break
            if processed <= adjustment.available_ms or factor >= maximum - 0.0001:
                break
            factor = min(
                maximum, factor * processed / max(1, adjustment.available_ms - 1)
            )
    return (
        max(slot_end, cursor + adjustment.start_delay_ms + processed),
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


def repair_early_blocks(
    handler: Any, run_id: str, progress: Any, cancel_event: Any
) -> dict[str, Any]:
    """One pass over original blocks; never recursively repair new children."""
    with handler.database.session() as session:
        source_run = session.get(GenerationRun, run_id)
        snapshot = deepcopy(source_run.settings_snapshot_json or {})
        source_revision_id = source_run.plan_revision_id
        session_id = source_run.session_id
        active = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        if active is None or active.active_revision_id != source_revision_id:
            return {"repaired_blocks": 0}
    groups, take_ids = _load_groups(handler, run_id)
    if not groups:
        return {"repaired_blocks": 0}
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
    cursor = 0
    audio_settings = dict(snapshot.get("audio") or {})
    tts_settings = dict(snapshot.get("tts") or {})
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
        for index, group in enumerate(groups):
            if stop.is_set():
                break
            progress(
                index / len(groups),
                f"Checking voiceover timing {index + 1} of {len(groups)}",
            )
            slot_end = (
                groups[index + 1].start_ms if index + 1 < len(groups) else group.end_ms
            )
            try:
                baseline_cursor, duration, start_delay = advance_timing(
                    group, cursor, slot_end, audio_settings, work, stop
                )
            except Exception:
                logger.warning(
                    "Voiceover timing inspection stopped; selected audio is unchanged.",
                    exc_info=True,
                )
                break
            segment = group.segments[0]
            boundary = None
            if len(group.segments) == 1:
                boundary = find_repair_boundary(
                    segment.text,
                    segment.optimized_text or segment.text,
                    dict(segment.speech_block_provenance_json or {}),
                    duration,
                    incoming_delay_ms=max(0, cursor - group.start_ms),
                    start_delay_ms=start_delay,
                    **thresholds,
                )
            if boundary is None or (boundary.start_ms, boundary.end_ms) != (
                group.start_ms,
                group.end_ms,
            ):
                cursor = baseline_cursor
                continue
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
                        # audio used to measure the proposed repair.
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
                    child_snapshot["generation_audio_identities"] = (
                        plan_audio_identities(
                            session, proposal["plan_revision_id"], child_snapshot
                        )
                    )
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
                        job_id=source_run.job_id,
                        sequence_number=sequence,
                        operation="generate",
                        status="queued",
                        settings_snapshot_json=child_snapshot,
                        settings_hash=stable_hash(child_snapshot),
                    )
                    session.add(staged)
                    session.flush()
                    staged_run_id = staged.id
                    for old_id, descendants in proposal["lineage"].items():
                        if len(descendants) != 1:
                            continue
                        original_take_id = take_ids.get(old_id)
                        if original_take_id is None:
                            raise ValueError(
                                "The original audio selection changed during repair."
                            )
                        clone = session.scalar(
                            select(AudioTake).where(
                                AudioTake.generation_segment_id == descendants[0],
                                AudioTake.parent_take_id == original_take_id,
                            )
                        )
                        if clone is None:
                            raise ValueError(
                                "The original audio take cannot be preserved."
                            )
                        clone.generation_run_id = staged.id
                    source_signature = plan_signature(session, current_revision_id)
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
                )
                child_cursor, _, _ = advance_timing(
                    replacement[1], child_cursor, slot_end, audio_settings, work, stop
                )
                if child_cursor > baseline_cursor:
                    # A repair must not create new delay for subsequent original blocks.
                    repair_status, repair_reason = "not_applied", "added_delay"
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
                if staged_revision_id and repair_status != "applied":
                    with handler.database.session() as session:
                        _record_repair_outcome(
                            session, staged_revision_id, repair_status, repair_reason
                        )
    return {
        "repaired_blocks": repaired,
        "repaired_generation_run_id": current_run_id,
        "repaired_plan_revision_id": current_revision_id,
    }
