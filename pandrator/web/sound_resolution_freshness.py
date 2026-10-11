"""Read-only, batched freshness checks for authored word cue resolutions."""

from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from .models import Artifact, AudioTake, GenerationSegment

ActiveRow = tuple[GenerationSegment, AudioTake | None, Artifact | None]


def active_rows(session: Session, segment_ids: set[str]) -> dict[str, list[ActiveRow]]:
    """Fetch current take and artifact metadata in bounded SQL batches."""
    rows: dict[str, list[ActiveRow]] = {}
    ordered = sorted(segment_ids)
    for start in range(0, len(ordered), 400):
        query = (
            select(GenerationSegment, AudioTake, Artifact)
            .outerjoin(AudioTake, and_(
                AudioTake.generation_segment_id == GenerationSegment.id,
                AudioTake.is_active.is_(True),
            ))
            .outerjoin(Artifact, Artifact.id == AudioTake.artifact_id)
            .where(GenerationSegment.id.in_(ordered[start:start + 400]))
        )
        for segment, take, artifact in session.execute(query):
            rows.setdefault(segment.id, []).append((segment, take, artifact))
    return rows


def current_ready(
    cue: dict[str, Any], rows: dict[str, list[ActiveRow]], active: str, session_id: str
) -> bool:
    """Only a ready pin to this exact completed take, artifact, and text is current."""
    anchor = cue["anchor_json"]
    resolution = anchor.get("resolution")
    matches = rows.get(anchor.get("segment_id"), [])
    if not isinstance(resolution, dict) or len(matches) != 1:
        return False
    segment, take, artifact = matches[0]
    if (
        segment.plan_revision_id != active or segment.removed
        or take is None or take.status != "completed"
        or artifact is None or artifact.kind != "audio" or artifact.state != "current"
        or artifact.session_id != session_id or not artifact.content_hash
    ):
        return False
    transcript = (artifact.metadata_json or {}).get("synthesized_text")
    if (
        not isinstance(transcript, str) or not transcript.strip()
        or transcript != (segment.optimized_text or segment.text)
    ):
        return False
    return (
        resolution.get("status") == "ready"
        and resolution.get("take_id") == take.id
        and resolution.get("artifact_id") == artifact.id
        and resolution.get("take_content_hash") == artifact.content_hash
        and resolution.get("transcript_hash")
        == hashlib.sha256(transcript.encode("utf-8")).hexdigest()
    )
