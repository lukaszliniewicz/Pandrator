"""Transaction-local sound-plan revision hooks for topology changes and forks."""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import GenerationPlan, GenerationPlanRevision, GenerationSegment
from .sound_project_commands import latest_cues, write_version
from .sound_project_queries import current_header, plan_header


def rebase_sound_plan(
    session: Session,
    session_id: str,
    new_speech_revision_id: str,
    boundary_mapping: dict[tuple[str, str], str | None],
) -> dict[str, Any] | None:
    """Create a sound-plan version aligned to an activated speech revision."""

    _record, old, active = current_header(session, session_id)
    if old is None:
        return None
    if not isinstance(boundary_mapping, dict):
        raise ValueError("boundary_mapping must be a mapping.")
    if new_speech_revision_id != active:
        raise ValueError("Rebase target must be the active speech plan revision.")
    if (
        session.scalar(
            select(GenerationPlanRevision.id)
            .join(GenerationPlan, GenerationPlan.id == GenerationPlanRevision.plan_id)
            .where(
                GenerationPlanRevision.id == new_speech_revision_id,
                GenerationPlan.session_id == session_id,
            )
        )
        is None
    ):
        raise ValueError("Speech revision does not belong to this project.")
    cues = latest_cues(session, old)
    if not cues and old.speech_plan_revision_id == new_speech_revision_id:
        return None
    for cue in cues:
        anchor = cue["anchor_json"]
        kind = anchor["kind"]
        if kind == "source_time":
            continue
        if kind in {"after_word", "next_pause"}:
            anchor.pop("start", None)
            anchor.pop("end", None)
            cue["placement_status"] = "needs_placement"
            continue
        old_segment = anchor.get("segment_id")
        mapped = (
            None
            if old_segment is None
            else boundary_mapping.get((old_segment, kind))
        )
        if mapped is None:
            cue["placement_status"] = "needs_placement"
            continue
        if (
            session.scalar(
                select(GenerationSegment.id).where(
                    GenerationSegment.id == mapped,
                    GenerationSegment.plan_revision_id == new_speech_revision_id,
                )
            )
            is None
        ):
            raise ValueError("Mapped segment does not belong to the new speech revision.")
        anchor["segment_id"] = mapped
        cue["placement_status"] = "ready"
    plan = write_version(session, session_id, old, new_speech_revision_id, cues)
    return {
        **plan_header(session_id, plan, new_speech_revision_id),
        "cue_count": len(cues),
    }


def clone_sound_plan(
    session: Session,
    source_id: str,
    target_id: str,
    source_segment_map: dict[str, str],
    carry_media_assets: bool = True,
) -> dict[str, Any]:
    """Copy authored sound cues into a fork without copying sound assets."""

    _source_record, source, _source_active = current_header(session, source_id)
    _target_record, target, target_active = current_header(session, target_id)
    if source_id == target_id or target is not None:
        raise ValueError("Target project must have no sound plan.")
    if not isinstance(source_segment_map, dict) or type(carry_media_assets) is not bool:
        raise ValueError("Invalid clone options.")
    if source is None:
        return {**plan_header(target_id, None, target_active), "cue_count": 0}
    cues = latest_cues(session, source)
    for cue in cues:
        anchor = cue["anchor_json"]
        # The fork map covers document segments; generation plans are not forked.
        anchor["origin_session_id"] = anchor.get("origin_session_id", source_id)
        if anchor["kind"] != "source_time" or not carry_media_assets:
            cue["placement_status"] = "needs_placement"
    plan = write_version(session, target_id, None, target_active, cues)
    return {**plan_header(target_id, plan, target_active), "cue_count": len(cues)}
