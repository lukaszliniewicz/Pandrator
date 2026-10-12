"""Bounded read projections for versioned project sound cues."""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Artifact, GenerationPlan, SessionRecord, SoundItem, SoundVariant
from .sound_project_models import ProjectSoundCue, ProjectSoundPlan

MAX_CUES = 1000


def current_header(
    session: Session, session_id: str
) -> tuple[SessionRecord, ProjectSoundPlan | None, str | None]:
    latest = (
        select(ProjectSoundPlan.id)
        .where(ProjectSoundPlan.session_id == SessionRecord.id)
        .order_by(ProjectSoundPlan.revision.desc())
        .limit(1)
        .correlate(SessionRecord)
        .scalar_subquery()
    )
    row = session.execute(
        select(SessionRecord, ProjectSoundPlan, GenerationPlan.active_revision_id)
        .outerjoin(ProjectSoundPlan, ProjectSoundPlan.id == latest)
        .outerjoin(GenerationPlan, GenerationPlan.session_id == SessionRecord.id)
        .where(SessionRecord.id == session_id)
    ).one_or_none()
    if row is None:
        raise KeyError(session_id)
    return (row[0], row[1], row[2])


def cue_rows(
    session: Session,
    plan_id: str,
    *,
    cursor: str | None = None,
    logical_id: str | None = None,
    limit: int | None = None,
    segment_ids: list[str] | None = None,
) -> list[tuple[Any, ...]]:
    statement = (
        select(ProjectSoundCue, SoundVariant, SoundItem, Artifact)
        .join(SoundVariant, SoundVariant.id == ProjectSoundCue.variant_id)
        .outerjoin(SoundItem, SoundItem.id == SoundVariant.item_id)
        .outerjoin(Artifact, Artifact.id == SoundVariant.artifact_id)
        .where(ProjectSoundCue.plan_id == plan_id)
    )
    if cursor is not None:
        statement = statement.where(ProjectSoundCue.logical_id > cursor)
    if logical_id is not None:
        statement = statement.where(ProjectSoundCue.logical_id == logical_id)
    if segment_ids is not None:
        statement = statement.where(
            func.json_extract(ProjectSoundCue.anchor_json, "$.segment_id").in_(segment_ids)
        )
    statement = statement.order_by(ProjectSoundCue.logical_id)
    if limit is not None:
        statement = statement.limit(limit)
    return [
        (row[0], row[1], row[2], row[3]) for row in session.execute(statement).all()
    ]


def cue_summary(row: tuple[Any, ...]) -> dict[str, Any]:
    cue, variant, item, artifact = row
    provenance = variant.provenance_json if isinstance(variant.provenance_json, dict) else {}
    name = (
        item.name
        if item is not None
        else str(provenance.get("display_name") or provenance.get("prompt") or "Sound")[:255]
    )
    return {
        "logical_id": cue.logical_id,
        "variant_id": cue.variant_id,
        "anchor": dict(cue.anchor_json),
        "playback": dict(cue.playback_json),
        "included": bool(cue.included),
        "placement_status": cue.placement_status,
        "sound": {
            "name": name,
            "artifact_id": variant.artifact_id,
            "duration_seconds": variant.duration_seconds,
            "content_hash": artifact.content_hash if artifact is not None else None,
            "status": variant.state,
        },
    }


def plan_header(
    session_id: str, plan: ProjectSoundPlan | None, active_revision_id: str | None
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "revision": plan.revision if plan is not None else 0,
        "plan_id": plan.id if plan is not None else None,
        "speech_plan_revision_id": plan.speech_plan_revision_id
        if plan is not None
        else active_revision_id,
    }
