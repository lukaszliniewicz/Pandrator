"""Caller-transaction helpers for immutable project sound plan mutations."""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Artifact, GenerationSegment, SoundVariant, new_id
from .settings_policy import RevisionConflict
from .sound_project_models import ProjectSoundCue, ProjectSoundPlan
from .sound_project_queries import MAX_CUES
from .sound_project_schemas import SoundAnchor, SoundPlayback


def assert_revision(plan: ProjectSoundPlan | None, expected: int) -> None:
    actual = plan.revision if plan is not None else 0
    if expected != actual:
        raise RevisionConflict(
            f"Sound plan revision changed: expected {expected}, current {actual}."
        )


def require_variant(session: Session, variant_id: str) -> SoundVariant:
    variant = session.get(SoundVariant, variant_id)
    if variant is None or variant.state != "ready" or not variant.artifact_id:
        raise ValueError("Choose a ready sound variant with a managed artifact.")
    artifact = session.get(Artifact, variant.artifact_id)
    if (
        artifact is None
        or artifact.kind != "audio"
        or artifact.role != "sound.original"
        or artifact.state != "current"
        or artifact.session_id is not None
    ):
        raise ValueError("Sound variants must reference a current global audio artifact.")
    return variant


def normalize_placement(
    session: Session,
    anchor: SoundAnchor,
    playback: SoundPlayback,
    workflow_kind: str,
    speech_revision_id: str | None,
) -> tuple[dict[str, Any], dict[str, Any], str]:
    mode = playback.mode or ("insert" if workflow_kind == "audiobook" else "overlay")
    if workflow_kind in {"voiceover", "subtitles"} and mode == "insert":
        raise ValueError("Insert mode is unavailable for this timed workflow; select overlay.")
    if anchor.kind == "source_time":
        if workflow_kind not in {"voiceover", "subtitles", "media_edit"}:
            raise ValueError("Source-time anchors require a timed workflow.")
        if mode != "overlay":
            raise ValueError("Source-time anchors require overlay mode.")
    if mode == "insert" and playback.offset_ms < 0:
        raise ValueError("Insert offset must be nonnegative.")
    if anchor.segment_id is not None:
        segment = session.scalar(
            select(GenerationSegment.id).where(
                GenerationSegment.id == anchor.segment_id,
                GenerationSegment.plan_revision_id == speech_revision_id,
            )
        )
        if segment is None:
            raise ValueError("Anchor segment does not belong to the active speech plan revision.")
    status = "needs_placement" if anchor.kind in {"after_word", "next_pause"} else "ready"
    return (
        anchor.model_dump(exclude_none=True),
        {**playback.model_dump(exclude_none=True), "mode": mode},
        status,
    )


def cue_data(cue: ProjectSoundCue) -> dict[str, Any]:
    return {
        "logical_id": cue.logical_id,
        "variant_id": cue.variant_id,
        "anchor_json": deepcopy(cue.anchor_json),
        "playback_json": deepcopy(cue.playback_json),
        "included": cue.included,
        "placement_status": cue.placement_status,
    }


def latest_cues(session: Session, plan: ProjectSoundPlan | None) -> list[dict[str, Any]]:
    if plan is None:
        return []
    return [
        cue_data(cue)
        for cue in session.scalars(
            select(ProjectSoundCue)
            .where(ProjectSoundCue.plan_id == plan.id)
            .order_by(ProjectSoundCue.logical_id)
        )
    ]


def write_version(
    session: Session,
    session_id: str,
    old: ProjectSoundPlan | None,
    speech_revision_id: str | None,
    cues: list[dict[str, Any]],
) -> ProjectSoundPlan:
    if len(cues) > MAX_CUES:
        raise ValueError(f"A project can contain at most {MAX_CUES} authored sound cues.")
    plan = ProjectSoundPlan(
        session_id=session_id,
        revision=(old.revision if old else 0) + 1,
        speech_plan_revision_id=speech_revision_id,
    )
    session.add(plan)
    session.flush()
    session.add_all(ProjectSoundCue(plan_id=plan.id, **cue) for cue in cues)
    session.flush()
    from .sound_mix_state import invalidate_sound_mixes

    invalidate_sound_mixes(session, session_id)
    return plan


def new_cue(
    variant_id: str,
    anchor: dict[str, Any],
    playback: dict[str, Any],
    included: bool,
    placement_status: str,
) -> dict[str, Any]:
    return {
        "logical_id": new_id(),
        "variant_id": variant_id,
        "anchor_json": anchor,
        "playback_json": playback,
        "included": included,
        "placement_status": placement_status,
    }
