"""Optional second-pass regroup after a complete passage-first voiceover run.

The first pass generates every timed passage independently. When
``speech_block_regroup_enabled`` is true (OFF by default), this pass selects
disjoint adjacent-passage groups solely from first-pass measurements and
regenerates each selected group exactly once with the SAME TTS provider/model
(the staged run reuses the frozen first-pass snapshot, so no new model and no
word alignment are involved). The individual first-pass takes are never
deleted and stay the safe fallback: any unsuitable or failed group retains
its originals.

Only passage-mode voiceover runs are eligible here. Legacy mode keeps the old
early split repair instead; the two passes never run together, so a
split/regenerate loop is impossible. Regenerated groups are never regrouped
again (staged snapshots carry ``regroup_parent_run_id``, and selection runs
once on first-pass evidence).

Original passage IDs, boundaries, and takes remain inspectable through the
immutable plan history: every accepted group is one (or, for triples, two)
ordinary ``merge`` topology revision(s) with reason ``passage_regroup``,
revertible with a restore. Unactivated staged revisions and runs are left in
place on fallback, exactly like the early-repair path.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pandrator.logic.dubbing.passage_regroup import (
    RegroupPassage,
    normalize_regroup_settings,
    output_cut_positions_ms,
    regenerated_group_fits,
    select_regroup_candidates,
)

from .generation_audio_identity import plan_audio_identities
from .models import (
    Artifact,
    AudioTake,
    GenerationPlan,
    GenerationPlanRevision,
    GenerationRun,
    GenerationSegment,
    MediaEditPlan,
    MediaEditPlanRevision,
    Segment,
    utcnow,
)
from .speech_plan_workspace import freeze_speech_snapshot, plan_signature
from .voiceover_repair import (
    RepairCancellation,
    _record_repair_outcome,
    _selection_state,
)
from .workspace import (
    GenerationService,
    RevisionConflict,
    mark_output_assemblies_stale,
    stable_hash,
)

logger = logging.getLogger(__name__)

REPAIR_REASON = "passage_regroup"

_MANUAL_EVENT_ACTIONS = {"manual_split", "manual_merge"}
_MANUAL_REASON_PREFIX = "manual_topology"


@dataclass
class _FirstPassState:
    passages: list[RegroupPassage]
    take_ids: dict[str, str]


def _is_user_locked(segment: GenerationSegment, risk_flags: list[str]) -> bool:
    if segment.marked:
        return True
    if int(segment.silence_after_ms or 0) != 0:
        return True
    speech_plan = dict(segment.speech_plan_json or {})
    if str(speech_plan.get("status") or "") == "manual_topology":
        return True
    provenance = dict(segment.speech_block_provenance_json or {})
    if str(provenance.get("origin") or "") == "manual":
        return True
    for event in provenance.get("formation_events") or []:
        if not isinstance(event, dict):
            continue
        if event.get("action") in _MANUAL_EVENT_ACTIONS:
            return True
        if str(event.get("reason_code") or "").startswith(_MANUAL_REASON_PREFIX):
            return True
    return "timing_forced_fragment" in set(risk_flags or [])


def _load_first_pass(handler: Any, run_id: str) -> _FirstPassState | None:
    """Load first-pass evidence with the same strictness as assembly.

    Any ambiguity (missing take, unresolvable timing, non-voiceover node
    kind) yields ``None``: regroup then silently does nothing and the
    first-pass audio stays selected.
    """

    with handler.database.session() as session:
        run = session.get(GenerationRun, run_id)
        if run is None:
            return None
        revision = session.get(GenerationPlanRevision, run.plan_revision_id)
        if revision is None or revision.source_revision_id is None:
            return None
        timings: dict[str, tuple[int, int]] = {}
        for cue in session.scalars(
            select(Segment).where(Segment.revision_id == revision.source_revision_id)
        ):
            if cue.start_ms is not None and cue.end_ms is not None:
                timings[str(cue.id)] = (int(cue.start_ms), int(cue.end_ms))
                timings[str(cue.ordinal + 1)] = (int(cue.start_ms), int(cue.end_ms))
        if not timings:
            return None
        segments = list(
            session.scalars(
                select(GenerationSegment)
                .where(
                    GenerationSegment.plan_revision_id == run.plan_revision_id,
                    GenerationSegment.removed.is_(False),
                )
                .order_by(GenerationSegment.ordinal)
            ).all()
        )
        if not segments:
            return None
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

        by_segment: dict[str, tuple[GenerationSegment, AudioTake]] = {}
        for segment, take, _artifact in rows:
            by_segment.setdefault(segment.id, (segment, take))
        if set(by_segment) != {segment.id for segment in segments}:
            return None

        passages: list[RegroupPassage] = []
        take_ids: dict[str, str] = {}
        for segment in segments:
            if segment.node_kind != "subtitle_cue":
                return None
            _segment, take = by_segment[segment.id]
            refs = [str(ref) for ref in segment.source_segment_ids_json or []]
            windows = [timings[ref] for ref in refs if ref in timings]
            if not refs or len(windows) != len(refs):
                return None
            start_ms = min(window[0] for window in windows)
            end_ms = max(window[1] for window in windows)
            if end_ms <= start_ms:
                return None
            if take.duration_ms is None or int(take.duration_ms) < 0:
                return None
            provenance = dict(segment.speech_block_provenance_json or {})
            risk_flags = [str(flag) for flag in provenance.get("risk_flags") or []]
            speech_text = (segment.optimized_text or segment.text or "").strip()
            passages.append(
                RegroupPassage(
                    key=segment.id,
                    ordinal=int(segment.ordinal),
                    speaker=str(segment.speaker or "").strip(),
                    voice=str(segment.voice or "").strip(),
                    voice_id=str(segment.voice_id or ""),
                    language=str(segment.language or "").strip(),
                    start_ms=start_ms,
                    end_ms=end_ms,
                    take_duration_ms=int(take.duration_ms),
                    display_chars=len((segment.text or "").strip()),
                    speech_chars=len(speech_text),
                    risk_flags=tuple(risk_flags),
                    user_locked=_is_user_locked(segment, risk_flags),
                    boundary_after_cut=bool(segment.paragraph_break_after),
                    source_refs=tuple(segment.source_segment_ids_json or []),
                )
            )
            take_ids[segment.id] = take.id
        return _FirstPassState(passages=passages, take_ids=take_ids)


def _load_source_cuts(
    handler: Any, session_id: str, plan_revision_id: str
) -> tuple[list[int] | None, bool]:
    """Resolve true edit-cut positions in the CURRENT output timeline.

    Bounded lineage only (no broad scans): reads the plan revision's stored
    ``_source_artifact_id``, follows that artifact's exact subtitle
    provenance chain, and loads at most one ``MediaEditPlanRevision`` row
    for the single immutable edit identity. Returns ``(cuts, edited_known)``
    where ``cuts`` is the output-timeline cut list (possibly empty when the
    edit kept one contiguous range) and ``None`` when the mapping is
    missing or uncertain; ``edited_known`` reports cut-derived lineage.
    Ordinary unedited inputs yield ``(None, False)`` and stay eligible.
    """

    from .subtitle_media import _lineage_artifacts, _media_identity

    with handler.database.session() as session:
        revision = session.get(GenerationPlanRevision, plan_revision_id)
        settings = dict((revision.settings_json if revision else {}) or {})
        source_id = str(settings.get("_source_artifact_id") or "").strip()
        if not source_id:
            return None, False
        source = session.get(Artifact, source_id)
        if source is None:
            return None, False
        try:
            cut_ancestors, _explicit_media = _lineage_artifacts(
                session, session_id, source
            )
        except ValueError:
            # Provenance chain broken (deleted/unattached parent): only
            # conservatively block when the source itself is cut-derived.
            if source.role == "media_edit_subtitles":
                return None, True
            return None, False
        if not cut_ancestors:
            return None, False
        identities = {
            identity
            for artifact in cut_ancestors
            if (identity := _media_identity(artifact)) is not None
        }
        if (
            any(_media_identity(artifact) is None for artifact in cut_ancestors)
            or len(identities) != 1
        ):
            return None, True
        edit_revision_id, _content_hash = next(iter(identities))
        edit_revision = session.get(MediaEditPlanRevision, edit_revision_id)
        if edit_revision is None:
            return None, True
        plan = session.get(MediaEditPlan, edit_revision.plan_id)
        if plan is None or plan.session_id != session_id:
            return None, True
        ranges: list[tuple[int, int]] = []
        for item in edit_revision.keep_ranges_json or []:
            if not isinstance(item, dict):
                return None, True
            start_raw, end_raw = item.get("start_ms"), item.get("end_ms")
            if (
                isinstance(start_raw, bool)
                or isinstance(end_raw, bool)
                or not isinstance(start_raw, int)
                or not isinstance(end_raw, int)
                or end_raw <= start_raw
            ):
                return None, True
            ranges.append((start_raw, end_raw))
        if not ranges:
            return None, True
        return output_cut_positions_ms(ranges), True


def _stage_group_revision(
    session: Session,
    session_id: str,
    current_revision_id: str,
    members: list[GenerationSegment],
    *,
    group_index: int,
    run_id: str,
    batch_snapshot: dict[str, Any],
    max_chars: int,
) -> tuple[str, str, dict[str, list[str]]]:
    """Stage one inactive revision merging a whole candidate group at once.

    This mirrors the pairwise ``merge`` topology semantics (combined text,
    provenance folding, take preservation for untouched segments) but folds
    up to eight passages in a single revision, so every group needs exactly
    one generation. The revision stays inactive until regenerated audio and
    forward-timing validation both succeed.
    """

    from .repair_batches import GUARD_KEY

    if len(members) < 2:
        raise ValueError("A regroup group needs at least two passages.")
    plan = session.scalar(
        select(GenerationPlan).where(GenerationPlan.session_id == session_id)
    )
    if plan is None or str(plan.active_revision_id) != current_revision_id:
        raise RevisionConflict("The generation plan changed in another client.")
    current = session.get(GenerationPlanRevision, current_revision_id)
    if current is None:
        raise KeyError(current_revision_id)
    current_segments = list(
        session.scalars(
            select(GenerationSegment)
            .where(GenerationSegment.plan_revision_id == current.id)
            .order_by(GenerationSegment.ordinal)
        ).all()
    )
    member_ids = [member.id for member in members]
    by_id = {segment.id: segment for segment in current_segments}
    resolved = [by_id.get(member_id) for member_id in member_ids]
    if any(item is None or item.removed for item in resolved):
        raise ValueError("The regroup candidates changed during staging.")
    members = [item for item in resolved if item is not None]
    ordinals = [int(member.ordinal) for member in members]
    if ordinals != list(range(ordinals[0], ordinals[0] + len(members))):
        raise ValueError("Regroup candidates must stay adjacent.")

    texts = [(member.text or "").strip() for member in members]
    speeches = [
        ((member.optimized_text or member.text) or "").strip() for member in members
    ]
    if any(not text for text in texts) or any(not text for text in speeches):
        raise ValueError("Regroup candidates must have usable text.")
    merged_text = " ".join(texts)
    merged_speech = " ".join(speeches)
    if len(merged_text) > max_chars or len(merged_speech) > max_chars:
        raise ValueError("Merged speech text exceeds speech_block_max_chars.")

    provenance = dict(members[0].speech_block_provenance_json or {})
    display_len = len(texts[0])
    speech_len = len(speeches[0])
    for position, member in enumerate(members[1:], start=1):
        member_provenance = dict(member.speech_block_provenance_json or {})
        references = list(
            dict.fromkeys(
                [
                    *GenerationService._provenance_source_refs(provenance),
                    *GenerationService._provenance_source_refs(member_provenance),
                ]
            )
        )
        event = {
            "action": "automatic_merge",
            "reason_code": "passage_regroup_merge",
            "summary": "Adjacent voiceover passages regenerated as one group.",
            "measurements": {
                "group_index": group_index,
                "member_position": position,
                "member_count": len(members),
                "speech_length": len(merged_speech),
                "max_chars": max_chars,
                "left_segment_id": members[position - 1].id,
                "right_segment_id": member.id,
            },
            "source_references": references,
        }
        provenance = GenerationService._merge_provenance(
            provenance,
            member_provenance,
            display_offset=display_len + 1,
            speech_offset=speech_len + 1,
            event=event,
        )
        display_len += 1 + len(texts[position])
        speech_len += 1 + len(speeches[position])
    provenance["manual_topology"] = {
        "operation": "merge",
        "parent_segment_ids": list(member_ids),
        "parent_revision_ids": [current.id] * len(members),
        "parent_speech_plan_ids": [
            *[
                plan_id
                for member in members
                for plan_id in GenerationService._speech_plan_ids(
                    member.speech_plan_json
                )
            ]
        ],
    }

    merged_values = GenerationService._segment_copy_values(members[0])
    merged_values.update(
        {
            "text": merged_text,
            "optimized_text": (
                merged_speech
                if any(member.optimized_text for member in members)
                else None
            ),
            "source_segment_ids_json": list(
                dict.fromkeys(
                    [
                        ref
                        for member in members
                        for ref in (member.source_segment_ids_json or [])
                    ]
                )
            ),
            "speech_block_provenance_json": provenance,
            "speech_plan_json": {
                "version": 1,
                "status": "manual_topology",
                "parent_segment_ids": list(member_ids),
                "parent_revision_ids": [current.id] * len(members),
                "removed_boundary_before": deepcopy(
                    (members[-1].speech_block_provenance_json or {}).get(
                        "boundary_before"
                    )
                ),
            },
            "speaker": members[0].speaker,
            "marked": any(member.marked for member in members),
            "status": "stale",
            "optimization_status": "stale",
            "optimization_source_hash": None,
            "optimization_reviewed": False,
            "optimization_model": None,
            "silence_after_ms": members[-1].silence_after_ms,
            "paragraph_break_after": members[-1].paragraph_break_after,
        }
    )

    member_set = set(member_ids)
    values_sequence: list[dict[str, Any]] = []
    for segment in current_segments:
        if segment.id in member_set:
            if segment.id == member_ids[0]:
                values_sequence.append(merged_values)
            continue
        values_sequence.append(GenerationService._segment_copy_values(segment))
    GenerationService._recompute_alignment_group_values(values_sequence)

    operation_json: dict[str, Any] = {
        "action": "merge",
        "expected_revision_id": current_revision_id,
        "reason": REPAIR_REASON,
        GUARD_KEY: deepcopy(batch_snapshot),
        "source_block_ordinal": group_index,
        "source_generation_run_id": run_id,
        "repair_status": "pending",
        # Per-member traceability: every original passage ID maps to the one
        # merged segment, so a trio (or pair) stays fully inspectable and
        # revertible through the immutable history.
        "mapping": [
            {"source_segment_id": member_id, "new_segments": ["merged"]}
            for member_id in member_ids
        ],
    }
    revision_number = (
        int(
            session.scalar(
                select(func.max(GenerationPlanRevision.revision_number)).where(
                    GenerationPlanRevision.plan_id == plan.id
                )
            )
            or 0
        )
        + 1
    )
    revision = GenerationPlanRevision(
        plan_id=plan.id,
        parent_revision_id=current.id,
        source_revision_id=current.source_revision_id,
        revision_number=revision_number,
        settings_json=deepcopy(current.settings_json or {}),
        operation_json=deepcopy(operation_json),
        content_hash=stable_hash(
            {
                "parent_revision_id": current.id,
                "operation": operation_json,
                "segments": values_sequence,
            }
        ),
    )
    session.add(revision)
    session.flush()
    new_segments: list[GenerationSegment] = []
    for ordinal, values in enumerate(values_sequence):
        persisted = deepcopy(values)
        persisted["ordinal"] = ordinal
        new_segment = GenerationSegment(plan_revision_id=revision.id, **persisted)
        if new_segment.status == "running":
            new_segment.status = "ready"
        session.add(new_segment)
        new_segments.append(new_segment)
    session.flush()
    GenerationService._recompute_alignment_groups(new_segments)
    session.flush()

    first_position = next(
        index
        for index, segment in enumerate(current_segments)
        if segment.id == member_ids[0]
    )
    merged_segment = new_segments[first_position]
    lineage: dict[str, list[str]] = {}
    take_pairs: list[tuple[str, str]] = []
    cursor = 0
    for new_segment in new_segments:
        if new_segment.id == merged_segment.id:
            for member_id in member_ids:
                lineage[member_id] = [merged_segment.id]
            cursor += len(member_ids)
        else:
            source = current_segments[cursor]
            lineage[source.id] = [new_segment.id]
            take_pairs.append((source.id, new_segment.id))
            cursor += 1
    GenerationService._clone_available_takes_batch(session, take_pairs)

    operation_json["lineage"] = lineage
    revision.operation_json = deepcopy(operation_json)
    revision.content_hash = stable_hash(
        {
            "parent_revision_id": current.id,
            "operation": operation_json,
            "segments": values_sequence,
        }
    )
    session.flush()
    return revision.id, merged_segment.id, lineage


def repair_regroup_blocks(
    handler: Any, run_id: str, progress: Any, cancel_event: Any
) -> dict[str, Any]:
    """One regroup pass over first-pass groups; never regroups regenerated audio."""

    from .repair_batches import capture_repair_base, record_accepted_repair

    with handler.database.session() as session:
        source_run = session.get(GenerationRun, run_id)
        if source_run is None:
            return {"regrouped_groups": 0}
        snapshot = deepcopy(source_run.settings_snapshot_json or {})
        source_revision_id = source_run.plan_revision_id
        session_id = source_run.session_id
        source_job_id = source_run.job_id
        active = session.scalar(
            select(GenerationPlan).where(GenerationPlan.session_id == session_id)
        )
        if active is None or active.active_revision_id != source_revision_id:
            return {"regrouped_groups": 0}
        already_attempted = session.scalar(
            select(GenerationRun.id)
            .where(
                GenerationRun.session_id == session_id,
                GenerationRun.settings_snapshot_json[
                    "regroup_parent_run_id"
                ].as_string()
                == run_id,
            )
            .limit(1)
        )
        if already_attempted is not None:
            # Replay safety: re-running (or resuming) this pass must never
            # stage duplicate revisions for the same first-pass run. The
            # original takes stay selected; inspect history for the prior
            # attempt instead.
            logger.info(
                "Voiceover regroup already attempted for run %s; not staging again.",
                run_id,
            )
            return {"regrouped_groups": 0, "regroup_status": "already_attempted"}
        batch_snapshot = capture_repair_base(session, source_revision_id)

    tts_settings = dict(snapshot.get("tts") or {})
    try:
        regroup = normalize_regroup_settings(tts_settings)
    except ValueError:
        logger.warning(
            "Voiceover regroup skipped; the stored regroup settings are invalid."
        )
        return {"regrouped_groups": 0}
    if regroup["speech_block_generation_mode"] != "passage":
        return {"regrouped_groups": 0}
    try:
        max_chars = max(1, int(tts_settings.get("speech_block_max_chars") or 220))
    except (TypeError, ValueError):
        max_chars = 220

    first_pass = _load_first_pass(handler, run_id)
    if first_pass is None:
        return {"regrouped_groups": 0}
    source_cuts, edited_known = _load_source_cuts(
        handler, session_id, source_revision_id
    )
    if edited_known and source_cuts is None:
        # Known cut-derived input but the persisted keep_ranges mapping is
        # missing or uncertain: regrouping anywhere could bridge a removed
        # section, so select nothing and keep every first-pass take.
        logger.warning(
            "Voiceover regroup skipped; the edited source cut mapping is unavailable."
        )
        return {
            "regrouped_groups": 0,
            "regroup_rejected": len(first_pass.passages),
            "regroup_status": "edited_source_cuts_unknown",
        }
    selection = select_regroup_candidates(
        first_pass.passages,
        max_chars=max_chars,
        max_mismatch_ms=regroup["speech_block_regroup_max_mismatch_ms"],
        max_mismatch_percent=regroup["speech_block_regroup_max_mismatch_percent"],
        max_gap_ms=regroup["speech_block_regroup_max_gap_ms"],
        max_passages=regroup["speech_block_regroup_max_passages"],
        max_boundary_shift_ms=regroup["speech_block_regroup_max_boundary_shift_ms"],
        source_cut_positions=source_cuts,
    )
    if selection.rejected:
        logger.info(
            "Voiceover regroup rejected %d passage(s): %s.",
            len(selection.rejected),
            selection.rejected,
        )
    if not selection.groups:
        return {"regrouped_groups": 0, "regroup_rejected": len(selection.rejected)}

    stop = RepairCancellation(handler, run_id, cancel_event)
    current_ids = {passage.key: passage.key for passage in first_pass.passages}
    take_ids = dict(first_pass.take_ids)
    current_run_id = run_id
    current_revision_id = source_revision_id
    regrouped = 0
    rejected = len(selection.rejected)

    for index, group in enumerate(selection.groups):
        if stop.is_set():
            break
        progress(
            index / len(selection.groups),
            f"Checking voiceover regroup {index + 1} of {len(selection.groups)}",
        )
        member_ids = [current_ids[key] for key in group]
        staged_run_id: str | None = None
        staged_revision_id: str | None = None
        repair_status = "failed"
        repair_reason: str | None = "generation_failed"
        try:
            with handler.database.immediate_session() as session:
                choices, source_take_signature = _selection_state(
                    session, current_revision_id
                )
                if not take_ids or choices != take_ids:
                    # A choice made after the first pass wins over the audio
                    # used to select this group.
                    break
                source_signature = plan_signature(session, current_revision_id)
                members = [
                    session.get(GenerationSegment, member_id)
                    for member_id in member_ids
                ]
                if any(
                    member is None or member.plan_revision_id != current_revision_id
                    for member in members
                ):
                    raise ValueError("The regroup candidates changed during staging.")
                staged_revision_id, merged_id, lineage = _stage_group_revision(
                    session,
                    session_id,
                    current_revision_id,
                    [member for member in members if member is not None],
                    group_index=index,
                    run_id=run_id,
                    batch_snapshot=batch_snapshot,
                    max_chars=max_chars,
                )
                child_snapshot = deepcopy(snapshot)
                child_snapshot["speech_plan_revision_id"] = staged_revision_id
                child_snapshot["regroup_parent_run_id"] = run_id
                freeze_speech_snapshot(
                    session,
                    staged_revision_id,
                    child_snapshot,
                    explicit=True,
                )
                child_snapshot["generation_audio_identities"] = plan_audio_identities(
                    session, staged_revision_id, child_snapshot
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
                    plan_revision_id=staged_revision_id,
                    source_generation_run_id=current_run_id,
                    job_id=source_job_id,
                    sequence_number=sequence,
                    operation="generate",
                    status="queued",
                    settings_snapshot_json=child_snapshot,
                    settings_hash=stable_hash(child_snapshot),
                )
                session.add(staged)
                session.flush()
                staged_run_id = staged.id
                # Reuse the original takes for every untouched passage: every
                # staged take without a run is a preservation clone of an
                # unchanged segment (merge replacements intentionally carry
                # no take mapping), so pointing them at this run lets resume
                # skip them instead of regenerating them.
                clones = list(
                    session.scalars(
                        select(AudioTake)
                        .join(
                            GenerationSegment,
                            GenerationSegment.id == AudioTake.generation_segment_id,
                        )
                        .where(
                            GenerationSegment.plan_revision_id == staged_revision_id,
                            AudioTake.generation_run_id.is_(None),
                        )
                    ).all()
                )
                for clone in clones:
                    clone.generation_run_id = staged.id
            progress(
                index / len(selection.groups),
                f"Regenerating voiceover group {index + 1} of {len(selection.groups)}",
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
            with handler.database.session() as session:
                merged_take = session.scalar(
                    select(AudioTake)
                    .join(Artifact, Artifact.id == AudioTake.artifact_id)
                    .where(
                        AudioTake.generation_segment_id == merged_id,
                        AudioTake.generation_run_id == staged_run_id,
                        AudioTake.status == "completed",
                        Artifact.state == "current",
                    )
                    .order_by(AudioTake.created_at.desc(), AudioTake.id.desc())
                )
                total_duration = (
                    int(merged_take.duration_ms)
                    if merged_take is not None and merged_take.duration_ms is not None
                    else -1
                )
            group_span = max(
                passage.end_ms
                for passage in first_pass.passages
                if passage.key in group
            ) - min(
                passage.start_ms
                for passage in first_pass.passages
                if passage.key in group
            )
            if not regenerated_group_fits(
                total_duration,
                group_span,
                max_mismatch_ms=regroup["speech_block_regroup_max_mismatch_ms"],
                max_mismatch_percent=regroup[
                    "speech_block_regroup_max_mismatch_percent"
                ],
            ):
                # Unsuitable regeneration: retain the original takes. The
                # staged revision and run stay in history but are never
                # activated or referenced.
                repair_status, repair_reason = "not_applied", "duration_misfit"
                rejected += 1
                continue
            with handler.database.immediate_session() as session:
                plan = session.scalar(
                    select(GenerationPlan).where(
                        GenerationPlan.session_id == session_id
                    )
                )
                if (
                    stop.is_set()
                    or plan.active_revision_id != current_revision_id
                    or plan_signature(session, current_revision_id) != source_signature
                    or _selection_state(session, current_revision_id)[1]
                    != source_take_signature
                ):
                    repair_status, repair_reason = (
                        ("stopped", "generation_stopped")
                        if stop.is_set()
                        else ("not_applied", "selection_changed")
                    )
                    break
                plan.active_revision_id = staged_revision_id
                plan.updated_at = utcnow()
                mark_output_assemblies_stale(
                    session, session_id, cancel_active=True, jobs=handler.jobs
                )
                _record_repair_outcome(session, staged_revision_id, "applied")
                record_accepted_repair(session, staged_revision_id)
            repair_status, repair_reason = "applied", None
            current_ids = {
                original: lineage[current][0]
                for original, current in current_ids.items()
                if current in lineage and lineage[current]
            }
            with handler.database.session() as session:
                take_ids = _selection_state(session, staged_revision_id)[0]
            current_revision_id = staged_revision_id
            current_run_id = staged_run_id
            regrouped += 1
        except Exception:
            if stop.is_set():
                repair_status, repair_reason = "stopped", "generation_stopped"
            logger.warning(
                "Optional voiceover regroup stopped; the last selected plan and audio remain usable.",
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
        "regrouped_groups": regrouped,
        "regrouped_generation_run_id": current_run_id,
        "regrouped_plan_revision_id": current_revision_id,
        "regroup_rejected": rejected,
    }
