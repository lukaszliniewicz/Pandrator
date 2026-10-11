"""Read-only pins for bounded word-cue resolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from pandrator.logic.book_timing import alignment_identity
from pandrator.logic.sound_boundary_refinement import BOUNDARY_REFINEMENT_VERSION
from pandrator.runtime import DataPaths

from .models import Artifact, AudioTake, GenerationSegment
from .settings_policy import RevisionConflict
from .sound_project_commands import assert_revision, latest_cues
from .sound_project_queries import current_header
from .sound_resolution_freshness import active_rows, current_ready
from .workspace_settings import WorkspaceSettingsService

WORD_KINDS = frozenset({"after_word", "next_pause"})
REFINEMENT_SETTINGS = {
    "version": BOUNDARY_REFINEMENT_VERSION,
    "quiet_dbfs": -55.0,
    "min_quiet_ms": 80,
    "guard_ms": 30,
    "max_search_ms": 2000,
    "vad_threshold": 0.5,
    "vad_min_speech_ms": 40,
    "vad_min_silence_ms": 80,
    "vad_speech_pad_ms": 30,
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkItem:
    cue_id: str
    anchor: dict[str, Any]
    playback: dict[str, Any]
    reason: str | None
    take_id: str | None
    artifact_id: str | None
    take_hash: str | None
    transcript_hash: str | None
    transcript: str | None
    path: Path | None
    language: str | None
    identity: dict[str, Any] | None
    active_take_ids: tuple[str, ...] = ()
    take_status: str | None = None
    artifact_state: str | None = None

    def pin(self) -> dict[str, Any]:
        return {
            "cue_id": self.cue_id,
            "reason": self.reason,
            "take_id": self.take_id,
            "active_take_ids": list(self.active_take_ids),
            "take_status": self.take_status,
            "artifact_id": self.artifact_id,
            "artifact_state": self.artifact_state,
            "take_hash": self.take_hash,
            "transcript_hash": self.transcript_hash,
            "language": self.language,
            "alignment_identity_hash": digest(self.identity) if self.identity is not None else None,
        }


@dataclass(frozen=True, slots=True)
class SoundResolutionSnapshot:
    session_id: str
    plan_id: str
    revision: int
    speech_revision_id: str
    cue_ids: tuple[str, ...]
    remaining_count: int
    items: tuple[WorkItem, ...]
    settings: dict[str, Any]
    pins: dict[str, Any]


def capture(
    session: Session,
    session_id: str,
    expected_revision: int,
    cue_ids: list[str] | None,
    settings_service: WorkspaceSettingsService,
    paths: DataPaths,
) -> SoundResolutionSnapshot:
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer.")
    record, plan, active = current_header(session, session_id)
    assert_revision(plan, expected_revision)
    if plan is None or not active or plan.speech_plan_revision_id != active:
        raise RevisionConflict("Sound plan must match the active speech revision.")
    if record.trashed_at is not None or record.status == "purging":
        raise ValueError("Restore the session before resolving sound cues.")
    cues = latest_cues(session, plan)
    word_cues = {
        cue["logical_id"]: cue for cue in cues if cue["anchor_json"].get("kind") in WORD_KINDS
    }
    if cue_ids is None:
        included = [cue for cue in cues if cue["included"] and cue["logical_id"] in word_cues]
        segment_ids = {
            value for cue in included
            if isinstance(value := cue["anchor_json"].get("segment_id"), str)
        }
        rows = active_rows(session, segment_ids)
        pending = [
            cue["logical_id"]
            for cue in included if cue["placement_status"] == "needs_placement"
        ]
        stale = [
            cue["logical_id"] for cue in included
            if cue["placement_status"] == "ready"
            and not current_ready(cue, rows, active, session_id)
        ]
        eligible = pending + stale
        chosen = eligible[:20]
        remaining = len(eligible) - len(chosen)
    else:
        if (
            not isinstance(cue_ids, list)
            or not 1 <= len(cue_ids) <= 20
            or any(not isinstance(value, str) for value in cue_ids)
            or len(set(cue_ids)) != len(cue_ids)
        ):
            raise ValueError("Supply one through twenty distinct cue IDs.")
        if any(value not in word_cues for value in cue_ids):
            raise ValueError("Every requested cue must be a current word cue.")
        chosen, remaining = list(cue_ids), 0
    if not chosen:
        raise ValueError("There are no word cues to resolve.")
    resolved, _ = settings_service.resolve_in_session(session, session_id, ["stt"])
    stt = dict(resolved["stt"])
    items: list[WorkItem] = []
    for cue_id in chosen:
        cue = word_cues[cue_id]
        anchor = dict(cue["anchor_json"])
        segment_id = anchor.get("segment_id")
        segment = (
            session.get(GenerationSegment, segment_id) if isinstance(segment_id, str) else None
        )
        reason = None
        if segment is None or segment.plan_revision_id != active or segment.removed:
            reason = "anchor_segment_unavailable"
        takes = (
            list(
                session.scalars(
                    select(AudioTake)
                    .where(
                        AudioTake.generation_segment_id == segment_id, AudioTake.is_active.is_(True)
                    )
                    .order_by(AudioTake.id)
                )
            )
            if reason is None
            else []
        )
        if reason is None and len(takes) != 1:
            reason = "missing_active_take" if not takes else "ambiguous_active_take"
        take = takes[0] if len(takes) == 1 else None
        artifact = (
            session.get(Artifact, take.artifact_id)
            if take is not None and take.artifact_id
            else None
        )
        if reason is None and (
            take is None
            or take.status != "completed"
            or artifact is None
            or artifact.kind != "audio"
            or artifact.state != "current"
            or artifact.session_id != session_id
            or not artifact.content_hash
        ):
            reason = "active_take_unavailable"
        transcript = (
            (artifact.metadata_json or {}).get("synthesized_text") if artifact is not None else None
        )
        if reason is None and (not isinstance(transcript, str) or not transcript.strip()):
            reason = "spoken_transcript_unavailable"
        if (
            reason is None
            and segment is not None
            and transcript != (segment.optimized_text or segment.text)
        ):
            reason = "spoken_transcript_mismatch"
        language = (
            str(segment.language or stt.get("original_language") or stt.get("language") or "")
            if segment is not None
            else ""
        )
        runtime = {**stt, "original_language": language} if language else stt
        identity = (
            alignment_identity(runtime, transcript)
            if reason is None and isinstance(transcript, str)
            else None
        )
        path = (
            paths.managed_path(artifact.relative_path)
            if reason is None and artifact is not None
            else None
        )
        items.append(
            WorkItem(
                cue_id,
                anchor,
                dict(cue["playback_json"]),
                reason,
                take.id if take else None,
                artifact.id if artifact else None,
                artifact.content_hash if artifact else None,
                hashlib.sha256(transcript.encode("utf-8")).hexdigest()
                if isinstance(transcript, str)
                else None,
                transcript if isinstance(transcript, str) else None,
                path,
                language or None,
                identity,
                tuple(take_row.id for take_row in takes),
                take.status if take else None,
                artifact.state if artifact else None,
            )
        )
    pins = {
        "session_id": session_id,
        "sound_plan_id": plan.id,
        "sound_plan_revision": plan.revision,
        "speech_revision_id": active,
        "cue_ids": chosen,
        "remaining_count": remaining,
        "items": [item.pin() for item in items],
        "refinement_settings": REFINEMENT_SETTINGS,
    }
    return SoundResolutionSnapshot(
        session_id,
        plan.id,
        plan.revision,
        active,
        tuple(chosen),
        remaining,
        tuple(items),
        stt,
        pins,
    )
