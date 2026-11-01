"""Freeze export subtitle track identities without changing display projection."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .artifact_selection import ROLE_TO_STAGE, selected_artifacts
from .models import Artifact
from .workflow_output_context import OutputWorkflowContext, authorize_output_input

SUBTITLE_TRACK_ROLES = ("correction", "media_edit_subtitles", "transcription", "translation")
_TRACK_FIELDS = frozenset({"role", "artifact_id", "content_hash", "revision_id"})


def collect_export_roles(
    current: Sequence[Artifact], selections: Mapping[str, Artifact]
) -> dict[str, Artifact]:
    """Preserve the export worker's current rows plus selected-role overrides."""
    by_role: dict[str, Artifact] = {}
    for item in current:
        by_role.setdefault(item.role, item)
    for item in selections.values():
        by_role[item.role] = item
    return by_role


def _revision_id(artifact: Artifact) -> Any:
    metadata = artifact.metadata_json
    return metadata.get("revision_id") if isinstance(metadata, dict) else None


def _validated_tracks(snapshot: Any) -> list[dict[str, Any]]:
    malformed = "The queued subtitle track snapshot is malformed; submit the export again."
    if (
        not isinstance(snapshot, dict)
        or set(snapshot) != {"version", "tracks"}
        or type(snapshot.get("version")) is not int
        or snapshot["version"] != 1
        or not isinstance(snapshot.get("tracks"), list)
        or len(snapshot["tracks"]) > len(SUBTITLE_TRACK_ROLES)
    ):
        raise ValueError(malformed)
    tracks: list[dict[str, Any]] = []
    roles: set[str] = set()
    artifact_ids: set[str] = set()
    for track in snapshot["tracks"]:
        if not isinstance(track, dict) or set(track) != _TRACK_FIELDS:
            raise ValueError(malformed)
        role, artifact_id = track["role"], track["artifact_id"]
        if (
            not isinstance(role, str)
            or role not in SUBTITLE_TRACK_ROLES
            or role in roles
            or not isinstance(artifact_id, str)
            or not artifact_id.strip()
            or artifact_id in artifact_ids
            or any(
                value is not None and not isinstance(value, str)
                for value in (track["content_hash"], track["revision_id"])
            )
        ):
            raise ValueError(malformed)
        roles.add(role)
        artifact_ids.add(artifact_id)
        tracks.append(track)
    return tracks


def capture_subtitle_track_snapshot(session: Session, session_id: str) -> dict[str, Any]:
    """Capture the existing worker selection using database metadata only."""
    current = list(
        session.scalars(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.state == "current",
                Artifact.role.in_(SUBTITLE_TRACK_ROLES),
            )
            .order_by(Artifact.created_at.desc(), Artifact.id.desc())
        ).all()
    )
    # Keep siblings of the relevant stages: the legacy fallback chooses one
    # artifact per stage, even when multiple roles share that stage.
    stages = {ROLE_TO_STAGE[role] for role in SUBTITLE_TRACK_ROLES}
    fallback_roles = tuple(sorted(role for role, stage in ROLE_TO_STAGE.items() if stage in stages))
    fallback = list(
        session.scalars(
            select(Artifact)
            .where(
                Artifact.session_id == session_id,
                Artifact.state == "current",
                Artifact.role.in_(fallback_roles),
            )
            .order_by(Artifact.created_at.desc())
        ).all()
    )
    by_role = collect_export_roles(current, selected_artifacts(session, session_id, fallback))
    snapshot = {
        "version": 1,
        "tracks": [
            {
                "role": role,
                "artifact_id": by_role[role].id,
                "content_hash": by_role[role].content_hash,
                "revision_id": _revision_id(by_role[role]),
            }
            for role in SUBTITLE_TRACK_ROLES
            if role in by_role
        ],
    }
    _validated_tracks(snapshot)
    return snapshot


def resolve_subtitle_track_snapshot(
    context: OutputWorkflowContext,
    session: Session,
    session_id: str,
    snapshot: Any,
    current: Sequence[Artifact],
) -> dict[str, Artifact]:
    """Resolve exact retained pins in the worker's existing database unit."""
    tracks = _validated_tracks(snapshot)
    known = {item.id: item for item in current}
    missing = [track["artifact_id"] for track in tracks if track["artifact_id"] not in known]
    if missing:
        known.update(
            (item.id, item)
            for item in session.scalars(select(Artifact).where(Artifact.id.in_(missing))).all()
        )
    pinned: dict[str, Artifact] = {}
    for track in tracks:
        artifact = known.get(track["artifact_id"])
        if artifact is None or artifact.state == "deleted" or artifact.session_id != session_id:
            raise ValueError(
                "A subtitle track captured by this export is unavailable in this session; "
                "submit the export again."
            )
        authorize_output_input(context, session, artifact.id)
        if (
            artifact.role != track["role"]
            or artifact.content_hash != track["content_hash"]
            or _revision_id(artifact) != track["revision_id"]
        ):
            raise ValueError(
                "A subtitle track captured by this export changed; submit the export again."
            )
        pinned[track["role"]] = artifact
    return pinned
